"""Incremental transcript indexing: discovery -> parse -> filter-incidents -> persist.

`scan_all` walks every `SessionSource`, parses only bytes appended since the
last run (resume offset = `sessions.bytes_scanned`), runs the deterministic
filter-incidents detectors on the new events, and persists incidents / error fingerprints /
session bookkeeping through `Store`. No LLM calls happen here.

Contracts consumed (peer modules must conform; see also AGENTS.md):

- ``SessionSource.parse(file_path, start_offset)`` streams TurnEvents; after
  the iterator is exhausted, ``source.parse_stats`` must expose this pass's
  ``bytes_consumed`` (absolute byte offset after the last *complete* parsed
  line — never inside a truncated final line), ``lines_consumed`` (complete
  lines processed this pass, malformed included), and ``malformed_lines``
  (bad lines skipped-and-counted this pass). A mapping or a dataclass
  instance is accepted; anything else, or a missing field, fails that file
  loudly (counted in the error taxonomy — never guessed).
- ``filter_incidents.detect(events, cfg)`` returns an object with ``.incidents``
  (each with attributes ``signal_type``, ``matched_text``, ``score``,
  ``ts``), ``.fingerprints`` (each with attributes ``fingerprint``,
  ``count_in_session``, ``sample_text`` — already redacted — and
  ``first_ts``), and ``.dropped`` (dict signal_type -> count dropped by
  per-session caps).
- ``archive_trajectory.build_window(events, incident, cfg)`` returns the redacted,
  JSON-serializable context window (list) for one incident.

Deliberate choices (surfaced per fail-loud policy):

- Skip rule: a file is skipped only when the stored ``mtime`` AND
  ``file_size`` both match discovery. A failed file is recorded with
  ``mtime=0`` so the next run always retries it.
- A file whose size shrank below ``bytes_scanned`` is not append-only any
  more: it is re-parsed from offset 0 and counted under
  ``anomaly:file_shrunk_full_reparse`` (earlier incidents from it may now be
  duplicated — surfaced, not hidden).
- Denylisted sessions (project_path or slug matching
  ``cfg.denylist_substrings``) are recorded in ``sessions`` (status ``ok``)
  so they are skipped-unchanged next run, but contribute zero incidents and
  zero fingerprints; each drop is counted per matched substring.
- Session status: ``error`` on any per-file failure, ``partial`` when the
  cumulative malformed-line count is nonzero, else ``ok``.
- Cross-session promotion identifies synthetic ``repeated_error`` incidents
  by ``matched_text == fingerprint`` — that equality is the dedupe key on
  later runs.
- On a per-file failure the current transaction is rolled back (we commit
  after every successful file), so a half-persisted file never advances its
  resume offset.
- Measurement (``scan_observations.py``): every changed file also publishes a
  complete, content-free line and occurrence projection inside the same
  per-file transaction. A resumed read is reconciled by re-parsing the whole
  file from byte 0, so incremental and whole-file scans agree; the bytes this
  costs are reported. The incremental incident pass above keeps its chunk-only
  detector context. A failed file records failure history after the rollback.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .sources.base import LINE_BLANK, LINE_MALFORMED, SessionFileInfo, SessionSource, TurnEvent
from .store import Store, utc_now_iso


class ScanError(Exception):
    """The scan's own accounting contradicts itself."""


@dataclass
class ScanStats:
    """Progress accounting for one scan pass: attempted/succeeded/failed plus taxonomy.

    Invariant: ``files_attempted == files_succeeded + files_failed``;
    ``files_denylisted`` is a subset of ``files_succeeded`` (parsed and
    recorded, deliberately not mined). ``lines_scanned`` / ``malformed_lines``
    count only lines processed in THIS pass.
    """

    files_attempted: int = 0
    files_succeeded: int = 0
    files_failed: int = 0
    files_skipped_unchanged: int = 0
    files_denylisted: int = 0
    # Individual events dropped because the SOURCE marked them denylisted
    # (codex: a mid-file session_meta switched cwd into a denylisted tree
    # while the file as a whole stayed clean).
    events_denylisted: int = 0
    denylisted_by_substring: dict[str, int] = field(default_factory=dict)
    lines_scanned: int = 0
    malformed_lines: int = 0
    # Carry parser failure causes and unknown record types into scan statistics.
    # A total alone cannot explain which format or access path needs investigation.
    malformed_by_cause: dict[str, int] = field(default_factory=dict)
    unknown_lines: int = 0
    unknown_line_types: dict[str, int] = field(default_factory=dict)
    # Known record types deliberately excluded from mining, grouped by kind.
    # Changing skip volume can reveal a transcript-format shift.
    skipped_line_types: dict[str, int] = field(default_factory=dict)
    incidents_by_signal: dict[str, int] = field(default_factory=dict)
    fingerprints_recorded: int = 0
    # Incidents skipped because an identical (file, signal, ts) row already
    # existed — the rescan idempotency guard.
    incidents_deduped: int = 0
    # How each session's project identity was resolved (see
    # project_identity.METHODS). A run whose sessions are mostly
    # 'remote_url' rather than 'gh_repo_id' still collapses clones, but is one
    # repo-rename away from fracturing — so the split is reported, not hidden.
    project_key_methods: dict[str, int] = field(default_factory=dict)
    promoted_repeated_errors: int = 0
    dropped_by_cap: dict[str, int] = field(default_factory=dict)
    # Per-detector "skipped, not guessed" counters from filter_incidents
    # (``instruction_edit_missing_path``, ``friction_edit_missing_file``,
    # ``instruction_edit_path_denied``, ``headless_text_skipped``). These were
    # being computed per session and dropped on the floor: scan consumed
    # .incidents/.fingerprints/.dropped and never .stats. They exist precisely
    # so a detector that declines to guess is visible, which makes discarding
    # them the one thing that must not happen to them.
    detector_taxonomy: dict[str, int] = field(default_factory=dict)
    error_taxonomy: dict[str, int] = field(default_factory=dict)
    # Scan measurement outcomes, counted only after a successful publish:
    # observations_recorded, projections_unavailable, observation_failures_recorded,
    # full_reparses, full_reparse_bytes, lines_observed, occurrences_observed,
    # incident_links, incident_links_ambiguous, unchanged_unobserved (skipped
    # files with no observation yet), unchanged_uncovered_version (skipped files
    # whose latest observation used a different compatibility key).
    measurement: dict[str, int] = field(default_factory=dict)

    def check_invariants(self) -> None:
        """Raise if the accounting contradicts itself.

        The docstring above has stated `files_attempted == files_succeeded +
        files_failed` since this class was written, and exactly one test with
        one two-file fixture ever checked it. A documented invariant nothing
        enforces is a comment. This runs at the end of every scan, so a path
        that loses a file — an exception between the counter bumps, a `continue`
        that skips one — is a loud failure rather than a total that quietly
        does not add up in the operator's report.
        """
        if self.files_attempted != self.files_succeeded + self.files_failed:
            raise ScanError(
                "scan accounting does not add up: files_attempted "
                f"{self.files_attempted} != files_succeeded "
                f"{self.files_succeeded} + files_failed {self.files_failed}. "
                "A file was counted as attempted and then reached neither "
                "outcome."
            )
        if self.files_denylisted > self.files_succeeded:
            raise ScanError(
                f"files_denylisted {self.files_denylisted} exceeds "
                f"files_succeeded {self.files_succeeded}; denylisted files are "
                "parsed and recorded, so they are a SUBSET of the successes."
            )

        from .scan_reporting import validate_measurement, ScanReportingError
        try:
            validate_measurement(self.measurement, "scan measurement")
        except ScanReportingError as exc:
            raise ScanError(str(exc)) from exc

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class _PhaseError(Exception):
    """Wraps a per-file failure with the pipeline phase it happened in."""

    def __init__(self, phase: str, cause: Exception):
        super().__init__(f"{phase}: {type(cause).__name__}: {cause}")
        self.phase = phase
        self.cause = cause


def _bump(counter: dict[str, int], key: str, n: int = 1) -> None:
    counter[key] = counter.get(key, 0) + n


# Parse/detection counters describe work attempted. These fields instead claim
# persisted results and must roll back with the per-file transaction.
_RECORDED_STATS = (
    "incidents_by_signal", "fingerprints_recorded", "measurement",
    "files_denylisted", "denylisted_by_substring",
)


def _restore_recorded_stats(stats: ScanStats, before: dict) -> None:
    for name, value in before.items():
        setattr(stats, name, value)


def _min_iso(a: str, b: str) -> str:
    """Earlier of two ISO-UTC strings; empty means absent (never wins)."""
    if not a:
        return b
    if not b:
        return a
    return min(a, b)


def _max_iso(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


def _denylist_match(cfg: Config, project_path: str, project_slug: str) -> str:
    """Return the first denylist substring matching the session, or ''."""
    for sub in cfg.denylist_substrings:
        if (project_path and sub in project_path) or (project_slug and sub in project_slug):
            return sub
    return ""


def _parse_stats_field(parse_stats: object, name: str) -> int:
    """Extract a required int field from a source's parse_stats; loud on absence."""
    if isinstance(parse_stats, Mapping):
        if name not in parse_stats:
            raise KeyError(f"parse_stats missing required key {name!r}")
        value = parse_stats[name]
    elif dataclasses.is_dataclass(parse_stats) and not isinstance(parse_stats, type):
        if not hasattr(parse_stats, name):
            raise AttributeError(f"parse_stats missing required field {name!r}")
        value = getattr(parse_stats, name)
    else:
        raise TypeError(
            "parse_stats must be a mapping or dataclass with bytes_consumed/"
            f"lines_consumed/malformed_lines, got {type(parse_stats).__name__}"
        )
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"parse_stats[{name!r}] must be an int, got {value!r}")
    return value


def resolve_project(project_path: str, cfg: Config, cache: dict, path_cache: dict):
    """Canonical identity for a session's cwd (see project_identity.resolve).

    Isolated here so scan has one seam for it and tests can reason about the
    cache. Never raises: an unresolvable path degrades to a coarser key with
    the degradation recorded in ``method``.
    """
    from .project_identity import resolve

    return resolve(
        project_path,
        cache=cache,
        path_cache=path_cache,
        use_gh=cfg.project_identity_use_gh,
    )


#: Session count at which a promoted repeated_error saturates at score 1.0.
#: Chosen so a 2-session repeat (the promotion threshold) sits below
#: instruction_edit's 0.9 and a corpus-wide recurrence rises above it.
REPEATED_ERROR_SATURATION = 200


def repeated_error_score(n_sessions: int) -> float:
    """Normalize a cross-session repeat count into the documented [0, 1] band.

    Raw session counts would outrank every bounded detector score and starve
    other signals under ``mine_order='signal_then_recent'``. Logarithmic
    scaling preserves the ordering of recurrence counts below saturation,
    while a simple clamp would collapse them.
    """
    import math

    if n_sessions <= 1:
        return 0.5
    frac = math.log10(n_sessions) / math.log10(REPEATED_ERROR_SATURATION)
    return round(min(1.0, 0.5 + 0.5 * frac), 4)


def _session_project_key(store: Store, session_file: str) -> str:
    """The canonical project key already recorded for a session file ('' if none)."""
    row = store.get_session(session_file)
    return (row or {}).get("project_key", "") or ""


def _load_identity_cache(store: Store) -> dict:
    """Seed the in-memory cache from project_identity_cache.

    Keyed by normalized remote URL, matching project_identity.resolve's own
    cache contract.
    """
    from .project_identity import ProjectIdentity

    cache: dict = {}
    for r in store.query("SELECT * FROM project_identity_cache"):
        cache[r["remote_norm"]] = ProjectIdentity(
            r["project_key"], r["display"], r["method"]
        )
    return cache


def _save_identity_cache(store: Store, cache: dict) -> None:
    """Persist resolutions so the next run does not re-hit the network.

    Upsert rather than insert: a repo rename changes display (and can promote
    the method from remote_url to gh_repo_id) for an unchanged remote_norm.
    """
    for remote_norm, ident in cache.items():
        store.conn.execute(
            "INSERT INTO project_identity_cache "
            " (remote_norm, project_key, display, method, resolved_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(remote_norm) DO UPDATE SET "
            " project_key=excluded.project_key, display=excluded.display, "
            " method=excluded.method, resolved_at=excluded.resolved_at",
            (remote_norm, ident.key, ident.display, ident.method, utc_now_iso()),
        )


def scan_all(
    store: Store,
    cfg: Config,
    sources: list[SessionSource],
    run_id: str,
    *,
    detect_fn: Callable | None = None,
    build_window_fn: Callable | None = None,
) -> ScanStats:
    """Index every discovered transcript file incrementally; one bad file never kills the scan.

    ``detect_fn`` / ``build_window_fn`` default to ``filter_incidents.detect`` and
    ``archive_trajectory.build_window``; tests inject fakes so this module has no
    hard import-time dependency on those peers.
    """
    if detect_fn is None:
        from . import filter_incidents

        detect_fn = filter_incidents.detect
    if build_window_fn is None:
        from . import archive_trajectory

        build_window_fn = archive_trajectory.build_window

    from . import scan_observations

    stats = ScanStats()
    # Cache remote resolution across sessions, clones, and runs through
    # project_identity_cache.
    identity_cache = _load_identity_cache(store)
    # Per-run, keyed by raw cwd, checked before any subprocess: without it
    # every session pays two `git` spawns because the
    # remote-URL cache above is only reachable after both have already run.
    path_cache: dict = {}
    for source in sources:
        # Captured before any file is read, so it names the detector and parser
        # implementation this pass actually executes.
        manifest = scan_observations.detector_manifest(cfg, source, detect_fn)
        for info in source.discover():
            existing = store.get_session(info.file_path)
            if (
                existing is not None
                and existing["mtime"] == info.mtime
                and existing["file_size"] == info.size
            ):
                stats.files_skipped_unchanged += 1
                _note_unchanged_coverage(store, info, manifest, stats)
                continue
            stats.files_attempted += 1
            recorded_before = {
                name: dict(value) if isinstance(value, dict) else value
                for name in _RECORDED_STATS for value in (getattr(stats, name),)
            }
            try:
                _scan_one(
                    store, cfg, source, info, existing, run_id, stats,
                    detect_fn, build_window_fn, identity_cache, path_cache, manifest,
                )
                store.commit()
                stats.files_succeeded += 1
            except _PhaseError as exc:
                store.conn.rollback()
                _restore_recorded_stats(stats, recorded_before)
                stats.files_failed += 1
                _bump(stats.error_taxonomy, f"{exc.phase}:{type(exc.cause).__name__}")
                _record_file_error(store, info, existing, str(exc))
                _record_observation_failure(store, info, run_id, manifest, str(exc), stats)
                store.commit()
            except Exception as exc:  # our own bug — still counted, never fatal
                store.conn.rollback()
                _restore_recorded_stats(stats, recorded_before)
                stats.files_failed += 1
                _bump(stats.error_taxonomy, f"scan_internal:{type(exc).__name__}")
                _record_file_error(store, info, existing, f"scan_internal: {exc}")
                _record_observation_failure(
                    store, info, run_id, manifest, f"scan_internal: {exc}", stats
                )
                store.commit()

    _promote_repeated_errors(store, cfg, run_id, stats)
    _save_identity_cache(store, identity_cache)
    store.commit()
    # Fail loud on a total that does not add up, rather than reporting it.
    stats.check_invariants()
    return stats


def _scan_one(
    store: Store,
    cfg: Config,
    source: SessionSource,
    info: SessionFileInfo,
    existing: dict | None,
    run_id: str,
    stats: ScanStats,
    detect_fn: Callable,
    build_window_fn: Callable,
    identity_cache: dict,
    path_cache: dict,
    manifest: dict,
) -> None:
    """Parse one file from its resume offset, filter, and persist. Raises _PhaseError."""
    start_offset = 0
    if existing is not None:
        if info.size < existing["bytes_scanned"]:
            # Not append-only after all: full re-parse, loudly counted.
            _bump(stats.error_taxonomy, "anomaly:file_shrunk_full_reparse")
        else:
            start_offset = existing["bytes_scanned"]
    resumed = start_offset > 0

    # A measurable source read from byte 0 also yields the complete line
    # projection, so measurement reuses this pass instead of reading twice.
    same_pass = getattr(source, "supports_line_records", False) is True and start_offset == 0
    try:
        if same_pass:
            events: list[TurnEvent] = list(
                source.parse(info.file_path, start_offset, record_lines=True)
            )
        else:
            events = list(source.parse(info.file_path, start_offset))
    except Exception as exc:
        raise _PhaseError("parse", exc) from exc

    try:
        raw_stats = getattr(source, "parse_stats", None)
        if raw_stats is None:
            raise RuntimeError(
                f"{type(source).__name__}.parse_stats is unset after parse(); "
                "the SessionSource contract requires it"
            )
        bytes_consumed = _parse_stats_field(raw_stats, "bytes_consumed")
        lines_consumed = _parse_stats_field(raw_stats, "lines_consumed")
        malformed = _parse_stats_field(raw_stats, "malformed_lines")
        # Optional on the SessionSource contract: a source that reports no
        # taxonomy still scans, it just cannot explain itself. Named
        # differently by the two sources for historical reasons.
        cause_tax = (
            getattr(raw_stats, "error_taxonomy", None)
            or getattr(raw_stats, "malformed_taxonomy", None)
            or {}
        )
        unknown_n = int(getattr(raw_stats, "unknown_lines", 0) or 0)
        unknown_tax = getattr(raw_stats, "unknown_taxonomy", None) or {}
        skipped_tax = (
            getattr(raw_stats, "skipped_taxonomy", None)
            or getattr(raw_stats, "skipped_by_type", None)
            or {}
        )
        if bytes_consumed < start_offset:
            raise ValueError(
                f"parse_stats.bytes_consumed={bytes_consumed} went backwards from "
                f"start_offset={start_offset}"
            )
        first_pass = (
            _MeasurementRead(
                events, list(raw_stats.line_records), bytes_consumed, _truncated(raw_stats)
            )
            if same_pass
            else None
        )
    except _PhaseError:
        raise
    except Exception as exc:
        raise _PhaseError("parse_stats", exc) from exc

    stats.lines_scanned += lines_consumed
    stats.malformed_lines += malformed
    for key, count in dict(cause_tax).items():
        _bump(stats.malformed_by_cause, str(key), int(count))
    stats.unknown_lines += unknown_n
    for key, count in dict(unknown_tax).items():
        _bump(stats.unknown_line_types, str(key), int(count))
    for key, count in dict(skipped_tax).items():
        _bump(stats.skipped_line_types, str(key), int(count))

    # ---- session metadata: logical time from event data, never mtime ----
    session_id = (existing or {}).get("session_id", "") if resumed else ""
    project_path = (existing or {}).get("project_path", "") if resumed else ""
    headless = bool((existing or {}).get("headless", 0)) if resumed else False
    first_ts = (existing or {}).get("first_ts", "") if resumed else ""
    last_ts = (existing or {}).get("last_ts", "") if resumed else ""
    for ev in events:
        if not session_id and ev.session_id:
            session_id = ev.session_id
        if not project_path and ev.project_path:
            project_path = ev.project_path
        headless = headless or ev.headless
        if ev.ts_utc:
            first_ts = _min_iso(first_ts, ev.ts_utc)
            last_ts = _max_iso(last_ts, ev.ts_utc)

    lines_total = (existing["lines_scanned"] if resumed and existing else 0) + lines_consumed
    malformed_total = (existing["malformed_lines"] if resumed and existing else 0) + malformed

    # A partial flag describes unresolved errors in the current file, while the
    # cumulative malformed count records historical loss. A full read can clear the
    # flag after verifying every byte. An incremental read can set it when new errors
    # appear, but a clean tail cannot clear errors in an unread prefix.
    # mark_partial_for_reverify forces the full read needed for that decision.
    if resumed:
        was_partial = (existing or {}).get("status", "") == "partial"
        session_status = "partial" if (malformed > 0 or was_partial) else "ok"
        cause_totals = dict(_parse_cause_json(existing))
        for key, count in dict(cause_tax).items():
            _bump(cause_totals, str(key), int(count))
    else:
        session_status = "partial" if malformed > 0 else "ok"
        cause_totals = {str(k): int(v) for k, v in dict(cause_tax).items()}

    # Canonical project identity: many working copies of one repo must be one
    # project. Resolved here because scan is the only place a filesystem cwd
    # enters the system, so fixing it here fixes routing, project_count, the
    # Projects view and write amplification at once.
    identity = resolve_project(project_path, cfg, identity_cache, path_cache)
    stats.project_key_methods[identity.method] = (
        stats.project_key_methods.get(identity.method, 0) + 1
    )

    session_row = {
        "file_path": info.file_path,
        "source": info.source,
        "session_id": session_id,
        "project_path": project_path,
        "project_key": identity.key,
        "project_display": identity.display,
        "project_key_method": identity.method,
        "headless": 1 if headless else 0,
        "is_subagent": 1 if info.is_subagent else 0,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "mtime": info.mtime,
        "file_size": info.size,
        "bytes_scanned": bytes_consumed,
        "lines_scanned": lines_total,
        "malformed_lines": malformed_total,
        "malformed_by_cause": json.dumps(cause_totals, ensure_ascii=False, sort_keys=True),
        "status": session_status,
        "error": "",
        "last_scanned_at": utc_now_iso(),
    }

    # ---- denylist: applies even when discovery couldn't know the project ----
    matched = _denylist_match(cfg, project_path, info.project_slug)
    if matched:
        try:
            store.upsert_session(session_row)
        except Exception as exc:
            raise _PhaseError("persist", exc) from exc
        # Denied lines are still observed, as excluded exposure with no
        # attribution. No detection runs over them.
        _measure(
            store, cfg, source, info, run_id, manifest, stats,
            detect_fn=detect_fn, identity_cache=identity_cache, path_cache=path_cache,
            first_pass=first_pass, denied_file=True, inserted_incident_ids=set(),
        )
        stats.files_denylisted += 1
        _bump(stats.denylisted_by_substring, matched)
        return

    try:
        store.upsert_session(session_row)
    except Exception as exc:
        raise _PhaseError("persist", exc) from exc

    # ---- filter-incidents on the newly parsed events only ----
    # Honor per-event denylist marks (session metadata above deliberately used
    # the full list — a denylisted event may carry the cwd evidence). All
    # downstream indexing (incident.event_index, window extraction, incident
    # ts) uses this same clean list.
    clean_events = events
    n_denylisted = sum(1 for e in events if e.meta.get("denylisted"))
    if n_denylisted:
        clean_events = [e for e in events if not e.meta.get("denylisted")]
        stats.events_denylisted += n_denylisted
    try:
        result = detect_fn(clean_events, cfg)
        incidents = list(result.incidents)
        fingerprints = list(result.fingerprints)
        dropped = result.dropped
        if not isinstance(dropped, dict):
            raise TypeError(
                f"detect().dropped must be a dict of signal->count, got {type(dropped).__name__}"
            )
    except _PhaseError:
        raise
    except Exception as exc:
        raise _PhaseError("detect", exc) from exc

    for signal, count in dropped.items():
        _bump(stats.dropped_by_cap, str(signal), int(count))

    # Strict, like .dropped above: a detect() that forgets to return .stats
    # would otherwise silently contribute nothing, which is precisely the bug
    # this field exists to prevent (counters computed and discarded).
    try:
        detector_stats = result.stats
    except AttributeError as exc:
        raise _PhaseError(
            "detect",
            AttributeError("detect() result must carry a .stats dict"),
        ) from exc
    if detector_stats is None:
        detector_stats = {}
    if not isinstance(detector_stats, dict):
        raise _PhaseError(
            "detect",
            TypeError(
                "detect().stats must be a dict of name->count, got "
                f"{type(detector_stats).__name__}"
            ),
        )
    for name, count in detector_stats.items():
        _bump(stats.detector_taxonomy, str(name), int(count))

    inserted_incident_ids: set[str] = set()
    try:
        for inc in incidents:
            inc_ts = clean_events[inc.event_index].ts_utc
            # Idempotency: a rescan (new detectors over already-indexed
            # sessions) must not duplicate incidents that already exist in
            # ANY status — mined ones carry learnings links. Skips counted.
            if store.query_one(
                "SELECT 1 FROM incidents WHERE session_file = ? AND signal_type = ? "
                "AND ts = ?",
                (info.file_path, inc.signal_type, inc_ts),
            ):
                stats.incidents_deduped += 1
                continue
            window = build_window_fn(clean_events, inc, cfg)
            incident_id = store.insert_incident(
                {
                    "session_file": info.file_path,
                    "session_id": session_id,
                    "project_path": project_path,
                    "project_key": identity.key,
                    # Incident timestamp = the triggering event's data
                    # timestamp (CandidateIncident itself carries no ts).
                    "ts": clean_events[inc.event_index].ts_utc,
                    "signal_type": inc.signal_type,
                    "matched_text": inc.matched_text,
                    "window": window,
                    "score": float(inc.score),
                    "run_id": run_id,
                }
            )
            inserted_incident_ids.add(incident_id)
            _bump(stats.incidents_by_signal, inc.signal_type)
    except Exception as exc:
        raise _PhaseError("window", exc) from exc

    try:
        if existing is not None and not resumed:
            # A read from byte 0 recounts every error in the file. Replace the
            # file's counts; adding them again would inflate count_in_session
            # on every rescan. Appends (resumed reads) still accumulate.
            store.conn.execute(
                "DELETE FROM error_fingerprints WHERE session_file = ?", (info.file_path,)
            )
        for fp in fingerprints:
            _upsert_fingerprint(store, info.file_path, session_id, project_path, fp)
            stats.fingerprints_recorded += 1
    except Exception as exc:
        raise _PhaseError("fingerprint", exc) from exc

    _measure(
        store, cfg, source, info, run_id, manifest, stats,
        detect_fn=detect_fn, identity_cache=identity_cache, path_cache=path_cache,
        first_pass=first_pass, denied_file=False, inserted_incident_ids=inserted_incident_ids,
    )


@dataclass(frozen=True)
class _MeasurementRead:
    """One complete read from byte 0: its events and content-free line records."""

    events: list
    lines: list
    bytes_end: int
    truncated: bool


def _truncated(parse_stats: object) -> bool:
    """The unterminated-tail flag, which the two sources name differently."""
    for name in ("truncated_final_line", "truncated_tail"):
        if hasattr(parse_stats, name):
            return bool(getattr(parse_stats, name))
    raise AttributeError(f"{type(parse_stats).__name__} reports no truncated-tail flag")


def _note_unchanged_coverage(
    store: Store, info: SessionFileInfo, manifest: dict, stats: ScanStats
) -> None:
    """Count skipped files whose measurement is absent or from another version.

    No observation is claimed for an unchanged file. A changed detector over an
    unchanged transcript therefore stays explicitly uncovered for the new
    compatibility key until the file changes or is marked for rescan.
    """
    row = store.query_one(
        "SELECT compatibility_key FROM scan_observations WHERE session_file = ? "
        "AND outcome = 'succeeded' ORDER BY observed_at DESC, id DESC LIMIT 1",
        (info.file_path,),
    )
    if row is None:
        _bump(stats.measurement, "unchanged_unobserved")
    elif row["compatibility_key"] != manifest["compatibility_key"]:
        _bump(stats.measurement, "unchanged_uncovered_version")


def _record_observation_failure(
    store: Store,
    info: SessionFileInfo,
    run_id: str,
    manifest: dict,
    cause: str,
    stats: ScanStats,
) -> None:
    """Failure history for a rolled-back file, in the caller's follow-up transaction.

    Deliberately unguarded: a database failure here propagates instead of
    recursing into another failure record.
    """
    from . import scan_observations as so

    identity = so.transcript_identity(info.source, info.file_path)
    observed_at = utc_now_iso()
    cause = cause[:2000]
    observation = {
        "id": so.content_id(
            ["scan-failure/1", run_id, identity["transcript_id"],
             manifest["compatibility_key"], observed_at, info.size, cause]
        ),
        "run_id": run_id,
        "source": info.source,
        "session_file": info.file_path,
        **identity,
        "manifest": manifest,
        "file_size": info.size,
        "observed_at": observed_at,
    }
    so.record_scan_failure(store, observation=observation, cause=cause)
    _bump(stats.measurement, "observation_failures_recorded")


def _measure(
    store: Store,
    cfg: Config,
    source: SessionSource,
    info: SessionFileInfo,
    run_id: str,
    manifest: dict,
    stats: ScanStats,
    *,
    detect_fn: Callable,
    identity_cache: dict,
    path_cache: dict,
    first_pass: _MeasurementRead | None,
    denied_file: bool,
    inserted_incident_ids: set[str],
) -> None:
    """Publish this transcript's complete measurement projection. Raises _PhaseError.

    Runs inside the per-file transaction, so a failure here also rolls back the
    session offset, incidents, and fingerprints written earlier for this file.
    Counters move only after the publish succeeds.
    """
    try:
        counters = _measure_file(
            store, cfg, source, info, run_id, manifest,
            detect_fn=detect_fn, identity_cache=identity_cache, path_cache=path_cache,
            first_pass=first_pass, denied_file=denied_file,
            inserted_incident_ids=inserted_incident_ids,
        )
    except Exception as exc:
        raise _PhaseError("measure", exc) from exc
    for key, value in counters.items():
        _bump(stats.measurement, key, value)


def _measure_file(
    store: Store,
    cfg: Config,
    source: SessionSource,
    info: SessionFileInfo,
    run_id: str,
    manifest: dict,
    *,
    detect_fn: Callable,
    identity_cache: dict,
    path_cache: dict,
    first_pass: _MeasurementRead | None,
    denied_file: bool,
    inserted_incident_ids: set[str],
) -> dict[str, int]:
    from . import scan_observations as so
    from .project_identity import METHOD_UNRESOLVED

    identity = so.transcript_identity(info.source, info.file_path)
    transcript_id = identity["transcript_id"]
    compatibility = manifest["compatibility_key"]
    observed_at = utc_now_iso()
    base = {
        "run_id": run_id,
        "source": info.source,
        "session_file": info.file_path,
        **identity,
        "manifest": manifest,
        "file_size": info.size,
        "observed_at": observed_at,
        "caps": {
            "max_incidents_per_signal_per_session": cfg.max_incidents_per_signal_per_session,
            "measurement_capped": False,
        },
        "provenance": so.package_provenance(),
    }

    if getattr(source, "supports_line_records", False) is not True:
        observation = {
            **base,
            "id": so.content_id(
                ["scan-attempt/1", run_id, transcript_id, compatibility, observed_at, "unavailable"]
            ),
            "projection": "unavailable",
            "byte_end": 0,
            "line_end": 0,
            "pending_bytes": 0,
            "counts": {},
            "coverage": {"incomplete_causes": ["line_records_unavailable"], "reconciliation": "none"},
        }
        so.record_scan(store, observation=observation, lines=[], occurrences=[])
        return {"observations_recorded": 1, "projections_unavailable": 1}

    counters = {"observations_recorded": 1}
    if first_pass is None:
        full_events = list(source.parse(info.file_path, 0, record_lines=True))
        parse_stats = source.parse_stats
        read = _MeasurementRead(
            full_events,
            list(parse_stats.line_records),
            _parse_stats_field(parse_stats, "bytes_consumed"),
            _truncated(parse_stats),
        )
        reconciliation = "full_reparse"
        counters["full_reparses"] = 1
        counters["full_reparse_bytes"] = read.bytes_end
    else:
        read, reconciliation = first_pass, "same_pass"
    causes = ["truncated_tail"] if read.truncated else []

    # ---- physical lines ----
    file_headless = any(ev.headless for ev in read.events)
    lines: list[dict] = []
    for rec in read.lines:
        denied = (
            denied_file
            or rec.denylisted
            or bool(rec.project_path and _denylist_match(cfg, rec.project_path, ""))
        )
        occurred_at = so.normalize_timestamp(rec.ts_raw)
        if occurred_at:
            time_status = "known"
        else:
            time_status = "invalid" if rec.ts_raw.strip() else "missing"
        project_key = method = session_key = ""
        working_copy = None
        # Carried identity attributes later RECORDS. A blank or malformed line
        # is not a record, so it stays unattributed rather than inheriting one.
        if not denied and rec.category not in (LINE_BLANK, LINE_MALFORMED):
            if rec.project_path:
                resolved = resolve_project(rec.project_path, cfg, identity_cache, path_cache)
                method = resolved.method
                if resolved.method != METHOD_UNRESOLVED:
                    project_key = resolved.key
                    working_copy = so.working_copy_identity(project_key, rec.project_path)
            session_key = so.logical_session_key(info.source, rec.session_id)
        exclusion = so.classify_line(
            category=rec.category, denied=denied, project_key=project_key, time_status=time_status
        )
        lines.append(
            {
                "line_no": rec.line_no,
                "line_key": so.line_key(transcript_id, rec.line_no, rec.sha256),
                "sha256": rec.sha256,
                "byte_start": rec.byte_start,
                "byte_end": rec.byte_end,
                "occurred_at": occurred_at if occurred_at and not denied else "",
                "time_status": time_status,
                "source": info.source,
                "project_key": project_key,
                "project_key_method": method,
                "working_copy": working_copy,
                "logical_session_key": session_key,
                "headless": bool(file_headless if rec.headless is None else rec.headless),
                "is_subagent": bool(info.is_subagent if rec.is_subagent is None else rec.is_subagent),
                "category": rec.category,
                "cause": rec.cause,
                "exclusion": exclusion,
                "detector_coverage": so.detector_coverage(rec.category, exclusion),
                "events": rec.events,
            }
        )

    # ---- occurrences: uncapped detector candidates over the WHOLE file ----
    occurrences: list[dict] = []
    counts: dict = {"events": len(read.events)}
    links = ambiguous = 0
    if not denied_file:
        clean = [ev for ev in read.events if not ev.meta.get("denylisted")]
        result = detect_fn(clean, cfg)
        if not isinstance(result.dropped, dict):
            raise TypeError("detect().dropped must be a dict of signal->count")
        candidates = getattr(result, "candidates", None)
        error_occurrences = getattr(result, "error_occurrences", None)
        if candidates is None:
            causes.append("detector_candidates_unavailable")
        if error_occurrences is None:
            causes.append("error_occurrences_unavailable")
        ordinals: list[int] = []
        per_line: dict[int, int] = {}
        for ev in clean:
            ordinals.append(per_line.get(ev.line_no, 0))
            per_line[ev.line_no] = ordinals[-1] + 1

        def line_of(index: int) -> dict:
            if not 0 <= index < len(clean):
                raise IndexError(f"detector event index {index} outside {len(clean)} events")
            number = clean[index].line_no
            if not 1 <= number <= len(lines):
                raise ValueError(f"event {index} names line {number}, outside {len(lines)} parsed lines")
            return lines[number - 1]

        def occurrence(kind: str, signal: str, index: int, support: tuple, evidence: dict) -> dict:
            trigger = line_of(index)
            support_lines = {line_of(i)["line_no"]: line_of(i) for i in (*support, index)}
            support_keys = [support_lines[n]["line_key"] for n in sorted(support_lines)]
            discriminator = so.occurrence_discriminator(ordinals[index], evidence)
            return {
                "occurrence_id": so.occurrence_identity(
                    transcript_id=transcript_id,
                    compatibility_key=compatibility,
                    kind=kind,
                    signal_type=signal,
                    supporting_line_keys=support_keys,
                    trigger_line_key=trigger["line_key"],
                    discriminator=discriminator,
                ),
                "compatibility_key": compatibility,
                "kind": kind,
                "signal_type": signal,
                "trigger_line_key": trigger["line_key"],
                "trigger_line_no": trigger["line_no"],
                "supporting_line_keys": support_keys,
                "discriminator": discriminator,
                "occurred_at": trigger["occurred_at"],
                "evidence": evidence,
                "incident_links": [],
            }

        by_incident_key: dict[tuple[str, str], list[dict]] = {}
        for candidate in candidates or []:
            occ = occurrence(
                so.KIND_SIGNAL,
                candidate.signal_type,
                candidate.event_index,
                tuple(candidate.support_indices),
                so.identity_evidence(candidate.detail),
            )
            occurrences.append(occ)
            key = (candidate.signal_type, clean[candidate.event_index].ts_utc)
            by_incident_key.setdefault(key, []).append(occ)
        for fingerprint, index in error_occurrences or []:
            occurrences.append(
                occurrence(
                    so.KIND_ERROR_FINGERPRINT,
                    so.SIGNAL_REPEATED_ERROR,
                    index,
                    (),
                    {"fingerprint": fingerprint},
                )
            )

        # Exact links only: one occurrence and one incident per (signal, raw
        # data timestamp), the scanner's own incident dedupe key. Cross-session
        # promotions (matched_text is a fingerprint) are derived, never linked.
        incident_ids: dict[tuple[str, str], list[str]] = {}
        for row in store.query(
            "SELECT id, signal_type, ts FROM incidents WHERE session_file = ? "
            "AND NOT (signal_type = 'repeated_error' "
            "AND matched_text IN (SELECT fingerprint FROM error_fingerprints))",
            (info.file_path,),
        ):
            incident_ids.setdefault((row["signal_type"], row["ts"]), []).append(row["id"])
        for key, occs in by_incident_key.items():
            ids = incident_ids.get(key, [])
            if not ids:
                continue
            if len(occs) != 1 or len(ids) != 1:
                ambiguous += len(ids)
                continue
            kind = "produced" if ids[0] in inserted_incident_ids else "corroborated"
            occs[0]["incident_links"].append({"incident_id": ids[0], "link_kind": kind})
            links += 1
        counts.update(
            {
                "candidates": len(candidates or []),
                "error_occurrences": len(error_occurrences or []),
                "queue_dropped_by_cap": dict(result.dropped),
                "detector_stats": dict(result.stats or {}),
                "incident_links": links,
                "incident_links_ambiguous": ambiguous,
            }
        )

    digest = hashlib.sha256("".join(line["sha256"] for line in lines).encode("ascii")).hexdigest()
    observation = {
        **base,
        "id": so.content_id(
            ["scan-attempt/1", run_id, transcript_id, compatibility, observed_at, read.bytes_end, digest]
        ),
        "projection": "complete",
        "byte_end": read.bytes_end,
        "line_end": len(lines),
        "pending_bytes": max(info.size - read.bytes_end, 0),
        "counts": counts,
        "coverage": {
            "incomplete_causes": sorted(set(causes)),
            "reconciliation": reconciliation,
            "measurement_bytes_read": read.bytes_end,
            "file_denied": denied_file,
        },
    }
    so.record_scan(store, observation=observation, lines=lines, occurrences=occurrences)
    counters.update(
        {
            "lines_observed": len(lines),
            "occurrences_observed": len(occurrences),
            "incident_links": links,
            "incident_links_ambiguous": ambiguous,
        }
    )
    return counters


def _upsert_fingerprint(
    store: Store, session_file: str, session_id: str, project_path: str, fp: object
) -> None:
    """Query-first upsert into error_fingerprints (composite PK fingerprint+session_file).

    On re-scan of an appended file the per-session count accumulates and
    first_ts keeps the earliest data timestamp; sample_text keeps the first
    sample ever recorded (stable evidence).
    """
    existing = store.query_one(
        "SELECT count_in_session, first_ts FROM error_fingerprints "
        "WHERE fingerprint = ? AND session_file = ?",
        (fp.fingerprint, session_file),
    )
    if existing is None:
        store.insert(
            "error_fingerprints",
            {
                "fingerprint": fp.fingerprint,
                "session_file": session_file,
                "session_id": session_id,
                "project_path": project_path,
                "count_in_session": int(fp.count),
                "sample_text": fp.sample_text,
                "first_ts": fp.first_ts,
            },
        )
    else:
        store.conn.execute(
            "UPDATE error_fingerprints SET count_in_session = ?, first_ts = ? "
            "WHERE fingerprint = ? AND session_file = ?",
            (
                existing["count_in_session"] + int(fp.count),
                _min_iso(existing["first_ts"], fp.first_ts),
                fp.fingerprint,
                session_file,
            ),
        )


def _record_file_error(
    store: Store, info: SessionFileInfo, existing: dict | None, error_text: str
) -> None:
    """Persist a failed file's session row with mtime=0 so the next run retries it."""
    base = existing or {}
    store.upsert_session(
        {
            "file_path": info.file_path,
            "source": info.source,
            "session_id": base.get("session_id", ""),
            "project_path": base.get("project_path", ""),
            "headless": base.get("headless", 0),
            "is_subagent": 1 if info.is_subagent else 0,
            "first_ts": base.get("first_ts", ""),
            "last_ts": base.get("last_ts", ""),
            "mtime": 0.0,  # deliberately mismatched: never skipped-unchanged
            "file_size": base.get("file_size", 0),
            "bytes_scanned": base.get("bytes_scanned", 0),
            "lines_scanned": base.get("lines_scanned", 0),
            "malformed_lines": base.get("malformed_lines", 0),
            "status": "error",
            "error": error_text[:2000],
            "last_scanned_at": utc_now_iso(),
        }
    )


def _promote_repeated_errors(
    store: Store, cfg: Config, run_id: str, stats: ScanStats
) -> None:
    """Create synthetic repeated_error incidents for fingerprints spanning sessions.

    A fingerprint seen in >= cfg.repeated_error_min_sessions distinct session
    files, with no existing incident whose matched_text equals the
    fingerprint, becomes one incident anchored at the earliest-by-data-time
    contributing session; its window carries every session's redacted sample.
    """
    rows = store.query(
        "SELECT fingerprint, COUNT(DISTINCT session_file) AS n FROM error_fingerprints "
        "GROUP BY fingerprint HAVING COUNT(DISTINCT session_file) >= ?",
        (cfg.repeated_error_min_sessions,),
    )
    for row in rows:
        fingerprint = row["fingerprint"]
        already = store.query_one(
            "SELECT id FROM incidents WHERE signal_type = 'repeated_error' AND matched_text = ?",
            (fingerprint,),
        )
        if already is not None:
            continue
        members = store.query(
            "SELECT * FROM error_fingerprints WHERE fingerprint = ? "
            "ORDER BY CASE WHEN first_ts = '' THEN 1 ELSE 0 END, first_ts, session_file",
            (fingerprint,),
        )
        anchor = members[0]
        window = [
            {
                "session_file": m["session_file"],
                "project_path": m["project_path"],
                "ts": m["first_ts"],
                "count_in_session": m["count_in_session"],
                "text": m["sample_text"],
            }
            for m in members
        ]
        store.insert_incident(
            {
                "session_file": anchor["session_file"],
                "session_id": anchor["session_id"],
                "project_path": anchor["project_path"],
                # Read back from the anchor's session rather than re-resolving:
                # cross-session promotion runs after every session is indexed,
                # so the canonical key is already known and re-resolving could
                # disagree if the directory moved mid-run.
                "project_key": _session_project_key(store, anchor["session_file"]),
                "ts": anchor["first_ts"],
                "signal_type": "repeated_error",
                "matched_text": fingerprint,
                "window": window,
                # Normalized, not the raw count: see repeated_error_score.
                "score": repeated_error_score(int(row["n"])),
                "run_id": run_id,
            }
        )
        stats.promoted_repeated_errors += 1
        _bump(stats.incidents_by_signal, "repeated_error")


def _parse_cause_json(existing: Mapping | None) -> dict:
    """Read a session's stored ``malformed_by_cause``, guarding the PARSE and
    the SHAPE.

    AGENTS.md, twice: a careful guard under an unguarded ``json.loads`` cannot
    fire, and a guard that checks the parse but not the type is half a guard.
    This column is written by us, so anything else in it is a DB invariant
    violation and is named rather than absorbed. A wrong shape here would go on
    to be merged with `_bump` and could not fail loudly later.
    """
    if not existing:
        return {}
    raw = existing.get("malformed_by_cause") or "{}"
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"session {existing.get('file_path', '<unknown>')!r}: "
            f"malformed_by_cause is not valid JSON (DB invariant violation): "
            f"{raw!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"session {existing.get('file_path', '<unknown>')!r}: "
            f"malformed_by_cause is not an object (DB invariant violation): "
            f"{type(parsed).__name__} {raw!r}"
        )
    return parsed


def mark_partial_for_reverify(store: Store) -> dict:
    """Force a FULL re-read of every session currently flagged ``partial``.

    The targeted counterpart to :func:`mark_for_rescan`, and deliberately NOT
    the same function. `mark_for_rescan` DELETES each file's 'new' and
    'dismissed' incidents so they regenerate. This function resets the scan
    offset only for partial sessions and preserves every incident. The
    insert-time idempotency guard in ``_scan_one`` makes that safe when the
    re-parse emits the same events.

    Only the offset is cleared, never ``malformed_lines``: that column is the
    honest record that this session HAS lost lines, and the re-read is what
    re-derives it. Sessions whose transcript is gone are skipped and counted,
    for the same reason `mark_for_rescan` skips them: there is nothing to
    re-read, so resetting the offset would only guarantee a future no-op.
    """
    rows = store.query("SELECT file_path FROM sessions WHERE status = 'partial'")
    files = [r["file_path"] for r in rows]
    gone = [fp for fp in files if not Path(fp).exists()]
    present = [fp for fp in files if Path(fp).exists()]
    for fp in present:
        store.update(
            "sessions", "file_path", fp,
            {"bytes_scanned": 0, "lines_scanned": 0, "mtime": 0.0},
        )
    store.commit()
    return {
        "partial_sessions": len(files),
        "marked_for_full_reread": len(present),
        "skipped_transcript_gone": len(gone),
        "incidents_deleted": 0,
    }


def mark_for_rescan(store: Store, cfg: Config, project_filter: str = "") -> dict:
    """Reset scan offsets so matching sessions are re-parsed and re-filtered.

    Used after adding a detector: already-indexed sessions would otherwise
    never be looked at again (scan_all skips unchanged files by mtime+size).
    Clears bytes/lines/malformed counters and zeroes mtime (forcing re-parse),
    and DELETES that file's incidents in status 'new' or 'dismissed' so they
    are regenerated cleanly. Incidents already 'mined' are kept — they carry
    learnings links — and the insert-time idempotency guard in _scan_one
    prevents them from being duplicated.

    Sessions whose transcript file NO LONGER EXISTS are skipped entirely and
    counted as ``sessions_skipped_transcript_gone``. The re-parse regenerates
    incidents from the file; with no file there is nothing to regenerate, so
    deleting their incidents would destroy the archived ``window_json`` — the
    only surviving evidence — permanently.

    ``project_filter`` is a substring matched against project_path (falling
    back to file_path when the project is unknown); empty selects ALL
    sessions. Returns attempted/affected counts; SQL errors are not caught
    (fail loud).
    """
    like = f"%{project_filter}%"
    if project_filter:
        rows = store.query(
            "SELECT file_path FROM sessions WHERE project_path LIKE ? "
            "OR (project_path = '' AND file_path LIKE ?)",
            (like, like),
        )
    else:
        rows = store.query("SELECT file_path FROM sessions")
    files = [r["file_path"] for r in rows]
    # A session whose transcript is GONE must be left completely alone. The
    # re-parse below regenerates incidents from the file; with no file there is
    # nothing to regenerate, so deleting its 'new'/'dismissed' incidents
    # destroys the archived window_json — the only surviving evidence for that
    # incident — permanently. Resetting its offsets accomplishes nothing else,
    # so skipping is pure gain. Counted, because a silent skip and a silent
    # delete look the same from the outside.
    skipped_gone = [fp for fp in files if not Path(fp).exists()]
    files = [fp for fp in files if Path(fp).exists()]
    deleted = 0
    for fp in files:
        # Links name incidents by id; remove them with the incidents they name.
        store.conn.execute(
            "DELETE FROM scan_incident_links WHERE incident_id IN "
            "(SELECT id FROM incidents WHERE session_file = ? AND status IN ('new', 'dismissed'))",
            (fp,),
        )
        cur = store.conn.execute(
            "DELETE FROM incidents WHERE session_file = ? "
            "AND status IN ('new', 'dismissed')",
            (fp,),
        )
        deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        store.update(
            "sessions",
            "file_path",
            fp,
            {
                "bytes_scanned": 0,
                "lines_scanned": 0,
                "malformed_lines": 0,
                "mtime": 0.0,
            },
        )
    store.commit()
    return {
        "sessions_marked": len(files),
        "incidents_deleted": deleted,
        # Reported, not implied: these sessions keep their incidents because
        # their transcripts no longer exist and a re-scan cannot rebuild them.
        "sessions_skipped_transcript_gone": len(skipped_gone),
        "project_filter": project_filter or "(all)",
    }
