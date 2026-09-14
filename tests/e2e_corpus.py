"""Reusable end-to-end corpus builder + a scripted LLM stand-in.

Why this exists
---------------
Per-module tests pass against fakes that per-module authors invented, so
contract drift between real modules survives them. ``test_pipeline_integration``
already covers the dry-run half (scan -> filter-incidents -> report, zero LLM). This
module covers the other half: a corpus rich enough to exercise MINE end to end
with the real sandbox builder, the real store, and the real source parsers.
Model responses are scripted, and provider startup uses temporary executables
that accept only the version probe. No provider installation is required.

The substitution happens at ``run_pipeline``'s documented ``_llm_factory``
seam, not by monkeypatching module internals.

What the corpus deliberately contains
-------------------------------------
- two Claude sessions in **two on-disk clones of one git repo**, reached by
  two different paths (a real dir and a symlink to it), so clone-collapse can
  be asserted rather than assumed;
- a Claude session in an unrelated repo, so collapse cannot pass by merging
  everything;
- a **headless** (``entrypoint: "sdk-cli"``) session, since the miner must
  learn from its own ``claude -p`` runs;
- a **Codex** session, so the two source parsers are both live;
- a session whose transcript is deleted after scanning, which is the only way
  to exercise the ``TranscriptAgedOut`` -> ``window_json`` fallback.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

from self_improve.config import Config
from self_improve.llm import BudgetExhausted
from self_improve.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "codex"

# One remote, many working copies — the shape that makes one repo count as N
# projects when project identity is a raw filesystem path.
REMOTE_URL = "git@github.com:example/demo-service.git"
OTHER_REMOTE_URL = "git@github.com:example/unrelated-thing.git"


# ----------------------------------------------------------------------
# Claude transcript synthesis
# ----------------------------------------------------------------------


class _LineWriter:
    """Emits Claude Code JSONL envelope lines with a per-instance counter.

    A module-level counter (the pattern in test_pipeline_integration) leaks
    uuid state across tests; this keeps each corpus self-contained.
    """

    def __init__(self, session_id: str, cwd: str, entrypoint: str = "cli"):
        self.session_id = session_id
        self.cwd = cwd
        self.entrypoint = entrypoint
        self.n = 0

    def __call__(self, **kw) -> str:
        base = {
            "uuid": f"{self.session_id}-u{self.n}",
            "parentUuid": None if self.n == 0 else f"{self.session_id}-u{self.n - 1}",
            "isSidechain": False,
            "isMeta": None,
            "userType": "external",
            "entrypoint": self.entrypoint,
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": "2.1.233",
            "gitBranch": "main",
            "slug": "e2e-fixture",
        }
        base.update(kw)
        self.n += 1
        return json.dumps(base) + "\n"


def failing_edit_then_correction(
    session_id: str,
    cwd: str,
    *,
    entrypoint: str = "cli",
    day: str = "2026-08-10",
    correction: str = (
        "no, that's wrong - you piped the CLI straight into jq again. "
        "Always dump raw output to a file and read it before parsing."
    ),
) -> str:
    """A minimal but realistic incident: tool error -> human correction.

    Shaped to trip the ``correction`` detector: a human text turn that follows
    prior assistant tool activity and an errored tool_result.
    """
    line = _LineWriter(session_id, cwd, entrypoint)
    err = {
        "type": "tool_result",
        "tool_use_id": f"toolu_{session_id}",
        "is_error": True,
        "content": "jq: error (at <stdin>:0): Invalid numeric literal at line 1",
    }
    return (
        line(
            type="user",
            timestamp=f"{day}T01:00:00.000Z",
            message={"role": "user", "content": "get me the json from that CLI"},
        )
        + line(
            type="assistant",
            timestamp=f"{day}T01:00:05.000Z",
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": f"toolu_{session_id}",
                        "input": {"command": "npm run twin -- --json | jq .items"},
                    }
                ],
            },
        )
        + line(
            type="user",
            timestamp=f"{day}T01:00:09.000Z",
            message={"role": "user", "content": [err]},
        )
        + line(
            type="user",
            timestamp=f"{day}T01:01:00.000Z",
            message={"role": "user", "content": correction},
        )
    )


def _git_repo(path: Path, remote: str) -> None:
    """Create a local Git repository with an invented remote URL.

    Git initialization lets identity resolution inspect real repository metadata.
    The remote URL is a fixture; this helper does not contact the host.
    """
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        a, cwd=path, check=True, capture_output=True
    )
    run("git", "init", "-q")
    run("git", "remote", "add", "origin", remote)


# ----------------------------------------------------------------------
# corpus
# ----------------------------------------------------------------------


@dataclasses.dataclass
class Corpus:
    cfg: Config
    store: Store
    root: Path
    # on-disk working copies, by the name the test refers to them by
    clone_a: Path
    clone_b_symlinked: Path
    unrelated: Path
    # transcripts, so a test can delete one to force the aged-out path
    transcripts: dict[str, Path]

    def session_row(self, session_id: str) -> dict:
        return self.store.query_one(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )

    def incidents(self) -> list[dict]:
        return self.store.query("SELECT * FROM incidents ORDER BY ts")


def build_corpus(tmp_path: Path) -> Corpus:
    """Materialize the whole fake world: repos, transcripts, config, store."""
    code = tmp_path / "Code"
    code.mkdir(parents=True)

    # Invented clone layout: two working copies plus a compatibility symlink.
    # Resolve their repository identity independently of the directory names.
    clone_a = code / "demo-service" / "repo-0"
    _git_repo(clone_a, REMOTE_URL)
    clone_real_b = code / "demo-service" / "repo-3"
    _git_repo(clone_real_b, REMOTE_URL)
    (code / "old-service").symlink_to(code / "demo-service")
    clone_b_symlinked = code / "old-service" / "repo-3"

    unrelated = code / "unrelated-thing"
    _git_repo(unrelated, OTHER_REMOTE_URL)

    projects = tmp_path / "claude" / "projects"
    transcripts: dict[str, Path] = {}

    def add_claude(session_id: str, cwd: Path, **kw) -> None:
        slug = "-" + str(cwd).strip("/").replace("/", "-")
        d = projects / slug
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{session_id}.jsonl"
        p.write_text(failing_edit_then_correction(session_id, str(cwd), **kw))
        transcripts[session_id] = p

    # same repo, two paths (one of them through the symlink)
    add_claude("sess-clone-a", clone_a, day="2026-08-10")
    add_claude("sess-clone-b", clone_b_symlinked, day="2026-08-11")
    # unrelated repo — collapse must not swallow this one
    add_claude("sess-unrelated", unrelated, day="2026-08-12")
    # headless: the miner learning from its own `claude -p` runs
    add_claude("sess-headless", clone_a, day="2026-08-13", entrypoint="sdk-cli")
    # this one's transcript gets deleted by the aged-out test
    add_claude("sess-agedout", clone_a, day="2026-08-14")

    codex_sessions = tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
    codex_sessions.mkdir(parents=True)
    codex_archived = tmp_path / "codex" / "archived_sessions"
    codex_archived.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "main_session.jsonl",
        codex_sessions
        / "rollout-2026-08-05T10-00-00-aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa.jsonl",
    )

    providers = tmp_path / "providers"
    providers.mkdir()
    for name in ("claude", "codex"):
        executable = providers / name
        executable.write_text(
            '#!/bin/sh\n'
            'if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
            '  printf "synthetic provider 1.0\\n"\n'
            'else\n'
            '  printf "fixture permits only a version probe\\n" >&2\n'
            '  exit 64\n'
            'fi\n'
        )
        executable.chmod(0o700)

    cfg = dataclasses.replace(
        Config(),
        allowed_providers=("claude", "codex"),
        claude_path=str(providers / "claude"),
        codex_path=str(providers / "codex"),
        claude_projects_dir=str(projects),
        claude_history_path=str(tmp_path / "claude" / "history.jsonl"),
        codex_sessions_dir=str(tmp_path / "codex" / "sessions"),
        codex_archived_dir=str(codex_archived),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(tmp_path / "global" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
        # pytest's tmp_path lives under /var/folders, which the production
        # denylist exists to exclude. This corpus needs cwd to be a REAL
        # directory (real git repos, real symlink, working realpath), so the
        # denylist is cleared here. Its own behaviour is covered by
        # test_scan.py; leaving it on would silently scan nothing.
        denylist_substrings=(),
        # The fixture remotes use invented example/ repository URLs. Git metadata
        # exercises URL-based identity without contacting GitHub. Canonical host-ID
        # lookup is covered separately with injected responses.
        project_identity_use_gh=False,
    )
    return Corpus(
        cfg=cfg,
        store=Store(tmp_path / "state" / "state.db"),
        root=tmp_path,
        clone_a=clone_a,
        clone_b_symlinked=clone_b_symlinked,
        unrelated=unrelated,
        transcripts=transcripts,
    )


# ----------------------------------------------------------------------
# scripted LLM
# ----------------------------------------------------------------------


def mine_payload(rule: str, **overrides) -> dict:
    """A contract-valid agentic mine response. Overrides are applied last.

    Kept in sync with miner.MINE_AGENTIC_JSON_KEYS by
    ``test_e2e_pipeline.test_mine_payload_helper_matches_the_live_contract`` —
    if the contract gains a key, that test fails here rather than letting
    every E2E test silently exercise a stale shape.
    """
    payload = {
        "is_real_learning": True,
        "incident_summary": "piped a wrapped CLI's stdout into jq and it broke",
        "generalized_rule": rule,
        "why": "the banner precedes the JSON, so the parser sees a literal",
        "scope_guess": "project",
        "category": "tooling",
        "duplicate_of_existing_rule": None,
        "confidence": 0.75,
        "dedup_decision": "new",
        # These three are "" when absent, NOT null — the contract validates
        # them as strings. (duplicate_of_existing_rule, by contrast, is
        # explicitly string-or-null.) Learned by this harness rejecting the
        # first version of this helper.
        "dedup_target_id": "",
        "amended_rule_text": "",
        "amended_why": "",
        "violated_existing_rule": "",
        "path_globs": [],
    }
    payload.update(overrides)
    return payload


@dataclasses.dataclass
class _Result:
    """Duck-type of llm.LLMResult — only the fields the pipeline reads."""

    ok: bool
    parsed: dict | None
    text: str = ""
    outcome: str = "ok"
    provider: str = "fake"
    model_reported: str = "fake-model"
    account: str = "fake-account"
    error: str = ""


class ScriptedLLM:
    """Stand-in for LLMRunner that spends nothing and records everything.

    Constructed by the pipeline as ``_llm_factory(cfg, store, run_id, raw_dir)``,
    so the real signature is absorbed positionally.

    ``mine_responses`` is consumed in order; when exhausted, every further mine
    call fails as a parse failure rather than silently repeating the last
    payload — a fake that never runs out would hide a miscounted fan-out.
    """

    def __init__(self, *args, mine_responses=None, other_responses=None,
                 mine_failure=None, **kwargs):
        self.mine_responses = list(mine_responses or [])
        # A CALL that fails, as opposed to an answer that will not parse.
        # `(outcome, error)` — e.g. ("spawn_error", "exit 127: env: node: ...").
        # Without this the fake can only produce parse failures, so no test
        # could drive the real adapter's failure branch and a sabotage of it
        # passed clean.
        self.mine_failure = mine_failure
        self.other_responses = dict(other_responses or {})
        self.calls: list[dict] = []
        self.sandboxes: list[Path] = []
        self.sandbox_listings: list[set[str]] = []
        self._cheap = 0
        self._refused = 0
        # Gate-pool accounting. Production bills by STAGE (llm.py routes
        # cfg.gate_stages to the gate pool), and this fake did not, so
        # `calls_made` never had a "gate" key. Both budget preflights read
        # `made.get("gate", 0)`, which meant they saw 0 forever and could not
        # refuse anything under test: two safety branches, dead in the suite.
        self._gate = 0
        # The pipeline constructs the runner as (cfg, store, run_id, raw_dir),
        # so the cap comes from the same config the real runner reads. Without
        # this the fake ignores the budget entirely and an E2E test that sets
        # max_cheap_calls silently does not cap anything.
        self._cfg = args[0] if args else None
        # The real runner INSERTS an llm_calls row per call, and the report's
        # mine funnel reads that table. The fake must write those rows too,
        # so report tests count the actual attempted work in their fixture.
        self._store = args[1] if len(args) > 1 else None
        self._run_id = args[2] if len(args) > 2 else "run-fake"

    def _spend(self, stage: str) -> None:
        """Charge the pool production would charge for this stage."""
        if stage in getattr(self._cfg, "gate_stages", ()):
            cap = getattr(self._cfg, "max_gate_calls_per_run", None)
            if cap is not None and self._gate >= cap:
                self._refused += 1
                raise BudgetExhausted("gate", cap, self._refused)
            self._gate += 1
            return
        self._spend_cheap()

    def _spend_cheap(self) -> None:
        """Mirror LLMRunner._run_call: charge BEFORE dispatching.

        Known simplification: every call is charged to the CHEAP tier, while
        the real runner charges by model class and keeps a separate strong
        budget. That makes this fake stricter than production — a strong-model
        call here shortens the cheap budget instead of drawing on its own — so
        E2E tests err toward stopping early rather than over-spending, which is
        the safe direction for a harness. Deliberate, not an oversight; fix it
        by mirroring `_tier` if a test ever needs the strong budget modelled.

        The ordering is the point. An attempt that later fails to parse has
        still consumed budget, so a systematically broken prompt costs the cap
        and stops - it does not walk the whole incident queue. A fake that
        charged only on success would make that regression invisible.
        """
        cap = getattr(self._cfg, "max_cheap_calls_per_run", None)
        if cap is not None and self._cheap >= cap:
            self._refused += 1
            raise BudgetExhausted("cheap", cap, self._refused)
        self._cheap += 1

    def _record(self, stage: str, outcome: str, error: str = "") -> None:
        """Mirror LLMRunner's llm_calls insert. Only the columns readers use
        carry meaningful values; the rest are shaped, not invented."""
        if self._store is None:
            return
        from self_improve.store import new_id, utc_now_iso

        self._store.insert(
            "llm_calls",
            {
                "id": new_id(),
                "run_id": self._run_id,
                "stage": stage,
                "provider": "fake",
                "account": "fake-account",
                "model_requested": "fake-model",
                "model_reported": "fake-model",
                "prompt_sha": "0" * 64,
                "tokens_in": 0,
                "tokens_out": 0,
                "duration_ms": 1,
                "outcome": outcome,
                "error": error,
                "created_at": utc_now_iso(),
            },
        )
        # Commit immediately, as LLMRunner does. A later incident rollback must
        # preserve the call row, including when every attempt fails.
        self._store.commit()

    def call(self, stage, model_class, prompt, expect_json=True, **kw):
        self.calls.append({"stage": stage, "kind": "call", "prompt": prompt})
        # Charge the budget here too. The real LLMRunner routes call() and
        # call_agentic() through one _run_call and charges both; a fake that
        # charged only the agentic path let every post-mine stage (gate,
        # contradictions, ab_prune) keep spending after the budget was gone,
        # which is the opposite of production - there they are starved.
        self._spend(stage)
        if stage in self.other_responses:
            self._record(stage, "ok")
            return _Result(ok=True, parsed=self.other_responses[stage])
        # Unscripted non-mine stages fail cleanly: the pipeline treats that as
        # a failed call, which is the honest outcome for "we didn't script it".
        self._record(stage, "parse_failure")
        return _Result(ok=False, parsed=None, outcome="parse_failure")

    def call_agentic(self, stage, model_class, prompt, cwd, expect_json=True, **kw):
        sandbox = Path(cwd)
        self.sandboxes.append(sandbox)
        self.sandbox_listings.append(
            {p.name for p in sandbox.iterdir()} if sandbox.is_dir() else set()
        )
        self.calls.append(
            {"stage": stage, "kind": "agentic", "cwd": str(sandbox), "prompt": prompt}
        )
        self._spend(stage)
        if self.mine_failure is not None:
            outcome, error = self.mine_failure
            self._record(stage, outcome, error)
            return _Result(ok=False, parsed=None, outcome=outcome, error=error)
        if not self.mine_responses:
            self._record(stage, "parse_failure")
            return _Result(ok=False, parsed=None, outcome="parse_failure")
        self._record(stage, "ok")
        return _Result(ok=True, parsed=self.mine_responses.pop(0))

    def stats(self):
        return {
            "attempted": len(self.calls),
            "succeeded": sum(1 for c in self.calls if c["kind"] == "agentic"),
            "failed": 0,
            "by_outcome": {},
            "calls_made": {"cheap": self._cheap, "strong": 0, "gate": self._gate},
            "refused": {"cheap": self._refused, "strong": 0, "gate": 0},
            # Reported by the real runner and carried into runs.stats_json.
            # Nothing branches on it, but a divergence is a divergence, and
            # today's lesson is that they disable things silently.
            "policy_waits": 0,
        }

    def factory(self):
        """Return the ``_llm_factory`` callable that yields *this* instance.

        Binds the cfg the pipeline hands in. It matters because
        ``run_pipeline`` applies a ``max_cheap_calls`` override by REPLACING
        cfg just before constructing the runner — so an instance built earlier
        in the test holds the un-overridden config, and a fake that ignored
        this argument would enforce the default cap (or none) while the test
        believed it had set one.
        """

        def _make(*args, **kwargs):
            # (cfg, store, run_id, raw_dir) — the pipeline constructs the
            # runner here, so this is where the store and run_id arrive.
            # Setting them only in __init__ left `_record` a silent no-op for
            # every test that builds the fake with no positional args, which
            # is nearly all of them.
            if args:
                self._cfg = args[0]
            if len(args) > 1:
                self._store = args[1]
            if len(args) > 2:
                self._run_id = args[2]
            return self

        return _make
