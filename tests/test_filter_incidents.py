"""Tests for the filter-incidents detectors and the archive-trajectory window builder."""

from __future__ import annotations

import dataclasses

import pytest

from self_improve.config import Config
from self_improve.archive_trajectory import build_window
from self_improve.filter_incidents import (
    SIGNAL_CORRECTION,
    SIGNAL_INSTRUCTION_EDIT,
    _detect_instruction_edit,
    _edited_paths,
    SIGNAL_FRICTION_LOOP,
    SIGNAL_FRUSTRATION,
    SIGNAL_REPEATED_ERROR,
    SIGNAL_STANDING_INSTRUCTION,
    detect,
    fingerprint_error_text,
)
from self_improve.sources.base import (
    KIND_MESSAGE,
    KIND_PATCH_APPLY,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    ROLE_ASSISTANT,
    ROLE_HUMAN,
    ROLE_TOOL_RESULT,
    TurnEvent,
)

CFG = Config()


def ev(
    role: str,
    text: str = "",
    kind: str = KIND_MESSAGE,
    tool_name: str = "",
    is_error: bool = False,
    ts: str = "",
    meta: dict | None = None,
) -> TurnEvent:
    return TurnEvent(
        source="claude",
        session_file="/x/session.jsonl",
        session_id="sess",
        project_path="/proj",
        ts_utc=ts,
        role=role,
        kind=kind,
        text=text,
        tool_name=tool_name,
        is_error=is_error,
        meta=meta or {},
    )


def tool_use(tool_name: str = "Edit", meta: dict | None = None) -> TurnEvent:
    return TurnEvent(
        source="claude",
        session_file="/x/session.jsonl",
        session_id="sess",
        project_path="/proj",
        ts_utc="",
        role=ROLE_ASSISTANT,
        kind=KIND_TOOL_USE,
        text="",
        tool_name=tool_name,
        meta=meta or {},
    )


def by_signal(result, signal: str):
    return [c for c in result.incidents if c.signal_type == signal]


# ---------------------------------------------------------------------------
# correction
# ---------------------------------------------------------------------------


def test_correction_fires_after_tool_use():
    events = [
        tool_use("Edit"),
        ev(ROLE_TOOL_RESULT, "ok", kind=KIND_TOOL_RESULT),
        ev(ROLE_HUMAN, "No, that's not what I asked for"),
    ]
    hits = by_signal(detect(events, CFG), SIGNAL_CORRECTION)
    assert len(hits) == 1
    assert hits[0].event_index == 2
    assert "not_what_i" in hits[0].detail["patterns"]
    assert hits[0].score >= 0.7
    assert "not what I asked" in hits[0].matched_text


def test_correction_requires_prior_assistant_tool_use():
    events = [ev(ROLE_HUMAN, "no, that's wrong")]
    assert by_signal(detect(events, CFG), SIGNAL_CORRECTION) == []


def test_correction_false_positives_do_not_fire():
    for text in ("no worries, that works", "I know, thanks", "looks good to me"):
        events = [tool_use("Edit"), ev(ROLE_HUMAN, text)]
        assert by_signal(detect(events, CFG), SIGNAL_CORRECTION) == [], text


def test_correction_length_cap():
    long_text = "no, " + "x" * CFG.correction_max_len
    events = [tool_use("Edit"), ev(ROLE_HUMAN, long_text)]
    assert by_signal(detect(events, CFG), SIGNAL_CORRECTION) == []


def test_correction_skips_quoted_assistant_lines():
    events = [
        tool_use("Edit"),
        ev(ROLE_HUMAN, "> no, that's not what I asked\nlooks good"),
    ]
    assert by_signal(detect(events, CFG), SIGNAL_CORRECTION) == []


def test_correction_ignores_assistant_text():
    events = [
        tool_use("Edit"),
        ev(ROLE_ASSISTANT, "No, that's not what I asked for"),
    ]
    assert by_signal(detect(events, CFG), SIGNAL_CORRECTION) == []


# ---------------------------------------------------------------------------
# standing_instruction
# ---------------------------------------------------------------------------


def test_standing_instruction_fires():
    events = [ev(ROLE_HUMAN, "From now on, always run pytest before committing")]
    hits = by_signal(detect(events, CFG), SIGNAL_STANDING_INSTRUCTION)
    assert len(hits) == 1
    assert "from_now_on" in hits[0].detail["patterns"]
    assert "always" in hits[0].detail["patterns"]
    assert hits[0].score >= 0.8


def test_standing_instruction_remember_to():
    events = [ev(ROLE_HUMAN, "Remember to use uv run for tests")]
    hits = by_signal(detect(events, CFG), SIGNAL_STANDING_INSTRUCTION)
    assert len(hits) == 1


def test_standing_instruction_not_on_assistant():
    events = [ev(ROLE_ASSISTANT, "From now on I will always run pytest")]
    assert by_signal(detect(events, CFG), SIGNAL_STANDING_INSTRUCTION) == []


# ---------------------------------------------------------------------------
# frustration
# ---------------------------------------------------------------------------


def test_frustration_caps_and_question_marks():
    events = [ev(ROLE_HUMAN, "WHY is this STILL broken??")]
    hits = by_signal(detect(events, CFG), SIGNAL_FRUSTRATION)
    assert len(hits) == 1
    assert set(hits[0].detail["patterns"]) >= {"all_caps", "multi_question"}


def test_frustration_acronyms_do_not_fire():
    for text in (
        "parse the JSON and YAML config",
        "update CLAUDE.md and the TESTS.md file",
        "set CLAUDE_CONFIG_DIR before running",
    ):
        events = [ev(ROLE_HUMAN, text)]
        assert by_signal(detect(events, CFG), SIGNAL_FRUSTRATION) == [], text


def test_frustration_seriously_needs_anchor_or_question():
    events = [ev(ROLE_HUMAN, "please take this warning seriously")]
    assert by_signal(detect(events, CFG), SIGNAL_FRUSTRATION) == []
    events = [ev(ROLE_HUMAN, "seriously? it failed again?")]
    hits = by_signal(detect(events, CFG), SIGNAL_FRUSTRATION)
    assert len(hits) == 1
    assert set(hits[0].detail["patterns"]) >= {"seriously", "again_q"}


# ---------------------------------------------------------------------------
# repeated_error
# ---------------------------------------------------------------------------


def err_event(path: str, line: int, ts: str = "") -> TurnEvent:
    return ev(
        ROLE_TOOL_RESULT,
        f"FileNotFoundError: {path} line {line} not found",
        kind=KIND_TOOL_RESULT,
        is_error=True,
        ts=ts,
    )


def test_fingerprint_stable_across_paths_lines_uuids_hex():
    a = fingerprint_error_text(
        "FileNotFoundError: /Users/a/proj/main.py line 42 not found "
        "(id 550e8400-e29b-41d4-a716-446655440000, addr 0xdeadbeef)"
    )
    b = fingerprint_error_text(
        "FileNotFoundError: /home/b/other/x.py line 977 not found "
        "(id 123e4567-e89b-12d3-a456-426614174000, addr 0x1f2e3d4c)"
    )
    assert a == b
    assert a != fingerprint_error_text("TypeError: cannot add int and str")


def test_repeated_error_incident_at_threshold():
    events = [
        err_event("/Users/a/one.py", 10, ts="2026-08-01T00:00:00Z"),
        ev(ROLE_ASSISTANT, "let me fix that"),
        err_event("/Users/a/two.py", 99, ts="2026-08-01T00:01:00Z"),
        err_event("/Users/a/three.py", 7, ts="2026-08-01T00:02:00Z"),
    ]
    result = detect(events, CFG)
    hits = by_signal(result, SIGNAL_REPEATED_ERROR)
    assert len(hits) == 1
    assert hits[0].detail["count"] == 3
    # event_index is where the 3rd occurrence crossed the threshold.
    assert hits[0].event_index == 3
    assert len(result.fingerprints) == 1
    fp = result.fingerprints[0]
    assert fp.count == 3
    assert fp.first_ts == "2026-08-01T00:00:00Z"
    assert "FileNotFoundError" in fp.sample_text


def test_repeated_error_below_threshold_fingerprint_only():
    events = [
        err_event("/Users/a/one.py", 10),
        err_event("/Users/a/two.py", 99),
    ]
    result = detect(events, CFG)
    assert by_signal(result, SIGNAL_REPEATED_ERROR) == []
    assert len(result.fingerprints) == 1
    assert result.fingerprints[0].count == 2


# ---------------------------------------------------------------------------
# friction_loop
# ---------------------------------------------------------------------------


def edit_error_cycles(file_path: str, n: int) -> list[TurnEvent]:
    events: list[TurnEvent] = []
    for k in range(n):
        events.append(tool_use("Edit", meta={"file_path": file_path}))
        events.append(
            ev(
                ROLE_TOOL_RESULT,
                f"SyntaxError attempt {k}",
                kind=KIND_TOOL_RESULT,
                is_error=True,
            )
        )
    return events


def test_friction_loop_fires():
    events = edit_error_cycles("/proj/a.py", CFG.friction_loop_min_cycles)
    hits = by_signal(detect(events, CFG), SIGNAL_FRICTION_LOOP)
    assert len(hits) == 1
    assert hits[0].detail == {
        "file": "/proj/a.py",
        "cycles": CFG.friction_loop_min_cycles,
    }
    assert hits[0].event_index == len(events) - 1  # last error of the run


def test_friction_loop_needs_same_file():
    events: list[TurnEvent] = []
    for k in range(CFG.friction_loop_min_cycles):
        events += edit_error_cycles(f"/proj/file{k}.py", 1)
    assert by_signal(detect(events, CFG), SIGNAL_FRICTION_LOOP) == []


def test_friction_loop_below_min_cycles():
    events = edit_error_cycles("/proj/a.py", CFG.friction_loop_min_cycles - 1)
    assert by_signal(detect(events, CFG), SIGNAL_FRICTION_LOOP) == []


def test_friction_loop_respects_event_window():
    cycles = CFG.friction_loop_min_cycles
    events: list[TurnEvent] = []
    # Padding sized so the span from the first edit to the last error
    # (cycles-1 blocks of 2+padding events, plus the final edit+error pair)
    # strictly exceeds the window.
    #
    # Divide by cycles-1, not cycles: the span accumulates over the GAPS
    # between cycles, of which there are cycles-1. The old form happened to
    # over-shoot at window=30 and silently under-shot when the window widened
    # to 800, making the test's own precondition assertion fail rather than the
    # behaviour it guards.
    per_gap = (CFG.friction_loop_window_events - 2) // (cycles - 1) + 1
    padding = [ev(ROLE_ASSISTANT, "thinking...")] * per_gap
    for _ in range(cycles):
        events.append(tool_use("Edit", meta={"file_path": "/proj/a.py"}))
        events.append(
            ev(ROLE_TOOL_RESULT, "boom", kind=KIND_TOOL_RESULT, is_error=True)
        )
        events += padding
    span = (cycles - 1) * (2 + len(padding)) + 2
    assert span > CFG.friction_loop_window_events
    # Cycles exist but are spread beyond the window.
    assert by_signal(detect(events, CFG), SIGNAL_FRICTION_LOOP) == []


def test_friction_loop_missing_file_counted_not_guessed():
    events: list[TurnEvent] = []
    for _ in range(CFG.friction_loop_min_cycles):
        events.append(tool_use("Edit"))  # no meta file_path
        events.append(
            ev(ROLE_TOOL_RESULT, "boom", kind=KIND_TOOL_RESULT, is_error=True)
        )
    result = detect(events, CFG)
    assert by_signal(result, SIGNAL_FRICTION_LOOP) == []
    assert result.stats["friction_edit_missing_file"] == CFG.friction_loop_min_cycles


# ---------------------------------------------------------------------------
# caps and reporting
# ---------------------------------------------------------------------------


def test_per_signal_cap_reports_drops():
    # The cap is off by default now, so this exercises it explicitly.
    cfg = dataclasses.replace(CFG, max_incidents_per_signal_per_session=5)
    n = cfg.max_incidents_per_signal_per_session + 2
    events: list[TurnEvent] = [tool_use("Edit")]
    for _ in range(n):
        events.append(ev(ROLE_HUMAN, "no, that's not what I asked"))
    result = detect(events, cfg)
    hits = by_signal(result, SIGNAL_CORRECTION)
    assert len(hits) == cfg.max_incidents_per_signal_per_session
    assert result.dropped[SIGNAL_CORRECTION] == 2
    # Equal scores: earliest events win the tie-break.
    assert [h.event_index for h in hits] == list(
        range(1, cfg.max_incidents_per_signal_per_session + 1)
    )


def test_cap_keeps_highest_score_regardless_of_order():
    cfg = dataclasses.replace(CFG, max_incidents_per_signal_per_session=1)
    events = [
        tool_use("Edit"),
        ev(ROLE_HUMAN, "no, use tabs"),  # weak: one pattern
        ev(ROLE_HUMAN, "that's not what I asked, I told you to use tabs"),
    ]
    result = detect(events, cfg)
    hits = by_signal(result, SIGNAL_CORRECTION)
    assert len(hits) == 1
    assert hits[0].event_index == 2  # the stronger, later one survives
    assert result.dropped[SIGNAL_CORRECTION] == 1


def test_detect_empty_session():
    result = detect([], CFG)
    assert result.incidents == []
    assert result.fingerprints == []
    assert result.dropped == {}


def test_matched_text_is_redacted():
    events = [
        tool_use("Edit"),
        ev(ROLE_HUMAN, "no, you didn't rotate AKIAABCDEFGHIJKLMNOP"),
    ]
    hits = by_signal(detect(events, CFG), SIGNAL_CORRECTION)
    assert len(hits) == 1
    assert "AKIAABCDEFGHIJKLMNOP" not in hits[0].matched_text
    assert "[REDACTED:aws_key]" in hits[0].matched_text


# ---------------------------------------------------------------------------
# archive_trajectory.build_window
# ---------------------------------------------------------------------------


def make_incident(index: int):
    from self_improve.filter_incidents import CandidateIncident

    return CandidateIncident(
        signal_type=SIGNAL_CORRECTION,
        event_index=index,
        matched_text="x",
        score=0.5,
    )


def test_window_slice_and_shape():
    # Long enough that the configured window fits without clamping, so this
    # test measures the SLICE. Clamping has its own test below — it used to be
    # incidental (a 15-event fixture with a +/-6 window) and became the common
    # case once the archive widened to 20/10.
    n = CFG.context_turns_before + CFG.context_turns_after + 8
    centre = CFG.context_turns_before + 2
    events = [
        ev(ROLE_HUMAN, f"msg {i}", ts=f"2026-08-01T00:{i // 60:02d}:{i % 60:02d}Z")
        for i in range(n)
    ]
    window = build_window(events, make_incident(centre), CFG)
    lo = centre - CFG.context_turns_before
    hi = centre + CFG.context_turns_after + 1
    assert len(window) == hi - lo
    assert window[0] == {
        "role": ROLE_HUMAN,
        "ts": f"2026-08-01T00:{lo // 60:02d}:{lo % 60:02d}Z",
        "text": f"msg {lo}",
    }


def test_window_clamps_to_session_bounds():
    """Clamp the lookback at session boundaries so a negative slice cannot wrap."""
    events = [
        ev(ROLE_HUMAN, f"msg {i}", ts=f"2026-08-01T00:00:{i:02d}Z") for i in range(5)
    ]
    window = build_window(events, make_incident(1), CFG)
    assert len(window) == 5, "clamped window must cover the whole short session"
    assert window[0]["text"] == "msg 0", "must not wrap to the end of the session"
    assert window[-1]["text"] == "msg 4"


def test_window_clamped_at_session_start():
    events = [ev(ROLE_HUMAN, f"msg {i}") for i in range(3)]
    window = build_window(events, make_incident(0), CFG)
    assert len(window) == 3
    assert window[0]["text"] == "msg 0"


def test_window_truncation_marker():
    long_text = "A" * (CFG.context_max_chars_per_message + 500)
    events = [ev(ROLE_HUMAN, long_text)]
    window = build_window(events, make_incident(0), CFG)
    assert window[0]["text"] == (
        "A" * CFG.context_max_chars_per_message + "[truncated 500 chars]"
    )


def test_window_short_text_untouched():
    events = [ev(ROLE_HUMAN, "short")]
    window = build_window(events, make_incident(0), CFG)
    assert window[0]["text"] == "short"


def test_window_text_is_redacted():
    events = [ev(ROLE_HUMAN, "my key is AKIAABCDEFGHIJKLMNOP ok")]
    window = build_window(events, make_incident(0), CFG)
    assert "AKIAABCDEFGHIJKLMNOP" not in window[0]["text"]
    assert "[REDACTED:aws_key]" in window[0]["text"]


def test_window_pulls_in_distant_tool_result():
    events = [tool_use("Bash")]
    events += [ev(ROLE_ASSISTANT, f"step {i}") for i in range(10)]
    events.append(
        ev(
            ROLE_TOOL_RESULT,
            "the result",
            kind=KIND_TOOL_RESULT,
            ts="2026-08-01T00:09:00Z",
        )
    )
    window = build_window(events, make_incident(0), CFG)
    # slice [0, after+1) plus the tool_result at index 11.
    assert len(window) == CFG.context_turns_after + 2
    assert window[-1]["text"] == "the result"


def test_window_bad_index_raises():
    import pytest

    with pytest.raises(IndexError):
        build_window([ev(ROLE_HUMAN, "x")], make_incident(5), CFG)


# ---------------------------------------------------------------------------
# fingerprints must not split on the ids they exist to ignore
# ---------------------------------------------------------------------------


def test_short_hex_ids_do_not_split_one_error_into_many():
    """Remove short hexadecimal IDs before digit stripping leaves letter residue.
    Varying IDs must not split one recurring error into unrelated fingerprints.
    """
    a = fingerprint_error_text("Chunk ID: abc123\nProcess exited with code 127")
    b = fingerprint_error_text("Chunk ID: def456\nProcess exited with code 127")
    c = fingerprint_error_text("Chunk ID: 123456\nProcess exited with code 127")
    assert a == b == c, "the same error still fingerprints three different ways"


@pytest.mark.parametrize("word", ["sha1", "md5", "utf8", "base64", "decade", "facade"])
def test_meaningful_tokens_survive_normalization(word):
    """The fix must not become over-merging.

    A rule broad enough to catch abc123 can easily eat sha1/md5/utf8/base64 (or
    hex-safe English words like decade), which would merge genuinely different
    errors. Requiring a DIGIT inside the hex run is what keeps these.
    """
    distinct = fingerprint_error_text(f"checksum {word} mismatch")
    other = fingerprint_error_text("checksum mismatch")
    assert distinct != other, f"{word!r} was stripped, merging different errors"


def test_long_hex_and_uuids_are_still_stripped():
    """The pre-existing behaviour must not regress."""
    a = fingerprint_error_text("failed at deadbeefcafe1234 in handler")
    b = fingerprint_error_text("failed at 0123456789abcdef in handler")
    assert a == b


def test_friction_loop_fires_at_a_realistic_event_span():
    """Edit/error cycles can be separated by many intervening transcript events.
    Exercise the configured lookback with spaced cycles rather than adjacent ones.
    """
    events = []
    for _ in range(4):
        events.append(
            ev(ROLE_ASSISTANT, '{"file_path": "/p/app.py", "new_string": "x"}',
               kind=KIND_TOOL_USE, tool_name="Edit", ts="2026-08-10T00:00:00Z")
        )
        events += [ev(ROLE_ASSISTANT, "thinking", ts="2026-08-10T00:00:00Z")
                   for _ in range(25)]
        events.append(
            ev(ROLE_TOOL_RESULT, "boom", kind=KIND_TOOL_RESULT, is_error=True,
               ts="2026-08-10T00:00:00Z")
        )
        events += [ev(ROLE_ASSISTANT, "thinking", ts="2026-08-10T00:00:00Z")
                   for _ in range(35)]

    loops = [i for i in detect(events, CFG).incidents
             if i.signal_type == SIGNAL_FRICTION_LOOP]
    assert loops, (
        "four edit->error cycles on one file spanning ~240 events did not fire; "
        f"window is {CFG.friction_loop_window_events}"
    )


# ---------------------------------------------------------------------------
# Codex apply_patch paths (instruction_edit / friction_loop were Claude-only)
# ---------------------------------------------------------------------------

CODEX_PATCH_TEXT = (
    "Success. Updated the following files:\n"
    "A /Users/t/Code/proj/docs/notes.md\n"
    "M /Users/t/Code/proj/AGENTS.md\n"
    "D /Users/t/Code/proj/old.py\n"
)


def _codex_patch_event(text=CODEX_PATCH_TEXT, ts="2026-08-01T00:00:00Z"):
    return TurnEvent(
        source="codex", session_file="/s.jsonl", session_id="s",
        project_path="/Users/t/Code/proj", ts_utc=ts, role=ROLE_ASSISTANT,
        kind=KIND_PATCH_APPLY, text=text, tool_name="apply_patch",
        is_error=False, headless=True, line_no=1, meta={},
    )


def test_codex_patch_paths_are_extracted():
    """Read every status/path line from a Codex patch result for both detectors."""
    paths = _edited_paths(_codex_patch_event())
    assert paths == [
        "/Users/t/Code/proj/docs/notes.md",
        "/Users/t/Code/proj/AGENTS.md",
        "/Users/t/Code/proj/old.py",
    ]


def test_claude_json_edit_paths_still_work():
    """Regression guard: the JSON shape must keep working unchanged."""
    e = TurnEvent(
        source="claude", session_file="/s.jsonl", session_id="s", project_path="/p",
        ts_utc="2026-08-01T00:00:00Z", role=ROLE_ASSISTANT, kind=KIND_TOOL_USE,
        text='{"file_path": "/p/CLAUDE.md", "old_string": "a", "new_string": "b"}',
        tool_name="Edit", is_error=False, headless=False, line_no=1, meta={},
    )
    assert _edited_paths(e) == ["/p/CLAUDE.md"]


def test_instruction_edit_fires_when_the_rule_file_is_not_first_in_the_patch():
    """A multi-file patch must not hide the instruction file behind another.

    Returning only the first path would make the signal depend on the order
    Codex happens to list files in.
    """
    ev = _codex_patch_event()
    out = _detect_instruction_edit([ev], Config(), {})
    assert len(out) == 1, out
    assert out[0].signal_type == SIGNAL_INSTRUCTION_EDIT


def test_a_patch_with_no_recognisable_paths_is_counted_not_guessed():
    stats: dict = {}
    ev = _codex_patch_event(text="Failed to apply patch: hunk did not match\n")
    out = _detect_instruction_edit([ev], Config(), stats)
    assert out == []
    assert stats.get("instruction_edit_missing_path") == 1


def test_an_instruction_file_inside_a_hidden_scratch_dir_is_denied():
    """Hidden .tmp directories contain disposable copies and must be excluded."""
    stats: dict = {}
    ev = _codex_patch_event(
        text=(
            "Success. Updated the following files:\n"
            "M /Users/t/Documents/proj/.tmp/repo-merge-overlay/AGENTS.md\n"
        )
    )
    out = _detect_instruction_edit([ev], Config(), stats)
    assert out == []
    assert stats.get("instruction_edit_path_denied") == 1


def test_a_real_agents_md_alongside_a_denied_one_still_fires():
    """One scratch path must not disqualify a real rule file in the same patch."""
    ev = _codex_patch_event(
        text=(
            "Success. Updated the following files:\n"
            "M /Users/t/Documents/proj/.tmp/overlay/AGENTS.md\n"
            "M /Users/t/Code/proj/AGENTS.md\n"
        )
    )
    out = _detect_instruction_edit([ev], Config(), {})
    assert len(out) == 1


def test_denied_counter_means_an_instruction_edit_was_blocked():
    """Count a denied instruction edit only when the patch edits an instruction file.
    An unrelated patch in a denied directory must not inflate this counter.
    """
    stats: dict = {}
    ev = _codex_patch_event(
        text=(
            "Success. Updated the following files:\n"
            "M /Users/t/Code/proj/node_modules/pkg/index.js\n"
            "M /Users/t/Code/proj/src/thing.py\n"
        )
    )
    out = _detect_instruction_edit([ev], Config(), stats)
    assert out == []
    assert "instruction_edit_path_denied" not in stats, stats

    # But a rule file that IS blocked must still be counted.
    stats2: dict = {}
    ev2 = _codex_patch_event(
        text=(
            "Success. Updated the following files:\n"
            "M /Users/t/Code/proj/node_modules/pkg/AGENTS.md\n"
        )
    )
    assert _detect_instruction_edit([ev2], Config(), stats2) == []
    assert stats2.get("instruction_edit_path_denied") == 1, stats2


# ---------------------------------------------------------------------------
# the cap is off by default (operator decision, 2026-08-17)
# ---------------------------------------------------------------------------


def test_cap_is_disabled_by_default():
    """Default config keeps every incident and reports no cap drops.

    The explicit-cap tests cover opt-in truncation separately."""
    assert CFG.max_incidents_per_signal_per_session is None
    # Interleave tool activity: a correction only counts when an assistant
    # tool_use appears in the previous 10 events, which is a precision rule of
    # the detector and not a cap.
    events: list[TurnEvent] = []
    for _ in range(12):
        events.append(tool_use("Edit"))
        events.append(ev(ROLE_HUMAN, "no, that's not what I asked"))
    result = detect(events, CFG)
    assert len(by_signal(result, SIGNAL_CORRECTION)) == 12
    assert result.dropped == {}


def test_cap_still_works_when_set_explicitly():
    """Removing the default must not remove the mechanism."""
    cfg = dataclasses.replace(CFG, max_incidents_per_signal_per_session=3)
    events: list[TurnEvent] = [tool_use("Edit")]
    for _ in range(7):
        events.append(ev(ROLE_HUMAN, "no, that's not what I asked"))
    result = detect(events, cfg)
    assert len(by_signal(result, SIGNAL_CORRECTION)) == 3
    assert result.dropped[SIGNAL_CORRECTION] == 4


def test_zero_is_not_a_silent_synonym_for_disabled():
    """0 must mean 'keep nothing', not 'keep everything'.

    A permissive reading of 0 is exactly the accept-and-guess branch the repo
    rules forbid: it turns a misconfiguration into a quiet wrong answer.
    """
    cfg = dataclasses.replace(CFG, max_incidents_per_signal_per_session=0)
    events: list[TurnEvent] = [tool_use("Edit"), ev(ROLE_HUMAN, "no, that's wrong")]
    result = detect(events, cfg)
    assert by_signal(result, SIGNAL_CORRECTION) == []


def test_a_secret_straddling_the_truncation_boundary_cannot_leak():
    """Redaction must run BEFORE truncation, and nothing pinned that.

    `archive_trajectory` does `_truncate(redact_text(text), limit)`, and its
    docstring says why: reverse the two and a secret sitting across the cut is
    sliced in half, so the tail is discarded and the HEAD is archived in clear.
    The implementation was right; no test would have failed if a refactor
    swapped it.

    This places an AWS key so it straddles the boundary exactly.
    """
    secret = "AKIAABCDEFGHIJKLMNOP"
    limit = CFG.context_max_chars_per_message
    # A non-word character before the invented AWS key supplies the word
    # boundary required by the redaction pattern. The key then crosses the
    # archive cut, so truncating before redaction would retain its prefix.
    filler = "word " * ((limit - 6) // 5)
    filler = filler[: limit - 6 - 1] + " "
    text = filler + secret + " trailing"
    events = [ev(ROLE_HUMAN, text)]
    window = build_window(events, make_incident(0), CFG)
    archived = window[0]["text"]

    assert secret not in archived, "the whole key survived"
    # And no PREFIX of it either — that is the leak the ordering prevents.
    for cut in range(6, len(secret) + 1):
        assert secret[:cut] not in archived, (
            f"a {cut}-char prefix of the key survived truncation: "
            "redaction must run before the cut"
        )
    # The PLACEHOLDER may itself be clipped by the cut — here it lands as
    # "[REDACTED" — and that is fine. What matters is that the thing being cut
    # is the placeholder and not the key.
    assert "[REDACTED" in archived, archived[-120:]
    assert archived.endswith("chars]"), archived[-60:]


def test_the_boundary_test_is_actually_exercising_the_boundary():
    """Guard the guard: if the key no longer straddles the cut, the test above
    proves nothing. Assert the arithmetic that makes it a boundary case."""
    limit = CFG.context_max_chars_per_message
    secret = "AKIAABCDEFGHIJKLMNOP"
    filler = "word " * ((limit - 6) // 5)
    filler = filler[: limit - 6 - 1] + " "
    text = filler + secret + " trailing"
    assert filler.endswith(" "), "no word boundary before the key; redaction cannot fire"
    assert len(filler) < limit < len(filler) + len(secret), (
        "the secret does not span the truncation limit; the test is vacuous"
    )
    assert len(text) > limit, "text is not long enough to truncate at all"
