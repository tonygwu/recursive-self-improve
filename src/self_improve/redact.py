"""Secret/PII scrubbing applied before any transcript text reaches an LLM
prompt or the DB (``window_json``, ``matched_text``, ``sample_text``).

Every redaction replaces the secret with a *typed* placeholder such as
``[REDACTED:aws_key]`` so downstream mining can still see that a credential
was present without seeing its value.

Policy: err toward redacting. The generic high-entropy pass (documented at
:func:`_is_high_entropy`) runs last so that known key shapes get their
specific type name first.

``redact_text`` is idempotent: placeholders contain no character runs long
enough (and no ``@``/``.``-joined shapes) to re-trigger any pattern, and the
idempotence is covered by tests.
"""

from __future__ import annotations

import math
import re

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


def _is_high_entropy(token: str) -> bool:
    """Heuristic for "looks like a secret" on a >=32-char base64/hex-ish run.

    Documented thresholds (err toward redacting):
    - tokens with >= 2 ``/`` are treated as filesystem paths and kept —
      paths are common in transcripts and are not credentials;
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
    if token.count("/") >= 2:
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


def redact_text(s: str) -> str:
    """Return ``s`` with secrets replaced by typed ``[REDACTED:<type>]`` tags.

    Idempotent: redacting already-redacted text is a no-op.
    """
    for type_name, pattern in _SPECIFIC_PATTERNS:
        s = pattern.sub(f"[REDACTED:{type_name}]", s)
    return _ENTROPY_TOKEN_RE.sub(_entropy_sub, s)
