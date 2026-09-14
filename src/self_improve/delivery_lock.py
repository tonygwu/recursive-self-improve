"""One local instruction-write lane, shared by worker, nightly, and CLI.

The OS releases this lock when a process exits, including an abrupt exit.
A persistent file is not a running lock. Keep the file: unlinking it would let
another process lock a different inode and enter the same critical section.
"""
from contextlib import contextmanager
import fcntl


class WriteBusy(RuntimeError):
    pass


@contextmanager
def instruction_write_lock(cfg):
    path = cfg.state_path('instruction-write.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WriteBusy('Another instruction writer holds the local execution lock.') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
