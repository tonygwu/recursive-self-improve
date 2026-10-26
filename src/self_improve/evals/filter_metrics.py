"""Exact-anchor scoring, blinded judgments, and paired session bootstrap.

Weighted anchor estimates describe only the frozen sampling frame. Episode
recall is over observed labeled episodes: sampling anchors does not enumerate
every episode, so a population episode-recall claim would be unjustified.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import random

from ..data_boundary import DataBoundaryError
from .filter_dataset import LANGUAGE, STRUCTURAL

LABELS = {"worth_mining", "not_worth_mining", "insufficient_context"}


def validate_labels(rows: list, data: dict) -> dict:
    if not isinstance(rows, list):
        raise DataBoundaryError("labels must be a list")
    cases = {c["id"]: c for c in data["cases"]}
    anchors = {e["meta"]["filter_eval_id"]: s["id"] for s in data["sessions"] for e in s["events"]}
    labels = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in cases or row["id"] in labels:
            raise DataBoundaryError("unknown or duplicate label id")
        case = cases[row["id"]]
        if (row.get("label") not in LABELS or row.get("provenance") not in {"human", "reference"} or
                not isinstance(row.get("rationale"), str) or not row["rationale"].strip() or
                not isinstance(row.get("signals"), list) or
                any(s not in LANGUAGE | STRUCTURAL for s in row["signals"])):
            raise DataBoundaryError(f'invalid label rubric: {row["id"]}')
        acceptable = row.get("acceptable_anchors")
        if not isinstance(acceptable, list) or any(anchors.get(a) != case["session"] for a in acceptable):
            raise DataBoundaryError("acceptable anchors must exist in the same session")
        if row["label"] == "worth_mining":
            if (not isinstance(row.get("episode"), str) or not row["episode"] or
                    row["id"] not in acceptable or not row["signals"]):
                raise DataBoundaryError("positive label needs episode, signals and the exact target anchor")
        elif acceptable or row.get("episode"):
            raise DataBoundaryError("nonpositive labels cannot invent episodes or acceptable anchors")
        labels[row["id"]] = row
    for row in labels.values():
        for anchor in row["acceptable_anchors"]:
            other = labels.get(anchor)
            if other and (other["label"] != "worth_mining" or other["episode"] != row["episode"]):
                raise DataBoundaryError("conflicting labels for an acceptable episode anchor")
    return labels


def _ratio(a, b):
    return a / b if b else None


def score(cases: list, labels: dict, queue: list) -> dict:
    by_anchor = defaultdict(list)
    for item in queue:
        if item.get("anchor"):
            by_anchor[item["anchor"]].append(item)
    weighted = Counter()
    episodes, found = set(), set()
    episode_anchors = defaultdict(set)
    duplicates = 0
    false_negatives, false_positives = [], []
    signal_counts = defaultdict(Counter)
    for case in cases:
        anchor = case["id"]
        label = labels[anchor]
        weight = 1 / case["inclusion_probability"]
        selected = by_anchor[anchor]
        positive = label["label"] == "worth_mining"
        uncertain = label["label"] == "insufficient_context"
        weighted["anchors"] += weight
        weighted["queue_items"] += weight * len(selected)
        weighted["selected_anchors"] += weight * bool(selected)
        weighted["reference_uncertain"] += weight * uncertain
        weighted["uncertain_referrals"] += weight * sum(x["signal"] == "uncertain" for x in selected)
        if uncertain:
            continue
        weighted["positive"] += weight * positive
        weighted["negative"] += weight * (not positive)
        weighted["tp"] += weight * (positive and bool(selected))
        weighted["fp"] += weight * (not positive and bool(selected))
        weighted["fn"] += weight * (positive and not selected)
        weighted["tn"] += weight * (not positive and not selected)
        weighted["positive_queue_items"] += weight * len(selected) * positive
        weighted["judged_queue_items"] += weight * len(selected)
        if positive:
            episode = (case["session"], label["episode"])
            episodes.add(episode)
            episode_anchors[episode].update(label["acceptable_anchors"])
            # Exact acceptable anchors only; no date/session-level credit.
            matched = sum(len(by_anchor[a]) for a in set(label["acceptable_anchors"]))
            if matched:
                found.add(episode)
            if not selected:
                false_negatives.append(anchor)
            for signal in label["signals"]:
                signal_counts[signal]["positive_weight"] += weight
                signal_counts[signal]["selected_weight"] += weight * bool(selected)
        elif selected:
            false_positives.append(anchor)
        duplicates += max(0, len(selected) - 1)
    return {"sample_anchors": len(cases), "weighted_counts": dict(weighted),
            "precision": _ratio(weighted["tp"], weighted["tp"] + weighted["fp"]),
            "anchor_recall": _ratio(weighted["tp"], weighted["positive"]),
            "queue_precision": _ratio(weighted["positive_queue_items"], weighted["judged_queue_items"]),
            "referral_rate": _ratio(weighted["selected_anchors"], weighted["anchors"]),
            "observed_episode_recall": _ratio(len(found), len(episodes)),
            "observed_episodes": len(episodes), "observed_episodes_found": len(found),
            "extra_items_same_anchor": duplicates, "by_signal": dict(signal_counts),
            "extra_items_same_observed_episode": sum(max(0, sum(len(by_anchor[a]) for a in anchors) - 1)
                                                     for anchors in episode_anchors.values()),
            "false_negative_ids": false_negatives, "false_positive_ids": false_positives}


def compare(data: dict, labels: dict, arms: dict, *, bootstrap=500, split="test") -> dict:
    cases = [c for c in data["cases"] if c["split"] == split]
    exclusions = {name: dict(arm.get("excluded", {})) for name, arm in arms.items()}
    excluded = set().union(*(set(v) for v in exclusions.values()))
    missing = {c["id"] for c in cases if c["id"] not in labels}
    paired = [c for c in cases if c["id"] not in excluded | missing]
    # Unjudged/unpaired acceptable anchors cannot rescue an episode in one arm.
    paired_ids = {c["id"] for c in paired}
    queues = {name: [r for r in arm["queue"] if r["anchor"] in paired_ids] for name, arm in arms.items()}
    results = {name: score(paired, labels, q) for name, q in queues.items()}
    human = [c for c in paired if labels[c["id"]]["provenance"] == "human"]
    for name, result in results.items():
        human_ids = {c["id"] for c in human}
        result["human_reviewed_only"] = score(human, labels, [q for q in queues[name] if q["anchor"] in human_ids])
        result["by_source"] = {s: score([c for c in paired if c["source"] == s], labels, queues[name])
                               for s in sorted({c["source"] for c in paired})}
        result["raw_candidates"] = len(arms[name]["candidates"])
        result["admitted_total"] = len(arms[name]["queue"])
        result["queue_stats"] = arms[name]["stats"]
        # Curves describe fixed frozen queue order on the judged sample, not tuned thresholds.
        result["budget_curve"] = [{"sample_queue_budget": n,
                                   "observed_episode_recall": score(paired, labels, queues[name][:n])["observed_episode_recall"]}
                                  for n in sorted({0, 1, 5, 10, 25, 50, 100, len(queues[name])}) if n <= len(queues[name])]
    deltas = {}
    if "current" in queues:
        groups = defaultdict(list)
        for case in paired:
            groups[case["session"]].append(case)
        names = sorted(groups)
        for challenger in set(queues) - {"current"}:
            samples = defaultdict(list)
            rng = random.Random(7321)
            for _ in range(bootstrap if len(names) >= 2 else 0):
                # Relabel duplicate resampled clusters so episode dedup remains correct.
                sample_cases, sample_labels, sample_queues = [], {}, {n: [] for n in queues}
                for k, sid in enumerate(rng.choices(names, k=len(names))):
                    prefix = f"{k}/"
                    for case in groups[sid]:
                        anchor = case["id"]
                        sample_cases.append({**case, "id": prefix + anchor, "session": prefix + sid})
                        label = labels[anchor]
                        sample_labels[prefix + anchor] = {**label, "acceptable_anchors": [prefix + a for a in label["acceptable_anchors"]]}
                    selected_ids = {c["id"] for c in groups[sid]}
                    for name, queue in queues.items():
                        sample_queues[name].extend({**q, "anchor": prefix + q["anchor"]} for q in queue if q["anchor"] in selected_ids)
                base = score(sample_cases, sample_labels, sample_queues["current"])
                other = score(sample_cases, sample_labels, sample_queues[challenger])
                for metric in ("precision", "anchor_recall", "observed_episode_recall", "referral_rate"):
                    if base[metric] is not None and other[metric] is not None:
                        samples[metric].append(other[metric] - base[metric])
            deltas[challenger] = {metric: {"low": sorted(v)[int(.025 * (len(v) - 1))],
                                          "high": sorted(v)[int(.975 * (len(v) - 1))],
                                          "replicates": len(v)} for metric, v in samples.items() if v}
    operating_points = {}
    if "current" in queues:
        base_budget = len(queues["current"])
        base_recall = results["current"]["observed_episode_recall"]
        for name, queue in queues.items():
            matched_recall_budget = None
            if base_recall is not None:
                for n in range(len(queue) + 1):
                    recall = score(paired, labels, queue[:n])["observed_episode_recall"]
                    if recall is not None and recall >= base_recall:
                        matched_recall_budget = n
                        break
            operating_points[name] = {"current_sample_queue_budget": base_budget,
                                      "episode_recall_at_current_budget": score(paired, labels, queue[:base_budget])["observed_episode_recall"],
                                      "sample_items_to_match_current_episode_recall": matched_recall_budget}
    return {"schema": "filter-comparison/1", "split": split, "arms": results,
            "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "paired_cases": len(paired), "paired_sessions": len({c["session"] for c in paired}),
            "missing_labels": sorted(missing), "excluded_symmetric": sorted(excluded & {c["id"] for c in cases}),
            "exclusions_per_arm": {n: {"count": len(set(e) & {c["id"] for c in cases}),
                                       "causes": dict(Counter(e[c["id"]] for c in cases if c["id"] in e))} for n, e in exclusions.items()},
            "paired_delta_ci95": deltas, "bootstrap": "paired session clusters, seed 7321",
            "sample_operating_points": operating_points,
            "population_estimates_valid": not missing and not (excluded & {c["id"] for c in cases}) and
                                          data.get("sampling", {}).get("unrepresented_strata", 1) == 0,
            "label_status": "human" if len(human) == len(paired) and paired else "provisional_or_incomplete",
            "interpretation": "Screening only. Incomplete label/prediction coverage makes weighted metrics descriptive of the paired subset, not population estimates. Complete weighted anchor estimates cover only selected sessions; observed episode recall is not population recall. Human-only slices are audited subsets. Queue totals with partial Jev coverage are not comparable production workload.",
            "winner": "not_automatically_declared"}


def review_selection(data, labels, arms, seed=932):
    selected = defaultdict(set)
    for case in data["cases"]:
        label = labels.get(case["id"])
        if not label or label["label"] == "insufficient_context":
            selected[case["id"]].add("uncertain_or_missing")
            continue
        decisions = []
        for arm in arms.values():
            if case["id"] not in arm.get("excluded", {}):
                decisions.append(any(q["anchor"] == case["id"] for q in arm["queue"]))
        if len(set(decisions + [label["label"] == "worth_mining"])) > 1:
            selected[case["id"]].add("disagreement")
    strata = defaultdict(list)
    for case in data["cases"]:
        strata[(case["source"], case["split"], case["stratum"].split("/")[-1])].append(case["id"])
    rng = random.Random(seed)
    for group in strata.values():
        for anchor in rng.sample(sorted(group), math.ceil(.2 * len(group))):
            selected[anchor].add("random_20_percent_audit")
    return {k: sorted(v) for k, v in selected.items()}
