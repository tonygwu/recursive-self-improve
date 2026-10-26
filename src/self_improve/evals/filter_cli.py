"""Standalone experiment commands, dispatched before live state is opened."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from ..data_boundary import DataBoundaryError, private_storage_path
from .filter_dataset import case_context, load, prepare
from .filter_metrics import compare, review_selection, validate_labels
from .filter_models import atomic_json, pilot_cases, run, verified_predictions
from .filter_replay import replay


def add_parser(sub):
    parser = sub.add_parser("eval-filter", help="private pre-mining benchmark; Jev uses bounded API calls, reference uses quota-selected CLI accounts")
    parser.add_argument("mode", choices=["prepare", "baseline", "reference", "jev", "review", "compare", "agreement"])
    parser.add_argument("--dataset", help="frozen private filtering bundle")
    parser.add_argument("--output", required=True, help="private output file, or directory for prepare/model modes")
    parser.add_argument("--labels", help="JSON with dataset_sha and rows; human or reference judgments")
    parser.add_argument("--predictions", help="retained Jev result JSON, bound to this dataset")
    parser.add_argument("--count", type=int, default=1000, help="sampled anchors when preparing")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--chunk-lines", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--limit", type=int, default=50, help="fixed pilot membership for paid calls")
    parser.add_argument("--order-seed", type=int, default=0, help="repeat pilot with 0, 1, 2 using one spend ledger")
    parser.add_argument("--jev-gateway", choices=["typesafe", "typesafe-1.14", "zen-free"], default="typesafe",
                        help="typesafe pins 1.13.0; typesafe-1.14 pins paid 1.14.0; zen-free is anonymous 1.13-free; no fallback")
    parser.add_argument("--reference-profile", choices=["opus", "frontier-ultra"], default="frontier-ultra",
                        help="reference judge; frontier-ultra uses a quota-selected Codex frontier model")
    parser.add_argument("--reference-results", nargs=3, help="three distinct retained frontier run files for agreement")
    parser.add_argument("--continue-after-invalid-label", action="append", default=[], metavar="CASE_ID",
                        help="after inspecting a rejected reference label, continue other cases; this exact case remains missing")
    parser.add_argument("--cap-usd", type=float, default=50)
    parser.add_argument("--split", choices=["dev", "test"], default="test")


def bound_json(path, sha):
    obj = json.loads(Path(path).expanduser().read_text())
    if not isinstance(obj, dict) or obj.get("dataset_sha") != sha:
        raise DataBoundaryError("artifact belongs to a different frozen dataset")
    return obj


def command(args):
    try:
        if args.mode != "jev" and args.jev_gateway != "typesafe":
            raise DataBoundaryError("--jev-gateway applies only to jev mode; comparisons use retained provenance")
        if args.continue_after_invalid_label and args.mode != 'reference':
            raise DataBoundaryError('--continue-after-invalid-label applies only to reference mode')
        output = private_storage_path(Path(args.output))
        if args.mode == "prepare":
            from ..config import load_config
            result = prepare(load_config(args.config), output, count=args.count,
                             sessions=args.sessions, seed=args.seed, chunk_lines=args.chunk_lines)
            print(json.dumps({"dataset_sha": result["sha256"], "cases": result["case_count"],
                              "sessions": result["session_count"], "output": str(output)}))
            return 0
        if not args.dataset:
            raise DataBoundaryError("eval-filter requires --dataset; no synthetic fallback")
        root = Path(args.dataset).expanduser().resolve()
        data, manifest = load(root)
        sha = manifest["sha256"]
        if args.mode in {"reference", "jev"}:
            invalid = []
            reference_settings = {}
            if args.mode == "reference":
                from ..config import load_config
                reference_settings["cfg"] = load_config(args.config)
                reference_settings["reference_profile"] = args.reference_profile
                reference_settings["continue_after_invalid_label"] = args.continue_after_invalid_label
            result = run(data, manifest, output, provider=args.mode, limit=args.limit,
                         order_seed=args.order_seed, cap=args.cap_usd, jev_gateway=args.jev_gateway, **reference_settings)
            if args.mode == "reference":
                rows, invalid = [], []
                for anchor, item in result["records"].items():
                    if item["status"] != "ok":
                        continue
                    row = {**item["label"], "id": anchor, "provenance": "reference",
                           "reference_model": item["served_model"], "reference_account": item.get("account"),
                           "reference_transport": item.get("transport", "legacy-api"),
                           "reference_effort": item.get("served_effort"),
                           "raw_response": item.get("raw_response"), "response_sha": item.get("response_sha")}
                    try:
                        validate_labels([row], data)
                    except (DataBoundaryError, TypeError, KeyError):
                        invalid.append(anchor)
                        continue
                    rows.append(row)
                label_name = (f"reference-frontier-ultra-labels-order-{args.order_seed}.json"
                              if args.reference_profile == "frontier-ultra" else "reference-labels.json")
                atomic_json(output / label_name, {"dataset_sha": sha, "rows": rows, "invalid_label_ids": invalid})
            print(json.dumps({"completed": sum(v["status"] == "ok" for v in result["records"].values()),
                              "attempted_items": len(result["records"]),
                              "invalid_reference_labels": len(invalid),
                              "charged_or_reserved_usd": result["charged_or_reserved_usd"], "output": str(output),
                              "result_file": result.get("result_file")}))
            return 0 if not invalid and all(v["status"] == "ok" for v in result["records"].values()) else 2
        arms = {name: json.loads((root / f"{name}.json").read_text()) for name in ("current", "regex-context")}
        if args.mode == "agreement":
            from .filter_frontier import agreement, verified_labels
            if not args.reference_results:
                raise DataBoundaryError("agreement requires --reference-results with three independent runs")
            paths = [Path(p).expanduser().resolve() for p in args.reference_results]
            artifacts = [bound_json(p, sha) for p in paths]
            if (len(set(paths)) != 3 or len({a.get('order_seed') for a in artifacts}) != 3 or
                    any(a.get('reference_settings') != artifacts[0].get('reference_settings') for a in artifacts)):
                raise DataBoundaryError("agreement requires distinct runs of the same reference profile")
            labels = [verified_labels(p,data,sha) for p in paths]
            decisions = [verified_labels(p,data,sha,category_only=True) for p in paths]
            expected = [c['id'] for c in pilot_cases(data,args.limit)]
            result = {"dataset_sha": sha, "reference_settings": artifacts[0]['reference_settings'],
                      "runs": [str(p) for p in paths],
                      **agreement(decisions,expected),
                      "complete_schema_agreement": agreement(labels,expected),
                      "per_run_schema_invalid_ids": [sorted(a for a,r in rows.items() if not r['full_schema_valid'])
                                                     for rows in decisions]}
        elif args.mode == "baseline":
            result = {"dataset_sha": sha, "label_status": "not_scored", "arms": arms,
                      "sampled_cases": len(data["cases"]), "sampling": data["sampling"]}
        else:
            if not args.labels:
                raise DataBoundaryError("review/compare requires --labels bound to this dataset")
            label_file = bound_json(args.labels, sha)
            labels = validate_labels(label_file["rows"], data)
            jev_provenance = None
            if args.predictions:
                predictions = verified_predictions(Path(args.predictions).expanduser(), data, sha)
                arms["jev"] = replay(data, "jev", predictions)
                successes = [v for v in predictions.values() if v["status"] == "ok" and v.get("served_model") != "structural-only" and "gateway" in v]
                jev_provenance = {"gateways": sorted({v["gateway"] for v in successes}),
                                  "served_models": sorted({v["served_model"] for v in successes}),
                                  "identity_scopes": sorted({v["identity_scope"] for v in successes}),
                                  "note": ("Zen free identifies a gateway alias, not a verified backend patch version."
                                           if any(v["gateway"] == "zen-free" for v in successes)
                                           else "Direct TypeSafe responses identify the pinned backend version.")}
            if args.mode == "review":
                selection = review_selection(data, labels, arms)
                result = {"dataset_sha": sha, "instructions": "Review the target anchor, not just its session. Keep IDs; change provenance to human only after inspecting the evidence. Merge reviewed rows into the labels file, then compare. Episode IDs should identify a shared earliest evidence anchor.",
                          "rows": [{"id": c["id"], "review_reasons": selection[c["id"]],
                                    "draft": labels.get(c["id"]), "context": case_context(data, c)}
                                   for c in data["cases"] if c["id"] in selection]}
            else:
                result = {"dataset_sha": sha, **compare(data, labels, arms, split=args.split)}
            if jev_provenance is not None:
                result["jev_provenance"] = jev_provenance
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if output.exists():
            raise DataBoundaryError("report exists; choose a new output to preserve prior evidence")
        atomic_json(output, result)
        print(json.dumps({"output": str(output), "dataset_sha": sha,
                          "label_status": result.get("label_status", "review_required"),
                          "paired_cases": result.get("paired_cases")}))
        return 0
    except (DataBoundaryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Filter evaluation refused: {exc}", file=sys.stderr)
        return 2
