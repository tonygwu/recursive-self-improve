"""Miner self-eval against an explicitly selected labeled dataset.

Labels preserve source references privately. A score over one conversation
establishes recall only for that conversation, not generalization across
projects, transcript sources, or incident types. See docs/DATA_BOUNDARY.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

# Exact top-level keys of a labeled-incident YAML file (strict: missing or
# extra keys raise — a mislabeled file must never be silently half-read).
LABELED_KEYS = (
    "id",
    "title",
    "date_range",
    "source_session_ids",
    "expected_rule_gist",
    "already_encoded_in",
)

_STR_KEYS = ("id", "title", "expected_rule_gist", "already_encoded_in")

DEFAULT_PRECISION_SAMPLE_SIZE = 20


class LabeledDataError(Exception):
    """Raised for missing/empty labeled dirs or malformed labeled files."""


def _validate_labeled(data: object, origin: str) -> dict:
    if not isinstance(data, dict):
        raise LabeledDataError(
            f"{origin}: labeled incident must be a mapping, got {type(data).__name__}"
        )
    missing = [k for k in LABELED_KEYS if k not in data]
    unknown = [k for k in data if k not in LABELED_KEYS]
    if missing:
        raise LabeledDataError(f"{origin}: missing required keys {missing}")
    if unknown:
        raise LabeledDataError(
            f"{origin}: unknown keys {unknown} (allowed: {list(LABELED_KEYS)})"
        )
    for key in _STR_KEYS:
        if not isinstance(data[key], str) or not data[key].strip():
            raise LabeledDataError(
                f"{origin}: key {key!r} must be a non-empty string, got {data[key]!r}"
            )
    ids = data["source_session_ids"]
    if not isinstance(ids, list) or not all(isinstance(s, str) for s in ids):
        raise LabeledDataError(
            f"{origin}: source_session_ids must be a list of strings, got {ids!r}"
        )
    dr = data["date_range"]
    if (
        not isinstance(dr, dict)
        or set(dr) != {"start", "end"}
        or not all(isinstance(dr[k], str) and dr[k].strip() for k in ("start", "end"))
    ):
        raise LabeledDataError(
            f"{origin}: date_range must be a mapping with non-empty string keys "
            f"'start' and 'end', got {dr!r}"
        )
    return data


def load_labeled(dir_path: str | Path) -> list[dict]:
    """Load all labeled incidents from ``<dir>/*.yaml``, strictly validated.

    Missing directory, zero labeled files, and any malformed file all raise
    LabeledDataError — a self-eval over silently missing labels would report
    a meaningless recall. Files are read in sorted-name order.
    """
    d = Path(dir_path)
    if not d.is_dir():
        raise LabeledDataError(f"labeled eval dir does not exist: {d}")
    files = sorted(d.glob("*.yaml"))
    if not files:
        raise LabeledDataError(f"no labeled eval files (*.yaml) in {d}")
    items = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        items.append(_validate_labeled(data, origin=str(f)))
    return items


def _sample_for_precision(produced: list[dict], sample_size: int) -> list[dict]:
    """Deterministic sample for hand review: sort by id, then evenly spaced
    indices across the sorted list (no RNG — reruns must sample identically)."""
    ordered = sorted(produced, key=lambda learning: learning["id"])
    if len(ordered) <= sample_size:
        return ordered
    return [ordered[i * len(ordered) // sample_size] for i in range(sample_size)]


def evaluate(
    produced_learnings: list[dict],
    labeled: list[dict],
    judge: Callable[[str, str], bool],
    *,
    sample_size: int = DEFAULT_PRECISION_SAMPLE_SIZE,
) -> dict:
    """Score produced learnings against labeled incidents.

    A labeled item is *found* when ``judge(expected_rule_gist, rule_text)``
    is True for some produced learning; it fully *passes* only when a
    matching learning also has ``duplicate_of`` set (non-empty). This
    evaluation's labels describe previously encoded rules, so a match without
    that flag is a dedupe failure, counted separately from a miss.

    Returns ``{"recall", "per_item", "dedupe_failures", "sample_for_precision"}``:
    recall = full passes / len(labeled); per_item rows are
    ``{"id", "found", "flagged_duplicate"}``; sample_for_precision is a
    deterministic sample of up to ``sample_size`` produced learnings for
    hand review. Empty ``labeled`` raises (recall over nothing is
    meaningless); produced learnings missing id/rule_text/duplicate_of raise.
    """
    if not labeled:
        raise LabeledDataError("evaluate() called with no labeled items")
    for learning in produced_learnings:
        missing = [k for k in ("id", "rule_text", "duplicate_of") if k not in learning]
        if missing:
            raise LabeledDataError(
                f"produced learning {learning.get('id', '<no id>')!r} is missing "
                f"required keys {missing}"
            )

    per_item = []
    passes = 0
    dedupe_failures = 0
    for item in labeled:
        matches = [
            learning
            for learning in produced_learnings
            if judge(item["expected_rule_gist"], learning["rule_text"])
        ]
        found = bool(matches)
        flagged = any(learning["duplicate_of"] for learning in matches)
        if found and flagged:
            passes += 1
        elif found:
            dedupe_failures += 1
        per_item.append({"id": item["id"], "found": found, "flagged_duplicate": flagged})

    return {
        "recall": passes / len(labeled),
        "per_item": per_item,
        "dedupe_failures": dedupe_failures,
        "sample_for_precision": _sample_for_precision(produced_learnings, sample_size),
    }


def detection_coverage(store, labeled: list[dict]) -> dict:
    """Do the labeled incidents produce CANDIDATES at all? (no LLM, no cost)

    Candidate detection is a precondition for mining recall. Agent-discovered
    mistakes may have no tool error or corrective utterance; the
    ``instruction_edit`` and ``self_observation`` detectors cover those signals.

    Checking this separately matters because a 0 here and a 0 after mining look
    identical in the final score, while meaning completely different things:
    nothing was ever put in front of the miner, versus the miner looked and
    found nothing.

    Returns per-label counts plus ``covered`` / ``total``.
    """
    out = {"total": len(labeled), "covered": 0, "labels": []}
    for spec in labeled:
        sids = spec.get("source_session_ids") or []
        rows: list[dict] = []
        for sid in sids:
            rows += [
                dict(r)
                for r in store.query(
                    "SELECT signal_type, ts FROM incidents WHERE session_id = ?",
                    (sid,),
                )
            ]
        dr = spec.get("date_range") or {}
        start, end = dr.get("start", ""), dr.get("end", "9999-99-99")
        in_range = [r for r in rows if start <= (r["ts"] or "")[:10] <= end]
        signals: dict[str, int] = {}
        for r in in_range:
            signals[r["signal_type"]] = signals.get(r["signal_type"], 0) + 1
        out["covered"] += bool(in_range)
        out["labels"].append(
            {
                "id": spec["id"],
                "sessions": len(sids),
                "incidents_in_sessions": len(rows),
                "in_date_range": len(in_range),
                "signals": signals,
            }
        )
    return out
