"""Minimal cross-platform exclusive file lock used by the JSONL store."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

if os.name == "nt":  # pragma: no cover - exercised on Windows CI
    import msvcrt
else:  # pragma: no cover - platform-specific import boundary
    import fcntl


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


def _thread_lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _registry_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


class FileLock:
    """Cross-thread, cross-process exclusive lock; the lock file remains after release."""

    def __init__(self, path: Path, timeout: float = 5.0, poll_interval: float = 0.01):
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._thread_lock: threading.Lock | None = None
        self._handle = None
        self._acquired = False

    def __enter__(self) -> Self:
        deadline = time.monotonic() + self.timeout
        self._thread_lock = _thread_lock_for(self.path)
        remaining = max(0.0, deadline - time.monotonic())
        if not self._thread_lock.acquire(timeout=remaining):
            raise TimeoutError("lock timeout")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_lock_file()
            self._handle = open(self.path, "r+b")
            self._handle.seek(0)
            while True:
                try:
                    self._try_lock()
                    self._acquired = True
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("lock timeout") from None
                    time.sleep(
                        min(self.poll_interval, max(0.001, deadline - time.monotonic()))
                    )
        except Exception:
            self._close_handle()
            self._thread_lock.release()
            self._thread_lock = None
            raise

    def _initialize_lock_file(self) -> None:
        """Create one lock byte without a race before any process attempts to lock."""
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            return
        try:
            os.write(descriptor, b"0")
        finally:
            os.close(descriptor)

    def _try_lock(self) -> None:
        if os.name == "nt":
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if os.name == "nt":
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def _close_handle(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if self._acquired:
                self._unlock()
        finally:
            self._acquired = False
            self._close_handle()
            if self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_lock = None


@contextmanager
def locked_file(path: Path, timeout: float = 5.0) -> Iterator[None]:
    """Concise lock context for the store."""
    with FileLock(path, timeout=timeout):
        yield


__all__ = ["FileLock", "locked_file"]
