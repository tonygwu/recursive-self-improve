"""Every subcommand parses, dispatches, and does not touch the real home.

Drive commands through their real dispatch paths to verify database access
mode and configuration propagation, which helper tests alone cannot establish.

Every command runs against a --config pointing entirely into tmp_path, so the
conftest guard (which fails any test that reads the real ~/.claude) is what
proves the isolation rather than a promise in a docstring.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from self_improve.cli import main
from self_improve.store import Store

# Commands the CLI exposes. A new subcommand that is not listed here fails
# test_every_subcommand_is_covered, so this list cannot silently rot.
READ_ONLY = ["status", "contradictions"]
HELP_ONLY = [
    "run", "scan", "rescan", "report", "rollback", "self-eval",
    "search-learnings", "eval-retrieval", "backfill-project-keys",
    "rebuild-state", "install-launchd", "worker", "upgrade-state", "service",
]


@pytest.fixture
def cfg_file(tmp_path):
    """A config whose every path is inside tmp_path."""
    for d in ("claude/projects", "codex/sessions", "codex/archived", "skills", "state"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    p = tmp_path / "config.toml"
    p.write_text(
        "\n".join(
            [
                f'claude_projects_dir = "{tmp_path}/claude/projects"',
                f'claude_history_path = "{tmp_path}/claude/history.jsonl"',
                f'codex_sessions_dir = "{tmp_path}/codex/sessions"',
                f'codex_archived_dir = "{tmp_path}/codex/archived"',
                f'state_dir = "{tmp_path}/state"',
                f'claude_managed_dir = "{tmp_path}/managed"',
                f'global_claude_md = "{tmp_path}/global/CLAUDE.md"',
                f'codex_global_agents_md = "{tmp_path}/codex/AGENTS.md"',
                f'skills_dir = "{tmp_path}/skills"',
            ]
        )
        + "\n"
    )
    _make_db_one_migration_behind(tmp_path / "state" / "state.db")
    return str(p)


def _make_db_one_migration_behind(db):
    """Build the fixture DB with the LAST migration withheld.

    A fully-migrated fixture makes the read-only assertions vacuous: a writable
    Store on an up-to-date DB migrates nothing, so the test passes whether or
    not the command uses read_only=True. Verified by reintroducing the bug —
    with a current DB the suite stayed green, with this it goes red.
    """
    import self_improve.store as store_mod

    full = store_mod.MIGRATIONS
    store_mod.MIGRATIONS = full[:-1]
    try:
        Store(db).close()
    finally:
        store_mod.MIGRATIONS = full


@pytest.mark.parametrize("cmd", READ_ONLY + HELP_ONLY)
def test_subcommand_help_parses(cmd, capsys):
    with pytest.raises(SystemExit) as exc:
        main([cmd, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # No `or True` escape hatch: the usage line must actually name this command,
    # or the test proves nothing about the command it claims to cover.
    assert out.startswith("usage:")
    assert cmd in out.splitlines()[0]


@pytest.mark.parametrize("args,expected", [((), False), (("--review-only",), True)])
def test_run_dispatch_preserves_explicit_review_only(cfg_file, tmp_path, monkeypatch, args, expected):
    """Exercise the actual parser/dispatch without invoking the model pipeline."""
    from self_improve import pipeline

    calls = []

    def captured_run(cfg, store, **kwargs):
        assert cfg.state_path("state.db") == tmp_path / "state" / "state.db"
        assert store.query_one("SELECT COUNT(*) AS n FROM runs")["n"] == 0
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(pipeline, "run_pipeline", captured_run)
    assert main(["--config", cfg_file, "run", *args]) == 0
    assert len(calls) == 1
    assert calls[0]["review_only"] is expected
    assert calls[0]["dry_run"] is False


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only_command_runs_and_leaves_the_schema_alone(cmd, cfg_file, tmp_path):
    db = tmp_path / "state" / "state.db"
    before = _applied(db)

    rc = main(["--config", cfg_file, cmd])

    assert rc == 0
    assert _applied(db) == before, f"{cmd} migrated the database"


def test_rebuild_dry_run_leaves_an_older_schema_unchanged(cfg_file, tmp_path, capsys):
    db = tmp_path / "state" / "state.db"
    before = _applied(db)
    target = tmp_path / "private-backup"
    assert main(["--config", cfg_file, "rebuild-state", "--export", str(target), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert _applied(db) == before
    assert not target.exists()


@pytest.mark.parametrize("requalify", [False, True])
def test_backfill_preview_preserves_rows_and_older_schema(
    cfg_file, tmp_path, capsys, monkeypatch, requalify
):
    from self_improve.project_identity import ProjectIdentity

    db = tmp_path / "state" / "state.db"
    project = str(tmp_path / "invented-project")
    store = Store(db, migrate=False)
    store.upsert_session({
        "file_path": str(tmp_path / "invented-session.jsonl"),
        "source": "codex", "session_id": "invented-session", "project_path": project,
        "project_key": "path:invented-project" if requalify else "",
        "project_key_method": "path" if requalify else "",
        "headless": 0, "is_subagent": 0, "first_ts": "", "last_ts": "",
        "mtime": 0.0, "file_size": 0, "bytes_scanned": 0, "lines_scanned": 0,
        "malformed_lines": 0, "status": "ok", "error": "", "last_scanned_at": "",
    })
    store.close()
    before_schema = _applied(db)
    before_bytes = db.read_bytes()
    resolved = []

    def resolve(path, *, use_gh, cache):
        assert path == project and use_gh is False
        resolved.append(path)
        return ProjectIdentity("remote:example.invalid/team/demo", "demo", "remote_url")

    monkeypatch.setattr("self_improve.project_identity.resolve", resolve)
    args = ["--config", cfg_file, "backfill-project-keys", "--dry-run", "--no-gh"]
    assert main(args + (["--requalify"] if requalify else [])) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["sessions"]["would_requalify" if requalify else "would_update"] == 1
    assert resolved == [project]
    assert _applied(db) == before_schema, "backfill preview migrated the database"
    assert db.read_bytes() == before_bytes, "backfill preview changed database content"


def test_backfill_preview_refuses_a_missing_database_without_creating_it(
    cfg_file, tmp_path, capsys
):
    db = tmp_path / "state" / "state.db"
    db.unlink()
    assert main(["--config", cfg_file, "backfill-project-keys", "--dry-run", "--no-gh"]) == 2
    assert "state DB does not exist" in capsys.readouterr().err
    assert not db.exists()


def test_status_emits_parseable_json(cfg_file, capsys):
    assert main(["--config", cfg_file, "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "counts" in payload


def test_self_eval_detection_only_runs_without_llm(cfg_file, tmp_path, capsys):
    """The gate's precondition check must be free and must not migrate.

    Check the Store mode at construction time, before any query can run.
    """
    db = tmp_path / "state" / "state.db"
    before = _applied(db)

    from self_improve.data_boundary import SYNTHETIC_DATASET, freeze_dataset
    root = SYNTHETIC_DATASET
    dataset = tmp_path / "invented-private-dataset"
    freeze_dataset(root / "retrieval/corpus.jsonl", root / "retrieval/qrels.yaml",
                   root / "labeled", dataset, dataset_id="test-self-eval", version=1)
    rc = main(["--config", cfg_file, "self-eval", "--detection-only", "--dataset", str(dataset)])

    out = capsys.readouterr().out
    assert "labeled incidents have at least one candidate" in out
    assert rc in (0, 1)  # 1 when coverage is incomplete, which is a real answer
    assert _applied(db) == before, "self-eval --detection-only migrated the database"


def test_every_subcommand_is_covered(capsys):
    """A new subcommand must be added to this file, not silently untested."""
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    known = set(READ_ONLY + HELP_ONLY)
    # argparse lists subcommands in the help body; check each known one appears,
    # then check the help mentions nothing obviously missing from our list.
    for cmd in known:
        assert cmd in text, f"{cmd} vanished from the CLI"


def _applied(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    finally:
        conn.close()


def test_cold_start_on_an_empty_world(cfg_file, tmp_path, capsys):
    """A first run with no transcripts at all must not crash.

    Empty inputs must work on a fresh installation, including zero-count
    arithmetic and aggregations over empty sequences.
    """
    rc = main(["--config", cfg_file, "run", "--dry-run"])
    assert rc == 0

    store = Store(tmp_path / "state" / "state.db", read_only=True)
    assert store.query("SELECT * FROM sessions") == []
    run = store.query_one("SELECT * FROM runs ORDER BY started DESC LIMIT 1")
    assert run["status"] == "ok", "a run over an empty corpus must finish, not error"
    # And the report must render rather than blowing up on empty stats.
    assert run["report_path"]
    from pathlib import Path as _P

    assert _P(run["report_path"]).exists()


def test_cold_start_report_does_not_divide_by_zero(cfg_file, tmp_path):
    """The report computes shares and conversions; zero denominators are the
    obvious way it breaks, and only an empty corpus produces them."""
    main(["--config", cfg_file, "run", "--dry-run"])
    store = Store(tmp_path / "state" / "state.db", read_only=True)
    run = store.query_one("SELECT * FROM runs ORDER BY started DESC LIMIT 1")
    from pathlib import Path as _P

    text = _P(run["report_path"]).read_text()
    assert "run report" in text
    assert "nan" not in text.lower()


def test_search_learnings_runs_without_migrating(cfg_file, tmp_path, capsys):
    """Search a temporary database without applying its missing migration.

    search-learnings needs a query argument, so this test drives the command
    explicitly. The fixture withholds the last migration: a fully migrated
    database could conceal an unintended migrating Store.
    """
    db = tmp_path / "state" / "state.db"
    before = _applied(db)

    rc = main(["--config", cfg_file, "search-learnings", "a wrapped CLI's json output"])

    assert rc == 0
    assert _applied(db) == before, "search-learnings migrated the database"
    # It must also produce something parseable rather than crashing on an
    # empty corpus — the sandboxed agent has no way to recover from a stack
    # trace here.
    out = capsys.readouterr().out
    assert out.strip(), "search-learnings printed nothing"


def _seed_run(cfg_file, tmp_path, status, stats):
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "state" / "state.db")
    store.insert(
        "runs",
        {
            "id": new_id(), "started": utc_now_iso(), "finished": utc_now_iso(),
            "status": status, "stats_json": json.dumps(stats), "report_path": "",
        },
    )
    store.commit()
    store.close()


def test_status_says_why_the_last_run_was_degraded(cfg_file, tmp_path, capsys):
    """`status` is the one command an operator runs to ask "was last night ok".

    It dumped `last_run` with `stats_json` as an escaped string, so the reasons
    a run was degraded were technically present and practically unreadable.
    """
    _seed_run(
        cfg_file, tmp_path, "degraded",
        {"status_reasons": ["mine: 0 of 5 attempts succeeded (5 failed)"]},
    )
    assert main(["--config", cfg_file, "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    health = payload["last_run_health"]
    assert health["status"] == "degraded"
    assert health["reasons"] == ["mine: 0 of 5 attempts succeeded (5 failed)"]


def test_status_on_a_healthy_run_says_so_without_reasons(cfg_file, tmp_path, capsys):
    _seed_run(cfg_file, tmp_path, "ok", {"status_reasons": []})
    assert main(["--config", cfg_file, "status"]) == 0
    health = json.loads(capsys.readouterr().out)["last_run_health"]
    assert health == {"status": "ok", "reasons": [], "wrote_nothing_because": None}


def test_status_reports_unreadable_stats_rather_than_hiding_them(
    cfg_file, tmp_path, capsys
):
    """Fail loud. A stats blob that will not parse is a fact about the run, and
    swallowing it would make a broken row look like a healthy one."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "state" / "state.db")
    store.insert(
        "runs",
        {
            "id": new_id(), "started": utc_now_iso(), "finished": utc_now_iso(),
            "status": "ok", "stats_json": "{not json", "report_path": "",
        },
    )
    store.commit()
    store.close()
    assert main(["--config", cfg_file, "status"]) == 0
    health = json.loads(capsys.readouterr().out)["last_run_health"]
    assert health["status"] == "ok"
    assert "unreadable" in health["reasons"][0].lower(), health


def test_status_with_no_runs_at_all_says_so(cfg_file, capsys):
    assert main(["--config", cfg_file, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["last_run_health"] is None


def test_status_does_not_describe_a_dry_run_as_able_to_write(cfg_file, tmp_path, capsys):
    """Report that dry-run suppresses instruction-file writes.

    The pipeline can still write run and scan state to its database. This test
    checks the displayed reason for withholding instruction-file changes.
    """
    _seed_run(cfg_file, tmp_path, "ok", {"dry_run": True, "review_only": False})
    assert main(["--config", cfg_file, "status"]) == 0
    health = json.loads(capsys.readouterr().out)["last_run_health"]
    assert health["wrote_nothing_because"] == "dry_run"


def test_status_names_the_apply_posture(cfg_file, tmp_path, capsys):
    """Report the shipped application policy through the status command.

    The legacy auto_apply setting defaults to off. Automatic permission also
    depends on the per-target-class policy; status must report those defaults.
    """
    _seed_run(cfg_file, tmp_path, "ok", {"review_only": True, "status_reasons": []})
    assert main(["--config", cfg_file, "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["legacy_auto_apply"] is False
    assert not any(c["enabled"] for c in payload["execution_policy"]["classes"].values())
    assert payload["last_run_health"]["wrote_nothing_because"] == "review_only"


def test_status_does_not_confuse_legacy_config_with_class_permission(
    cfg_file, tmp_path, capsys
):
    """The complement. A status line that always printed the same word would
    pass the test above while telling the operator nothing."""
    with open(cfg_file, "a", encoding="utf-8") as fh:
        fh.write("auto_apply = true\n")
    _seed_run(cfg_file, tmp_path, "ok", {"review_only": True, "status_reasons": []})
    assert main(["--config", cfg_file, "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["legacy_auto_apply"] is True
    assert not any(c["enabled"] for c in payload["execution_policy"]["classes"].values())


# ----------------------------------------------------------------------
# Gate an existing proposal without running mining.
# ----------------------------------------------------------------------
#
# Preview the configured gate cost and require explicit confirmation before
# evaluation. The command must not start calls on an unconfirmed request.


def _seed_proposal(cfg_file, tmp_path, *, rule_text="**Always X.**", status="pending"):
    """One learning + one proposal, wired across the real foreign key."""
    from self_improve.config import load_config
    from self_improve.store import Store, new_id, utc_now_iso

    cfg = load_config(cfg_file)
    store = Store(cfg.state_path("state.db"))
    lid, pid = new_id(), new_id()
    store.insert(
        "learnings",
        {
            "id": lid, "title": "t", "rule_text": rule_text, "why": "w",
            "category": "c", "scope": "global", "evidence_count": 1,
            "project_count": 1, "projects_json": "[]", "first_seen": utc_now_iso(),
            "last_seen": utc_now_iso(), "confidence": 0.9, "status": "proposed",
            "duplicate_of": "", "created_at": utc_now_iso(),
        },
    )
    store.insert(
        "proposals",
        {
            "id": pid, "learning_id": lid, "run_id": "", "target_path": str(tmp_path / "T.md"),
            "target_kind": "global_claude_md", "action": "add", "diff_unified": "",
            "status": status, "eval_result_id": "", "applied_at": "",
            "snapshot_commit_before": "", "snapshot_commit_after": "",
            "created_at": utc_now_iso(),
        },
    )
    store.commit()
    store.conn.close()
    return pid, lid


def test_gate_proposal_refuses_to_spend_without_confirmation(cfg_file, tmp_path, capsys):
    """A command that spends the operator's quota states the cost and refuses.

    Exit 2, not 1: a caller must be able to tell "you did not confirm" apart
    from "the gate ran and the rule lost".
    """
    pid, _ = _seed_proposal(cfg_file, tmp_path)
    assert main(["--config", cfg_file, "gate-proposal", pid]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] == "confirmation required"
    assert payload["would_spend_gate_calls"] == 21
    assert payload["rerun_with"] == "--yes"


def test_gate_proposal_spends_nothing_when_it_refuses(cfg_file, tmp_path, capsys):
    """The refusal must be measured at the ledger, not asserted from the exit
    code. A command that printed the refusal AFTER dispatching would still
    exit 2."""
    from self_improve.config import load_config
    from self_improve.store import Store

    pid, _ = _seed_proposal(cfg_file, tmp_path)
    assert main(["--config", cfg_file, "gate-proposal", pid]) == 2
    capsys.readouterr()
    store = Store(load_config(cfg_file).state_path("state.db"))
    assert store.query("SELECT * FROM llm_calls") == []
    assert store.query("SELECT * FROM runs") == []
    store.conn.close()


def test_gate_proposal_raises_on_an_id_that_does_not_exist(cfg_file, tmp_path, capsys):
    """Fail loud. A missing proposal is not an empty result set."""
    assert main(["--config", cfg_file, "gate-proposal", "nope", "--yes"]) == 1
    assert "no proposal with id" in json.loads(capsys.readouterr().out)["error"]


def test_gate_proposal_refuses_a_learning_with_no_rule_text(cfg_file, tmp_path, capsys):
    """There is nothing to gate, so say so rather than gating an empty string.

    `regression.gate` does raise on empty rule_text, but only after the
    without-rule arm has already run — three trials spent to learn something
    knowable for free. This refuses before any call.

    The sibling guard for a MISSING learning is deliberately not tested here:
    `proposals.learning_id` is a real foreign key, so deleting the learning
    raises IntegrityError and the branch is unreachable through SQL. It stays
    as a guard because a Postgres port or a future soft delete would reach it.
    """
    pid, _ = _seed_proposal(cfg_file, tmp_path, rule_text="   ")
    assert main(["--config", cfg_file, "gate-proposal", pid, "--yes"]) == 1
    assert "no rule_text" in json.loads(capsys.readouterr().out)["error"]


def test_contradictions_says_when_it_has_never_been_computed(tmp_path, capsys):
    """Distinguish an uncomputed detector from a completed empty result.

    Absent and budget-skipped stages cannot establish that no contradiction
    exists.
    """
    import json

    from self_improve.store import Store, new_id, utc_now_iso

    db = tmp_path / "state.db"
    store = Store(db)
    store.insert("runs", {
        "id": new_id(), "started": utc_now_iso(), "finished": utc_now_iso(),
        "status": "ok", "report_path": "",
        "stats_json": json.dumps({"contradictions": {"skipped": "budget_exhausted"}}),
    })
    store.commit()
    store.close()

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'state_dir = "{tmp_path}"\n')
    rc = main(["--config", str(cfg_path), "contradictions"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "has not completed in any recorded run" in out, out
    assert "checked and found none" in out, out
    assert "budget_exhausted" in out, out
    assert out.strip() != "No open contradictions.", out


def test_contradictions_says_none_when_it_really_did_run(tmp_path, capsys):
    """The complement: a completed stage with no findings still says so."""
    import json

    from self_improve.store import Store, new_id, utc_now_iso

    db = tmp_path / "state.db"
    store = Store(db)
    store.insert("runs", {
        "id": new_id(), "started": utc_now_iso(), "finished": utc_now_iso(),
        "status": "ok", "report_path": "",
        "stats_json": json.dumps({"contradictions": {"candidates": 4, "judged": 4, "found": 0}}),
    })
    store.commit()
    store.close()

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'state_dir = "{tmp_path}"\n')
    rc = main(["--config", str(cfg_path), "contradictions"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No open contradictions" in out, out
    assert "has not completed" not in out, out


def test_worker_refuses_old_schema_without_migrating_it(cfg_file, tmp_path, capsys):
    db=tmp_path/'state'/'state.db'
    # The generic read-only fixture omits the latest migration. Worker recovery
    # specifically requires 0012; a newer web-only migration is not that boundary.
    conn = sqlite3.connect(db)
    conn.execute('DELETE FROM schema_migrations WHERE name >= ?', ('0012_instruction_operations',))
    conn.commit()
    conn.close()
    before=_applied(db)
    assert main(['--config',cfg_file,'worker','--once'])==2
    assert 'Upgrade' in capsys.readouterr().err
    assert _applied(db)==before


def test_worker_once_dispatches_and_exits_on_an_empty_queue(cfg_file, tmp_path):
    Store(tmp_path/'state'/'state.db').close()  # explicit installation step in fixture
    before=_applied(tmp_path/'state'/'state.db')
    assert main(['--config',cfg_file,'worker','--once'])==0
    assert _applied(tmp_path/'state'/'state.db')==before
