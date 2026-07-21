"""File lock for ingest — prevents concurrent writers corrupting Chroma."""

from __future__ import annotations

import atexit
import os
import time
from contextlib import contextmanager
from typing import Iterator

from rag_engine.config import ingest_lock_file, persist_dir


class IngestLockError(RuntimeError):
    pass


@contextmanager
def ingest_lock(timeout_s: float = 0) -> Iterator[None]:
    """Exclusive lock around ingest/reindex.

    timeout_s=0 means fail immediately if locked; >0 waits up to that many seconds.
    """
    persist_dir().mkdir(parents=True, exist_ok=True)
    path = ingest_lock_file()
    deadline = time.time() + timeout_s if timeout_s > 0 else None
    fd = None
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
            break
        except FileExistsError:
            if deadline is None or time.time() >= deadline:
                raise IngestLockError(
                    f"Another ingest holds {path}. Retry later or remove a stale lock."
                ) from None
            time.sleep(0.5)

    def _release() -> None:
        nonlocal fd
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_release)
    try:
        yield
    finally:
        atexit.unregister(_release)
        _release()
