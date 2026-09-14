"""Run the real dashboard with invented data in a temporary directory.

Usage: uv run python examples/dashboard_demo.py
This demo never reads session history or invokes a model. Decisions affect
only its disposable database. Stop with Ctrl-C; each launch starts fresh.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from self_improve.config import Config
from self_improve.dashboard.app import STATIC_DIR, create_app
from self_improve.store import Store

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
RULES = (
    ("Verify the served model", "Check response telemetry before comparing model outputs.",
     "A successful request does not establish which model answered.", "evaluation"),
    ("Keep the error message", "Read stderr before retrying a failed command.",
     "An unchanged retry hides the cause and spends another attempt.", "tooling"),
    ("Measure performance changes", "Record a baseline and rerun the same workload after a performance change.",
     "A smaller diff does not establish a faster program.", "verification"),
    ("Make missing results visible", "Report attempted, succeeded, and failed counts together.",
     "A failed evaluation must not look like an empty queue.", "reporting"),
)


def seed(root: Path) -> Config:
    """Create a self-contained synthetic workspace and dashboard database."""
    cfg = Config(
        state_dir=str(root / "state"),
        claude_projects_dir=str(root / "empty-claude"),
        claude_history_path=str(root / "empty-history.jsonl"),
        codex_sessions_dir=str(root / "empty-codex"),
        codex_archived_dir=str(root / "empty-archive"),
        global_claude_md=str(root / "instructions" / "CLAUDE.md"),
        codex_global_agents_md=str(root / "instructions" / "AGENTS.md"),
        skills_dir=str(root / "skills"),
        production_repo_path=str(root / "unused-production"),
        auto_apply=False,
    )
    store = Store(Path(cfg.state_dir) / "state.db")
    try:
        for day in (9, 10, 11):
            rid = f"demo-run-{day}"
            stamp = f"2026-09-{day:02d}T08:00:00Z"
            stats = {
                "run_id": rid, "review_only": True,
                "scan": {"files_attempted": 12, "files_succeeded": 12, "files_failed": 0},
                "mine": {"attempted": 4, "succeeded": 4, "failed": 0, "taxonomy": {}},
                "cluster": {"candidates": 4, "mode": "agentic_passthrough"},
                "gate": {"attempted": 4, "gated_pass": 0, "gated_fail": 0,
                         "ungated": 4, "inconclusive": 0, "failed": 0},
                "apply": {"attempted": 4, "applied": 0, "held": 4, "failed": 0, "taxonomy": {}},
            }
            store.insert("runs", {"id": rid, "started": stamp,
                         "finished": f"2026-09-{day:02d}T08:12:00Z", "status": "ok",
                         "stats_json": json.dumps(stats), "report_path": ""})
        for i, (title, rule, why, category) in enumerate(RULES):
            project = root / "projects" / ("example-api" if i % 2 else "example-evals")
            project.mkdir(parents=True, exist_ok=True)
            target = project / "AGENTS.md"
            target.write_text("# Example project\n\nRun tests before making a release.\n")
            sid, lid, iid, pid = (f"demo-{kind}-{i+1}" for kind in ("session", "rule", "incident", "proposal"))
            key = f"demo:{project.name}"
            stamp = "2026-09-11T08:00:00Z"
            store.upsert_session({"file_path": sid, "source": "claude" if i % 2 else "codex",
                "session_id": sid, "project_path": str(project), "project_key": key,
                "project_display": project.name, "project_key_method": "synthetic_demo",
                "headless": 0, "is_subagent": 0, "first_ts": stamp, "last_ts": stamp,
                "mtime": 0.0, "file_size": 0, "bytes_scanned": 0, "lines_scanned": 120,
                "malformed_lines": 0, "status": "ok", "error": "", "last_scanned_at": stamp})
            store.insert("incidents", {"id": iid, "session_file": sid, "session_id": sid,
                "project_path": str(project), "project_key": key, "ts": stamp,
                "signal_type": "correction", "matched_text": rule,
                "window_json": json.dumps([{"role": "user", "ts": stamp, "text": rule}]),
                "score": 1.0, "status": "mined", "run_id": "demo-run-11", "created_at": stamp})
            store.insert("learnings", {"id": lid, "title": title, "rule_text": rule, "why": why,
                "category": category, "scope": "project", "evidence_count": 1, "project_count": 1,
                "projects_json": json.dumps([str(project)]), "first_seen": stamp, "last_seen": stamp,
                "confidence": 0.8, "status": "proposed", "created_at": stamp,
                "source": "claude" if i % 2 else "codex", "primary_project_path": str(project)})
            store.insert("incident_learnings", {"incident_id": iid, "learning_id": lid})
            store.insert("proposals", {"id": pid, "learning_id": lid, "run_id": "demo-run-11",
                "target_path": str(target), "target_kind": "project_agents_md", "action": "add",
                "diff_unified": f"--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1,2 @@\n # Example project\n+{rule}\n",
                "status": "held", "created_at": stamp})
    finally:
        store.close()
    return cfg


def main() -> None:
    import uvicorn

    demo_parent = "/tmp" if Path("/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="si-demo-", dir=demo_parent) as folder:
        root = Path(folder)
        cfg = seed(root)
        static = root / "static"
        shutil.copytree(STATIC_DIR, static)
        index = static / "index.html"
        index.write_text(index.read_text().replace(
            "127.0.0.1 &middot; no auth", "Synthetic demo &middot; no model calls"))
        print("Synthetic demo: http://127.0.0.1:8876 — no real sessions or model calls.", flush=True)
        uvicorn.run(create_app(cfg, clock=lambda: NOW, static_dir=static), host="127.0.0.1", port=8876)


if __name__ == "__main__":
    main()
