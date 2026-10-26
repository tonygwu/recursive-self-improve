"""Private, immutable inputs for the pre-mining experiment. No runtime Store.

The sampling population is explicitly the selected sessions, not all history.
Native parsers run once at freeze time; replay uses their redacted events.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
import hashlib
import importlib
import json
from pathlib import Path
import random

from ..config import Config
from ..data_boundary import DataBoundaryError, _write_bundle, private_storage_path, verify_files
from ..filter_incidents import detect
from ..redact import redact_text
from ..scan_observations import DETECTION_CONFIG_FIELDS, detector_manifest
from ..sources.base import TurnEvent

SCHEMA = "filter-dataset/1"
LANGUAGE = frozenset({"correction", "standing_instruction", "frustration", "self_observation"})
STRUCTURAL = frozenset({"repeated_error", "friction_loop", "instruction_edit"})


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def eligible(event: TurnEvent) -> bool:
    # Deliberately omit regex proximity/length gates: these are hypotheses to test.
    return (bool(event.text) and not event.meta.get("denylisted") and
            ((event.role == "human" and not event.headless and event.kind != "slash_command") or
             (event.role == "assistant" and event.kind == "message")))


def context(events: list[TurnEvent], index: int) -> dict:
    """Same bounded, role-labelled text for both matched-context arms.

    Allocate the anchor first, then nearest neighbors; retain chronological order.
    Byte limits conservatively bound token input, including non-ASCII text.
    """
    lo, hi = max(0, index - 20), min(len(events), index + 11)
    remaining = 20000
    texts = {}
    for i in sorted(range(lo, hi), key=lambda j: (abs(j - index), j)):
        raw = redact_text(events[i].text).encode()
        take = raw[:min(6000, remaining)].decode("utf-8", errors="ignore")
        remaining -= len(take.encode())
        texts[i] = (take, len(take.encode()) < len(raw))
    return {"anchor": events[index].meta["filter_eval_id"], "events": [
        {"id": events[i].meta["filter_eval_id"], "role": events[i].role,
         "kind": events[i].kind, "text": texts[i][0], "truncated": texts[i][1]}
        for i in range(lo, hi)], "before": index - lo, "after": hi - index - 1}


def config_values(cfg: Config) -> dict:
    return json.loads(encoded({name: getattr(cfg, name) for name in (*DETECTION_CONFIG_FIELDS, "mine_order")}))


def runtime_fingerprint():
    names = ("filter_incidents", "scan", "redact", "pipeline", "sources.base",
             "sources.claude_code", "sources.codex", "evals.filter_dataset", "evals.filter_replay")
    return {name: hashlib.sha256(Path(importlib.import_module("self_improve." + name).__file__).read_bytes()).hexdigest()
            for name in names}


def _baseline_signature(result):
    return ([(c.event_index, c.signal_type, c.score) for c in result.candidates],
            [(f.fingerprint, f.count, f.first_ts) for f in result.fingerprints],
            result.error_occurrences)


def dataset_config(data: dict) -> Config:
    values = data["config"].copy()
    for name in ("denylist_substrings", "instruction_edit_filenames"):
        values[name] = tuple(values[name])
    return replace(Config(), **values)


def prepare(cfg: Config, destination: Path, *, count=1000, sessions=60, seed=20261007,
            chunk_lines=200, sources=None) -> dict:
    """Freeze latest nonempty sessions per provider, then probability-sample anchors.

    Session selection is purposive and recorded. Within each session/hit stratum
    anchor selection is uniform without replacement with a known inclusion rate.
    """
    from ..sources.claude_code import ClaudeCodeSource
    from ..sources.codex import CodexSource
    from .filter_replay import replay
    from ..data_boundary import private_destination

    private_destination(destination)
    if count < 1 or sessions < 2 or chunk_lines < 1:
        raise DataBoundaryError("count >= 1, sessions >= 2 and chunk-lines >= 1 required")
    sources = sources if sources is not None else [ClaudeCodeSource(cfg), CodexSource(cfg)]
    frozen, excluded, manifests = [], defaultdict(int), {}
    seen = set()
    for source in sources:
        manifests[source.name] = detector_manifest(cfg, source, detect)
        infos = sorted(source.discover(), key=lambda x: (-x.mtime, x.file_path))
        taken = 0
        for info in infos:
            if taken >= (sessions + len(sources) - 1) // len(sources):
                break
            path = Path(info.file_path)
            before = path.stat()
            events = list(source.parse(info.file_path))
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                excluded["changed_during_parse"] += 1
                continue
            if not events or not any(eligible(e) for e in events):
                excluded["no_eligible_text"] += 1
                continue
            identity = (source.name, events[0].session_id or str(path.resolve()))
            if identity in seen:
                excluded["duplicate_native_session"] += 1
                continue
            project = next((e.project_path for e in events if e.project_path), "")
            if any(s in project or s in info.project_slug for s in cfg.denylist_substrings):
                excluded["denied_session"] += 1
                continue
            events = [e for e in events if not e.meta.get("denylisted")]
            sid = digest(identity)[:24]
            records = []
            for i, event in enumerate(events):
                record = redact(asdict(event))
                record["meta"]["filter_eval_id"] = f"{sid}:{i}"
                records.append(record)
            # Redaction must not change the current detector's decisions unnoticed.
            # Compare each reconstructed append slice before discarding raw text.
            groups = defaultdict(list)
            for i, event in enumerate(events):
                groups[(max(1, event.line_no) - 1) // chunk_lines].append(i)
            changed = False
            for indices in groups.values():
                raw_hits = detect([events[i] for i in indices], cfg)
                redacted_hits = detect([TurnEvent(**records[i]) for i in indices], cfg)
                if _baseline_signature(raw_hits) != _baseline_signature(redacted_hits):
                    changed = True
                    break
            if changed:
                excluded["redaction_changed_baseline"] += 1
                continue
            seen.add(identity)
            frozen.append({"id": sid, "source": source.name, "project": redact_text(project),
                           "source_bytes": before.st_size, "source_mtime_ns": before.st_mtime_ns,
                           "events": records})
            taken += 1
    if not frozen:
        raise DataBoundaryError("no eligible sessions; never substitute synthetic data")
    rng = random.Random(seed)
    # Random session split before anchor sampling; no overlapping windows cross splits.
    for provider in sorted({s["source"] for s in frozen}):
        group = [s for s in frozen if s["source"] == provider]
        rng.shuffle(group)
        dev_n = max(1, round(len(group) * .2)) if len(group) > 1 else 0
        for i, session in enumerate(group):
            session["split"] = "dev" if i < dev_n else "test"
    data = {"schema": SCHEMA, "config": config_values(cfg), "sessions": frozen,
            "seed": seed, "chunk_lines": chunk_lines, "cases": []}
    # Freeze deterministic predictions before either labels or challenger responses exist.
    baseline = replay(data, "current")
    matched = replay(data, "regex-context")
    hits = {x["anchor"] for x in baseline["candidates"]}
    structural = {x["anchor"] for x in baseline["candidates"] if x["signal"] in STRUCTURAL}
    strata = defaultdict(list)
    for session in frozen:
        for i, record in enumerate(session["events"]):
            event = TurnEvent(**record)
            anchor = event.meta["filter_eval_id"]
            if eligible(event) or anchor in structural:
                key = f'{session["id"]}/{"hit" if anchor in hits else "unselected"}'
                strata[key].append({"id": anchor, "session": session["id"], "index": i,
                                    "source": session["source"], "split": session["split"],
                                    "semantic_eligible": eligible(event), "stratum": key})
    # Equal allocation across nonempty session/hit strata, with census for small strata.
    allocation = {k: 0 for k in strata}
    keys = sorted(strata)
    rng.shuffle(keys)
    remaining = min(count, sum(map(len, strata.values())))
    while remaining:
        for key in keys:
            if allocation[key] < len(strata[key]) and remaining:
                allocation[key] += 1
                remaining -= 1
    for key in sorted(strata):
        population, n = len(strata[key]), allocation[key]
        for case in rng.sample(strata[key], n):
            case["inclusion_probability"] = n / population
            data["cases"].append(case)
    rng.shuffle(data["cases"])
    data["sampling"] = {"population": "eligible anchors in selected sessions only",
                        "session_selection": "most recent mtime per provider; not random history",
                        "strata": {k: {"population": len(v), "sample": allocation[k]} for k, v in strata.items()},
                        "requested_count": count, "requested_sessions": sessions,
                        "unrepresented_strata": sum(n == 0 for n in allocation.values()),
                        "excluded_sessions": dict(excluded)}
    return _write_bundle(destination, {"dataset.json": encoded(data),
                                      "current.json": encoded(baseline),
                                      "regex-context.json": encoded(matched)},
                         {"schema": SCHEMA, "kind": "private", "parsers": manifests,
                          "runtime": runtime_fingerprint(),
                          "session_count": len(frozen), "case_count": len(data["cases"])})


def load(root: Path) -> tuple[dict, dict]:
    private_storage_path(root)
    manifest = verify_files(root)
    if manifest.get("schema") != SCHEMA or manifest.get("kind") != "private":
        raise DataBoundaryError("expected a private filter-dataset/1 bundle")
    if manifest.get("runtime") != runtime_fingerprint():
        raise DataBoundaryError("replay code differs from frozen baseline; use the original revision")
    if set(manifest["files"]) != {"dataset.json", "current.json", "regex-context.json"}:
        raise DataBoundaryError("unexpected filtering dataset members")
    data = json.loads((root / "dataset.json").read_text())
    if data.get("schema") != SCHEMA or not isinstance(data.get("cases"), list) or not data["cases"]:
        raise DataBoundaryError("invalid or empty filtering cases")
    ids, sessions = set(), {}
    for session in data["sessions"]:
        if session["id"] in sessions or session["split"] not in {"dev", "test"}:
            raise DataBoundaryError("duplicate session or invalid split")
        sessions[session["id"]] = session
        for event in session["events"]:
            item = TurnEvent(**event)
            anchor = item.meta["filter_eval_id"]
            if anchor in ids:
                raise DataBoundaryError("duplicate anchor")
            ids.add(anchor)
    case_ids = set()
    for case in data["cases"]:
        session = sessions[case["session"]]
        if (case["id"] in case_ids or case["id"] not in ids or
                case["split"] != session["split"] or
                session["events"][case["index"]]["meta"]["filter_eval_id"] != case["id"] or
                type(case["inclusion_probability"]) not in (int, float) or
                not 0 < case["inclusion_probability"] <= 1):
            raise DataBoundaryError("invalid case identity, split or inclusion probability")
        case_ids.add(case["id"])
    return data, manifest


def case_context(data: dict, case: dict) -> dict:
    session = next(s for s in data["sessions"] if s["id"] == case["session"])
    return context([TurnEvent(**e) for e in session["events"]], case["index"])
