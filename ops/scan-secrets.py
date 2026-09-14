#!/usr/bin/env python3
"""Scan tracked worktree bytes and optionally reachable Git history privately.

Run with the source environment: uv run python ops/scan-secrets.py --help.
This is an audit, not a claim that transcript-derived content is public.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess

from self_improve.data_boundary import DataBoundaryError, backup_checkout, private_destination

SCANNER_VERSION = "8.30.1"
ROOT = Path(__file__).resolve().parents[1]


def scan(scanner: str, output: Path, *, history: bool = False) -> int:
    output = private_destination(output)
    version = subprocess.run([scanner, "version"], capture_output=True, text=True, check=True).stdout.strip()
    if version != SCANNER_VERSION:
        raise ValueError(f"expected Gitleaks {SCANNER_VERSION}, got {version!r}")
    # A new report directory retains the source bytes and evidence for this run.
    # Never overwrite an earlier audit or export its excerpts into a checkout.
    output.mkdir(parents=True, mode=0o700)
    old_mask = os.umask(0o077)
    try:
        snapshot = backup_checkout(ROOT, output / "worktree")
        config = output / "worktree" / "snapshot" / ".gitleaks.toml"
        (output / "scanner-config.toml").write_bytes(config.read_bytes())
        ignore = output / "empty-ignore"
        ignore.write_text("")
        common = ["--config", str(output / "scanner-config.toml"),
                  "--gitleaks-ignore-path", str(ignore), "--ignore-gitleaks-allow",
                  "--redact=100", "--max-archive-depth=3", "--max-decode-depth=5",
                  "--no-banner", "--no-color", "--report-format=json", "--timeout=300"]
        modes = [("current", ["dir", str(output / "worktree" / "snapshot")])]
        if history:
            modes.append(("history", ["git", "--log-opts=--all --full-history -m", str(ROOT)]))
        refs_before = subprocess.check_output(
            ["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=ROOT, text=True)
        summaries = []
        for name, args in modes:
            report = output / f"{name}.json"
            result = subprocess.run([scanner, *args, *common, "--report-path", str(report)],
                                    capture_output=True, text=True)
            (output / f"{name}.log").write_text(result.stdout + result.stderr)
            if result.returncode not in (0, 1) or not report.is_file():
                raise ValueError(f"{name} scan failed (exit {result.returncode}); inspect its private log")
            findings = json.loads(report.read_text())
            if not isinstance(findings, list) or bool(findings) != (result.returncode == 1):
                raise ValueError(f"{name} scan report and exit status disagree")
            if any(not isinstance(row, dict) or not isinstance(row.get("RuleID"), str) for row in findings):
                raise ValueError(f"{name} scan report has invalid finding records")
            summaries.append({"mode": name, "findings": len(findings),
                              "by_rule": dict(Counter(row["RuleID"] for row in findings))})
        refs_after = subprocess.check_output(
            ["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=ROOT, text=True)
        if history and refs_before != refs_after:
            raise ValueError("Git refs changed during the history scan; repeat in a new audit directory")
        summary = {"scanner_version": version, "revision": snapshot["revision"],
                   "worktree_manifest_sha256": snapshot["sha256"],
                   "tracked_files": len(snapshot["files"]), "scans": summaries,
                   "local_refs": refs_before.splitlines() if history else []}
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        public_summary = {key: value for key, value in summary.items() if key != "local_refs"}
        public_summary["local_ref_count"] = len(summary["local_refs"])
        print(json.dumps(public_summary))
        return int(any(row["findings"] for row in summaries))
    finally:
        os.umask(old_mask)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new private directory outside every Git checkout")
    parser.add_argument("--scanner", default="gitleaks", help=f"Gitleaks {SCANNER_VERSION} executable")
    parser.add_argument("--history", action="store_true", help="also scan changes in all locally reachable refs")
    args = parser.parse_args()
    try:
        return scan(args.scanner, args.output, history=args.history)
    except (DataBoundaryError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"secret audit failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
