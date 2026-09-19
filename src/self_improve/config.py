"""Configuration for the self-improve miner.

Fail-loud policy: every knob is an explicit field with an explicit default here.
A user override file (~/.self-improve/config.toml) may set any field, but an
unknown key in that file raises ConfigError rather than being ignored, and a
field whose value has the wrong type raises rather than being coerced.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import sysconfig
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .resources import SOURCE_ROOT

HOME = Path.home()
_SEARCH_CLI = Path(sysconfig.get_path("scripts")) / ("selfimprove.exe" if os.name == "nt" else "selfimprove")


class ConfigError(Exception):
    """Raised for unknown keys, wrong types, or missing required paths."""


def default_claude_managed_dir() -> str:
    """Native provider location; selecting a default performs no filesystem I/O."""
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    if sys.platform == "win32":
        return "C:/Program Files/ClaudeCode"
    return "/etc/claude-code"


@dataclass(frozen=True)
class Config:
    # --- data sources (read-only) ---
    claude_projects_dir: str = str(HOME / ".claude" / "projects")
    claude_history_path: str = str(HOME / ".claude" / "history.jsonl")
    codex_sessions_dir: str = str(HOME / ".codex" / "sessions")
    codex_archived_dir: str = str(HOME / ".codex" / "archived_sessions")

    # Project slugs / cwd prefixes that must never be mined (generated eval
    # trees and ephemeral scratch dirs). Substring match on slug or cwd.
    denylist_substrings: tuple[str, ...] = (
        "-private-tmp-",
        "/private/tmp/",
        # macOS tempdirs and our own eval-trial sandboxes: the miner's
        # eval trials run claude -p with cwd inside these, and those sessions
        # must never be mined as if a human worked there (meta-recursion still
        # covers the miner's own top-level runs, whose cwd is this repo).
        "-var-folders-",
        "/var/folders/",
        ".self-improve/runs",
        "-self-improve-runs-",
    )

    # --- runtime state (read-write, OUTSIDE the repo) ---
    state_dir: str = str(HOME / ".self-improve")

    # --- instruction-file targets ---
    global_claude_md: str = str(HOME / ".claude" / "CLAUDE.md")
    codex_global_agents_md: str = str(HOME / ".codex" / "AGENTS.md")
    skills_dir: str = str(HOME / ".claude" / "skills")
    # Empty derives .agents/skills beside the configured Codex home. Override
    # when the user's skill root and CODEX_HOME do not share a parent.
    codex_skills_dir: str = ""
    # Observation only. This is never an instruction-delivery destination.
    claude_managed_dir: str = field(default_factory=default_claude_managed_dir)
    # Empty derives plugins/ beside the configured global Claude instruction file.
    claude_plugins_dir: str = ""
    # Line budget for the global CLAUDE.md: at/over budget an addition must be
    # paired with a deletion proposal or demoted to a skill.
    global_claude_md_line_budget: int = 250

    # --- clustering / embeddings ---
    # Local static-embedding model (model2vec); rule text never leaves the
    # machine for embedding. Downloaded from HF on first use, then cached.
    embedding_model: str = "minishlab/potion-base-8M"
    # Hub models require a full commit ID. Local directories use content hashes.
    embedding_revision: str = "bf8b056651a2c21b8d2565580b8569da283cab23"
    # Optional expected bundle digest; set this when reproducing a frozen model.
    embedding_model_sha256: str = ""
    # Cosine-similarity thresholds. Grouping candidates is looser than calling
    # something a duplicate of an existing/rejected rule (a false duplicate
    # silently suppresses a real learning).
    cluster_group_cosine: float = 0.80
    cluster_dup_cosine: float = 0.85

    # --- autonomous mining ---
    # The mine stage runs an agentic headless session that explores a redacted
    # rendering of the FULL transcript in a sandbox (root-causing failures
    # that manifest many turns after their origin). "fast" = legacy
    # single-turn window call.
    mine_mode: str = "agentic"  # agentic | fast
    # Keep exploration below the turn cap and reserve turns for a final answer.
    # Tune against a frozen private evaluation; turn-cap failures remain explicit.
    mine_agent_max_turns: int = 24
    # The miner's dedup tool: embedding search over the learnings table via
    # `selfimprove search-learnings`, which opens the DB READ-ONLY
    # (search.py). Absolute venv-bin path so the sandboxed agent needs no
    # uv/PATH indirection.
    mine_search_cli: str = str(_SEARCH_CLI)
    # The scheduler requires a separate production checkout. Source installs
    # default to a sibling repo-prod; wheel installs use a location under state.
    # Installation validates that the configured checkout contains the nightly
    # script. This is the execution location, not an instruction-write target.
    production_repo_path: str = str(
        SOURCE_ROOT.parent / "repo-prod" if SOURCE_ROOT else HOME / ".self-improve" / "repo-prod"
    )
    # Where the gate WRITES the eval specs it generates. Under the state dir,
    # never inside a checkout: `generate_spec` writes one YAML per scenario and
    # `evals/regression` is git-tracked, so a repo-relative target would make
    # the production checkout dirty on every night that reaches the gate. The
    # checked-in `evals/regression/seed-*.yaml` stay in the repo as the
    # labelled seed corpus; only generated specs move here.
    # Relative paths resolve under state_dir; an explicit absolute private
    # directory is also supported. Redirecting state_dir redirects this default.
    regression_specs_dir: str = "evals/regression"
    # Read-only exploration tools plus exactly one allowlisted command (the
    # search CLI above). No Write/Edit/network, no general Bash.
    mine_agent_allowed_tools: tuple[str, ...] = (
        "Read",
        "Grep",
        "Glob",
        f"Bash({_SEARCH_CLI} search-learnings:*)",
    )

    # --- LLM invocation ---
    # Retained for config compatibility; Python routes through the library and
    # the shell scheduler uses its own SI_QUOTAPICK setting. See the declared
    # unused-field list in tests/test_config.py.
    quotapick_path: str = str(HOME / ".local" / "bin" / "quotapick")
    claude_path: str = str(HOME / ".local" / "bin" / "claude")
    codex_path: str = "/opt/homebrew/bin/codex"
    # Model *classes* handed to quotapick; per-provider concrete model comes
    # back from the pick / is asserted from response telemetry.
    # Exclude providers whose CLI cannot start inside the configured sandbox.
    # This is a provider capability policy, not an account allowlist. Routing
    # derives account exclusions from the provider reported for each account.
    # An empty tuple disables the exclusion after capability is verified locally.
    sandbox_incompatible_providers: tuple[str, ...] = ("codex",)

    cheap_model_class: str = "sonnet"
    strong_model_class: str = "opus"
    # Providers quotapick may choose among. "claude" covers the configured
    # Claude accounts via CLAUDE_CONFIG_DIR in the pick's exec env.
    allowed_providers: tuple[str, ...] = ("claude", "codex")
    llm_timeout_seconds: int = 600
    # NOTHING READS THIS either: LLM calls are serial, no dispatcher consults
    # it, and setting it changes nothing. Left in place because concurrency is
    # a real planned feature, but it must not read as if it already works.
    llm_concurrency: int = 3
    # When a pick has fits=true but meets_policy=false, wait at most this many
    # seconds (one logged sleep until the decision's available_at, then one
    # fresh pick); a longer or unknown wait fails the call as quota_exhausted.
    quota_wait_max_seconds: int = 900

    # --- per-run budgets (hard caps, reported when they bite) ---
    # Mining and evaluation use separate pools, so increasing the mining
    # allowance cannot consume the gate's allowance.
    max_cheap_calls_per_run: int = 80
    max_strong_calls_per_run: int = 10
    # The default is sized for 63 calls across three maximum-cost proposal gates
    # (3 scenarios x (1 generation + 2 x 3 trials)) and 15 for the A/B sweep.
    # Both use this pool. Actual calls depend on skipped arms and failures;
    # increasing one stage's work does not reserve capacity for the other.
    max_gate_calls_per_run: int = 78
    # Stages billed to the gate pool rather than the mining pool. eval_gen is
    # the strong-model spec generation; grade is the per-trial agent call.
    gate_stages: tuple[str, ...] = ("eval_gen", "grade")

    # --- eval agent sandbox (Anthropic Sandbox Runtime) ---
    # The eval agent gets write and execute, so the boundary is OS-level rather
    # than a permission rule. srt wraps the whole process, so Write/Edit are
    # inside the boundary too; the built-in Bash sandbox covers only Bash.
    eval_sandbox_enabled: bool = True
    # Pinned: srt is a 0.0.x research preview and its config format is
    # documented as liable to change. This is the boundary the gate rests on.
    eval_sandbox_npx_package: str = "@anthropic-ai/sandbox-runtime@0.0.73"
    npx_path: str = "npx"
    eval_sandbox_probe_timeout_seconds: int = 180
    # Independent scenarios per rule. Majority-vote thresholds are constants
    # in evals/regression.py, so configuration cannot lower the voting bar.
    eval_scenarios: int = 3
    eval_sandbox_allowed_domains: tuple[str, ...] = (
        "api.anthropic.com",
        "claude.ai",
        "platform.claude.com",
        "statsig.anthropic.com",
        "chatgpt.com",
        "api.openai.com",
        "auth.openai.com",
    )

    # --- filter_incidents knobs ---
    # None disables this retention cap. Zero keeps no incidents. Per-session
    # caps can suppress bursty signals differently from isolated corrections.
    max_incidents_per_signal_per_session: int | None = None
    correction_max_len: int = 2000
    repeated_error_min_in_session: int = 3
    repeated_error_min_sessions: int = 2
    friction_loop_min_cycles: int = 4
    # Event span in which the minimum edit/error cycles must occur. Tool
    # results and intervening reasoning also occupy events. Tune against an
    # explicit frozen dataset; the default is not a universal optimum.
    friction_loop_window_events: int = 800

    # Evidence-retention budget. The agentic miner reads the full session
    # when available and falls back to this archive after source deletion.
    # Preserve more preceding context because a failure can surface after its
    # cause. These limits control recoverability, not the normal miner prompt.
    context_turns_before: int = 20
    context_turns_after: int = 10
    context_max_chars_per_message: int = 6000

    # --- eval gating ---
    eval_trials: int = 3
    # without-rule arm must fail at least this many trials for the eval to
    # count as able to detect the mistake ("gated"); otherwise "ungated".
    gate_without_min_failures: int = 1
    gate_with_min_passes: int = 2

    # --- apply policy ---
    # Legacy config field retained so existing config files still load. It no
    # longer grants permission. Persisted per-target classes all start off;
    # only gated_pass is automatic after class consent (execution_policy.py).
    auto_apply: bool = False
    # Additional manual-review actions. Hooks and human deletions are mandatory
    # even when this configurable list is empty.
    review_queue_actions: tuple[str, ...] = ("convert_to_hook", "delete_human_line")
    project_branch_name: str = "self-improve/rules"

    # signal_then_recent orders by score and then recency. age_out_risk puts
    # older surviving transcripts first. Both place already-deleted sources
    # last. With a finite budget, either policy can leave some incidents waiting;
    # compare evidence retention and project coverage on a selected dataset.
    mine_order: str = "signal_then_recent"

    # Stop starting mine calls after this wall-clock duration. A nightly lock
    # held too long can prevent the next scheduled run. Use wall time because
    # the macOS monotonic clock pauses during sleep. The gate has its own budget.
    max_run_wall_seconds: int = 12 * 3600

    # A run left in status='running' longer than this is assumed dead and is
    # reaped as 'abandoned'. Generous on purpose: the nightly lock only
    # serializes nightly-vs-nightly, so a manual run can legitimately be in
    # flight while another process starts, and reaping a LIVE run would put a
    # lie in the audit trail.
    run_stale_after_hours: int = 6

    # --- project identity ---
    # Resolve a repo's canonical key via `gh api repos/<owner>/<name>`, whose
    # numeric id survives renames and transfers. Off -> the normalized remote
    # URL is the key, which still collapses clones but fractures when a repo is
    # renamed (old clones keep the old URL forever; a fresh clone gets the new
    # one). Failure to reach gh degrades to the same URL key and is recorded in
    # scan stats as project_key_methods, never silently.
    project_identity_use_gh: bool = True

    # --- routing ---
    # A learning seen in >= this many distinct projects routes to the global file.
    # NOTE: counts distinct project_key (canonical repo), not distinct cwd —
    # otherwise N working copies of one repo self-promote to the global file.
    global_promotion_min_projects: int = 3

    # --- Part 2: new detectors ---
    # Basenames whose Edit/Write tool_use events fire the instruction_edit
    # detector (the highest-precision signal: someone encoded a lesson).
    instruction_edit_filenames: tuple[str, ...] = (
        "CLAUDE.md",
        "AGENTS.md",
        "SKILL.md",
        "MEMORY.md",
    )

    # --- Part 2: rule-effectiveness A/B pruning ---
    # Max applied rules re-tested per run (each costs ~2x eval_trials calls).
    ab_prune_max_rules_per_run: int = 5

    # --- Part 2: contradiction detection ---
    # Candidate pairs at cosine >= this go to the LLM contradiction judge
    # (contradictory rules are topically similar with opposing polarity).
    contradiction_candidate_cosine: float = 0.55
    contradiction_max_judgments_per_run: int = 20

    def state_path(self, *parts: str) -> Path:
        return Path(self.state_dir).joinpath(*parts)

    def __post_init__(self) -> None:
        """Require configured review actions to use the proposal vocabulary.

        Unknown values cannot match proposals.action, so reject them when
        Config is constructed. Mandatory review checks in execution_policy
        apply independently of this configurable action list.

        __post_init__ runs for every Config. validate also checks source
        directories and is reserved for entry points that need those paths.
        """
        if not isinstance(self.claude_managed_dir, str) or not Path(self.claude_managed_dir).is_absolute():
            raise ConfigError("claude_managed_dir must be an absolute directory")
        if not isinstance(self.claude_plugins_dir, str) or (self.claude_plugins_dir and not Path(self.claude_plugins_dir).is_absolute()):
            raise ConfigError("claude_plugins_dir must be empty or an absolute directory")
        from .store import PROPOSAL_ACTIONS

        unknown = sorted(set(self.review_queue_actions) - PROPOSAL_ACTIONS)
        if unknown:
            raise ConfigError(
                f"review_queue_actions names {unknown}, which is not an action "
                f"any code writes. Known actions: {sorted(PROPOSAL_ACTIONS)}. "
                "A carve-out naming an action nothing produces never fires."
            )

    def validate(self) -> None:
        """Check that read-only source paths exist. Raises ConfigError."""
        for name in ("claude_projects_dir", "codex_sessions_dir"):
            p = Path(getattr(self, name))
            if not p.is_dir():
                raise ConfigError(f"{name} does not exist: {p}")


_FIELD_TYPES = {f.name: f for f in dataclasses.fields(Config)}


def load_config(override_path: str | os.PathLike | None = None) -> Config:
    """Load Config with optional TOML overrides. Unknown keys raise."""
    path = Path(override_path) if override_path else Path(HOME / ".self-improve" / "config.toml")
    if not path.exists():
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    kwargs = {}
    for key, value in raw.items():
        if key not in _FIELD_TYPES:
            raise ConfigError(
                f"Unknown config key {key!r} in {path}. Known keys: {sorted(_FIELD_TYPES)}"
            )
        default = getattr(Config(), key)
        if isinstance(default, tuple):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"Config key {key!r} must be a list of strings, got {value!r}")
            value = tuple(value)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"Config key {key!r} must be a bool, got {value!r}")
        elif isinstance(default, int):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"Config key {key!r} must be an int, got {value!r}")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise ConfigError(f"Config key {key!r} must be a string, got {value!r}")
        kwargs[key] = value
    return Config(**kwargs)
