"""Whether a `running` row in `runs` still has a live owner.

Each writer that creates a `running` row records the identity of its own
process in the row's `stats_json` under `process`: the PID and the process
start time, stored as ISO UTC to the second. The run is live only while a
process with that PID exists, has not exited (a zombie that only waits to be
reaped has), and has that same start time. A reused PID has a different start
time, so it never keeps a dead run alive.

The ops lock in `ops/run-nightly.sh` (`start_time_of`, `lock_holder_live`) is
a separate and weaker check. It compares `ps -o lstart=` text and has no
zombie check. It also trusts the PID alone when the lock has no start time,
and it accepts a start time that an older script wrote in the local format.

The start time and the process state come from the kernel, never from `ps`.
On macOS `/bin/ps` is setuid root, and a seatbelt sandbox such as Codex's
refuses to run it. On macOS the reader asks `sysctl` for the process record
(`KERN_PROC_PID`, one `struct kinfo_proc`). On Linux it reads
`/proc/<pid>/stat` and the boot time in `/proc/stat`. On macOS the result is
the value that `ps -o lstart=` prints in the C locale and UTC, which the
earlier `ps` reader stored, so identities recorded by that reader still
compare equal. On Linux the result matched procps-ng 4.0.4 (Ubuntu 24.04)
for every process in one local check. Any other platform raises. If the
kernel returns no record for a PID that still exists, the read raises too: a
hidden process is not a dead one.

On Linux the start time is relative to boot: the boot time plus a tick count.
The kernel derives the boot time from the current wall clock, so after a
wall-clock step the start time of the same process can shift, as it does in
`ps`. A live run then reads as ended. Production runs on macOS.

The pipeline's stale-run reaper (`pipeline.finalize_stale_runs`) and Review
membership (`execution_policy.proposal_dispositions`) both call
:func:`run_liveness`. A crashed run's proposals return to Review as soon as
its process is gone, and a live run is never reaped however long it takes.

A row without a recorded identity was written before this record existed. It
keeps the age rule: it is live while its `started` time is younger than
`cfg.run_stale_after_hours`. If that time cannot be read, its liveness is
`unknown`: the reaper leaves the row `running` and counts it, and Review keeps
treating the run as live, as both did before.

A recorded identity that cannot be read raises :class:`RunLivenessError` with
the run id. It never counts as alive or dead silently. A failed process-table
read also raises, and also names the run.

This module reads process records and the given rows. It never writes, and it
never sends a signal: `os.kill(pid, 0)` only asks whether the PID exists.
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import struct
import sys
from datetime import datetime, timedelta, timezone

PROCESS_KEY = "process"
LIVENESS = ("live", "ended", "unknown")
#: pid_t is a signed 32-bit integer on macOS and Linux. A larger recorded PID
#: cannot name any process, so it is a corrupt identity.
PID_MAX = 2**31 - 1
_ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"

# macOS. <sys/sysctl.h>: CTL_KERN, KERN_PROC, KERN_PROC_PID name one process.
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1
#: sizeof(struct kinfo_proc) on 64-bit macOS (arm64 and x86_64).
KINFO_PROC_SIZE = 648
#: The record starts with kp_proc, a struct extern_proc (<sys/proc.h>):
#: offset 0 is p_un.__p_starttime.tv_sec (int64, the start time that `ps`
#: prints as lstart), offset 36 is p_stat (char), and offset 40 is p_pid
#: (int32). The reader checks p_pid against the PID it asked for, so a layout
#: change raises instead of reading the wrong bytes.
P_STARTTIME_SEC, P_STAT, P_PID = 0, 36, 40
#: p_stat of a process that has exited and waits to be reaped.
SZOMB = 5

# Linux. Field 22 of /proc/<pid>/stat is starttime in clock ticks after boot;
# `btime` in /proc/stat is the boot time in seconds since the epoch.
PROC_ROOT = "/proc"
#: /proc/<pid>/stat states of a process that has exited (zombie, dead).
_LINUX_EXITED = ("Z", "X")


class RunLivenessError(ValueError):
    """A recorded process identity or the process table could not be read."""


def process_start_time(pid: int) -> str | None:
    """Return the UTC start time of process ``pid`` as ISO text.

    Return None only when no process has that PID, or when it has exited and
    only its exit status remains (a zombie). Any other outcome raises, because
    a guess either way would reap a live run or strand a dead run's proposals.
    """
    if type(pid) is not int or not 0 < pid <= PID_MAX:
        raise RunLivenessError(f"invalid process id {pid!r}")
    record = _process_record(pid)
    if record is None:
        if _pid_exists(pid):
            raise RunLivenessError(
                f"PID {pid} exists, but the kernel returned no process record for it")
        return None
    exited, seconds = record
    if exited:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime(_ISO_UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise RunLivenessError(f"unreadable start time {seconds!r} for PID {pid}: {exc}") from exc


def _process_record(pid: int) -> tuple[bool, int] | None:
    """(has exited, start time in whole seconds since the epoch), or None."""
    if sys.platform == "darwin":
        return _darwin_record(pid)
    if sys.platform.startswith("linux"):
        return _linux_record(pid)
    raise RunLivenessError(
        f"cannot read process start times on platform {sys.platform!r}: "
        "this reader supports macOS (sysctl) and Linux (/proc)")


def _pid_exists(pid: int) -> bool:
    # Signal 0 delivers nothing. The kernel looks the PID up first, so
    # ESRCH means that no process has it, even under a sandbox.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        raise RunLivenessError(f"cannot tell whether PID {pid} exists: {exc}") from exc
    return True


@functools.cache
def _sysctl():
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.sysctl
    call.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                     ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
    call.restype = ctypes.c_int
    return call


def _darwin_record(pid: int) -> tuple[bool, int] | None:
    try:
        sysctl = _sysctl()
    except (OSError, AttributeError) as exc:
        raise RunLivenessError(f"cannot load sysctl from the C library: {exc}") from exc
    mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid)
    buffer = ctypes.create_string_buffer(KINFO_PROC_SIZE)
    size = ctypes.c_size_t(KINFO_PROC_SIZE)
    if sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        code = ctypes.get_errno()
        raise RunLivenessError(
            f"sysctl KERN_PROC_PID for PID {pid} failed: {os.strerror(code)} (errno {code})")
    if size.value == 0:
        return None
    if size.value != KINFO_PROC_SIZE:
        raise RunLivenessError(
            f"sysctl returned {size.value} bytes for PID {pid}; this reader expects a "
            f"{KINFO_PROC_SIZE}-byte struct kinfo_proc")
    raw = buffer.raw
    (named,) = struct.unpack_from("=i", raw, P_PID)
    if named != pid:
        raise RunLivenessError(
            f"the sysctl record for PID {pid} names PID {named}: the struct kinfo_proc "
            "layout differs from the one this reader expects")
    (seconds,) = struct.unpack_from("=q", raw, P_STARTTIME_SEC)
    return raw[P_STAT] == SZOMB, seconds


def _linux_record(pid: int, root: str = PROC_ROOT) -> tuple[bool, int] | None:
    path = f"{root}/{pid}/stat"
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise RunLivenessError(f"cannot read {path}: {exc}") from exc
    # "<pid> (<comm>) <state> ...": comm may hold spaces and ")", so the
    # fields after the LAST ")" start at field 3, the state.
    head, close, rest = data.rpartition(b")")
    fields = rest.split()
    try:
        if not close or int(head.split(b" (", 1)[0]) != pid or len(fields) < 20:
            raise ValueError("unexpected field layout")
        state, ticks = fields[0].decode("ascii"), int(fields[19])
    except (ValueError, UnicodeDecodeError) as exc:
        raise RunLivenessError(f"unreadable {path}: {exc}") from exc
    return state in _LINUX_EXITED, _linux_boot_seconds(root) + ticks // _clock_ticks()


def _linux_boot_seconds(root: str = PROC_ROOT) -> int:
    path = f"{root}/stat"
    try:
        with open(path, "rb") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise RunLivenessError(f"cannot read {path}: {exc}") from exc
    found = [line.split() for line in lines if line.startswith(b"btime ")]
    try:
        if len(found) != 1 or len(found[0]) != 2:
            raise ValueError("expected exactly one 'btime <seconds>' line")
        return int(found[0][1])
    except ValueError as exc:
        raise RunLivenessError(f"unreadable boot time in {path}: {exc}") from exc


def _clock_ticks() -> int:
    ticks = os.sysconf("SC_CLK_TCK")
    if ticks <= 0:
        raise RunLivenessError(f"invalid SC_CLK_TCK {ticks}")
    return ticks


def current_process_identity() -> dict:
    """The identity a writer records for the run that this process executes."""
    pid = os.getpid()
    started = process_start_time(pid)
    if started is None:
        raise RunLivenessError(f"the kernel returned no record of this process (PID {pid})")
    return {"pid": pid, "started": started}


def recorded_identity(run: dict) -> dict | None:
    """Return the recorded identity of ``run``, or None when it has none."""
    raw = run["stats_json"]
    # An empty value is read as no stats, as proposal_dispositions reads it.
    try:
        stats = json.loads(raw) if raw != "" else {}
    except (TypeError, ValueError) as exc:
        raise RunLivenessError(f"run {run['id']}: unreadable stats_json") from exc
    if not isinstance(stats, dict):
        raise RunLivenessError(f"run {run['id']}: stats_json is not an object")
    if PROCESS_KEY not in stats:
        return None
    identity = stats[PROCESS_KEY]
    if (not isinstance(identity, dict) or set(identity) != {"pid", "started"}
            or type(identity["pid"]) is not int or not 0 < identity["pid"] <= PID_MAX
            or not isinstance(identity["started"], str) or not _is_iso_utc(identity["started"])):
        raise RunLivenessError(f"run {run['id']}: invalid recorded process identity {identity!r}")
    return identity


def _is_iso_utc(value: str) -> bool:
    # strptime also accepts unpadded fields and a lower-case "t" or "z". The
    # kernel reader always prints the canonical form, so only that form can
    # ever compare equal. Any other spelling is a corrupt identity.
    try:
        return datetime.strptime(value, _ISO_UTC).strftime(_ISO_UTC) == value
    except ValueError:
        return False


def run_liveness(run: dict, cfg, *, now: datetime | None = None,
                 readings: dict | None = None) -> str:
    """Return 'live', 'ended' or 'unknown' for a row whose status is running.

    ``readings`` maps a PID to its start time as already read in this
    transaction; a new reading is added to it. Every error names the run, so
    the operator knows which row to repair.
    """
    identity = recorded_identity(run)
    if identity is not None:
        pid = identity["pid"]
        if readings is not None and pid in readings:
            current = readings[pid]
        else:
            try:
                current = process_start_time(pid)
            except RunLivenessError as exc:
                raise RunLivenessError(f"run {run['id']}: {exc}") from exc
            if readings is not None:
                readings[pid] = current
        return "live" if current == identity["started"] else "ended"
    # Legacy rows: the age rule from before process identities were recorded.
    try:
        started = datetime.fromisoformat((run["started"] or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise RunLivenessError(f"run {run['id']}: the comparison time must carry a timezone")
    return "live" if started >= now - timedelta(hours=cfg.run_stale_after_hours) else "ended"


def live_run_ids(store, cfg, *, now: datetime | None = None) -> set[str]:
    """Running rows whose run may still deliver: live or unknown, never ended.

    Inside one owned Store transaction, each recorded process is read once
    and the clock for legacy rows is read once (`Store.transaction_memo`).
    Every Review membership read in that transaction then agrees, as the
    database snapshot does. A process that ends during the transaction is
    seen by the next one. Outside an owned transaction every call reads afresh.
    """
    rows = store.query("SELECT id, started, stats_json FROM runs WHERE status='running'")
    memo = store.transaction_memo()
    readings = None
    if memo is not None:
        if __name__ not in memo:
            memo[__name__] = {"now": datetime.now(timezone.utc), "processes": {}}
        shared = memo[__name__]
        now = now if now is not None else shared["now"]
        readings = shared["processes"]
    return {row["id"] for row in rows if run_liveness(row, cfg, now=now, readings=readings) != "ended"}


def recording_stats(stats: dict) -> dict:
    """Return ``stats`` with this process recorded as the run's live owner."""
    return {**stats, PROCESS_KEY: current_process_identity()}
