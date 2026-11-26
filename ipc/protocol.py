"""Wire protocol for the Live Activity API.

Newline-delimited JSON over a loopback TCP socket. One JSON object per
line, one response object per line. That choice is deliberate: a named pipe
would be marginally more Windows-native, but JSON over TCP can be driven
from Python, Node, Go, PowerShell, curl or a shell one-liner without a
binding, and the whole point of this API is that anything a developer
already runs can report into the island.

Access control: the server binds to 127.0.0.1 only, and every command must
carry a token read from a file in the user's roaming profile. That stops
another user's session on the same machine from pushing content, while
keeping the barrier low enough that a build script can use it.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024

# Operations the server understands.
OP_PUSH = "push"
OP_DONE = "done"
OP_RETRACT = "retract"
OP_LIST = "list"
OP_PING = "ping"
OPERATIONS = {OP_PUSH, OP_DONE, OP_RETRACT, OP_LIST, OP_PING}

VALID_URGENCY = {"neutral", "info", "success", "warning", "critical"}
VALID_STATUS = {"ok", "fail", "cancelled"}


class ProtocolError(ValueError):
    """Malformed or rejected command."""


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Command:
    op: str
    token: str = ""
    id: str = ""
    title: str | None = None
    detail: str | None = None
    glyph: str | None = None
    progress: float | None = None
    urgency: str | None = None
    status: str | None = None
    ttl: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _clean_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    # Control characters would corrupt the island's single-line layout and
    # a newline would break the framing on the way back out.
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def parse_command(line: str | bytes) -> Command:
    """Parse one protocol line. Raises :class:`ProtocolError`."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError("command too large")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"invalid utf-8: {exc}") from exc

    try:
        raw = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"invalid json: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("command must be a json object")

    op = str(raw.get("op", "")).lower().strip()
    if op not in OPERATIONS:
        raise ProtocolError(f"unknown op {op!r}")

    command = Command(op=op, token=str(raw.get("token", "")))

    if op in (OP_PUSH, OP_DONE, OP_RETRACT):
        activity_id = _clean_text(raw.get("id"), 64)
        if not activity_id:
            raise ProtocolError(f"op {op!r} requires an id")
        # Keys are namespaced by the server; keep ids free of the separator
        # so one producer cannot address another's content.
        command.id = activity_id.replace(".", "-").replace(" ", "-")

    command.title = _clean_text(raw.get("title"), 120)
    command.detail = _clean_text(raw.get("detail"), 160)
    command.glyph = _clean_text(raw.get("glyph"), 32)

    if op == OP_PUSH and not command.title:
        raise ProtocolError("op 'push' requires a title")

    progress = raw.get("progress")
    if progress is not None:
        try:
            value = float(progress)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("progress must be a number") from exc
        if value < 0:
            command.progress = -1.0  # indeterminate
        else:
            command.progress = min(value, 1.0)

    urgency = raw.get("urgency")
    if urgency is not None:
        text = str(urgency).lower().strip()
        if text not in VALID_URGENCY:
            raise ProtocolError(
                f"urgency must be one of {sorted(VALID_URGENCY)}"
            )
        command.urgency = text

    status = raw.get("status")
    if status is not None:
        text = str(status).lower().strip()
        if text not in VALID_STATUS:
            raise ProtocolError(f"status must be one of {sorted(VALID_STATUS)}")
        command.status = text

    ttl = raw.get("ttl")
    if ttl is not None:
        try:
            command.ttl = max(1.0, float(ttl))
        except (TypeError, ValueError) as exc:
            raise ProtocolError("ttl must be a number of seconds") from exc

    return command


def ok(**fields: Any) -> str:
    payload = {"ok": True, "v": PROTOCOL_VERSION}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def error(message: str, **fields: Any) -> str:
    payload = {"ok": False, "v": PROTOCOL_VERSION, "error": str(message)}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def encode_command(command: dict[str, Any]) -> bytes:
    return (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8")
