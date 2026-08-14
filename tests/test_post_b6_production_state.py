"""POST_B6 read-only production state sentinels (Programme B - B6R).

Describes legitimate post-B6 production facts without mutating production data
and without implying cutover has occurred.

Runs critical checks in a clean subprocess so other tests' monkeypatches cannot
pollute production path / config / compatibility evaluation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = r"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from rag_engine.certified_generation.paths import (
    PRODUCTION_GENERATIONS_ROOT,
    PRODUCTION_RAG_DB,
    PRODUCTION_RAG_STATE,
    PRODUCTION_REGISTRY_DB,
)
from rag_engine.config import persist_dir
from rag_engine.index_compatibility.compatibility import evaluate_compatibility
from rag_engine.index_compatibility.exceptions import FingerprintLegacyBlockedError
from rag_engine.index_compatibility.policy import enforce_ingest_compatibility
import rag_engine.config as cfg

ACCEPTED_GEN = PRODUCTION_GENERATIONS_ROOT / "raggen_20260814T182037Z_698e0df44604"
ACCEPTED_GEN_ID = "raggen:20260814T182037Z:698e0df44604"
EXPECTED_VECTORS = 124638

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def freeze():
    paths = [
        PRODUCTION_RAG_DB / "chroma.sqlite3",
        PRODUCTION_RAG_DB / "embedded.json",
        PRODUCTION_RAG_DB / "index_fingerprint.json",
        ACCEPTED_GEN / "chroma.sqlite3",
        ACCEPTED_GEN / "embedded.json",
        ACCEPTED_GEN / "index_embedding_fingerprint_v1.json",
        ACCEPTED_GEN / "certified_generation_checkpoint.json",
        PRODUCTION_REGISTRY_DB,
    ]
    out = {}
    for p in paths:
        if not p.exists():
            out[str(p)] = (False, None, None, None)
            continue
        st = p.stat()
        digest = None if st.st_size > 50_000_000 else sha256(p)
        out[str(p)] = (True, digest, st.st_size, st.st_mtime_ns)
    return out

# Ensure default production config (no session cutover env).
os.environ.pop("RAG_DB_PATH", None)
os.environ.pop("CE_LIBRARY_ROOT", None)
cfg.load_registry.cache_clear()

before = freeze()

assert PRODUCTION_RAG_DB.is_dir()
assert not (PRODUCTION_RAG_DB / "index_embedding_fingerprint_v1.json").exists()
old = evaluate_compatibility(PRODUCTION_RAG_DB)
assert old.state == "UNKNOWN_LEGACY", old

assert PRODUCTION_GENERATIONS_ROOT.is_dir()
assert not PRODUCTION_GENERATIONS_ROOT.is_symlink()
assert ACCEPTED_GEN.is_dir()
assert not ACCEPTED_GEN.is_symlink()
assert ACCEPTED_GEN.resolve() != PRODUCTION_RAG_DB.resolve()
assert (ACCEPTED_GEN / "index_embedding_fingerprint_v1.json").is_file()

new = evaluate_compatibility(ACCEPTED_GEN)
assert new.state == "KNOWN_COMPATIBLE", new
assert new.vector_count == EXPECTED_VECTORS

ckpt = json.loads((ACCEPTED_GEN / "certified_generation_checkpoint.json").read_text(encoding="utf-8"))
assert ckpt["generation_id"] == ACCEPTED_GEN_ID
assert ckpt["state"] == "BUILT_UNVALIDATED"
assert ckpt["accepted"] is False
assert ckpt["vector_count"] == EXPECTED_VECTORS

assert PRODUCTION_RAG_STATE.is_dir()
assert PRODUCTION_REGISTRY_DB.is_file()
uri = f"file:{PRODUCTION_REGISTRY_DB}?mode=ro"
con = sqlite3.connect(uri, uri=True)
try:
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0] == 1738
    assert con.execute("SELECT COUNT(*) FROM chunk_vector_map").fetchone()[0] == EXPECTED_VECTORS
finally:
    con.close()

assert PRODUCTION_RAG_DB.name == ".rag_db"
assert not PRODUCTION_RAG_DB.is_symlink()
assert persist_dir().resolve() == PRODUCTION_RAG_DB.resolve()

# Ordinary ingest path does not pass registry_db; legacy remains blocked.
assert evaluate_compatibility(PRODUCTION_RAG_DB).state == "UNKNOWN_LEGACY"
try:
    enforce_ingest_compatibility(PRODUCTION_RAG_DB)
except FingerprintLegacyBlockedError:
    pass
else:
    raise AssertionError("expected FingerprintLegacyBlockedError for legacy .rag_db")

after = freeze()
assert after == before
print("POST_B6_READONLY_OK")
"""


def test_post_b6_production_state_readonly() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("RAG_DB_PATH", None)
    env.pop("CE_LIBRARY_ROOT", None)
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + "\n" + proc.stdout
    assert "POST_B6_READONLY_OK" in proc.stdout
