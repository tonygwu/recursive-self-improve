"""OS-level isolation for the eval agent, via the Anthropic Sandbox Runtime.

The eval gate has to let an agent write files and run commands, because a
regression eval works by making the agent actually perform a task and then
grading the files it left behind. That is a real capability grant, so it needs
a real boundary rather than a permission prompt.

Why this and not the built-in Bash sandbox: the built-in one covers Bash
commands and their child processes only, and the docs are explicit about it.
Built-in file tools such as Write and Edit run inside the Claude Code process
and would escape. `srt` wraps the *whole* process in the same macOS Seatbelt
isolation, so every tool is inside the boundary. Verified 2026-08-18 by telling
an agent not to use Bash and watching its Write-tool escape fail with
``EPERM: operation not permitted``.

Why not a Bash-only tool profile, which would also be hermetic: it makes the
eval unrepresentative. A rule such as "read the file before you edit it" cannot
be exercised by an agent with no Edit tool, so the without-rule arm passes, and
the gate concludes the rule is unnecessary. That is a false verdict produced by
the harness — the same class of bug as the permission failure this replaces,
only quieter.

Three properties this module is responsible for:

* **Deny by default for writes.** ``allowWrite`` is an allowlist, so anything
  not named is refused. Measured: with only the trial directory allowed, writes
  to ``$HOME``, the repo, and the global CLAUDE.md were all refused.
* **Fail loud on a bad policy.** ``srt`` refuses to start when ``--settings``
  names a file it cannot load, so the settings path is always passed
  explicitly. Never rely on the default ``~/.srt-settings.json``: without it
  the runtime starts anyway with built-in defaults, and a clean start is then
  not evidence that your policy loaded.
* **Prove the boundary before trusting it.** :func:`verify_boundary` runs a
  real escape attempt and raises unless it is refused. A sandbox that silently
  stopped working looks exactly like one that works.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


class SandboxError(RuntimeError):
    """The sandbox is missing, misconfigured, or failed to contain a probe."""


# The schema requires all four keys. The published README example omits
# `network.deniedDomains` and `filesystem.denyRead`, and srt refuses to start
# without them, so build the dict here rather than copying the example.
@dataclass(frozen=True)
class SandboxPolicy:
    """What the sandboxed process may write and which hosts it may reach."""

    allow_write: tuple[str, ...]
    deny_write: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()

    def to_settings(self) -> dict:
        return {
            "filesystem": {
                "allowWrite": list(self.allow_write),
                "denyWrite": list(self.deny_write),
                "denyRead": [],
            },
            "network": {
                "allowedDomains": list(self.allowed_domains),
                "deniedDomains": [],
            },
        }


def instruction_paths_to_protect(cfg) -> tuple[str, ...]:
    """Files the eval agent must never be able to write, even by accident.

    The agent runs with permissions and no prompts, and its whole job is to
    edit files. These are the ones whose corruption would be silent and
    compounding: the instruction files this system exists to propose edits to,
    and the state DB holding the evidence.
    """
    # NOT the whole state_dir. Trials run under
    # `<state_dir>/runs/<run-id>/trials/...`, and denyWrite beats allowWrite,
    # so denying the directory would also deny the trial workspace. Protect
    # the DB file itself and test both allowed and refused writes.
    return tuple(
        str(Path(p).expanduser())
        for p in (
            cfg.global_claude_md,
            cfg.codex_global_agents_md,
            cfg.skills_dir,
            str(Path(cfg.global_claude_md).expanduser().parent / "settings.json"),
            str(Path(cfg.state_dir).expanduser() / "state.db"),
        )
        if p
    )


def policy_for_trial(sandbox_dir: str | Path, cfg, *, config_dir: str = "") -> SandboxPolicy:
    """Policy for one eval trial: write the trial dir, nothing else that matters.

    ``config_dir`` is the agent's own config directory, which it needs writable
    to run at all (session state, caches). It is allowed as a whole and then
    holed out by ``deny_write``, because ``denyWrite`` takes precedence over
    ``allowWrite`` — the reverse of the read rules, which is easy to get
    backwards.
    """
    allow = [str(Path(sandbox_dir).resolve()), tempfile.gettempdir(), "/private/tmp"]
    if config_dir:
        allow.append(str(Path(config_dir).expanduser()))
    return SandboxPolicy(
        allow_write=tuple(dict.fromkeys(allow)),
        deny_write=instruction_paths_to_protect(cfg),
        allowed_domains=tuple(cfg.eval_sandbox_allowed_domains),
    )


def write_settings(policy: SandboxPolicy, path: str | Path) -> Path:
    """Materialize the policy where srt can read it. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(policy.to_settings(), indent=2), encoding="utf-8")
    return p


def wrap_argv(argv: list[str], settings_path: str | Path, cfg) -> list[str]:
    """Prefix a command with the sandbox runtime.

    ``--settings`` is always explicit. The package version is pinned in config
    because srt is a 0.0.x research preview whose configuration format is
    documented as liable to change, and this is the boundary the eval gate's
    safety rests on.

    ``--`` terminates srt's own options. Without it srt's parser claims flags
    that belong to the WRAPPED command and answers them itself, exiting 0.
    Measured 2026-09-04 against the pinned 0.0.73:

        srt --settings S /bin/echo    --version -> "1.0.0"
        srt --settings S <codex>      --version -> "1.0.0"
        srt --settings S <node>       --version -> "1.0.0"
        srt --settings S -- <codex>   --version -> "codex-cli 0.147.0"

    Three programs reporting one version is the tell: none of them ran.
    ``--settings X`` in the wrapped command was worse — exit 1, no output.

    srt's options are -V/--version, -d/--debug, -s/--settings, -c,
    --control-fd and -h/--help. None collides with what ``llm.py`` sends the
    agents today, so this fixes no live bug; it closes a seam where a future
    flag would be swallowed silently and look like a successful call.
    """
    return [
        cfg.npx_path,
        "-y",
        cfg.eval_sandbox_npx_package,
        "--settings",
        str(settings_path),
        "--",
        *argv,
    ]


def escape_target_for(policy: SandboxPolicy, *, home: str | Path | None = None) -> Path:
    """A path the sandbox MUST refuse: outside every ``allowWrite`` entry.

    Deriving this from the probe's own directory looks obvious and is wrong.
    ``policy_for_trial`` allows ``tempfile.gettempdir()`` outright, because the
    agent needs a temp directory, and the default probe root is
    ``tempfile.mkdtemp()`` — which lives inside it. So the "escape" landed on a
    path the policy explicitly permits, the sandbox correctly permitted it, and
    the probe reported ``SANDBOX DID NOT CONTAIN THE PROBE``. Measured
    2026-08-22 by calling ``verify_boundary(cfg)`` with no ``probe_root``.

    The home directory is the candidate because it is writable, is somewhere a
    real escape would plausibly land, and is never in ``allow_write``: the
    agent's config dir under it is allowed, the directory itself is not.

    Raises rather than returning a path inside an allowed tree. A probe that
    cannot fail is not a test, and reporting its result as a containment
    breach would be a claim about the sandbox when the truth is a claim about
    the probe.
    """
    root = Path(home).expanduser() if home is not None else Path.home()
    # The suffix is load-bearing for readability in logs and stderr tails.
    candidate = root / f".si-sandbox-escape-{uuid.uuid4().hex[:8]}-OUTSIDE-must-not-exist.txt"
    resolved = candidate.resolve()
    for allowed in policy.allow_write:
        a = Path(allowed).resolve()
        if resolved == a or a in resolved.parents:
            raise SandboxError(
                "no valid escape target: every candidate path sits inside an "
                f"allowWrite entry ({candidate} is under {a}). The policy is "
                "too wide to prove containment, so this probe would pass "
                "vacuously. Narrow allowWrite before trusting the sandbox."
            )
    return candidate


def verify_boundary(cfg, *, probe_root: str | Path | None = None) -> dict:
    """Prove the sandbox refuses an escape, or raise.

    Runs two writes through the real runtime: one inside the allowed directory
    that must succeed, and one outside that must fail. Both directions matter.
    A sandbox that refuses everything would also "pass" an escape-only check
    while making every trial fail for a reason unrelated to the rule.

    Returns a dict of what it observed. Raises :class:`SandboxError` on any
    outcome other than allowed-inside and refused-outside.
    """
    root = Path(probe_root) if probe_root else Path(tempfile.mkdtemp(prefix="si-sandbox-probe-"))
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    inside = work / "inside.txt"

    # Use the same policy as a trial, including its deny list. A simpler policy
    # would not test the entries that can refuse an otherwise allowed write.
    policy = policy_for_trial(work, cfg, config_dir=agent_config_dir())
    # The escape target is chosen AGAINST that policy, not from the probe's own
    # directory. See escape_target_for for why the obvious choice is invalid.
    outside = escape_target_for(policy)
    settings = write_settings(policy, root / "probe-settings.json")
    script = f"echo in > {inside} ; echo out > {outside}"
    argv = wrap_argv(["/bin/sh", "-c", script], settings, cfg)

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=cfg.eval_sandbox_probe_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise SandboxError(
            f"sandbox runtime not runnable ({cfg.npx_path} "
            f"{cfg.eval_sandbox_npx_package}): {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(
            f"sandbox probe timed out after {cfg.eval_sandbox_probe_timeout_seconds}s"
        ) from exc

    escaped = outside.exists()
    if escaped:
        # A real breach has just written a file outside the sandbox. Record it,
        # then remove it: leaving litter in the operator's home is not part of
        # reporting a failure.
        outside.unlink(missing_ok=True)
    observed = {
        "inside_written": inside.exists(),
        "outside_written": escaped,
        "escape_target": str(outside),
        "returncode": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-400:],
    }
    if observed["outside_written"]:
        raise SandboxError(
            "SANDBOX DID NOT CONTAIN THE PROBE: a write outside the allowed "
            f"directory succeeded at {outside}. Refusing to run eval trials. "
            f"stderr: {observed['stderr_tail']}"
        )
    if not observed["inside_written"]:
        raise SandboxError(
            "sandbox refused an ALLOWED write, so every trial would fail for a "
            "reason unrelated to the rule under test. "
            f"returncode={proc.returncode} stderr: {observed['stderr_tail']}"
        )
    return observed


def agent_config_dir() -> str:
    """The config directory the eval agent will run under.

    Read from the environment rather than guessed. Setting CLAUDE_CONFIG_DIR to
    a directory that has never been logged in scaffolds an empty account, so
    the eval agent inherits whichever account the run already uses.
    """
    return os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
