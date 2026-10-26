"""Quota-selected reference labels: invented router decisions and CLI receipts."""
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import pytest
from quota_router import Selection

from self_improve.config import Config
from self_improve.data_boundary import DataBoundaryError
from self_improve.evals import filter_models as models
from self_improve.evals import filter_reference as ref
from tests.test_filter_eval import dataset, frozen, label


def pick(*, account="fixture_account", fits=True, policy=True, overlay=None, prefix=(), unset=()):
    return Selection.from_payload({"contract_version": 1,
        "decision": {"account": account, "provider": "claude", "fits": fits, "meets_policy": policy},
        "exec": {"env": overlay or {}, "argv_prefix": list(prefix), "unset_env": list(unset)}})


def envelope(*, model=models.REFERENCE, result=None, cost=.02):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": json.dumps(result or label("s:1")),
                       "usage": {"input_tokens": 100, "output_tokens": 80},
                       "modelUsage": {model: {"inputTokens": 100, "outputTokens": 80}},
                       "total_cost_usd": cost})


def payload():
    return models.reference_request({"anchor": "s:1", "events": []})


def test_router_discovers_accounts_from_configuration_not_hardcoded_ids(monkeypatch):
    accounts = [SimpleNamespace(id="personal_main", provider="claude"),
                SimpleNamespace(id="work_secondary", provider="claude"),
                SimpleNamespace(id="another", provider="codex")]
    monkeypatch.setattr(ref, "load_router_config", lambda **kw: SimpleNamespace(enabled_accounts=lambda: accounts))
    assert ref.claude_accounts({}, "/invented") == ("personal_main", "work_secondary")


def test_router_policy_account_overlay_and_tool_free_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-send-this-key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/wrong/inherited/account")
    monkeypatch.setenv("DROP_ME", "discard")
    seen = []
    selection = pick(overlay={"CLAUDE_CONFIG_DIR": "/selected/account"}, prefix=("wrapper",), unset=("DROP_ME",))
    def selector(**kwargs):
        seen.append(kwargs)
        return selection
    def execute(argv, *, prompt, cwd, env):
        assert argv[:2] == ["wrapper", "/fixture/claude"]
        assert env["CLAUDE_CONFIG_DIR"] == "/selected/account"
        assert "ANTHROPIC_API_KEY" not in env and "DROP_ME" not in env
        assert argv[argv.index("--tools") + 1] == ""
        assert "--safe-mode" in argv and "--strict-mcp-config" in argv
        assert "--no-session-persistence" in argv
        assert argv[argv.index("--max-turns") + 1] == "1"
        assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1600"
        assert str(tmp_path) in cwd
        return 0, envelope(), ""
    result, cost = ref.invoke(payload(), tmp_path, "request", replace(Config(), claude_path="/fixture/claude"),
                              selector=selector, executor=execute, accounts=("fixture_account",), exclude=("failed_account",))
    assert result["status"] == "ok" and result["account"] == "fixture_account" and cost == .02
    assert seen[0]["model"] == "opus" and seen[0]["only"] == ["fixture_account"]
    assert seen[0]["exclude"] == ["failed_account"] and seen[0]["no_sticky"] is True
    assert seen[0]["record"] is True
    assert (tmp_path / "request.response.json").exists()
    assert "never-send-this-key" not in (tmp_path / "request.pick.json").read_text()


def test_default_account_clears_stale_inherited_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/wrong")
    def execute(argv, **kwargs):
        assert "CLAUDE_CONFIG_DIR" not in kwargs["env"]
        return 0, envelope(), ""
    result, _ = ref.invoke(payload(), tmp_path, "request", Config(), selector=lambda **kw: pick(),
                           executor=execute, accounts=("fixture_account",))
    assert result["status"] == "ok"


@pytest.mark.parametrize("selection,status", [(pick(policy=False), "quota_unavailable"),
                                              (pick(fits=False), "quota_unavailable"),
                                              (pick(policy=None), "router_contract_error"),
                                              (pick(account="other"), "quota_unavailable")])
def test_unavailable_or_malformed_pick_never_starts_model(tmp_path, selection, status):
    def forbidden(*args, **kwargs):
        pytest.fail("rejected pick reached CLI")
    result, cost = ref.invoke(payload(), tmp_path, "request", Config(), selector=lambda **kw: selection,
                              executor=forbidden, accounts=("fixture_account",))
    assert result["status"] == status and cost == 0


def test_model_substitution_and_missing_cost_are_not_labels():
    result, cost = ref.parse_cli(envelope(model="claude-sonnet-4-6"), "", 0)
    assert result["status"] == "model_mismatch" and cost is None
    raw = json.loads(envelope())
    del raw["total_cost_usd"]
    assert ref.parse_cli(json.dumps(raw), "", 0)[0]["status"] == "invalid_response"
    raw = json.loads(envelope())
    raw["modelUsage"]["claude-opus-5-5"] = {}
    assert ref.parse_cli(json.dumps(raw), "", 0)[0]["status"] == "model_mismatch"


def test_timeout_retains_raw_and_cannot_auto_retry(tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture", 90, output=b"partial", stderr=b"diagnostic")
    result, cost = ref.invoke(payload(), tmp_path, "request", Config(), selector=lambda **kw: pick(),
                              executor=timeout, accounts=("fixture_account",))
    raw = json.loads((tmp_path / "request.response.json").read_text())
    assert raw["stdout"] == "partial" and raw["stderr"] == "diagnostic"
    assert result["status"] == "timeout_unknown" and result["retryable"] is False and cost is None


def test_reference_without_api_key_caches_and_repicks_only_quota_failures(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    excluded = []
    outcomes = [{"status": "oauth_refresh", "retryable": True, "account": "first"},
                {"status": "quota_exhausted", "retryable": True, "account": "first"},
                {"status": "ok", "retryable": False, "account": "second", "label": label("s:1"),
                 "served_model": models.REFERENCE}]
    def transport(payload, output, cache_key, cfg, *, exclude):
        excluded.append(exclude)
        result = outcomes.pop(0)
        return result, .01 if result["status"] == "ok" else None
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="reference", cfg=Config(),
                        limit=1, reference_transport=transport, sleep=lambda _: None)
    assert excluded == [(), (), ("first",)]
    assert next(iter(result["records"].values()))["account"] == "second"
    assert result["charged_or_reserved_usd"] == 1.01
    models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="reference", cfg=Config(),
               limit=1, reference_transport=transport, sleep=lambda _: None)
    assert len(excluded) == 3


def test_refused_router_stops_batch_without_spending(tmp_path):
    attempts = []
    def refuse(*args, **kwargs):
        attempts.append(1)
        return {"status": "quota_unavailable", "retryable": False}, 0.0
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="reference", cfg=Config(),
                        limit=3, reference_transport=refuse)
    assert len(attempts) == 1 and result["charged_or_reserved_usd"] == 0


def test_budget_overshoot_cannot_cache_as_success(tmp_path):
    ledger = models.Ledger(tmp_path, "pin")
    try:
        ledger.reserve("call", .5, {})
        with pytest.raises(DataBoundaryError, match="exceeds reserved"):
            ledger.finish("call", {"status": "ok"}, .6)
        assert ledger.data["calls"]["call"]["status"] == "budget_violation"
        assert ledger.reserved() == .6
    finally:
        ledger.close()


def test_key_based_reference_transport_is_not_a_fallback():
    with pytest.raises(DataBoundaryError, match="quota-selected CLI"):
        models.http_call("reference", payload(), "invented")


def test_invalid_rubric_stops_before_spending_on_more_cases(tmp_path):
    calls = []
    def invalid(*args, **kwargs):
        calls.append(1)
        return {"status": "ok", "label": {"label": "yes"}, "retryable": False}, .02
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="reference", cfg=Config(),
                        limit=3, reference_transport=invalid)
    assert len(calls) == 1 and result["charged_or_reserved_usd"] == .02
    assert next(iter(result["records"].values()))["status"] == "invalid_label"


def test_reference_cli_uses_selected_config_without_opening_runtime_store(tmp_path, monkeypatch):
    from self_improve.cli import main
    from self_improve import config
    from self_improve.evals import filter_cli
    from self_improve.store import Store
    root, data, manifest = frozen(tmp_path)
    cfg = replace(Config(), claude_path="/fixture/selected-claude")
    seen = []
    def load(path):
        seen.append(path)
        return cfg
    def forbidden(*args, **kwargs):
        pytest.fail("reference CLI opened runtime Store")
    def scripted(*args, **kwargs):
        assert kwargs["cfg"] is cfg
        return {"records": {"s:1": {"status": "ok", "label": label("s:1"),
                                     "served_model": models.REFERENCE, "account": "fixture_account",
                                     "transport": ref.TRANSPORT}}, "charged_or_reserved_usd": .02}
    monkeypatch.setattr(config, "load_config", load)
    monkeypatch.setattr(filter_cli, "run", scripted)
    monkeypatch.setattr(Store, "__init__", forbidden)
    out = tmp_path / "calls"
    out.mkdir()
    assert main(["--config", "/invented/reference.toml", "eval-filter", "reference",
                 "--reference-profile", "opus", "--dataset", str(root), "--output", str(out)]) == 0
    assert [str(p) for p in seen] == ["/invented/reference.toml"]
    row = json.loads((out / "reference-labels.json").read_text())["rows"][0]
    assert row["reference_account"] == "fixture_account" and row["reference_transport"] == ref.TRANSPORT


def test_deadline_kills_and_reaps_owned_process_group(monkeypatch):
    calls = []
    class Process:
        pid, returncode = 123456, None
        def communicate(self, *args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("fixture", 90)
            return b"retained partial", b"timeout diagnostic"
    def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return Process()
    killed = []
    monkeypatch.setattr(ref.subprocess, "Popen", spawn)
    monkeypatch.setattr(ref.os, "killpg", lambda *args: killed.append(args))
    with pytest.raises(subprocess.TimeoutExpired) as error:
        ref.execute(["fixture"], prompt="test", cwd="/invented", env={})
    assert len(calls) == 2 and killed == [(123456, ref.signal.SIGKILL)]
    assert error.value.stdout == b"retained partial"
