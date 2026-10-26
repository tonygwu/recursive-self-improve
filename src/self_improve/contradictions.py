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


def find_candidates(cfg: Config, paths: list[str], embedder: Embedder, *,
                    unreadable: list[dict] | None = None) -> list[dict]:
    """Embedding-similar rule-unit pairs worth judging for contradiction.

    Units come from :func:`cluster.split_rule_units` per file, DEDUPED BY TEXT
    across files (see the comment in the body), with the same
    filters and vector-cache addressing as :func:`cluster.existing_rule_vectors`
    (units under 3 content tokens skipped; owner_kind ``'rule_unit'``,
    owner_key sha1 of ``"<path>\\n<unit>"`` with the expanded path — so the
    vectors are shared with the dedup system's cache). A listed file that does
    not exist is skipped; one that exists but cannot be read raises, unless the
    caller passes an ``unreadable`` list, exactly as in
    :func:`cluster.existing_rule_vectors`.

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
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            if unreadable is None:
                raise
            unreadable.append({"path": str(path), "cause": type(exc).__name__, "detail": str(exc)})
            continue
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


class JudgeRefused(Exception):
    """The judge call was refused before it ran, for example by the call budget."""


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
    - ``llm_json`` raised :class:`JudgeRefused` for this or an earlier
      candidate (the shared call budget is spent) -> ``deferred`` and
      ``budget_deferred``; judged rows are kept;
    - budget of ``cfg.contradiction_max_judgments_per_run`` LLM attempts
      spent -> ``deferred`` (counted, never silent);
    - otherwise render ``prompts/judge_contradiction.md`` and call
      ``llm_json``. A response that is not a dict with a bool ``contradicts``
      and a str ``explanation`` is ``judge_failed``: nothing stored, nothing
      guessed. A valid response is stored as a contradictions row with
      verdict ``'contradicts'`` or ``'compatible'`` and status ``'new'`` —
      compatible rows are stored precisely so the pair is never re-judged.

    Returns ``{candidates, judged, contradicts, compatible, judge_failed,
    deferred, budget_deferred, skipped_already_judged}``. ``budget_deferred``
    is the part of ``deferred`` that the call budget refused; the rest hit
    the judgment cap.
    """
    stats = {
        "candidates": len(candidates),
        "judged": 0,
        "contradicts": 0,
        "compatible": 0,
        "judge_failed": 0,
        "deferred": 0,
        "budget_deferred": 0,
        "skipped_already_judged": 0,
    }
    template = Path(prompts_dir) / PROMPT_NAME
    refused = False
    for cand in candidates:
        # Before the refusal branch: a pair an earlier run judged was not
        # deferred by this run's budget, and counting it there would overstate
        # the work the refusal left undone.
        if _already_judged(store, cand):
            stats["skipped_already_judged"] += 1
            continue
        if refused:
            # The budget refused a judgment: keep what was judged and count
            # every remaining candidate as deferred, never as "did not run".
            stats["deferred"] += 1
            stats["budget_deferred"] += 1
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
        try:
            result = llm_json(prompt)
        except JudgeRefused:
            refused = True
            stats["deferred"] += 1
            stats["budget_deferred"] += 1
            continue
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


#: The counters every reader of a recorded sweep uses. judge_pairs has
#: written ``judged``, ``judge_failed`` and ``deferred`` since the stage was
#: added. ``budget_deferred`` came with BL-37.
SWEEP_COUNTERS = ("judged", "judge_failed", "deferred", "budget_deferred")


class SweepCounterError(ValueError):
    """A recorded sweep whose counters cannot be read.

    ``kind`` is ``missing``, ``unreadable`` or ``disagree``. ``reason`` is a
    short outcome name, such as ``unreadable judged counter``. The message
    says which value is wrong.
    """

    def __init__(self, kind: str, reason: str, detail: str):
        super().__init__(detail)
        self.kind, self.reason = kind, reason


def sweep_counters(entry: dict) -> dict[str, int]:
    """The counters of one recorded sweep entry, checked, or a raise.

    ``ever_completed``, the report's refusal lines and its pool history read
    a recorded sweep through this one check, so they agree about which
    records they can read. Each counter in ``SWEEP_COUNTERS`` must be an int
    of 0 or more, so a bool, float, string, null or negative value raises.
    A missing ``budget_deferred`` counts as 0, because an entry written
    before BL-37 has none and had no refusal. A missing ``judged``,
    ``judge_failed`` or ``deferred`` raises, because judge_pairs always
    writes them. ``budget_deferred`` above ``deferred`` raises, because each
    pair the budget left unjudged is one of the deferred pairs.
    """
    counters: dict[str, int] = {}
    for name in SWEEP_COUNTERS:
        if name not in entry:
            if name == "budget_deferred":
                counters[name] = 0
                continue
            raise SweepCounterError("missing", f"missing {name} counter", f"`{name}` is missing")
        value = entry[name]
        if type(value) is not int or value < 0:
            raise SweepCounterError(
                "unreadable", f"unreadable {name} counter",
                f"`{name}` is {value!r}, not a whole number of 0 or more",
            )
        counters[name] = value
    if counters["budget_deferred"] > counters["deferred"]:
        raise SweepCounterError(
            "disagree", "budget_deferred counter above deferred",
            f"`budget_deferred` is {counters['budget_deferred']} but `deferred` "
            f"is only {counters['deferred']}",
        )
    return counters


#: ever_completed's reason for an older-shape refusal with no verdict in this
#: database. rebuild_state deletes every contradictions row, so an empty
#: result does not show that the run stored nothing.
OLDER_REFUSAL_NO_VERDICT = (
    "skipped: budget_exhausted, and this database holds no verdict from that "
    "run; a state rebuild removes stored verdicts"
)


def stored_verdicts(store, run_id: str) -> int:
    """How many verdicts from run ``run_id`` this database holds, of either
    verdict and status.

    judge_pairs stores a row for each pair it judged and never re-judges a
    stored pair, so this is the number of pairs the run judged, unless rows
    were later removed. A state rebuild removes every row. A reader that has
    only the older refusal shape uses it as a lower bound on the run's
    judgments, and 0 does not show that the run judged nothing.
    """
    return store.query_one(
        "SELECT COUNT(*) AS n FROM contradictions WHERE run_id = ?", (run_id,)
    )["n"]


def ever_completed(store) -> tuple[bool, str]:
    """Report whether contradiction detection completed in a recorded run.

    An empty result is evidence only after detection ran. Distinguish missing or
    budget-refused stages from completed stages. Return the observed reason with
    the completion flag.

    A run whose call budget refused a judgment before any pair was judged
    (``budget_deferred > 0`` and ``judged == 0``) does not count: it found
    candidates and checked none of them. Its reason names any judgment calls
    that failed before the refusal. A run that judged some pairs before the
    refusal does
    count, the same way a run that hit its judgment cap counts. Its verdicts
    are stored, and its deferred pairs are offered again next run.

    Before BL-37 a refusal replaced the run's whole entry with ``{"skipped":
    "budget_exhausted"}``, even after the sweep had stored verdicts. Such a
    run counts when this database holds at least one verdict from it (see
    :func:`stored_verdicts`), for the same reason a newer partial refusal
    counts. With no verdict it does not count. Its reason
    (``OLDER_REFUSAL_NO_VERDICT``) does not say that the run stored nothing,
    because a state rebuild removes every stored verdict.

    Counters are read through :func:`sweep_counters`. A run whose record
    cannot be read does not count, and its reason names the run.
    """
    from collections import Counter

    outcomes: Counter = Counter()
    for row in store.query("SELECT id, stats_json FROM runs WHERE stats_json <> ''"):
        run = row["id"]
        try:
            stats = json.loads(row["stats_json"])
        except (TypeError, ValueError):
            # A run whose stats we cannot read tells us nothing either way; it
            # must not be counted as evidence that the stage ran.
            outcomes[f"unreadable stats_json in run {run}"] += 1
            continue
        if not isinstance(stats, dict):
            outcomes[f"stats_json is not an object in run {run}"] += 1
            continue
        entry = stats.get("contradictions")
        if entry is None:
            outcomes["stage absent from the run"] += 1
        elif not isinstance(entry, dict):
            outcomes[f"stage recorded a non-object in run {run}"] += 1
        elif entry.get("skipped") == "budget_exhausted":
            if stored_verdicts(store, run):
                # The older refusal shape, written after this run had stored
                # verdicts: it judged some pairs before the refusal, so it
                # counts as the newer partial refusal does.
                return True, ""
            # No row proves the run stored nothing: a state rebuild deletes
            # every contradictions row.
            outcomes[OLDER_REFUSAL_NO_VERDICT] += 1
        elif "skipped" in entry:
            outcomes[f"skipped: {entry['skipped']}"] += 1
        elif "error" in entry:
            outcomes[f"error: {entry['error']}"] += 1
        else:
            try:
                counters = sweep_counters(entry)
            except SweepCounterError as exc:
                outcomes[f"{exc.reason} in run {run}"] += 1
                continue
            if counters["budget_deferred"] and counters["judged"] == 0:
                # A failed call spent from the pool before the refusal, so
                # "refused every judgment" is true only with no failure.
                failed = counters["judge_failed"]
                outcomes["call budget refused every judgment" if not failed else
                         f"no pair judged: {failed} judgment call(s) failed, "
                         "then the call budget refused the next"] += 1
                continue
            return True, ""
    if not outcomes:
        return False, "no run has ever recorded stats"
    parts = ", ".join(f"{k} x{v}" for k, v in outcomes.most_common())
    return False, parts
