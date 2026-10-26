"""BL-55: a running run is live exactly while its recorded process exists.

The pipeline's stale-run reaper and Review membership share one predicate,
`run_liveness.run_liveness`. A run records its PID and process start time;
the same PID with another start time is a reused PID, not the run. Rows that
record no process keep the age rule. Live PIDs here are real short-lived
child processes, ended and reaped by the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from self_improve.config import Config
from self_improve.dashboard import queries
from self_improve.execution_policy import PolicyError, proposal_dispositions, set_class_policy
from self_improve.pipeline import finalize_stale_runs
from self_improve.run_liveness import (
    RunLivenessError, current_process_identity, process_start_time, run_liveness,
)
from self_improve.store import Store
from tests.test_apply import NEW, OLD, insert_proposal, make_diff
from tests.test_model_jobs import cfg, env, store  # noqa: F401  (fixtures for the writer tests)


def live_run_stats(**stats) -> str:
    """Stats for a run that this test process owns, as the pipeline records them."""
    return json.dumps({**stats, "process": current_process_identity()})


@contextmanager
def child_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        yield proc
    finally:
        end(proc)


def end(proc) -> None:
    """Kill and reap, so the process table no longer lists the child."""
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)


def identity_of(proc) -> dict:
    started = process_start_time(proc.pid)
    assert started is not None, proc.pid
    return {"pid": proc.pid, "started": started}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@pytest.fixture
def state(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "state"))
    with closing(Store(tmp_path / "state" / "state.db")) as store:
        set_class_policy(store, "global", True, now="2020-01-01T00:00:00Z")
        yield store, cfg


def _run_with_proposal(store, tmp_path, run_id, *, started, stats_json):
    store.insert("runs", {"id": run_id, "started": started, "status": "running", "stats_json": stats_json})
    proposal = insert_proposal(store, target=tmp_path / f"{run_id}.md", diff=make_diff(OLD, NEW))
    store.update("proposals", "id", proposal["id"], {"run_id": run_id})
    store.commit()
    return proposal["id"]


def _step(store, cfg, proposal_id):
    row = {p["id"]: p for p in proposal_dispositions(store, cfg)}[proposal_id]
    return row["next_step"], row["execution"]["reason"]


def _status(store, run_id):
    return store.query_one("SELECT status FROM runs WHERE id=?", (run_id,))["status"]


def test_the_start_time_of_a_child_is_read_while_it_lives_and_none_once_it_is_gone():
    with child_process() as proc:
        first = process_start_time(proc.pid)
        assert first is not None and first == process_start_time(proc.pid)
        datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ")
        end(proc)
        assert process_start_time(proc.pid) is None


#: How long make_zombie polls for the zombie when os.waitid is missing.
ZOMBIE_WAIT_S = 30


def make_zombie(proc) -> None:
    """Kill ``proc`` and wait until it has exited, without reaping it.

    `os.waitid` with WNOWAIT waits without reaping. On macOS it exists only
    from Python 3.13, and the project supports 3.12. Without it, poll the
    module's own process reader until it reports the zombie, for at most
    ZOMBIE_WAIT_S seconds. That loop never reaps the child either.
    """
    from self_improve import run_liveness as liveness

    proc.kill()
    if hasattr(os, "waitid"):
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        return
    deadline = time.monotonic() + ZOMBIE_WAIT_S
    while True:
        record = liveness._process_record(proc.pid)
        assert record is not None, f"PID {proc.pid} vanished before the reader saw its zombie"
        if record[0]:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"PID {proc.pid} was not a zombie {ZOMBIE_WAIT_S} s after SIGKILL")
        time.sleep(0.01)


def test_make_zombie_polls_the_reader_where_os_waitid_is_missing(monkeypatch):
    """BL-55 review: under Python 3.12 on macOS, os.waitid is missing and both
    zombie tests failed with AttributeError. The fallback must still leave an
    exited, unreaped child."""
    import signal

    from self_improve import run_liveness as liveness

    monkeypatch.delattr(os, "waitid", raising=False)
    with child_process() as proc:
        make_zombie(proc)
        # Still in the process table, so not reaped, and marked as exited.
        record = liveness._process_record(proc.pid)
        assert record is not None and record[0] is True, record
        # The kernel's own wait status, independent of the reader: the child
        # had already exited, so a non-blocking reap returns at once.
        assert proc.poll() == -signal.SIGKILL


def ps_reading(pid: int) -> tuple[str, str] | None:
    """(state, ISO UTC start) as `ps` prints them in the C locale and UTC.

    This is what the earlier `ps` reader stored. None when ps lists no such
    process. A sandbox that refuses to run the setuid ps skips the caller.
    """
    try:
        done = subprocess.run(["/bin/ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=30,
                              env={"LC_ALL": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"})
    except PermissionError as exc:
        pytest.skip(f"this sandbox refuses to run /bin/ps ({exc}); the ps cross-check needs it")
    if done.returncode == 1 and not done.stdout.strip():
        return None
    assert done.returncode == 0 and not done.stderr.strip(), done
    state, lstart = done.stdout.strip().split(None, 1)
    started = datetime.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y")
    return state, started.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_kernel_reader_matches_what_ps_printed_for_live_processes():
    """BL-55 review: identities recorded by the earlier ps reader must still
    compare equal. This process, a child and PID 1 read the same as ps."""
    with child_process() as proc:
        for pid in (os.getpid(), proc.pid, 1):
            state, started = ps_reading(pid)
            assert not state.startswith("Z"), (pid, state)
            assert process_start_time(pid) == started, pid


def test_the_struct_offsets_match_ps_for_a_live_child_and_its_zombie():
    """The macOS reader relies on three kinfo_proc offsets: the start time
    (0), p_stat (36) and p_pid (40, checked inside the reader). Linux reads
    the state and field 22 of /proc/<pid>/stat. Both must agree with ps."""
    from self_improve import run_liveness as liveness

    with child_process() as proc:
        alive = liveness._process_record(proc.pid)
        state, started = ps_reading(proc.pid)
        assert alive == (False, int(datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
                                    .replace(tzinfo=timezone.utc).timestamp()))
        make_zombie(proc)
        assert liveness._process_record(proc.pid) == (True, alive[1])
        state, again = ps_reading(proc.pid)  # "Z", or "Z+" in a terminal's foreground group
        assert state.startswith("Z") and again == started


def test_an_exited_child_that_is_not_yet_reaped_is_not_alive(state, tmp_path):
    """A crashed run whose parent has not reaped it is a zombie. The kernel
    still holds its record and start time, but the run can no longer deliver."""
    store, cfg = state
    with child_process() as proc:
        pid = _run_with_proposal(store, tmp_path, "zombie-run", started=_iso(datetime.now(timezone.utc)),
                                 stats_json=json.dumps({"process": identity_of(proc)}))
        make_zombie(proc)
        assert process_start_time(proc.pid) is None
        assert _step(store, cfg, pid) == ("review", "automatic_delivery_missed")
        assert finalize_stale_runs(store, cfg)["abandoned_process_gone"] == 1


def test_an_unsupported_platform_raises_with_the_run_id(state, tmp_path, monkeypatch):
    """No fallback to alive or dead on a platform this reader cannot read."""
    from types import SimpleNamespace

    from self_improve import run_liveness as liveness

    store, cfg = state
    _run_with_proposal(store, tmp_path, "elsewhere-run", started=_iso(datetime.now(timezone.utc)),
                       stats_json=live_run_stats())
    monkeypatch.setattr(liveness, "sys", SimpleNamespace(platform="sunos5"))
    with pytest.raises(RunLivenessError, match="run elsewhere-run: cannot read process start times "
                                               "on platform 'sunos5'"):
        finalize_stale_runs(store, cfg)
    with pytest.raises(PolicyError, match="run elsewhere-run: cannot read process start times"):
        proposal_dispositions(store, cfg)
    assert _status(store, "elsewhere-run") == "running"


def test_a_pid_that_exists_without_a_kernel_record_raises_instead_of_reading_as_dead(monkeypatch):
    """A sandbox or a hidepid mount can hide a live process. Hidden is not dead."""
    from self_improve import run_liveness as liveness

    monkeypatch.setattr(liveness, "_process_record", lambda pid: None)
    with pytest.raises(RunLivenessError, match=f"PID {os.getpid()} exists, but the kernel returned no"):
        process_start_time(os.getpid())
    with pytest.raises(RunLivenessError, match="PID 1 exists"):  # EPERM still means it exists
        process_start_time(1)


@pytest.mark.skipif(sys.platform != "darwin", reason="the sysctl reader runs only on macOS")
@pytest.mark.parametrize("damage, message", [
    ("load", "cannot load sysctl from the C library: invented missing symbol"),
    ("errno", "sysctl KERN_PROC_PID for PID .* failed: Operation not permitted"),
    ("short", "sysctl returned 100 bytes for PID .*; this reader expects a 648-byte struct kinfo_proc"),
    ("other_pid", "the sysctl record for PID .* names PID .*: the struct kinfo_proc layout differs"),
])
def test_an_unreadable_sysctl_record_raises(monkeypatch, damage, message):
    import ctypes
    import errno
    import struct

    from self_improve import run_liveness as liveness

    real = liveness._sysctl()

    def fake(mib, count, buffer, size, new, new_size):
        if damage == "errno":
            ctypes.set_errno(errno.EPERM)
            return -1
        assert real(mib, count, buffer, size, new, new_size) == 0
        if damage == "short":
            size._obj.value = 100
        else:
            struct.pack_into("=i", buffer, liveness.P_PID, mib[3] + 1)
        return 0

    def load():
        if damage == "load":
            raise AttributeError("invented missing symbol")
        return fake
    monkeypatch.setattr(liveness, "_sysctl", load)
    with pytest.raises(RunLivenessError, match=message):
        process_start_time(os.getpid())


def _proc_stat(pid: int, comm: bytes, state: str, ticks: int) -> bytes:
    # Fields 3 to 52 of /proc/<pid>/stat; field 22 (index 19 here) is starttime.
    rest = [state] + ["7"] * 18 + [str(ticks)] + ["0"] * 30
    return f"{pid} (".encode() + comm + b") " + " ".join(rest).encode() + b"\n"


def test_the_linux_reader_parses_proc_and_refuses_what_it_cannot_read(tmp_path):
    """Exercised on every platform against an invented /proc tree."""
    from self_improve import run_liveness as liveness

    hz = os.sysconf("SC_CLK_TCK")
    (tmp_path / "stat").write_bytes(b"cpu  1 2 3\nbtime 1760000000\nprocesses 9\n")
    proc_dir = tmp_path / "4321"
    proc_dir.mkdir()
    comm = b"a (strange) name)"  # spaces and parentheses, as a real comm may hold
    (proc_dir / "stat").write_bytes(_proc_stat(4321, comm, "S", 250 * hz + hz - 1))
    assert liveness._linux_record(4321, str(tmp_path)) == (False, 1760000250)
    for exited in ("Z", "X"):
        (proc_dir / "stat").write_bytes(_proc_stat(4321, comm, exited, 250 * hz))
        assert liveness._linux_record(4321, str(tmp_path)) == (True, 1760000250)
    assert liveness._linux_record(4322, str(tmp_path)) is None
    (proc_dir / "stat").write_bytes(_proc_stat(9999, comm, "S", 1))
    with pytest.raises(RunLivenessError, match="unreadable .*4321/stat: unexpected field layout"):
        liveness._linux_record(4321, str(tmp_path))
    (proc_dir / "stat").write_bytes(b"4321 (no closing parenthesis S 1 2 3\n")
    with pytest.raises(RunLivenessError, match="unexpected field layout"):
        liveness._linux_record(4321, str(tmp_path))
    (proc_dir / "stat").write_bytes(_proc_stat(4321, comm, "S", 1))
    (tmp_path / "stat").write_bytes(b"cpu  1 2 3\n")
    with pytest.raises(RunLivenessError, match="unreadable boot time .*expected exactly one 'btime"):
        liveness._linux_record(4321, str(tmp_path))


def test_a_run_whose_recorded_process_lives_holds_its_proposal_and_is_never_reaped(state, tmp_path):
    """A month-old run is far past any age threshold. Its process still runs,
    so it keeps its proposal out of Review and the reaper leaves it alone."""
    store, cfg = state
    with child_process() as proc:
        month_old = _iso(datetime.now(timezone.utc) - timedelta(days=30))
        pid = _run_with_proposal(store, tmp_path, "long-run", started=month_old,
                                 stats_json=json.dumps({"process": identity_of(proc)}))
        assert _step(store, cfg, pid) == ("automatic_delivery", "")
        assert pid not in queries.waiting_proposal_ids(store, cfg)
        stats = finalize_stale_runs(store, cfg)
        assert (stats["abandoned"], stats["left_running"]) == (0, 1)
        assert _status(store, "long-run") == "running"


def test_a_crashed_run_releases_its_proposal_at_once_and_is_reaped_at_the_next_start(state, tmp_path):
    """BL-55: a crashed run held its proposals out of Review until a later
    pipeline start found it older than run_stale_after_hours (24 hours)."""
    store, cfg = state
    with child_process() as proc:
        just_now = _iso(datetime.now(timezone.utc))
        pid = _run_with_proposal(store, tmp_path, "crashed-run", started=just_now,
                                 stats_json=json.dumps({"review_only": False, "process": identity_of(proc)}))
        assert pid not in queries.waiting_proposal_ids(store, cfg)
        end(proc)
        # Still 'running' in the database: no pipeline has started since.
        assert _status(store, "crashed-run") == "running"
        assert _step(store, cfg, pid) == ("review", "automatic_delivery_missed")
        assert pid in queries.waiting_proposal_ids(store, cfg)
        stats = finalize_stale_runs(store, cfg)
    assert (stats["abandoned"], stats["abandoned_process_gone"], stats["abandoned_by_age"]) == (1, 1, 0)
    row = store.query_one("SELECT * FROM runs WHERE id='crashed-run'")
    assert row["status"] == "abandoned" and row["finished"]


def test_a_reused_pid_with_another_start_time_does_not_keep_a_run_alive(state, tmp_path):
    store, cfg = state
    with child_process() as proc:
        reused = {"pid": proc.pid, "started": "2001-01-01T00:00:00Z"}
        pid = _run_with_proposal(store, tmp_path, "reused-run", started=_iso(datetime.now(timezone.utc)),
                                 stats_json=json.dumps({"process": reused}))
        assert run_liveness(store.query_one("SELECT * FROM runs WHERE id='reused-run'"), cfg) == "ended"
        assert _step(store, cfg, pid) == ("review", "automatic_delivery_missed")
        assert finalize_stale_runs(store, cfg)["abandoned_process_gone"] == 1
        assert proc.poll() is None, "the reaper judged the run, it never touched the process"
    assert _status(store, "reused-run") == "abandoned"


def test_a_legacy_run_without_a_recorded_process_keeps_the_age_rule(state, tmp_path):
    store, cfg = state
    now = datetime.now(timezone.utc)
    recent = _run_with_proposal(store, tmp_path, "recent-legacy", started=_iso(now - timedelta(hours=2)),
                                stats_json="{}")
    old = _run_with_proposal(store, tmp_path, "old-legacy", started=_iso(now - timedelta(hours=48)),
                             stats_json='{"review_only": false}')
    unreadable = _run_with_proposal(store, tmp_path, "unreadable-legacy", started="not-a-timestamp",
                                    stats_json="{}")
    assert _step(store, cfg, recent) == ("automatic_delivery", "")
    assert _step(store, cfg, old) == ("review", "automatic_delivery_missed")
    # Unknown age is not ended: Review keeps it held, as before, and the
    # reaper counts it rather than guessing.
    assert _step(store, cfg, unreadable) == ("automatic_delivery", "")
    stats = finalize_stale_runs(store, cfg)
    assert (stats["abandoned"], stats["abandoned_by_age"], stats["left_running"],
            stats["unparseable_started"]) == (1, 1, 1, 1)
    assert [_status(store, r) for r in ("recent-legacy", "old-legacy", "unreadable-legacy")] == [
        "running", "abandoned", "running"]


@pytest.mark.parametrize("stats_json", [
    "{oops",
    '["process"]',
    '{"process": "pid 12"}',
    '{"process": {"pid": "12", "started": "2026-10-17T00:00:00Z"}}',
    '{"process": {"pid": true, "started": "2026-10-17T00:00:00Z"}}',
    '{"process": {"pid": -4, "started": "2026-10-17T00:00:00Z"}}',
    '{"process": {"pid": 12, "started": "yesterday"}}',
    '{"process": {"pid": 12, "started": "2026-10-17T00:00:00+00:00"}}',
    # strptime parses these, but the kernel reader always prints the padded,
    # upper-case form, so they never compare equal: a live run read as ended.
    '{"process": {"pid": 12, "started": "2026-10-18T1:0:10Z"}}',
    '{"process": {"pid": 12, "started": "2026-1-8T01:00:10Z"}}',
    '{"process": {"pid": 12, "started": "2026-10-18T01:00:10z"}}',
    '{"process": {"pid": 12, "started": "2026-10-18t01:00:10Z"}}',
    '{"process": {"pid": 12}}',
    '{"process": {"pid": 12, "started": "2026-10-17T00:00:00Z", "host": "x"}}',
    # Beyond a 32-bit pid_t: no process can carry this PID.
    '{"process": {"pid": 2147483648, "started": "2026-10-17T00:00:00Z"}}',
    '{"process": {"pid": 99999999999, "started": "2026-10-17T00:00:00Z"}}',
    '{"process": {"pid": 18446744073709551616, "started": "2026-10-17T00:00:00Z"}}',
])
def test_an_unreadable_recorded_identity_raises_with_the_run_id(state, tmp_path, stats_json):
    """Neither the reaper nor Review may count a corrupt identity as alive or
    dead. Both refuse by run id, and the reaper writes nothing."""
    store, cfg = state
    healthy_dead = {"pid": 1, "started": "2001-01-01T00:00:00Z"}  # judged first, never reaped
    store.insert("runs", {"id": "a-dead-run", "started": "2026-10-17T00:00:00Z", "status": "running",
                          "stats_json": json.dumps({"process": healthy_dead})})
    _run_with_proposal(store, tmp_path, "corrupt-run", started=_iso(datetime.now(timezone.utc)),
                       stats_json=stats_json)
    with pytest.raises(RunLivenessError, match="run corrupt-run"):
        finalize_stale_runs(store, cfg)
    assert {r["id"]: r["status"] for r in store.query("SELECT id, status FROM runs")} == {
        "a-dead-run": "running", "corrupt-run": "running"}
    with pytest.raises(PolicyError, match="run corrupt-run"):
        proposal_dispositions(store, cfg)


def test_a_failed_process_read_names_the_run_in_the_reaper_and_in_review(state, tmp_path, monkeypatch):
    """BL-55 review: the operator needs the run id to repair a row. A failed
    process-table read for a well-formed identity must name the run too."""
    from self_improve import run_liveness as liveness

    store, cfg = state
    _run_with_proposal(store, tmp_path, "unreadable-process-run", started=_iso(datetime.now(timezone.utc)),
                       stats_json=json.dumps({"process": {"pid": 4242, "started": "2026-10-17T00:00:00Z"}}))

    def refuse(pid):
        raise RunLivenessError(f"invented process-table failure for PID {pid}")
    monkeypatch.setattr(liveness, "process_start_time", refuse)
    with pytest.raises(RunLivenessError, match="run unreadable-process-run: invented process-table failure"):
        finalize_stale_runs(store, cfg)
    assert _status(store, "unreadable-process-run") == "running"
    with pytest.raises(PolicyError, match="run unreadable-process-run: invented process-table failure"):
        proposal_dispositions(store, cfg)


def test_the_reaper_leaves_a_row_that_changed_after_it_was_judged(state, tmp_path, monkeypatch):
    """A model-job worker can resume an ended run between the reaper's read
    and its write. The write matches the row as judged, so the resumed run,
    which now records its new owner, stays running."""
    from self_improve import run_liveness as liveness

    store, cfg = state
    dead = {"pid": 1, "started": "2001-01-01T00:00:00Z"}
    store.insert("runs", {"id": "resumed-run", "started": _iso(datetime.now(timezone.utc)), "status": "running",
                          "stats_json": json.dumps({"process": dead})})
    store.commit()
    real = liveness.run_liveness

    def judged_then_resumed(row, *args, **kwargs):
        verdict = real(row, *args, **kwargs)
        store.update("runs", "id", row["id"], {"stats_json": live_run_stats()})
        return verdict
    monkeypatch.setattr(liveness, "run_liveness", judged_then_resumed)
    stats = finalize_stale_runs(store, cfg)
    assert (stats["abandoned"], stats["changed_since_read"]) == (0, 1)
    assert _status(store, "resumed-run") == "running"


def test_review_reads_liveness_from_a_read_only_store(state, tmp_path):
    """The Review reader may read the process table but never writes."""
    store, cfg = state
    with child_process() as proc:
        held = _run_with_proposal(store, tmp_path, "held-run", started=_iso(datetime.now(timezone.utc)),
                                  stats_json=json.dumps({"process": identity_of(proc)}))
        end(proc)
        with closing(Store(store.db_path, read_only=True)) as reader:
            assert held in queries.waiting_proposal_ids(reader, cfg)
    assert _status(store, "held-run") == "running"


def test_one_transaction_reads_each_recorded_process_once(state, tmp_path, monkeypatch):
    """BL-55 review: every Review membership read in one transaction must
    agree, as the database snapshot does. A process that ends during the
    transaction is seen by the next one."""
    from self_improve import run_liveness as liveness

    store, cfg = state
    reads = []
    real = liveness.process_start_time

    def counted(pid):
        reads.append(pid)
        return real(pid)
    monkeypatch.setattr(liveness, "process_start_time", counted)
    with child_process() as proc:
        held = _run_with_proposal(store, tmp_path, "ends-mid-read", started=_iso(datetime.now(timezone.utc)),
                                  stats_json=json.dumps({"process": identity_of(proc)}))
        with closing(Store(store.db_path, read_only=True)) as reader:
            with reader.transaction():
                assert _step(reader, cfg, held) == ("automatic_delivery", "")
                end(proc)
                assert _step(reader, cfg, held) == ("automatic_delivery", "")
                assert held not in queries.waiting_proposal_ids(reader, cfg)
            assert reads == [proc.pid]
            # Outside an owned transaction every call reads afresh, as before.
            assert _step(reader, cfg, held) == ("review", "automatic_delivery_missed")
            with reader.transaction():
                assert _step(reader, cfg, held) == ("review", "automatic_delivery_missed")
        assert reads == [proc.pid] * 3


def test_the_review_queue_agrees_with_itself_when_a_run_ends_during_the_request(state, tmp_path, monkeypatch):
    """review_queue checks its grouped proposals against a second membership
    read. A run that ended between the two reads made the route a 500."""
    pytest.importorskip("fastapi", reason="the dashboard extra is not installed")
    from fastapi.testclient import TestClient

    from self_improve import run_liveness as liveness
    from self_improve.dashboard.app import create_app

    store, cfg = state
    recorded = {"pid": 4242, "started": "2026-10-17T00:00:00Z"}
    held = _run_with_proposal(store, tmp_path, "ends-mid-request", started=_iso(datetime.now(timezone.utc)),
                              stats_json=json.dumps({"process": recorded}))
    reads = []

    def ends_after_the_first_read(pid):
        reads.append(pid)
        return recorded["started"] if len(reads) == 1 else None
    monkeypatch.setattr(liveness, "process_start_time", ends_after_the_first_read)
    with TestClient(create_app(cfg, db_path=store.db_path), raise_server_exceptions=False) as client:
        response = client.get("/api/review-queue")
        assert response.status_code == 200, response.text
        assert held not in {p["id"] for family in response.json()["families"] for p in family["proposals"]}
        assert reads == [4242]
        # Overview resolves membership six times; it reads the process once.
        reads.clear()
        assert client.get("/api/overview").status_code == 200
        assert reads == [4242]


# ---------------------------------------------------------------------------
# Every writer that creates a running row records its own process.
# ---------------------------------------------------------------------------

def _running_row(store, run_id=None):
    sql = "SELECT * FROM runs WHERE status='running'" + (" AND id=?" if run_id else "")
    rows = store.query(sql, (run_id,) if run_id else ())
    assert len(rows) == 1, rows
    return rows[0]


def test_the_pipeline_records_its_process_before_any_stage_runs(tmp_path, monkeypatch):
    from dataclasses import replace

    from self_improve import scan
    from self_improve.pipeline import run_pipeline

    for d in ("claude/projects", "codex/sessions", "codex/archived", "skills", "codex/skills"):
        (tmp_path / d).mkdir(parents=True)
    cfg = replace(Config(), state_dir=str(tmp_path / "state"),
                  claude_projects_dir=str(tmp_path / "claude/projects"),
                  claude_history_path=str(tmp_path / "claude/history.jsonl"),
                  codex_sessions_dir=str(tmp_path / "codex/sessions"),
                  codex_archived_dir=str(tmp_path / "codex/archived"),
                  claude_managed_dir=str(tmp_path / "managed"),
                  global_claude_md=str(tmp_path / "global/CLAUDE.md"),
                  codex_global_agents_md=str(tmp_path / "codex/AGENTS.md"),
                  skills_dir=str(tmp_path / "skills"), codex_skills_dir=str(tmp_path / "codex/skills"))
    seen = []
    real_scan = scan.scan_all

    def observe(store, *args, **kwargs):
        seen.append(json.loads(_running_row(store)["stats_json"]))
        return real_scan(store, *args, **kwargs)
    monkeypatch.setattr(scan, "scan_all", observe)
    with closing(Store(cfg.state_path("state.db"))) as store:
        run_pipeline(cfg, store, dry_run=True)
    assert len(seen) == 1 and seen[0]["process"] == current_process_identity()


def test_a_manual_regate_records_its_process(env):
    from self_improve.llm import LLMRunner
    from self_improve.pipeline import gate_existing_proposal

    cfg, store, proposal, calls = env
    seen = []

    def inspect(*args, **kwargs):
        seen.append(json.loads(_running_row(store)["stats_json"]))
        return LLMRunner(*args, **kwargs)
    gate_existing_proposal(cfg, store, proposal["id"], _llm_factory=inspect)
    assert seen == [{"process": current_process_identity()}]


def test_a_model_job_records_its_process_and_a_resumed_job_records_the_new_owner(env):
    """A resumed job keeps the earlier stats of its run and replaces the owner."""
    from self_improve import job_worker
    from self_improve.commands import submit_command
    from self_improve.llm import LLMRunner
    from self_improve.store import new_id
    from tests.test_model_jobs import request

    cfg, store, proposal, calls = env
    queued = submit_command(store, cfg, request(env))
    seen = []

    def broken(*args, **kwargs):
        seen.append(json.loads(_running_row(store)["stats_json"]))
        raise ValueError("invented initialization failure")
    failed = job_worker.run_once(store, cfg, _llm_factory=broken)
    assert seen == [{"process": current_process_identity()}]
    stale = {"pid": 1, "started": "2001-01-01T00:00:00Z"}
    previous = json.loads(store.query_one("SELECT stats_json FROM runs WHERE id=?", (failed["run_id"],))["stats_json"])
    assert previous["error_code"] == "ValueError"
    store.update("runs", "id", failed["run_id"], {"stats_json": json.dumps({**previous, "process": stale})})
    store.commit()
    submit_command(store, cfg, {"action": "resume_job", "command_id": queued["id"], "request_key": new_id()})

    def inspect(*args, **kwargs):
        seen.append(json.loads(_running_row(store, failed["run_id"])["stats_json"]))
        return LLMRunner(*args, **kwargs)
    assert job_worker.run_once(store, cfg, _llm_factory=inspect)["state"] == "completed"
    assert seen[1] == {**previous, "process": current_process_identity()}


@pytest.mark.parametrize("damage, message", [
    ("UPDATE runs SET stats_json='{oops' WHERE id=?", "unreadable stats_json"),
    ("UPDATE runs SET stats_json='[1]' WHERE id=?", "stats_json is not an object"),
    ("DELETE FROM runs WHERE id=?", "is missing"),
])
def test_a_resumed_model_job_blocks_on_a_run_row_it_cannot_read(env, damage, message):
    """The resume path must not overwrite or skip a run it cannot read.

    BL-55 review: it raised instead, and `serve` re-raised, so the worker
    exited; launchd restarted it on the same command forever and nothing was
    recorded. The command is now blocked by name, as JobRuntimeChanged is,
    and the worker moves on."""
    from self_improve import job_worker
    from self_improve.commands import submit_command
    from self_improve.store import new_id
    from tests.test_model_jobs import request

    cfg, store, proposal, calls = env
    queued = submit_command(store, cfg, request(env))

    def broken(*args, **kwargs):
        raise ValueError("invented initialization failure")
    failed = job_worker.run_once(store, cfg, _llm_factory=broken)
    submit_command(store, cfg, {"action": "resume_job", "command_id": queued["id"], "request_key": new_id()})
    store.conn.execute("PRAGMA foreign_keys=OFF")
    store.conn.execute(damage, (failed["run_id"],))
    store.commit()
    run_before = store.query("SELECT * FROM runs WHERE id=?", (failed["run_id"],))
    # The real service entry point records the cause and exits 1, not a raise.
    assert job_worker.serve(cfg, once=True) == 1
    result = job_worker.jobs.status(store, store.query_one("SELECT * FROM commands WHERE id=?", (queued["id"],)))
    assert (result["id"], result["state"], result["error_code"]) == (queued["id"], "blocked", "JobRunUnreadable")
    assert f"run {failed['run_id']}" in result["error_detail"] and message in result["error_detail"]
    assert result["can_retry"] is True  # after the run row is repaired
    # The damaged run row is neither overwritten nor recreated.
    assert store.query("SELECT * FROM runs WHERE id=?", (failed["run_id"],)) == run_before
    # Nothing else is claimable, so the worker does not loop on this command.
    assert job_worker.run_once(store, cfg) is None
    assert not calls
