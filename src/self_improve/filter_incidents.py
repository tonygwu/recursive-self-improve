"""Deterministic signal detection over one session's normalized events.

Zero LLM calls, zero DB access: :func:`detect` takes the full ordered
``list[TurnEvent]`` for a single session plus the :class:`Config` and returns
candidate incidents (what the miner will later look at) and every error
fingerprint seen (cross-session promotion of repeated errors is ``scan.py``'s
job — this module only reports what happened inside the one session).

Detectors and their documented scoring (all scores in [0, 1]):

- ``correction`` — a short human message (len < ``correction_max_len``)
  matching a correction phrase, with >= 1 assistant tool_use in the prior 10
  events (the assistant must have *done* something to be corrected about).
  Score = weight of the strongest matched pattern + 0.1 per additional
  distinct pattern, capped at 1.0.
- ``standing_instruction`` — human message containing a durable-instruction
  marker ("from now on", "always", "rule:", ...). Same scoring scheme.
- ``frustration`` — human message with an ALL-CAPS word (>= 4 chars, not a
  known technical acronym and not a filename stem), "??", or an expletive.
  Same scoring scheme.
- ``repeated_error`` — the same normalized error fingerprint occurring
  >= ``repeated_error_min_in_session`` times. Score = 0.5 + 0.1 per
  occurrence beyond the threshold, capped at 1.0. The incident's
  ``event_index`` is the occurrence at which the threshold was crossed.
- ``friction_loop`` — >= ``friction_loop_min_cycles`` edit->error cycles on
  the same file within ``friction_loop_window_events`` events. Score =
  cycles / (min_cycles + 2), capped at 1.0. ``event_index`` is the last
  error of the qualifying run.
- ``instruction_edit`` — an assistant Edit/Write/MultiEdit/NotebookEdit
  tool_use whose target file is an instruction file: basename in
  ``cfg.instruction_edit_filenames``, or path containing ``/.claude/rules/``.
  The highest-precision signal in the system: someone (human-directed or
  agent) encoded a lesson into an instruction file mid-session — the lesson
  is literally the edit content. Flat score 0.9; ``matched_text`` is the
  redacted head of the edit content.
- ``self_observation`` — an ASSISTANT text message matching self-discovery
  language ("root cause was", "silently dropped", "never actually ran",
  "reading the wrong file", ...) with >= 1 tool_result in the prior 10
  events (discovery follows evidence). This covers agent-discovered mistakes
  that lack human pushback or visible tool errors.
  Scored like ``correction`` (0.5-0.7 pattern weights + multi-match bonus).

Precision choices made here (deliberate, surfaced so they can be reviewed):

- Text detectors only run on ``role == "human"`` events and skip
  ``kind == "slash_command"`` events (slash commands are not free text).
- Lines starting with ``>`` are dropped before matching — that is the human
  quoting the assistant, not the human speaking.
- Weak generic cues ("no,", "nope", "wrong") must appear at the start of the
  message; phrase cues may appear anywhere.
- ALL-CAPS matching ignores a fixed allowlist of technical acronyms
  (``_CAPS_ALLOWLIST``) and words glued to a file extension (``CLAUDE.md``).
- ``friction_loop`` needs the edited file path; it reads
  ``event.meta["file_path"]`` on Edit/Write/patch tool_use events. Edits
  without one are counted in ``FilterIncidentsResult.stats``
  (``friction_edit_missing_file``), never guessed.

Per-signal caps: at most ``max_incidents_per_signal_per_session`` incidents
per signal are kept — the highest-scoring ones, earliest ``event_index``
winning ties. Everything cut is counted in ``FilterIncidentsResult.dropped``
(silent truncation is forbidden).

All ``matched_text`` / ``sample_text`` snippets pass through
:func:`redact.redact_text` before leaving this module, so incident rows can
be persisted or prompted as-is.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .config import Config
from .redact import redact_text
from .sources.base import (
    KIND_PATCH_APPLY,
    ROLE_TOOL_RESULT,
    KIND_SLASH_COMMAND,
    KIND_TOOL_USE,
    ROLE_ASSISTANT,
    ROLE_HUMAN,
    TurnEvent,
)

SIGNAL_CORRECTION = "correction"
SIGNAL_STANDING_INSTRUCTION = "standing_instruction"
SIGNAL_FRUSTRATION = "frustration"
SIGNAL_REPEATED_ERROR = "repeated_error"
SIGNAL_FRICTION_LOOP = "friction_loop"
SIGNAL_INSTRUCTION_EDIT = "instruction_edit"
SIGNAL_SELF_OBSERVATION = "self_observation"

# Path fragments that disqualify an instruction-file edit: fixtures, temp
# sandboxes and vendored trees are not the user's real instruction fleet.
_INSTRUCTION_EDIT_PATH_DENY = (
    "/tmp/",
    # Hidden scratch directories can contain disposable instruction-file copies.
    "/.tmp/",
    "/var/folders/",
    "/private/tmp/",
    "/.venv/",
    "/node_modules/",
    "/site-packages/",
    "/tests/fixtures/",
)

# Assistant self-discovery language: the model reporting a mistake it just
# found. Weights mirror _CORRECTION_PATTERNS (strongest wins, +0.1 each extra).
_SELF_OBSERVATION_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "silently_did",
        re.compile(
            r"\bsilently\s+(?:served|substituted|dropped|ignored|failed|"
            r"downgraded|swallowed|truncated)\b",
            re.IGNORECASE,
        ),
        0.7,
    ),
    ("mislabeled", re.compile(r"\bmislabel(?:ed|led|ing)?\b", re.IGNORECASE), 0.6),
    ("turns_out", re.compile(r"\bturns out\b", re.IGNORECASE), 0.5),
    (
        "root_cause",
        re.compile(r"\broot cause (?:was|is|turned out)\b", re.IGNORECASE),
        0.6,
    ),
    (
        "actual_cause",
        re.compile(r"\bthe actual (?:cause|problem|issue|bug)\b", re.IGNORECASE),
        0.6,
    ),
    (
        "checking_wrong",
        re.compile(
            r"\b(?:check|checking|read|reading|look|looking at|inspect|inspecting)"
            r"(?:ed|ing)?\s+the\s+wrong\b",
            re.IGNORECASE,
        ),
        0.7,
    ),
    (
        "wrong_thing",
        re.compile(
            r"\bwrong\s+(?:artifact|file|directory|dir|branch|model|path|account|table)\b",
            re.IGNORECASE,
        ),
        0.6,
    ),
    (
        "never_actually",
        re.compile(
            r"\bnever actually\s+(?:ran|fired|executed|called|applied|used)\b",
            re.IGNORECASE,
        ),
        0.7,
    ),
    ("stale", re.compile(r"\bstale\s+(?:cache|copy|data|state|value)\b", re.IGNORECASE), 0.5),
]

# Assistant messages longer than this are prose/reports, not discovery moments.
_SELF_OBSERVATION_MAX_LEN = 4000

_MATCHED_TEXT_MAX = 200  # chars of trigger snippet kept on an incident


@dataclass(frozen=True)
class CandidateIncident:
    """One detector hit, pointing at ``events[event_index]``."""

    signal_type: str
    event_index: int
    matched_text: str      # redacted trigger snippet
    score: float           # [0, 1], per-detector formula documented above
    detail: dict = field(default_factory=dict)  # detector-specific evidence
    # Other events whose content the detection depends on (event_index is
    # always implied). Stable under append: only evidence that qualified the hit.
    support_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class ErrorFingerprint:
    """One distinct normalized error within the session (all counts, not
    just those over the incident threshold — scan.py promotes across
    sessions)."""

    fingerprint: str   # sha1 hex of normalized error text
    count: int
    sample_text: str   # redacted first-occurrence excerpt
    first_ts: str      # ISO UTC of first occurrence, from the data


@dataclass
class FilterIncidentsResult:
    incidents: list[CandidateIncident]
    fingerprints: list[ErrorFingerprint]
    # signal_type -> number of incidents cut by the per-signal cap.
    dropped: dict[str, int] = field(default_factory=dict)
    # taxonomy counters for anything skipped-not-guessed, e.g.
    # "friction_edit_missing_file".
    stats: dict[str, int] = field(default_factory=dict)
    # Every detector hit BEFORE the per-signal cap, in (event_index, signal)
    # order. Measurement counts these; `incidents` stays the capped mining
    # queue. None means the producer did not report them.
    candidates: list[CandidateIncident] | None = None
    # (fingerprint, event_index) for every individual error occurrence, in
    # event order. ErrorFingerprint keeps only a per-session count and first_ts.
    error_occurrences: list[tuple[str, int]] | None = None


# ---------------------------------------------------------------------------
# Pattern tables: (name, regex, weight). All matching is case-insensitive on
# quote-stripped text.
# ---------------------------------------------------------------------------

_CORRECTION_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    # Weak generic cues: anchored to the start of the message for precision
    # ("no worries", "I know" and mid-sentence mentions must not fire).
    ("no_comma", re.compile(r"^\s*no,", re.IGNORECASE), 0.5),
    ("nope", re.compile(r"^\s*nope\b", re.IGNORECASE), 0.5),
    (
        "wrong",
        re.compile(
            r"^\s*wrong\b|\b(?:that'?s|that is|this is|it'?s|it is|still)\s+wrong\b",
            re.IGNORECASE,
        ),
        0.5,
    ),
    ("thats_not", re.compile(r"\bthat'?s not\b", re.IGNORECASE), 0.5),
    (
        "not_what_i",
        re.compile(r"\bnot what i (?:asked|meant|wanted)\b", re.IGNORECASE),
        0.7,
    ),
    (
        "you_didnt",
        re.compile(r"\byou (?:didn'?t|ignored|missed|broke)\b", re.IGNORECASE),
        0.6,
    ),
    ("why_did_you", re.compile(r"\bwhy did you\b", re.IGNORECASE), 0.6),
    ("i_told_you", re.compile(r"\bi told you\b", re.IGNORECASE), 0.7),
    ("i_said", re.compile(r"\bi said\b", re.IGNORECASE), 0.5),
    ("stop_doing", re.compile(r"\bstop doing\b", re.IGNORECASE), 0.6),
    ("dont_do_that", re.compile(r"\bdon'?t do that\b", re.IGNORECASE), 0.6),
    ("undo_revert", re.compile(r"\b(?:undo|revert) that\b", re.IGNORECASE), 0.6),
]

_STANDING_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("from_now_on", re.compile(r"\bfrom now on\b", re.IGNORECASE), 0.8),
    ("going_forward", re.compile(r"\bgoing forward\b", re.IGNORECASE), 0.7),
    ("in_the_future", re.compile(r"\bin the future\b", re.IGNORECASE), 0.7),
    ("remember", re.compile(r"\bremember (?:to|that)\b", re.IGNORECASE), 0.7),
    (
        "make_sure",
        re.compile(r"\bmake sure (?:you |to )?(?:always|never)\b", re.IGNORECASE),
        0.8,
    ),
    ("rule_colon", re.compile(r"(?:^|\n)\s*rule:", re.IGNORECASE), 0.8),
    # Bare always/never are common in ordinary prose; low weight so the
    # per-signal cap prefers the explicit markers.
    ("always", re.compile(r"\balways\b", re.IGNORECASE), 0.4),
    ("never", re.compile(r"\bnever\b", re.IGNORECASE), 0.4),
]

# Technical acronyms/emphasis words that are ALL-CAPS without being shouting.
_CAPS_ALLOWLIST = frozenset(
    """
    JSON JSONL YAML TOML HTML HTTP HTTPS UUID UUIDS SQL SQLITE JWT AWS GCP
    REST GRPC UTC README TODO NOTE POST TRUE FALSE NULL NONE PATH HOME CSV
    YYYY CORS CRUD OAUTH GITHUB CLAUDE AGENTS SKILL CONFIG UTF ASCII ISO
    IMPORTANT CRITICAL WARNING ERROR INFO DEBUG
    """.split()
)

# ALL-CAPS word of >= 4 chars, not glued to a file extension (CLAUDE.md).
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{4,}\b(?!\.[A-Za-z])")

_FRUSTRATION_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("multi_question", re.compile(r"\?{2,}"), 0.5),
    ("expletive", re.compile(r"\b(?:ugh+|ffs|wtf)\b", re.IGNORECASE), 0.7),
    ("come_on", re.compile(r"\bcome on\b", re.IGNORECASE), 0.5),
    # "seriously" only at message/line start or with a "?" — "take this
    # seriously" must not fire.
    (
        "seriously",
        re.compile(r"(?:^|\n)\s*seriously\b|\bseriously\?", re.IGNORECASE),
        0.5,
    ),
    ("again_q", re.compile(r"\bagain\?", re.IGNORECASE), 0.5),
]
_ALL_CAPS_WEIGHT = 0.5

# Tool names that count as "editing a file" for friction_loop.
_EDIT_TOOL_NAMES = frozenset({"edit", "write", "multiedit", "notebookedit"})


def _is_edit_tool_use(e: TurnEvent) -> bool:
    if e.kind == KIND_PATCH_APPLY:
        return True
    if e.role != ROLE_ASSISTANT or e.kind != KIND_TOOL_USE:
        return False
    name = e.tool_name.lower()
    return name in _EDIT_TOOL_NAMES or "patch" in name


#: Codex reports applied patches as a header line followed by one
#: ``<status letter> <path>`` line per file, for example:
#: ``Success. Updated the following files:\nA /abs/one.md\nM /abs/two.py``.
#: Paths can be relative. Read these lines only after the success header.
_CODEX_PATCH_PATH_RE = re.compile(r"^\s*[A-Z]\s+(\S.*?)\s*$", re.MULTILINE)
# The apply_patch INVOCATION carries its paths in the patch header, and the
# path may be relative ("*** Update File: src/example.py"). Anchored at
# line start with no leading "+"/"-", so an identical line inside the diff body
# is content rather than a header. This matters for friction_loop, which counts
# edit-then-failure cycles: a FAILED patch never emits a success result, so the
# invocation is the ONLY place its path appears.
_CODEX_PATCH_HEADER_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File:[ \t]+(\S.*?)\s*$", re.MULTILINE
)


def _edited_paths(e: TurnEvent) -> list[str]:
    """Every file path an edit event touched, in order ('' -> empty list).

    Supported edit-event shapes:

    * Claude puts the path in the stringified tool-input JSON carried in
      ``event.text``.
    * Codex ``apply_patch`` events carry no JSON at all — the files are listed
      in invocation headers or ``<status> <path>`` result lines. Paths can be
      relative. Failed patches have invocation headers but no success result.

    Returns ALL paths rather than the first: a patch that touches five files
    including ``AGENTS.md`` is an instruction edit regardless of the order
    Codex happens to list them in.
    """
    from_meta = str(e.meta.get("file_path") or "")
    if from_meta:
        return [from_meta]
    text = e.text.lstrip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        for key in ("file_path", "notebook_path", "path", "filePath"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return [value]
        return []
    header = [m.group(1) for m in _CODEX_PATCH_HEADER_RE.finditer(e.text)]
    if header:
        return header
    # Success-result form. Anchor on the header and only then read the
    # `<status letter> <path>` lines: the listed path may be RELATIVE
    # ("M backlog/channels.md"), and matching relative paths anywhere in a
    # transcript would turn ordinary prose into edited files.
    marker = "Updated the following files:"
    idx = e.text.find(marker)
    if idx == -1:
        return []
    return [m.group(1) for m in _CODEX_PATCH_PATH_RE.finditer(e.text[idx + len(marker):])]


def _edited_path(e: TurnEvent) -> str:
    """Best-known path for an edit event ('' when none is recoverable).

    The FIRST path of :func:`_edited_paths`, which is what ``friction_loop``
    wants: it groups edits into per-file cycles, so it needs one file per
    event. ``instruction_edit`` uses the plural form instead, because a patch
    touching several files is an instruction edit if ANY of them is a rule
    file. Never raises; an unrecoverable path yields '' and the caller counts
    it rather than guessing.
    """
    paths = _edited_paths(e)
    return paths[0] if paths else ""


def _edit_content(e: TurnEvent) -> str:
    """The text an edit actually wrote, unwrapped from the tool-input JSON.

    Without this the whole ``{"file_path": ..., "content": ...}`` envelope
    would be reported as the trigger snippet (JSON boilerplate crowding out
    the lesson) and an edit with empty content would look non-empty.

    **Known gap for Codex.** An ``apply_patch`` *result* event lists the files
    it touched but not the diff, so this returns the summary line
    (``Success. Updated the following files: ...``) rather than the rule that
    was written. The lesson is still reachable — the agentic miner reads the
    whole session — but the trigger snippet is weaker than Claude's.

    Invocation events can carry the patch body. This helper returns their raw
    text when no supported JSON content field exists. It does not join a result
    to an invocation or extract individual added lines from a patch.
    """
    text = e.text.lstrip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return e.text.strip()
        if isinstance(payload, dict):
            for key in ("content", "new_string", "new_source", "text"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value.strip()
            # An edit whose input carries no content field at all: fall back
            # to the raw envelope rather than claiming it was empty.
            return e.text.strip()
    return e.text.strip()


def _detect_instruction_edit(
    events: list[TurnEvent], cfg: Config, stats: dict[str, int]
) -> list[CandidateIncident]:
    """Edits to instruction files — the highest-precision signal available.

    Someone encoded a lesson into CLAUDE.md/AGENTS.md/SKILL.md/MEMORY.md or a
    ``.claude/rules/`` file mid-session; the lesson is the edit content itself.
    Headless sessions are NOT excluded (agents encoding lessons headlessly is
    exactly the target). Edits without a recorded path are counted, never
    guessed (``instruction_edit_missing_path``).
    """
    out: list[CandidateIncident] = []
    for i, e in enumerate(events):
        if not _is_edit_tool_use(e):
            continue
        paths = _edited_paths(e)
        if not paths:
            stats["instruction_edit_missing_path"] = (
                stats.get("instruction_edit_missing_path", 0) + 1
            )
            continue
        # A patch may touch several files; it is an instruction edit if ANY of
        # them is a rule file. Denial is per-path for the same reason - one
        # fixture path must not disqualify a real CLAUDE.md in the same patch.
        path = ""
        denied = False
        for candidate in paths:
            basename = candidate.rsplit("/", 1)[-1]
            is_rule = basename in cfg.instruction_edit_filenames or (
                "/.claude/rules/" in candidate
            )
            if not is_rule:
                continue
            # Check instruction-ness FIRST. Denying on any deny-listed path
            # would count a patch that merely touched node_modules as a
            # blocked instruction edit, which is not what the counter means -
            # and, since a Codex patch routinely lists many files, would swamp
            # it with noise.
            if any(frag in candidate.lower() for frag in _INSTRUCTION_EDIT_PATH_DENY):
                denied = True
                continue
            path = candidate
            break
        if not path:
            if denied:
                stats["instruction_edit_path_denied"] = (
                    stats.get("instruction_edit_path_denied", 0) + 1
                )
            continue
        content = _edit_content(e)
        if not content:
            stats["instruction_edit_empty"] = stats.get("instruction_edit_empty", 0) + 1
            continue
        out.append(
            CandidateIncident(
                signal_type=SIGNAL_INSTRUCTION_EDIT,
                event_index=i,
                matched_text=redact_text(content)[:_MATCHED_TEXT_MAX],
                score=0.9,
                detail={"file": path},
            )
        )
    return out


def _detect_self_observation(
    events: list[TurnEvent], cfg: Config
) -> list[CandidateIncident]:
    """Assistant text reporting a mistake it just discovered.

    An agent may discover a mistake without human pushback or a visible tool
    error. Require >= 1 tool_result within the prior 10 events so the
    observation follows evidence rather than unsupported speculation.
    """
    out: list[CandidateIncident] = []
    for i, e in enumerate(events):
        if e.role != ROLE_ASSISTANT or e.kind == KIND_TOOL_USE or not e.text:
            continue
        if len(e.text) > _SELF_OBSERVATION_MAX_LEN:
            continue
        if not any(
            p.role == ROLE_TOOL_RESULT for p in events[max(0, i - 10) : i]
        ):
            continue
        text = _strip_quoted_lines(e.text)
        names, score, start = _match_patterns(text, _SELF_OBSERVATION_PATTERNS)
        if names:
            out.append(
                CandidateIncident(
                    signal_type=SIGNAL_SELF_OBSERVATION,
                    event_index=i,
                    matched_text=_snippet_for(text, start),
                    score=score,
                    detail={"patterns": names},
                )
            )
    return out


def _strip_quoted_lines(text: str) -> str:
    """Drop lines quoting the assistant (leading '>') before matching."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    )


def _is_human_text_event(e: TurnEvent) -> bool:
    # Headless events are excluded: in claude -p / codex exec / subagent
    # threads the "user" text is machine-authored (scripts, templated prompts,
    # inter-agent traffic), so text signals (correction/standing/frustration)
    # would be noise. Error-based detectors (repeated_error, friction_loop)
    # still run on headless sessions. detect() counts what this predicate
    # skips (stats["headless_text_skipped"]) — skipped, never silent.
    #
# This policy changes which signals can occur in a headless-heavy dataset.
# Report the excluded population when interpreting detector coverage.
    return (
        e.role == ROLE_HUMAN
        and e.kind != KIND_SLASH_COMMAND
        and bool(e.text)
        and not e.headless
    )


def _snippet_for(text: str, match_start: int) -> str:
    """The (redacted, length-capped) line containing the match."""
    line_start = text.rfind("\n", 0, match_start) + 1
    line_end = text.find("\n", match_start)
    if line_end == -1:
        line_end = len(text)
    return redact_text(text[line_start:line_end].strip()[:_MATCHED_TEXT_MAX])


def _match_patterns(
    text: str, patterns: list[tuple[str, re.Pattern[str], float]]
) -> tuple[list[str], float, int]:
    """Return (matched pattern names, score, best-match start offset)."""
    names: list[str] = []
    best_weight = 0.0
    best_start = 0
    for name, pattern, weight in patterns:
        m = pattern.search(text)
        if m:
            names.append(name)
            if weight > best_weight:
                best_weight = weight
                best_start = m.start()
    if not names:
        return [], 0.0, 0
    score = min(1.0, best_weight + 0.1 * (len(names) - 1))
    return names, score, best_start


# ---------------------------------------------------------------------------
# Text detectors.
# ---------------------------------------------------------------------------


def _detect_correction(
    events: list[TurnEvent], cfg: Config
) -> list[CandidateIncident]:
    out: list[CandidateIncident] = []
    for i, e in enumerate(events):
        if not _is_human_text_event(e):
            continue
        if len(e.text) >= cfg.correction_max_len:
            continue
        if not any(
            p.role == ROLE_ASSISTANT and p.kind == KIND_TOOL_USE
            for p in events[max(0, i - 10) : i]
        ):
            continue
        text = _strip_quoted_lines(e.text)
        names, score, start = _match_patterns(text, _CORRECTION_PATTERNS)
        if names:
            out.append(
                CandidateIncident(
                    signal_type=SIGNAL_CORRECTION,
                    event_index=i,
                    matched_text=_snippet_for(text, start),
                    score=score,
                    detail={"patterns": names},
                )
            )
    return out


def _detect_standing_instruction(
    events: list[TurnEvent], cfg: Config
) -> list[CandidateIncident]:
    out: list[CandidateIncident] = []
    for i, e in enumerate(events):
        if not _is_human_text_event(e):
            continue
        text = _strip_quoted_lines(e.text)
        names, score, start = _match_patterns(text, _STANDING_PATTERNS)
        if names:
            out.append(
                CandidateIncident(
                    signal_type=SIGNAL_STANDING_INSTRUCTION,
                    event_index=i,
                    matched_text=_snippet_for(text, start),
                    score=score,
                    detail={"patterns": names},
                )
            )
    return out


def _detect_frustration(
    events: list[TurnEvent], cfg: Config
) -> list[CandidateIncident]:
    out: list[CandidateIncident] = []
    for i, e in enumerate(events):
        if not _is_human_text_event(e):
            continue
        text = _strip_quoted_lines(e.text)
        # (name, weight, match start) for every cue that fired.
        hits: list[tuple[str, float, int]] = []
        caps = next(
            (
                m
                for m in _ALL_CAPS_RE.finditer(text)
                if m.group(0) not in _CAPS_ALLOWLIST
            ),
            None,
        )
        if caps is not None:
            hits.append(("all_caps", _ALL_CAPS_WEIGHT, caps.start()))
        for name, pattern, weight in _FRUSTRATION_PATTERNS:
            m = pattern.search(text)
            if m:
                hits.append((name, weight, m.start()))
        if hits:
            _, best_weight, best_start = max(hits, key=lambda h: h[1])
            out.append(
                CandidateIncident(
                    signal_type=SIGNAL_FRUSTRATION,
                    event_index=i,
                    matched_text=_snippet_for(text, best_start),
                    score=min(1.0, best_weight + 0.1 * (len(hits) - 1)),
                    detail={"patterns": [h[0] for h in hits]},
                )
            )
    return out


# ---------------------------------------------------------------------------
# repeated_error.
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ABS_PATH_RE = re.compile(r"(?<![\w])/[\w.+\-]+(?:/[\w.+\-]+)+")
_HEX_RUN_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b")
# Shorter ids (6-7 hex chars) that the 8-char rule above misses. Without this
# a 6-char id survived _HEX_RUN_RE, then _DIGITS_RE ate its digits and left
# alphabetic residue: "Chunk ID: abc123" -> "Chunk ID: abc" while
# "Chunk ID: 123456" -> "Chunk ID:", splitting one recurring error across
# different fingerprints.
# That biased repeated_error against any error carrying an id, since promotion
# needs the same fingerprint in >= 2 sessions.
#
# The lookahead requires a DIGIT inside the run, which is what keeps sha1, md5,
# utf8, base64 and hex-safe English words (decade, facade) intact — a rule
# broad enough to catch abc123 without it would merge different
# errors, trading under-merging for over-merging.
_SHORT_HEX_ID_RE = re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,7}\b")
_DIGITS_RE = re.compile(r"\d+")
_WS_RUN_RE = re.compile(r"\s+")


def fingerprint_error_text(text: str) -> str:
    """Stable fingerprint of an error message.

    Normalization: strip UUIDs, absolute paths, hex runs (8+, and 6-7 when
    they contain a digit), digits, then collapse whitespace; sha1 the first 200
    chars of the result. Two occurrences of the same error differing only in
    paths, line numbers, addresses, or ids fingerprint identically.
    """
    t = _UUID_RE.sub("", text)
    t = _ABS_PATH_RE.sub("", t)
    t = _HEX_RUN_RE.sub("", t)
    t = _SHORT_HEX_ID_RE.sub("", t)
    t = _DIGITS_RE.sub("", t)
    t = _WS_RUN_RE.sub(" ", t).strip()
    return hashlib.sha1(t[:200].encode("utf-8")).hexdigest()


def _detect_repeated_error(
    events: list[TurnEvent],
    cfg: Config,
    occurrences_out: list[tuple[str, int]] | None = None,
) -> tuple[list[CandidateIncident], list[ErrorFingerprint]]:
    # fingerprint -> list of event indices, in order.
    occurrences: dict[str, list[int]] = {}
    for i, e in enumerate(events):
        if not e.is_error:
            continue
        fp = fingerprint_error_text(e.text)
        occurrences.setdefault(fp, []).append(i)
        if occurrences_out is not None:
            occurrences_out.append((fp, i))

    fingerprints: list[ErrorFingerprint] = []
    incidents: list[CandidateIncident] = []
    threshold = cfg.repeated_error_min_in_session
    for fp, idxs in occurrences.items():
        first = events[idxs[0]]
        fingerprints.append(
            ErrorFingerprint(
                fingerprint=fp,
                count=len(idxs),
                sample_text=redact_text(first.text[:_MATCHED_TEXT_MAX]),
                first_ts=first.ts_utc,
            )
        )
        if len(idxs) >= threshold:
            crossing_index = idxs[threshold - 1]
            incidents.append(
                CandidateIncident(
                    signal_type=SIGNAL_REPEATED_ERROR,
                    event_index=crossing_index,
                    matched_text=redact_text(first.text[:_MATCHED_TEXT_MAX]),
                    score=min(1.0, 0.5 + 0.1 * (len(idxs) - threshold)),
                    detail={"fingerprint": fp, "count": len(idxs)},
                    support_indices=tuple(idxs[:threshold]),
                )
            )
    return incidents, fingerprints


# ---------------------------------------------------------------------------
# friction_loop.
# ---------------------------------------------------------------------------


def _detect_friction_loop(
    events: list[TurnEvent], cfg: Config, stats: dict[str, int]
) -> list[CandidateIncident]:
    # Per file: ordered edit indices. Errors are shared across files.
    edits_by_file: dict[str, list[int]] = {}
    error_indices: list[int] = []
    for i, e in enumerate(events):
        if e.is_error:
            error_indices.append(i)
        if _is_edit_tool_use(e):
            # Use the same path recovery as instruction_edit. Claude tool inputs
            # can carry the path in JSON without a corresponding metadata field.
            file_path = _edited_path(e)
            if not file_path:
                stats["friction_edit_missing_file"] = (
                    stats.get("friction_edit_missing_file", 0) + 1
                )
                continue
            edits_by_file.setdefault(file_path, []).append(i)

    incidents: list[CandidateIncident] = []
    for file_path, edit_idxs in edits_by_file.items():
        # cycles[k] = (edit_idx, error_idx): edit followed by an error before
        # the next edit of the same file.
        cycles: list[tuple[int, int]] = []
        for k, edit_idx in enumerate(edit_idxs):
            bound = edit_idxs[k + 1] if k + 1 < len(edit_idxs) else len(events)
            err = next(
                (x for x in error_indices if edit_idx < x < bound), None
            )
            if err is not None:
                cycles.append((edit_idx, err))

        # Best contiguous run of cycles fitting in the event window.
        best: tuple[int, int, int] | None = None  # (n_cycles, first cycle, last cycle)
        for s in range(len(cycles)):
            for t in range(s, len(cycles)):
                span = cycles[t][1] - cycles[s][0] + 1
                if span > cfg.friction_loop_window_events:
                    break
                n = t - s + 1
                if n >= cfg.friction_loop_min_cycles and (
                    best is None or n > best[0]
                ):
                    best = (n, s, t)
        if best is not None:
            n_cycles, first_cycle, last_cycle = best
            end_err = cycles[last_cycle][1]
            support = tuple(
                sorted({i for cycle in cycles[first_cycle : last_cycle + 1] for i in cycle})
            )
            incidents.append(
                CandidateIncident(
                    signal_type=SIGNAL_FRICTION_LOOP,
                    event_index=end_err,
                    matched_text=redact_text(
                        f"{n_cycles} edit->error cycles on {file_path}"
                    )[:_MATCHED_TEXT_MAX],
                    score=min(
                        1.0, n_cycles / (cfg.friction_loop_min_cycles + 2)
                    ),
                    detail={"file": file_path, "cycles": n_cycles},
                    support_indices=support,
                )
            )
    return incidents


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def detect(events: list[TurnEvent], cfg: Config) -> FilterIncidentsResult:
    """Run every deterministic detector over one session's events.

    Returns all surviving incidents (per-signal capped, drops counted in
    ``.dropped``) plus every error fingerprint seen in the session.
    """
    stats: dict[str, int] = {}
    headless_text = sum(
        1
        for e in events
        if e.role == ROLE_HUMAN
        and e.kind != KIND_SLASH_COMMAND
        and bool(e.text)
        and e.headless
    )
    if headless_text:
        stats["headless_text_skipped"] = headless_text
    error_occurrences: list[tuple[str, int]] = []
    repeated, fingerprints = _detect_repeated_error(events, cfg, error_occurrences)
    by_signal: dict[str, list[CandidateIncident]] = {
        SIGNAL_CORRECTION: _detect_correction(events, cfg),
        SIGNAL_STANDING_INSTRUCTION: _detect_standing_instruction(events, cfg),
        SIGNAL_FRUSTRATION: _detect_frustration(events, cfg),
        SIGNAL_REPEATED_ERROR: repeated,
        SIGNAL_FRICTION_LOOP: _detect_friction_loop(events, cfg, stats),
        SIGNAL_INSTRUCTION_EDIT: _detect_instruction_edit(events, cfg, stats),
        SIGNAL_SELF_OBSERVATION: _detect_self_observation(events, cfg),
    }

    # Pre-cap candidates: measurement must not inherit the mining queue's cap.
    candidates = sorted(
        (c for incidents in by_signal.values() for c in incidents),
        key=lambda c: (c.event_index, c.signal_type),
    )
    cap = cfg.max_incidents_per_signal_per_session
    kept: list[CandidateIncident] = []
    dropped: dict[str, int] = {}
    for signal, incidents in by_signal.items():
        # Highest score first; earliest event wins ties (documented).
        ranked = sorted(incidents, key=lambda c: (-c.score, c.event_index))
        if cap is None:
            kept.extend(ranked)
            continue
        kept.extend(ranked[:cap])
        if len(ranked) > cap:
            dropped[signal] = len(ranked) - cap

    kept.sort(key=lambda c: (c.event_index, c.signal_type))
    fingerprints.sort(key=lambda f: f.first_ts or "~")
    return FilterIncidentsResult(
        incidents=kept,
        fingerprints=fingerprints,
        dropped=dropped,
        stats=stats,
        candidates=candidates,
        error_occurrences=error_occurrences,
    )
