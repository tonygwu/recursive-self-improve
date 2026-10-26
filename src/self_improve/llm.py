"""Provider-agnostic headless LLM runner.

Every call is routed by ``quota_router.select_account(model=<class>)`` — the
quotapick CLI's own decision path imported as a library (hard dependency,
pinned by revision in pyproject; no CLI fallback) — among all allowed pools
(the configured Claude accounts via CLAUDE_CONFIG_DIR, or Codex); the
winning provider is invoked through one of two adapters sharing a single
interface:

- claude: ``claude -p --model <m> --output-format json``, prompt on stdin.
- codex:  ``codex exec --json --skip-git-repo-check -m <m> <prompt>`` with
  stdin=DEVNULL (codex exec reads inherited stdin and hangs on a live one).

Source installs run with cwd = this repo so miner sessions index under a real slug in
``~/.claude/projects`` / ``~/.codex/sessions`` and get mined next run.

Adapter contracts exercised by the library tests and invented envelope fixtures
in ``tests/fixtures/llm/``:

- quota_router ``Selection`` (contract_version 1): frozen dataclass
  with ``provider``/``account`` (None when no account won),
  ``fits``/``meets_policy``/``available_at`` (None means the key was
  absent), ``exec_env`` (env overlay dict; FREQUENTLY {} and that is
  CORRECT — the default Claude account is selected by the ABSENCE of
  CLAUDE_CONFIG_DIR, so the overlay is merged, never required non-empty),
  and ``to_dict()`` — the full ``pick --json`` payload verbatim, retained
  as the pickN.json audit artifact. Semantics: ``fits`` is a
  total-exhaustion signal only (can this account physically serve a call);
  ``meets_policy`` is THE field to gate on (did the pick satisfy the
  constraints supplied); ``available_at`` is epoch SECONDS (float),
  present/meaningful when ``meets_policy`` is false — when the policy bar
  will clear. ``select_account`` does NOT raise on routing failure (it
  returns a degraded Selection), but it CAN raise (ConfigError, bugs) —
  those map to the same failure path the pick subprocess errors used.
  ``env`` must still be passed explicitly (``env=dict(os.environ)``): the
  library defaults it to ``{}`` while the CLI reads ``os.environ``.
- ``ranked``/``excluded`` rows carry ``remaining`` as a fraction OR null,
  and null means UNKNOWN, not 0.0 — an account whose usage could not be
  read (expired OAuth token). Nothing here does arithmetic on it; the
  payload is retained verbatim and only ``degraded`` is inspected, for the
  ``"unreadable:"`` reason (see ``_unreadable_from``). The library keeps an
  unreadable account out of BOTH the ranked set and the fallback pool, so
  it can never be picked; that guarantee is a dependency's, so it is pinned
  by a test against the real library rather than assumed.
- claude envelope: single JSON object with ``type:"result"``, ``is_error``,
  ``result`` (assistant text), ``session_id``, ``usage.{input,output}_tokens``
  and ``modelUsage`` keyed by concrete model id. ``modelUsage`` can contain
  MORE than one model (a haiku sidecar rides along), so identity assertion
  searches every key/canonicalModel for the requested token.
- codex ``--json`` stdout: JSONL events ``thread.started`` {thread_id},
  ``turn.started``, ``item.completed`` {item.type=="agent_message", text},
  ``turn.completed`` {usage}. The stream carries NO model field, so identity
  is read from the session rollout file (found by thread_id under the invoked
  account's ``CODEX_HOME/sessions``, or ``codex_sessions_dir`` when no home
  override is present): the last ``turn_context`` record's
  ``payload.model``. If that cannot be read, the call fails as
  ``model_mismatch`` — identity is never silently assumed.

Fail-loud policy notes (deliberate choices, surfaced here rather than buried):

- Budget is charged one unit per ``call()``/``call_agentic()`` at entry;
  an agentic session is ONE unit (one invocation, though multi-turn
  inside), and internal retries
  (oauth retry, quota re-pick) do not consume extra units. Refused calls
  raise :class:`BudgetExhausted`, are counted per tier, and appear in
  ``stats()``. Refusal raises before an llm_calls row is created.
- ``tokens_in``/``tokens_out`` are telemetry copied verbatim from the
  envelope (claude ``usage.input_tokens``/``output_tokens`` — excludes cache
  reads/writes; codex ``turn.completed.usage``). A success envelope missing
  usage records 0s; token counts never gate correctness.
- On success after a quota re-pick, the llm_calls ``error`` column carries
  ``"repicked_from=<account>"`` so the re-pick is reported, not silent.
- A pick with ``fits`` true but ``meets_policy`` false is NOT a green
  light. If ``available_at`` is in the future and within
  ``cfg.quota_wait_max_seconds``, the runner sleeps ONCE until it (logged
  via logger.info and counted in ``stats()["policy_waits"]``), then takes
  one fresh pick — the pre-sleep pick is stale — and proceeds only if
  that fresh pick meets policy. Anything else (wait beyond the cap,
  available_at absent/past, or a second not-meets-policy pick) fails as
  ``quota_exhausted``; there is never a second sleep. The fast quota
  re-pick path (after a provider-reported quota failure) never sleeps:
  a not-meets-policy re-pick there is treated as no capacity.
- Every ``quota_exhausted`` message derived from a pick names the accounts
  the router could not READ, verbatim, when there are any. The outcome stays
  ``quota_exhausted`` (no call can be served either way, and a new outcome
  would ripple into store.py's taxonomy and the reports) but "exhausted" and
  "unreadable" want opposite responses — a wait vs a login — and a dark
  account has no window to reset, so the bare message would send an operator
  to wait forever. This is diagnostic only: it never changes which account is
  chosen or whether a call proceeds.
- ``expect_json`` requires a top-level JSON *object* (optionally inside a
  single ```` ```json ```` fence). When that strict parse fails, ONE narrow
  recovery is attempted: a string-aware brace scan of the text (double-quoted
  strings and backslash escapes respected, so braces inside string values
  never count). Iff EXACTLY ONE balanced top-level ``{...}`` span parses to
  a dict, it is accepted and the call SUCCEEDS with outcome
  ``parse_recovered`` — a success outcome kept DISTINCT from ``ok`` (and
  counted as succeeded in ``stats()`` and reports) so the frequency of
  prose-wrapped contract JSON stays visible instead of dissolving into
  ``ok``. Zero candidates, or two or more, keep ``parse_error`` with the raw
  retained — ambiguity is never guessed at.
- ``call_agentic()`` reuses this whole machinery; the llm_calls schema is
  unchanged — agentic-ness is encoded in the stage string the caller
  passes (e.g. ``"mine_agentic"``), not in a new column.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import quota_router
from quota_router import select_account

#: Router features this module's routing correctness depends on, as
#: ``submodule -> what breaks without it``.
#:
#: ``quota_router.reserve`` applies ``manual_rate_per_day`` from the operator's
#: config, which holds part of an account's weekly pool back for interactive use.
#: A router predating it does not error on the key -- it ignores it, returns a
#: well-formed pick, and names the reserved account. Nothing downstream can tell
#: that pick from a correct one, so the miner spends quota reserved for
#: interactive use and no signal says so.
#:
#: This is asserted rather than trusted because the dependency is PINNED to a
#: commit, and a pinned commit can predate the reserve.
_REQUIRED_ROUTER_FEATURES: dict[str, str] = {
    "reserve": (
        "manual_rate_per_day is ignored, so a pick can name an account whose "
        "weekly pool is reserved for the operator's interactive use"
    ),
}


def assert_router_conformance(module=quota_router) -> None:
    """Raise when the installed quota_router is too old to route correctly here.

    Checked against the imported package rather than its version string: the
    version is static across many commits, so it cannot answer this question.
    """
    import importlib.util

    def absent(name: str) -> bool:
        # find_spec RAISES ModuleNotFoundError when the PARENT package is gone,
        # and returns None only when the parent imports and the child does not.
        # Both mean the feature is unavailable here, so both are absent.
        try:
            return importlib.util.find_spec(f"{module.__name__}.{name}") is None
        except ImportError:
            return True

    missing = [
        f"quota_router.{name} ({why})"
        for name, why in _REQUIRED_ROUTER_FEATURES.items()
        if absent(name)
    ]
    if not missing:
        return
    raise RuntimeError(
        "installed quota_router is missing "
        + "; ".join(missing)
        + f". Loaded from {getattr(module, '__file__', '?')}. Bump the "
        "llm-quota-router pin in pyproject.toml and re-run `uv sync`."
    )


assert_router_conformance()

from .config import Config
from .store import Store, new_id, utc_now_iso, LLM_SUCCESS_OUTCOMES

logger = logging.getLogger(__name__)

# Source installs run adapters from their checkout. Installed wheels use the
# caller's working directory unless the invocation supplies an explicit cwd.
from .resources import SOURCE_ROOT as REPO_ROOT


# How long an exited provider CLI may leave descendants holding its output
# pipes before the whole process group is killed.
_DESCENDANT_GRACE_SECONDS = 5.0

# How often the provider guard checks that the caller is still alive. If the
# caller dies without cleanup (SIGKILL, for example launchd killing a job's
# process group), the provider group outlives it by at most about this long
# plus _TERM_GRACE_SECONDS (below), about 2.2 s in all.
_GUARD_POLL_SECONDS = 0.2

# How long a CLI that is still running has to exit after SIGTERM to its group,
# before SIGKILL. This applies at a timeout, at an interrupt or SIGTERM/SIGHUP
# to the caller, and in the guard when the caller dies. The real CLIs start
# their tool commands outside the group: claude starts its Bash tool shell and
# its hooks with setsid, and codex runs tools in process groups of their own.
# Only the CLI's own SIGTERM shutdown ends those, so SIGKILL alone left an eval
# agent's command running and writing into the sandbox. The wait ends when the
# guard reports the CLI's exit, so a CLI that exits promptly costs almost
# nothing. A CLI that ignores SIGTERM costs the full 2 s. That covers a Node
# CLI's handler on a loaded machine (it runs on the next event-loop turn), and
# it keeps the whole cleanup of a stopped caller (this wait, then the drain and
# reap waits below) near 4 s, so a service manager's own SIGKILL deadline is
# unlikely to cut it short. A CLI that needs longer is still killed at 2 s:
# claude 2.1.302 allows its own shutdown 5 s or more (session-end hooks and
# flushes), so its later steps can be cut short.
_TERM_GRACE_SECONDS = 2.0

# How long a call waits to reap the guard after SIGKILL to its group.
_REAP_SECONDS = 1.0

# After the group is killed, how long the output readers together may take to
# reach end of file. A reader still blocked after this names a pipe that a
# process outside the group (one that called setsid) still holds.
_DRAIN_SECONDS = 1.0

# The condition logged, and named in ProviderResult.held_streams, when that
# happens. The escaped process keeps running and its later output is lost.
HELD_OUTPUT_CONDITION = "output held open by a process outside the provider group"

# Signals that end the caller but do not reach the provider, because the
# provider runs in its own session. While a call runs in the main thread, each
# one becomes SystemExit(128 + signal), so the cleanup kills the provider group.
_CALLER_END_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class ProviderGuardError(RuntimeError):
    """The process that supervises a provider CLI failed, so the CLI's outcome
    is unknown. This is an infrastructure failure, never a model outcome."""


class ProviderResult(subprocess.CompletedProcess):
    """A finished provider call. ``held_streams`` names each output pipe
    ("stdout", "stderr") that a process outside the provider group still held
    when the call stopped reading (``HELD_OUTPUT_CONDITION``). The group kill
    cannot reach that process, so it keeps running, and anything it writes to
    the pipe later is not captured. The tuple is empty in the normal case."""

    def __init__(self, args, returncode, stdout, stderr, held_streams: tuple[str, ...]):
        super().__init__(args, returncode, stdout, stderr)
        self.held_streams = held_streams


# The provider guard is the leader of the provider's session and process group.
# It starts the CLI as its child in the same group, reports the CLI's exit
# status on the status pipe, and stays alive until the caller kills the group.
# It polls for the caller's death (its parent pid changes) and then ends its
# own group: SIGTERM, up to _TERM_GRACE_SECONDS for the CLI to exit, then
# SIGKILL. So a caller that dies without cleanup cannot leave the provider
# running. It runs with ``-I -S`` and imports only the standard library.
# The CLI shares its group, so a CLI that signals its own group (a shell's
# `trap 'kill 0' EXIT`) also signals the guard. The guard catches each signal
# in _GUARD_SURVIVES with a no-op Python handler, and Python itself ignores
# SIGPIPE and SIGXFSZ. A caught signal resets to its default in the CLI at
# exec, so the CLI's signal dispositions are the ones it had before the guard
# existed. A signal the caller ignores stays ignored, as before.
# Every other signal whose default action ends a process also ends the guard
# when the CLI sends it to its own group: SIGKILL; the fault signals SIGSEGV,
# SIGBUS, SIGILL and SIGFPE, which a no-op handler must not catch because a
# real fault would repeat forever; SIGABRT, SIGTRAP, SIGSYS and, on macOS,
# SIGEMT; and on Linux also SIGIO (SIGPOLL), SIGPWR, SIGSTKFLT and the
# real-time signals. The guard then usually dies before it reports the CLI's
# exit, so the call raises ProviderGuardError where a direct Popen returned
# the CLI's status, and a code-grader check that runs `kill -ABRT 0` is a
# grader_error instead of a graded fail. Now and then the guard's watch thread
# reports the CLI's status first (up to about 8 in 100 runs of `kill -KILL 0`
# from /bin/sh on a loaded Mac), and the call returns that status.
_GUARD_SURVIVES = ("SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM", "SIGUSR1", "SIGUSR2",
                   "SIGALRM", "SIGVTALRM", "SIGPROF", "SIGXCPU", "SIGTSTP")

# Errors from starting the guard that mean the interpreter itself cannot run.
# Any other error (E2BIG for an argv over the OS limit, ENOMEM, EAGAIN) is
# about this spawn, so it stays the OSError that _invoke records as
# spawn_error.
_GUARD_UNUSABLE_ERRNOS = frozenset(
    {errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM, errno.ENOEXEC, errno.ELOOP}
)
_GUARD_SOURCE = """\
import json, os, signal, subprocess, sys, threading, time
status, caller = int(sys.argv[1]), int(sys.argv[2])
poll, grace = float(sys.argv[3]), float(sys.argv[4])
for name in sys.argv[5].split(","):
    signum = getattr(signal, name)
    if signal.getsignal(signum) is not signal.SIG_IGN:
        signal.signal(signum, lambda signum, frame: None)
def report(**event):
    os.write(status, json.dumps(event).encode() + b"\\n")
if os.getppid() != caller:
    os.killpg(0, signal.SIGKILL)
try:
    cli = subprocess.Popen(sys.argv[6:])
except OSError as exc:
    report(event="spawn_error", errno=exc.errno, strerror=exc.strerror, filename=exc.filename)
    sys.exit(0)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
exited = threading.Event()
def watch():
    returncode = cli.wait()
    exited.set()
    report(event="exited", returncode=returncode)
threading.Thread(target=watch, daemon=True).start()
while os.getppid() == caller:
    time.sleep(poll)
os.killpg(0, signal.SIGTERM)
exited.wait(grace)
os.killpg(0, signal.SIGKILL)
"""


def _signal_group(proc: subprocess.Popen, signum: int) -> None:
    """Signal the guard's group. Callers do this only while the guard is
    unreaped: after that, its pid and so the group id can be reused."""
    try:
        os.killpg(proc.pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _end_group(proc: subprocess.Popen, status_r: int, cli_may_run: bool) -> None:
    """Kill the provider group. While the CLI may still run, send SIGTERM
    first, so the CLI can end the tool commands it started in other sessions,
    and wait up to ``_TERM_GRACE_SECONDS`` for it to exit. Then SIGKILL, also
    when a second signal interrupts that wait.

    The wait ends when the status pipe turns readable: the guard writes the
    CLI's exit report there, and the pipe reaches end of file when the guard
    has died. The wait reads nothing, so it cannot fail on a report. After a
    CLI exit or a spawn error, nothing waits: the CLI is gone. A lost guard
    (one that died before it reported) has closed the pipe, so SIGKILL
    follows SIGTERM at once and a CLI that outlived its guard gets no grace."""
    try:
        if cli_may_run:
            _signal_group(proc, signal.SIGTERM)
            with selectors.DefaultSelector() as selector:
                selector.register(status_r, selectors.EVENT_READ)
                selector.select(_TERM_GRACE_SECONDS)
    finally:
        _signal_group(proc, signal.SIGKILL)


@contextlib.contextmanager
def _caller_signals_end_the_call():
    """Turn each signal in ``_CALLER_END_SIGNALS`` into SystemExit for the
    duration of a call, then restore the previous handlers.

    Signal handlers can only be set from the main thread; a call from another
    thread relies on the guard alone. A signal the process ignores stays
    ignored, so a run under ``nohup`` still survives a hangup. A handler that
    was not set from Python (``getsignal`` returns None) cannot be restored,
    so it is left in place."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous: dict[int, object] = {}
    try:
        for signum in _CALLER_END_SIGNALS:
            current = signal.getsignal(signum)
            if current is signal.SIG_IGN or current is None:
                continue
            previous[signum] = signal.signal(signum, _terminate)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _await_guard(status_r: int, deadline: float) -> tuple[str, int | OSError | None]:
    """Wait for the guard's report of the CLI's exit.

    Returns ``("exited", returncode)``, ``("spawn_error", OSError)`` when the
    CLI could not start, ``("timeout", None)`` at the deadline, or
    ``("lost", None)`` when the guard ended without a report."""
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(status_r, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return "timeout", None
            data = os.read(status_r, 4096)
            if not data:
                return "lost", None
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    event = json.loads(line)
                    kind = event["event"]
                    if kind == "exited":
                        return "exited", int(event["returncode"])
                    if kind == "spawn_error":
                        # The same exception subprocess.Popen raises itself.
                        return "spawn_error", OSError(
                            event["errno"], event["strerror"], event["filename"]
                        )
                    raise ValueError(f"unknown event {kind!r}")
                except (ValueError, KeyError, TypeError) as exc:
                    raise ProviderGuardError(
                        f"unreadable provider guard report {line!r}: {exc}"
                    ) from None


def _run_provider(
    argv: list[str],
    *,
    timeout: float,
    env: dict | None = None,
    cwd: str | None = None,
    input: bytes | None = None,
    stdin=None,
    capture_output: bool = True,
    grace_seconds: float = _DESCENDANT_GRACE_SECONDS,
) -> ProviderResult:
    """``subprocess.run`` for provider CLIs, bounded at the process-group level.

    ``subprocess.run`` kills only its direct child on timeout, so an agent's
    descendants (an srt or npx wrapper's CLI, a dev server it started) kept
    running and spending quota. It also waits for every holder of the output
    pipes, so a background child turned a finished call into a timeout. The
    CLI runs in its own session under a guard process (``_GUARD_SOURCE``);
    when it exits or times out, the whole group is killed. SIGTERM, SIGHUP or
    an interrupt during the call also kill the group, and the guard kills it
    when the caller dies without cleanup. Each of these paths (a timeout,
    SIGTERM, SIGHUP, an interrupt, the caller's death) sends SIGTERM first and
    waits up to ``_TERM_GRACE_SECONDS`` for the CLI to exit, so the CLI can end
    the tool commands it started in other sessions. That can add up to that
    long to the call. After the CLI exits, both output
    pipes share one grace period (``grace_seconds``) and, after the group
    kill, one drain wait (``_DRAIN_SECONDS``). A pipe still held after that is
    named in ``ProviderResult.held_streams`` and logged as
    ``HELD_OUTPUT_CONDITION``. Raises ``subprocess.TimeoutExpired`` with the
    output so far, ``OSError`` when the CLI cannot start, and
    ``ProviderGuardError`` when the guard fails.
    """
    if not capture_output:
        raise ValueError("provider calls always capture output")
    if input is not None and stdin is not None:
        raise ValueError("stdin and input arguments may not both be used")
    if not argv:
        raise ValueError("a provider call needs a command")
    if not sys.executable:
        raise ProviderGuardError("sys.executable is empty, so the provider guard cannot start")
    deadline = time.monotonic() + timeout
    # Install the handlers before the spawn, so no signal can arrive while the
    # provider runs and the handlers are not yet in place.
    with _caller_signals_end_the_call():
        status_r, status_w = os.pipe()
        try:
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _GUARD_SOURCE,
                     str(status_w), str(os.getpid()), str(_GUARD_POLL_SECONDS),
                     str(_TERM_GRACE_SECONDS), ",".join(_GUARD_SURVIVES), *argv],
                    stdin=subprocess.PIPE if input is not None else stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=cwd,
                    start_new_session=True,
                    pass_fds=(status_w,),
                )
            except OSError as exc:
                # Popen names the executable it could not run, or the cwd it
                # could not enter. Only an unusable interpreter is a guard
                # failure; a missing cwd or an argv over the OS limit stays
                # the spawn error it always was.
                if exc.filename == sys.executable and exc.errno in _GUARD_UNUSABLE_ERRNOS:
                    raise ProviderGuardError(
                        f"the provider guard could not start {sys.executable}: {exc}"
                    ) from exc
                raise
            finally:
                os.close(status_w)
            try:
                return _supervise(proc, argv, status_r, deadline, timeout, input, grace_seconds)
            except BaseException:
                # For an interrupt that lands before _supervise's own cleanup
                # is in place. Once the guard is reaped its pid, and so the
                # group id, can be reused, so never signal it after that.
                if proc.returncode is None:
                    try:
                        _end_group(proc, status_r, cli_may_run=True)
                    except BaseException:  # noqa: BLE001 - the original error wins
                        pass
                    try:
                        proc.wait(timeout=_REAP_SECONDS)
                    except BaseException:  # noqa: BLE001 - the original error wins
                        pass
                raise
        finally:
            os.close(status_r)


def _supervise(
    proc: subprocess.Popen,
    argv: list[str],
    status_r: int,
    deadline: float,
    timeout: float,
    input: bytes | None,
    grace_seconds: float,
) -> ProviderResult:
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}

    def drain(stream, key: str) -> None:
        try:
            for data in iter(lambda: stream.read1(65536), b""):
                chunks[key].append(data)
        except (OSError, ValueError):
            pass
        finally:
            # At end of file, also after an interrupt that skipped collect().
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, "err"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if input is not None:
        def feed() -> None:
            try:
                proc.stdin.write(input)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=feed, daemon=True).start()

    def collect() -> tuple[bytes, bytes, tuple[str, ...]]:
        # One drain deadline for both pipes, so two held pipes cost one wait.
        drain_deadline = time.monotonic() + _DRAIN_SECONDS
        held = []
        for name, reader, stream in zip(("stdout", "stderr"), readers, (proc.stdout, proc.stderr)):
            reader.join(timeout=max(0.0, drain_deadline - time.monotonic()))
            # A descendant that left the group with setsid can still hold the
            # pipe. Closing it would wait for the blocked reader's lock, i.e.
            # for that descendant; leave it to the daemon thread instead.
            if reader.is_alive():
                held.append(name)
                continue
            try:
                stream.close()
            except OSError:
                pass
        # Copy each list first: a reader of a held pipe can still append.
        return b"".join(list(chunks["out"])), b"".join(list(chunks["err"])), tuple(held)

    def reap() -> None:
        try:
            proc.wait(timeout=_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            raise ProviderGuardError(
                f"provider guard {proc.pid} was still running {_REAP_SECONDS} s "
                "after SIGKILL to its group"
            ) from None

    outcome = None
    try:
        try:
            outcome, result = _await_guard(status_r, deadline)
            if outcome == "exited":
                # One grace deadline for both pipes, never past the call's deadline.
                grace_deadline = min(time.monotonic() + grace_seconds, deadline)
                for reader in readers:
                    reader.join(timeout=max(0.0, grace_deadline - time.monotonic()))
        finally:
            # Every path: exit, timeout, interrupt, SIGTERM/SIGHUP, guard failure.
            # Only an exit report or a spawn error shows that the CLI is gone.
            _end_group(proc, status_r, cli_may_run=outcome not in ("exited", "spawn_error"))
    except BaseException:
        try:
            reap()  # so no zombie outlives the call
        except BaseException:  # noqa: BLE001 - the original error wins
            pass
        raise
    reap()
    out, err, held = collect()
    if held:
        logger.warning(
            "%s: %s: %s still open %.1f s after the group was killed (%s). "
            "That process keeps running, and its later output is not captured.",
            argv[0], HELD_OUTPUT_CONDITION, " and ".join(held), _DRAIN_SECONDS,
            f"exit status {result}" if outcome == "exited" else outcome,
        )
    if outcome == "spawn_error":
        raise result
    if outcome == "timeout":
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    if outcome == "lost":
        raise ProviderGuardError(
            f"the provider guard for {argv[0]!r} ended with status {proc.returncode} "
            f"before it reported the CLI's exit; stderr: {_clip(_decode(err))}"
        )
    return ProviderResult(argv, result, out, err, held)

# Model-class -> per-provider concrete model/alias handed to the CLI. The
# reported model must CONTAIN this token or the call fails as model_mismatch.
# (Belongs in config.py per the plan; kept here pending that contract change.)
PROVIDER_MODEL_MAP: dict[str, dict[str, str]] = {
    "claude": {"sonnet": "sonnet", "opus": "opus"},
    "codex": {"sonnet": "gpt-5.6-terra", "opus": "gpt-5.6-terra"},
}

# Failure-text classification (checked case-insensitively, and ONLY on
# attempts that already failed — never against successful assistant text).
_OAUTH_PATTERN = "not logged in"
# Recognize session-limit wording in both failure classification and account
# retries. Review these patterns when provider wording changes.
_QUOTA_PATTERNS = (
    "usage limit",
    "limit reached",
    "rate limit",
    "quota",
    "session limit",
)

_FENCE_RE = re.compile(r"\A\s*```(?:json)?[ \t]*\n(.*?)\n?[ \t]*```\s*\Z", re.DOTALL)

# quota_router (>= a600845) reports an account it could not read — an expired
# OAuth access token, typically — as a `degraded` row whose reason begins with
# this prefix, and gives its `remaining` as null. Null is NOT 0.0: the headroom
# is UNKNOWN, and such an account is excluded from both the ranked set and the
# fallback pool, so it can never be picked (pinned by the library-contract tests
# in tests/test_llm.py). What it CAN do is make the whole fleet look exhausted:
# every candidate dark yields fits=false, which we report as quota_exhausted.
# That outcome is right — no call can be served — but on its own it tells the
# operator to wait out a reset, and a dark account has no window to reset. So
# the cause is carried through into the recorded error verbatim.
_UNREADABLE_PREFIX = "unreadable:"

# llm_calls outcome taxonomy, reconciled with store.py. Successes include
# ok, parse_recovered (strict expect_json
# parse failed but exactly one prose-wrapped top-level JSON object was
# recovered — counted separately so prose-wrapping stays visible), and
# oauth_transient_retried (completed after one credential-race retry).
OUTCOMES = (
    "ok",
    "parse_recovered",
    "quota_exhausted",
    "oauth_transient_retried",
    "timeout",
    "parse_error",
    # Empty stdout is distinct from an unparseable reply, even with exit 0.
    "empty_output",
    "model_mismatch",
    "spawn_error",
    # Record turn-cap failures separately so the configured limit can be tuned
    # against explicit evaluation results.
    "max_turns",
    # Invalid credentials require interactive login; an automatic retry cannot
    # repair them. Keep this failure separate so the operator can act on it.
    "auth_failed",
    # OS refusal happens before a model turn and must not look like a verdict.
    "sandbox_denied",
    # We REFUSED to attempt the call, because no account in the fleet belongs
    # to a provider that can run a sandboxed trial. Distinct from
    # `sandbox_denied`, which is the OS refusing a call we did make: this one
    # spends nothing and names a fleet-shaped problem the operator fixes by
    # adding capacity, not by retrying. Without its own bucket a refusal to
    # spend and a burnt call read identically in the report.
    "sandbox_incapable_fleet",
    "other",
)

# A lookup that raises beats a comment asking the next person to remember.
_unknown_success = set(LLM_SUCCESS_OUTCOMES) - set(OUTCOMES)
if _unknown_success:
    raise AssertionError(
        f"LLM_SUCCESS_OUTCOMES names outcomes the call path cannot produce: "
        f"{sorted(_unknown_success)}"
    )


#: `_Attempt.status` values that ARE the outcome, passed straight through.
#: Kept beside OUTCOMES so the two are read together; `_classify` raises on a
#: status that is in neither this tuple nor ("failed", "ok").
_TERMINAL_ATTEMPT_STATUSES = ("timeout", "spawn_error", "parse_error", "empty_output")


_TOOL_USE_STOP_RE = re.compile(r'"stop_reason"\s*:\s*"tool_use"', re.IGNORECASE)

# The shell's command-not-found (127) and found-but-not-executable (126).
# Anchored at the start, because both _parse_claude and _parse_codex build the
# failure text as f"exit {returncode}: ...": a model that merely WROTE "127"
# somewhere in its answer must not be reclassified.
_SPAWN_EXIT_RE = re.compile(r"^exit 12[67]\b")

# The provider's own fatal line, anchored to the start of a LINE. Two things
# it deliberately does NOT key on:
#   * "could not create PATH aliases" — the process prints that as a WARNING
#     and says it is proceeding, so it is not the fault.
#   * "Operation not permitted (os error 1)" alone — EPERM is far too broad
#     and appears in the warning above as well.
# A process-error wrapper can precede the fatal line, while an event-stream
# error can omit that wrapper. Inspect this text only on a failed attempt.
_SANDBOX_DENIED_RE = re.compile(
    r"^Error: failed to initialize in-process app-server client", re.MULTILINE
)

# BOTH markers are required. This text is whatever the failed process wrote,
# and an agent shelling out to git, gh or a package registry can print
# "failed to authenticate" about a credential that is not the one running the
# call. The two real messages are "Failed to authenticate. API Error: 401
# OAuth access token has been revoked." and "Failed to authenticate: OAuth
# session expired and could not be refreshed".
_AUTH_MARKERS = ("failed to authenticate", "oauth")


def classify_failure_text(text: str) -> str:
    """Outcome for a failed agentic attempt, from its response envelope.

    Split out of the call path so the taxonomy is testable against real
    envelopes rather than only through a live subprocess.

    A turn-cap DNF looks like ``is_error`` with ``stop_reason: "tool_use"`` —
    the session was still mid-tool-use when it was cut off, which is exactly
    why nothing came back. ``end_turn`` with an error is an ordinary failure.
    """
    # Whitespace-insensitive: the envelope is compact JSON today, but a
    # pretty-printed one would put a newline between key and value and a
    # space-only strip would silently miss it — falling back to "other" and
    # re-hiding the failure mode this exists to surface.
    if _TOOL_USE_STOP_RE.search(text or ""):
        return "max_turns"
    # A missing executable or interpreter is a spawn failure, not model output.
    if _SPAWN_EXIT_RE.match((text or "").lstrip()):
        return "spawn_error"
    # An OS refusal is a process failure, not a verdict about model behavior.
    if _SANDBOX_DENIED_RE.search(text or ""):
        return "sandbox_denied"
    # Checked AFTER the caller's own oauth branch, which owns the retryable
    # "not logged in" race. A race and a revoked token are different facts.
    low = (text or "").lower()
    if all(m in low for m in _AUTH_MARKERS):
        return "auth_failed"
    return "other"


def _unreadable_from(selection) -> tuple[str, ...]:
    """Accounts this decision could not read, from the payload's ``degraded``.

    ``degraded`` (not ``excluded``) is the source on purpose: quota_router
    appends an unreadable row there for every snapshot as soon as it is
    fetched, before eligibility runs, so it is populated even on the paths
    that never reach the decision layer — whereas ``excluded`` only carries
    the account if it got that far. Reasons are copied VERBATIM; the library
    owns that wording (``quota_router.types.unreadable_reason``) and it
    already states the thing that matters — the quota is unknown rather than
    free — so re-phrasing it here would just be a second thing to keep true.

    Rows that are not well-formed are skipped rather than raised on. This is
    a diagnostic decoration on an ALREADY-failing path; letting a malformed
    ``degraded`` row turn a clear quota_exhausted into a spawn_error would
    lose more information than it protects. Decision-bearing fields
    (fits/meets_policy/available_at) still fail loud in :meth:`_pick`.
    """
    out: list[str] = []
    for row in getattr(selection, "degraded", ()) or ():
        if not isinstance(row, dict):
            continue
        reason = row.get("reason")
        if isinstance(reason, str) and reason.startswith(_UNREADABLE_PREFIX):
            out.append(f"{row.get('account') or '<unknown account>'}: {reason}")
    return tuple(out)


def _with_unreadable(error: str, pick: "_Pick | None") -> str:
    """Append the unreadable-account cause to a quota failure message."""
    if pick is None or not pick.unreadable:
        return error
    return (
        f"{error}; {len(pick.unreadable)} account(s) UNREADABLE — remaining "
        "quota is UNKNOWN, not zero, and no reset clears this: "
        + "; ".join(pick.unreadable)
    )


class BudgetExhausted(Exception):
    """Raised when a call would exceed the per-run cap for its model tier."""

    def __init__(self, tier: str, cap: int, refused_so_far: int):
        self.tier = tier
        self.cap = cap
        self.refused_so_far = refused_so_far
        super().__init__(
            f"{tier} tier budget exhausted (cap={cap}); "
            f"{refused_so_far} request(s) refused this run"
        )


@dataclass(frozen=True)
class LLMResult:
    """Outcome of one LLMRunner.call(); one llm_calls row backs each result."""

    ok: bool
    text: str
    parsed: dict | None
    outcome: str            # one of OUTCOMES
    provider: str           # "claude" | "codex" | "" if never picked
    model_reported: str     # from response telemetry, "" if unavailable
    account: str = ""       # quotapick account id that served (or failed) it
    error: str = ""         # taxonomy detail; "repicked_from=<acct>" on ok-after-repick
    call_id: str = ""       # Exact durable llm_calls row, retained across journal replay.
    provider_attempts: int | None = None  # Unknown for legacy/injected executions.


@dataclass
class _Attempt:
    """One subprocess invocation of a provider CLI, parsed."""

    status: str             # ok|failed|timeout|spawn_error|parse_error
    text: str = ""
    model_reported: str = ""
    identity_ok: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    failure_text: str = ""  # only meaningful when status == "failed"
    error: str = ""


@dataclass
class _Pick:
    """Parsed quotapick decision (contract_version 1)."""

    provider: str
    account: str
    fits: bool          # total-exhaustion signal: can the account serve at all
    meets_policy: bool  # did the pick satisfy the supplied constraints (THE gate)
    available_at: float = 0.0  # epoch seconds the policy bar clears; 0.0 = absent
    env: dict = field(default_factory=dict)
    # "<account>: <verbatim library reason>" for every account the router could
    # not read on this pick. Diagnostic only — never an input to any decision.
    unreadable: tuple[str, ...] = ()


class LLMRunner:
    """Budgeted, audited LLM caller. One instance per pipeline run.

    Calls that pass the budget check insert one ``llm_calls`` row using OUTCOMES
    and retain the full decision/response
    artifacts (the pick's complete ``Selection.to_dict()`` payload, provider
    stdout/stderr per attempt) under ``raw_dir`` as
    ``<call-id>.pick<N>.json`` / ``<call-id>.a<N>.<provider>.std{out,err}``.
    """

    def __init__(self, cfg: Config, store: Store, run_id: str, raw_dir: Path, *, call_journal=None):
        self.cfg = cfg
        self.store = store
        self.run_id = run_id
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.call_journal = call_journal
        self._made: dict[str, int] = {"cheap": 0, "strong": 0, "gate": 0}
        self._refused: dict[str, int] = {"cheap": 0, "strong": 0, "gate": 0}
        self._outcomes: dict[str, int] = {}
        # Runner-level (not per-call-outcome) count of bounded policy waits:
        # sleeps taken because a pick had fits=true but meets_policy=false.
        self._policy_waits: int = 0
        # Fail loud at construction: every allowed provider must know how to
        # serve both configured model classes.
        for provider in cfg.allowed_providers:
            models = PROVIDER_MODEL_MAP.get(provider)
            if models is None:
                raise ValueError(f"no PROVIDER_MODEL_MAP entry for provider {provider!r}")
            for mc in (cfg.cheap_model_class, cfg.strong_model_class):
                if mc not in models:
                    raise ValueError(
                        f"provider {provider!r} has no model mapping for class {mc!r}"
                    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(
        self,
        stage: str,
        model_class: str,
        prompt: str,
        expect_json: bool,
        cwd: str | None = None,
        sandbox_dir: str | None = None,
        call_trace=None,
    ) -> LLMResult:
        """Route one prompt via quotapick and return the parsed result.

        Raises BudgetExhausted (before any subprocess) when the tier cap is
        hit; every other failure mode is an LLMResult with ok=False and a
        taxonomy outcome, recorded in llm_calls.

        ``cwd`` overrides the provider subprocess working directory (eval
        trials run the agent inside a sandbox). Source installs default to the
        repo root; installed wheels default to the caller's working directory.
        """
        return self._run_call(
            stage, model_class, prompt, expect_json, cwd=cwd, sandbox_dir=sandbox_dir,
            call_trace=call_trace,
        )

    def call_agentic(
        self,
        stage: str,
        model_class: str,
        prompt: str,
        cwd: str,
        expect_json: bool = True,
    ) -> LLMResult:
        """Run one multi-turn agentic session and return its FINAL result.

        Same pick/budget/identity/retry/taxonomy machinery as :meth:`call`;
        only the provider argv differs:

        - claude: adds ``--max-turns <cfg.mine_agent_max_turns>`` and
          ``--allowedTools <comma-joined cfg.mine_agent_allowed_tools>``
          (read-only exploration tools; no Write/Edit/network).
        - codex: adds ``--sandbox read-only`` so model-generated shell
          commands cannot write anywhere.

        ``cwd`` is REQUIRED and is the sandbox the agent explores — it is
        the agent's entire world, so an empty value raises rather than
        silently falling back to the repo root.

        Budget: one agentic session counts as ONE call against its tier cap.
        It is one CLI invocation (one subprocess, one envelope), even though
        the model takes many turns inside it.

        Auditing: NO llm_calls schema change — agentic-ness is encoded in
        the ``stage`` string the caller passes (e.g. ``"mine_agentic"``).

        ``expect_json`` (default True) uses the shared strict parse and bounded
        recovery of exactly one JSON object. Ambiguous or invalid output remains
        ``parse_error``. The claude envelope of a multi-turn
        run has the same result shape as a single-turn one (``num_turns``
        merely > 1); identity is asserted on ``modelUsage`` as always.
        """
        if not cwd:
            raise ValueError("call_agentic requires an explicit sandbox cwd")
        return self._run_call(
            stage, model_class, prompt, expect_json, cwd=cwd, agentic=True
        )

    def _run_call(
        self,
        stage: str,
        model_class: str,
        prompt: str,
        expect_json: bool,
        cwd: str | None = None,
        agentic: bool = False,
        sandbox_dir: str | None = None,
        call_trace=None,
    ) -> LLMResult:
        """Shared budget/audit wrapper behind call() and call_agentic()."""
        # Stage selects the gate's separate pool. Model tier alone would let
        # earlier mining calls consume the allowance reserved for evaluation.
        from .call_budgets import call_pool
        pool = call_pool(self.cfg,stage,model_class)
        cap = {
            "cheap": self.cfg.max_cheap_calls_per_run,
            "strong": self.cfg.max_strong_calls_per_run,
            "gate": self.cfg.max_gate_calls_per_run,
        }[pool]
        if self._made[pool] >= cap:
            self._refused[pool] += 1
            raise BudgetExhausted(pool, cap, self._refused[pool])
        self._made[pool] += 1

        def trace_data(cid):
            return {'run_id':self.run_id, 'stage':stage, 'model_class':model_class,
                    'prompt_sha':hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                    'expect_json':expect_json, 'cwd':cwd, 'sandbox_dir':sandbox_dir,
                    'raw_artifact_prefix':str(self.raw_dir/cid),
                    'timeout_seconds':self.cfg.llm_timeout_seconds}

        call_id = new_id()
        if self.call_journal is not None:
            call_id, recorded = self.call_journal.reserve(pool, {
                'stage':stage,'model_class':model_class,'prompt_sha':hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                'expect_json':expect_json,'cwd':cwd,'agentic':agentic,'sandbox_dir':sandbox_dir,
            }, **({'on_reserved':lambda cid:call_trace.reserve(self.store,cid,trace_data(cid))}
                  if call_trace is not None else {}))
            if recorded is not None:
                result = _ExecResult(**{**recorded, 'call_id':call_id})
                self._outcomes[result.outcome] = self._outcomes.get(result.outcome,0)+1
                return result
        if call_trace is not None and self.call_journal is None:
            call_trace.start(self.store, call_id, trace_data(call_id))
        started = time.monotonic()
        result = self._execute(
            call_id, stage, model_class, prompt, expect_json, cwd=cwd,
            agentic=agentic, sandbox_dir=sandbox_dir
        )
        from dataclasses import replace
        result = replace(result, call_id=call_id)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._outcomes[result.outcome] = self._outcomes.get(result.outcome, 0) + 1
        row = {
                "id": call_id,
                "run_id": self.run_id,
                "stage": stage,
                "provider": result.provider,
                "account": result.account,
                "model_requested": self._requested_for_row(model_class, result.provider),
                "model_reported": result.model_reported,
                "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "duration_ms": duration_ms,
                "outcome": result.outcome,
                "error": result.error,
                "created_at": utc_now_iso(),
            }
        if self.call_journal is None:
            if call_trace is None:
                self.store.insert('llm_calls',row)
                self.store.commit()
            else:
                with self.store.transaction(write=True):
                    self.store.insert('llm_calls',row)
                    call_trace.finish(self.store,row,provider_attempts=result.provider_attempts)
        else:
            from dataclasses import asdict
            with self.store.transaction(write=True):
                self.store.insert('llm_calls',row)
                self.call_journal.complete(call_id,asdict(result))
                if call_trace is not None:
                    call_trace.finish(self.store,row,provider_attempts=result.provider_attempts)
        return result

    def stats(self) -> dict:
        """Attempted/succeeded/failed + outcome taxonomy + budget refusals."""
        # parse_recovered is a SUCCESS (valid JSON recovered from exactly one
        # prose-wrapped object; counted distinctly in by_outcome so the
        # prose-wrapping frequency stays visible), as is
        # oauth_transient_retried (completed after one credential retry).
        succeeded = (
            self._outcomes.get("ok", 0)
            + self._outcomes.get("oauth_transient_retried", 0)
            + self._outcomes.get("parse_recovered", 0)
        )
        attempted = sum(self._made.values())
        return {
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": attempted - succeeded,
            "by_outcome": dict(self._outcomes),
            "calls_made": dict(self._made),
            "refused": dict(self._refused),
            "policy_waits": self._policy_waits,
        }

    # ------------------------------------------------------------------
    # Core flow
    # ------------------------------------------------------------------

    def _execute(
        self,
        call_id: str,
        stage: str,
        model_class: str,
        prompt: str,
        expect_json: bool,
        cwd: str | None = None,
        agentic: bool = False,
        sandbox_dir: str | None = None,
    ) -> "_ExecResult":
        pick_no = 1
        pick, pick_err = self._pick(call_id, model_class, pick_no=pick_no)
        if pick is None:
            return _ExecResult.fail("spawn_error", error=pick_err)
        if sandbox_dir:
            # quotapick routes by quota. Apply sandbox compatibility separately
            # so a provider with headroom cannot receive a trial it cannot run.
            #
            # Re-pick rather than pre-filter: the ACCOUNTS belonging to an
            # incompatible provider are derived from what quotapick reports.
            # Each rejected account is added to the exclusion so the
            # loop cannot be handed the same one twice, which is also what
            # bounds it.
            pick, pick_err, pick_no = self._repick_until_sandbox_capable(
                call_id, model_class, pick, pick_no
            )
            if pick is None:
                return _ExecResult.fail("sandbox_incapable_fleet", error=pick_err)
        if not pick.fits:
            return _ExecResult.fail(
                "quota_exhausted",
                provider=pick.provider,
                account=pick.account,
                error=_with_unreadable(
                    "quotapick: fits=false, no provider invoked", pick
                ),
            )
        if not pick.meets_policy:
            # fits=true but the policy bar was missed: NOT a green light.
            # One bounded, loud sleep until available_at, then ONE fresh pick
            # (the pre-sleep pick is stale); never a second sleep.
            cap = self.cfg.quota_wait_max_seconds
            wait = pick.available_at - time.time()
            if wait > cap:
                return _ExecResult.fail(
                    "quota_exhausted",
                    provider=pick.provider,
                    account=pick.account,
                    error=_with_unreadable(
                        "quotapick: meets_policy=false; available_at="
                        f"{_iso_utc(pick.available_at)} exceeds wait cap {cap}s",
                        pick,
                    ),
                )
            if wait <= 0:
                return _ExecResult.fail(
                    "quota_exhausted",
                    provider=pick.provider,
                    account=pick.account,
                    error=_with_unreadable(
                        "quotapick: meets_policy=false; available_at="
                        f"{_iso_utc(pick.available_at)} absent or already "
                        "past while meets_policy is still false; no bounded "
                        "wait possible",
                        pick,
                    ),
                )
            logger.info(
                "quotapick meets_policy=false (account=%s); sleeping %.1fs "
                "until available_at=%s, then re-picking once",
                pick.account,
                wait,
                _iso_utc(pick.available_at),
            )
            self._policy_waits += 1
            time.sleep(wait)
            first_available_at = pick.available_at
            pick_no += 1
            pick, pick_err = self._pick(call_id, model_class, pick_no=pick_no)
            if pick is None:
                return _ExecResult.fail(
                    "quota_exhausted",
                    error=(
                        f"quotapick: re-pick after {wait:.0f}s policy wait "
                        f"failed: {pick_err}"
                    ),
                )
            if sandbox_dir:
                pick, pick_err, pick_no = self._repick_until_sandbox_capable(
                    call_id, model_class, pick, pick_no
                )
                if pick is None:
                    return _ExecResult.fail("sandbox_incapable_fleet", error=pick_err)
            if not pick.fits:
                return _ExecResult.fail(
                    "quota_exhausted",
                    provider=pick.provider,
                    account=pick.account,
                    error=_with_unreadable(
                        "quotapick: fits=false after policy wait, "
                        "no provider invoked",
                        pick,
                    ),
                )
            if not pick.meets_policy:
                return _ExecResult.fail(
                    "quota_exhausted",
                    provider=pick.provider,
                    account=pick.account,
                    error=_with_unreadable(
                        "quotapick: meets_policy still false after policy "
                        f"wait; available_at first={_iso_utc(first_available_at)} "
                        f"then={_iso_utc(pick.available_at)}",
                        pick,
                    ),
                )
        if pick.provider not in self.cfg.allowed_providers:
            return _ExecResult.fail(
                "other",
                provider=pick.provider,
                account=pick.account,
                error=f"picked provider {pick.provider!r} not in allowed_providers",
            )

        oauth_retried = False
        repicked_from = ""
        attempt_no = 1
        attempt = self._invoke(
            call_id, attempt_no, pick, model_class, prompt, cwd=cwd,
            agentic=agentic, sandbox_dir=sandbox_dir
        )

        # One immediate retry on the OAuth credential race ("Not logged in").
        if attempt.status == "failed" and _OAUTH_PATTERN in attempt.failure_text.lower():
            oauth_retried = True
            attempt_no += 1
            attempt = self._invoke(
                call_id, attempt_no, pick, model_class, prompt, cwd=cwd,
                agentic=agentic, sandbox_dir=sandbox_dir
            )

        # One re-pick (possibly switching provider) on quota-exhausted text.
        if attempt.status == "failed" and _looks_quota(attempt.failure_text):
            failed_account = pick.account
            pick_no += 1
            pick2, pick2_err = self._pick(
                call_id, model_class, pick_no=pick_no, exclude=failed_account
            )
            if pick2 is None:
                return _ExecResult.fail(
                    "quota_exhausted",
                    provider=pick.provider,
                    account=pick.account,
                    error=f"quota failure on {failed_account}; re-pick failed: {pick2_err}",
                    provider_attempts=attempt_no,
                )
            if sandbox_dir:
                pick2, pick2_err, pick_no = self._repick_until_sandbox_capable(
                    call_id, model_class, pick2, pick_no, exclude=[failed_account]
                )
                if pick2 is None:
                    return _ExecResult.fail(
                        "quota_exhausted",
                        provider=pick.provider,
                        account=pick.account,
                        error=(
                            f"quota failure on {failed_account}; no sandbox-capable "
                            f"re-pick: {pick2_err}"
                        ),
                        provider_attempts=attempt_no,
                    )
            # This path exists to recover a failed call FAST, so a
            # not-meets-policy re-pick is treated like no capacity — no sleep.
            if (
                not pick2.fits
                or not pick2.meets_policy
                or pick2.provider not in self.cfg.allowed_providers
            ):
                return _ExecResult.fail(
                    "quota_exhausted",
                    provider=pick.provider,
                    account=pick.account,
                    # pick2 (not pick) carries the fresher readability picture.
                    error=_with_unreadable(
                        f"quota failure on {failed_account}; re-pick has no capacity",
                        pick2,
                    ),
                    provider_attempts=attempt_no,
                )
            repicked_from = failed_account
            pick = pick2
            attempt_no += 1
            attempt = self._invoke(
                call_id, attempt_no, pick, model_class, prompt, cwd=cwd,
                agentic=agentic, sandbox_dir=sandbox_dir
            )
            if attempt.status == "failed" and _OAUTH_PATTERN in attempt.failure_text.lower() and not oauth_retried:
                oauth_retried = True
                attempt_no += 1
                attempt = self._invoke(
                    call_id, attempt_no, pick, model_class, prompt, cwd=cwd, agentic=agentic, sandbox_dir=sandbox_dir
                )

        from dataclasses import replace
        return replace(self._finalize(
            attempt,
            pick,
            expect_json,
            oauth_retried=oauth_retried,
            repicked_from=repicked_from,
        ), provider_attempts=attempt_no)

    def _finalize(
        self,
        attempt: _Attempt,
        pick: _Pick,
        expect_json: bool,
        *,
        oauth_retried: bool,
        repicked_from: str,
    ) -> "_ExecResult":
        """Classify the final attempt into the outcome taxonomy."""
        base = dict(
            provider=pick.provider,
            account=pick.account,
            model_reported=attempt.model_reported,
            tokens_in=attempt.tokens_in,
            tokens_out=attempt.tokens_out,
        )
        if attempt.status in _TERMINAL_ATTEMPT_STATUSES:
            return _ExecResult.fail(attempt.status, error=attempt.error, **base)
        if attempt.status not in ("failed", "ok"):
            # Fail loud rather than fall through. This branch used to be a
            # hardcoded tuple, so adding `empty_output` to the parsers left it
            # unhandled — and an unhandled status reached the `status == "ok"`
            # path below and came out as `model_mismatch`, a confident wrong
            # answer about a call that never produced anything.
            raise AssertionError(
                f"attempt status {attempt.status!r} is not handled by "
                "_classify; add it to _TERMINAL_ATTEMPT_STATUSES or give it a "
                "branch"
            )
        if attempt.status == "failed":
            ftext = attempt.failure_text
            if _looks_quota(ftext):
                outcome = "quota_exhausted"
            elif _OAUTH_PATTERN in ftext.lower():
                outcome = "other"
                ftext = f"oauth failure persisted after retry: {ftext}"
            else:
                # quota and oauth are checked first: they are causes, and a
                # turn-cap DNF is a symptom that can co-occur with neither.
                outcome = classify_failure_text(ftext)
            return _ExecResult.fail(outcome, error=_clip(ftext), **base)

        # attempt.status == "ok": envelope parsed, assistant text extracted.
        if not attempt.identity_ok:
            return _ExecResult.fail(
                "model_mismatch",
                error=attempt.error
                or f"reported model {attempt.model_reported!r} lacks requested token",
                **base,
            )
        parsed: dict | None = None
        recovered = False
        if expect_json:
            try:
                parsed = _parse_strict_json(attempt.text)
            except ValueError as exc:
                # Narrow recovery for valid contract JSON wrapped in prose:
                # accept IFF the string-aware brace scan finds EXACTLY ONE
                # balanced top-level JSON object in the text. Zero or several
                # candidates stay parse_error — ambiguity is never guessed at.
                candidates = _find_top_level_json_objects(attempt.text)
                if len(candidates) != 1:
                    return _ExecResult.fail(
                        "parse_error",
                        error=(
                            f"expect_json: {exc}; recovery scan found "
                            f"{len(candidates)} top-level JSON object(s), "
                            "need exactly 1"
                        ),
                        text=attempt.text,
                        **base,
                    )
                parsed = candidates[0]
                recovered = True
        # Success-outcome precedence matches how plain ok composes with the
        # retry flow: an oauth retry claims the single outcome slot; a
        # recovery that rode along is then surfaced via the error column,
        # never silently dropped.
        if oauth_retried:
            outcome = "oauth_transient_retried"
        elif recovered:
            outcome = "parse_recovered"
        else:
            outcome = "ok"
        notes: list[str] = []
        if repicked_from:
            notes.append(f"repicked_from={repicked_from}")
        if recovered and oauth_retried:
            notes.append("parse_recovered")
        error = "; ".join(notes)
        return _ExecResult(
            ok=True,
            text=attempt.text,
            parsed=parsed,
            outcome=outcome,
            error=error,
            **base,
        )

    # ------------------------------------------------------------------
    # quotapick
    # ------------------------------------------------------------------

    def _repick_until_sandbox_capable(
        self,
        call_id: str,
        model_class: str,
        pick: "_Pick",
        pick_no: int,
        exclude: Sequence[str] = (),
    ) -> "tuple[_Pick | None, str, int]":
        """Keep re-picking until the account's provider can run a sandboxed
        trial, or the fleet is exhausted.

        Returns ``(pick, "", pick_no)`` or ``(None, error, pick_no)``. Refuse
        when no capable provider remains; an agent that cannot start cannot
        supply a rule verdict.

        Bounded by construction: every rejected account joins the exclusion
        list, and the fleet is finite, so the loop either finds a capable
        account or runs out. The extra `+ 2` on the cap is slack for the
        library returning an account we did not ask for; it is a backstop, not
        the mechanism.

        Every pick of a sandboxed call goes through here, including the fresh
        pick after a policy wait and the re-pick after a quota failure.
        ``exclude`` carries accounts that must stay excluded on every re-pick,
        such as the account that just ran out of quota.
        """
        banned = {p.lower() for p in self.cfg.sandbox_incompatible_providers}
        if not banned:
            return pick, "", pick_no
        rejected: list[str] = []
        cap = len(banned) + len(rejected) + 8
        while (pick.provider or "").lower() in banned:
            rejected.append(pick.account)
            if len(rejected) > cap:
                return (
                    None,
                    f"sandbox re-pick did not converge after {len(rejected)} "
                    f"attempts; rejected={rejected}",
                    pick_no,
                )
            logger.warning(
                "quotapick chose account=%s provider=%s, which cannot run a "
                "sandboxed trial; re-picking (excluding %s)",
                pick.account, pick.provider, rejected,
            )
            pick_no += 1
            pick, err = self._pick(
                call_id, model_class, pick_no=pick_no, exclude=[*exclude, *rejected]
            )
            if pick is None:
                return (
                    None,
                    f"no account can run a sandboxed trial "
                    f"(rejected {rejected}): {err}",
                    pick_no,
                )
        return pick, "", pick_no

    def _pick(
        self,
        call_id: str,
        model_class: str,
        pick_no: int,
        exclude: str | Sequence[str] | None = None,
    ) -> tuple[_Pick | None, str]:
        """Call quota_router.select_account and gate its contract-1 decision.

        Returns (pick, "") or (None, error). A re-pick passes ``exclude=`` (a
        LIST of account ids — the library iterates the sequence, so a bare
        string would be exploded into characters) for the account whose quota
        just failed plus ``no_sticky=True`` so the sticky incumbent cannot win
        again. ``record`` stays at its default (True) so the pick books a
        pileup reservation — this pick is acted on immediately.

        ``env=dict(os.environ)`` is passed explicitly: the library defaults
        its env injection point to {} while the CLI reads os.environ, and the
        old pick subprocess inherited our full environment. Passing it keeps
        config discovery (XDG_CONFIG_HOME / QUOTA_ROUTER_CONFIG) and the
        banned-proxy-env warnings identical to the CLI preflight's view.

        The library never raises on ROUTING failure (it returns a degraded
        Selection with account/provider None and fits False, which lands in
        the quota_exhausted path exactly as the CLI's null-decision JSON
        did); anything it does raise (ConfigError, unexpected bugs) maps to
        the same (None, error) return the pick subprocess errors used, which
        _execute classifies as spawn_error on pick 1 and quota_exhausted on
        re-picks — unchanged taxonomy. There is deliberately no wall-clock
        timeout: the old PICK_TIMEOUT_SECONDS bounded the subprocess; the
        library exposes no equivalent (its ``timeout_ms`` is the usage-oracle
        HTTP timeout config override, a different knob), so the constant was
        removed rather than left dead.
        """
        kwargs: dict = {"model": model_class, "env": dict(os.environ)}
        if exclude:
            # A bare string is wrapped, never passed through: the library
            # ITERATES this sequence, so the invented name "demo_a" would become
            # six entries 'd','e','m','o','_','a'. The sandbox
            # re-pick below passes a real list of several account ids.
            excluded = [exclude] if isinstance(exclude, str) else list(exclude)
            kwargs.update(exclude=excluded, no_sticky=True)
        try:
            selection = select_account(**kwargs)
        except Exception as exc:  # library boundary == old subprocess boundary
            return None, f"quotapick: {type(exc).__name__}: {exc}"
        # Retain the full decision payload as the CLI would have printed it
        # (json.dump indent=2, sort_keys=False, default=str, trailing \n).
        self._retain(
            call_id,
            f"pick{pick_no}.json",
            (
                json.dumps(selection.to_dict(), indent=2, sort_keys=False, default=str)
                + "\n"
            ).encode("utf-8"),
        )
        if selection.contract_version != 1:
            return None, (
                f"quotapick: unsupported contract_version {selection.contract_version!r}"
            )
        # fits/meets_policy are None when the decision lacked the key (the
        # Selection maps key-absent to None); that is malformed — fail loud.
        # provider/account None is DIFFERENT: a legitimate degraded no-winner
        # decision carries them as null with fits False, and under the CLI
        # that flowed through to the quota_exhausted path, so it still does.
        missing = [
            k
            for k, v in (("fits", selection.fits), ("meets_policy", selection.meets_policy))
            if v is None
        ]
        if missing:
            return None, f"quotapick: decision missing keys {missing}"
        # available_at is optional (meaningful when meets_policy is false):
        # absent or null -> 0.0; anything else non-numeric fails loud.
        available_at = selection.available_at
        if available_at is None:
            available_at = 0.0
        elif isinstance(available_at, (int, float)) and not isinstance(available_at, bool):
            available_at = float(available_at)
        else:
            return None, (
                "quotapick: decision available_at must be epoch seconds "
                f"(number) or null, got {available_at!r}"
            )
        return (
            _Pick(
                provider=selection.provider or "",
                account=selection.account or "",
                fits=bool(selection.fits),
                meets_policy=bool(selection.meets_policy),
                available_at=available_at,
                # exec_env is FREQUENTLY {} and that is CORRECT: the default
                # Claude account is selected by the ABSENCE of
                # CLAUDE_CONFIG_DIR. Merge the overlay; never require it
                # non-empty.
                env=dict(selection.exec_env),
                unreadable=_unreadable_from(selection),
            ),
            "",
        )

    # ------------------------------------------------------------------
    # Provider adapters
    # ------------------------------------------------------------------

    def _invoke(
        self,
        call_id: str,
        attempt_no: int,
        pick: _Pick,
        model_class: str,
        prompt: str,
        cwd: str | None = None,
        agentic: bool = False,
        sandbox_dir: str | None = None,
    ) -> _Attempt:
        """Run one provider subprocess and parse its envelope.

        ``agentic`` selects the multi-turn variant of each adapter (flags
        verified against the installed CLIs, 2026-08-16):

        - claude 2.1.233: ``--allowedTools`` per ``--help`` takes a "Comma
          or space-separated list of tool names"; one comma-joined argv
          token is the unambiguous form. ``--max-turns <turns>`` is hidden
          from ``--help`` in this version but present in the binary's
          option table ("Maximum number of agentic turns in
          non-interactive mode").
        - codex ``exec --help``: ``-s, --sandbox <SANDBOX_MODE>`` with
          possible values read-only|workspace-write|danger-full-access;
          read-only keeps the agent from writing anywhere.
        """
        model = PROVIDER_MODEL_MAP[pick.provider][model_class]
        env = {**os.environ, **pick.env}
        invocation_cwd = cwd or str(REPO_ROOT or Path.cwd())
        if pick.provider == "claude":
            argv = [
                self.cfg.claude_path,
                "-p",
                "--model",
                model,
                "--output-format",
                "json",
            ]
            if agentic:
                argv += [
                    "--max-turns",
                    str(self.cfg.mine_agent_max_turns),
                    "--allowedTools",
                    ",".join(self.cfg.mine_agent_allowed_tools),
                ]
            run_kwargs: dict = {"input": prompt.encode("utf-8")}
        else:  # codex — prompt on argv; codex exec hangs on inherited stdin.
            argv = [
                self.cfg.codex_path,
                "exec",
                "--json",
                "--skip-git-repo-check",
            ]
            if agentic:
                argv += ["--sandbox", "read-only"]
            argv += [
                "-m",
                model,
                prompt,
            ]
            run_kwargs = {"stdin": subprocess.DEVNULL}
        # The eval agent runs with write and execute, so its boundary is the
        # OS rather than a permission rule. Wrapping happens HERE, at the one
        # place argv is built, so a new provider adapter cannot quietly ship
        # an unsandboxed path.
        if sandbox_dir and self.cfg.eval_sandbox_enabled:
            from .sandbox import agent_config_dir, policy_for_trial, wrap_argv, write_settings

            policy = policy_for_trial(
                sandbox_dir, self.cfg, config_dir=agent_config_dir(env)
            )
            settings = write_settings(
                policy, Path(sandbox_dir).parent / "srt-settings.json"
            )
            argv = wrap_argv(argv, settings, self.cfg)
        try:
            proc = _run_provider(
                argv,
                capture_output=True,
                timeout=self.cfg.llm_timeout_seconds,
                env=env,
                cwd=invocation_cwd,
                **run_kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            self._retain_attempt(call_id, attempt_no, pick.provider, exc.stdout, exc.stderr)
            return _Attempt(
                status="timeout",
                error=f"{pick.provider} timed out after {self.cfg.llm_timeout_seconds}s",
            )
        except OSError as exc:
            return _Attempt(
                status="spawn_error", error=f"{pick.provider}: {type(exc).__name__}: {exc}"
            )
        self._retain_attempt(call_id, attempt_no, pick.provider, proc.stdout, proc.stderr)
        if pick.provider == "claude":
            return self._parse_claude(proc.returncode, proc.stdout, proc.stderr, model)
        # Bind the receipt lookup to THIS subprocess's effective account home.
        # A quota re-pick can change it within the same logical call. Never
        # fall back to another account's receipt when this one is missing.
        codex_home = env.get("CODEX_HOME")
        if codex_home:
            home = Path(codex_home)
            if not home.is_absolute():
                home = Path(invocation_cwd).absolute() / home
            sessions_dir = home / "sessions"
        else:
            sessions_dir = Path(self.cfg.codex_sessions_dir)
        return self._parse_codex(
            proc.returncode, proc.stdout, proc.stderr, model, sessions_dir=sessions_dir
        )

    def _parse_claude(
        self, returncode: int, stdout: bytes, stderr: bytes, expected_token: str
    ) -> _Attempt:
        """Parse the claude -p --output-format json envelope (see fixtures)."""
        out, err = _decode(stdout), _decode(stderr)
        if returncode != 0:
            # Envelope may still be present; fold both into the failure text.
            return _Attempt(
                status="failed",
                failure_text=f"exit {returncode}: {err}\n{out}",
            )
        if not out.strip():
            return _Attempt(
                status="empty_output",
                error=f"claude exited 0 with no output on stdout (stderr: {err.strip()[:200]!r})",
            )
        try:
            env_obj = json.loads(out)
        except json.JSONDecodeError as exc:
            return _Attempt(status="parse_error", error=f"claude envelope not JSON: {exc}")
        if not isinstance(env_obj, dict) or env_obj.get("type") != "result":
            return _Attempt(
                status="parse_error",
                error=f"claude envelope type={env_obj.get('type') if isinstance(env_obj, dict) else type(env_obj).__name__!r}, expected 'result'",
            )
        text = env_obj.get("result")
        if env_obj.get("is_error"):
            return _Attempt(
                status="failed", failure_text=f"is_error envelope: {text}\n{err}"
            )
        if not isinstance(text, str):
            return _Attempt(
                status="parse_error", error="claude envelope missing 'result' text"
            )
        usage = env_obj.get("usage") or {}
        model_usage = env_obj.get("modelUsage") or {}
        matched = ""
        for key, mu in model_usage.items():
            canonical = mu.get("canonicalModel", "") if isinstance(mu, dict) else ""
            if expected_token in key or expected_token in canonical:
                matched = key
                break
        if matched:
            return _Attempt(
                status="ok",
                text=text,
                model_reported=matched,
                identity_ok=True,
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
            )
        observed = ",".join(sorted(model_usage)) or "<none>"
        return _Attempt(
            status="ok",
            text=text,
            model_reported=observed,
            identity_ok=False,
            error=f"requested token {expected_token!r} not in reported models [{observed}]",
        )

    def _parse_codex(
        self, returncode: int, stdout: bytes, stderr: bytes, expected_token: str,
        *, sessions_dir: Path,
    ) -> _Attempt:
        """Parse codex exec --json JSONL and verify identity via the rollout.

        The JSONL stream has no model field; the reported model is the last
        ``turn_context.payload.model`` in the rollout file matching the
        stream's thread_id under this invocation's sessions_dir. No rollout / no
        model there -> identity unverifiable -> model_mismatch (never
        silently accepted).
        """
        out, err = _decode(stdout), _decode(stderr)
        if returncode != 0:
            return _Attempt(status="failed", failure_text=f"exit {returncode}: {err}\n{out}")
        if not out.strip():
            # Nothing on stdout is not an unreadable answer; it is no answer.
            # `parse_error` below covers output that ARRIVED and did not carry
            # one, which is a different fact and a different fix.
            return _Attempt(
                status="empty_output",
                error=f"codex exited 0 with no output on stdout (stderr: {err.strip()[:200]!r})",
            )
        thread_id = ""
        agent_text: str | None = None
        tokens_in = tokens_out = 0
        malformed = 0
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            etype = event.get("type", "")
            if etype == "thread.started":
                thread_id = event.get("thread_id", "")
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    agent_text = item.get("text", "")
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                tokens_in = int(usage.get("input_tokens") or 0)
                tokens_out = int(usage.get("output_tokens") or 0)
            elif etype in ("turn.failed", "error") or etype.endswith(".error"):
                return _Attempt(
                    status="failed", failure_text=f"codex event {etype}: {json.dumps(event)}\n{err}"
                )
        if malformed:
            return _Attempt(
                status="parse_error",
                error=f"codex JSONL: {malformed} malformed line(s) in stdout",
            )
        if agent_text is None:
            return _Attempt(
                status="parse_error",
                error="codex JSONL: no item.completed agent_message event",
            )
        model_reported, id_err = self._codex_reported_model(thread_id, sessions_dir)
        identity_ok = bool(model_reported) and expected_token in model_reported
        return _Attempt(
            status="ok",
            text=agent_text,
            model_reported=model_reported,
            identity_ok=identity_ok,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error=(
                ""
                if identity_ok
                else id_err
                or f"requested token {expected_token!r} not in rollout model {model_reported!r}"
            ),
        )

    def _codex_reported_model(self, thread_id: str, sessions_dir: Path) -> tuple[str, str]:
        """Read the served model out of the rollout DATA (never mtime).

        Returns (model, "") or ("", why-not). Malformed rollout lines are
        skipped while scanning: they cannot cause a silent wrong answer here
        because an unfound model fails loudly as model_mismatch.
        """
        if not thread_id:
            return "", "codex JSONL had no thread.started thread_id"
        root = sessions_dir
        matches = sorted(root.glob(f"**/rollout-*{thread_id}.jsonl"))
        if not matches:
            return "", f"no rollout file for thread {thread_id} under {root}"
        model = ""
        with open(matches[-1], encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"token_count"' in line:  # Usage records cannot supply the model.
                    continue
                if '"turn_context"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "turn_context":
                    continue
                m = (rec.get("payload") or {}).get("model")
                if isinstance(m, str) and m:
                    model = m  # last turn_context wins
        if not model:
            return "", f"rollout {matches[-1].name} has no turn_context payload.model"
        return model, ""

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _tier(self, model_class: str) -> str:
        from .call_budgets import model_tier
        return model_tier(self.cfg, model_class)

    def _requested_for_row(self, model_class: str, provider: str) -> str:
        """model_requested column: the token literally asked of the CLI."""
        mapped = PROVIDER_MODEL_MAP.get(provider, {}).get(model_class)
        return mapped if mapped else model_class

    def _retain(self, call_id: str, suffix: str, data: bytes) -> None:
        (self.raw_dir / f"{call_id}.{suffix}").write_bytes(data or b"")

    def _retain_attempt(
        self,
        call_id: str,
        attempt_no: int,
        provider: str,
        stdout: bytes | None,
        stderr: bytes | None,
    ) -> None:
        self._retain(call_id, f"a{attempt_no}.{provider}.stdout", stdout or b"")
        self._retain(call_id, f"a{attempt_no}.{provider}.stderr", stderr or b"")


@dataclass(frozen=True)
class _ExecResult(LLMResult):
    """Internal subclass of LLMResult carrying token telemetry for the row.

    Subclassing keeps ``call() -> LLMResult`` honest while the llm_calls
    insert reads the token fields directly (fail loud, no hasattr guards).
    """

    tokens_in: int = 0
    tokens_out: int = 0

    @classmethod
    def fail(
        cls,
        outcome: str,
        *,
        provider: str = "",
        account: str = "",
        model_reported: str = "",
        error: str = "",
        text: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        provider_attempts: int = 0,
    ) -> "_ExecResult":
        return cls(
            ok=False,
            text=text,
            parsed=None,
            outcome=outcome,
            provider=provider,
            account=account,
            model_reported=model_reported,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            provider_attempts=provider_attempts,
        )


def _looks_quota(failure_text: str) -> bool:
    low = failure_text.lower()
    return any(p in low for p in _QUOTA_PATTERNS)


def _iso_utc(epoch_seconds: float) -> str:
    """Render quotapick's epoch-seconds available_at as ISO-8601 UTC.

    0.0 means the field was absent/null in the decision; say so instead of
    rendering the misleading 1970 epoch.
    """
    if not epoch_seconds:
        return "absent"
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _clip(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[clipped {len(text) - limit} chars]"


def _parse_strict_json(text: str) -> dict:
    """Strict json.loads of assistant text, allowing one ```json fence.

    Raises ValueError unless the payload is a top-level JSON object.
    """
    body = text
    m = _FENCE_RE.match(text)
    if m:
        body = m.group(1)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"top-level JSON must be an object, got {type(parsed).__name__}")
    return parsed


def _find_top_level_json_objects(text: str) -> list[dict]:
    """String-aware brace scan for balanced top-level ``{...}`` spans.

    Returns every balanced top-level span that ``json.loads`` to a dict, in
    order of appearance. Used by the ``parse_recovered`` path, whose caller
    accepts the recovery IFF exactly one object comes back.

    Matcher rules (deliberate, all conservative — a miss is a loud
    ``parse_error``, never a wrong guess):

    - Outside a candidate, only ``{`` is significant. Prose quotes are NOT
      tracked: natural-language text has unbalanced quotes, and tracking
      them would let one apostrophe-free stray ``"`` hide real JSON.
    - Inside a candidate, double-quoted strings and backslash escapes are
      respected, so braces inside string values never open/close anything.
    - A balanced span that fails ``json.loads`` (prose like ``{curly}``) is
      simply not a candidate; scanning resumes after it, so anything nested
      inside it is NOT top-level and is not considered.
    - A candidate still open at end-of-text is discarded and, being greedy,
      swallows the rest of the text — an unbalanced ``{`` never yields a
      guessed inner object.
    """
    objects: list[dict] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        end = -1
        j = i
        while j < n:
            ch = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end == -1:
            break  # unbalanced candidate: swallows the rest, nothing guessed
        span = text[i : end + 1]
        try:
            parsed = json.loads(span)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            objects.append(parsed)
        i = end + 1
    return objects
