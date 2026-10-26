"""A denylist entry added after incidents were saved still keeps them from the miner.

The scan applies ``denylist_substrings`` when it first reads a session, so an
entry added later leaves that session's earlier incidents waiting. Mining must
skip those incidents, count each skip, and leave their stored status unchanged.
The corpus, transcripts and model responses are temporary fixtures.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.e2e_corpus import ScriptedLLM, build_corpus, mine_payload
from self_improve.pipeline import run_pipeline


@pytest.fixture
def corpus(tmp_path):
    corpus = build_corpus(tmp_path)
    run_pipeline(corpus.cfg, corpus.store, dry_run=True)
    return corpus


def _waiting(store):
    return store.query("SELECT id, session_file, project_path FROM incidents WHERE status = 'new'")


def _mine(corpus, cfg):
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**Invented rule {i}**") for i in range(40)])
    return run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())


def test_incidents_from_a_newly_denylisted_folder_are_not_mined(corpus):
    waiting = _waiting(corpus.store)
    folder = next(r["project_path"] for r in waiting if r["project_path"])
    denied = {r["id"] for r in waiting if folder in r["project_path"]}
    assert denied and len(denied) < len(waiting)

    stats = _mine(corpus, dataclasses.replace(corpus.cfg, denylist_substrings=(folder,)))

    still = {r["id"] for r in _waiting(corpus.store)}
    assert denied <= still, "a denylisted incident left the waiting queue"
    assert stats["mine"]["denylisted"]["incidents"] == len(denied)
    assert stats["mine"]["denylisted"]["by_substring"] == {folder: len(denied)}
    assert stats["mine"]["attempted"] >= 1
    assert stats["mine"]["attempted"] == stats["mine"]["succeeded"] + stats["mine"]["failed"]


def test_a_claude_project_folder_name_entry_also_matches(corpus):
    projects = Path(corpus.cfg.claude_projects_dir)
    waiting = _waiting(corpus.store)
    claude = [r for r in waiting if Path(r["session_file"]).is_relative_to(projects)]
    slug = Path(claude[0]["session_file"]).relative_to(projects).parts[0]
    denied = {r["id"] for r in claude if Path(r["session_file"]).relative_to(projects).parts[0] == slug}
    assert not any(slug in (r["project_path"] or "") for r in waiting), "fixture would match on the path"

    stats = _mine(corpus, dataclasses.replace(corpus.cfg, denylist_substrings=(slug,)))

    assert denied <= {r["id"] for r in _waiting(corpus.store)}
    assert stats["mine"]["denylisted"]["incidents"] == len(denied)


def test_an_empty_denylist_skips_nothing_and_says_so(corpus):
    stats = _mine(corpus, corpus.cfg)
    assert stats["mine"]["denylisted"] == {"incidents": 0, "by_substring": {}}


def test_the_run_report_lists_the_skipped_incidents(corpus):
    waiting = _waiting(corpus.store)
    folder = next(r["project_path"] for r in waiting if r["project_path"])
    _mine(corpus, dataclasses.replace(corpus.cfg, denylist_substrings=(folder,)))
    row = corpus.store.query_one("SELECT report_path FROM runs ORDER BY started DESC LIMIT 1")
    caps = Path(row["report_path"]).read_text().split("## Caps that bit")[1].split("\n## ")[0]
    assert "waiting incidents not mined: denylist now matches" in caps, caps
    assert folder in caps


def test_the_sessions_recorded_folder_matches_when_the_incident_path_differs(corpus):
    # A Codex incident can carry a later turn's cwd, while the scan matched
    # the session's recorded working directory.
    row = corpus.store.query_one(
        "SELECT i.id, i.session_file, s.project_path FROM incidents i JOIN sessions s "
        "ON s.file_path = i.session_file WHERE i.status = 'new' AND s.project_path <> '' LIMIT 1")
    corpus.store.update("incidents", "id", row["id"], {"project_path": "/invented/elsewhere"})
    corpus.store.commit()
    folder = row["project_path"]
    stats = _mine(corpus, dataclasses.replace(corpus.cfg, denylist_substrings=(folder,)))
    assert row["id"] in {r["id"] for r in _waiting(corpus.store)}
    assert stats["mine"]["denylisted"]["by_substring"].get(folder, 0) >= 1


@pytest.mark.parametrize("named", ["incident_folder", "session_folder"])
def test_choosing_a_denylisted_incident_to_mine_by_hand_is_refused(corpus, named):
    from self_improve import incident_jobs
    from self_improve.commands import CommandError

    row = corpus.store.query_one(
        "SELECT i.id, i.project_path, s.project_path AS recorded FROM incidents i JOIN sessions s "
        "ON s.file_path = i.session_file WHERE i.status = 'new' AND s.project_path <> '' LIMIT 1")
    folder = row["project_path"] if named == "incident_folder" else row["recorded"]
    if named == "session_folder":
        corpus.store.update("incidents", "id", row["id"], {"project_path": "/invented/elsewhere"})
        corpus.store.commit()
    cfg = dataclasses.replace(corpus.cfg, denylist_substrings=(folder,))
    with pytest.raises(CommandError) as raised:
        incident_jobs.preview(corpus.store, cfg, row["id"])
    assert raised.value.code == "IncidentDenylisted"
    assert row["id"] in {r["id"] for r in _waiting(corpus.store)}
