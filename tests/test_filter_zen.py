"""Invented Zen receipts expose gateway mixing, paid fallback and quota failures."""
import io
import json
from urllib.error import HTTPError

import pytest

from self_improve.cli import main
from self_improve.data_boundary import DataBoundaryError
from self_improve.evals import filter_cli
from self_improve.evals import filter_models as models
from tests.test_filter_eval import dataset, frozen, jev_response, label


def free_response():
    return jev_response(model=models.JEV_FREE)


def test_gateway_changes_only_model_and_preserves_context_and_questions():
    window = {"anchor": "s:1", "events": [{"role": "human", "text": "No, wrong file"}]}
    direct = models.jev_request(window)
    free = models.jev_request(window, "zen-free")
    assert direct["model"] == models.JEV and free["model"] == models.JEV_FREE
    assert {k:v for k,v in direct.items() if k != "model"} == {k:v for k,v in free.items() if k != "model"}


def test_free_http_never_attaches_credentials_or_accepts_paid_model(monkeypatch):
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n): return free_response().encode()
    class Opener:
        def open(self, req, timeout):
            seen.append(req)
            assert req.full_url == "https://opencode.ai/zen/v1/systemone"
            assert req.get_header("Authorization") is None
            assert req.get_header("User-agent") == "self-improve-eval-filter/1"
            assert timeout == 30
            return Response()
    def build(handler):
        assert handler().redirect_request(None, None, 307, "redirect", {}, "https://elsewhere.invalid") is None
        return Opener()
    monkeypatch.setattr(models.request, "build_opener", build)
    payload = models.jev_request({}, "zen-free")
    assert models.http_call("jev", payload, "must-not-send", gateway="zen-free")[0] == 200
    with pytest.raises(DataBoundaryError, match="paid-model fallback"):
        models.http_call("jev", models.jev_request({}), "must-not-send", gateway="zen-free")
    assert len(seen) == 1


def test_free_response_keeps_gateway_alias_and_zero_cost():
    parsed, cost = models.parse_response("jev", free_response(), gateway="zen-free")
    assert parsed["served_model"] == "jev-1.13-free" and cost == 0
    for wrong in (models.JEV, "jev-1.13", "jev-latest"):
        with pytest.raises(ValueError, match="served model"):
            models.parse_response("jev", jev_response(model=wrong), gateway="zen-free")


def test_free_and_direct_cache_identity_and_files_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "invented-direct-key")
    seen = []
    def transport(provider, payload, key, **kwargs):
        seen.append((payload["model"], key))
        return 200, jev_response(model=payload["model"]), None
    args = (dataset(), {"sha256": "pin"}, tmp_path)
    direct = models.run(*args, provider="jev", limit=1, transport=transport)
    free = models.run(*args, provider="jev", limit=1, transport=transport, jev_gateway="zen-free")
    assert seen == [(models.JEV, "invented-direct-key"), (models.JEV_FREE, None)]
    assert free["charged_or_reserved_usd"] == direct["charged_or_reserved_usd"]
    assert (tmp_path / "jev-order-0.json").exists() and (tmp_path / "jev-zen-free-order-0.json").exists()
    assert free["identity_scope"] == "gateway_alias_backend_revision_unknown"
    models.run(*args, provider="jev", limit=1, transport=transport, jev_gateway="zen-free")
    assert len(seen) == 2
    parsed = models.verified_predictions(tmp_path / "jev-zen-free-order-0.json", dataset(), "pin")
    assert next(iter(parsed.values()))["gateway"] == "zen-free"


@pytest.mark.parametrize("status,attempts", [(401, 1), (403, 1), (404, 1), (429, 3), (503, 3)])
def test_failed_free_route_stops_batch_after_bounded_attempts(tmp_path, monkeypatch, status, attempts):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    seen = []
    def transport(provider, payload, key, **kwargs):
        seen.append(payload["model"])
        return status, "unavailable", None
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="jev", jev_gateway="zen-free",
                        limit=3, transport=transport, sleep=lambda _: None)
    assert seen == [models.JEV_FREE] * attempts and len(result["records"]) == 1
    assert next(iter(result["records"].values()))["status"] == f"http_{status}"
    assert result["charged_or_reserved_usd"] == pytest.approx(.003 * attempts)


def test_free_raw_provenance_cannot_be_relabelled_as_direct(tmp_path):
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="jev", limit=1,
                        jev_gateway="zen-free", transport=lambda *a, **kw: (200, free_response(), None))
    path = tmp_path / "jev-zen-free-order-0.json"
    del result["gateway"]
    models.atomic_json(path, result)
    with pytest.raises(DataBoundaryError, match="input differs"):
        models.verified_predictions(path, dataset(), "pin")


def test_free_wrong_model_stops_and_retains_raw_evidence(tmp_path):
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="jev", limit=3,
                        jev_gateway="zen-free", transport=lambda *a, **kw: (200, jev_response(), None))
    assert len(result["records"]) == 1
    record = next(iter(result["records"].values()))
    assert record["status"] == "invalid_response"
    assert (tmp_path / record["raw_response"]).is_file()


@pytest.mark.parametrize("gateway,model,scope", [
    ("zen-free", models.JEV_FREE, "gateway_alias_backend_revision_unknown"),
    ("typesafe-1.14", "jev-1.14.0", "backend_version"),
])
def test_cli_comparison_records_gateway_identity(tmp_path, monkeypatch, gateway, model, scope):
    monkeypatch.setenv("TYPESAFE_API_KEY", "invented")
    root, data, manifest = frozen(tmp_path)
    real_run = filter_cli.run
    def scripted(*args, **kwargs):
        return real_run(*args, **kwargs, transport=lambda *a, **kw: (200, jev_response(model=model), None))
    monkeypatch.setattr(filter_cli, "run", scripted)
    calls = tmp_path / "calls"
    assert main(["eval-filter", "jev", "--jev-gateway", gateway, "--dataset", str(root), "--output", str(calls)]) == 0
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"dataset_sha": manifest["sha256"],
                                 "rows": [label("s:1"), label("s:2", False), label("s:3")]}))
    out = tmp_path / "comparison.json"
    assert main(["eval-filter", "compare", "--dataset", str(root), "--labels", str(labels),
                 "--predictions", str(calls / f"jev-{gateway}-order-0.json"), "--output", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["paired_cases"] == 3
    assert report["jev_provenance"]["served_models"] == [model]
    assert report["jev_provenance"]["identity_scopes"] == [scope]
    assert ("gateway alias" in report["jev_provenance"]["note"]) == (gateway == "zen-free")


def test_paid_114_separate_cache_same_inputs_and_shared_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "invented")
    seen = []
    def transport(provider, payload, key, **kwargs):
        seen.append(payload)
        assert key == "invented"
        return 200, jev_response(model=payload["model"]), None
    args = (dataset(), {"sha256": "pin"}, tmp_path)
    old = models.run(*args, provider="jev", limit=1, transport=transport)
    old_bytes = (tmp_path / "jev-order-0.json").read_bytes()
    paid = models.run(*args, provider="jev", limit=1, transport=transport, jev_gateway="typesafe-1.14")
    assert [p["model"] for p in seen] == [models.JEV, "jev-1.14.0"]
    assert {k:v for k,v in seen[0].items() if k != "model"} == {k:v for k,v in seen[1].items() if k != "model"}
    assert paid["charged_or_reserved_usd"] == pytest.approx(2 * old["charged_or_reserved_usd"])
    models.run(*args, provider="jev", limit=1, transport=transport, jev_gateway="typesafe-1.14")
    assert len(seen) == 2 and (tmp_path / "jev-order-0.json").read_bytes() == old_bytes
    path = tmp_path / paid["result_file"]
    assert next(iter(models.verified_predictions(path, dataset(), "pin").values()))["served_model"] == "jev-1.14.0"
    paid["authentication"] = "anonymous"
    models.atomic_json(path, paid)
    with pytest.raises(DataBoundaryError, match="metadata differs"):
        models.verified_predictions(path, dataset(), "pin")


def test_paid_114_requires_key_and_rejects_substitution(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(DataBoundaryError, match="TYPESAFE_API_KEY"):
        models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="jev", jev_gateway="typesafe-1.14")
    monkeypatch.setenv("TYPESAFE_API_KEY", "invented")
    result = models.run(dataset(), {"sha256": "pin"}, tmp_path, provider="jev", limit=3,
                        jev_gateway="typesafe-1.14", transport=lambda *a, **kw: (200, jev_response(), None))
    assert len(result["records"]) == 1
    record = next(iter(result["records"].values()))
    assert record["status"] == "invalid_response" and result["charged_or_reserved_usd"] == .003
    assert (tmp_path / record["raw_response"]).is_file()


def test_paid_114_http_authentication_and_exact_model(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n): return jev_response(model="jev-1.14.0").encode()
    class Opener:
        def open(self, req, timeout):
            assert req.full_url == "https://api.typesafe.ai/v1/systemone"
            assert req.get_header("Authorization") == "Bearer invented"
            assert json.loads(req.data)["model"] == "jev-1.14.0" and timeout == 30
            return Response()
    monkeypatch.setattr(models.request, "build_opener", lambda *a: Opener())
    payload = models.jev_request({}, "typesafe-1.14")
    status, raw, _ = models.http_call("jev", payload, "invented", gateway="typesafe-1.14")
    parsed, cost = models.parse_response("jev", raw, gateway="typesafe-1.14")
    assert status == 200 and cost == pytest.approx(parsed["usage"]["input_tokens"] * .042 / 1e6)
    with pytest.raises(DataBoundaryError, match="model differs"):
        models.http_call("jev", models.jev_request({}), "invented", gateway="typesafe-1.14")


def test_http_error_retains_retry_after_for_free_route(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError("https://opencode.ai/zen/v1/systemone", 429, "limited", {"Retry-After": "31"}, io.BytesIO(b"quota"))
    monkeypatch.setattr(models.request, "build_opener", lambda *args: Opener())
    assert models.http_call("jev", models.jev_request({}, "zen-free"), None, gateway="zen-free") == (429, "quota", "31")
