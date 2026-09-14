"""Dedicated suite for redact.redact_text.

Every case asserts the *exact* output string (fail-loud: no substring-only
checks where full equality is possible), and every input in the corpus is
re-run through the idempotence test at the bottom.

Where actual behavior on an edge differs from what a reader might expect,
the case pins the actual behavior with a comment rather than changing src.
"""

from __future__ import annotations

import pytest

from self_improve.redact import redact_text

# ---------------------------------------------------------------------------
# Corpus: (case id, input, expected exact output)
# ---------------------------------------------------------------------------

PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA7fake0keyMaterial\n"
    "abc123def456\n"
    "-----END RSA PRIVATE KEY-----"
)

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
)

SECRET_CASES: list[tuple[str, str, str]] = [
    # -- AWS access key ids ------------------------------------------------
    ("aws_akia", "AKIAABCDEFGHIJKLMNOP", "[REDACTED:aws_key]"),
    ("aws_asia", "ASIA0123456789ABCDEF", "[REDACTED:aws_key]"),
    # -- Google API keys: AIza + 35 chars ----------------------------------
    (
        "google_api_key",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "[REDACTED:google_api_key]",
    ),
    # -- OpenAI/Anthropic-style sk- keys, incl. sk-ant- --------------------
    (
        "sk_key",
        "sk-proj-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv",
        "[REDACTED:sk_api_key]",
    ),
    (
        "sk_ant_key",
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "[REDACTED:sk_api_key]",
    ),
    # -- GitHub tokens: ghp_/gho_ classic and fine-grained -----------------
    (
        "ghp_token",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "[REDACTED:github_token]",
    ),
    (
        "gho_token",
        "gho_SYNTHETIC0123456789ABCDEFGHIJKLMNOPQ",
        "[REDACTED:github_token]",
    ),
    (
        "github_pat",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz12345",
        "[REDACTED:github_token]",
    ),
    # -- Slack tokens: xox[bpars]- ------------------------------------------
    (
        "xoxb_token",
        "xoxb-2444333222111-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
        "[REDACTED:slack_token]",
    ),
    (
        "xoxp_token",
        "xoxp-9876543210987-8765432109876-abcdefghij",
        "[REDACTED:slack_token]",
    ),
    # -- 3-segment JWTs -----------------------------------------------------
    ("jwt", JWT, "[REDACTED:jwt]"),
    # -- Emails -------------------------------------------------------------
    ("email", "case.reader+test@example.co.uk", "[REDACTED:email]"),
    # -- PEM blocks (multi-line; whole block collapses to one tag) ----------
    ("pem_block", PEM_BLOCK, "[REDACTED:pem_block]"),
    # -- Pure-hex digests >= 32 chars with real entropy ---------------------
    ("hex32_md5", "d41d8cd98f00b204e9800998ecf8427e", "[REDACTED:high_entropy]"),
    (
        "hex40_sha1",
        "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12",
        "[REDACTED:high_entropy]",
    ),
    (
        "hex64_sha256",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "[REDACTED:high_entropy]",
    ),
    (
        "hex64_upper",
        "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855",
        "[REDACTED:high_entropy]",
    ),
    # -- Generic high-entropy base64-ish run (3+ char classes, 1 slash) -----
    (
        "base64_secret",
        "Tt3rQ8xZk1mN4vB7yC2wD9eF6gH0jK5+Lp/qRs=",
        "[REDACTED:high_entropy]",
    ),
]

NON_SECRET_CASES: list[tuple[str, str]] = [
    # Uniform run: regression for a fixed bug. 'A' is a hex character, so
    # 'A'*2000 fullmatches the pure-hex regex — but its Shannon entropy is 0,
    # far below the 3.0 bits/char floor, so it must survive.
    ("uniform_run_A2000", "A" * 2000),
    ("uniform_run_a40", "a" * 40),
    # Absolute file paths: tokens with >= 2 slashes are kept as paths.
    ("abs_path", "/Users/example/Code/self-improve/src/self_improve/redact.py"),
    # One-slash identifier still survives: only 2 char classes and low entropy.
    ("one_slash_ident", "self-improve/some_module_name_goes_here_ok"),
    # Normal English prose.
    ("sentence", "The quick brown fox jumps over the lazy dog repeatedly today."),
    # Short hex ids (< 32 chars) never enter the entropy sweep.
    ("short_hex_id", "deadbeef1234"),
    ("hex31", "d41d8cd98f00b204e9800998ecf8427"),
    # UUIDs with dashes SURVIVE (actual behavior, pinned): the dashes break
    # the pure-hex fullmatch, and while lower+digit+dash gives 3 char
    # classes, a UUID's entropy is ~3.39 bits/char — just under the 3.5
    # bits/char threshold — so the entropy sweep keeps it.
    ("uuid_lower", "550e8400-e29b-41d4-a716-446655440000"),
    ("uuid_upper", "550E8400-E29B-41D4-A716-446655440000"),
    # Code identifiers: 2 char classes (lower + underscore), entropy ~3.75
    # < the 4.5 bits/char classes-independent threshold.
    ("code_ident", "some_long_function_name_here_that_is_long"),
    # Near-miss key shapes: too short / wrong case for the typed patterns,
    # and too short (or too low-entropy) for the generic sweep.
    ("sk_too_short", "sk-tooshort12345"),
    ("aws_lowercase", "akiaabcdefghijklmnop"),
]


# ---------------------------------------------------------------------------
# Typed placeholders per pattern class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [(t, e) for _, t, e in SECRET_CASES],
    ids=[i for i, _, _ in SECRET_CASES],
)
def test_secret_redacts_to_typed_placeholder(text: str, expected: str):
    assert redact_text(text) == expected


# ---------------------------------------------------------------------------
# Non-secrets survive verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [t for _, t in NON_SECRET_CASES],
    ids=[i for i, _ in NON_SECRET_CASES],
)
def test_non_secret_survives_unchanged(text: str):
    assert redact_text(text) == text


# ---------------------------------------------------------------------------
# Boundary: 31-char vs 32-char pure hex
# ---------------------------------------------------------------------------


def test_hex_length_boundary_31_vs_32():
    hex32 = "d41d8cd98f00b204e9800998ecf8427e"
    hex31 = hex32[:-1]
    assert len(hex31) == 31 and len(hex32) == 32
    # 31 chars is below the sweep's minimum-run length; 32 is redacted.
    assert redact_text(hex31) == hex31
    assert redact_text(hex32) == "[REDACTED:high_entropy]"


# ---------------------------------------------------------------------------
# Mixed text: surrounding prose is kept intact, byte for byte
# ---------------------------------------------------------------------------


def test_mixed_prose_aws_and_email():
    text = "Set AWS_KEY=AKIAABCDEFGHIJKLMNOP and email reader@example.com in the env."
    assert redact_text(text) == (
        "Set AWS_KEY=[REDACTED:aws_key] and email [REDACTED:email] in the env."
    )


def test_mixed_prose_jwt_bearer_header():
    text = f"Authorization: Bearer {JWT} done"
    assert redact_text(text) == "Authorization: Bearer [REDACTED:jwt] done"


def test_mixed_prose_hex_digest_in_sentence():
    text = "commit d41d8cd98f00b204e9800998ecf8427e was reverted"
    assert redact_text(text) == "commit [REDACTED:high_entropy] was reverted"


def test_mixed_prose_path_survives_in_sentence():
    text = "see /Users/example/Code/self-improve/tests/test_redact.py for details"
    assert redact_text(text) == text


def test_mixed_prose_pem_block_between_lines():
    text = f"here is the key:\n{PEM_BLOCK}\nplease rotate it"
    assert redact_text(text) == (
        "here is the key:\n[REDACTED:pem_block]\nplease rotate it"
    )


def test_mixed_multiple_secret_types_each_get_own_tag():
    text = (
        "creds: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 "
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
        "xoxb-2444333222111-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx end"
    )
    assert redact_text(text) == (
        "creds: [REDACTED:sk_api_key] "
        "[REDACTED:github_token] "
        "[REDACTED:slack_token] end"
    )


# ---------------------------------------------------------------------------
# Idempotence: redact(redact(x)) == redact(x) for every corpus input
# ---------------------------------------------------------------------------

_ALL_INPUTS: list[tuple[str, str]] = (
    [(i, t) for i, t, _ in SECRET_CASES]
    + list(NON_SECRET_CASES)
    + [
        (
            "mixed_prose",
            "Set AWS_KEY=AKIAABCDEFGHIJKLMNOP and email reader@example.com "
            f"plus {JWT} and\n{PEM_BLOCK}\nand digest "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.",
        ),
    ]
)


@pytest.mark.parametrize(
    "text",
    [t for _, t in _ALL_INPUTS],
    ids=[i for i, _ in _ALL_INPUTS],
)
def test_idempotent(text: str):
    once = redact_text(text)
    assert redact_text(once) == once


# Contexts a secret can appear in inside a real transcript. Regex boundaries
# are where redaction fails in practice: a pattern anchored on whitespace
# leaks the moment the secret is quoted, bracketed, comma-separated, or sits
# at a line edge.
_ADJACENCY_CONTEXTS = [
    "{}", "prefix {} suffix", '"{}"', "'{}'", "({})", "[{}]", "<{}>", "`{}`",
    "key={}", "key: {}", "Authorization: Bearer {}", "{}\n", "\n{}", "  {}  ",
    "a,{},b", "url=https://x.test/?t={}&z=1", '{{"k":"{}"}}', "x={};y=2",
    "...{}...", "{}.", "{}!", "#{}", "{}\t|", "line1\n{}\nline3",
]


def test_no_secret_survives_any_adjacency_context():
    """Every known secret shape stays redacted in every surrounding context.

    The per-shape tests above check each secret alone. This checks the thing
    that actually breaks: adjacency. Redaction is the boundary between the
    transcript corpus and an LLM prompt, so a leak here is not a formatting
    bug, and "it worked on the sample" is not the same as "the pattern has no
    boundary case".
    """
    leaks = []
    for text, _expected in [(t, e) for _, t, e in SECRET_CASES]:
        for ctx in _ADJACENCY_CONTEXTS:
            out = redact_text(ctx.format(text))
            if text in out:
                leaks.append((text[:24], ctx))
    assert not leaks, f"{len(leaks)} secret(s) survived redaction: {leaks[:5]}"


def test_a_figma_personal_access_token_is_redacted():
    """A short invented Figma credential is scrubbed without losing prose."""
    # Invented input. No credential was issued for this test.
    text = "here is my personal access token to fix it: figd_AbCdEf12-34_gh56IjKl78"
    out = redact_text(text)
    assert "figd_" not in out, out
    assert "AbCdEf12-34_gh56IjKl78" not in out, out
    assert "personal access token" in out, "redaction ate the surrounding prose"


def test_a_short_figma_token_below_the_entropy_floor_is_still_redacted():
    """An invented credential below the 32-character entropy floor is scrubbed.

    A specific pattern is the right fix rather than lowering that floor, which
    would redact ordinary long identifiers across the whole corpus.
    """
    out = redact_text("token: figd_shortbutstillatoken12")
    assert "figd_" not in out, out


def test_the_generic_entropy_rule_alone_would_have_missed_it():
    """Proves the claim above rather than asserting it.

    If this ever fails, the entropy floor changed and the specific pattern may
    no longer be load-bearing — check before deleting it.
    """
    import re

    from self_improve.redact import _ENTROPY_TOKEN_RE

    assert _ENTROPY_TOKEN_RE.search("figd_AbCdEf12-34_gh56Ij") is None, (
        "a 23-char token now matches the entropy rule; re-check the floor"
    )
