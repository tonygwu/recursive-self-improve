"""The publication audit must not turn a failed scan into a clean result."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from self_improve.data_boundary import DataBoundaryError, verify_files


@pytest.fixture
def audit(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "ops" / "scan-secrets.py"
    spec = importlib.util.spec_from_file_location("secret_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    (checkout / "tracked.txt").write_text("original invented input\n")
    subprocess.run(["git", "-C", str(checkout), "add", ".gitleaks.toml", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Example",
                    "-c", "user.email=example@example.invalid", "commit", "-qm", "fixture"], check=True)
    (checkout / "tracked.txt").write_text("unstaged invented input\n")
    (checkout / "untracked.txt").write_text("untracked input\n")
    monkeypatch.setattr(module, "ROOT", checkout)
    return module


def fake_scanner(audit, monkeypatch, *, code=0, report="[]", version="8.30.1"):
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        if args[0] != "fixture-scanner":
            return original(args, **kwargs)
        calls.append(args)
        if args[1] == "version":
            return subprocess.CompletedProcess(args, 0, version + "\n", "")
        if report is not None:
            Path(args[args.index("--report-path") + 1]).write_text(report)
        return subprocess.CompletedProcess(args, code, "", "scanner status\n")

    monkeypatch.setattr(audit.subprocess, "run", run)
    return calls


def test_audit_refuses_checkout_output_before_invoking_scanner(audit, monkeypatch):
    calls = fake_scanner(audit, monkeypatch)
    with pytest.raises(DataBoundaryError, match="checkout"):
        audit.scan("fixture-scanner", audit.ROOT / "audit")
    assert calls == []
    assert not (audit.ROOT / "audit").exists()


def test_audit_refuses_an_unpinned_scanner_before_export(audit, monkeypatch, tmp_path):
    fake_scanner(audit, monkeypatch, version="0.0.0")
    output = tmp_path / "audit"
    with pytest.raises(ValueError, match="expected Gitleaks"):
        audit.scan("fixture-scanner", output)
    assert not output.exists()


@pytest.mark.parametrize(("code", "report"), [(2, None), (0, None), (0, "not JSON"),
                                              (0, '{"unexpected":true}'),
                                              (0, '[{"RuleID":"example"}]'), (1, "[]"),
                                              (1, '[{}]'), (1, '["invalid"]')])
def test_failed_missing_or_inconsistent_reports_never_claim_clean(audit, monkeypatch, tmp_path, code, report):
    fake_scanner(audit, monkeypatch, code=code, report=report)
    output = tmp_path / "audit"
    with pytest.raises(ValueError):
        audit.scan("fixture-scanner", output)
    assert not (output / "summary.json").exists()


def test_audit_scans_verified_worktree_bytes_with_explicit_redaction(audit, monkeypatch, tmp_path, capsys):
    calls = fake_scanner(audit, monkeypatch)
    output = tmp_path / "audit"
    assert audit.scan("fixture-scanner", output, history=True) == 0
    snapshot = output / "worktree"
    manifest = verify_files(snapshot)
    assert (snapshot / "snapshot/tracked.txt").read_text() == "unstaged invented input\n"
    assert not (snapshot / "snapshot/untracked.txt").exists()
    summary = json.loads((output / "summary.json").read_text())
    assert summary["worktree_manifest_sha256"] == manifest["sha256"]
    assert [row["mode"] for row in summary["scans"]] == ["current", "history"]
    for call in calls[1:]:
        assert "--redact=100" in call
        assert "--ignore-gitleaks-allow" in call
        assert Path(call[call.index("--gitleaks-ignore-path") + 1]).read_text() == ""
        assert Path(call[call.index("--config") + 1]).read_bytes() == (audit.ROOT / ".gitleaks.toml").read_bytes()
    assert "--log-opts=--all --full-history -m" in calls[-1]
    assert (output / "summary.json").stat().st_mode & 0o077 == 0
    console = json.loads(capsys.readouterr().out)
    assert "local_refs" not in console
    assert console["local_ref_count"] == len(summary["local_refs"])


def test_findings_produce_failure_and_only_aggregate_console_output(audit, monkeypatch, tmp_path, capsys):
    fake_scanner(audit, monkeypatch, code=1,
                 report='[{"RuleID":"example", "Match":"private example excerpt"}]')
    assert audit.scan("fixture-scanner", tmp_path / "audit") == 1
    console = capsys.readouterr().out
    assert "private example excerpt" not in console
    assert json.loads(console)["scans"] == [{"mode": "current", "findings": 1, "by_rule": {"example": 1}}]


def test_scanner_uses_the_frozen_configuration_even_if_source_changes(audit, monkeypatch, tmp_path):
    fake_scanner(audit, monkeypatch)
    original_backup = audit.backup_checkout
    original_config = (audit.ROOT / ".gitleaks.toml").read_bytes()

    def backup_then_edit(*args):
        manifest = original_backup(*args)
        (audit.ROOT / ".gitleaks.toml").write_text("changed after snapshot\n")
        return manifest

    monkeypatch.setattr(audit, "backup_checkout", backup_then_edit)
    output = tmp_path / "audit"
    assert audit.scan("fixture-scanner", output) == 0
    assert (output / "scanner-config.toml").read_bytes() == original_config


def test_history_ref_race_cannot_publish_a_clean_summary(audit, monkeypatch, tmp_path):
    fake_scanner(audit, monkeypatch)
    original = audit.subprocess.check_output
    reads = 0

    def read(args, **kwargs):
        nonlocal reads
        result = original(args, **kwargs)
        if args[:2] == ["git", "for-each-ref"]:
            reads += 1
            if reads == 2:
                result += "refs/heads/changed " + "1" * 40 + "\n"
        return result

    monkeypatch.setattr(audit.subprocess, "check_output", read)
    output = tmp_path / "audit"
    with pytest.raises(ValueError, match="refs changed"):
        audit.scan("fixture-scanner", output, history=True)
    assert not (output / "summary.json").exists()
