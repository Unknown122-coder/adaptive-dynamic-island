"""Detect credentials in copied text.

Windows clipboard history makes a secret *worse*: once you copy a token,
Win+V keeps it, and with cloud sync enabled it leaves the machine. The
island's clipboard feature exists mainly to undo that. It notices when
something you copied looks like a credential, refuses to display it, and
offers to wipe the clipboard.

The detection rules follow one principle: **prefer a known prefix over a
shape**. A rule that matches "forty characters of base64" will fire on
hashes, UUID lists, minified JavaScript and Git object ids, and a leak
detector that cries wolf gets switched off within a day. Rules that key on
an issuer's own prefix, like ``ghp_`` or ``AKIA``, are nearly impossible to
trigger by accident. Where no prefix exists, the rule requires a nearby
keyword such as ``password =`` instead of matching the value alone.

Nothing here is written to disk, logged, or sent anywhere. The matched text
is never retained: a match records only its position, its kind, and a
masked preview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

# Scanning is linear in the text size and runs on every clipboard change,
# so very large pastes are truncated. A credential in the first 128 KB is
# the realistic case; beyond that the user is copying a file, not a token.
MAX_SCAN_CHARS = 128 * 1024


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class Rule:
    id: str
    label: str
    severity: Severity
    pattern: re.Pattern[str]
    # When set, the capturing group holding the secret itself. Group 0
    # otherwise. Used so a keyword rule does not report the keyword as part
    # of the credential.
    group: int = 0


def _c(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


# Rules are ordered most specific first; scanning stops overlapping matches
# so a JWT inside an Authorization header is reported once, as a JWT.
RULES: tuple[Rule, ...] = (
    Rule(
        "private_key",
        "Private key",
        Severity.HIGH,
        _c(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ),
    Rule(
        "aws_access_key",
        "AWS access key",
        Severity.HIGH,
        _c(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b"),
    ),
    Rule(
        "github_pat",
        "GitHub token",
        Severity.HIGH,
        _c(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ),
    Rule(
        "github_fine_grained",
        "GitHub token",
        Severity.HIGH,
        _c(r"\bgithub_pat_[A-Za-z0-9_]{60,255}\b"),
    ),
    Rule(
        "slack_token",
        "Slack token",
        Severity.HIGH,
        _c(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    Rule(
        "slack_webhook",
        "Slack webhook",
        Severity.HIGH,
        _c(r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_-]{20,}"),
    ),
    Rule(
        "stripe_key",
        "Stripe key",
        Severity.HIGH,
        _c(r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    ),
    Rule(
        "google_api_key",
        "Google API key",
        Severity.HIGH,
        _c(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    Rule(
        "anthropic_key",
        "Anthropic API key",
        Severity.HIGH,
        _c(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ),
    Rule(
        "openai_key",
        "OpenAI API key",
        Severity.HIGH,
        _c(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    Rule(
        "npm_token",
        "npm token",
        Severity.HIGH,
        _c(r"\bnpm_[A-Za-z0-9]{36}\b"),
    ),
    Rule(
        "sendgrid_key",
        "SendGrid key",
        Severity.HIGH,
        _c(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    ),
    Rule(
        "connection_string",
        "Database URL with password",
        Severity.HIGH,
        _c(
            r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp|ftp)"
            r"://[^\s:/@]+:[^\s@]{3,}@[^\s/]+",
        ),
    ),
    Rule(
        "jwt",
        "JSON web token",
        Severity.MEDIUM,
        # Three base64url segments, the first two starting with the encoded
        # "{" that every JWT header and payload begins with.
        _c(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    Rule(
        "bearer_header",
        "Authorization header",
        Severity.MEDIUM,
        _c(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})"),
        group=1,
    ),
    Rule(
        "basic_auth_header",
        "Authorization header",
        Severity.MEDIUM,
        _c(r"(?i)\bbasic\s+([A-Za-z0-9+/=]{16,})"),
        group=1,
    ),
    Rule(
        "assigned_secret",
        "Password or secret",
        Severity.MEDIUM,
        # Keyword-anchored, because the value alone has no distinguishing
        # shape. Requires an assignment so prose like "my password is
        # terrible" does not trip it.
        _c(
            # The leading lookbehind rather than \b is deliberate: \b does
            # not fire inside DB_PASSWORD or APP_CLIENT_SECRET, because an
            # underscore is a word character, and those prefixed names are
            # the most common shape of all in a .env file.
            r"(?i)(?<![A-Za-z])(?:password|passwd|pwd|secret|api[_-]?key"
            r"|access[_-]?key|auth[_-]?token|client[_-]?secret"
            r"|private[_-]?key)(?![A-Za-z])"
            r"\s*[:=]\s*[\"']?([^\s\"',;]{6,})",
        ),
        group=1,
    ),
    Rule(
        "otp_code",
        "One-time code",
        Severity.LOW,
        _c(
            r"(?i)\b(?:otp|one[\s-]?time|verification|security|auth)"
            r"\s*(?:code|pin)?\D{0,12}\b(\d{4,8})\b",
        ),
        group=1,
    ),
)

# Values that match a rule's shape but are obviously placeholders. Flagging
# documentation and example config teaches users to ignore the warning.
_PLACEHOLDERS = frozenset(
    {
        "password",
        "changeme",
        "your_password",
        "yourpassword",
        "secret",
        "your_secret",
        "xxxxxx",
        "xxxxxxxx",
        "placeholder",
        "example",
        "redacted",
        "null",
        "none",
        "true",
        "false",
        "undefined",
        "<password>",
        "${password}",
        "******",
        "########",
    }
)


@dataclass(frozen=True)
class SecretMatch:
    """One detection. Deliberately does not retain the secret text."""

    rule_id: str
    label: str
    severity: Severity
    start: int
    end: int
    length: int

    @property
    def masked(self) -> str:
        """A fixed-width mask. Never reveals any of the value.

        Showing the last few characters is common practice and is a real
        leak: it narrows a brute force and is enough to confirm a guessed
        credential. The label already says which service it belongs to,
        which is the only thing a user actually needs here.
        """
        return "\u2022" * min(max(self.length, 6), 16)


def _is_placeholder(value: str) -> bool:
    stripped = value.strip().strip("\"'`")
    if stripped.lower() in _PLACEHOLDERS:
        return True
    # "xxxx", "****", "<...>", "{{...}}" and friends.
    if len(set(stripped)) <= 2 and len(stripped) >= 4:
        return True
    return stripped.startswith(("<", "{{", "${", "%(")) and stripped.endswith(
        (">", "}}", "}", ")s")
    )


def scan(text: str) -> list[SecretMatch]:
    """Find credentials in ``text``, most severe first.

    Overlapping matches are collapsed so one credential is reported once.
    """
    if not text:
        return []
    window = text[:MAX_SCAN_CHARS]

    found: list[SecretMatch] = []
    claimed: list[tuple[int, int]] = []

    for rule in RULES:
        for match in rule.pattern.finditer(window):
            start, end = match.span(rule.group)
            if start < 0 or end <= start:
                continue
            value = match.group(rule.group)
            if _is_placeholder(value):
                continue
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            found.append(
                SecretMatch(
                    rule_id=rule.id,
                    label=rule.label,
                    severity=rule.severity,
                    start=start,
                    end=end,
                    length=end - start,
                )
            )

    found.sort(key=lambda m: (-int(m.severity), m.start))
    return found


def redact(text: str, matches: list[SecretMatch] | None = None) -> str:
    """Return ``text`` with every detected secret replaced by a mask."""
    if matches is None:
        matches = scan(text)
    if not matches:
        return text
    out = text
    # Replace from the end so earlier offsets stay valid.
    for match in sorted(matches, key=lambda m: m.start, reverse=True):
        out = out[: match.start] + match.masked + out[match.end :]
    return out


def summarise(matches: list[SecretMatch]) -> str:
    """A one-line description for the island."""
    if not matches:
        return ""
    labels: list[str] = []
    for match in matches:
        if match.label not in labels:
            labels.append(match.label)
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{labels[0]} and {len(labels) - 1} others"


def highest_severity(matches: list[SecretMatch]) -> Severity | None:
    if not matches:
        return None
    return max(m.severity for m in matches)
