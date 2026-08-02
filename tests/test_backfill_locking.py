from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rag_engine.backfill_collections as bc  # noqa: E402
from rag_engine.lock import IngestLockError  # noqa: E402


class DummyCollection:
    def __init__(self):
        self.updates = []

    def update(self, *, ids, metadatas):
        self.updates.append((list(ids), list(metadatas)))


class DummyChroma:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._collection = DummyCollection()
        DummyChroma.instances.append(self)

    def get(self, include=None):
        return {
            "ids": ["1", "2"],
            "metadatas": [
                {"source": "10_Company/doc.pdf", "collection": "other"},
                {"source": "90_CE_Wiki/note.md", "collection": "wiki"},
            ],
        }


@contextmanager
def _ok_lock(record):
    record.append("entered")
    try:
        yield
    finally:
        record.append("exited")



def test_backfill_write_path_uses_common_ingest_lock(monkeypatch):
    DummyChroma.instances.clear()
    lock_events = []
    monkeypatch.setattr(bc, "OllamaEmbeddings", lambda model: {"model": model})
    monkeypatch.setattr(bc, "Chroma", DummyChroma)
    monkeypatch.setattr(bc, "embed_model", lambda: "embed")
    monkeypatch.setattr(bc, "persist_dir", lambda: Path("/tmp/db"))
    monkeypatch.setattr(bc, "chroma_client_settings", lambda: object())
    monkeypatch.setattr(bc, "ingest_lock", lambda timeout_s=0: _ok_lock(lock_events))

    counts = bc.backfill(batch_size=10, dry_run=False)

    assert lock_events == ["entered", "exited"]
    assert counts["sms"] == 1
    assert counts["wiki"] == 1
    assert len(DummyChroma.instances) == 1
    assert DummyChroma.instances[0]._collection.updates == [
        (["1"], [{"source": "10_Company/doc.pdf", "collection": "sms"}])
    ]



def test_backfill_dry_run_skips_lock_and_write(monkeypatch):
    DummyChroma.instances.clear()
    lock_calls = {"n": 0}

    def fail_lock(timeout_s=0):
        lock_calls["n"] += 1
        raise AssertionError("lock should not be acquired for dry-run")

    monkeypatch.setattr(bc, "OllamaEmbeddings", lambda model: {"model": model})
    monkeypatch.setattr(bc, "Chroma", DummyChroma)
    monkeypatch.setattr(bc, "embed_model", lambda: "embed")
    monkeypatch.setattr(bc, "persist_dir", lambda: Path("/tmp/db"))
    monkeypatch.setattr(bc, "chroma_client_settings", lambda: object())
    monkeypatch.setattr(bc, "ingest_lock", fail_lock)

    counts = bc.backfill(batch_size=10, dry_run=True)

    assert lock_calls["n"] == 0
    assert counts["sms"] == 1
    assert counts["wiki"] == 1
    assert len(DummyChroma.instances) == 1
    assert DummyChroma.instances[0]._collection.updates == []



def test_backfill_lock_failure_is_clear(monkeypatch):
    monkeypatch.setattr(bc, "ingest_lock", lambda timeout_s=0: (_ for _ in ()).throw(IngestLockError("locked")))
    with pytest.raises(IngestLockError, match="locked"):
        bc.backfill(dry_run=False)
