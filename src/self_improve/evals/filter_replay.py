"""Replay cached decisions through the real scanner in a disposable Store.

No network is possible here. Append schedules are reconstructed, not claimed
to be the historical nightly schedule. Native parsing is frozen upstream.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from ..data_boundary import DataBoundaryError
from ..filter_incidents import CandidateIncident, FilterIncidentsResult, detect
from ..pipeline import order_incidents_for_mining
from ..scan import scan_all
from ..sources.base import SessionFileInfo, TurnEvent
from ..store import Store
from .filter_dataset import LANGUAGE, STRUCTURAL, context, dataset_config, eligible


class FrozenSource:
    """One JSON line per normalized event; byte offsets remain real offsets."""
    name = "filter-eval"
    supports_line_records = False

    def __init__(self, paths):
        self.paths = paths

    def discover(self):
        for source, path in self.paths:
            if path.exists():
                stat = path.stat()
                yield SessionFileInfo(source, str(path), stat.st_mtime, stat.st_size, "")

    def parse(self, file_path, start_offset=0):
        lines = 0
        with open(file_path, "rb") as fh:
            fh.seek(start_offset)
            for line in fh:
                lines += 1
                yield TurnEvent(**json.loads(line))
            self.parse_stats = {"bytes_consumed": fh.tell(), "lines_consumed": lines,
                                "malformed_lines": 0}


def _cap(candidates, cfg, original):
    by_signal = defaultdict(list)
    for candidate in candidates:
        by_signal[candidate.signal_type].append(candidate)
    kept, dropped = [], {}
    for signal, group in by_signal.items():
        group.sort(key=lambda c: (-c.score, c.event_index))
        n = cfg.max_incidents_per_signal_per_session
        kept.extend(group if n is None else group[:n])
        if n is not None and len(group) > n:
            dropped[signal] = len(group) - n
    return FilterIncidentsResult(sorted(kept, key=lambda c: (c.event_index, c.signal_type)),
                                 original.fingerprints, dropped, original.stats,
                                 candidates, original.error_occurrences)


def replay(data: dict, arm: str, predictions: dict | None = None, *, threshold=.5) -> dict:
    if arm not in {"current", "regex-context", "jev"} or not 0 <= threshold <= 1:
        raise DataBoundaryError("invalid replay arm or threshold")
    if arm == "jev" and predictions is None:
        raise DataBoundaryError("Jev replay requires cached predictions")
    all_candidates, excluded = [], {}
    originals = {s["id"]: [TurnEvent(**e) for e in s["events"]] for s in data["sessions"]}
    lookup = {e.meta["filter_eval_id"]: (events, i)
              for events in originals.values() for i, e in enumerate(events)}

    with tempfile.TemporaryDirectory(prefix="filter-eval-") as directory:
        root = Path(directory)
        cfg = replace(dataset_config(data), state_dir=str(root), project_identity_use_gh=False)
        paths = [(s["source"], root / f'{s["id"]}.jsonl') for s in data["sessions"]]
        source = FrozenSource(paths)
        store = Store(root / "replay.db")

        def detector(events, config):
            original = detect(events, config)
            if arm == "current":
                result = original
            else:
                candidates = [c for c in original.candidates if c.signal_type in STRUCTURAL]
                for index, event in enumerate(events):
                    if not eligible(event):
                        continue
                    anchor = event.meta["filter_eval_id"]
                    if arm == "regex-context":
                        full, position = lookup[anchor]
                        window = context(full, position)
                        by_id = {v["id"]: v["text"] for v in window["events"]}
                        window_events = [replace(e, text=by_id[e.meta["filter_eval_id"]])
                                         for e in full[max(0, position - 20):position + 11]]
                        for c in detect(window_events, config).candidates:
                            if (c.signal_type in LANGUAGE and
                                    window_events[c.event_index].meta["filter_eval_id"] == anchor):
                                candidates.append(replace(c, event_index=index, support_indices=()))
                    else:
                        prediction = predictions.get(anchor)
                        if not prediction or prediction.get("status") != "ok":
                            excluded[anchor] = (prediction or {}).get("status", "not_requested")
                            continue
                        # An uncertain decision is a referral, so it consumes workload.
                        probs = prediction["probabilities"]
                        if probs["insufficient_context"] >= .5:
                            candidates.append(CandidateIncident("uncertain", index, "", 1.0))
                        else:
                            for signal in sorted(LANGUAGE):
                                if probs[signal] >= threshold:
                                    candidates.append(CandidateIncident(signal, index, "", probs[signal]))
                result = _cap(candidates, config, original)
            for candidate in result.candidates:
                all_candidates.append({"anchor": events[candidate.event_index].meta["filter_eval_id"],
                                       "signal": candidate.signal_type, "score": candidate.score})
            return result

        def window(events, candidate, config):
            return {"filter_eval_anchor": events[candidate.event_index].meta["filter_eval_id"]}

        stats = Counter()
        # Source-line blocks are fixed across arms and retain multiple events on one line.
        blocks = {}
        for session in data["sessions"]:
            groups = defaultdict(list)
            for i, event in enumerate(originals[session["id"]]):
                groups[(max(1, event.line_no) - 1) // data["chunk_lines"]].append(event)
            blocks[session["id"]] = list(sorted(groups.items()))
        try:
            for step in range(max(len(v) for v in blocks.values())):
                for session, (_, path) in zip(data["sessions"], paths):
                    groups = blocks[session["id"]]
                    if step >= len(groups):
                        continue
                    with path.open("a") as fh:
                        for event in groups[step][1]:
                            # No filesystem inspection of a private source's project during replay.
                            record = {**event.__dict__, "project_path": "", "session_file": str(path)}
                            fh.write(json.dumps(record) + "\n")
                result = scan_all(store, cfg, [source], f"filter-eval-{step}",
                                  detect_fn=detector, build_window_fn=window)
                if result.files_failed:
                    raise DataBoundaryError(f"replay scan failed: {result.error_taxonomy}")
                stats.update({"deduped": result.incidents_deduped,
                              "cap_dropped": sum(result.dropped_by_cap.values()),
                              "promoted_unanchored": result.promoted_repeated_errors})
            ordered, ordering = order_incidents_for_mining(store, store.query("SELECT * FROM incidents"), cfg)
            queue = []
            for rank, incident in enumerate(ordered):
                archive = json.loads(incident["window_json"])
                anchor = archive.get("filter_eval_anchor") if isinstance(archive, dict) else None
                queue.append({"anchor": anchor, "signal": incident["signal_type"],
                              "score": incident["score"], "rank": rank})
        finally:
            store.close()
    return {"schema": "filter-replay/1", "arm": arm, "candidates": all_candidates,
            "queue": queue, "stats": dict(stats), "ordering": ordering, "excluded": excluded,
            "schedule": "reconstructed source-line blocks; not historical scan times",
            "context": "new events only" if arm == "current" else "20 before / 10 after, bounded bytes",
            "structural_policy": "unchanged incremental detectors"}
