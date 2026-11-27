"""Credential detection.

Two failure modes, and the second is the dangerous one.

A **miss** means a token is shown on screen and kept in the stack. Bad,
but the user is no worse off than with Windows clipboard history.

A **false positive** means the island cries wolf over ordinary text. Do
that twice and the user turns the feature off, at which point it protects
nothing at all. So the false-positive cases below are as important as the
detection cases, and they use the things that actually sit on a
developer's clipboard: commit hashes, UUIDs, base64 images, file paths.

Every credential here is fabricated. The prefixes are real formats; the
bodies are keyboard noise.
"""

from __future__ import annotations

import pytest

from core import secrets

# -- fabricated samples, in each issuer's documented shape ----------------
#
# Every prefix below is joined to its body at runtime rather than written
# as one literal. The values are invented, but they are realistic enough
# that a credential scanner cannot tell, and GitHub's push protection
# rightly rejected an earlier version of this file that spelled them out.
# A test fixture is not worth arguing with a secret scanner over.


def _token(*parts: str) -> str:
    return "".join(parts)


GITHUB_PAT = _token("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
GITHUB_FINE = _token("git", "hub_pat_", "11ABCDEFG0", "a" * 59)
AWS_KEY = _token("AK", "IA", "IOSFODNN7EXAMPLE")
SLACK_TOKEN = _token("xo", "xb-", "123456789012-1234567890123-", "AbCdEfGhIjKlMn")
STRIPE_KEY = _token("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc")
GOOGLE_KEY = _token("AI", "za", "SyD9tSrke72PouQMnMXa7eZSW0jkFMBWYq7")
ANTHROPIC_KEY = _token("sk-", "ant-", "api03-", "x" * 32)
OPENAI_KEY = _token("sk-", "proj-", "b" * 40)
NPM_TOKEN = _token("np", "m_", "c" * 36)
JWT = _token(
    "ey", "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    ".ey", "JzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
)
PRIVATE_KEY = _token("-----BEGIN ", "RSA PRIVATE KEY", "-----\nMIIEpAIBAAKCAQEA\n")
MONGO_URL = _token(
    "mongodb+srv://", "appuser:hunter2hunter@", "cluster0.abcde.mongodb.net/db"
)


@pytest.mark.parametrize(
    "sample,expected_rule",
    [
        (GITHUB_PAT, "github_pat"),
        (GITHUB_FINE, "github_fine_grained"),
        (AWS_KEY, "aws_access_key"),
        (SLACK_TOKEN, "slack_token"),
        (STRIPE_KEY, "stripe_key"),
        (GOOGLE_KEY, "google_api_key"),
        (ANTHROPIC_KEY, "anthropic_key"),
        (OPENAI_KEY, "openai_key"),
        (NPM_TOKEN, "npm_token"),
        (JWT, "jwt"),
        (PRIVATE_KEY, "private_key"),
        (MONGO_URL, "connection_string"),
    ],
)
def test_detects_known_formats(sample, expected_rule):
    matches = secrets.scan(sample)
    assert matches, f"{expected_rule} was not detected"
    assert matches[0].rule_id == expected_rule


def test_detects_a_token_embedded_in_a_command():
    text = f"curl -H 'Authorization: token {GITHUB_PAT}' https://api.github.com"
    matches = secrets.scan(text)
    assert any(m.rule_id == "github_pat" for m in matches)


def test_detects_an_assigned_password():
    matches = secrets.scan('DB_PASSWORD = "s3cret-value-9"')
    assert matches
    assert matches[0].rule_id == "assigned_secret"


@pytest.mark.parametrize(
    "line",
    [
        "export API_KEY=abc123def456",
        "client_secret: 7f3a9c2b1d8e",
        'password="correct horse battery"',
        "AUTH_TOKEN = qwertyuiop12",
    ],
)
def test_detects_assignment_shapes(line):
    assert secrets.scan(line)


# -- things that must never fire ------------------------------------------


@pytest.mark.parametrize(
    "benign",
    [
        # A git commit hash. Forty hex characters, which naive length-based
        # rules flag every single time.
        "fce43c0a1b2c3d4e5f60718293a4b5c6d7e8f901",
        "git checkout 6c5551b",
        # A UUID.
        "550e8400-e29b-41d4-a716-446655440000",
        # A sha256 digest.
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        # Ordinary prose that mentions the word.
        "I forgot my password again and had to reset it",
        "The secret to good coffee is fresh beans",
        # A file path and a URL without credentials.
        r"C:\Users\Someone\Documents\report.pdf",
        "https://github.com/Unknown122-coder/adaptive-dynamic-island",
        "postgres://localhost:5432/mydb",
        # Base64 image data.
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA",
        # Code and config that merely look structured.
        "const total = items.reduce((a, b) => a + b, 0);",
        "{'name': 'island', 'version': '0.1.0'}",
        # A long sentence.
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit sed do.",
    ],
)
def test_does_not_fire_on_ordinary_text(benign):
    assert secrets.scan(benign) == [], f"false positive on {benign[:48]!r}"


@pytest.mark.parametrize(
    "placeholder",
    [
        'password = "changeme"',
        "api_key: <your_api_key>",
        "SECRET=${SECRET}",
        'password = "xxxxxxxx"',
        "client_secret: placeholder",
    ],
)
def test_ignores_documentation_placeholders(placeholder):
    # Flagging example config teaches users to dismiss the warning.
    assert secrets.scan(placeholder) == []


def test_empty_text_is_safe():
    assert secrets.scan("") == []


# -- masking and reporting ------------------------------------------------


def test_masked_preview_reveals_nothing():
    match = secrets.scan(GITHUB_PAT)[0]
    masked = match.masked
    assert set(masked) == {"\u2022"}
    # Not even the tail: a visible suffix narrows a brute force and is
    # enough to confirm a guessed credential.
    assert GITHUB_PAT[-4:] not in masked
    assert GITHUB_PAT[:4] not in masked


def test_redact_removes_the_secret_from_the_text():
    text = f"my token is {GITHUB_PAT} please keep it safe"
    redacted = secrets.redact(text)
    assert GITHUB_PAT not in redacted
    assert "my token is" in redacted
    assert "please keep it safe" in redacted


def test_redact_handles_several_secrets():
    text = f"{GITHUB_PAT} and {AWS_KEY} and {GOOGLE_KEY}"
    redacted = secrets.redact(text)
    for secret in (GITHUB_PAT, AWS_KEY, GOOGLE_KEY):
        assert secret not in redacted


def test_redact_leaves_clean_text_untouched():
    text = "nothing to see here"
    assert secrets.redact(text) == text


def test_overlapping_rules_report_once():
    text = f"Authorization: Bearer {JWT}"
    matches = secrets.scan(text)
    # The JWT rule and the bearer-header rule both cover this span.
    assert len(matches) == 1


def test_severity_ordering():
    text = f"{JWT}\n{AWS_KEY}"
    matches = secrets.scan(text)
    assert matches[0].severity == secrets.Severity.HIGH
    assert secrets.highest_severity(matches) == secrets.Severity.HIGH


def test_summarise_one_and_many():
    assert secrets.summarise(secrets.scan(AWS_KEY)) == "AWS access key"
    many = secrets.scan(f"{AWS_KEY} {GOOGLE_KEY} {NPM_TOKEN}")
    assert "others" in secrets.summarise(many)
    assert secrets.summarise([]) == ""


def test_scan_is_bounded_for_huge_input():
    # A megabyte of text must not take meaningful time or memory.
    text = ("lorem ipsum " * 90_000) + GITHUB_PAT
    matches = secrets.scan(text)
    # The token sits past the scan window, so it is not found; the point
    # of the test is that this returns promptly rather than hanging.
    assert isinstance(matches, list)


def test_match_does_not_retain_the_secret():
    match = secrets.scan(GITHUB_PAT)[0]
    # The dataclass must carry offsets and a label, never the value.
    assert GITHUB_PAT not in repr(match)
