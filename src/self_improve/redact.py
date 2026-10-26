"""Secret/PII scrubbing applied before any transcript text reaches an LLM
prompt or the DB (``window_json``, ``matched_text``, ``sample_text``).

Every redaction replaces the secret with a *typed* placeholder such as
``[REDACTED:aws_key]`` so downstream mining can still see that a credential
was present without seeing its value.

Policy: err toward redacting. The generic high-entropy pass (documented at
:func:`_is_high_entropy`) runs last so that known key shapes get their
specific type name first.

``redact_text`` is idempotent by construction: it repeats the pass until a
pass changes nothing (see :func:`redact_text`). Placeholders contain no
character runs long enough (and no ``@``/``.``-joined shapes) to trigger any
pattern themselves, and the idempotence is covered by tests.
"""

from __future__ import annotations

import math
import re

# The version of the rules in this module. Every new instruction-text archive
# records it, and instruction_text validates each archive against the rules
# of the version it records (BACKLOG BL-35). Validation accepts a stored text
# exactly when redact_text leaves it unchanged. If a change can alter that set
# of accepted texts, give the rules a new version and keep this module's
# current rules as a frozen copy registered in instruction_text.RULES. A change
# that alters only the output for texts the rules already change keeps the
# version. The repeat-until-stable loop was such a change: it returns its input
# only when one pass changes nothing, so it accepts the same texts as one pass.
# A comment-only change also keeps the version.
# tests/test_redact.py pins this file's SHA-256 to the version. Archives
# recorded before versioning existed are validated by redact_legacy.
#
# One exception: the aws_secret_key pattern joined redaction-rules/2 on
# 2026-10-17 and changed its accepted texts. The version name stayed, because
# no released code had yet written a redaction-rules/2 archive. This holds
# only if the commits "Review fix (BL-03): redact an AWS secret ..." reach
# main with, or before, "Review fix (BL-35): text archives validate against
# their recorded redaction rules". If that commit reaches main first,
# production writes /2 archives under the older rules, and one that kept a
# 40-character value after an AWS secret-key name makes current_archives
# raise. Any later change to the set of accepted texts needs
# redaction-rules/3 and a frozen copy of the /2 rules registered in
# instruction_text.RULES.
RULES_VERSION = "redaction-rules/2"

# ---------------------------------------------------------------------------
# Specific, typed secret shapes. Order matters: multi-line PEM first, then
# structured tokens, then email, then the generic entropy sweep. Each entry is
# (type_name, compiled regex).
# ---------------------------------------------------------------------------

_SPECIFIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # -----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----
    (
        "pem_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----",
            re.DOTALL,
        ),
    ),
    # AWS secret access keys: 40 base64 characters without padding. The
    # entropy sweep can read one as a path: the example key in AWS's own
    # documentation switches character class on 0.229 of its pairs, under
    # _PATH_CLASS_SWITCH_MAX. So a 40-character value that follows an AWS
    # secret-key name is redacted here. The name is "secret access key" with
    # an optional "aws" before it, or "aws secret key", in any case, with " ",
    # "_", "." or "-" or nothing between words. So aws_secret_access_key,
    # AWS_SECRET_ACCESS_KEY, SecretAccessKey, "Secret access key" and
    # "AWS Secret Access Key" all match. A closing quote or backtick can follow
    # the name. Then comes "=", ":", "=>", ":=", "|" (a table), "(" (a call)
    # or whitespace. The "aws configure" prompt puts a default in brackets
    # before the colon: "AWS Secret Access Key [None]: ". An opening quote or
    # backtick can precede the value. Only the value is replaced; the "keep"
    # group keeps the name readable. A value without such a name still depends
    # on the entropy sweep (BACKLOG BL-62).
    (
        "aws_secret_key",
        re.compile(
            r"(?P<keep>(?i:(?:aws[ _.\-]?)?secret[ _.\-]?access[ _.\-]?key|aws[ _.\-]?secret[ _.\-]?key)"
            r"(?:\\?[\"'`])?"
            r"(?:\s*\[[^\[\]\r\n]*\]\s*:\s*|\s*(?:=>|:=|[:=|(])\s*|\s+)"
            r"(?:\\?[\"'`])?)"
            r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"
        ),
    ),
    # JWT: three dot-joined base64url segments, first starting "eyJ" ('{"').
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    ),
    # AWS access key ids (AKIA = long-lived, ASIA = STS temporary).
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Google API keys: "AIza" + 35 chars.
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    # GitHub tokens: ghp_/gho_/ghu_/ghs_/ghr_ + classic 36+, or fine-grained.
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    # Slack tokens: xoxb-/xoxp-/xoxa-/xoxr-/xoxs-/xoxe-...
    ("slack_token", re.compile(r"\bxox[abeprs]-[A-Za-z0-9\-]{8,}\b")),
    # OpenAI / Anthropic style secret keys ("sk-", "sk-proj-", "sk-ant-...").
    ("sk_api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    # Figma personal access tokens can be shorter than the generic entropy
    # floor. Use a typed pattern so short credentials are scrubbed without
    # hiding ordinary identifiers. Redaction protects future output; it
    # cannot revoke or repair an earlier credential exposure.
    ("figma_token", re.compile(r"\bfigd_[A-Za-z0-9_\-]{12,}")),
    # Email addresses.
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
]

# ---------------------------------------------------------------------------
# Generic high-entropy sweep.
# ---------------------------------------------------------------------------

# Maximal runs of base64/hex-ish characters, minimum 32 chars.
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_PURE_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")

# Documented thresholds for the entropy heuristic (see _is_high_entropy).
_ENTROPY_BITS_WITH_CLASSES = 3.5
_ENTROPY_BITS_ALONE = 4.5
_MIN_CHAR_CLASSES = 3


def _shannon_bits_per_char(token: str) -> float:
    """Shannon entropy of the token's character distribution, bits/char."""
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(token)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Path components are words, so their characters rarely switch between
# lower case, upper case and digits; a CamelCase word start is not a switch.
# Random base64 switches on about half of its adjacent pairs. Measured on
# 2026-10-12: 0.25 keeps all 2,321 path tokens built from this repository's
# tracked files and leaves 121 of 20,000 random 40-character two-slash secrets
# unredacted (all 20,000 before). A lower bar leaks less but also redacts text
# in real instruction files. Any change to it is a new RULES_VERSION.
#
# That rate is for a bare token. Inside a URL path, the lower-case words
# around a secret add pairs but no switches, so webhook secrets leak about
# 13 times as often. Re-measured on 2026-10-17 with random.Random(20261017),
# counting the secret as kept when it survives in "posted to <url> today".
# The rules at commit 930e04f kept every sample of each shape.
#   bare 40-character two-slash secret            120 of 20,000 kept (0.6%)
#   Slack webhook URL, 24-character secret         77 of 1,000 kept (7.7%)
#   Discord webhook URL, 68-character secret       76 of 1,000 kept (7.6%)
#   /v1/keys/ + 40-character [A-Za-z0-9+] key       6 of 1,000 kept (0.6%)
#   /v1/keys/ + 40-character [A-Za-z0-9+/] key     13 of 1,000 kept (1.3%)
# A typed pattern for webhook URLs would close the first two gaps; it is a
# new RULES_VERSION (BACKLOG BL-62).
_PATH_CLASS_SWITCH_MAX = 0.25
_SHORT_HEX_SEGMENT_RE = re.compile(r"[0-9a-f]{4,16}")
# Generated IDs switch between digits and letters on most pairs, yet session
# and run paths are full of them. A UUID, or a single-case hex word of 7 to 31
# characters that mixes digits and letters, adds no switches; the rest of the
# token still decides. Random base64 rarely forms such a word. A hex word of 32
# or more characters has the length of a key or digest, so it still counts,
# and git object paths are redacted with it. A UUID used as a secret inside a
# path is kept, as it was before BACKLOG BL-03 (see BACKLOG BL-62).
#
# Cost: _HEX_ID_WORD_RE removes a hex key of 7 to 31 characters inside a path
# from the count, so the rest of the path decides, and an ordinary path keeps
# the key. Measured on 2026-10-17 with random.Random(20261017), 2,000 samples
# of /v2/projects/acme/tokens/<hex>/x per length: all 2,000 kept at 7, 16, 24
# and 31 characters. The rules at commit 930e04f kept every two-slash token,
# so they kept these too (2,000 of 2,000 measured at 24). Commit b9b31c5,
# before this exemption, kept 387 of the 24-character keys. At 32 characters,
# 205 of 2,000 are kept, because the lower-case words of this path shape
# dilute the switch ratio.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}"
    # uuid4().hex, which store.new_id() uses for run and incident directories.
    # A random 32-character hex key has this version and variant layout one
    # time in 64; the boundaries keep a longer hex word whole.
    r"|(?<![0-9A-Za-z])[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}(?![0-9A-Za-z])"
)
_HEX_ID_WORD_RE = re.compile(r"(?=.*\d)(?=.*[a-fA-F])(?:[0-9a-f]{7,31}|[0-9A-F]{7,31})")
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
# A macOS per-user temp root, /var/folders/<2>/<random>/T/, carries a random
# folder name, and $TMPDIR ends in a slash, so joined paths often print "T//".
# The root is reduced to a fixed stand-in before the checks below.
_MACOS_TEMP_ROOT_RE = re.compile(r"(?:^|/)var/folders/[0-9a-z_]{2}/[0-9a-z_]{20,40}/[0-9A-Z]/+")


def _char_class(ch: str) -> str:
    if ch.islower():
        return "lower"
    if ch.isupper():
        return "upper"
    if ch.isdigit():
        return "digit"
    return "symbol"


def _looks_like_path(token: str) -> bool:
    """True when a slash-separated run reads like a filesystem path."""
    token = _MACOS_TEMP_ROOT_RE.sub("/var/folders/T/", token)
    if "//" in token.lstrip("/"):
        return False
    segments = [s for s in token.split("/") if s]
    # Short hex IDs joined by slashes, such as commit lists, were always kept
    # as paths, and these rules keep that behavior.
    if segments and all(_SHORT_HEX_SEGMENT_RE.fullmatch(s) for s in segments):
        return True
    pairs = switches = 0
    for segment in token.split("/"):
        for word in _WORD_SPLIT_RE.split(_UUID_RE.sub("-", segment)):
            if _HEX_ID_WORD_RE.fullmatch(word):
                continue
            for a, b in zip(word, word[1:]):
                ca, cb = _char_class(a), _char_class(b)
                pairs += 1
                # An upper-case letter followed by lower case starts a CamelCase
                # word, which is how path components are written; not a switch.
                switches += ca != cb and (ca, cb) != ("upper", "lower")
    return pairs == 0 or switches / pairs < _PATH_CLASS_SWITCH_MAX


def _is_high_entropy(token: str) -> bool:
    """Heuristic for "looks like a secret" on a >=32-char base64/hex-ish run.

    Documented thresholds (err toward redacting):
    - tokens with >= 2 ``/`` that also read like paths are kept: no ``//``
      after a leading slash, except the ``T//`` that a joined macOS temp root
      prints, and letters and digits change class on fewer than a quarter of
      adjacent pairs (see :func:`_looks_like_path`). Paths
      are common in transcripts; base64 secrets also contain slashes, and a
      ``1//`` OAuth refresh token is never a path;
    - pure hex of >= 32 chars (digests, hex-encoded keys) is always redacted;
    - otherwise redact when the token mixes >= 3 character classes
      (lower/upper/digit/symbol) AND has Shannon entropy >= 3.5 bits/char,
      or when entropy alone is >= 4.5 bits/char (random base64 sits near
      4.7-5.0 for 32+ chars; English-like identifiers sit near 4.0).
    """
    # Known limitation: a secret GLUED (no word boundary) onto a very long
    # low-entropy run dilutes this aggregate-entropy check below threshold and
    # also defeats the \b-anchored specific patterns. Truncation can remove a
    # trailing secret only when it falls beyond the configured cap. It does
    # not establish that every retained body is free of secrets.
    if token.count("/") >= 2 and _looks_like_path(token):
        return False
    if _PURE_HEX_RE.fullmatch(token):
        # Real digests/keys have near-uniform nibble distribution (~4 bits/char);
        # uniform runs like "AAAA..." are hex-alphabet but entropy ~0 — keep them.
        return _shannon_bits_per_char(token) >= 3.0
    classes = sum(
        (
            any(c.islower() for c in token),
            any(c.isupper() for c in token),
            any(c.isdigit() for c in token),
            any(not c.isalnum() for c in token),
        )
    )
    bits = _shannon_bits_per_char(token)
    if classes >= _MIN_CHAR_CLASSES and bits >= _ENTROPY_BITS_WITH_CLASSES:
        return True
    return bits >= _ENTROPY_BITS_ALONE


def _entropy_sub(match: re.Match[str]) -> str:
    token = match.group(0)
    return "[REDACTED:high_entropy]" if _is_high_entropy(token) else token


# A pattern with a "keep" group replaces only the text after that group.
_SPECIFIC_SUBS: list[tuple[re.Pattern[str], str]] = [
    (pattern, (r"\g<keep>" if "keep" in pattern.groupindex else "") + f"[REDACTED:{type_name}]")
    for type_name, pattern in _SPECIFIC_PATTERNS
]


def _redact_pass(s: str) -> str:
    for pattern, replacement in _SPECIFIC_SUBS:
        s = pattern.sub(replacement, s)
    return _ENTROPY_TOKEN_RE.sub(_entropy_sub, s)


def redact_text(s: str) -> str:
    """Return ``s`` with secrets replaced by typed ``[REDACTED:<type>]`` tags.

    Idempotent: redacting already-redacted text is a no-op. One pass is not
    enough on its own. The Google pattern has a fixed length and no end
    boundary, so text glued onto a key, such as a second key, starts at a word
    boundary only after the key becomes a placeholder that ends in ``]``. The
    pass therefore repeats until it changes nothing (BACKLOG BL-35). A pass
    that changes the text replaces at least one character outside the
    placeholders with a placeholder, so at most ``len(s)`` passes change the
    text. Reaching that bound means this argument no longer holds for the
    patterns, and the call raises instead of returning partly redacted text.
    """
    for _ in range(len(s) + 1):
        redacted = _redact_pass(s)
        if redacted == s:
            return s
        s = redacted
    raise RuntimeError("redact_text did not reach a fixed point")
