"""launchd integration for the nightly self-improve run.

Renders, installs, uninstalls, and inspects the ``com.tonygwu.self-improve``
LaunchAgent using the modern ``launchctl bootstrap gui/<uid>`` API (never the
legacy ``load``/``unload``). Every operation returns evidence text — the exact
commands run, their exit codes, and their output — never a bare success claim.

The plist content rendered here is the single source of truth. The shipped
``ops/com.tonygwu.self-improve.plist`` is a non-installable example with placeholder
paths. Installation always renders the operator's configured paths.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .config import Config

LABEL = "com.tonygwu.self-improve"

AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

def nightly_script_path(cfg: Config) -> Path:
    """The run-nightly.sh launchd must execute: the PRODUCTION checkout's copy.

    Automation runs from ``cfg.production_repo_path`` (repo-prod), never from a
    checkout an agent might be editing. The path is absolute because launchd's
    PATH is minimal. Derive the script path from the configured checkout so a
    repository move does not leave the installed job pointing at an old path.
    """
    return Path(cfg.production_repo_path).expanduser() / "ops" / "run-nightly.sh"


def plist_content(cfg: Config) -> dict:
    """The LaunchAgent plist as a dict; the single source of truth.

    launchd runs jobs with a minimal PATH (no ~/.local/bin, no
    /opt/homebrew/bin), so every path here is absolute.
    """
    logs = Path(cfg.state_dir).expanduser() / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(nightly_script_path(cfg))],
        "StartCalendarInterval": {"Hour": 2, "Minute": 30},
        "StandardOutPath": str(logs / "nightly.stdout.log"),
        # launchd requires StandardErrorPath. A misspelled key can parse as valid
        # plist XML while leaving stderr without the intended destination.
        "StandardErrorPath": str(logs / "nightly.stderr.log"),
        "RunAtLoad": False,
    }

_LAUNCHCTL = "/bin/launchctl"
_PLUTIL = "/usr/bin/plutil"

# Injectable command runner so tests exercise install/uninstall/status logic
# without ever touching the real launchd (the build must not install).
RunCmd = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class LaunchdError(Exception):
    """Raised when a launchd operation fails; the message carries the evidence."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _evidence(result: subprocess.CompletedProcess) -> str:
    """Format one executed command as evidence: the command, rc, and output."""
    args = result.args
    cmd = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
    parts = [f"$ {cmd}", f"rc={result.returncode}"]
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out:
        parts.append(out)
    if err:
        parts.append(err)
    return "\n".join(parts)


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def render_plist(cfg: Config | None = None) -> str:
    """Render the LaunchAgent plist XML.

    Paths come from cfg, or Config's defaults. The checked-in example uses
    explicit placeholder paths and is not an installed job definition.
    """
    return plistlib.dumps(plist_content(cfg or Config()), sort_keys=True).decode("utf-8")


def install(
    cfg: Config,
    *,
    run_cmd: RunCmd = _run,
    agent_plist_path: Path | None = None,
) -> str:
    """Install and bootstrap the LaunchAgent; return evidence text.

    Steps: ensure the log directory exists (launchd creates log files but not
    their directories), render the configured plist, ``plutil -lint`` it, copy it into
    ``~/Library/LaunchAgents/``, boot out any already-registered copy, then
    ``launchctl bootstrap gui/<uid>`` and verify registration with
    ``launchctl print gui/<uid>/<label>``. Any failing step raises
    :class:`LaunchdError` with the evidence collected so far.
    """
    dst = agent_plist_path or AGENT_PLIST
    domain = _gui_domain()
    target = f"{domain}/{LABEL}"
    steps: list[str] = []

    # Registration alone does not prove that ProgramArguments names an executable
    # script. Check that configured target before installing the job.
    script = nightly_script_path(cfg)
    if not script.is_file():
        raise LaunchdError(
            f"nightly script missing: {script}\n"
            f"cfg.production_repo_path = {cfg.production_repo_path}\n"
            "The LaunchAgent would register and then fail every night. "
            "Create the production checkout, or point production_repo_path at one."
        )
    if not os.access(script, os.X_OK):
        raise LaunchdError(f"nightly script is not executable: {script}")
    steps.append(f"verified nightly script exists and is executable: {script}")

    logs_dir = cfg.state_path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    steps.append(f"ensured log directory exists: {logs_dir}")

    # Render from cfg rather than copying the checked-in file, so the installed
    # job always names this machine's production checkout. The checked-in plist
    # is only an example; installing does not read it or require it to exist.
    rendered = render_plist(cfg)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".plist", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(rendered)
        staged = Path(fh.name)
    try:
        lint = run_cmd([_PLUTIL, "-lint", str(staged)])
        steps.append(_evidence(lint))
        if lint.returncode != 0:
            raise LaunchdError(
                "plutil -lint failed; not installing:\n" + "\n".join(steps)
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged, dst)
    finally:
        staged.unlink(missing_ok=True)
    steps.append(f"rendered plist from config -> {dst}")

    probe = run_cmd([_LAUNCHCTL, "print", target])
    if probe.returncode == 0:
        steps.append(f"{target} already registered; booting out before re-bootstrap")
        bootout = run_cmd([_LAUNCHCTL, "bootout", target])
        steps.append(_evidence(bootout))
        if bootout.returncode != 0:
            raise LaunchdError(
                "bootout of existing registration failed:\n" + "\n".join(steps)
            )
    else:
        steps.append(f"{target} not yet registered (launchctl print rc={probe.returncode})")

    bootstrap = run_cmd([_LAUNCHCTL, "bootstrap", domain, str(dst)])
    steps.append(_evidence(bootstrap))
    if bootstrap.returncode != 0:
        raise LaunchdError("launchctl bootstrap failed:\n" + "\n".join(steps))

    verify = run_cmd([_LAUNCHCTL, "print", target])
    steps.append(_evidence(verify))
    if verify.returncode != 0:
        raise LaunchdError(
            "bootstrap exited 0 but launchctl print does not show the job — "
            "registration NOT verified:\n" + "\n".join(steps)
        )
    return "\n".join(steps)


def uninstall(
    cfg: Config,
    *,
    run_cmd: RunCmd = _run,
    agent_plist_path: Path | None = None,
) -> str:
    """Boot out the LaunchAgent and remove its plist; return evidence text.

    Raises :class:`LaunchdError` if there was nothing to uninstall (neither a
    live registration nor a plist file) or if the bootout itself fails.
    """
    dst = agent_plist_path or AGENT_PLIST
    target = f"{_gui_domain()}/{LABEL}"
    steps: list[str] = []

    probe = run_cmd([_LAUNCHCTL, "print", target])
    registered = probe.returncode == 0
    if registered:
        bootout = run_cmd([_LAUNCHCTL, "bootout", target])
        steps.append(_evidence(bootout))
        if bootout.returncode != 0:
            raise LaunchdError("launchctl bootout failed:\n" + "\n".join(steps))
    else:
        steps.append(f"{target} not registered (launchctl print rc={probe.returncode})")

    if dst.exists():
        dst.unlink()
        steps.append(f"removed {dst}")
        removed = True
    else:
        steps.append(f"no plist at {dst}")
        removed = False

    if not registered and not removed:
        raise LaunchdError("nothing to uninstall:\n" + "\n".join(steps))
    return "\n".join(steps)


def status(*, run_cmd: RunCmd = _run) -> str:
    """Report whether the LaunchAgent is registered; return evidence text.

    Not-registered is a valid status, not an error, so this never raises for
    a missing job — the headline plus the raw ``launchctl print`` output is
    returned either way.
    """
    target = f"{_gui_domain()}/{LABEL}"
    result = run_cmd([_LAUNCHCTL, "print", target])
    headline = (
        f"{LABEL}: registered in {_gui_domain()}"
        if result.returncode == 0
        else f"{LABEL}: NOT registered in {_gui_domain()}"
    )
    return headline + "\n" + _evidence(result)


def worker_plist_content(cfg: Config) -> dict:
    """Reviewable worker service definition; this function installs nothing."""
    logs = cfg.state_path('logs')
    return {
        'Label': 'com.self-improve.worker',
        'ProgramArguments': ['/bin/bash', str(Path(cfg.production_repo_path).expanduser() / 'ops' / 'run-worker.sh')],
        'StandardOutPath': str(logs / 'worker.stdout.log'),
        'StandardErrorPath': str(logs / 'worker.stderr.log'),
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 5,
    }


def render_worker_plist(cfg: Config) -> str:
    return plistlib.dumps(worker_plist_content(cfg), sort_keys=False).decode('utf-8')
