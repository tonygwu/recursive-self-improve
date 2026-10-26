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
    # -- AWS secret access key after its name (AWS's documented example) ----
    (
        "aws_secret_access_key",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "aws_secret_access_key = [REDACTED:aws_secret_key]",
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


# Invented and low in entropy, so only the typed pattern removes it.
_GLUED_GOOGLE_KEY = "AIza" + "A" * 35


@pytest.mark.parametrize(
    "text, expected",
    [
        (_GLUED_GOOGLE_KEY * 2, "[REDACTED:google_api_key]" * 2),
        ("key=" + _GLUED_GOOGLE_KEY * 4 + ";", "key=" + "[REDACTED:google_api_key]" * 4 + ";"),
        (_GLUED_GOOGLE_KEY + "AKIAABCDEFGHIJKLMNOP", "[REDACTED:google_api_key][REDACTED:aws_key]"),
    ],
    ids=["two_google_keys", "four_google_keys", "google_then_aws"],
)
def test_glued_keys_are_redacted_in_one_call(text, expected):
    """BL-35: the Google pattern has a fixed length and no end boundary. Text
    glued onto a key gains a word boundary only after the key becomes a
    placeholder, so a single pass left the next key for the next call.
    redact_text was not idempotent, and instruction-text collection aborted."""
    from self_improve import redact_legacy

    # Premise: the frozen single-pass rules are not idempotent on this input.
    legacy_once = redact_legacy.redact_text(text)
    assert redact_legacy.redact_text(legacy_once) != legacy_once
    once = redact_text(text)
    assert once == expected
    assert redact_text(once) == once


def test_a_pass_that_never_settles_raises_instead_of_returning(monkeypatch):
    """The pass bound only trips if a pattern breaks the termination argument."""
    from self_improve import redact

    monkeypatch.setattr(redact, "_redact_pass", lambda s: s + "x")
    with pytest.raises(RuntimeError, match="fixed point"):
        redact.redact_text("abc")


def test_every_glued_pair_of_known_secrets_is_idempotent():
    """Each secret shape directly followed by another, with no separator."""
    inputs = [t for _, t, _ in SECRET_CASES] + [_GLUED_GOOGLE_KEY]
    failures = []
    for first in inputs:
        for second in inputs:
            once = redact_text(first + second)
            if redact_text(once) != once:
                failures.append((first[:12], second[:12]))
    assert not failures, f"{len(failures)} glued pairs changed on a second call: {failures[:5]}"


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


# ---------------------------------------------------------------------------
# BL-03: slash-containing secrets. Fixture values are generated at run time so
# no new credential-shaped literal enters the tracked tree.
# ---------------------------------------------------------------------------

def _base64_with_two_slashes(seed: int, length: int = 40) -> str:
    import random
    import string

    rnd = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + "+"
    chars = [rnd.choice(alphabet) for _ in range(length)]
    chars[length // 3] = "/"
    chars[2 * length // 3] = "/"
    return "".join(chars)


@pytest.mark.parametrize("seed", range(25))
def test_base64_secret_with_two_slashes_is_redacted(seed):
    secret = _base64_with_two_slashes(seed)
    # A neutral name leaves the decision to the entropy sweep.
    assert redact_text(f"key = {secret}") == "key = [REDACTED:high_entropy]"
    # An AWS secret-key name lets the typed pattern decide first.
    text = f"aws_secret_access_key = {secret}"
    assert redact_text(text) == "aws_secret_access_key = [REDACTED:aws_secret_key]"


# The example secret access key in AWS's public documentation. It is not a
# credential. BL-03 named it as a value that survives redact_text.
AWS_DOCUMENTED_EXAMPLE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_the_entropy_sweep_alone_keeps_the_documented_aws_example():
    """Premise of the typed pattern: the sweep reads this value as a path.
    If this fails, the sweep became stricter (BACKLOG BL-62); check whether
    the aws_secret_key pattern is still load-bearing before changing it."""
    from self_improve import redact

    assert redact._is_high_entropy(AWS_DOCUMENTED_EXAMPLE_SECRET) is False
    text = f"key = {AWS_DOCUMENTED_EXAMPLE_SECRET}"
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "template",
    [
        "aws_secret_access_key = {}",
        "aws_secret_access_key={}\n",
        "AWS_SECRET_ACCESS_KEY={}",
        'export AWS_SECRET_ACCESS_KEY="{}"',
        "MY_AWS_SECRET_ACCESS_KEY={};",
        '{{"SecretAccessKey": "{}"}}',
        '{{\\"SecretAccessKey\\": \\"{}\\"}}',
        "SecretAccessKey: {}",
        "aws_secret_access_key: '{}'",
        "secretAccessKey => '{}'",
        "aws configure set aws_secret_access_key {}",
        "Secret access key: {}",
        "aws_secret_key = {}",
        # A closing quote or backtick after the name.
        "`AWS_SECRET_ACCESS_KEY`: {}",
        "`AWS_SECRET_ACCESS_KEY`=`{}`",
        "'aws_secret_access_key' => '{}'",
        # The prompt of "aws configure", with and without a stored default.
        "AWS Secret Access Key [None]: {}",
        "AWS Secret Access Key [****************EKEY]: {}",
        # The spaced name in other cases.
        "aws secret access key: {}",
        "AWS SECRET ACCESS KEY: {}",
        # A Markdown table cell and a builder call.
        "| AWS_SECRET_ACCESS_KEY | {} |",
        "| `AWS_SECRET_ACCESS_KEY` | `{}` |",
        '.secretAccessKey("{}")',
    ],
)
def test_the_documented_aws_example_secret_is_redacted_after_its_name(template):
    text = template.format(AWS_DOCUMENTED_EXAMPLE_SECRET)
    expected = template.format("[REDACTED:aws_secret_key]")
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Put aws_secret_access_key in ~/.aws/credentials, never in src/self_improve/config.py",
        "aws_secret_access_key: ~/.aws/credentials",
        "SecretAccessKey: /Users/example/Code/self-improve/repo-0/docs/DEPLOYMENT.md",
        "aws_secret_access_key_file = /run/secrets/aws/secret_access_key",
        "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}",
        "aws_secret_access_key = YOUR_SECRET_ACCESS_KEY_HERE",
        "AWS_SHARED_CREDENTIALS_FILE=/Users/example/Code/self-improve/repo-0/x/credentials",
        "`AWS_SECRET_ACCESS_KEY`: read it from ~/.aws/credentials, never from src/self_improve/config.py",
        "AWS Secret Access Key [None]: ",
        "AWS Secret Access Key [****************EKEY]: /Users/example/Code/self-improve/repo-0/docs/DEPLOYMENT.md",
        "| AWS_SECRET_ACCESS_KEY | Your secret access key. Required. |",
        "| `AWS_SECRET_ACCESS_KEY` | `~/.aws/credentials` |",
        "| AWS_SECRET_ACCESS_KEY | /Users/example/Code/self-improve/repo-0/x |",
        '.secretAccessKey(System.getenv("AWS_SECRET_ACCESS_KEY"))',
        "Rotate the AWS Secret Access Key (see docs/DEPLOYMENT.md) every 90 days.",
    ],
)
def test_paths_and_references_after_an_aws_secret_key_name_are_kept(text):
    assert redact_text(text) == text


def test_the_aws_pattern_needs_an_aws_secret_key_name():
    """A 40-character run of letters, digits and slashes is kept as a path
    unless an AWS secret-key name precedes it. That is also the pattern's
    cost: such a path right after the name is redacted."""
    path = "/Users/example/Code/selfimprove/src/abcd"
    assert len(path) == 40
    for text in (f"secret_key = {path}", f"access_key = {path}", f"opened {path} today"):
        assert redact_text(text) == text
    assert redact_text(f"aws_secret_access_key = {path}") == "aws_secret_access_key = [REDACTED:aws_secret_key]"


def test_a_41_character_path_after_an_aws_secret_key_name_is_kept():
    """An AWS secret access key has exactly 40 characters, so the typed
    pattern leaves a longer value to the entropy sweep, which keeps a path."""
    path = "/Users/example/Code/selfimprove/src/abcde"
    assert len(path) == 41
    for text in (f"aws_secret_access_key = {path}", f"AWS_SECRET_ACCESS_KEY={path}"):
        assert redact_text(text) == text


def test_refresh_token_with_a_double_slash_prefix_is_redacted():
    import random
    import string

    rnd = random.Random(11)
    tail = "".join(rnd.choice(string.ascii_letters + string.digits + "-_") for _ in range(96))
    token = "1//0g" + tail
    text = f'"refresh_token": "{token}"'
    assert redact_text(text) == '"refresh_token": "[REDACTED:high_entropy]"'


@pytest.mark.parametrize(
    "path",
    [
        "/Users/example/Code/self-improve/repo-0/tests/browser/verify_review_shortcuts_v2.py",
        "src/self_improve/dashboard/static/fonts/FiraMono-Regular.woff2",
        "~/Code/self-improve/repo-0/docs/dashboard-parity/CHARACTER_SHORTCUTS.md",
        "tests/browser/verify_class_evidence_read_states.py",
    ],
)
def test_ordinary_paths_with_mixed_classes_survive(path):
    text = f"edited {path} today"
    assert redact_text(text) == text


def test_slash_joined_camel_case_and_hex_ids_stay_paths():
    """Path shapes that were kept before BL-03 are still kept, so the rules
    change does not redact them in ordinary instruction text."""
    for text in (
        "see ProjectAlpha/SomeModuleName/AnotherCamelCaseName in the tree",
        "commits a1b2c3d/e4f5a6b/c7d8e9f/a0b1c2d/e3f4a5b/c6d7e8f landed",
    ):
        assert redact_text(text) == text


def _id_path_shapes(seed: int) -> list[str]:
    """Path shapes that carry generated IDs, as transcripts record them."""
    import random
    import uuid

    rnd = random.Random(seed)
    u = lambda: str(uuid.UUID(int=rnd.getrandbits(128), version=4))
    h = lambda n: "".join(rnd.choice("0123456789abcdef") for _ in range(n))
    return [
        f"/Users/example/.self-improve/runs/{u()}/raw",
        # store.new_id() is uuid4().hex: run and incident directories use it.
        f"~/.self-improve/runs/{u().replace('-', '')}/mine/{u().replace('-', '')}/transcript",
        f"~/.claude/projects/-Users-example-Code-repo/{u()}.jsonl",
        f"~/.claude/projects/-Users-x-Code-y/{u()}/subagents/agent-{h(16)}.jsonl",
        f"~/.codex/sessions/2026/10/12/rollout-2026-10-12T10-00-00-{u()}.jsonl",
        f"/private/tmp/claude-501/-Users-x-Code-y/{u()}/scratchpad/a.py",
        f"/Library/Containers/{u().upper()}/Data/x.plist",
    ]


@pytest.mark.parametrize("seed", range(40))
def test_paths_that_carry_uuids_and_hex_ids_stay_paths(seed):
    """Review of BL-03: a UUID or hex ID switches between digits and letters on
    most of its pairs, so the switch ratio read most session, run and object
    paths as secrets. Before BL-03 every one of these was kept."""
    for path in _id_path_shapes(seed):
        text = f"opened {path} to check"
        assert redact_text(text) == text, path


@pytest.mark.parametrize("seed", range(25))
def test_a_uuid_between_random_segments_does_not_make_a_secret_a_path(seed):
    """Only the ID words are neutral; random segments around one still count."""
    import random
    import uuid

    secret = _base64_with_two_slashes(seed, length=60)
    first, second = secret.split("/", 1)
    an_id = uuid.UUID(int=random.Random(seed).getrandbits(128), version=4)
    token = f"{first}/{an_id}/{second}"
    assert redact_text(f"key = {token}") == "key = [REDACTED:high_entropy]"


@pytest.mark.parametrize("seed", range(40))
def test_macos_temp_paths_stay_paths(seed):
    """Review of BL-03: a per-user temp root, /var/folders/<2>/<random>/T/,
    carries a random folder name, and $TMPDIR ends in a slash, so joined paths
    print T//. Both made real temp paths read as secrets."""
    import random
    import string

    rnd = random.Random(seed)
    pick = lambda n: "".join(rnd.choice(string.ascii_lowercase + string.digits) for _ in range(n))
    root = f"/var/folders/{pick(2)}/{pick(28)}/T"
    for path in (
        f"{root}//claude-501/x",
        f"/private{root}/tmpab12cd/run",
        f"{root}/pytest-of-example/pytest-1/test_case0/state",
    ):
        text = f"wrote {path} then"
        assert redact_text(text) == text, path


def test_a_double_slash_outside_a_temp_root_still_marks_a_secret():
    """The temp-root exemption must not widen the general double-slash rule."""
    secret = _base64_with_two_slashes(3)
    token = secret.replace("/", "//", 1)
    assert redact_text(f"key = {token}") == "key = [REDACTED:high_entropy]"


def test_a_key_length_hex_word_inside_a_path_is_still_redacted():
    """Second review of BL-03: making every hex word neutral kept 32- and
    40-character hex keys in URL paths verbatim (0 of 1,000 redacted). A hex
    word of key or digest length counts again, and git object paths are
    redacted as a consequence. The heuristic is statistical: a random hex
    string that rarely switches between digits and letters still reads as a
    path, so this asserts a rate. Measured on 2026-10-12: 979 and 989 of
    1,000 for the two lower-case shapes."""
    import random

    rnd = random.Random(5)
    shapes = (
        lambda: f"/api/v1/keys/{rnd.getrandbits(128):032x}",
        lambda: f"/hooks/token/{rnd.getrandbits(160):040x}",
        lambda: f"/hooks/{rnd.getrandbits(160):040X}/send",
    )
    for make in shapes:
        paths = [make() for _ in range(400)]
        kept = [p for p in paths if redact_text(f"posted to {p} today") == f"posted to {p} today"]
        assert len(kept) <= 40, (len(kept), kept[:3])


# ---------------------------------------------------------------------------
# BL-35: versioned redaction rules for retained instruction-text archives.
# ---------------------------------------------------------------------------

# SHA-256 of src/self_improve/redact.py for each rules version. A change to
# that file must update this pin in the same commit, after deciding whether
# the change can alter the set of texts that redact_text leaves unchanged.
_RULES_SOURCE_SHA256 = {
    "redaction-rules/2": "1aedaa1467fa93dc35fb72d61d9689a4dc7d4e54b81ceffe7b71e9d8ed5d49f8",
}
# The Unicode versions under which this file and tests/test_instruction_text.py
# passed with each pin. The rules also depend on the interpreter's Unicode
# tables, which \b (a boundary between \w and other characters), \s,
# str.isalnum() and the case tests in _char_class follow. So the same
# redact.py can accept different texts under different Unicode versions, and
# it already does: U+10D40, a letter new in Unicode 16.0, glued before a
# Figma token ("figd_" and twelve letters) hides the token from \b under
# Unicode 16.0.0 (Python 3.14), but not under 15.0.0 (Python 3.12) or 15.1.0
# (Python 3.13). CI runs
# Python 3.12 and 3.14 (.github/workflows/checks.yml), and pyproject.toml
# allows 3.13, so this records every checked version instead of one.
_RULES_UNICODE_VERSIONS = {
    "redaction-rules/2": frozenset({"15.0.0", "15.1.0", "16.0.0"}),
}
# SHA-256 of `git show 930e04f:src/self_improve/redact.py`, the rules that
# wrote every archive recorded before versioning existed.
_LEGACY_BODY_SHA256 = "a9b6076aae9f5d378111e81ddfc43f5183fbd4394da9923de50fe780b34ca67e"
_LEGACY_MARKER = b"# ---- frozen body: 930e04f:src/self_improve/redact.py ----\n"


def test_the_rules_source_is_pinned_to_its_version():
    import hashlib
    import unicodedata
    from pathlib import Path

    from self_improve import redact

    source = Path(redact.__file__).read_bytes()
    assert hashlib.sha256(source).hexdigest() == _RULES_SOURCE_SHA256[redact.RULES_VERSION], (
        "redact.py changed. If the change can alter the set of texts that "
        "redact_text leaves unchanged, freeze the old rules as a module, "
        "register them in instruction_text.RULES and give the new rules a "
        "new RULES_VERSION. Otherwise update this pin."
    )
    checked = _RULES_UNICODE_VERSIONS[redact.RULES_VERSION]
    assert unicodedata.unidata_version in checked, (
        f"This interpreter has Unicode {unicodedata.unidata_version}, and the "
        f"{redact.RULES_VERSION} pin was checked only under Unicode "
        f"{', '.join(sorted(checked))}. A Unicode version change can change "
        "the set of texts that redact_text leaves unchanged, with redact.py "
        "unchanged, so an archive written under one Unicode version can fail "
        "validation under another. Run this file and "
        "tests/test_instruction_text.py under this interpreter, then add the "
        "version here. Before production changes Unicode version, check that "
        "its retained archives still validate."
    )


def test_the_legacy_rules_are_the_frozen_pre_versioning_module():
    import hashlib
    from pathlib import Path

    from self_improve import redact_legacy

    source = Path(redact_legacy.__file__).read_bytes()
    assert source.count(_LEGACY_MARKER) == 1, "the frozen-body marker is missing"
    body = source.split(_LEGACY_MARKER, 1)[1]
    assert hashlib.sha256(body).hexdigest() == _LEGACY_BODY_SHA256, (
        "redact_legacy.py must stay byte-identical to 930e04f:src/self_improve/redact.py"
    )


def _legacy_rule_uses(source: str) -> set[str]:
    """Each way a module can reach redact_legacy: an import, an attribute
    such as instruction_text.redact_legacy, or a module-name string such as
    importlib.import_module("self_improve.redact_legacy") or getattr(m,
    "redact_legacy"). Prose that mentions the name is not a use."""
    import ast

    uses = set()
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [f"{node.module or ''}.{alias.name}" for alias in node.names]
            names.append(node.module or "")
        if any(name.split(".")[-1] == "redact_legacy" for name in names):
            uses.add("import")
        if isinstance(node, ast.Attribute) and node.attr == "redact_legacy":
            uses.add("attribute")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
            node.value == "redact_legacy" or node.value.endswith(".redact_legacy")
        ):
            uses.add("string")
    return uses


def test_the_legacy_rule_use_scan_detects_each_kind_of_use():
    """The scan below can only pass for the right reason if it can fire."""
    assert _legacy_rule_uses("from . import redact, redact_legacy") == {"import"}
    assert _legacy_rule_uses("import self_improve.redact_legacy as r") == {"import"}
    assert _legacy_rule_uses(
        "from . import instruction_text\ninstruction_text.redact_legacy.redact_text('x')"
    ) == {"attribute"}
    assert _legacy_rule_uses("importlib.import_module('self_improve.redact_legacy')") == {"string"}
    assert _legacy_rule_uses("getattr(instruction_text, 'redact_legacy')") == {"string"}
    assert _legacy_rule_uses('"""Validated by redact_legacy, never by new data."""') == set()


def test_only_the_archive_validator_uses_the_legacy_rules():
    """The frozen rules validate old archives; they must never redact new data."""
    from pathlib import Path

    import self_improve

    package = Path(self_improve.__file__).resolve().parent
    assert package == Path(__file__).resolve().parents[1] / "src" / "self_improve", package
    users = {}
    for path in sorted(package.rglob("*.py")):
        uses = _legacy_rule_uses(path.read_text(encoding="utf-8"))
        if uses:
            users[path.relative_to(package).as_posix()] = uses
    assert users == {"instruction_text.py": {"import"}}, users
