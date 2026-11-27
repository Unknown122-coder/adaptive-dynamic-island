"""Classifying and describing copied text.

The bar for this feature is that it says something the user did not
already know. "Copied 47 characters" fails that bar, so these tests check
the enrichment, not just the label.
"""

from __future__ import annotations

import json

import pytest

from core import textkind


def describe(text: str) -> textkind.Insight:
    return textkind.describe(text)


# ---- colours -------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_hex",
    [
        ("#5B8CFF", "#5B8CFF"),
        ("#5b8cff", "#5B8CFF"),
        ("#abc", "#AABBCC"),
        ("rgb(91, 140, 255)", "#5B8CFF"),
        ("rgba(91,140,255,0.5)", "#5B8CFF"),
    ],
)
def test_colours_are_recognised_and_normalised(value, expected_hex):
    insight = describe(value)
    assert insight.kind is textkind.Kind.COLOR
    assert insight.title.startswith(expected_hex)
    # The accent is what lets the island paint the actual swatch.
    assert insight.accent == expected_hex


def test_colour_alpha_is_kept_in_the_label_but_not_the_swatch():
    insight = describe("#5B8CFF80")
    assert insight.title == "#5B8CFF80"
    assert insight.accent == "#5B8CFF"


@pytest.mark.parametrize(
    "value,expected_word",
    [
        ("#000000", "black"),
        ("#FFFFFF", "white"),
        ("#808080", "grey"),
        ("#FF0000", "red"),
        ("#00FF00", "green"),
    ],
)
def test_colour_gets_a_human_name(value, expected_word):
    assert expected_word in describe(value).detail


def test_not_every_hash_is_a_colour():
    assert describe("#hashtag").kind is not textkind.Kind.COLOR
    assert describe("#12345").kind is not textkind.Kind.COLOR


# ---- urls and emails -----------------------------------------------------


def test_url_shows_its_host():
    insight = describe("https://github.com/Unknown122-coder/adaptive-dynamic-island")
    assert insight.kind is textkind.Kind.URL
    assert insight.title == "github.com"


def test_url_without_a_path():
    assert describe("https://example.com").title == "example.com"


def test_email_shows_its_domain():
    insight = describe("someone@example.com")
    assert insight.kind is textkind.Kind.EMAIL
    assert insight.detail == "example.com"


# ---- json ----------------------------------------------------------------


def test_valid_json_object_reports_key_count():
    insight = describe(json.dumps({"a": 1, "b": 2, "c": 3}))
    assert insight.kind is textkind.Kind.JSON
    assert "3 keys" in insight.title


def test_single_key_is_singular():
    assert "1 key" in describe('{"only": 1}').title


def test_json_array_reports_length():
    assert "4 items" in describe("[1, 2, 3, 4]").title


def test_malformed_json_is_called_out():
    # Catching a truncated copy here saves a debugging detour later.
    insight = describe('{"a": 1, "b":')
    assert insight.kind is textkind.Kind.JSON
    assert insight.title == "Invalid JSON"
    assert insight.glyph == "warning"


# ---- timestamps and numbers ---------------------------------------------


def test_unix_seconds_become_a_date():
    insight = describe("1764201600")
    assert insight.kind is textkind.Kind.TIMESTAMP
    assert "2025" in insight.title or "2026" in insight.title
    assert "in s" in insight.detail


def test_unix_milliseconds_become_a_date():
    insight = describe("1764201600000")
    assert insight.kind is textkind.Kind.TIMESTAMP
    assert "in ms" in insight.detail


def test_a_plain_number_is_not_a_timestamp():
    assert describe("42").kind is textkind.Kind.NUMBER
    assert describe("1234567890123456").kind is not textkind.Kind.TIMESTAMP


def test_large_numbers_get_thousands_separators():
    assert describe("1234567").detail == "1,234,567"


def test_year_like_numbers_are_not_timestamps():
    # Ten digits is the giveaway, not "looks numeric".
    assert describe("2026").kind is textkind.Kind.NUMBER


# ---- paths and code ------------------------------------------------------


def test_windows_path_shows_the_filename():
    insight = describe(r"C:\Users\Someone\Documents\report.pdf")
    assert insight.kind is textkind.Kind.PATH
    assert insight.title == "report.pdf"


def test_unc_path_is_recognised():
    assert describe(r"\\server\share\file.txt").kind is textkind.Kind.PATH


def test_posix_path_is_recognised():
    assert describe("/usr/local/bin/python").title == "python"


def test_code_is_recognised_by_shape():
    snippet = (
        "import sys\n"
        "def main():\n"
        "    total = sum(x for x in range(10));\n"
        "    return total;\n"
    )
    insight = describe(snippet)
    assert insight.kind is textkind.Kind.CODE
    assert "lines" in insight.title


def test_prose_is_not_mistaken_for_code():
    prose = (
        "This is a paragraph of ordinary writing.\n"
        "It spans a couple of lines and mentions nothing technical.\n"
    )
    assert describe(prose).kind is textkind.Kind.TEXT


# ---- plain text ----------------------------------------------------------


def test_single_line_text_shows_itself():
    insight = describe("remember to buy milk")
    assert insight.kind is textkind.Kind.TEXT
    assert insight.title == "remember to buy milk"
    assert "4 words" in insight.detail


def test_long_single_line_is_truncated():
    insight = describe("word " * 200)
    assert len(insight.title) <= 61


def test_multiline_text_reports_counts():
    insight = describe("one\ntwo\nthree")
    assert "3 lines" in insight.title


def test_empty_text_is_handled():
    assert describe("").title == "Empty"


def test_very_large_input_is_bounded():
    insight = describe("x" * (textkind.MAX_INSPECT_CHARS * 2))
    assert insight is not None
