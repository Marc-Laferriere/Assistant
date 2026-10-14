"""Cross-process run lock, so two scheduled jobs (or a manual command and a
scheduled one) never ingest into the same database at the same instant.

The lock is an OS-level byte-range lock on a file in data/. The OS drops it
when the holding process exits - even if it crashes or is killed - so a stale
lock can't wedge the schedule.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager

from .config import DATA_DIR, ensure_dirs

log = logging.getLogger(__name__)

LOCK_PATH = DATA_DIR / "run.lock"
_POLL_SECONDS = 2.0


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def run_lock(name: str, wait_seconds: float = 0):
    """Hold the run lock for the duration of the block.

    Yields True when acquired. If another process holds it for longer than
    `wait_seconds`, yields False without running anything - the caller should
    skip its work.
    """
    ensure_dirs()
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_seconds
    acquired = _try_lock(fd)
    if not acquired and wait_seconds > 0:
        log.info("%s: another run is in progress, waiting up to %ds", name, wait_seconds)
    while not acquired and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        acquired = _try_lock(fd)
    try:
        if not acquired:
            log.info("%s: another run is in progress, skipping", name)
        yield acquired
    finally:
        if acquired:
            _unlock(fd)
        os.close(fd)
