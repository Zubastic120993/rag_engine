"""Incremental doc ingest: SHA-256 keyed tracker, NFKC text, lock-protected."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag_engine.config import (
    chunk_overlap,
    chunk_size,
    collection_from_relpath,
    embed_model,
    library_root,
    persist_dir,
    should_skip_dir,
    track_file,
    wiki_extensions,
)
from rag_engine.lock import ingest_lock
from rag_engine.text import normalize_text


def _load_tracker() -> dict:
    tf = track_file()
    if tf.exists():
        return json.loads(tf.read_text(encoding="utf-8"))
    return {}


def _save_tracker(tracker: dict) -> None:
    persist_dir().mkdir(parents=True, exist_ok=True)
    tf = track_file()
    tmp = tf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tracker, indent=2), encoding="utf-8")
    tmp.replace(tf)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _path_index(tracker: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for digest, meta in tracker.items():
        for p in meta.get("paths") or []:
            out[p] = digest
    return out


def _clean_chunks(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size(),
        chunk_overlap=chunk_overlap(),
        separators=["\n\n", "\n", ".", " "],
    )
    chunks = splitter.split_documents(docs)
    valid = []
    for c in chunks:
        text = normalize_text(c.page_content)
        if 50 < len(text) < 3000:
            c.page_content = text
            valid.append(c)
    return valid


def os_walk_filtered(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        if should_skip_dir(dirpath):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames if not should_skip_dir(str(Path(dirpath) / d))
        ]
        yield dirpath, dirnames, filenames


def _iter_docs() -> list[tuple[Path, str]]:
    """Return (abspath, source_rel) under CE_LIBRARY_ROOT."""
    found: list[tuple[Path, str]] = []
    root = library_root()
    md_ext = wiki_extensions()
    if not root.is_dir():
        return found

    for dirpath, _, files in os_walk_filtered(root):
        for name in files:
            lower = name.lower()
            path = Path(dirpath) / name
            rel = str(path.relative_to(root)).replace("\\", "/")
            if lower.endswith(".pdf"):
                found.append((path, rel))
            elif any(lower.endswith(ext) for ext in md_ext) and rel.startswith(
                "90_CE_Wiki/"
            ):
                if ".backup" in lower:
                    continue
                found.append((path, rel))
    return found


def _load_documents(path: Path) -> list[Document]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return PyPDFLoader(str(path)).load()
    if suffix == ".md":
        docs = TextLoader(str(path), encoding="utf-8").load()
        for d in docs:
            d.metadata["page"] = 1
        return docs
    raise ValueError(f"unsupported file type: {path}")


def _embed_chunks(
    db: Chroma,
    path: Path,
    source_rel: str,
) -> tuple[list[str], str, int]:
    docs = _load_documents(path)
    collection = collection_from_relpath(source_rel)
    for d in docs:
        d.metadata["source"] = source_rel
        d.metadata["page"] = d.metadata.get("page", "?")
        d.metadata["collection"] = collection

    valid = _clean_chunks(docs)
    if not valid:
        return [], collection, 0

    ids: list[str] = []
    batch = 100
    for i in range(0, len(valid), batch):
        chunk = valid[i : i + batch]
        added = db.add_documents(chunk)
        if isinstance(added, list):
            ids.extend(added)
        else:
            got = db.get(where={"source": source_rel})
            ids = list(got.get("ids") or [])
    return ids, collection, len(valid)


def _verify_ids(db: Chroma, ids: list[str]) -> bool:
    if not ids:
        return True
    got = db.get(ids=ids)
    return len(got.get("ids") or []) == len(ids)


def _detach_path(tracker: dict, path_to_hash: dict[str, str], source_rel: str) -> None:
    old = path_to_hash.get(source_rel)
    if not old or old not in tracker:
        return
    paths = [p for p in tracker[old].get("paths") or [] if p != source_rel]
    tracker[old]["paths"] = paths
    del path_to_hash[source_rel]


def _attach_path(
    tracker: dict,
    path_to_hash: dict[str, str],
    source_rel: str,
    digest: str,
) -> str:
    if path_to_hash.get(source_rel) and path_to_hash[source_rel] != digest:
        _detach_path(tracker, path_to_hash, source_rel)

    meta = tracker[digest]
    paths = list(meta.get("paths") or [])
    if source_rel not in paths:
        paths.append(source_rel)
        meta["paths"] = paths
        path_to_hash[source_rel] = digest
        _save_tracker(tracker)
        return f"   0 chunks  [{meta.get('collection', '?')}]  DEDUPE/RENAME  {source_rel}"
    path_to_hash[source_rel] = digest
    return f"   0 chunks  [{meta.get('collection', '?')}]  SKIP  {source_rel}"


def _ingest_new_hash(
    db: Chroma,
    tracker: dict,
    path_to_hash: dict[str, str],
    path: Path,
    source_rel: str,
    digest: str,
    force: bool,
) -> str:
    old_digest = path_to_hash.get(source_rel)
    old_ids: list[str] = []
    sole_owner = False
    if old_digest and old_digest in tracker and old_digest != digest:
        old_meta = tracker[old_digest]
        old_paths = list(old_meta.get("paths") or [])
        if source_rel in old_paths and len(old_paths) == 1:
            sole_owner = True
            old_ids = list(old_meta.get("chunk_ids") or [])
        elif source_rel in old_paths:
            tracker[old_digest]["paths"] = [p for p in old_paths if p != source_rel]

    if force and digest in tracker:
        sole_owner = True
        old_ids = list(tracker[digest].get("chunk_ids") or [])

    ids, collection, n = _embed_chunks(db, path, source_rel)
    if n and not _verify_ids(db, ids):
        raise RuntimeError(f"verify failed for {source_rel} ({len(ids)} ids)")

    if sole_owner and old_ids:
        db.delete(ids=old_ids)
        if old_digest and old_digest in tracker and old_digest != digest:
            del tracker[old_digest]

    paths = [source_rel]
    if digest in tracker and not force:
        existing = [p for p in tracker[digest].get("paths") or [] if p != source_rel]
        paths = existing + [source_rel]

    tracker[digest] = {
        "paths": paths,
        "chunk_ids": ids,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "collection": collection,
    }
    path_to_hash[source_rel] = digest
    _save_tracker(tracker)
    return f"{n:4d} chunks  [{collection}]  NEW  {source_rel}"


def run_ingest(force: bool = False, max_new: int | None = None) -> None:
    with ingest_lock(timeout_s=0):
        _run_ingest_locked(force=force, max_new=max_new)


def _run_ingest_locked(force: bool = False, max_new: int | None = None) -> None:
    persist_dir().mkdir(parents=True, exist_ok=True)
    tracker = _load_tracker()
    if tracker and any(isinstance(v, bool) for v in tracker.values()):
        print("Legacy path-keyed tracker detected — starting fresh hash tracker.")
        tracker = {}

    path_to_hash = _path_index(tracker)
    embeddings = OllamaEmbeddings(model=embed_model())
    db = Chroma(persist_directory=str(persist_dir()), embedding_function=embeddings)

    docs = _iter_docs()
    print(f"Found {len(docs)} docs under {library_root()}. Index: {persist_dir()}")
    print(f"Chunking: size={chunk_size()} overlap={chunk_overlap()}")
    if max_new:
        print(f"This run: stop after {max_new} NEW embeds")

    new_count = 0
    for i, (path, rel) in enumerate(docs, 1):
        try:
            digest = _file_sha256(path)

            if digest in tracker and not force:
                msg = _attach_path(tracker, path_to_hash, rel, digest)
                print(f"[{i}/{len(docs)}] {msg}")
                continue

            if not force and path_to_hash.get(rel) == digest:
                print(f"[{i}/{len(docs)}]    0 chunks  SKIP  {rel}")
                continue

            msg = _ingest_new_hash(
                db, tracker, path_to_hash, path, rel, digest, force
            )
            print(f"[{i}/{len(docs)}] {msg}")
            new_count += 1
            gc.collect()
            if max_new and new_count >= max_new:
                print(f"Reached max-new={max_new}; exiting for clean resume.")
                break
        except Exception as e:
            print(f"[{i}/{len(docs)}] FAILED  {rel}: {e}")
            gc.collect()

    print(
        f"Batch done. {len(tracker)} unique hashes → {persist_dir()} "
        f"(new this run: {new_count})"
    )
    if max_new and new_count >= max_new:
        print("MORE")
    else:
        print(f"Done. {len(tracker)} unique content hashes → {persist_dir()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest library docs into Chroma")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-new", type=int, default=None)
    args = parser.parse_args()
    run_ingest(force=args.force, max_new=args.max_new)


if __name__ == "__main__":
    main()
