"""Tests for the budgeted, audited, quota-routed LLM runner.

Scripted provider responses replace subprocess.run, and scripted account
selections replace select_account. The payloads in tests/fixtures/llm/ are
invented; their README records the fixture contract. Selection.from_payload
constructs real quota_router.Selection objects from those invented payloads.
Separate library-contract tests inject an oracle and temporary configuration.

An unexpected binary or exhausted response queue raises AssertionError."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from quota_router import Selection, select_account as real_select_account
from quota_router.cli import Deps as QuotaDeps
from quota_router.types import AccountSnapshot, Window, unreadable_reason

from self_improve import llm as llm_mod
from self_improve.config import Config
from self_improve.llm import REPO_ROOT, BudgetExhausted, LLMRunner
from self_improve.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
PICK_CLAUDE = (FIXTURES / "quotapick_pick_claude.json").read_bytes()
PICK_CODEX = (FIXTURES / "quotapick_pick_codex.json").read_bytes()
PICK_POLICY_WAIT = (FIXTURES / "quotapick_pick_policy_wait.json").read_bytes()
CLAUDE_ENVELOPE = (FIXTURES / "claude_envelope_ok.json").read_bytes()
CODEX_ENVELOPE = (FIXTURES / "codex_envelope_ok.jsonl").read_bytes()
CODEX_ROLLOUT = (FIXTURES / "codex_rollout_snippet.jsonl").read_bytes()

# Matches thread.started in codex_envelope_ok.jsonl and the rollout snippet.
CODEX_THREAD_ID = "00000000-0000-7000-8000-000000000000"
PROMPT = "Reply with exactly: OK"

CLAUDE_BIN = "/fake/bin/claude"
CODEX_BIN = "/fake/bin/codex"


class FakeSubprocess:
    """Scripted stand-in for subprocess.run, dispatched on argv[0].

    Responses are FIFO queues per binary. Each queued item is either a
    ``(returncode, stdout_bytes, stderr_bytes)`` tuple or an exception
    instance to raise from the call. Every invocation (argv + kwargs) is
    recorded for later assertions. Only provider CLIs live here — the pick
    is a library call, faked by :class:`FakeSelectAccount`; any attempt to
    spawn a quotapick binary would land in the unexpected-binary assertion.
    """

    def __init__(self) -> None:
        self.queues: dict[str, list] = {CLAUDE_BIN: [], CODEX_BIN: []}
        self.calls: list[tuple[list[str], dict]] = []

    def expect_claude(self, *responses) -> None:
        self.queues[CLAUDE_BIN].extend(responses)

    def expect_codex(self, *responses) -> None:
        self.queues[CODEX_BIN].extend(responses)

    def calls_for(self, binary: str) -> list[tuple[list[str], dict]]:
        return [(argv, kw) for argv, kw in self.calls if argv[0] == binary]

    def provider_calls(self) -> list[tuple[list[str], dict]]:
        return [
            (argv, kw)
            for argv, kw in self.calls
            if argv[0] in (CLAUDE_BIN, CODEX_BIN)
        ]

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        binary = argv[0]
        # A sandboxed eval call is `npx -y <srt-pkg> --settings <path> <real argv>`.
        # Look past that prefix for the provider binary, but only when the
        # prefix is actually there, so an unexpected binary still fails loudly.
        if any("sandbox-runtime" in a for a in argv[:3]):
            binary = next((a for a in argv if a in self.queues), binary)
        if binary not in self.queues:
            raise AssertionError(f"unexpected subprocess binary {binary!r}: {argv}")
        queue = self.queues[binary]
        if not queue:
            raise AssertionError(f"no scripted response left for {binary!r}: {argv}")
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


class FakeSelectAccount:
    """Scripted stand-in for quota_router.select_account (keyword-only).

    Responses are a FIFO queue; each item is a real :class:`Selection`
    (build one via :func:`selection`) or an exception instance to raise.
    Every call's kwargs are recorded for assertions. Positional arguments
    raise TypeError exactly like the real keyword-only signature would.
    """

    def __init__(self) -> None:
        self.queue: list = []
        self.calls: list[dict] = []

    def expect(self, *responses) -> None:
        self.queue.extend(responses)

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self.queue:
            raise AssertionError(
                f"no scripted Selection left for select_account(**{kwargs})"
            )
        response = self.queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def selection(base: bytes = PICK_CLAUDE, **decision_overrides) -> Selection:
    """Build a real quota_router.Selection from an invented pick payload."""
    data = json.loads(base)
    data["decision"].update(decision_overrides)
    return Selection.from_payload(data)


def selection_without(base: bytes, *decision_keys: str) -> Selection:
    """A Selection whose decision LACKS the named keys (from_payload -> None)."""
    data = json.loads(base)
    for key in decision_keys:
        del data["decision"][key]
    return Selection.from_payload(data)


def selection_degraded(
    degraded: list[dict], base: bytes = PICK_CLAUDE, **decision_overrides
) -> Selection:
    """A Selection carrying explicit ``degraded`` rows (fixtures ship none)."""
    data = json.loads(base)
    data["decision"].update(decision_overrides)
    data["degraded"] = list(degraded)
    return Selection.from_payload(data)


# The unreadable-account reason string is taken from the LIBRARY, not retyped,
# so this test file cannot drift from the wording llm.py keys off. Shape is
# exactly what providers/claude_oauth reports for an expired access token:
# no windows, available=False, and a note naming the cause.
def dark_snapshot(account: str, note: str = "access token expired") -> AccountSnapshot:
    return AccountSnapshot(
        id=account, windows=(), source="live", confidence=0.0,
        available=False, note=note,
    )


UNREADABLE_REASON = unreadable_reason(dark_snapshot("claude"))
assert UNREADABLE_REASON is not None and UNREADABLE_REASON.startswith("unreadable:")


def unreadable_degraded(*accounts: str) -> list[dict]:
    """``degraded`` rows exactly as quota_router's _prepare appends them."""
    return [
        {"account": a, "reason": unreadable_reason(dark_snapshot(a))} for a in accounts
    ]


def pick_json_bytes(sel: Selection) -> bytes:
    """The exact pickN.json bytes llm.py retains (CLI _dump_json format)."""
    return (
        json.dumps(sel.to_dict(), indent=2, sort_keys=False, default=str) + "\n"
    ).encode("utf-8")


def pick_kwargs(model: str = "sonnet") -> dict:
    """The kwargs a plain (non-re-pick) _pick hands to select_account."""
    return {"model": model, "env": dict(os.environ)}


def make_runner(tmp_path, monkeypatch, **cfg_overrides) -> SimpleNamespace:
    codex_sessions = tmp_path / "codex-sessions"
    codex_sessions.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        claude_path=CLAUDE_BIN,
        codex_path=CODEX_BIN,
        codex_sessions_dir=str(codex_sessions),
        state_dir=str(tmp_path / "state"),
        **cfg_overrides,
    )
    fake = FakeSubprocess()
    monkeypatch.setattr(llm_mod.subprocess, "run", fake)
    pick = FakeSelectAccount()
    monkeypatch.setattr(llm_mod, "select_account", pick)
    store = Store(tmp_path / "state" / "test.db")
    raw_dir = tmp_path / "raw"
    runner = LLMRunner(cfg, store, run_id="run-test", raw_dir=raw_dir)
    return SimpleNamespace(
        runner=runner, fake=fake, pick=pick, store=store, cfg=cfg, raw_dir=raw_dir
    )


def write_codex_rollout(h, content: bytes = CODEX_ROLLOUT, thread_id: str = CODEX_THREAD_ID) -> Path:
    """Plant a rollout file where _codex_reported_model's glob will find it."""
    day_dir = Path(h.cfg.codex_sessions_dir) / "2026" / "08" / "15"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-15T06-15-47-{thread_id}.jsonl"
    path.write_bytes(content)
    return path


def one_llm_row(store: Store) -> dict:
    rows = store.query("SELECT * FROM llm_calls")
    assert len(rows) == 1, f"expected exactly one llm_calls row, got {rows}"
    return rows[0]


def claude_envelope(result_text: str, is_error: bool = False) -> bytes:
    """Set assistant text and error status in the invented envelope."""
    data = json.loads(CLAUDE_ENVELOPE)
    data["result"] = result_text
    data["is_error"] = is_error
    return json.dumps(data).encode("utf-8")


def run_claude_ok(h, stage: str = "mine", expect_json: bool = False, **call_kwargs):
    h.pick.expect(selection())
    h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
    return h.runner.call(stage, "sonnet", PROMPT, expect_json=expect_json, **call_kwargs)


# ---------------------------------------------------------------------------
# Construction fails loud on unmapped providers / model classes
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_unknown_provider_raises(self, tmp_path, monkeypatch):
        with pytest.raises(ValueError, match="no PROVIDER_MODEL_MAP entry.*'gemini'"):
            make_runner(tmp_path, monkeypatch, allowed_providers=("claude", "gemini"))

    def test_unmapped_model_class_raises(self, tmp_path, monkeypatch):
        with pytest.raises(ValueError, match="no model mapping for class 'haiku'"):
            make_runner(tmp_path, monkeypatch, cheap_model_class="haiku")

    def test_unknown_model_class_at_call_raises_without_subprocess(
        self, tmp_path, monkeypatch
    ):
        h = make_runner(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="unknown model_class 'haiku'"):
            h.runner.call("mine", "haiku", PROMPT, expect_json=False)
        assert h.fake.calls == []
        assert h.pick.calls == []
        assert h.store.query("SELECT * FROM llm_calls") == []


# ---------------------------------------------------------------------------
# Happy path: claude
# ---------------------------------------------------------------------------


class TestClaudeHappyPath:
    def test_result_fields(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        res = run_claude_ok(h)
        assert res.ok is True
        assert res.text == "OK"
        assert res.parsed is None
        assert res.outcome == "ok"
        assert res.provider == "claude"
        assert res.account == "claude_b"
        # modelUsage has TWO keys (haiku sidecar); identity must match the
        # requested sonnet key, not the sidecar.
        assert res.model_reported == "claude-sonnet-5"
        assert res.error == ""

    def test_llm_calls_row(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h, stage="mine")
        row = one_llm_row(h.store)
        assert row["run_id"] == "run-test"
        assert row["stage"] == "mine"
        assert row["provider"] == "claude"
        assert row["account"] == "claude_b"
        assert row["model_requested"] == "sonnet"  # claude token for class sonnet
        assert row["model_reported"] == "claude-sonnet-5"
        assert row["prompt_sha"] == hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
        # usage.input_tokens/output_tokens verbatim (cache reads excluded)
        assert row["tokens_in"] == 2
        assert row["tokens_out"] == 4
        assert row["duration_ms"] >= 0
        assert row["outcome"] == "ok"
        assert row["error"] == ""
        assert row["created_at"].endswith("Z")

    def test_raw_bytes_retained(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h)
        call_id = one_llm_row(h.store)["id"]
        # pick1.json is the FULL Selection.to_dict() payload in the CLI's
        # own _dump_json format — the complete decision, retained for audit.
        assert (h.raw_dir / f"{call_id}.pick1.json").read_bytes() == pick_json_bytes(
            selection()
        )
        assert (h.raw_dir / f"{call_id}.a1.claude.stdout").read_bytes() == CLAUDE_ENVELOPE
        assert (h.raw_dir / f"{call_id}.a1.claude.stderr").read_bytes() == b""

    def test_subprocess_contract(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h)
        # The pick is a library call, not a subprocess: model class plus the
        # real process env (CLI-parity config discovery), nothing else — in
        # particular no exclude/no_sticky on a first pick.
        assert h.pick.calls == [pick_kwargs()]
        (argv, kw) = h.fake.calls_for(CLAUDE_BIN)[0]
        assert argv == [CLAUDE_BIN, "-p", "--model", "sonnet", "--output-format", "json"]
        assert kw["input"] == PROMPT.encode("utf-8")  # prompt on stdin
        assert kw["timeout"] == h.cfg.llm_timeout_seconds
        # The pick's exec_env overlay (CLAUDE_CONFIG_DIR for account B) is applied.
        assert kw["env"]["CLAUDE_CONFIG_DIR"] == "/Users/USER/.claude-b"

    def test_stats_after_one_success(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h)
        stats = h.runner.stats()
        assert stats["attempted"] == 1
        assert stats["succeeded"] == 1
        assert stats["failed"] == 0
        assert stats["by_outcome"] == {"ok": 1}
        assert stats["calls_made"] == {"cheap": 1, "strong": 0, "gate": 0}
        assert stats["refused"] == {"cheap": 0, "strong": 0, "gate": 0}


# ---------------------------------------------------------------------------
# Happy path: codex (identity read from the rollout file, not the stream)
# ---------------------------------------------------------------------------


class TestCodexHappyPath:
    def test_result_and_row(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        write_codex_rollout(h)
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        res = h.runner.call("propose", "sonnet", PROMPT, expect_json=False)
        assert res.ok is True
        assert res.outcome == "ok"
        assert res.text == "OK"
        assert res.provider == "codex"
        assert res.account == "codex"
        assert res.model_reported == "gpt-5.6-terra"  # from rollout turn_context
        row = one_llm_row(h.store)
        assert row["stage"] == "propose"
        assert row["model_requested"] == "gpt-5.6-terra"
        assert row["model_reported"] == "gpt-5.6-terra"
        assert row["prompt_sha"] == hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
        assert row["tokens_in"] == 1200  # invented turn.completed input-token count
        assert row["tokens_out"] == 5
        assert row["outcome"] == "ok"
        call_id = row["id"]
        assert (h.raw_dir / f"{call_id}.pick1.json").read_bytes() == pick_json_bytes(
            selection(PICK_CODEX)
        )
        assert (h.raw_dir / f"{call_id}.a1.codex.stdout").read_bytes() == CODEX_ENVELOPE

    def test_subprocess_contract(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        write_codex_rollout(h)
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        h.runner.call("propose", "sonnet", PROMPT, expect_json=False)
        (argv, kw) = h.fake.calls_for(CODEX_BIN)[0]
        assert argv == [
            CODEX_BIN,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-m",
            "gpt-5.6-terra",
            PROMPT,  # prompt on argv, not stdin
        ]
        assert kw["stdin"] is subprocess.DEVNULL  # codex hangs on live stdin
        assert "input" not in kw
        # PICK_CODEX's exec_env is {} and that is CORRECT (the default
        # account is selected by the ABSENCE of CLAUDE_CONFIG_DIR): the
        # empty overlay merges to the plain process env — it is never
        # required to be non-empty.
        assert selection(PICK_CODEX).exec_env == {}
        assert kw["env"] == dict(os.environ)

    def test_missing_rollout_is_model_mismatch(self, tmp_path, monkeypatch):
        # Identity is never silently assumed: no rollout file -> model_mismatch.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        res = h.runner.call("propose", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "model_mismatch"
        assert res.model_reported == ""
        assert "no rollout file" in res.error
        assert one_llm_row(h.store)["outcome"] == "model_mismatch"


# ---------------------------------------------------------------------------
# model_mismatch
# ---------------------------------------------------------------------------


class TestModelMismatch:
    def test_claude_envelope_lacks_requested_token(self, tmp_path, monkeypatch):
        # Request the strong class (opus); the fixture envelope reports only
        # sonnet + a haiku sidecar -> identity fails, nothing is accepted.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        res = h.runner.call("propose", "opus", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "model_mismatch"
        assert res.model_reported == "claude-haiku-4-5-20251001,claude-sonnet-5"
        assert "'opus'" in res.error
        row = one_llm_row(h.store)
        assert row["outcome"] == "model_mismatch"
        assert row["model_requested"] == "opus"
        assert row["model_reported"] == "claude-haiku-4-5-20251001,claude-sonnet-5"

    def test_codex_rollout_reports_different_model(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        write_codex_rollout(h, content=CODEX_ROLLOUT.replace(b"gpt-5.6-terra", b"gpt-4.1-mini"))
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        res = h.runner.call("propose", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "model_mismatch"
        assert res.model_reported == "gpt-4.1-mini"
        assert "'gpt-5.6-terra'" in res.error
        assert one_llm_row(h.store)["outcome"] == "model_mismatch"


# ---------------------------------------------------------------------------
# OAuth transient retry
# ---------------------------------------------------------------------------


class TestOauthRetry:
    def test_retry_once_then_success(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude(
            (1, b"", b"Not logged in"),
            (0, CLAUDE_ENVELOPE, b""),
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is True
        assert res.outcome == "oauth_transient_retried"
        assert res.provider_attempts == 2
        assert res.text == "OK"
        # Exactly two provider invocations, one pick (no re-pick).
        assert len(h.fake.calls_for(CLAUDE_BIN)) == 2
        assert len(h.pick.calls) == 1
        row = one_llm_row(h.store)
        assert row["outcome"] == "oauth_transient_retried"
        call_id = row["id"]
        assert (h.raw_dir / f"{call_id}.a1.claude.stderr").read_bytes() == b"Not logged in"
        assert (h.raw_dir / f"{call_id}.a2.claude.stdout").read_bytes() == CLAUDE_ENVELOPE
        # oauth_transient_retried counts as a success in stats.
        stats = h.runner.stats()
        assert stats["succeeded"] == 1
        assert stats["failed"] == 0
        assert stats["by_outcome"] == {"oauth_transient_retried": 1}

    def test_oauth_persisting_after_retry_is_other(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude(
            (1, b"", b"Not logged in"),
            (1, b"", b"Not logged in"),
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "other"
        assert "oauth failure persisted after retry" in res.error
        assert res.provider_attempts == 2
        # Exactly one retry — never a third attempt.
        assert len(h.fake.calls_for(CLAUDE_BIN)) == 2
        assert one_llm_row(h.store)["outcome"] == "other"


# ---------------------------------------------------------------------------
# Quota re-pick
# ---------------------------------------------------------------------------


class TestQuotaRepick:
    # Invented failure envelope with session-limit wording and reset details.
    # No other quota marker appears, so these tests isolate "session limit".
    SESSION_LIMIT = (
        '{"is_error":true,"num_turns":1,"stop_reason":"stop_sequence",'
        '"terminal_reason":"api_error","subtype":"success","type":"result",'
        '"result":"You\'ve hit your session limit \u00b7 resets 7:15am '
        '(UTC)"}'
    ).encode()

    def test_session_limit_text_is_recognised_as_quota(self):
        """Recognize session-limit wording in the invented failure envelope."""
        from self_improve.llm import _looks_quota

        assert _looks_quota(self.SESSION_LIMIT.decode()) is True

    def test_session_limit_triggers_the_repick(self, tmp_path, monkeypatch):
        """An exhausted usage window must trigger the actual account re-pick path."""
        h = make_runner(tmp_path, monkeypatch)
        write_codex_rollout(h)
        h.pick.expect(selection(), selection(PICK_CODEX))
        h.fake.expect_claude((1, self.SESSION_LIMIT, b""))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is True
        assert res.provider == "codex"
        assert res.error == "repicked_from=claude_b"
        assert h.pick.calls == [
            pick_kwargs(),
            {**pick_kwargs(), "exclude": ["claude_b"], "no_sticky": True},
        ]

    def test_session_limit_with_no_second_account_is_quota_exhausted(
        self, tmp_path, monkeypatch
    ):
        """And when there is nowhere to fail over to, it is named, not `other`."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(), selection(PICK_CODEX, fits=False))
        h.fake.expect_claude((1, self.SESSION_LIMIT, b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert "re-pick has no capacity" in res.error
        assert res.provider_attempts == 1
        assert one_llm_row(h.store)["outcome"] == "quota_exhausted"

    def test_repick_to_other_provider_success(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        write_codex_rollout(h)
        h.pick.expect(selection(), selection(PICK_CODEX))
        h.fake.expect_claude((1, b"", b"Claude AI usage limit reached|1755237600"))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is True
        assert res.outcome == "ok"
        assert res.provider == "codex"
        assert res.account == "codex"
        # The re-pick is reported, not silent.
        assert res.error == "repicked_from=claude_b"
        # Second pick excludes the failed account (as a LIST — the library
        # iterates the sequence) and disables stickiness.
        assert h.pick.calls == [
            pick_kwargs(),
            {**pick_kwargs(), "exclude": ["claude_b"], "no_sticky": True},
        ]
        row = one_llm_row(h.store)
        assert row["outcome"] == "ok"
        assert row["provider"] == "codex"
        assert row["error"] == "repicked_from=claude_b"
        call_id = row["id"]
        assert (h.raw_dir / f"{call_id}.pick1.json").read_bytes() == pick_json_bytes(
            selection()
        )
        assert (h.raw_dir / f"{call_id}.pick2.json").read_bytes() == pick_json_bytes(
            selection(PICK_CODEX)
        )
        assert (h.raw_dir / f"{call_id}.a2.codex.stdout").read_bytes() == CODEX_ENVELOPE

    def test_repick_without_capacity_is_quota_exhausted(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(), selection(PICK_CODEX, fits=False))
        h.fake.expect_claude((1, b"", b"Claude AI usage limit reached|1755237600"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert "re-pick has no capacity" in res.error
        # Attribution stays on the account that actually failed.
        assert res.provider == "claude"
        assert res.account == "claude_b"
        assert len(h.fake.calls_for(CLAUDE_BIN)) == 1
        assert h.fake.calls_for(CODEX_BIN) == []


# ---------------------------------------------------------------------------
# quota_exhausted from the pick itself (fits: false)
# ---------------------------------------------------------------------------


class TestQuotaExhaustedPick:
    def test_fits_false_spawns_no_provider(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(fits=False))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert res.provider == "claude"
        assert res.account == "claude_b"
        assert "fits=false" in res.error
        # No provider subprocess was ever spawned; exactly one pick was made.
        assert h.fake.calls == []
        assert len(h.pick.calls) == 1
        row = one_llm_row(h.store)
        assert row["outcome"] == "quota_exhausted"
        assert row["error"] == "quotapick: fits=false, no provider invoked"

    def test_degraded_no_winner_pick_is_quota_exhausted(self, tmp_path, monkeypatch):
        # A degraded routing failure comes back as a Selection (the library
        # never raises for it): account/provider None, fits False. Under the
        # CLI this was a null-decision JSON flowing to quota_exhausted; the
        # library path lands in the same outcome with blank attribution.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(
            selection(account=None, provider=None, fits=False, meets_policy=False)
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert res.provider == ""
        assert res.account == ""
        assert h.fake.calls == []
        assert one_llm_row(h.store)["outcome"] == "quota_exhausted"


# ---------------------------------------------------------------------------
# Unreadable accounts (quota_router >= a600845)
#
# An account whose OAuth access token has expired is reported as UNREADABLE
# rather than silently dropped from routing. Its remaining quota is UNKNOWN --
# not zero -- so it reaches us as fits=false with `remaining: null` rows and a
# "unreadable:" reason in `degraded`. "Unknown" and "exhausted" demand opposite
# responses (a login vs a wait), and a run report that says only
# "fits=false, no provider invoked" sends the operator to wait out a reset that
# will never come, because a dark account has no window to reset.
# ---------------------------------------------------------------------------


class TestUnreadableAccounts:
    def test_dark_fleet_error_names_the_unreadable_accounts(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(
            selection_degraded(
                unreadable_degraded("claude", "codex"),
                account=None,
                provider=None,
                fits=False,
                meets_policy=False,
            )
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert h.fake.calls == []  # nothing was spawned against a dead account
        error = one_llm_row(h.store)["error"]
        # The original quota_exhausted text is kept ...
        assert "quotapick: fits=false, no provider invoked" in error
        # ... and BOTH dark accounts are named, with the library's own verbatim
        # cause, so the row says the quota is unknown rather than spent.
        assert "claude" in error and "codex" in error
        assert error.count(UNREADABLE_REASON) == 2
        assert res.error == error

    def test_unreadable_diagnosis_survives_the_repick_path(self, tmp_path, monkeypatch):
        # A quota failure on the winner triggers one re-pick; if what is left is
        # dark, the recorded error must still say so rather than report bare
        # "no capacity".
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(
            selection(),
            selection_degraded(
                unreadable_degraded("claude_b"),
                account=None,
                provider=None,
                fits=False,
                meets_policy=False,
            ),
        )
        h.fake.expect_claude((1, b"", b"Claude AI usage limit reached"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.outcome == "quota_exhausted"
        error = one_llm_row(h.store)["error"]
        assert "re-pick has no capacity" in error
        assert UNREADABLE_REASON in error

    def test_merely_stale_degraded_row_is_not_a_credentials_diagnosis(
        self, tmp_path, monkeypatch
    ):
        # `degraded` is ALSO set when a snapshot is merely stale, which says
        # nothing about readability. Conflating the two is the same mistake that
        # made meets_policy read Decision.degraded; the message must stay clean.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(
            selection_degraded(
                [{"account": "claude_b", "reason": "snapshot is 42m old (max 30m)"}],
                fits=False,
            )
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.outcome == "quota_exhausted"
        assert one_llm_row(h.store)["error"] == (
            "quotapick: fits=false, no provider invoked"
        )

    def test_no_degraded_rows_leaves_the_message_byte_identical(
        self, tmp_path, monkeypatch
    ):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(fits=False))  # fixtures ship `degraded: []`
        h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert one_llm_row(h.store)["error"] == (
            "quotapick: fits=false, no provider invoked"
        )


class TestUnreadableAccountLibraryContract:
    """Verify the library guarantees behind unreadable-account handling.

    These tests call select_account with an injected oracle and record=False.
    The supplied environment uses a temporary HOME to avoid personal config and
    sticky selections. Unknown account capacity must stay distinct from zero."""

    NOW = 1_800_000_000.0

    def _real_pick(self, tmp_path, snapshots):
        def wrapper(**kwargs):
            # llm.py must still pass the full environment explicitly: the
            # library defaults `env` to {} while the CLI reads os.environ.
            assert kwargs["env"] == dict(os.environ)
            return real_select_account(
                model=kwargs["model"],
                env={"HOME": str(tmp_path / "home")},
                now_s=self.NOW,
                record=False,
                no_sticky=True,
                deps=QuotaDeps(load_snapshots=lambda **kw: list(snapshots)),
            )

        return wrapper

    def _healthy(self, account: str, used: float) -> AccountSnapshot:
        return AccountSnapshot(
            id=account,
            windows=(
                Window(key="5h", used_fraction=used, length_s=18000.0,
                       resets_at_s=self.NOW + 3600.0, observed_at_s=self.NOW),
                Window(key="7d", used_fraction=used, length_s=604800.0,
                       resets_at_s=self.NOW + 200000.0, observed_at_s=self.NOW),
            ),
            source="live", confidence=1.0, available=True,
        )

    def test_unreadable_account_is_never_chosen_over_a_healthy_one(
        self, tmp_path, monkeypatch
    ):
        h = make_runner(tmp_path, monkeypatch)
        snapshots = [dark_snapshot("claude"), self._healthy("claude_b", 0.10)]
        monkeypatch.setattr(
            llm_mod, "select_account", self._real_pick(tmp_path, snapshots)
        )
        h.fake.expect_claude((0, claude_envelope("OK"), b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is True
        assert res.account == "claude_b"  # never the dark account
        # The dark account is reported with remaining NULL -- unknown, not 0.0.
        call_id = one_llm_row(h.store)["id"]
        payload = json.loads((h.raw_dir / f"{call_id}.pick1.json").read_text())
        dark_rows = [r for r in payload["excluded"] if r["account"] == "claude"]
        assert len(dark_rows) == 1
        assert dark_rows[0]["remaining"] is None
        assert dark_rows[0]["reason"].startswith("unreadable:")
        assert {d["account"] for d in payload["degraded"]} == {"claude"}

    def test_wholly_dark_fleet_never_yields_a_winner(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        snapshots = [dark_snapshot("claude"), dark_snapshot("codex")]
        monkeypatch.setattr(
            llm_mod, "select_account", self._real_pick(tmp_path, snapshots)
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert h.fake.calls == []  # no provider spawned against a dead fleet
        # A dark fleet must NOT be describable as merely out of quota.
        assert UNREADABLE_REASON in one_llm_row(h.store)["error"]


# ---------------------------------------------------------------------------
# timeout / spawn_error
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_provider_timeout(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude(
            subprocess.TimeoutExpired(
                cmd=[CLAUDE_BIN], timeout=600, output=b"partial-out", stderr=b"partial-err"
            )
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "timeout"
        assert f"timed out after {h.cfg.llm_timeout_seconds}s" in res.error
        row = one_llm_row(h.store)
        assert row["outcome"] == "timeout"
        # Partial output captured by TimeoutExpired is still retained.
        call_id = row["id"]
        assert (h.raw_dir / f"{call_id}.a1.claude.stdout").read_bytes() == b"partial-out"
        assert (h.raw_dir / f"{call_id}.a1.claude.stderr").read_bytes() == b"partial-err"


class TestSpawnError:
    def test_provider_oserror(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude(OSError("exec format error"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "spawn_error"
        assert "claude" in res.error
        assert "OSError" in res.error
        row = one_llm_row(h.store)
        assert row["outcome"] == "spawn_error"
        # Nothing ran, so no attempt bytes exist; the pick is still retained.
        call_id = row["id"]
        assert (h.raw_dir / f"{call_id}.pick1.json").exists()
        assert not (h.raw_dir / f"{call_id}.a1.claude.stdout").exists()

    def test_select_account_exception_is_spawn_error(self, tmp_path, monkeypatch):
        # The library boundary maps exactly like the old subprocess boundary:
        # anything select_account raises (ConfigError, bugs — it never raises
        # for mere routing failure) becomes spawn_error, no provider spawned.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(OSError("No such file or directory"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "spawn_error"
        assert res.error.startswith("quotapick:")
        assert "OSError" in res.error
        assert res.provider == ""
        assert res.account == ""
        assert h.fake.calls == []
        row = one_llm_row(h.store)
        assert row["outcome"] == "spawn_error"
        # No provider was picked; model_requested falls back to the class.
        assert row["model_requested"] == "sonnet"
        assert row["provider"] == ""
        # The library raised before any payload existed, so nothing to retain.
        assert list(h.raw_dir.glob("*.pick*.json")) == []


# ---------------------------------------------------------------------------
# parse_error — envelope and expect_json, raw retained, nothing guessed
# ---------------------------------------------------------------------------


class TestParseError:
    def test_expect_json_with_non_json_text(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        res = run_claude_ok(h, expect_json=True)  # assistant text is "OK"
        assert res.ok is False
        assert res.outcome == "parse_error"
        assert res.parsed is None
        assert res.text == "OK"  # raw assistant text retained, never guessed
        assert res.error.startswith("expect_json:")
        row = one_llm_row(h.store)
        assert row["outcome"] == "parse_error"
        # Raw envelope bytes are still on disk for the post-mortem.
        assert (h.raw_dir / f"{row['id']}.a1.claude.stdout").read_bytes() == CLAUDE_ENVELOPE

    def test_expect_json_fenced_object_parses(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, claude_envelope('```json\n{"verdict": "pass"}\n```'), b""))
        res = h.runner.call("grade", "sonnet", PROMPT, expect_json=True)
        assert res.ok is True
        assert res.outcome == "ok"
        assert res.parsed == {"verdict": "pass"}

    def test_expect_json_top_level_array_is_parse_error(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, claude_envelope("[1, 2]"), b""))
        res = h.runner.call("grade", "sonnet", PROMPT, expect_json=True)
        assert res.ok is False
        assert res.outcome == "parse_error"
        assert "object" in res.error

    def test_claude_stdout_not_json_is_parse_error(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, b"this is not a JSON envelope", b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "parse_error"
        assert "claude envelope not JSON" in res.error
        row = one_llm_row(h.store)
        assert (
            h.raw_dir / f"{row['id']}.a1.claude.stdout"
        ).read_bytes() == b"this is not a JSON envelope"

    def test_no_stdout_at_all_is_not_reported_as_an_unreadable_reply(
        self, tmp_path, monkeypatch
    ):
        """An empty successful process produced no answer to parse.

        Classify it as empty_output, with the same outcome in storage and the result."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, b"", b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "empty_output", (
            f"a call that produced nothing was reported as {res.outcome!r}"
        )
        assert "no output" in res.error.lower(), res.error
        assert one_llm_row(h.store)["outcome"] == "empty_output"

    def test_whitespace_only_stdout_is_also_empty(self, tmp_path, monkeypatch):
        """A CLI that prints a newline and exits has still said nothing."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, b"\n  \n", b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.outcome == "empty_output", res.outcome

    def test_codex_producing_no_lines_is_empty_not_unparseable(
        self, tmp_path, monkeypatch
    ):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, b"", b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.outcome == "empty_output", res.outcome

    def test_output_that_arrived_and_did_not_parse_is_still_parse_error(
        self, tmp_path, monkeypatch
    ):
        """Narrowness guard. Bytes arrived; `parse_error` is the right word."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude((0, b"this is not a JSON envelope", b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.outcome == "parse_error", res.outcome

    def test_is_error_envelope_without_quota_text_is_other(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection())
        h.fake.expect_claude(
            (0, claude_envelope("API Error: 500 internal server error", is_error=True), b"")
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "other"
        assert "is_error envelope" in res.error


# ---------------------------------------------------------------------------
# parse_recovered — exactly ONE prose-wrapped JSON object is accepted, loudly
# ---------------------------------------------------------------------------


RECOVERED_PAYLOAD = {"findings": [{"rule_text": "x"}], "confidence": "high"}
RECOVERED_JSON = json.dumps(RECOVERED_PAYLOAD)


def run_claude_json(h, result_text: str, stage: str = "grade"):
    """One expect_json call whose assistant text is result_text."""
    h.pick.expect(selection())
    h.fake.expect_claude((0, claude_envelope(result_text), b""))
    return h.runner.call(stage, "sonnet", PROMPT, expect_json=True)


class TestParseRecovered:
    def test_prose_before_json_recovers(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        text = (
            "After exploring the transcripts, here is my analysis.\n\n"
            + RECOVERED_JSON
        )
        res = run_claude_json(h, text)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == RECOVERED_PAYLOAD
        assert res.text == text  # raw prose-wrapped text retained verbatim
        assert res.error == ""
        row = one_llm_row(h.store)
        assert row["outcome"] == "parse_recovered"
        assert row["error"] == ""
        # parse_recovered is a SUCCESS in stats but stays distinct in
        # by_outcome so prose-wrapping frequency remains visible.
        stats = h.runner.stats()
        assert stats["attempted"] == 1
        assert stats["succeeded"] == 1
        assert stats["failed"] == 0
        assert stats["by_outcome"] == {"parse_recovered": 1}

    def test_prose_after_json_recovers(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        text = RECOVERED_JSON + "\n\nLet me know if anything needs a closer look."
        res = run_claude_json(h, text)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == RECOVERED_PAYLOAD
        assert one_llm_row(h.store)["outcome"] == "parse_recovered"

    def test_fenced_json_with_trailing_prose_recovers(self, tmp_path, monkeypatch):
        # The strict fence regex requires the fence to be the ENTIRE text;
        # a fence followed by prose falls through to the recovery scan.
        h = make_runner(tmp_path, monkeypatch)
        text = f"```json\n{RECOVERED_JSON}\n```\nHope this helps!"
        res = run_claude_json(h, text)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == RECOVERED_PAYLOAD

    def test_two_objects_is_ambiguous_parse_error(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        text = 'Either {"verdict": "pass"} or {"verdict": "fail"} fits here.'
        res = run_claude_json(h, text)
        assert res.ok is False
        assert res.outcome == "parse_error"
        assert res.parsed is None
        assert res.text == text  # raw retained, never guessed
        assert res.error.startswith("expect_json:")
        assert "2 top-level JSON object(s)" in res.error
        assert one_llm_row(h.store)["outcome"] == "parse_error"
        stats = h.runner.stats()
        assert stats["succeeded"] == 0
        assert stats["by_outcome"] == {"parse_error": 1}

    def test_zero_objects_is_parse_error(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        res = run_claude_json(h, "I could not complete the analysis.")
        assert res.ok is False
        assert res.outcome == "parse_error"
        assert res.parsed is None
        assert res.error.startswith("expect_json:")
        assert "0 top-level JSON object(s)" in res.error

    def test_braces_inside_string_values_do_not_split_the_object(
        self, tmp_path, monkeypatch
    ):
        # Braces and escaped quotes INSIDE a JSON string value must not open
        # or close anything in the matcher.
        h = make_runner(tmp_path, monkeypatch)
        payload = {"rule_text": 'say "{not json}" and \\ {backslash}', "ok": True}
        text = "Here is the rule.\n" + json.dumps(payload) + "\nEnd of report."
        res = run_claude_json(h, text)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == payload

    def test_nested_objects_count_as_one_top_level(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        payload = {"outer": {"inner": {"deep": 1}}, "list": [{"y": 2}]}
        res = run_claude_json(h, "Result: " + json.dumps(payload))
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == payload

    def test_non_json_prose_braces_are_not_candidates(self, tmp_path, monkeypatch):
        # A balanced-but-invalid {curly} span in prose is not a candidate:
        # exactly one REAL object remains, so recovery still applies.
        h = make_runner(tmp_path, monkeypatch)
        text = 'Use {curly} placeholders. Final answer: {"verdict": "pass"}'
        res = run_claude_json(h, text)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == {"verdict": "pass"}

    def test_single_object_inside_array_recovers(self, tmp_path, monkeypatch):
        # Deliberate consequence of the exactly-one rule: a top-level array
        # wrapping ONE object recovers to that object (previously
        # parse_error). An array with several objects stays refused.
        h = make_runner(tmp_path, monkeypatch)
        res = run_claude_json(h, '[{"a": 1}]')
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == {"a": 1}

    def test_strict_bare_object_still_ok_not_recovered(self, tmp_path, monkeypatch):
        # Regression: the strict path is untouched — a bare JSON object is
        # outcome "ok", never parse_recovered.
        h = make_runner(tmp_path, monkeypatch)
        res = run_claude_json(h, '{"verdict": "pass"}')
        assert res.ok is True
        assert res.outcome == "ok"
        assert res.parsed == {"verdict": "pass"}
        assert one_llm_row(h.store)["outcome"] == "ok"

    def test_agentic_final_text_recovers(self, tmp_path, monkeypatch):
        # The invented final answer wraps contract JSON in prose.
        # Recovery must preserve the agentic stage in the call record.
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        text = "I explored the transcript. Findings below.\n\n" + json.dumps(
            {"findings": []}
        )
        res = run_claude_agentic_ok(h, sandbox, result_text=text)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == {"findings": []}
        row = one_llm_row(h.store)
        assert row["stage"] == "mine_agentic"
        assert row["outcome"] == "parse_recovered"

    def test_recovery_composes_with_oauth_retry(self, tmp_path, monkeypatch):
        # Same precedence plain ok has: the oauth retry claims the single
        # outcome slot; the recovery stays visible in the error column.
        h = make_runner(tmp_path, monkeypatch)
        text = "Prose first.\n" + RECOVERED_JSON
        h.pick.expect(selection())
        h.fake.expect_claude(
            (1, b"", b"Not logged in"),
            (0, claude_envelope(text), b""),
        )
        res = h.runner.call("grade", "sonnet", PROMPT, expect_json=True)
        assert res.ok is True
        assert res.outcome == "oauth_transient_retried"
        assert res.parsed == RECOVERED_PAYLOAD
        assert res.error == "parse_recovered"
        row = one_llm_row(h.store)
        assert row["outcome"] == "oauth_transient_retried"
        assert row["error"] == "parse_recovered"
        assert h.runner.stats()["succeeded"] == 1

    def test_recovery_composes_with_quota_repick(self, tmp_path, monkeypatch):
        # After a quota re-pick, recovery works exactly as ok does: outcome
        # parse_recovered, error still reports the re-pick.
        h = make_runner(tmp_path, monkeypatch)
        text = "Prose first.\n" + RECOVERED_JSON
        h.pick.expect(selection(), selection(account="claude_c"))
        h.fake.expect_claude(
            (1, b"", b"Claude AI usage limit reached|1755237600"),
            (0, claude_envelope(text), b""),
        )
        res = h.runner.call("grade", "sonnet", PROMPT, expect_json=True)
        assert res.ok is True
        assert res.outcome == "parse_recovered"
        assert res.parsed == RECOVERED_PAYLOAD
        assert res.error == "repicked_from=claude_b"
        assert res.account == "claude_c"
        row = one_llm_row(h.store)
        assert row["outcome"] == "parse_recovered"
        assert row["error"] == "repicked_from=claude_b"


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


class TestBudget:
    def test_second_cheap_call_refused(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, max_cheap_calls_per_run=1)
        run_claude_ok(h)
        with pytest.raises(BudgetExhausted) as excinfo:
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        exc = excinfo.value
        assert exc.tier == "cheap"
        assert exc.cap == 1
        assert exc.refused_so_far == 1
        # Refusal happens BEFORE any pick or subprocess: still exactly one
        # pick and one claude invocation from the first (allowed) call.
        assert len(h.pick.calls) == 1
        assert len(h.fake.calls) == 1
        # Refused calls get no llm_calls row (documented taxonomy gap).
        assert len(h.store.query("SELECT * FROM llm_calls")) == 1
        stats = h.runner.stats()
        assert stats["attempted"] == 1
        assert stats["succeeded"] == 1
        assert stats["refused"] == {"cheap": 1, "strong": 0, "gate": 0}
        # A further refusal keeps counting.
        with pytest.raises(BudgetExhausted) as excinfo2:
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert excinfo2.value.refused_so_far == 2
        assert h.runner.stats()["refused"] == {"cheap": 2, "strong": 0, "gate": 0}

    def test_cheap_refusal_does_not_touch_strong_budget(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, max_cheap_calls_per_run=0)
        with pytest.raises(BudgetExhausted):
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        stats = h.runner.stats()
        assert stats["calls_made"] == {"cheap": 0, "strong": 0, "gate": 0}
        assert stats["refused"] == {"cheap": 1, "strong": 0, "gate": 0}
        assert stats["attempted"] == 0
        assert h.fake.calls == []
        assert h.pick.calls == []


# ---------------------------------------------------------------------------
# cwd parameter
# ---------------------------------------------------------------------------


class TestCwd:
    def test_installed_package_uses_the_callers_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("self_improve.llm.REPO_ROOT", None)
        monkeypatch.chdir(tmp_path)
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h)
        (_, kw) = h.fake.calls_for(CLAUDE_BIN)[0]
        assert kw["cwd"] == str(tmp_path)

    def test_default_cwd_is_repo_root(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h)
        (_, kw) = h.fake.calls_for(CLAUDE_BIN)[0]
        assert kw["cwd"] == str(REPO_ROOT)
        # The pick is not given a cwd (select_account's cwd only affects
        # ./.quota-router.toml discovery; inherit-cwd matches the old CLI).
        assert "cwd" not in h.pick.calls[0]

    def test_cwd_override_passed_through(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "eval-sandbox"
        sandbox.mkdir()
        run_claude_ok(h, cwd=str(sandbox))
        (_, kw) = h.fake.calls_for(CLAUDE_BIN)[0]
        assert kw["cwd"] == str(sandbox)


# ---------------------------------------------------------------------------
# Disallowed provider from the pick
# ---------------------------------------------------------------------------


class TestDisallowedProvider:
    def test_picked_provider_not_allowed(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, allowed_providers=("claude",))
        h.pick.expect(selection(PICK_CODEX))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "other"
        assert "not in allowed_providers" in res.error
        assert res.provider == "codex"
        assert res.account == "codex"
        # The disallowed provider is never spawned.
        assert h.fake.provider_calls() == []
        assert one_llm_row(h.store)["outcome"] == "other"


# ---------------------------------------------------------------------------
# stats() math across a mixed sequence
# ---------------------------------------------------------------------------


class TestStatsMixedSequence:
    def test_attempted_succeeded_failed_math(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, max_cheap_calls_per_run=3)

        # 1. cheap ok
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        assert h.runner.call("mine", "sonnet", PROMPT, expect_json=False).ok is True

        # 2. cheap parse_error (expect_json against plain "OK")
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        assert (
            h.runner.call("mine", "sonnet", PROMPT, expect_json=True).outcome
            == "parse_error"
        )

        # 3. strong model_mismatch (envelope reports sonnet, opus requested)
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        assert (
            h.runner.call("propose", "opus", PROMPT, expect_json=False).outcome
            == "model_mismatch"
        )

        # 4. cheap timeout
        h.pick.expect(selection())
        h.fake.expect_claude(subprocess.TimeoutExpired(cmd=[CLAUDE_BIN], timeout=600))
        assert (
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False).outcome
            == "timeout"
        )

        # 5. cheap refused: cap of 3 cheap calls is spent
        with pytest.raises(BudgetExhausted):
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False)

        assert h.runner.stats() == {
            "attempted": 4,
            "succeeded": 1,
            "failed": 3,
            "by_outcome": {
                "ok": 1,
                "parse_error": 1,
                "model_mismatch": 1,
                "timeout": 1,
            },
            "calls_made": {"cheap": 3, "strong": 1, "gate": 0},
            "refused": {"cheap": 1, "strong": 0, "gate": 0},
            "policy_waits": 0,
        }
        # One llm_calls row per attempted call, none for the refusal.
        rows = h.store.query("SELECT outcome FROM llm_calls ORDER BY created_at")
        assert [r["outcome"] for r in rows] == [
            "ok",
            "parse_error",
            "model_mismatch",
            "timeout",
        ]


# ---------------------------------------------------------------------------
# call_agentic — multi-turn agentic sessions (same machinery, agentic argv)
# ---------------------------------------------------------------------------


def claude_envelope_multiturn(result_text: str, num_turns: int = 7) -> bytes:
    """Set result text and a multi-turn count in the invented envelope."""
    data = json.loads(CLAUDE_ENVELOPE)
    data["result"] = result_text
    data["num_turns"] = num_turns
    return json.dumps(data).encode("utf-8")


AGENTIC_FENCED_JSON = '```json\n{"findings": []}\n```'


def run_claude_agentic_ok(
    h, sandbox: Path, stage: str = "mine_agentic", result_text: str = AGENTIC_FENCED_JSON, **call_kwargs
):
    h.pick.expect(selection())
    h.fake.expect_claude((0, claude_envelope_multiturn(result_text), b""))
    return h.runner.call_agentic(stage, "sonnet", PROMPT, cwd=str(sandbox), **call_kwargs)


class TestAgenticClaude:
    def test_argv_flags_come_from_config(self, tmp_path, monkeypatch):
        # Overridden (non-default) config values must appear verbatim in the
        # argv — proves the flags are read from cfg, not hard-coded.
        h = make_runner(
            tmp_path,
            monkeypatch,
            mine_agent_max_turns=4,
            mine_agent_allowed_tools=("Read", "Grep"),
        )
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        res = run_claude_agentic_ok(h, sandbox)
        assert res.ok is True
        (argv, kw) = h.fake.calls_for(CLAUDE_BIN)[0]
        assert argv == [
            CLAUDE_BIN,
            "-p",
            "--model",
            "sonnet",
            "--output-format",
            "json",
            "--max-turns",
            "4",
            "--allowedTools",
            "Read,Grep",  # comma-joined per `claude --help` value format
        ]
        assert kw["input"] == PROMPT.encode("utf-8")  # prompt still on stdin
        # The sandbox cwd is the agent's world — it MUST be honored.
        assert kw["cwd"] == str(sandbox)

    def test_default_config_flags(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        run_claude_agentic_ok(h, sandbox)
        (argv, _) = h.fake.calls_for(CLAUDE_BIN)[0]
        i = argv.index("--max-turns")
        assert argv[i + 1] == str(h.cfg.mine_agent_max_turns)
        j = argv.index("--allowedTools")
        assert argv[j + 1] == ",".join(h.cfg.mine_agent_allowed_tools)

    def test_expect_json_parses_final_multiturn_result(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        res = run_claude_agentic_ok(h, sandbox)  # expect_json defaults to True
        assert res.ok is True
        assert res.outcome == "ok"
        assert res.parsed == {"findings": []}
        # Identity assertion on modelUsage is unchanged for multi-turn runs.
        assert res.model_reported == "claude-sonnet-5"
        row = one_llm_row(h.store)
        # Agentic-ness is encoded in the stage string — no schema change.
        assert row["stage"] == "mine_agentic"
        assert row["outcome"] == "ok"

    def test_expect_json_failure_is_parse_error(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        res = run_claude_agentic_ok(h, sandbox, result_text="I explored the transcript.")
        assert res.ok is False
        assert res.outcome == "parse_error"
        assert res.parsed is None
        assert res.text == "I explored the transcript."  # raw retained
        assert res.error.startswith("expect_json:")
        assert one_llm_row(h.store)["outcome"] == "parse_error"

    def test_identity_still_enforced(self, tmp_path, monkeypatch):
        # Request the strong class (opus); the envelope reports sonnet + a
        # haiku sidecar -> model_mismatch, exactly as for call().
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        h.pick.expect(selection())
        h.fake.expect_claude((0, claude_envelope_multiturn(AGENTIC_FENCED_JSON), b""))
        res = h.runner.call_agentic("mine_agentic", "opus", PROMPT, cwd=str(sandbox))
        assert res.ok is False
        assert res.outcome == "model_mismatch"
        assert res.model_reported == "claude-haiku-4-5-20251001,claude-sonnet-5"
        assert "'opus'" in res.error
        assert one_llm_row(h.store)["outcome"] == "model_mismatch"

    def test_empty_cwd_raises_before_any_subprocess(self, tmp_path, monkeypatch):
        # An empty sandbox cwd must fail loud, never silently fall back to
        # the repo root (the sandbox is the agent's entire world).
        h = make_runner(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="sandbox cwd"):
            h.runner.call_agentic("mine_agentic", "sonnet", PROMPT, cwd="")
        assert h.fake.calls == []
        assert h.pick.calls == []
        assert h.store.query("SELECT * FROM llm_calls") == []
        # Nothing was charged against the budget either.
        assert h.runner.stats()["calls_made"] == {"cheap": 0, "strong": 0, "gate": 0}


class TestAgenticCodex:
    def test_argv_has_read_only_sandbox(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        write_codex_rollout(h)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        res = h.runner.call_agentic(
            "mine_agentic", "sonnet", PROMPT, cwd=str(sandbox), expect_json=False
        )
        assert res.ok is True
        assert res.text == "OK"
        assert res.model_reported == "gpt-5.6-terra"
        (argv, kw) = h.fake.calls_for(CODEX_BIN)[0]
        assert argv == [
            CODEX_BIN,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",  # exact value per `codex exec --help`
            "-m",
            "gpt-5.6-terra",
            PROMPT,  # prompt on argv, not stdin
        ]
        assert kw["stdin"] is subprocess.DEVNULL
        assert "input" not in kw
        assert kw["cwd"] == str(sandbox)


class TestAgenticBudget:
    def test_agentic_is_one_call_against_shared_tier_cap(self, tmp_path, monkeypatch):
        # One agentic session = ONE budget unit (one invocation, though
        # multi-turn inside), charged against the SAME tier cap as call().
        h = make_runner(tmp_path, monkeypatch, max_cheap_calls_per_run=1)
        sandbox = tmp_path / "mine-sandbox"
        sandbox.mkdir()
        res = run_claude_agentic_ok(h, sandbox)
        assert res.ok is True
        assert h.runner.stats()["calls_made"] == {"cheap": 1, "strong": 0, "gate": 0}
        # The next plain call() on the same tier is refused before any
        # subprocess: the budget is shared, not per-method.
        with pytest.raises(BudgetExhausted) as excinfo:
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert excinfo.value.tier == "cheap"
        assert excinfo.value.cap == 1
        # One pick + one claude from the agentic call, nothing more.
        assert len(h.pick.calls) == 1
        assert len(h.fake.calls) == 1
        # And the reverse direction: an agentic call is refused too.
        with pytest.raises(BudgetExhausted):
            h.runner.call_agentic("mine_agentic", "sonnet", PROMPT, cwd=str(sandbox))
        assert h.runner.stats()["refused"] == {"cheap": 2, "strong": 0, "gate": 0}


# ---------------------------------------------------------------------------
# meets_policy gate — bounded wait, single fresh pick, loud failures
# ---------------------------------------------------------------------------


def iso_utc(epoch: float) -> str:
    """Independent ISO-UTC rendering to check error text against."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class FakeClock:
    """Deterministic time.time/time.sleep pair; sleeping advances the clock."""

    def __init__(self, start: float) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def install_clock(monkeypatch, start: float = 1_786_870_000.0) -> FakeClock:
    clock = FakeClock(start)
    monkeypatch.setattr(llm_mod.time, "time", clock.time)
    monkeypatch.setattr(llm_mod.time, "sleep", clock.sleep)
    return clock


class TestMeetsPolicyGate:
    def test_pick_missing_meets_policy_fails_loud(self, tmp_path, monkeypatch):
        # A decision that LACKS meets_policy (Selection.from_payload maps the
        # absent key to None) is malformed -> spawn_error, no provider ever
        # spawned — never treated as either policy verdict.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection_without(PICK_CLAUDE, "meets_policy"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "spawn_error"
        assert res.error.startswith("quotapick:")
        assert "meets_policy" in res.error
        assert res.provider == ""
        assert res.account == ""
        assert h.fake.calls == []
        assert one_llm_row(h.store)["outcome"] == "spawn_error"

    def test_pick_missing_fits_fails_loud(self, tmp_path, monkeypatch):
        # Same malformed-decision handling for a fits-less decision.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection_without(PICK_CLAUDE, "fits"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "spawn_error"
        assert "fits" in res.error
        assert h.fake.calls == []

    def test_unsupported_contract_version_fails_loud(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        data = json.loads(PICK_CLAUDE)
        data["contract_version"] = 2
        h.pick.expect(Selection.from_payload(data))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "spawn_error"
        assert "unsupported contract_version 2" in res.error
        assert h.fake.calls == []

    def test_non_numeric_available_at_fails_loud(self, tmp_path, monkeypatch):
        # from_payload passes available_at through unvalidated; the gate
        # still refuses anything that is not epoch seconds or null.
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(PICK_POLICY_WAIT, available_at="soon"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "spawn_error"
        assert "available_at must be epoch seconds" in res.error
        assert h.fake.calls == []

    def test_policy_wait_sleeps_repicks_and_proceeds(self, tmp_path, monkeypatch):
        # fits=true + meets_policy=false with a near available_at: ONE loud
        # sleep, then a FRESH pick (the pre-sleep pick is stale) that meets
        # policy, and the call proceeds normally on it.
        h = make_runner(tmp_path, monkeypatch)
        clock = install_clock(monkeypatch)
        wait_sel = selection(PICK_POLICY_WAIT, available_at=clock.now + 2.0)
        h.pick.expect(wait_sel, selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is True
        assert res.outcome == "ok"
        assert res.text == "OK"
        # Exactly one sleep, for ~the whole wait.
        assert len(clock.sleeps) == 1
        assert clock.sleeps[0] == pytest.approx(2.0, abs=0.01)
        # The post-sleep pick is a plain pick — no exclude/no_sticky; the
        # stale account may have become eligible while we slept.
        assert h.pick.calls == [pick_kwargs(), pick_kwargs()]
        # Both pick payloads retained under distinct numbers.
        row = one_llm_row(h.store)
        assert row["outcome"] == "ok"
        call_id = row["id"]
        assert (h.raw_dir / f"{call_id}.pick1.json").read_bytes() == pick_json_bytes(
            wait_sel
        )
        assert (h.raw_dir / f"{call_id}.pick2.json").read_bytes() == pick_json_bytes(
            selection()
        )
        assert h.runner.stats()["policy_waits"] == 1

    def test_second_pick_still_failing_policy_is_quota_exhausted(
        self, tmp_path, monkeypatch
    ):
        # The fresh pick after the sleep also misses policy: quota_exhausted,
        # EXACTLY one sleep (no wait loops), both available_at values named.
        h = make_runner(tmp_path, monkeypatch)
        clock = install_clock(monkeypatch)
        first_at = clock.now + 2.0
        second_at = clock.now + 500.0
        h.pick.expect(
            selection(PICK_POLICY_WAIT, available_at=first_at),
            selection(PICK_POLICY_WAIT, available_at=second_at),
        )
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert len(clock.sleeps) == 1  # never a second sleep
        assert h.fake.provider_calls() == []
        assert iso_utc(first_at) in res.error
        assert iso_utc(second_at) in res.error
        assert one_llm_row(h.store)["outcome"] == "quota_exhausted"
        assert h.runner.stats()["policy_waits"] == 1

    def test_wait_beyond_cap_fails_immediately_without_sleep(
        self, tmp_path, monkeypatch
    ):
        h = make_runner(tmp_path, monkeypatch, quota_wait_max_seconds=60)
        clock = install_clock(monkeypatch)
        far_at = clock.now + 3600.0
        h.pick.expect(selection(PICK_POLICY_WAIT, available_at=far_at))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert "meets_policy=false" in res.error
        assert f"{iso_utc(far_at)} exceeds wait cap 60s" in res.error
        # time.sleep was never called; one pick, no provider.
        assert clock.sleeps == []
        assert len(h.pick.calls) == 1
        assert h.fake.provider_calls() == []
        assert h.runner.stats()["policy_waits"] == 0
        assert one_llm_row(h.store)["outcome"] == "quota_exhausted"

    def test_absent_available_at_fails_immediately_without_sleep(
        self, tmp_path, monkeypatch
    ):
        # meets_policy=false with NO available_at: there is no bounded wait
        # to take, so the call fails loud right away.
        h = make_runner(tmp_path, monkeypatch)
        clock = install_clock(monkeypatch)
        h.pick.expect(selection_without(PICK_POLICY_WAIT, "available_at"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert "meets_policy=false" in res.error
        assert "absent" in res.error
        assert clock.sleeps == []
        assert h.fake.provider_calls() == []
        assert h.runner.stats()["policy_waits"] == 0

    def test_quota_repick_failing_policy_is_no_capacity_no_sleep(
        self, tmp_path, monkeypatch
    ):
        # The FAST re-pick after a provider-reported quota failure never
        # sleeps: a not-meets-policy pick2 is treated like no capacity.
        h = make_runner(tmp_path, monkeypatch)
        clock = install_clock(monkeypatch)
        h.pick.expect(
            selection(),
            selection(PICK_CODEX, meets_policy=False, available_at=clock.now + 2.0),
        )
        h.fake.expect_claude((1, b"", b"Claude AI usage limit reached|1755237600"))
        res = h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        assert res.ok is False
        assert res.outcome == "quota_exhausted"
        assert "re-pick has no capacity" in res.error
        assert clock.sleeps == []  # no sleep on the fast-recovery path
        # Attribution stays on the account that actually failed; the
        # not-meets-policy provider is never spawned.
        assert res.provider == "claude"
        assert res.account == "claude_b"
        assert h.fake.calls_for(CODEX_BIN) == []
        assert h.runner.stats()["policy_waits"] == 0

    def test_stats_expose_policy_waits(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        assert h.runner.stats()["policy_waits"] == 0
        clock = install_clock(monkeypatch)
        # Call 1: one policy wait, then success on the fresh pick.
        h.pick.expect(
            selection(PICK_POLICY_WAIT, available_at=clock.now + 1.0),
            selection(),
        )
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        assert h.runner.call("mine", "sonnet", PROMPT, expect_json=False).ok is True
        # Call 2: plain success, no wait — the counter must not move.
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        assert h.runner.call("mine", "sonnet", PROMPT, expect_json=False).ok is True
        stats = h.runner.stats()
        assert stats["policy_waits"] == 1
        assert stats["attempted"] == 2
        assert stats["succeeded"] == 2
        assert stats["by_outcome"] == {"ok": 2}


# ---------------------------------------------------------------------------
# a turn-budget DNF is its own failure mode, not "other"
# ---------------------------------------------------------------------------


def test_max_turns_dnf_gets_its_own_outcome():
    """A turn-cap failure has its own outcome, separate from generic failure."""
    from self_improve.llm import OUTCOMES, classify_failure_text

    assert "max_turns" in OUTCOMES
    envelope = (
        '{"is_error":true,"duration_api_ms":120000,"num_turns":25,'
        '"stop_reason":"tool_use","session_id":"example-session"}'
    )
    assert classify_failure_text(envelope) == "max_turns"


def test_an_ordinary_failure_is_still_other():
    """The new bucket must not swallow everything else."""
    from self_improve.llm import classify_failure_text

    assert classify_failure_text('{"is_error":true,"stop_reason":"end_turn"}') == "other"
    assert classify_failure_text("something went wrong") == "other"


def test_quota_and_oauth_still_win_over_max_turns():
    """Ordering matters: a quota failure that happens to mention turns is quota."""
    from self_improve.llm import classify_failure_text

    assert classify_failure_text('{"stop_reason":"tool_use","num_turns":25}') == "max_turns"


@pytest.mark.parametrize(
    "envelope",
    [
        '{"is_error":true,"stop_reason":"tool_use"}',
        '{"is_error": true, "stop_reason": "tool_use"}',
        '{\n  "is_error": true,\n  "stop_reason": "tool_use"\n}',
        '{"stop_reason"\t:\t"TOOL_USE"}',
    ],
)
def test_max_turns_detection_survives_whitespace_and_case(envelope):
    """A space-only strip would miss a pretty-printed envelope and fall back to
    "other", re-hiding the very failure mode this classification exists to
    surface."""
    from self_improve.llm import classify_failure_text

    assert classify_failure_text(envelope) == "max_turns"


# ---------------------------------------------------------------------------
# Separate pools for mining and the eval gate
# ---------------------------------------------------------------------------


class TestGatePool:
    """Mining and evaluation draw on separate call budgets.

    Exhausting the mining pool must leave the gate pool available."""

    def test_gate_stages_bill_to_the_gate_pool(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h, stage="grade")
        stats = h.runner.stats()
        assert stats["calls_made"]["gate"] == 1
        assert stats["calls_made"]["cheap"] == 0

    def test_an_exhausted_mine_pool_leaves_the_gate_runnable(self, tmp_path, monkeypatch):
        """The whole point. Mining takes everything; the gate still works."""
        h = make_runner(tmp_path, monkeypatch, max_cheap_calls_per_run=1)
        run_claude_ok(h, stage="mine")
        with pytest.raises(BudgetExhausted):
            h.runner.call("mine", "sonnet", PROMPT, expect_json=False)
        run_claude_ok(h, stage="grade")
        assert h.runner.stats()["calls_made"] == {"cheap": 1, "strong": 0, "gate": 1}

    def test_an_exhausted_gate_pool_leaves_mining_runnable(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, max_gate_calls_per_run=1)
        run_claude_ok(h, stage="grade")
        with pytest.raises(BudgetExhausted) as exc:
            h.runner.call("grade", "sonnet", PROMPT, expect_json=False)
        assert exc.value.tier == "gate"
        run_claude_ok(h, stage="mine")
        assert h.runner.stats()["calls_made"] == {"cheap": 1, "strong": 0, "gate": 1}

    def test_eval_gen_is_gate_work_whatever_tier_it_runs_on(self, tmp_path, monkeypatch):
        """Spec generation is gate work. Billing it by tier instead of by stage
        would reintroduce the coupling through the other pool."""
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h, stage="eval_gen")
        stats = h.runner.stats()
        assert stats["calls_made"]["gate"] == 1
        assert stats["calls_made"]["cheap"] == 0

    def test_non_gate_stages_are_unaffected(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h, stage="mine")
        assert h.runner.stats()["calls_made"] == {"cheap": 1, "strong": 0, "gate": 0}


class TestEvalSandboxWrapping:
    """The eval agent's subprocess must be wrapped, and only the eval agent's."""

    def test_a_sandboxed_call_is_wrapped_by_srt(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        sandbox = tmp_path / "trial" / "sandbox"
        sandbox.mkdir(parents=True)
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        h.runner.call(
            "grade", "sonnet", PROMPT, expect_json=False,
            cwd=str(sandbox), sandbox_dir=str(sandbox),
        )
        argv = h.fake.calls[0][0]
        assert "@anthropic-ai/sandbox-runtime@" in " ".join(argv)
        assert "--settings" in argv
        # ... and the settings file really exists, so srt cannot fall back to
        # its built-in defaults and start anyway.
        assert Path(argv[argv.index("--settings") + 1]).is_file()

    def test_the_policy_written_protects_the_instruction_files(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, global_claude_md=str(tmp_path / "G.md"))
        sandbox = tmp_path / "trial" / "sandbox"
        sandbox.mkdir(parents=True)
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        h.runner.call(
            "grade", "sonnet", PROMPT, expect_json=False,
            cwd=str(sandbox), sandbox_dir=str(sandbox),
        )
        argv = h.fake.calls[0][0]
        written = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        assert str(tmp_path / "G.md") in written["filesystem"]["denyWrite"]
        assert str(sandbox.resolve()) in written["filesystem"]["allowWrite"]

    def test_mining_calls_are_not_wrapped(self, tmp_path, monkeypatch):
        """Only the eval agent gets the sandbox; the miner is already read-only."""
        h = make_runner(tmp_path, monkeypatch)
        run_claude_ok(h, stage="mine")
        assert "sandbox-runtime" not in " ".join(h.fake.calls[0][0])

    def test_disabling_the_sandbox_is_explicit_and_visible(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch, eval_sandbox_enabled=False)
        sandbox = tmp_path / "trial" / "sandbox"
        sandbox.mkdir(parents=True)
        h.pick.expect(selection())
        h.fake.expect_claude((0, CLAUDE_ENVELOPE, b""))
        h.runner.call(
            "grade", "sonnet", PROMPT, expect_json=False,
            cwd=str(sandbox), sandbox_dir=str(sandbox),
        )
        assert "sandbox-runtime" not in " ".join(h.fake.calls[0][0])


class TestAnUnstartableCLIIsASpawnError:
    """Classify command-not-found and non-executable errors as spawn_error.
    Exit codes 127 and 126 indicate startup failure, not model behavior.
    """

    def test_exit_127_classifies_as_spawn_error(self):
        from self_improve.llm import classify_failure_text

        assert (
            classify_failure_text("exit 127: env: node: No such file or directory\n")
            == "spawn_error"
        )

    def test_exit_126_classifies_as_spawn_error(self):
        from self_improve.llm import classify_failure_text

        assert classify_failure_text("exit 126: Permission denied\n") == "spawn_error"

    def test_an_ordinary_nonzero_exit_is_not_a_spawn_error(self):
        """The guard must be narrow: a CLI that RAN and then failed is a
        different fact, and calling it spawn_error would hide a real failure."""
        from self_improve.llm import classify_failure_text

        assert classify_failure_text("exit 1: the model refused\n") == "other"
        assert classify_failure_text("exit 2: bad arguments\n") == "other"

    def test_the_turn_cap_classification_still_wins(self):
        """max_turns is a more specific fact than either; do not regress it."""
        from self_improve.llm import classify_failure_text

        text = 'exit 1: {"is_error": true, "stop_reason": "tool_use"}'
        assert classify_failure_text(text) == "max_turns"

    def test_a_127_inside_the_body_does_not_trigger_it(self):
        """Anchored on the exit code, not on the digits appearing anywhere.
        A model that wrote '127' in its answer must not be reclassified."""
        from self_improve.llm import classify_failure_text

        assert classify_failure_text("exit 1: the answer is 127\n") == "other"


def test_an_unhandled_attempt_status_raises_instead_of_becoming_model_mismatch(
    tmp_path, monkeypatch
):
    """The guard itself, driven rather than asserted about.

    Before it existed, a status `_classify` did not know fell through to the
    `status == "ok"` branch, failed the identity check because the attempt
    carried no reported model, and was recorded as `model_mismatch` — a
    specific, confident, wrong diagnosis. Silence is what made adding
    `empty_output` a bug instead of a no-op.
    """
    h = make_runner(tmp_path, monkeypatch)
    h.pick.expect(selection())
    h.fake.expect_claude((0, b'{"type":"result","result":"hi"}', b""))
    monkeypatch.setattr(
        llm_mod.LLMRunner,
        "_parse_claude",
        lambda self, rc, out, err, model: llm_mod._Attempt(status="brand_new_status"),
    )
    with pytest.raises(AssertionError, match="brand_new_status"):
        h.runner.call("mine", "sonnet", PROMPT, expect_json=False)


def test_every_attempt_status_the_parsers_emit_is_classified():
    """The list-drift guard, stated as a contract rather than a tuple.

    `_classify` used to test a hardcoded tuple of terminal statuses. Adding
    `empty_output` to the parsers left it out, and the unhandled status fell
    through to the `status == "ok"` path and came back `model_mismatch` — a
    confident wrong answer about a call that produced nothing. Exactly the
    shape that the dashboard's DRAWN_STATES had the same night.

    Reads the statuses out of the source so a new one cannot be added without
    either being handled or failing here.
    """
    import re as _re

    source = (
        Path(llm_mod.__file__).read_text(encoding="utf-8")
    )
    emitted = set(_re.findall(r'_Attempt\(\s*status="([a-z_]+)"', source))
    emitted |= set(_re.findall(r'_Attempt\(status="([a-z_]+)"', source))
    handled = set(llm_mod._TERMINAL_ATTEMPT_STATUSES) | {"failed", "ok"}
    assert emitted, "the scanner found no statuses; it has stopped working"
    unhandled = emitted - handled
    assert not unhandled, (
        f"_Attempt statuses {sorted(unhandled)} are emitted by a parser but "
        "_classify has no branch for them"
    )
    # And every terminal status must be a real outcome, or it reaches the DB
    # as a value nothing can render.
    assert set(llm_mod._TERMINAL_ATTEMPT_STATUSES) <= set(llm_mod.OUTCOMES), (
        sorted(set(llm_mod._TERMINAL_ATTEMPT_STATUSES) - set(llm_mod.OUTCOMES))
    )


# Generic CLI diagnostic: a nonfatal warning precedes the actual startup error.
# Classification must read the complete message, not only its first line.
_SANDBOX_DENIED_ENVELOPE = (
    "exit 1: WARNING: proceeding, even though we could not create PATH "
    "aliases: Operation not permitted (os error 1)\n"
    "Reading additional input from stdin...\n"
    "Error: failed to initialize in-process app-server client: Operation "
    "not permitted (os error 1)\n"
)


def test_sandbox_denial_gets_its_own_outcome():
    """A sandbox that prevents trial startup must have an explicit failure cause."""
    from self_improve.llm import OUTCOMES, classify_failure_text

    assert "sandbox_denied" in OUTCOMES
    assert classify_failure_text(_SANDBOX_DENIED_ENVELOPE) == "sandbox_denied"


def test_sandbox_denial_is_not_read_from_the_warning_line():
    """The warning alone is not the fault: the process said it was proceeding.

    A classifier keyed on "could not create PATH aliases" would claim a cause
    the provider explicitly declined to treat as fatal, and would fire on runs
    that then succeeded.
    """
    from self_improve.llm import classify_failure_text

    warning_only = (
        "exit 1: WARNING: proceeding, even though we could not create PATH "
        "aliases: Operation not permitted (os error 1)\n"
    )
    assert classify_failure_text(warning_only) == "other"


def test_sandbox_denial_does_not_swallow_an_ordinary_eperm():
    """`Operation not permitted` alone is far too broad to name this class."""
    from self_improve.llm import classify_failure_text

    assert classify_failure_text("exit 1: Operation not permitted (os error 1)") == "other"


# Provider diagnostic shapes for revoked and expired OAuth credentials.
# These contain no credential value or account identity.
_REVOKED = (
    '{"is_error":true,"terminal_reason":"api_error","result":"Failed to '
    'authenticate. API Error: 401 OAuth access token has been revoked.",'
    '"type":"result"}'
)
_EXPIRED = (
    '{"is_error":true,"terminal_reason":"api_error","result":"Failed to '
    'authenticate: OAuth session expired and could not be refreshed",'
    '"type":"result"}'
)


def test_dead_credentials_get_their_own_outcome():
    """Invalid credentials need a distinct outcome that directs interactive login."""
    from self_improve.llm import OUTCOMES, classify_failure_text

    assert "auth_failed" in OUTCOMES
    assert classify_failure_text(_REVOKED) == "auth_failed"
    assert classify_failure_text(_EXPIRED) == "auth_failed"


def test_auth_failed_needs_both_markers():
    """"Failed to authenticate" alone is not proof the CLAUDE account is dead.

    An agent shelling out to git, gh or a registry can print it about some
    other credential entirely, and this text is whatever the failed process
    wrote. Requiring the OAuth marker too keeps the class about the thing it
    is named for.
    """
    from self_improve.llm import classify_failure_text

    assert classify_failure_text("exit 1: Failed to authenticate to registry") == "other"
    assert classify_failure_text("exit 1: OAuth is configured") == "other"


def test_the_oauth_retry_race_is_still_not_auth_failed():
    """"Not logged in" is the transient credential race, which is retried.

    It has its own path and its own annotated error, and must not be swallowed
    by the new class — a retryable race and a dead account are different facts.
    """
    from self_improve.llm import classify_failure_text

    assert classify_failure_text("exit 1: Not logged in") == "other"


# ---------------------------------------------------------------------------
# a sandboxed trial must never be handed to a provider that cannot run one
# ---------------------------------------------------------------------------
#
# Quota availability alone cannot establish sandbox compatibility. The runner
# must exclude accounts whose provider cannot execute the requested trial.


class TestSandboxCapablePick:
    def test_a_codex_pick_is_replaced_before_any_call_is_made(self, tmp_path, monkeypatch):
        h = make_runner(tmp_path, monkeypatch)
        codex_pick = selection(PICK_CODEX)
        h.pick.expect(codex_pick, selection(PICK_CLAUDE))
        h.fake.expect_claude((0, claude_envelope("done"), b""))

        result = h.runner.call(
            "grade", "sonnet", "p", expect_json=False,
            cwd=str(tmp_path), sandbox_dir=str(tmp_path),
        )
        assert result.ok, result.error
        assert result.provider == "claude", "the trial ran on the incapable provider"
        assert len(h.pick.calls) == 2, h.pick.calls
        # the rejected account is excluded, so the library cannot return it again
        rejected = codex_pick.account or codex_pick.to_dict()["decision"]["account"]
        assert h.pick.calls[1]["exclude"] == [rejected], h.pick.calls[1]
        assert h.pick.calls[1]["no_sticky"] is True

    def test_a_capable_pick_is_used_as_is(self, tmp_path, monkeypatch):
        """The control. Without it, a loop that always re-picked would satisfy
        the test above and double every call's routing cost."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(PICK_CLAUDE))
        h.fake.expect_claude((0, claude_envelope("done"), b""))
        result = h.runner.call(
            "grade", "sonnet", "p", expect_json=False,
            cwd=str(tmp_path), sandbox_dir=str(tmp_path),
        )
        assert result.ok and len(h.pick.calls) == 1

    def test_a_NON_sandboxed_call_is_left_alone(self, tmp_path, monkeypatch):
        """A call without a sandbox keeps its selected provider."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        write_codex_rollout(h)
        result = h.runner.call("mine", "sonnet", "p", expect_json=False)
        assert len(h.pick.calls) == 1, "a non-sandboxed call was re-picked"
        assert result.provider == "codex"

    def test_a_fleet_that_cannot_run_a_trial_REFUSES_instead_of_spending(
        self, tmp_path, monkeypatch
    ):
        """Refuse before a call when no selected provider supports the sandbox."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(*[selection(PICK_CODEX) for _ in range(40)])
        result = h.runner.call(
            "grade", "sonnet", "p", expect_json=False,
            cwd=str(tmp_path), sandbox_dir=str(tmp_path),
        )
        assert not result.ok
        assert result.outcome == "sandbox_incapable_fleet", result.outcome
        assert h.fake.calls == [], "it spent a call on a provider that cannot run one"

    def test_the_refusal_is_bounded_and_does_not_spin(self, tmp_path, monkeypatch):
        """The loop is bounded by the exclusion list growing. If it were not,
        a fleet of one incapable account would hang the nightly."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(*[selection(PICK_CODEX) for _ in range(40)])
        h.runner.call(
            "grade", "sonnet", "p", expect_json=False,
            cwd=str(tmp_path), sandbox_dir=str(tmp_path),
        )
        assert len(h.pick.calls) < 20, (
            f"re-picked {len(h.pick.calls)} times; the bound is not holding"
        )

    def test_an_empty_config_list_disables_the_check(self, tmp_path, monkeypatch):
        """A machine whose codex CAN run sandboxed turns this off, and then the
        pick must be used untouched."""
        h = make_runner(tmp_path, monkeypatch, sandbox_incompatible_providers=())
        h.pick.expect(selection(PICK_CODEX))
        h.fake.expect_codex((0, CODEX_ENVELOPE, b""))
        write_codex_rollout(h)
        h.runner.call(
            "grade", "sonnet", "p", expect_json=False,
            cwd=str(tmp_path), sandbox_dir=str(tmp_path),
        )
        assert len(h.pick.calls) == 1

    def test_a_bare_string_exclusion_is_not_iterated_into_characters(
        self, tmp_path, monkeypatch
    ):
        """`select_account` ITERATES `exclude`. The docstring already warned a
        bare string explodes into letters; now that _pick accepts a list too,
        the string path needs a test or it will be the one that rots."""
        h = make_runner(tmp_path, monkeypatch)
        h.pick.expect(selection(PICK_CLAUDE))
        h.runner._pick("cid", "sonnet", pick_no=2, exclude="claude_d")
        assert h.pick.calls[-1]["exclude"] == ["claude_d"]
