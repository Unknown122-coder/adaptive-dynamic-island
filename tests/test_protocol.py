"""Live Activity protocol parsing.

The producers are other people's scripts, so the parser is the app's
exposed surface. These tests pin down what it accepts, what it rejects,
and what it sanitises.
"""

from __future__ import annotations

import json

import pytest

from ipc import protocol


def line(**fields) -> str:
    return json.dumps(fields)


def test_push_round_trips():
    command = protocol.parse_command(
        line(op="push", id="build", title="docker build", progress=0.5)
    )
    assert command.op == "push"
    assert command.id == "build"
    assert command.title == "docker build"
    assert command.progress == pytest.approx(0.5)


def test_unknown_op_is_rejected():
    with pytest.raises(protocol.ProtocolError, match="unknown op"):
        protocol.parse_command(line(op="destroy", id="x"))


def test_push_requires_a_title():
    with pytest.raises(protocol.ProtocolError, match="requires a title"):
        protocol.parse_command(line(op="push", id="build"))


def test_push_requires_an_id():
    with pytest.raises(protocol.ProtocolError, match="requires an id"):
        protocol.parse_command(line(op="push", title="docker build"))


def test_invalid_json_is_rejected():
    with pytest.raises(protocol.ProtocolError, match="invalid json"):
        protocol.parse_command("{not json")


def test_non_object_is_rejected():
    with pytest.raises(protocol.ProtocolError, match="json object"):
        protocol.parse_command("[1, 2, 3]")


def test_oversized_command_is_rejected():
    payload = b"x" * (protocol.MAX_LINE_BYTES + 1)
    with pytest.raises(protocol.ProtocolError, match="too large"):
        protocol.parse_command(payload)


def test_progress_is_clamped_to_one():
    command = protocol.parse_command(
        line(op="push", id="b", title="t", progress=7.5)
    )
    assert command.progress == pytest.approx(1.0)


def test_negative_progress_means_indeterminate():
    command = protocol.parse_command(
        line(op="push", id="b", title="t", progress=-0.3)
    )
    assert command.progress == -1.0


def test_progress_must_be_numeric():
    with pytest.raises(protocol.ProtocolError, match="must be a number"):
        protocol.parse_command(
            line(op="push", id="b", title="t", progress="half")
        )


def test_control_characters_are_stripped_from_text():
    command = protocol.parse_command(
        line(op="push", id="b", title="one\ntwo\x00three")
    )
    # A newline would break the island's single-line layout, and would
    # also break the framing if it were ever echoed back.
    assert "\n" not in command.title
    assert "\x00" not in command.title


def test_title_is_truncated():
    command = protocol.parse_command(
        line(op="push", id="b", title="x" * 500)
    )
    assert len(command.title) == 120


def test_id_cannot_escape_its_namespace():
    # A dot would let a producer address another provider's content.
    command = protocol.parse_command(
        line(op="push", id="privacy.faces", title="t")
    )
    assert "." not in command.id


def test_invalid_urgency_is_rejected():
    with pytest.raises(protocol.ProtocolError, match="urgency must be"):
        protocol.parse_command(
            line(op="push", id="b", title="t", urgency="screaming")
        )


def test_invalid_status_is_rejected():
    with pytest.raises(protocol.ProtocolError, match="status must be"):
        protocol.parse_command(line(op="done", id="b", status="exploded"))


def test_ttl_has_a_floor():
    command = protocol.parse_command(
        line(op="push", id="b", title="t", ttl=0.001)
    )
    assert command.ttl == pytest.approx(1.0)


def test_responses_are_single_line_json():
    for payload in (protocol.ok(id="b"), protocol.error("nope")):
        assert "\n" not in payload
        decoded = json.loads(payload)
        assert decoded["v"] == protocol.PROTOCOL_VERSION


def test_tokens_are_unique_and_long():
    tokens = {protocol.new_token() for _ in range(32)}
    assert len(tokens) == 32
    assert all(len(t) >= 32 for t in tokens)
