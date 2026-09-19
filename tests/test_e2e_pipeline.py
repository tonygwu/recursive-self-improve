"""End-to-end scan, filtering, and mining over a constructed multi-source corpus.
The LLM and provider executables are fixtures. Real parsers, detectors,
sandbox builders, stores, and persistence exercise their shared contracts.
Resources are temporary and provider calls spend no quota.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tests.e2e_corpus import ScriptedLLM, build_corpus, mine_payload
from self_improve.miner import MINE_AGENTIC_JSON_KEYS
from self_improve.pipeline import run_pipeline


@pytest.fixture
def corpus(tmp_path):
    return build_corpus(tmp_path)


# ----------------------------------------------------------------------
# the fake must track the real contract
# ----------------------------------------------------------------------


def test_mine_payload_helper_matches_the_live_contract():
    """If the miner contract gains or drops a key, fail HERE.

    Otherwise every E2E test below keeps exercising a stale response shape and
    reports green while production would reject the payload.
    """
    assert set(mine_payload("r")) == set(MINE_AGENTIC_JSON_KEYS)


# ----------------------------------------------------------------------
# scan + filter-incidents over the whole corpus
# ----------------------------------------------------------------------


def test_scan_indexes_both_sources_and_marks_headless(corpus):
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)

    sessions = corpus.store.query("SELECT * FROM sessions")
    by_source = {}
    for s in sessions:
        by_source.setdefault(s["source"], []).append(s)
    assert set(by_source) == {"claude", "codex"}, (
        "both parsers must be live in one run; got " f"{sorted(by_source)}"
    )
    assert len(by_source["claude"]) == 5

    headless = corpus.session_row("sess-headless")
    assert headless["headless"] == 1, (
        "entrypoint sdk-cli is `claude -p`; the miner must learn from its own "
        "headless runs"
    )
    assert corpus.session_row("sess-clone-a")["headless"] == 0


def test_filter_incidents_finds_the_correction_in_every_claude_session(corpus):
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)

    corrections = corpus.store.query(
        "SELECT * FROM incidents WHERE signal_type = 'correction'"
    )
    got = {c["session_id"] for c in corrections}
    assert got >= {"sess-clone-a", "sess-clone-b", "sess-unrelated", "sess-agedout"}


def test_incident_timestamps_come_from_the_data_not_the_clock(corpus):
    """Logical time is read out of the transcript, never from mtime/now.

    The fixture files are all written *now*; their content claims August 10-14.
    An implementation that used mtime would produce today's date here.
    """
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    inc = corpus.store.query_one(
        "SELECT * FROM incidents WHERE session_id = 'sess-clone-a' "
        "AND signal_type = 'correction'"
    )
    assert inc["ts"] == "2026-08-10T01:01:00.000Z"


# ----------------------------------------------------------------------
# one repo is one project, however many working copies
# ----------------------------------------------------------------------


def test_two_clones_of_one_repo_scan_into_one_project(corpus):
    """Working copies of one repository must count as one project for routing."""
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)

    a = corpus.session_row("sess-clone-a")
    b = corpus.session_row("sess-clone-b")

    assert a["project_path"] != b["project_path"], "fixture must use two paths"
    # Non-empty first: an earlier version of this test passed on '' == '',
    # because the session upsert silently dropped the new columns. Equality
    # alone cannot tell "collapsed correctly" from "never resolved".
    assert a["project_key"], "project_key was never resolved"
    assert a["project_key"] == b["project_key"], (
        f"clones did not collapse: {a['project_key']!r} vs {b['project_key']!r}"
    )
    assert a["project_display"] == "demo-service"
    assert a["project_key_method"] == "remote_url"


def test_an_unrelated_repo_keeps_its_own_project(corpus):
    """Collapse must not pass by merging everything into one bucket."""
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    assert (
        corpus.session_row("sess-unrelated")["project_key"]
        != corpus.session_row("sess-clone-a")["project_key"]
    )


def test_incidents_carry_the_canonical_key_too(corpus):
    """Downstream grouping reads incidents, not sessions."""
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    keys = {
        i["project_key"]
        for i in corpus.incidents()
        if i["session_id"] in {"sess-clone-a", "sess-clone-b"}
    }
    assert len(keys) == 1 and keys != {""}


def test_scan_reports_how_each_project_key_was_resolved(corpus):
    """A degraded resolution must be visible, never silent.

    With gh off these resolve by remote URL, which collapses clones but would
    fracture on a repo rename — the operator needs to be able to see that from
    the run report.
    """
    stats = run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    methods = stats["scan"]["project_key_methods"]
    assert methods, "resolution method was not reported at all"
    assert sum(methods.values()) == stats["scan"]["files_attempted"]
    assert "remote_url" in methods


# ----------------------------------------------------------------------
# mine, with the sandbox the agent actually gets
# ----------------------------------------------------------------------


def _mine_run(corpus, n_payloads=8, **kw):
    llm = ScriptedLLM(
        mine_responses=[
            mine_payload(f"**Never pipe a wrapped CLI into a parser** (v{i})")
            for i in range(n_payloads)
        ]
    )
    stats = run_pipeline(
        corpus.cfg,
        corpus.store,
        review_only=True,
        _llm_factory=llm.factory(),
        **kw,
    )
    return llm, stats


def test_mine_builds_a_real_sandbox_the_agent_can_explore(corpus):
    llm, stats = _mine_run(corpus)

    assert llm.sandboxes, "the mine stage never ran"
    listing = llm.sandbox_listings[0]
    assert "transcript.md" in listing
    assert "environment.md" in listing
    assert "learnings.jsonl" in listing, (
        "the keyword half of dedup is Grep over learnings.jsonl; without it "
        "the agent can only do embedding search"
    )
    # The sandbox holds the FULL session, not a +/-6 window: the agentic miner
    # root-causes failures that surface many turns after their cause.
    text = (llm.sandboxes[0] / "transcript.md").read_text()
    assert "npm run twin" in text and "that's wrong" in text


def test_mine_persists_learnings_and_links_them_to_their_incident(corpus):
    """Persist an incident link for every learning produced by mining.

    The link preserves the evidence needed to explain and inspect a learning."""
    llm, stats = _mine_run(corpus)

    learnings = corpus.store.query("SELECT * FROM learnings")
    assert learnings, "mine produced no learnings"
    for row in learnings:
        links = corpus.store.query(
            "SELECT * FROM incident_learnings WHERE learning_id = ?", (row["id"],)
        )
        assert links, f"learning {row['id']} has no incident link (provenance lost)"


def test_mine_records_provenance_fields_needed_by_the_rules_browser(corpus):
    llm, stats = _mine_run(corpus)
    row = corpus.store.query("SELECT * FROM learnings")[0]

    assert row["source"] in {"claude", "codex"}, "which agent produced this"
    assert row["first_seen"], "when the incident happened"
    projects = json.loads(row["projects_json"])
    assert projects and all(projects), "which project it came from"


def test_mine_reports_attempted_succeeded_failed_not_a_bare_count(corpus):
    _, stats = _mine_run(corpus)
    mine = stats["mine"]
    assert {"attempted", "succeeded", "failed", "taxonomy"} <= set(mine)
    assert mine["attempted"] == mine["succeeded"] + mine["failed"]


def test_exhausted_fake_is_a_failed_call_never_a_repeated_payload(corpus):
    """The fake runs out on purpose; the pipeline must record failures.

    A fake that silently repeats its last response would let a miscounted
    fan-out (N incidents, 1 response) read as a clean run.
    """
    _, stats = _mine_run(corpus, n_payloads=1)
    mine = stats["mine"]
    assert mine["attempted"] > 1
    assert mine["failed"] >= 1
    assert mine["succeeded"] == 1


def test_the_fake_stops_at_max_cheap_calls_like_the_real_runner(corpus):
    """The scripted provider must enforce the real runner's attempt budget.
    Failed attempts still spend budget; the fixture must stop at the configured cap.
    """
    _, stats = _mine_run(corpus, n_payloads=1, max_cheap_calls=1)
    mine = stats["mine"]

    # One call spent, then the loop stops — not one call and a free tail.
    assert mine["attempted"] == 2, mine
    assert mine["succeeded"] == 1 and mine["failed"] == 1, mine
    assert mine["attempted"] == mine["succeeded"] + mine["failed"]

    tax = mine["taxonomy"]
    assert tax.get("budget_refused_this_incident") == 1, tax
    # The untouched remainder is counted separately so the two never overlap.
    assert "budget_exhausted" in tax, tax


# ----------------------------------------------------------------------
# the aged-out fallback — the one thing the archive stage exists for
# ----------------------------------------------------------------------


def test_deleted_transcript_falls_back_to_the_archived_window(corpus):
    """Mine the archived window when the original transcript has been deleted.

    The fallback sandbox must identify the evidence as an incomplete archive."""
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)

    inc = corpus.store.query_one(
        "SELECT * FROM incidents WHERE session_id = 'sess-agedout' "
        "AND signal_type = 'correction'"
    )
    window = json.loads(inc["window_json"])
    assert window, "nothing archived, so the incident dies with the transcript"

    corpus.transcripts["sess-agedout"].unlink()

    llm = ScriptedLLM(mine_responses=[mine_payload("**Rule from an aged-out window**")])
    run_pipeline(
        corpus.cfg,
        corpus.store,
        review_only=True,
        project_filter=str(corpus.clone_a),
        _llm_factory=llm.factory(),
    )

    aged = [
        s
        for s in llm.sandboxes
        if (s / "transcript.md").exists()
        and "AGED OUT" in (s / "transcript.md").read_text().upper()
    ]
    assert aged, (
        "the fallback sandbox must be marked so the agent knows it is seeing "
        "a truncated window, not the full session"
    )


# ----------------------------------------------------------------------
# the miner's own duplicate verdict must be honoured
# ----------------------------------------------------------------------


def test_a_miner_flagged_duplicate_never_becomes_a_proposal(corpus, tmp_path):
    """Honor an explicit duplicate judgment that names an existing rule.

    The pipeline must retain the named rule, reject the duplicate learning,
    and count the drop without producing another proposal."""
    global_md = Path(corpus.cfg.global_claude_md)
    global_md.parent.mkdir(parents=True, exist_ok=True)
    existing_line = (
        "- **Read one full raw response before trusting any parser**, and assert "
        "the response telemetry names what you requested.\n"
    )
    global_md.write_text(existing_line)

    llm = ScriptedLLM(
        mine_responses=[
            mine_payload(
                "**Never pipe a wrapped CLI's stdout straight into a parser** — "
                "redirect to a file and inspect the raw output first.",
                duplicate_of_existing_rule=existing_line.strip(),
            )
        ]
        + [
            mine_payload(f"**An unrelated lesson number {i}**")
            for i in range(8)
        ]
    )
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )

    flagged = corpus.store.query(
        "SELECT * FROM learnings WHERE duplicate_of != ''"
    )
    assert flagged, "fixture did not produce a miner-flagged duplicate"
    for row in flagged:
        proposals = corpus.store.query(
            "SELECT * FROM proposals WHERE learning_id = ?", (row["id"],)
        )
        assert not proposals, (
            "the miner said this lesson is already written and named the line; "
            "a duplicate learning must not produce another proposal"
        )
        assert row["status"] == "rejected"

    assert stats["gate"]["dup_dropped"] >= 1


def test_the_drop_is_attributed_to_a_specific_rule_not_a_bare_flag(corpus):
    """A drop must always name the line it duplicated, so it is auditable."""
    global_md = Path(corpus.cfg.global_claude_md)
    global_md.parent.mkdir(parents=True, exist_ok=True)
    global_md.write_text("- **Some existing rule** — with a body.\n")

    llm = ScriptedLLM(
        mine_responses=[
            mine_payload(
                "**A restatement of that rule**",
                duplicate_of_existing_rule="- **Some existing rule** — with a body.",
            )
        ]
        + [mine_payload(f"**Unrelated {i}**") for i in range(8)]
    )
    run_pipeline(corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    row = corpus.store.query_one("SELECT * FROM learnings WHERE duplicate_of != ''")
    assert "Some existing rule" in row["duplicate_of"]


def test_a_learning_from_a_vanished_project_is_counted_not_silently_dropped(corpus):
    """Report routing failures when a learning's project directory is missing.

    Archived evidence can still be mined. A resulting routing failure must
    have a named taxonomy entry rather than disappear from the run totals."""
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    # Point every incident at a directory that does not exist.
    corpus.store.conn.execute(
        "UPDATE incidents SET project_path = ?, project_key = ?",
        ("/Users/x/Code/retired-demo", "unresolved:/Users/x/Code/retired-demo"),
    )
    corpus.store.commit()

    llm = ScriptedLLM(
        mine_responses=[
            mine_payload(f"**A rule scoped to a project that is gone** {i}",
                         scope_guess="rule_path", path_globs=["src/**/*.py"])
            for i in range(8)
        ]
    )
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )

    assert stats["mine"]["succeeded"] > 0, "mining should still work from the window"
    taxonomy = stats["apply"]["taxonomy"]
    assert any("RoutingError" in k for k in taxonomy), (
        f"an unroutable learning must be named in the taxonomy, got {taxonomy}"
    )


def test_project_filter_matches_the_renamed_repo_under_its_old_path(corpus):
    """`--project demo-service` must find sessions recorded as old-service.

    The filter was a substring match on project_path alone. After the rename,
    old sessions keep the old cwd forever, so filtering by the CURRENT repo
    name silently mined a subset — the failure mode being a quiet under-count,
    not an error.
    """
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    old_path_session = corpus.session_row("sess-clone-b")
    assert "old-service" in old_path_session["project_path"]
    assert "demo-service" in old_path_session["project_key"]

    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        corpus.cfg,
        corpus.store,
        review_only=True,
        project_filter="demo-service",
        _llm_factory=llm.factory(),
    )

    mined_sessions = {
        r["session_id"]
        for r in corpus.store.query(
            "SELECT session_id FROM incidents WHERE status != 'new'"
        )
    }
    assert "sess-clone-b" in mined_sessions, (
        "filtering by the current repo name missed the session recorded under "
        "the pre-rename path"
    )


def test_the_configured_mine_order_actually_reaches_the_queue(corpus):
    """cfg.mine_order must not be silently ignored.

    order_incidents_for_mining took cfg=None defaulting to Config(), and the
    pipeline called it without cfg — so setting mine_order in config.toml
    changed nothing, and the run report cheerfully printed the DEFAULT order as
    though it had been chosen. A silent default that also self-reports as
    correct is the worst version of this bug.
    """
    import dataclasses

    cfg = dataclasses.replace(corpus.cfg, mine_order="signal_then_recent")
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    assert stats["mine"]["queue"]["order"] == "signal_then_recent", (
        "the configured ordering never reached the queue"
    )


def test_mine_accounting_holds_when_the_budget_bites(corpus):
    """Budget refusal must preserve attempted == succeeded + failed.
    The refused incident must not increment the attempt count.
    """
    from self_improve.llm import BudgetExhausted

    class BudgetedLLM(ScriptedLLM):
        """Succeeds twice, then refuses like a real exhausted budget."""

        def call_agentic(self, *a, **kw):
            if len([c for c in self.calls if c["kind"] == "agentic"]) >= 2:
                raise BudgetExhausted("cheap", 2, 1)
            return super().call_agentic(*a, **kw)

    llm = BudgetedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    mine = stats["mine"]

    assert mine["attempted"] == mine["succeeded"] + mine["failed"], (
        f"attempted={mine['attempted']} but succeeded={mine['succeeded']} + "
        f"failed={mine['failed']}"
    )
    assert mine["taxonomy"].get("budget_exhausted"), "the refusal must still be named"
    # The refused-and-never-attempted remainder is counted separately from the
    # one that WAS attempted, so neither number double-counts the other.
    assert mine["attempted"] >= 3


def test_apply_counts_a_held_proposal_as_attempted(corpus):
    """A review-only hold is an application outcome and counts as attempted."""
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    ap = stats["apply"]

    assert ap["held"] > 0, "fixture produced no proposals to hold"
    assert ap["attempted"] == ap["applied"] + ap["held"] + ap["failed"], (
        f"attempted={ap['attempted']} but applied+held+failed="
        f"{ap['applied'] + ap['held'] + ap['failed']}"
    )


def test_gate_outcomes_account_for_every_attempt(corpus):
    """Every gate attempt must reconcile with its declared outcome buckets."""
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    g = stats["gate"]

    assert g["attempted"] > 0, "fixture produced no proposals to gate"
    accounted = _gate_accounted(g)
    assert g["attempted"] == accounted, (
        f"attempted={g['attempted']} but outcomes sum to {accounted}: {g}"
    )


def _gate_accounted(g: dict) -> int:
    """Sum the declared verdict, failure, and refusal outcomes of gate attempts.

    Budget refusals and inconclusive verdicts are separate outcomes and must
    both contribute to the attempted-work reconciliation."""
    return (
        g["gated_pass"]
        + g["gated_fail"]
        + g["ungated"]
        + g.get("inconclusive", 0)
        + g.get("failed", 0)
        + g.get("refused", 0)
    )


def test_the_gate_invariant_holds_when_the_budget_refuses_everything(corpus):
    """When the gate pool is exhausted, attempted still equals all outcomes."""
    import dataclasses

    cfg = dataclasses.replace(corpus.cfg, max_gate_calls_per_run=0)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())
    g = stats["gate"]

    assert g["attempted"] > 0, "fixture produced no proposals to gate"
    assert g.get("refused", 0) > 0, f"expected budget refusals, got {g}"
    assert g["attempted"] == _gate_accounted(g), (
        f"attempted={g['attempted']} but outcomes sum to {_gate_accounted(g)}: {g}"
    )


def test_apply_accounting_holds_in_the_auto_apply_path_too(corpus):
    """Reconcile apply outcomes when the pipeline runs outside review-only mode.

    Setting the legacy auto_apply flag does not grant target-class consent.
    Every attempted proposal must still be counted as applied, held, or failed."""
    import dataclasses

    cfg = dataclasses.replace(corpus.cfg, auto_apply=True)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(cfg, corpus.store, review_only=False, _llm_factory=llm.factory())
    ap = stats["apply"]

    assert ap["attempted"] > 0
    assert ap["attempted"] == ap["applied"] + ap["held"] + ap["failed"], ap


# ----------------------------------------------------------------------
# The WIRING, not the function
# ----------------------------------------------------------------------
#
# Twice on 2026-08-18 a fix's call site turned out to be untested, so deleting
# the wiring broke nothing. It happened a third time while building the
# majority gate: every unit test called gate_majority and generate_spec
# directly, and removing `scenario=` and `scenarios=cfg.eval_scenarios` from
# pipeline.py left all 919 tests green. These tests drive the real pipeline and
# assert on what the call site actually passed.


def _capture_gate_wiring(monkeypatch, *, verdict="ungated"):
    """Fake out generate_spec/gate, keep the real gate_majority and the real
    pipeline call site. Returns the list of captured calls."""
    from self_improve.evals import harness, regression

    calls = {"generate": [], "gate": []}

    def fake_generate_spec(learning, llm_json, prompts_dir, **kw):
        calls["generate"].append(kw)
        sid = learning["id"] if kw.get("scenario") is None else (
            f"{learning['id']}-s{kw['scenario']}"
        )
        return harness.EvalSpec(
            id=sid, title="t", scenario_prompt="p", workspace_files={"a": "b"},
            success_criteria="c", grader={"type": "code", "check": "true"},
        )

    def fake_gate(spec, rule_text, agent_runner, cfg, **kw):
        calls["gate"].append({"spec_id": spec.id, **kw})
        return {
            "verdict": verdict, "without_stats": {}, "with_stats": None,
            "eval_result_id": "",
        }

    monkeypatch.setattr(regression, "generate_spec", fake_generate_spec)
    monkeypatch.setattr(regression, "gate", fake_gate)
    monkeypatch.setattr(
        "self_improve.pipeline._ensure_sandbox", lambda *a, **kw: None
    )
    return calls


@pytest.mark.parametrize("verdict", ["ungated", "gated_pass", "gated_fail"])
def test_pipeline_asks_for_every_configured_scenario(corpus, monkeypatch, verdict):
    """Sabotage: `scenarios=1` in pipeline.py. Only one scenario runs; fails."""
    calls = _capture_gate_wiring(monkeypatch, verdict=verdict)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    assert calls["gate"], "the gate stage was never reached; fixture is wrong"
    per_proposal = len(calls["gate"]) / len({c["spec_id"].rsplit("-s", 1)[0]
                                             for c in calls["gate"]})
    assert per_proposal == corpus.cfg.eval_scenarios, (
        f"pipeline ran {per_proposal} scenarios per proposal, "
        f"cfg.eval_scenarios is {corpus.cfg.eval_scenarios}"
    )


def test_pipeline_threads_the_scenario_index_into_the_spec_id(corpus, monkeypatch):
    """Sabotage: delete `scenario=scenario` from pipeline's generate_spec call.

    Every scenario then writes <learning-id>.yaml and overwrites the last, so
    N-1 scenarios leave no evidence behind. This fails on the missing kwarg.
    """
    calls = _capture_gate_wiring(monkeypatch)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    assert calls["generate"], "generate_spec was never called"
    for kw in calls["generate"]:
        assert "scenario" in kw, (
            "pipeline called generate_spec without scenario=; all scenarios "
            "would write the same YAML file"
        )
    indexes = sorted({kw["scenario"] for kw in calls["generate"]})
    assert indexes == list(range(corpus.cfg.eval_scenarios))


def test_pipeline_gives_each_scenario_its_own_trial_directory(corpus, monkeypatch):
    """Sabotage: drop the f"scenario-{scenario}" segment from work_dir.

    run_trials builds its sandbox with exist_ok=False, so a shared directory
    makes every scenario after the first die with FileExistsError.
    """
    calls = _capture_gate_wiring(monkeypatch)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    dirs = [str(c["work_dir"]) for c in calls["gate"]]
    assert len(dirs) == len(set(dirs)), f"trial directories collide: {dirs}"


def test_pipeline_records_the_scenario_split_for_the_report(corpus, monkeypatch):
    """A verdict with no split reintroduces the problem the majority fixed."""
    _capture_gate_wiring(monkeypatch)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    splits = stats["gate"].get("scenario_splits") or []
    assert splits, "gate produced no per-scenario split for the report"
    for row in splits:
        assert set(row["tally"]) == {"gated_pass", "gated_fail", "ungated", "error"}
        assert row["scenarios_run"] == corpus.cfg.eval_scenarios


# ----------------------------------------------------------------------
# Persist run statistics before generating the report
# ----------------------------------------------------------------------
#
# report.generate reads runs.stats_json from the database. The pipeline must
# persist complete statistics before calling it. A populated funnel alone cannot
# prove this ordering because that section reads its own database queries.
# These tests inspect the report call and the resulting file.


def test_the_report_is_generated_after_the_stats_it_describes(corpus, monkeypatch):
    """Sabotage: move `report.generate` back above the runs update in
    pipeline.py. `seen` is then `{}` and this fails."""
    from self_improve import pipeline as pl
    from self_improve import report as rp

    seen: list[dict] = []
    real_generate = rp.generate

    def spy(store, cfg, run_id, out_path):
        row = store.query_one("SELECT stats_json FROM runs WHERE id = ?", (run_id,))
        seen.append(json.loads((row or {}).get("stats_json") or "{}"))
        return real_generate(store, cfg, run_id, out_path)

    monkeypatch.setattr("self_improve.report.generate", spy)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(4)])
    run_pipeline(corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    assert seen, "report.generate was never called"
    stats_at_report_time = seen[-1]
    assert stats_at_report_time, (
        "the report was generated while runs.stats_json was still empty, so "
        "every narrative section built from `stats` renders as nothing"
    )
    assert "scan" in stats_at_report_time, (
        f"stats at report time is missing the scan block: "
        f"{sorted(stats_at_report_time)}"
    )


def test_a_stats_derived_section_actually_reaches_the_report_file(corpus):
    """Through the real pipeline, on the real file it writes.

    The previous test pins the ordering; this one pins the consequence, so a
    future refactor cannot satisfy the ordering and still lose the content.
    """
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(4)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    text = Path(stats["report_path"]).read_text(encoding="utf-8")
    # The scan block always carries a skipped-type or taxonomy story on this
    # fixture; at minimum the report must show it saw a non-empty scan.
    assert "Sessions scanned" in text or "scan" in text.lower()
    assert stats["scan"]["files_attempted"] > 0


def test_a_budget_failure_in_a_trial_lands_in_the_bucket_the_report_explains(
    corpus, monkeypatch
):
    """run_trials now RE-RAISES BudgetExhausted rather than burying it as
    agent_error, which is right — but a raw one escaping the gate would fall
    into the pipeline's generic handler and be filed as `gate_BudgetExhausted`,
    while report._gate_starved_lines keys on `gate_budget_exhausted`.

    The proposal is left pending either way, so there is no false verdict. The
    cost is that the operator sees an unfamiliar key with no explanation beside
    it, which is exactly the shape this repo keeps being bitten by.

    Sabotage: remove the BudgetExhausted clause from the gate's handlers.
    """
    from self_improve.evals import regression
    from self_improve.llm import BudgetExhausted

    def broke(*a, **kw):
        raise BudgetExhausted("gate", 78, 1)

    from self_improve.evals import harness

    def fake_spec(learning, llm_json, prompts_dir, **kw):
        return harness.EvalSpec(
            id=learning["id"], title="t", scenario_prompt="p",
            workspace_files={"a": "b"}, success_criteria="c",
            grader={"type": "code", "check": "true"},
        )

    monkeypatch.setattr(regression, "generate_spec", fake_spec)
    monkeypatch.setattr(regression, "gate", broke)
    monkeypatch.setattr("self_improve.pipeline._ensure_sandbox", lambda *a, **kw: None)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    tax = stats["apply"]["taxonomy"]
    assert tax.get("gate_budget_exhausted"), (
        f"a budget failure was filed under an unexplained key: {sorted(tax)}"
    )
    assert "gate_BudgetExhausted" not in tax
    # `refused`, not `failed`: a cap is not a fault, and AGENTS.md says a run
    # with no eval verdicts must not be read as a gate that failed. The two
    # counters are separate so a starved gate cannot degrade the run status.
    assert stats["gate"]["refused"] > 0
    assert stats["gate"]["failed"] == 0


def test_the_gate_preflight_actually_refuses_when_its_pool_is_spent(corpus, monkeypatch):
    """The preflight branch was dead code in this suite.

    `ScriptedLLM.stats()` reported `calls_made` with only cheap and strong keys,
    so `made.get("gate", 0)` was 0 forever and neither the gate's preflight nor
    the A/B sweep's could ever refuse anything under test. Two safety branches
    whose whole job is refusing up front, never once exercised.

    The fake now bills by STAGE the way llm.py does. With the pool set below
    what one proposal costs, the gate must refuse BEFORE running a trial and
    file it under the key the report explains.

    Sabotage: drop the `need > left` check in pipeline.py.
    """
    import dataclasses

    from self_improve.evals import harness, regression

    ran: list[str] = []

    def fake_spec(learning, llm_json, prompts_dir, **kw):
        return harness.EvalSpec(
            id=learning["id"], title="t", scenario_prompt="p",
            workspace_files={"a": "b"}, success_criteria="c",
            grader={"type": "code", "check": "true"},
        )

    def loud_gate(*a, **kw):
        ran.append("gate")
        raise AssertionError("a trial ran even though the pool could not pay for it")

    monkeypatch.setattr(regression, "generate_spec", fake_spec)
    monkeypatch.setattr(regression, "gate", loud_gate)
    monkeypatch.setattr("self_improve.pipeline._ensure_sandbox", lambda *a, **kw: None)

    # One call short of what a single proposal needs.
    from self_improve.pipeline import gate_calls_needed

    starved = dataclasses.replace(
        corpus.cfg, max_gate_calls_per_run=gate_calls_needed(corpus.cfg) - 1
    )
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(
        starved, corpus.store, review_only=True, _llm_factory=llm.factory()
    )

    assert not ran, "the gate ran a trial it could not afford"
    assert stats["apply"]["taxonomy"].get("gate_budget_exhausted"), (
        f"the refusal was not filed where the report explains it: "
        f"{sorted(stats['apply']['taxonomy'])}"
    )


def test_the_fake_reports_the_same_shape_as_the_real_runner(corpus):
    """A contract test between the double and the original.

    Pinning the keys by hand is what let this drift in the first place: the
    fake reported `calls_made` with cheap and strong, production also has gate,
    and the two budget preflights read a key the fake never produced. So
    compare against the REAL LLMRunner rather than against a list somebody has
    to remember to update.
    """
    import tempfile
    from pathlib import Path

    from self_improve.config import Config
    from self_improve.llm import LLMRunner
    from self_improve.store import Store, new_id

    raw = Path(tempfile.mkdtemp())
    store = Store(raw / "probe.db")
    real = LLMRunner(Config(), store, new_id(), raw).stats()
    store.close()
    fake = ScriptedLLM(mine_responses=[mine_payload("**R**")]).stats()

    missing_top = set(real) - set(fake)
    assert not missing_top, f"the fake omits top-level stats keys: {sorted(missing_top)}"

    for pool_key in ("calls_made", "refused"):
        missing = set(real[pool_key]) - set(fake[pool_key])
        assert not missing, (
            f"the fake's {pool_key} omits {sorted(missing)}, so any production "
            "branch reading those pools cannot be exercised under test"
        )
    assert "gate" in fake["calls_made"], "both budget preflights read this key"


# ---------------------------------------------------------------------------
# Reject an unusable provider before spending the mining budget
# ---------------------------------------------------------------------------
#
# A version probe can report a missing interpreter before any model call.


def test_a_usable_provider_passes_the_preflight(tmp_path):
    from self_improve.config import Config
    from self_improve.pipeline import provider_preflight

    ok = tmp_path / "ok-cli"
    ok.write_text("#!/bin/bash\necho 'cli 1.0'\n")
    ok.chmod(0o755)
    cfg = Config(allowed_providers=("codex",), codex_path=str(ok))

    got = provider_preflight(cfg)
    assert got["usable"] == ["codex"]
    assert got["unusable"] == {}
    assert got["any_usable"] is True


def test_corpus_preflight_only_executes_its_own_provider_fixtures(corpus):
    import subprocess
    from self_improve.pipeline import provider_preflight

    refused = []
    seen = []
    def fixture_only(argv):
        if not Path(argv[0]).is_relative_to(corpus.root):
            refused.append(argv)
            raise AssertionError("the scripted pipeline tried a host provider")
        seen.append(argv)
        return subprocess.run(argv, capture_output=True, text=True, timeout=5)

    got = provider_preflight(corpus.cfg, run=fixture_only)
    assert not refused, "provider paths escaped the synthetic corpus"
    assert got["usable"] == ["claude", "codex"] and got["unusable"] == {}
    assert len(seen) == 2 and all(argv[1:] == ["--version"] for argv in seen)
    for argv in seen.copy():
        attempted_model = fixture_only([argv[0], "-p", "invented input"])
        assert attempted_model.returncode != 0, "a probe fixture accepted a model invocation"


def test_a_provider_whose_interpreter_is_missing_is_reported_with_its_stderr(tmp_path):
    """An executable provider can still fail when its interpreter is unavailable."""
    from self_improve.config import Config
    from self_improve.pipeline import provider_preflight

    broken = tmp_path / "broken-cli"
    broken.write_text("#!/bin/bash\necho 'env: node: No such file or directory' >&2\nexit 127\n")
    broken.chmod(0o755)
    cfg = Config(allowed_providers=("codex",), codex_path=str(broken))

    got = provider_preflight(cfg)
    assert got["usable"] == []
    assert got["any_usable"] is False
    assert "node" in got["unusable"]["codex"], got["unusable"]
    assert "127" in got["unusable"]["codex"]


def test_an_absent_binary_is_reported_not_raised(tmp_path):
    from self_improve.config import Config
    from self_improve.pipeline import provider_preflight

    cfg = Config(allowed_providers=("codex",), codex_path=str(tmp_path / "nope"))
    got = provider_preflight(cfg)
    assert got["any_usable"] is False
    assert "codex" in got["unusable"]


def test_one_working_provider_is_enough(tmp_path):
    """quotapick may route to either; the run is viable if ANY can start."""
    from self_improve.config import Config
    from self_improve.pipeline import provider_preflight

    ok = tmp_path / "ok"
    ok.write_text("#!/bin/bash\nexit 0\n")
    ok.chmod(0o755)
    cfg = Config(
        allowed_providers=("claude", "codex"),
        claude_path=str(ok),
        codex_path=str(tmp_path / "missing"),
    )
    got = provider_preflight(cfg)
    assert got["any_usable"] is True
    assert got["usable"] == ["claude"]
    assert "codex" in got["unusable"]


def test_a_run_with_no_usable_provider_spends_nothing(corpus, monkeypatch):
    """A provider preflight refusal prevents all mining calls.

    Removing the mine_blocked guard must fail this test: the miner would
    attempt calls after preflight established that no provider is usable."""
    import dataclasses

    cfg = dataclasses.replace(corpus.cfg, codex_path="/nonexistent/codex",
                              claude_path="/nonexistent/claude")
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    assert stats["providers"]["any_usable"] is False
    assert stats["mine"]["attempted"] == 0, "the miner spent calls anyway"
    assert stats["llm"]["calls_made"]["cheap"] == 0
    assert stats["mine"]["taxonomy"].get("provider_unavailable"), (
        "the run does not say WHY nothing was mined"
    )


def test_the_scan_still_commits_when_the_provider_is_dead(corpus, monkeypatch):
    """Provider refusal must preserve the scan's successfully recorded incidents."""
    import dataclasses

    cfg = dataclasses.replace(corpus.cfg, codex_path="/nonexistent/codex",
                              claude_path="/nonexistent/claude")
    llm = ScriptedLLM(mine_responses=[])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    assert stats["scan"]["files_succeeded"] > 0
    incidents = corpus.store.query_one("SELECT COUNT(*) AS n FROM incidents")
    assert incidents["n"] > 0, "the scan's work was thrown away"


def test_a_dry_run_is_not_blocked_by_a_dead_provider(corpus):
    """--dry-run makes no LLM calls at all, so a dead CLI is irrelevant to it."""
    import dataclasses

    cfg = dataclasses.replace(corpus.cfg, codex_path="/nonexistent/codex",
                              claude_path="/nonexistent/claude")
    stats = run_pipeline(cfg, corpus.store, dry_run=True)
    assert stats["scan"]["files_attempted"] > 0


def test_a_dead_provider_is_excluded_while_a_live_one_still_runs(corpus, tmp_path):
    """Remove unavailable providers from the router's candidate set.
    One available provider must not make an unavailable provider eligible again.
    """
    import dataclasses

    ok = tmp_path / "ok-cli"
    ok.write_text("#!/bin/bash\nexit 0\n")
    ok.chmod(0o755)
    dead = tmp_path / "dead-cli"
    dead.write_text("#!/bin/bash\necho 'env: node: No such file or directory' >&2\nexit 127\n")
    dead.chmod(0o755)

    cfg = dataclasses.replace(
        corpus.cfg, allowed_providers=("claude", "codex"),
        claude_path=str(ok), codex_path=str(dead),
    )
    seen: list[tuple] = []

    def factory(passed_cfg, store, run_id, raw_dir):
        seen.append(passed_cfg.allowed_providers)
        return ScriptedLLM(
            passed_cfg, store, run_id, raw_dir,
            mine_responses=[mine_payload(f"**R{i}**") for i in range(8)],
        ).factory()(passed_cfg, store, run_id, raw_dir)

    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=factory)

    assert stats["providers"]["usable"] == ["claude"]
    assert "codex" in stats["providers"]["unusable"]
    assert seen and seen[-1] == ("claude",), (
        f"the router was still offered the dead provider: {seen}"
    )
    # and the run was NOT blocked — one working provider is enough to mine
    assert stats["mine"]["attempted"] > 0


def test_a_call_that_never_produced_output_is_not_reported_as_prose(corpus, monkeypatch):
    """Keep a failed call distinct from an unparseable answer.

    Script the failure at the LLM boundary so the adapter and downstream report
    are both exercised."""
    llm = ScriptedLLM(
        mine_responses=[mine_payload(f"**R{i}**") for i in range(4)],
        mine_failure=("spawn_error", "exit 127: env: node: No such file or directory"),
    )
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    tax = stats["mine"]["taxonomy"]
    assert not tax.get("MineParseFailure"), (
        f"a call that never ran was filed as an unparseable answer: {tax}"
    )
    assert any(k.startswith("call_failed") for k in tax), (
        f"the real outcome is not named anywhere: {sorted(tax)}"
    )
    assert "spawn_error" in " ".join(tax), f"the outcome class is lost: {sorted(tax)}"
    # Call failures must leave retryable incidents in the queue. The exception
    # must propagate before a successful mining result is persisted.
    still_new = corpus.store.query(
        "SELECT COUNT(*) AS n FROM incidents WHERE status = 'new'"
    )[0]["n"]
    assert still_new > 0, "a failed call consumed the queue"


def test_a_stage_that_only_ran_out_of_budget_is_not_degraded():
    """Budget refusal alone must not mark a stage as degraded.

    Contrast zero successful and zero failed calls with actual call failures,
    completed gate verdicts, and proposals deliberately held for review."""
    from self_improve.pipeline import derive_run_status

    verdicts = {"gated_pass": 0, "gated_fail": 0, "ungated": 0, "inconclusive": 0}
    status, reasons = derive_run_status(
        {"gate": {"attempted": 32, "failed": 0, **verdicts}}
    )
    assert (status, reasons) == ("ok", []), (
        f"a budget-refused stage was called {status!r}: {reasons}"
    )

    # Same shape, one real failure: now it is degraded and says so.
    status, reasons = derive_run_status(
        {"gate": {"attempted": 32, "failed": 1, **verdicts}}
    )
    assert status == "degraded" and "gate" in reasons[0], (status, reasons)

    # A gated_fail is the gate WORKING. It must never read as a dead stage.
    status, reasons = derive_run_status(
        {"gate": {"attempted": 3, "failed": 2, **{**verdicts, "gated_fail": 1}}}
    )
    assert (status, reasons) == ("ok", []), (status, reasons)

    # Under --review-only every proposal is held, and that is the intended
    # outcome, not a broken apply stage.
    assert derive_run_status(
        {"apply": {"attempted": 4, "applied": 0, "held": 4, "failed": 0}}
    ) == ("ok", [])

    # A stage that never ran at all is silent, not degraded.
    assert derive_run_status({"mine": {"attempted": 0, "succeeded": 0, "failed": 0}}) == (
        "ok",
        [],
    )
    # A stage that reported no counts at all is a legal shape — the pipeline
    # writes `{"skipped": "budget_exhausted"}` — and is not judged.
    assert derive_run_status({"mine": {"skipped": "budget_exhausted"}}) == ("ok", [])

    # But HALF a triple is a bug in that stage. Defaulting the missing half to
    # zero turns a broken reporter into a confident "the night was fine".
    with pytest.raises(ValueError, match="incomplete"):
        derive_run_status({"mine": {"attempted": 81, "succeeded": None, "failed": 81}})
    with pytest.raises(ValueError, match="incomplete"):
        derive_run_status({"mine": {"attempted": 81}})
    # A stage that RENAMED a counter is the same bug wearing a different hat.
    with pytest.raises(ValueError, match="incomplete"):
        derive_run_status({"gate": {"attempted": 3, "failed": 0, "passed": 3}})


def test_a_run_whose_whole_stage_failed_is_not_recorded_ok(corpus):
    """An entirely failed attempted stage must not be recorded as a healthy run.

    Sabotage: hardcode ok in the finish-run update."""
    llm = ScriptedLLM(
        mine_responses=[],
        mine_failure=("spawn_error", "exit 127: env: node: No such file or directory"),
    )
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    assert stats["mine"]["attempted"] > 0 and stats["mine"]["succeeded"] == 0

    row = corpus.store.query("SELECT status FROM runs ORDER BY started DESC LIMIT 1")[0]
    assert row["status"] == "degraded", (
        f"a run with no successful mine call was recorded {row['status']!r}"
    )
    # And the reason must name the stage, or 'degraded' is just a different
    # word for 'something happened'.
    reasons = " ".join(stats["status_reasons"])
    assert "mine" in reasons, f"the failing stage is not named: {stats['status_reasons']}"


def test_a_healthy_run_is_still_ok(corpus):
    """Narrowness guard: the rule fires on total stage failure, not on any
    failure at all. A run with successes stays `ok`."""
    # The gate is refused by its own budget rather than left unscripted: an
    # unscripted gate genuinely fails every call, and this test is about mining
    # succeeding, not about hiding a dead stage. `test_an_unscripted_gate_is_a
    # _real_degradation` below covers the other case on purpose.
    cfg = dataclasses.replace(corpus.cfg, max_gate_calls_per_run=0)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(40)])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())
    assert stats["mine"]["succeeded"] > 0
    row = corpus.store.query("SELECT status FROM runs ORDER BY started DESC LIMIT 1")[0]
    assert row["status"] == "ok", (
        f"a healthy run was downgraded to {row['status']!r}: {stats['status_reasons']}"
    )
    assert stats["status_reasons"] == []


def test_an_unscripted_gate_is_a_real_degradation(corpus):
    """The finding that fixing the two tests above turned up.

    With mining scripted and the gate not, the gate attempts every proposal and
    fails every one. That run IS degraded, and the status now says so — where
    before it said `ok` and the operator had no signal at all. Kept as a test
    because it is the second stage, after mine, proven to reach this state.
    """
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(40)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    assert stats["gate"]["attempted"] > 0 and stats["gate"]["failed"] > 0
    row = corpus.store.query("SELECT status FROM runs ORDER BY started DESC LIMIT 1")[0]
    assert row["status"] == "degraded"
    assert any("gate" in r for r in stats["status_reasons"]), stats["status_reasons"]
    assert not any("mine" in r for r in stats["status_reasons"]), (
        f"mining worked; it must not be named: {stats['status_reasons']}"
    )


def test_a_partly_failing_stage_is_still_ok(corpus):
    """Partial success remains ok with failures reported in stage counts.

    A short scripted response list produces both successful and failed calls."""
    cfg = dataclasses.replace(corpus.cfg, max_gate_calls_per_run=0)
    llm = ScriptedLLM(mine_responses=[mine_payload("**R**")])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())
    mine = stats["mine"]
    assert mine["succeeded"] > 0 and mine["failed"] > 0, (
        f"this test needs a MIXED stage to mean anything: {mine}"
    )
    row = corpus.store.query("SELECT status FROM runs ORDER BY started DESC LIMIT 1")[0]
    assert row["status"] == "ok", (
        f"a partial failure was called {row['status']!r}; only a stage that "
        f"produced nothing is degraded: {mine}"
    )
    assert stats["status_reasons"] == []


def test_an_answer_that_really_did_not_parse_still_says_so(corpus, monkeypatch):
    """The guard must be narrow: a model that DID reply with prose is a real
    MineParseFailure and the label is correct for it."""
    from self_improve import pipeline as pl

    monkeypatch.setattr(pl, "_llm_agentic_json", lambda *a, **kw: None)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(4)])
    stats = run_pipeline(
        corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory()
    )
    assert stats["mine"]["taxonomy"].get("MineParseFailure")


def test_a_constraint_break_names_the_constraint_not_a_story_about_one():
    """Classify the constraint named by SQLite instead of guessing its cause.

    Unique, not-null, and foreign-key failures receive distinct keys.
    An unrecognized message remains explicitly unparsed."""
    import sqlite3

    from self_improve.pipeline import integrity_key

    assert (
        integrity_key(sqlite3.IntegrityError("UNIQUE constraint failed: t.a, t.b"))
        == "IntegrityError:unique:t"
    )
    assert (
        integrity_key(sqlite3.IntegrityError("NOT NULL constraint failed: t.c"))
        == "IntegrityError:not_null:t.c"
    )
    assert (
        integrity_key(sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
        == "IntegrityError:foreign_key"
    )
    # A message we have never seen is reported as unparsed rather than guessed
    # into the nearest bucket. A wrong bucket is worse than an unknown one.
    assert (
        integrity_key(sqlite3.IntegrityError("something new from a future sqlite"))
        == "IntegrityError:unparsed"
    )


def test_the_mine_loop_files_a_constraint_break_under_its_constraint(corpus, monkeypatch):
    """The wiring. The helper above is worth nothing if nothing calls it."""
    import sqlite3

    from self_improve import pipeline as pl
    from self_improve import miner as mn

    def boom(*a, **kw):
        raise sqlite3.IntegrityError("NOT NULL constraint failed: learnings.rule_text")

    monkeypatch.setattr(mn, "_persist_mine_payload", boom)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(40)])
    cfg = dataclasses.replace(corpus.cfg, max_gate_calls_per_run=0)
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    tax = stats["mine"]["taxonomy"]
    assert "IntegrityError" not in tax, (
        f"the constraint failure was filed under a bare class: {tax}"
    )
    assert tax.get("IntegrityError:not_null:learnings.rule_text"), (
        f"the constraint is not named: {sorted(tax)}"
    )


def test_the_specs_dir_follows_state_dir_into_a_temp_tree(tmp_path):
    """Resolve generated specs under the configured state directory.

    Changing state_dir must also move the default specs directory. An explicit
    absolute private destination remains supported."""
    import dataclasses

    from self_improve.config import Config
    from self_improve.pipeline import regression_specs_dir

    cfg = Config(state_dir=str(tmp_path / "state"))
    got = regression_specs_dir(cfg)
    assert got == tmp_path / "state" / "evals" / "regression", got
    assert got.is_dir()

    # An absolute override selects another private directory. The default
    # remains relative to state_dir.
    elsewhere = tmp_path / "elsewhere"
    cfg2 = dataclasses.replace(cfg, regression_specs_dir=str(elsewhere))
    assert regression_specs_dir(cfg2) == elsewhere

    # And production still lands where it always did.
    assert Config().regression_specs_dir == "evals/regression"


def test_the_funnel_in_a_real_run_report_has_data_behind_it(corpus, tmp_path):
    """The scripted provider must persist call rows that the report funnel reads.
    Making ScriptedLLM._record a no-op must break this assertion.
    """
    import dataclasses

    from self_improve import report

    cfg = dataclasses.replace(corpus.cfg, max_gate_calls_per_run=0)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(40)])
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    rows = corpus.store.query(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE run_id = ? AND stage LIKE 'mine%'",
        (stats["run_id"],),
    )
    assert rows[0]["n"] == stats["mine"]["attempted"], (
        f"llm_calls holds {rows[0]['n']} mine rows but the stage counted "
        f"{stats['mine']['attempted']} attempts; the report reads the table"
    )

    out = tmp_path / "r.md"
    report.generate(corpus.store, cfg, stats["run_id"], out)
    text = out.read_text()
    attempted = stats["mine"]["attempted"]
    assert f"| Mine calls attempted | {attempted} |" in text, (
        [ln for ln in text.splitlines() if "Mine calls" in ln]
    )
    assert "| Mine calls attempted | 0 |" not in text


def test_the_funnel_counts_failed_calls_too(corpus, tmp_path):
    """The funnel counts unsuccessful calls as attempted work.

    A provider fake must log failures as well as successes."""
    import dataclasses

    from self_improve import report

    cfg = dataclasses.replace(corpus.cfg, max_gate_calls_per_run=0)
    llm = ScriptedLLM(
        mine_responses=[],
        mine_failure=("spawn_error", "exit 127: env: node: No such file or directory"),
    )
    stats = run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())
    attempted = stats["mine"]["attempted"]
    assert attempted > 0 and stats["mine"]["succeeded"] == 0

    n = corpus.store.query(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE run_id = ? AND stage LIKE 'mine%' "
        "AND outcome = 'spawn_error'",
        (stats["run_id"],),
    )[0]["n"]
    assert n == attempted, f"only {n} of {attempted} failed calls were logged"

    out = tmp_path / "r2.md"
    report.generate(corpus.store, cfg, stats["run_id"], out)
    text = out.read_text()
    assert f"| Mine calls attempted | {attempted} |" in text
    assert f"| Mine calls failed | {attempted} |" in text
    assert "spawn_error" in text, "the real outcome is not in the taxonomy table"


@pytest.mark.parametrize("enabled,review_only,verdict,expected", [
    (False, False, "gated_pass", False),
    (True, False, "gated_pass", True),
    (True, True, "gated_pass", False),
    (True, False, "ungated", False),
])
def test_pipeline_enforces_class_policy_at_the_actual_writer(
    corpus, monkeypatch, enabled, review_only, verdict, expected
):
    """A passing gate must reach a real file write only with current class consent."""
    from self_improve.execution_policy import set_class_policy, waiting_proposals

    _capture_gate_wiring(monkeypatch, verdict=verdict)
    # Exercise direct global-file delivery, independent of the fixture's
    # unrelated clone/symlink topology. Routing and apply remain real.
    cfg = dataclasses.replace(corpus.cfg, auto_apply=True)
    if enabled:
        set_class_policy(corpus.store, "global", True)
    target = Path(cfg.global_claude_md)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Instructions\n")
    # One candidate keeps this about policy and final delivery rather than
    # sequential proposals whose generated hunks may overlap.
    llm = ScriptedLLM(mine_responses=[mine_payload("**Keep full raw output before parsing.**", scope_guess="global")])
    stats = run_pipeline(cfg, corpus.store, review_only=review_only, _llm_factory=llm.factory())
    proposals = corpus.store.query("SELECT * FROM proposals WHERE run_id=?", (stats["run_id"],))
    assert len(proposals) == 1, stats
    assert stats["gate"][verdict] == 1, stats
    assert stats["apply"]["attempted"] == 1
    assert stats["apply"]["failed"] == 0, stats["apply"]
    assert stats["apply"]["applied"] == int(expected), stats["apply"]
    assert ("Keep full raw output before parsing." in target.read_text()) is expected
    assert len(waiting_proposals(corpus.store, cfg)) == int(not expected)
    assert stats["execution_policy"]["classes"]["global"]["enabled"] is enabled
    from self_improve.dashboard import run_data
    deliveries = run_data.records(corpus.store, stats["run_id"], kind="deliveries")
    assert len(deliveries["records"]) == int(expected)
    assert deliveries["reason"] == ""
    assert stats["budget_limits"] == {
        "cheap": cfg.max_cheap_calls_per_run,
        "strong": cfg.max_strong_calls_per_run,
        "gate": cfg.max_gate_calls_per_run,
    }
    if expected:
        actual = deliveries["records"][0]
        assert actual["id"] == stats["apply"]["operation_ids"][0]
        assert actual["record"]["proposal"]["id"] == proposals[0]["id"]
        assert actual["result"]["snapshot_commit_after"] == proposals[0]["snapshot_commit_after"]


# ----------------------------------------------------------------------
# gate-proposal: re-gating an EXISTING proposal
# ----------------------------------------------------------------------


def _existing_proposal(store, *, rule_text="**Always X.**"):
    from self_improve.store import new_id, utc_now_iso

    lid, pid = new_id(), new_id()
    store.insert(
        "learnings",
        {
            "id": lid, "title": "t", "rule_text": rule_text, "why": "w", "category": "c",
            "scope": "global", "evidence_count": 1, "project_count": 1,
            "projects_json": "[]", "first_seen": utc_now_iso(), "last_seen": utc_now_iso(),
            "confidence": 0.9, "status": "proposed", "duplicate_of": "",
            "created_at": utc_now_iso(),
        },
    )
    store.insert(
        "proposals",
        {
            "id": pid, "learning_id": lid, "run_id": "", "target_path": "/tmp/T.md",
            "target_kind": "global_claude_md", "action": "add", "diff_unified": "",
            "status": "pending", "eval_result_id": "", "applied_at": "",
            "snapshot_commit_before": "", "snapshot_commit_after": "",
            "created_at": utc_now_iso(),
        },
    )
    store.commit()
    return pid, lid


def test_gate_proposal_updates_the_existing_row_and_never_inserts_a_second(
    corpus, monkeypatch
):
    """The whole point of the command, asserted at the database.

    `_persist_proposal` INSERTS, which is right for the pipeline and wrong
    here: re-gating a proposal must move the row it was asked about, not leave
    the old status behind under a duplicate id.
    """
    from self_improve.pipeline import gate_existing_proposal

    _capture_gate_wiring(monkeypatch)
    pid, lid = _existing_proposal(corpus.store)
    llm = ScriptedLLM(mine_responses=[])

    stats = gate_existing_proposal(
        corpus.cfg, corpus.store, pid, _llm_factory=llm.factory()
    )

    rows = corpus.store.query("SELECT * FROM proposals WHERE learning_id = ?", (lid,))
    assert len(rows) == 1, f"expected the one row to move, found {len(rows)}"
    assert rows[0]["id"] == pid
    assert rows[0]["status"] == "ungated", rows[0]
    assert stats["gate"]["attempted"] == 1
    assert stats["gate"]["ungated"] == 1
    assert stats["gate_proposal"]["status_before"] == "pending"
    assert stats["gate_proposal"]["status_after"] == "ungated"


def test_gate_proposal_records_the_transition_in_the_audit_trail(corpus, monkeypatch):
    """A status change with no proposal_events row is a change nobody can
    explain later. rollback and apply both write one; so must this."""
    from self_improve.pipeline import gate_existing_proposal

    _capture_gate_wiring(monkeypatch)
    pid, _ = _existing_proposal(corpus.store)
    llm = ScriptedLLM(mine_responses=[])
    gate_existing_proposal(corpus.cfg, corpus.store, pid, _llm_factory=llm.factory())

    events = corpus.store.query(
        "SELECT * FROM proposal_events WHERE proposal_id = ?", (pid,)
    )
    assert [e["event"] for e in events] == ["gated"], events
    # actor="auto", not "user". A person typed `gate-proposal`, and the GATE
    # made the judgement. Since V3 shipped, actor="user" is how "what did a
    # human decide?" is answered, so letting the gate borrow the word makes
    # that question unanswerable. The mapping is store.ACTOR_FOR_EVENT.
    assert events[0]["actor"] == "auto"
    assert "pending -> ungated" in events[0]["note"]


def test_regating_retains_every_configured_scenario_after_two_failures(corpus, monkeypatch):
    from self_improve.pipeline import gate_existing_proposal

    calls = _capture_gate_wiring(monkeypatch, verdict="gated_fail")
    pid, _ = _existing_proposal(corpus.store)
    target = corpus.root / "manual-rule.md"
    target.write_text("invented unchanged instruction\n")
    corpus.store.update("proposals", "id", pid, {"target_path": str(target)})
    corpus.store.commit()
    stats = gate_existing_proposal(corpus.cfg, corpus.store, pid,
                                   _llm_factory=ScriptedLLM(mine_responses=[]).factory())
    assert [c["scenario"] for c in calls["generate"]] == list(range(corpus.cfg.eval_scenarios))
    assert len(calls["gate"]) == corpus.cfg.eval_scenarios
    split = stats["gate"]["scenario_splits"][0]
    assert split["scenarios_run"] == split["tally"]["gated_fail"] == corpus.cfg.eval_scenarios
    assert stats["gate_proposal"]["status_after"] == "gated_fail"
    assert target.read_text() == "invented unchanged instruction\n"


def test_gate_proposal_bills_only_the_gate_pool(corpus, monkeypatch):
    """Re-evaluating an existing proposal must not spend the mining call pool."""
    from self_improve.pipeline import gate_existing_proposal

    _capture_gate_wiring(monkeypatch)
    pid, _ = _existing_proposal(corpus.store)
    llm = ScriptedLLM(mine_responses=[])
    stats = gate_existing_proposal(
        corpus.cfg, corpus.store, pid, _llm_factory=llm.factory()
    )
    assert stats["llm"]["calls_made"]["cheap"] == 0, stats["llm"]


@pytest.mark.parametrize("decided", ["approved_user", "rejected_user", "applied", "rolled_back", "superseded"])
def test_regating_records_new_eval_history_without_undoing_a_decision(corpus, monkeypatch, decided):
    from self_improve.pipeline import gate_existing_proposal

    _capture_gate_wiring(monkeypatch)
    pid, _ = _existing_proposal(corpus.store)
    corpus.store.update("proposals", "id", pid, {"status": decided})
    corpus.store.commit()
    before = corpus.store.query_one("SELECT * FROM proposals WHERE id=?", (pid,))
    stats = gate_existing_proposal(corpus.cfg, corpus.store, pid,
                                   _llm_factory=ScriptedLLM(mine_responses=[]).factory())
    assert corpus.store.query_one("SELECT * FROM proposals WHERE id=?", (pid,)) == before
    assert stats["gate_proposal"]["status_after"] == decided
    assert stats["gate_proposal"]["eval_verdict"] == "ungated"
    history = corpus.store.query("SELECT * FROM proposal_eval_history WHERE proposal_id=?", (pid,))
    assert len(history) == 1
    assert history[0]["verdict"] == "ungated"
    frozen = corpus.store.query_one("SELECT * FROM proposal_revisions WHERE id=?", (history[0]["source_revision_id"],))
    assert json.loads(frozen["snapshot_json"])["proposal"] == before


@pytest.mark.parametrize("change", ["approve", "edit"])
def test_an_eval_finishing_after_review_or_edit_does_not_overwrite_it(corpus, monkeypatch, change):
    from self_improve import pipeline
    from self_improve.commands import review_snapshot, submit_command
    from self_improve.review import preview_selection
    from self_improve.propose import make_unified_diff

    _capture_gate_wiring(monkeypatch)
    pid, _ = _existing_proposal(corpus.store)
    target = corpus.root / 'reviewed-during-eval.md'
    target.write_text('invented existing rule\n')
    corpus.store.update('proposals', 'id', pid, {'target_path': str(target),
        'diff_unified': make_unified_diff('invented existing rule\n', 'invented existing rule\nnew rule\n', str(target))})
    corpus.store.commit()
    original = pipeline.gate_one_proposal
    captured = {}
    def interleaved(store, cfg, *args, **kwargs):
        if change == "approve":
            reviewed = review_snapshot(store, pid, cfg)
            shown = preview_selection(store, cfg, [pid])
            captured["command"] = submit_command(store, cfg, {
                "request_key": "decision-during-eval", "action": "approve",
                "preview_revision": shown['revision'],
                "members": [{"proposal_id": pid, "revision": reviewed["revision"]}],
            })
        else:
            store.update("proposals", "id", pid, {"diff_unified": "a newly reviewed edit"})
            store.commit()
        captured["proposal"] = store.query_one("SELECT * FROM proposals WHERE id=?", (pid,))
        return original(store, cfg, *args, **kwargs)
    monkeypatch.setattr(pipeline, "gate_one_proposal", interleaved)
    stats = pipeline.gate_existing_proposal(corpus.cfg, corpus.store, pid,
                                            _llm_factory=ScriptedLLM(mine_responses=[]).factory())
    assert corpus.store.query_one("SELECT * FROM proposals WHERE id=?", (pid,)) == captured["proposal"]
    assert stats["gate_proposal"]["status_update"] == ("preserved_decision" if change == "approve" else "preserved_changed_revision")
    assert len(corpus.store.query("SELECT * FROM proposal_eval_history WHERE proposal_id=?", (pid,))) == 1


def test_queue_snapshots_bind_effective_arguments_even_on_scan_failure(corpus,monkeypatch):
    from self_improve import queue_history
    def fail(*args,**kwargs):raise RuntimeError('invented scanner failure')
    monkeypatch.setattr('self_improve.scan.scan_all',fail)
    with pytest.raises(RuntimeError,match='invented scanner'):
        run_pipeline(corpus.cfg,corpus.store,dry_run=True,max_cheap_calls=3,max_strong_calls=2,project_filter='invented-filter')
    snapshots=corpus.store.query('SELECT * FROM queue_snapshots ORDER BY phase')
    assert {s['phase'] for s in snapshots}=={'start','finish'}
    assert len({s['settings_json'] for s in snapshots})==1
    settings=json.loads(snapshots[0]['settings_json'])
    assert settings['cheap_call_cap']==3 and settings['strong_call_cap']==2
    assert settings['gate_call_cap']==corpus.cfg.max_gate_calls_per_run
    assert settings['project_filter']=='invented-filter' and settings['dry_run']
    assert queue_history.read_run(corpus.store,snapshots[0]['run_id'])['snapshot']['phase']=='finish'


def test_report_failure_keeps_first_terminal_queue_snapshot(corpus,monkeypatch):
    from self_improve import queue_history
    observed=[]
    def fail(store,cfg,run_id,path):
        snapshot=store.query_one("SELECT * FROM queue_snapshots WHERE run_id=? AND phase='finish'",(run_id,))
        assert snapshot;observed.append(snapshot)
        session=store.query_one('SELECT file_path FROM sessions LIMIT 1')
        store.insert_incident({'id':'arrived-after-terminal','session_file':session['file_path'],'signal_type':'correction'})
        store.commit()
        raise RuntimeError('invented report failure')
    monkeypatch.setattr('self_improve.report.generate',fail)
    with pytest.raises(RuntimeError,match='invented report'):
        run_pipeline(corpus.cfg,corpus.store,dry_run=True)
    assert len(observed)==1
    snap=observed[0];got=queue_history.read_run(corpus.store,snap['run_id'])
    assert got['snapshot']['id']==snap['id'] and got['queue_count']==snap['queue_count']
    assert corpus.store.query_one('SELECT status FROM runs WHERE id=?',(snap['run_id'],))['status']=='error'
    assert corpus.store.query_one("SELECT COUNT(*) n FROM incidents WHERE status='new'")['n']==snap['queue_count']+1


def test_queue_capture_failure_does_not_publish_a_phantom_running_run(corpus):
    from self_improve.queue_history import QueueHistoryError
    corpus.store.conn.execute('DROP TRIGGER queue_snapshots_no_update')
    with pytest.raises(QueueHistoryError,match='trigger'):
        run_pipeline(corpus.cfg,corpus.store,dry_run=True)
    assert not corpus.store.conn.in_transaction
    assert corpus.store.query('SELECT * FROM runs')==[]
    corpus.store.commit()
    assert corpus.store.query('SELECT * FROM queue_snapshots')==[]
