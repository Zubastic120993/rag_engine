"""Generation-scoped exclusive lock for certified append (operational only)."""

from __future__ import annotations

import atexit
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from rag_engine.certified_generation.exceptions import AppendConcurrencyError

LOCK_NAME = "certified_append.lock"


def append_lock_path(persist_dir: str | Path) -> Path:
    return Path(persist_dir) / LOCK_NAME


@contextmanager
def certified_append_lock(
    persist_dir: str | Path,
    *,
    timeout_s: float = 0,
) -> Iterator[Path]:
    """Exclusive lock scoped to one certified generation persist directory.

    timeout_s=0 ? fail immediately if locked.
    """
    persist = Path(persist_dir)
    persist.mkdir(parents=True, exist_ok=True)
    path = append_lock_path(persist)
    deadline = time.time() + timeout_s if timeout_s > 0 else None
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
            break
        except FileExistsError:
            if deadline is None or time.time() >= deadline:
                raise AppendConcurrencyError(
                    "another certified append holds the generation lock",
                    details={"lock": str(path)},
                ) from None
            time.sleep(0.05)

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
        yield path
    finally:
        atexit.unregister(_release)
        _release()
