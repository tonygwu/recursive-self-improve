"""Rendered explanations must stay within the evidence in invented payloads."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from self_improve.evals.regression import majority_verdict


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    root = tmp_path_factory.mktemp("display-evidence")
    source = Path(__file__).resolve().parents[1] / "src/self_improve/dashboard/static/app.js"
    module = root / "app.mjs"
    module.write_bytes(source.read_bytes())
    probe = root / "probe.mjs"
    probe.write_text('''
import * as app from "./app.mjs";
const row = {
  id: "invented-learning", rule_text: "Check a result before using it.",
  why: "A missing result does not establish a successful check.",
  enforcement_gap: {flagged: false, violated_existing_rule: "", label: "Not recorded"},
  targets: [{target_path: "/invented/AGENTS.md", action: "add", eval: null}],
  unlinked_subject_evals: ["invented-eval"],
};
const project = {
  rules_written_here: 7, rules_received_detail: [], signals: {},
  benefit: {computable: false, value: "—", reason: "Recurrence measurements are unavailable."},
};
console.log(JSON.stringify({
  gate: app.renderGate({by_verdict: {inconclusive: 1}}),
  rule: app.renderRuleInspector(row, "why"),
  project: app.renderProjectInspector(project, "rules"),
}));
''')
    node = shutil.which("node")
    assert node, "Node is required to verify dashboard explanations"
    result = subprocess.run(
        [node, str(probe)], cwd=root, capture_output=True,
        text=True, check=True, timeout=60,
    )
    return json.loads(result.stdout)


def test_inconclusive_does_not_invent_disagreement_when_all_scenarios_error(rendered):
    assert majority_verdict({"error": 3}) == "inconclusive"
    assert "did not establish a passing or failing majority" in rendered["gate"]
    assert "scenarios disagreed" not in rendered["gate"]


def test_missing_measurements_do_not_invent_a_project_history(rendered):
    text = rendered["project"]
    assert "7</span> rule(s)" in text
    assert "Recurrence measurements are unavailable." in text
    assert "Every proposal so far targets a global" not in text
    assert "No rule has been applied here" not in text


def test_missing_rule_and_eval_links_report_the_recorded_scope(rendered):
    text = rendered["rule"]
    assert "invented-eval" in text
    assert "No violated rule was recorded" in text
    assert "no existing rule covered this" not in text
    assert "not linked to this proposal" in text
    assert "broken LINK" not in text
