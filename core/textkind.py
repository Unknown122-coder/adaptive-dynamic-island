"""Work out what a piece of copied text actually is, and say something
useful about it.

A clipboard tool that reports "copied 47 characters" is not worth the
screen space. The value is in telling the user something they would
otherwise have to go and check: that the JSON they copied is malformed,
that the number is last Tuesday, that the hex colour is this shade of
blue, that the URL points at a different host than they expected.

Pure functions over a string. No file reads, no network, no clipboard.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

MAX_INSPECT_CHARS = 64 * 1024


class Kind(StrEnum):
    COLOR = "color"
    URL = "url"
    EMAIL = "email"
    JSON = "json"
    PATH = "path"
    TIMESTAMP = "timestamp"
    NUMBER = "number"
    CODE = "code"
    TEXT = "text"


_COLOR_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_COLOR_FUNC = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*"
    r"(?:,\s*[\d.]+\s*)?\)$",
    re.IGNORECASE,
)
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)[^\n\r]*$")
_POSIX_PATH = re.compile(r"^(?:~|/)[^\n\r:*?\"<>|]*$")
_INTEGER = re.compile(r"^-?\d{1,19}$")
_NUMERIC = re.compile(r"^-?\d{1,15}(?:\.\d+)?$")

# Lines that strongly suggest source code rather than prose.
_CODE_HINTS = (
    re.compile(r"^\s*(?:def|class|import|from|function|const|let|var|public"
               r"|private|package|#include|using)\s", re.MULTILINE),
    re.compile(r"[;{}]\s*$", re.MULTILINE),
    re.compile(r"^\s*(?://|#|/\*|\*)\s", re.MULTILINE),
)

# Unix timestamps outside this window are almost certainly just numbers.
_EPOCH_MIN = 946_684_800      # 2000-01-01
_EPOCH_MAX = 4_102_444_800    # 2100-01-01


@dataclass(frozen=True)
class Insight:
    """What to show for a piece of copied text."""

    kind: Kind
    title: str
    detail: str
    glyph: str
    # Hex colour the island should use as its accent, for a colour swatch.
    accent: str | None = None


def _normalise_hex(value: str) -> str:
    """Expand #abc to #aabbcc and upper-case it."""
    text = value.strip()
    body = text[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return "#" + body.upper()


def _contrast_name(hex_colour: str) -> str:
    """A rough human name for a colour, for the secondary line."""
    body = hex_colour.lstrip("#")
    r, g, b = (int(body[i : i + 2], 16) for i in (0, 2, 4))
    # Perceived luminance, the usual sRGB weighting.
    luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    if max(r, g, b) - min(r, g, b) < 18:
        shade = "black" if luminance < 0.1 else (
            "white" if luminance > 0.9 else "grey"
        )
        return shade
    if r >= g and r >= b:
        hue = "red" if g <= b else "orange"
    elif g >= r and g >= b:
        hue = "green"
    else:
        hue = "blue" if r <= g else "purple"
    tone = "dark " if luminance < 0.35 else ("light " if luminance > 0.7 else "")
    return f"{tone}{hue}"


def _try_color(text: str) -> Insight | None:
    if _COLOR_HEX.match(text):
        normalised = _normalise_hex(text)
        opaque = normalised[:7]
        return Insight(
            kind=Kind.COLOR,
            title=normalised,
            detail=_contrast_name(opaque),
            glyph="sparkle",
            accent=opaque,
        )
    match = _COLOR_FUNC.match(text)
    if match:
        channels = [min(255, int(c)) for c in match.groups()[:3]]
        hex_colour = "#" + "".join(f"{c:02X}" for c in channels)
        return Insight(
            kind=Kind.COLOR,
            title=hex_colour,
            detail=_contrast_name(hex_colour),
            glyph="sparkle",
            accent=hex_colour,
        )
    return None


def _try_url(text: str) -> Insight | None:
    if not _URL.match(text):
        return None
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(text)
        host = parts.netloc or text
    except ValueError:
        host = text
    path = parts.path if parts.path not in ("", "/") else ""
    detail = f"{parts.scheme}{path}" if path else parts.scheme
    return Insight(Kind.URL, host, detail[:80], "download")


def _try_timestamp(text: str) -> Insight | None:
    if not _INTEGER.match(text):
        return None
    value = int(text)
    digits = len(text.lstrip("-"))
    if digits == 13:
        seconds = value / 1000.0
    elif digits == 10:
        seconds = float(value)
    else:
        return None
    if not (_EPOCH_MIN <= seconds <= _EPOCH_MAX):
        return None
    try:
        moment = datetime.fromtimestamp(seconds, tz=UTC).astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    unit = "ms" if digits == 13 else "s"
    return Insight(
        kind=Kind.TIMESTAMP,
        title=moment.strftime("%d %b %Y, %H:%M:%S"),
        detail=f"unix timestamp in {unit}",
        glyph="clock",
    )


def _try_number(text: str) -> Insight | None:
    if not _NUMERIC.match(text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    rendered = f"{int(value):,}" if value.is_integer() else f"{value:,}"
    return Insight(Kind.NUMBER, text, rendered, "text")


def _try_json(text: str) -> Insight | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError as exc:
        # An invalid JSON paste is worth flagging: it is usually a
        # truncated copy, and finding that out now saves a debugging detour.
        message = str(exc).split(":")[0]
        return Insight(
            kind=Kind.JSON,
            title="Invalid JSON",
            detail=message[:70],
            glyph="warning",
        )
    if isinstance(parsed, dict):
        count = len(parsed)
        noun = "key" if count == 1 else "keys"
        preview = ", ".join(list(parsed)[:3])
        return Insight(
            kind=Kind.JSON,
            title=f"JSON object · {count} {noun}",
            detail=preview[:70],
            glyph="clipboard",
        )
    count = len(parsed)
    noun = "item" if count == 1 else "items"
    return Insight(Kind.JSON, f"JSON array · {count} {noun}", "", "clipboard")


def _try_path(text: str) -> Insight | None:
    if "\n" in text:
        return None
    if not (_WINDOWS_PATH.match(text) or _POSIX_PATH.match(text)):
        return None
    name = re.split(r"[\\/]", text.rstrip("\\/"))[-1] or text
    return Insight(Kind.PATH, name, text[:80], "clipboard")


def _try_email(text: str) -> Insight | None:
    if not _EMAIL.match(text):
        return None
    return Insight(Kind.EMAIL, text, text.split("@", 1)[1], "text")


def _looks_like_code(text: str) -> bool:
    if "\n" not in text:
        return False
    return sum(1 for hint in _CODE_HINTS if hint.search(text)) >= 2


def describe(text: str) -> Insight:
    """Classify ``text`` and build what the island should show for it."""
    if not text:
        return Insight(Kind.TEXT, "Empty", "", "clipboard")

    window = text[:MAX_INSPECT_CHARS]
    single = window.strip()
    one_line = "\n" not in single

    if one_line:
        for attempt in (
            _try_color,
            _try_url,
            _try_email,
            _try_path,
            _try_timestamp,
        ):
            found = attempt(single)
            if found is not None:
                return found

    found = _try_json(window)
    if found is not None:
        return found

    if one_line:
        found = _try_number(single)
        if found is not None:
            return found

    lines = window.count("\n") + 1
    words = len(window.split())
    if _looks_like_code(window):
        return Insight(
            kind=Kind.CODE,
            title=f"Code · {lines} lines",
            detail=_first_line(single),
            glyph="text",
        )

    if one_line:
        return Insight(
            kind=Kind.TEXT,
            title=_first_line(single, limit=60),
            detail=f"{words} words",
            glyph="clipboard",
        )
    return Insight(
        kind=Kind.TEXT,
        title=f"{lines} lines · {words} words",
        detail=_first_line(single),
        glyph="clipboard",
    )


def _first_line(text: str, limit: int = 70) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    line = " ".join(line.split())
    return line[:limit] + ("…" if len(line) > limit else "")
