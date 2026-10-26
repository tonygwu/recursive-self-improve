"""Reference judgments through quota-selected, authenticated Claude CLI accounts.

The provider/model remain fixed across labels. Account selection, reserves and
quota policy belong to llm-quota-router; no credential files are read here.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

import quota_router
from quota_router.config import load_config as load_router_config

from ..config import Config
from ..data_boundary import DataBoundaryError
from ..llm import assert_router_conformance
from .filter_dataset import digest, redact
from .filter_models import REFERENCE, atomic_json

TRANSPORT = "quota-router-claude-cli/1"
TIMEOUT = 90
# Never let inherited API/proxy/model overrides replace the selected subscription.
_AUTH_OVERRIDES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def claude_accounts(env, cwd):
    cfg = load_router_config(env=env, cwd=cwd)
    return tuple(a.id for a in cfg.enabled_accounts() if a.provider == "claude")


def parse_cli(stdout: str, stderr: str, returncode: int):
    """Read actual model, usage and answer from the retained CLI envelope."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = {}
    if not isinstance(envelope, dict):
        return {"status": "invalid_response", "error": "CLI envelope is not an object", "retryable": False}, None
    if returncode or envelope.get("is_error"):
        # Failure text only; transcript strings inside successful JSON are not errors.
        failure = (stderr + "\n" + str(envelope.get("result", stdout))).lower()
        if "not logged in" in failure:
            status, retry = "oauth_refresh", True
        elif any(s in failure for s in ("usage limit", "rate limit", "session limit", "quota", "limit reached")):
            status, retry = "quota_exhausted", True
        else:
            status, retry = "cli_failed", False
        return {"status": status, "retryable": retry}, None
    if envelope.get("type") != "result" or envelope.get("subtype") != "success":
        return {"status": "invalid_response", "error": "CLI did not finish successfully", "retryable": False}, None
    usage, models = envelope.get("usage"), envelope.get("modelUsage")
    if not isinstance(usage, dict) or not isinstance(models, dict) or not models:
        return {"status": "invalid_response", "error": "missing model/usage telemetry", "retryable": False}, None
    served = [name for name, detail in models.items()
              if name == REFERENCE or isinstance(detail, dict) and detail.get("canonicalModel") == REFERENCE]
    # Haiku sidecars can appear in the CLI envelope; another main judge cannot.
    other_main = [name for name in models if name not in served and not name.startswith("claude-haiku-")]
    if len(served) != 1 or other_main:
        return {"status": "model_mismatch", "observed_models": sorted(models), "retryable": False}, None
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
        return {"status": "invalid_response", "error": "invalid usage telemetry", "retryable": False}, None
    cost = envelope.get("total_cost_usd")
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        return {"status": "invalid_response", "error": "missing CLI cost telemetry", "retryable": False}, None
    text = envelope.get("result")
    if not isinstance(text, str):
        return {"status": "invalid_response", "error": "missing label text", "retryable": False}, cost
    text = re.sub(r"\A\s*```(?:json)?\s*\n(.*?)\n```\s*\Z", r"\1", text, flags=re.S)
    try:
        label = json.loads(text)
        if not isinstance(label, dict):
            raise ValueError("label is not an object")
    except ValueError:
        return {"status": "invalid_response", "error": "label is not a JSON object", "retryable": False}, cost
    return {"status": "ok", "label": label, "served_model": served[0], "usage": usage,
            "auxiliary_models": sorted(set(models) - set(served)), "retryable": False,
            "billing_basis": "CLI-reported API-equivalent usage; not a subscription invoice"}, cost


def execute(argv, *, prompt, cwd, env, timeout=TIMEOUT):
    """Wall-clock deadline includes the process group; no orphaned CLI children."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(prompt.encode(), timeout=timeout)
    except BaseException as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        raise
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def invoke(payload, output: Path, cache_key: str, cfg: Config, *, exclude=(),
           selector=None, executor=None, accounts=None):
    selector = selector or quota_router.select_account
    executor = executor or execute
    assert_router_conformance()
    env = dict(os.environ)
    accounts = accounts if accounts is not None else claude_accounts(env, str(Path.cwd()))
    if "claude" not in cfg.allowed_providers or not accounts:
        return {"status": "quota_unavailable", "error": "no enabled Claude accounts", "retryable": False}, 0.0
    kwargs = {"model": "opus", "only": list(accounts), "env": env, "record": True,
              "timeout_ms": 10000}
    if exclude:
        kwargs.update(exclude=list(exclude), no_sticky=True)
    selection = selector(**kwargs)
    # Keep the router's audit, but not a credential accidentally put in exec.env.
    pick_artifact = redact(selection.to_dict())
    execution = pick_artifact.get("exec")
    if isinstance(execution, dict) and isinstance(execution.get("env"), dict):
        execution["env"] = {k: v if k in {"CLAUDE_CONFIG_DIR", "CODEX_HOME"} else "<omitted>"
                            for k, v in execution["env"].items()}
    atomic_json(output / f"{cache_key}.pick.json", pick_artifact)
    if (selection.contract_version != 1 or type(selection.fits) is not bool or
            type(selection.meets_policy) is not bool):
        return {"status": "router_contract_error", "retryable": False}, 0.0
    audit = {"account": selection.account, "cli_provider": selection.provider,
             "router_pick": f"{cache_key}.pick.json", "router_pick_sha": digest(pick_artifact)}
    if (not selection.fits or not selection.meets_policy or selection.provider != "claude" or
            selection.account not in accounts or selection.account in exclude):
        return {**audit, "status": "quota_unavailable", "retryable": False,
                "available_at": selection.available_at}, 0.0
    # Absence of CLAUDE_CONFIG_DIR means the default account, not the inherited one.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(selection.exec_env)
    for key in (*selection.exec_unset_env, *_AUTH_OVERRIDES):
        env.pop(key, None)
    env.update(CLAUDE_CODE_MAX_OUTPUT_TOKENS="1600", MAX_THINKING_TOKENS="0",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    if payload["model"] != REFERENCE:
        raise DataBoundaryError("reference model differs from pin")
    argv = [*selection.exec_argv_prefix, cfg.claude_path, "-p", "--model", REFERENCE,
            "--output-format", "json", "--safe-mode", "--tools", "", "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--no-session-persistence", "--no-chrome", "--max-turns", "1",
            "--max-budget-usd", "0.25", "--system-prompt", payload["system"]]
    stdout, stderr = "", ""
    returncode = None
    try:
        with tempfile.TemporaryDirectory(prefix="reference-", dir=output) as cwd:
            returncode, stdout, stderr = executor(argv, prompt=payload["messages"][0]["content"], cwd=cwd, env=env)
        result, cost = parse_cli(stdout, stderr, returncode)
    except subprocess.TimeoutExpired as exc:
        decode = lambda value: value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        stdout, stderr = decode(exc.stdout), decode(exc.stderr)
        result, cost = {"status": "timeout_unknown", "retryable": False}, None
    except OSError as exc:
        result, cost = {"status": "spawn_error", "error_type": type(exc).__name__, "retryable": False}, None
    raw = {"transport": TRANSPORT, "returncode": returncode, "stdout": stdout, "stderr": stderr}
    atomic_json(output / f"{cache_key}.response.json", raw)
    return {**result, **audit, "response_sha": digest(raw)}, cost
