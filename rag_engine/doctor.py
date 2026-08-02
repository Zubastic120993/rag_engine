"""Read-only health checks for the local rag-engine installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import requests
import yaml

from rag_engine.config import (
    SCOPES_FILE,
    chroma_client_settings,
    embed_model,
    hermes_aliases,
    known_scopes,
    library_root,
    llm_model,
    load_registry,
    persist_dir,
    track_file,
)
from rag_engine.fingerprint import compare_fingerprint, fingerprint_path
from rag_engine.ingest import os_walk_filtered
from rag_engine.scope_rules import RegistryError, validate_registry
from rag_engine.text import is_valid_pdf


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _ollama_tags() -> set[str]:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    r = requests.get(f"{host}/api/tags", timeout=5)
    r.raise_for_status()
    models = r.json().get("models") or []
    names: set[str] = set()
    for m in models:
        n = m.get("name") or ""
        names.add(n)
        if ":" in n:
            names.add(n.split(":", 1)[0])
    return names


def _tracker_paths() -> list[str]:
    tf = track_file()
    if not tf.exists():
        return []
    data = json.loads(tf.read_text(encoding="utf-8"))
    paths: list[str] = []
    for meta in data.values():
        if isinstance(meta, dict):
            for p in meta.get("paths") or []:
                paths.append(str(p).replace("\\", "/"))
    return paths


def _normalize_rel_source_path(source: str, root: Path) -> str | None:
    src = str(source or "").strip().replace("\\", "/")
    if not src:
        return None
    candidate = Path(src)
    if candidate.is_absolute():
        try:
            return str(candidate.resolve().relative_to(root.resolve())).replace("\\", "/")
        except ValueError:
            return None
    return src.lstrip("./")


def _normalize_prefix(prefix: str) -> str:
    pref = str(prefix or "").strip().replace("\\", "/").lstrip("./")
    if pref and not pref.endswith("/"):
        pref += "/"
    return pref


def _scope_prefixes_indexed_check(root: Path, reg: dict[str, Any], tracker_paths: list[str]) -> dict[str, Any]:
    normalized_paths = {
        rel
        for rel in (
            _normalize_rel_source_path(path, root) for path in tracker_paths
        )
        if rel
    }
    prefix_ok = True
    details: list[str] = []
    for name, meta in (reg.get("scopes") or {}).items():
        prefixes = list(meta.get("path_prefixes") or [])
        if not prefixes:
            continue
        for pref in prefixes:
            p = root / pref
            exists = p.is_dir()
            norm_pref = _normalize_prefix(pref)
            n_docs = sum(1 for rel in normalized_paths if rel.startswith(norm_pref))
            if not exists:
                prefix_ok = False
                details.append(f"{name}:{pref} missing dir")
            elif n_docs == 0:
                prefix_ok = False
                details.append(f"{name}:{pref} dir ok but 0 indexed docs for prefix")
            else:
                details.append(f"{name}:{pref} ok ({n_docs} docs)")
    return _check(
        "scope_prefixes_indexed",
        prefix_ok,
        "; ".join(details) if details else "no prefixes",
    )


class _ProbeEmbeddings:
    """Deterministic, dependency-free embedding stub for the persistence
    probe below — no Ollama needed, and a fixed dimension so it never
    collides with the real collection's real embedding vectors (it lives in
    its own collection anyway)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1] * 8


_PROBE_COLLECTION = "rag_engine_doctor_probe"


def _check_cross_process_persistence() -> dict[str, Any]:
    """The only check in this file that would have caught the is_persistent
    regression (chroma_client_settings() silently defaulting to an in-memory
    backend): every other check here opens Chroma via chromadb.PersistentClient
    directly, which forces is_persistent=True regardless of the Settings it's
    given — so none of them ever exercised the vulnerable code path, which is
    langchain_chroma.Chroma(client_settings=...) as used by ingest.py/query.py.

    Write a probe document in a genuinely separate OS process (matching the
    real ingest.py/query.py construction exactly), then read it back from
    this process. A same-process check cannot catch this: the whole failure
    mode is data that verifies fine within the process that wrote it and
    vanishes the moment that process exits.

    Runs entirely in its own tempfile.mkdtemp() directory, never in the real
    persist_dir(). Doctor must be safe to run at any time, and a crash
    mid-probe (subprocess timeout, an exception between write and cleanup)
    must not leave debris in the live index. This also sidesteps a separate,
    already-confirmed issue: Chroma.delete_collection() does not remove the
    collection's on-disk HNSW segment directory, so cleanup here is just
    deleting the whole temp directory afterward rather than trying to
    reverse individual chromadb operations."""
    import shutil
    import tempfile

    probe_dir = Path(tempfile.mkdtemp(prefix="rag_engine_doctor_probe_"))
    probe_id = str(uuid.uuid4())
    try:
        write_script = (
            "from langchain_chroma import Chroma\n"
            "from langchain_core.documents import Document\n"
            "from rag_engine.config import chroma_client_settings\n"
            "from rag_engine.doctor import _ProbeEmbeddings, _PROBE_COLLECTION\n"
            "db = Chroma(\n"
            f"    persist_directory={str(probe_dir)!r},\n"
            "    collection_name=_PROBE_COLLECTION,\n"
            "    embedding_function=_ProbeEmbeddings(),\n"
            "    client_settings=chroma_client_settings(),\n"
            ")\n"
            "db.add_documents(\n"
            '    [Document(page_content="doctor persistence probe", metadata={"probe": True})],\n'
            f"    ids=[{probe_id!r}],\n"
            ")\n"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", write_script],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            return _check(
                "cross_process_persistence",
                False,
                f"probe write subprocess errored: {e}",
            )
        if proc.returncode != 0:
            return _check(
                "cross_process_persistence",
                False,
                f"probe write subprocess failed: {proc.stderr.strip()[:300]}",
            )

        from langchain_chroma import Chroma

        try:
            db = Chroma(
                persist_directory=str(probe_dir),
                collection_name=_PROBE_COLLECTION,
                embedding_function=_ProbeEmbeddings(),
                client_settings=chroma_client_settings(),
            )
            got = db.get(ids=[probe_id])
            found = probe_id in (got.get("ids") or [])
        except Exception as e:  # noqa: BLE001
            return _check("cross_process_persistence", False, f"probe read failed: {e}")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    if found:
        return _check(
            "cross_process_persistence",
            True,
            "probe written in a separate process was read back correctly",
        )
    return _check(
        "cross_process_persistence",
        False,
        "probe written in a separate process was NOT visible here — "
        "chroma_client_settings()/is_persistent regression",
    )


def _chroma_source_collections() -> dict[str, set[str]]:
    import chromadb

    client = chromadb.PersistentClient(
        path=str(persist_dir()), settings=chroma_client_settings()
    )
    cols = client.list_collections()
    if not cols:
        return {}
    col = client.get_collection(cols[0].name)
    raw = col.get(include=["metadatas"])
    by: dict[str, set[str]] = {}
    for m in raw.get("metadatas") or []:
        if not m:
            continue
        c = str(m.get("collection") or "other")
        s = str(m.get("source") or "").replace("\\", "/")
        by.setdefault(c, set()).add(s)
    return by


def run_doctor(*, skip_ollama: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    root = library_root()
    db = persist_dir()

    checks.append(
        _check("library_root_exists", root.is_dir(), str(root))
    )
    checks.append(_check("db_path_exists", db.is_dir(), str(db)))

    try:
        load_registry()
        checks.append(_check("scopes_yaml_loads", True, str(SCOPES_FILE)))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("scopes_yaml_loads", False, str(e)))

    try:
        validate_registry()
        checks.append(_check("alias_registry_valid", True, "no duplicate/collision aliases"))
    except RegistryError as e:
        checks.append(_check("alias_registry_valid", False, str(e)))

    # prefix dirs exist + have indexed docs
    try:
        reg = load_registry()
        tracker_paths = _tracker_paths()
        checks.append(_scope_prefixes_indexed_check(root, reg, tracker_paths))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("scope_prefixes_indexed", False, str(e)))

    if skip_ollama:
        checks.append(_check("ollama_reachable", True, "skipped"))
        checks.append(_check("embed_model_available", True, "skipped"))
        checks.append(_check("chat_model_available", True, "skipped"))
    else:
        try:
            tags = _ollama_tags()
            checks.append(_check("ollama_reachable", True, f"{len(tags)} models"))
            em = embed_model()
            ok_e = em in tags or em.split(":")[0] in tags
            checks.append(_check("embed_model_available", ok_e, em))
            lm = llm_model()
            ok_l = lm in tags or lm.split(":")[0] in tags
            checks.append(_check("chat_model_available", ok_l, lm))
        except Exception as e:  # noqa: BLE001
            checks.append(_check("ollama_reachable", False, str(e)))
            checks.append(_check("embed_model_available", False, "ollama unreachable"))
            checks.append(_check("chat_model_available", False, "ollama unreachable"))

    fp = compare_fingerprint()
    # Missing fingerprint is FAIL (cannot detect embed drift)
    checks.append(
        _check(
            "index_fingerprint",
            fp["match"],
            fp["message"] + (f" path={fingerprint_path()}" if not fp["match"] else ""),
        )
    )

    try:
        import chromadb

        client = chromadb.PersistentClient(
            path=str(db), settings=chroma_client_settings()
        )
        cols = client.list_collections()
        checks.append(
            _check("chroma_open", bool(cols), f"collections={[c.name for c in cols]}")
        )
    except Exception as e:  # noqa: BLE001
        checks.append(_check("chroma_open", False, str(e)))

    checks.append(_check_cross_process_persistence())

    tf = track_file()
    try:
        if tf.exists():
            json.loads(tf.read_text(encoding="utf-8"))
            checks.append(_check("tracker_readable", True, str(tf)))
        else:
            checks.append(_check("tracker_readable", False, f"missing {tf}"))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("tracker_readable", False, str(e)))

    paths = []
    try:
        paths = _tracker_paths()
    except Exception:  # noqa: BLE001
        paths = []

    hermes_hits = [p for p in paths if "Hermes_Library" in p]
    checks.append(
        _check(
            "no_hermes_library_paths",
            len(hermes_hits) == 0,
            f"count={len(hermes_hits)}",
        )
    )
    legacy_db = [p for p in paths if "99_Rules/.rag_db" in p or "99_Rules\\.rag_db" in p]
    checks.append(
        _check(
            "no_legacy_rules_rag_db_paths",
            len(legacy_db) == 0,
            f"count={len(legacy_db)}",
        )
    )

    sample_ok = False
    sample_detail = "no tracker paths"
    for p in paths[:50]:
        cand = root / p if not Path(p).is_absolute() else Path(p)
        if cand.is_file():
            sample_ok = True
            sample_detail = str(cand)
            break
    checks.append(_check("sample_source_exists", sample_ok, sample_detail))

    orphan = 0
    for p in paths:
        cand = root / p if not os.path.isabs(p) else Path(p)
        if not cand.is_file():
            orphan += 1
    checks.append(
        _check(
            "orphan_sources",
            orphan == 0,
            f"missing_files={orphan} of {len(paths)} tracker paths",
        )
    )

    # coverage gap: PDFs/md under library not in tracker (sampled count).
    # invalid_pdf: files with a .pdf extension whose bytes are not a real PDF.
    # Walks via os_walk_filtered()/should_skip_dir() — the exact same predicate
    # ingest.py uses to decide what's a candidate — so this never flags a
    # directory ingest itself would never have considered (see test_hardening's
    # test_coverage_gap_walk_matches_ingest_walk for the guard against drift).
    tracked = set(paths)
    unindexed = 0
    scanned = 0
    invalid_pdfs: list[str] = []
    try:
        for dirpath, dirnames, filenames in os_walk_filtered(root):
            for fn in filenames:
                low = fn.lower()
                if not low.endswith((".pdf", ".md")):
                    continue
                scanned += 1
                full = Path(dirpath) / fn
                rel = str(full.relative_to(root)).replace("\\", "/")
                if rel not in tracked:
                    unindexed += 1
                if low.endswith(".pdf") and not is_valid_pdf(full):
                    invalid_pdfs.append(rel)
        checks.append(
            _check(
                "coverage_gap",
                True,
                f"unindexed_files={unindexed} scanned={scanned}",
            )
        )
        # Informational: a fake PDF is never indexed, but flag it so the
        # corpus can be cleaned. ok=True keeps doctor PASS on its presence.
        invalid_detail = (
            f"invalid_pdf_files={len(invalid_pdfs)}"
            + (f" e.g. {', '.join(invalid_pdfs[:3])}" if invalid_pdfs else "")
        )
        checks.append(_check("invalid_pdf", True, invalid_detail))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("coverage_gap", False, str(e)))
        checks.append(_check("invalid_pdf", False, str(e)))

    # git tracked corpus?
    try:
        repo = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files"],
            capture_output=True,
            text=True,
            check=False,
        )
        tracked_git = proc.stdout.splitlines()
        bad = [
            f
            for f in tracked_git
            if f.lower().endswith(".pdf")
            or ".rag_db" in f
            or f.endswith("embedded.json")
            or f.endswith("chroma.sqlite3")
        ]
        checks.append(
            _check("git_no_corpus", len(bad) == 0, f"bad_files={bad[:5]}")
        )
    except Exception as e:  # noqa: BLE001
        checks.append(_check("git_no_corpus", False, str(e)))

    failed = [c for c in checks if not c["ok"]]
    # coverage_gap, invalid_pdf are informational (ok=True always); orphan_sources
    # fails when tracker paths point at files no longer on disk (stale metadata)
    status = "PASS" if not failed else "FAIL"
    return {
        "status": status,
        "checks": checks,
        "fingerprint": fp,
        "library_root": str(root),
        "db_path": str(db),
        "scopes": list(known_scopes()),
        "aliases": hermes_aliases(),
    }
