"""Cross-file contradiction detection over instruction-file rule units.

Why (Anthropic memory docs): "if two rules contradict each other, Claude may
pick one arbitrarily" — a contradiction anywhere in the instruction-file
fleet is a silent behavior bug. Contradictory rules are TOPICALLY similar
(high cosine) with opposing polarity, so detection is two-stage: embedding
pairing proposes candidates, an LLM judge decides each pair.

Candidate band (both ends from Config, deliberately reusing the dedup
ceiling):

- ``cfg.contradiction_candidate_cosine`` (default 0.55) is the floor: pairs
  below it don't share enough subject matter to contradict.
- ``cfg.cluster_dup_cosine`` (default 0.85) is the ceiling, EXCLUSIVE: pairs
  at/above it are near-duplicates — the dedup system's business, not
  contradictions.

Pair identity is canonical: the two (file, unit) tuples are sorted, so A-B
and B-A are one pair with one orientation everywhere (candidates, storage,
already-judged lookups).

Judgment calls made here, surfaced per project policy:

- The per-run judge budget counts LLM ATTEMPTS (``judged + judge_failed``),
  not successes — the budget bounds spend, and a failed call spends. Excess
  candidates are counted ``deferred``, never silently dropped.
- Every judged pair is stored, INCLUDING ``compatible`` verdicts: the stored
  row is what prevents re-judging the same pair next run.
- An invalid/None judge response is ``judge_failed``: nothing is stored for
  that pair (it stays eligible for a future run), never guessed.
- Duplicate (file, unit) entries within one scan (the same bullet appearing
  twice) are collapsed before pairing — they would only produce degenerate
  self-pairs and doubled candidates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from .cluster import normalize_tokens, split_rule_units
from .config import Config
from .embeddings import Embedder, cosine
from .miner import render_prompt
from .store import Store, new_id, utc_now_iso

# Judge callable: rendered prompt -> parsed strict-JSON dict, or None on a
# failed call (parse failure, quota exhaustion, ...). Same shape the pipeline
# uses for other single-shot LLM JSON calls.
LlmJson = Callable[[str], dict | None]

PROMPT_NAME = "judge_contradiction.md"


def _canonical(file_1: str, unit_1: str, file_2: str, unit_2: str) -> tuple[str, str, str, str]:
    """One canonical orientation per pair: sort the two (file, unit) tuples."""
    (fa, ua), (fb, ub) = sorted([(file_1, unit_1), (file_2, unit_2)])
    return fa, ua, fb, ub


def find_candidates(cfg: Config, paths: list[str], embedder: Embedder) -> list[dict]:
    """Embedding-similar rule-unit pairs worth judging for contradiction.

    Units come from :func:`cluster.split_rule_units` per file, DEDUPED BY TEXT
    across files (see the comment in the body), with the same
    filters and vector-cache addressing as :func:`cluster.existing_rule_vectors`
    (units under 3 content tokens skipped; owner_kind ``'rule_unit'``,
    owner_key sha1 of ``"<path>\\n<unit>"`` with the expanded path — so the
    vectors are shared with the dedup system's cache). A listed file that does
    not exist is skipped; one that exists but cannot be read raises.

    Pairs — cross-file AND same-file — qualify when
    ``contradiction_candidate_cosine <= cosine < cluster_dup_cosine``; at or
    above the ceiling they are near-duplicates and belong to dedup. Returned
    dicts are ``{file_a, unit_a, file_b, unit_b, cosine}`` in canonical
    orientation, sorted by cosine descending (deterministic tie-break on the
    pair tuple). Already-judged filtering happens in :func:`judge_pairs`,
    which has the store.

    O(n^2) over unit count across the target files — fine at instruction-file
    scale (hundreds of units); revisit before feeding thousands.
    """
    entries: list[tuple[str, str, list[float]]] = []
    # Deduplicate by unit text so copies across clones do not multiply the same
    # logical pair or its model calls. Keep the first path as a concrete location.
    seen: set[str] = set()
    for p in paths:
        path = Path(p).expanduser()
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="strict")
        for unit in split_rule_units(content):
            if len(normalize_tokens(unit)) < 3:
                continue
            if unit in seen:
                continue
            seen.add(unit)
            key = hashlib.sha1(f"{path}\n{unit}".encode("utf-8")).hexdigest()
            entries.append(
                (str(path), unit, embedder.cached_vector("rule_unit", key, unit))
            )
    out: list[dict] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            score = cosine(entries[i][2], entries[j][2])
            if score < cfg.contradiction_candidate_cosine or score >= cfg.cluster_dup_cosine:
                continue
            fa, ua, fb, ub = _canonical(
                entries[i][0], entries[i][1], entries[j][0], entries[j][1]
            )
            out.append(
                {"file_a": fa, "unit_a": ua, "file_b": fb, "unit_b": ub, "cosine": score}
            )
    out.sort(
        key=lambda c: (-c["cosine"], c["file_a"], c["unit_a"], c["file_b"], c["unit_b"])
    )
    return out


def _already_judged(store: Store, cand: dict) -> bool:
    """Does the contradictions table already hold this pair, either orientation?

    Candidates arrive canonically oriented, so rows we wrote match the first
    arm; the swapped arm guards against rows that entered in the other
    orientation (e.g. manual inserts).
    """
    row = store.query_one(
        "SELECT id FROM contradictions "
        "WHERE (file_a = ? AND unit_a = ? AND file_b = ? AND unit_b = ?) "
        "   OR (file_a = ? AND unit_a = ? AND file_b = ? AND unit_b = ?)",
        (
            cand["file_a"], cand["unit_a"], cand["file_b"], cand["unit_b"],
            cand["file_b"], cand["unit_b"], cand["file_a"], cand["unit_a"],
        ),
    )
    return row is not None


def judge_pairs(
    store: Store,
    cfg: Config,
    candidates: list[dict],
    llm_json: LlmJson,
    prompts_dir: str | Path,
    run_id: str,
) -> dict:
    """LLM-judge candidate pairs; store every completed verdict.

    Per candidate, in order (highest cosine first as produced by
    :func:`find_candidates`):

    - already in the contradictions table (either orientation) ->
      ``skipped_already_judged``, no budget consumed;
    - budget of ``cfg.contradiction_max_judgments_per_run`` LLM attempts
      spent -> ``deferred`` (counted, never silent);
    - otherwise render ``prompts/judge_contradiction.md`` and call
      ``llm_json``. A response that is not a dict with a bool ``contradicts``
      and a str ``explanation`` is ``judge_failed``: nothing stored, nothing
      guessed. A valid response is stored as a contradictions row with
      verdict ``'contradicts'`` or ``'compatible'`` and status ``'new'`` —
      compatible rows are stored precisely so the pair is never re-judged.

    Returns ``{candidates, judged, contradicts, compatible, judge_failed,
    deferred, skipped_already_judged}``.
    """
    stats = {
        "candidates": len(candidates),
        "judged": 0,
        "contradicts": 0,
        "compatible": 0,
        "judge_failed": 0,
        "deferred": 0,
        "skipped_already_judged": 0,
    }
    template = Path(prompts_dir) / PROMPT_NAME
    for cand in candidates:
        if _already_judged(store, cand):
            stats["skipped_already_judged"] += 1
            continue
        if stats["judged"] + stats["judge_failed"] >= cfg.contradiction_max_judgments_per_run:
            stats["deferred"] += 1
            continue
        prompt = render_prompt(
            template,
            {
                "file_a": cand["file_a"],
                "unit_a": cand["unit_a"],
                "file_b": cand["file_b"],
                "unit_b": cand["unit_b"],
            },
        )
        result = llm_json(prompt)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("contradicts"), bool)
            or not isinstance(result.get("explanation"), str)
        ):
            stats["judge_failed"] += 1
            continue
        verdict = "contradicts" if result["contradicts"] else "compatible"
        stats["judged"] += 1
        stats[verdict] += 1
        store.insert(
            "contradictions",
            {
                "id": new_id(),
                "run_id": run_id,
                "file_a": cand["file_a"],
                "unit_a": cand["unit_a"],
                "file_b": cand["file_b"],
                "unit_b": cand["unit_b"],
                "cosine": float(cand["cosine"]),
                "verdict": verdict,
                "explanation": result["explanation"],
                "status": "new",
                "created_at": utc_now_iso(),
            },
        )
    store.commit()
    return stats


def report_open(store: Store) -> list[dict]:
    """Unresolved contradiction findings for the CLI/report.

    Rows with verdict ``'contradicts'`` still in status ``'new'`` (the user
    has neither dismissed nor resolved them), strongest topical overlap
    first.
    """
    return store.query(
        "SELECT * FROM contradictions "
        "WHERE status = 'new' AND verdict = 'contradicts' "
        "ORDER BY cosine DESC, created_at ASC"
    )


def ever_completed(store) -> tuple[bool, str]:
    """Report whether contradiction detection completed in a recorded run.

    An empty result is evidence only after detection ran. Distinguish missing or
    budget-refused stages from completed stages. Return the observed reason with
    the completion flag.
    """
    from collections import Counter

    outcomes: Counter = Counter()
    for row in store.query("SELECT stats_json FROM runs WHERE stats_json <> ''"):
        try:
            stats = json.loads(row["stats_json"])
        except (TypeError, ValueError):
            # A run whose stats we cannot read tells us nothing either way; it
            # must not be counted as evidence that the stage ran.
            outcomes["unreadable stats_json"] += 1
            continue
        entry = stats.get("contradictions")
        if entry is None:
            outcomes["stage absent from the run"] += 1
        elif not isinstance(entry, dict):
            outcomes["stage recorded a non-object"] += 1
        elif "skipped" in entry:
            outcomes[f"skipped: {entry['skipped']}"] += 1
        elif "error" in entry:
            outcomes[f"error: {entry['error']}"] += 1
        else:
            return True, ""
    if not outcomes:
        return False, "no run has ever recorded stats"
    parts = ", ".join(f"{k} x{v}" for k, v in outcomes.most_common())
    return False, parts
