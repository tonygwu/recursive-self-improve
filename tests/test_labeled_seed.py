"""Public labeled examples are invented and load without personal transcripts."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from self_improve.evals.self_eval import load_labeled

REPO_ROOT = Path(__file__).resolve().parents[1]
LABELED_DIR = REPO_ROOT / "evals" / "synthetic" / "labeled"

EXPECTED_IDS = {"synthetic-retry"}


@pytest.fixture(scope="module")
def labeled() -> list[dict]:
    return load_labeled(LABELED_DIR)


def test_all_files_load_via_load_labeled(labeled):
    # load_labeled itself raises on any malformed file; reaching here means
    # every *.yaml passed strict validation. Pin the expected seed set.
    assert {item["id"] for item in labeled} == EXPECTED_IDS
    assert len(labeled) == len(EXPECTED_IDS), "duplicate ids in seed set"


def test_every_yaml_file_in_dir_was_loaded(labeled):
    # load_labeled globs *.yaml; a stray .yml or misnamed file would be
    # silently ignored, so assert the directory holds exactly the loaded set.
    on_disk = sorted(p.name for p in LABELED_DIR.iterdir() if p.is_file())
    assert all(name.endswith(".yaml") for name in on_disk)
    assert len(on_disk) == len(labeled)


def test_dates_are_iso_and_ordered(labeled):
    for item in labeled:
        dr = item["date_range"]
        start = dt.date.fromisoformat(dr["start"])
        end = dt.date.fromisoformat(dr["end"])
        assert start <= end, f"{item['id']}: start {start} after end {end}"


def test_expected_rule_gist_is_single_sentence(labeled):
    for item in labeled:
        gist = item["expected_rule_gist"].strip()
        # One sentence: exactly one terminal period (internal periods in
        # tokens like ~/.claude or $0.00 are followed by non-space).
        sentence_ends = [
            i
            for i, ch in enumerate(gist)
            if ch in ".!?" and (i + 1 == len(gist) or gist[i + 1] == " ")
        ]
        assert sentence_ends == [len(gist) - 1], (
            f"{item['id']}: expected_rule_gist must be exactly one sentence"
        )


def test_source_session_ids_are_invented(labeled):
    for item in labeled:
        assert item["source_session_ids"]
        assert all(sid.startswith("synthetic-session-") for sid in item["source_session_ids"])
