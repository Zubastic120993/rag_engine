"""Incremental PDF ingest: SHA-256 keyed tracker, NFKC text, new .rag_db path."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.document_loaders import PyPDFLoader
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DATA_ROOT,
    EMBED_MODEL,
    LIBRARY_ROOT,
    PERSIST_DIR,
    TRACK_FILE,
    collection_from_relpath,
    should_skip_dir,
)
from rag.text import normalize_text

# Tracker schema (content-hash keyed):
# {
#   "<sha256>": {
#     "paths": ["rel/a.pdf", "data/b.pdf"],
#     "chunk_ids": ["..."],
#     "ingested_at": "ISO8601",
#     "collection": "me-c"
#   }
# }


def _load_tracker() -> dict:
    if TRACK_FILE.exists():
        return json.loads(TRACK_FILE.read_text(encoding="utf-8"))
    return {}


def _save_tracker(tracker: dict) -> None:
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TRACK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tracker, indent=2), encoding="utf-8")
    tmp.replace(TRACK_FILE)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _path_index(tracker: dict) -> dict[str, str]:
    """Map source relpath → content hash."""
    out: dict[str, str] = {}
    for digest, meta in tracker.items():
        for p in meta.get("paths") or []:
            out[p] = digest
    return out


def _clean_chunks(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
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


def _iter_pdfs() -> list[tuple[Path, str, bool]]:
    """Return list of (abspath, source_rel, from_project_data)."""
    found: list[tuple[Path, str, bool]] = []

    if LIBRARY_ROOT.is_dir():
        for dirpath, _, files in os_walk_filtered(LIBRARY_ROOT):
            for name in files:
                if name.lower().endswith(".pdf"):
                    path = Path(dirpath) / name
                    rel = str(path.relative_to(LIBRARY_ROOT)).replace("\\", "/")
                    found.append((path, rel, False))

    if DATA_ROOT.is_dir():
        for path in sorted(DATA_ROOT.glob("*.pdf")):
            rel = f"data/{path.name}"
            found.append((path, rel, True))

    return found


def _embed_chunks(
    db: Chroma,
    path: Path,
    source_rel: str,
    from_project_data: bool,
) -> tuple[list[str], str, int]:
    """Load PDF, embed, return (chunk_ids, collection, n_chunks)."""
    docs = PyPDFLoader(str(path)).load()
    collection = collection_from_relpath(source_rel, from_project_data=from_project_data)
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
            # Older langchain may return None; fall back to query by source
            got = db.get(where={"source": source_rel})
            ids = list(got.get("ids") or [])
    return ids, collection, len(valid)


def _verify_ids(db: Chroma, ids: list[str]) -> bool:
    if not ids:
        return True
    got = db.get(ids=ids)
    return len(got.get("ids") or []) == len(ids)


def _detach_path(tracker: dict, path_to_hash: dict[str, str], source_rel: str) -> None:
    """Remove a path from whatever hash currently owns it (no chunk deletes)."""
    old = path_to_hash.get(source_rel)
    if not old or old not in tracker:
        return
    paths = [p for p in tracker[old].get("paths") or [] if p != source_rel]
    if paths:
        tracker[old]["paths"] = paths
    else:
        # Orphaned hash with no paths left — leave chunks; cleanup is a separate pass
        tracker[old]["paths"] = []
    del path_to_hash[source_rel]


def _attach_path(
    tracker: dict,
    path_to_hash: dict[str, str],
    source_rel: str,
    digest: str,
) -> str:
    """Same content hash already indexed — record another path (dedupe / rename)."""
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
    from_project_data: bool,
    digest: str,
    force: bool,
) -> str:
    """Embed file for a content hash. Order: insert → verify → delete old → tracker."""
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

    # Force re-embed of same hash: replace chunk set after verify
    if force and digest in tracker:
        sole_owner = True
        old_ids = list(tracker[digest].get("chunk_ids") or [])

    ids, collection, n = _embed_chunks(db, path, source_rel, from_project_data)
    if n and not _verify_ids(db, ids):
        raise RuntimeError(f"verify failed for {source_rel} ({len(ids)} ids)")

    if sole_owner and old_ids:
        db.delete(ids=old_ids)
        if old_digest and old_digest in tracker and old_digest != digest:
            del tracker[old_digest]

    paths = [source_rel]
    if digest in tracker and not force:
        # Should not normally reach here; keep any existing sibling paths
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
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    tracker = _load_tracker()
    if tracker and any(isinstance(v, bool) for v in tracker.values()):
        print("Legacy path-keyed tracker detected — starting fresh hash tracker.")
        tracker = {}

    path_to_hash = _path_index(tracker)
    embeddings = OllamaEmbeddings(model=EMBED_MODEL)
    db = Chroma(persist_directory=str(PERSIST_DIR), embedding_function=embeddings)

    pdfs = _iter_pdfs()
    print(f"Found {len(pdfs)} PDFs. Index: {PERSIST_DIR}")
    print(f"Chunking: size={CHUNK_SIZE} overlap={CHUNK_OVERLAP} (kept for this reindex)")
    if max_new:
        print(f"This run: stop after {max_new} NEW embeds")

    new_count = 0
    for i, (path, rel, from_data) in enumerate(pdfs, 1):
        try:
            digest = _file_sha256(path)

            if digest in tracker and not force:
                msg = _attach_path(tracker, path_to_hash, rel, digest)
                print(f"[{i}/{len(pdfs)}] {msg}")
                continue

            if not force and path_to_hash.get(rel) == digest:
                print(f"[{i}/{len(pdfs)}]    0 chunks  SKIP  {rel}")
                continue

            msg = _ingest_new_hash(
                db, tracker, path_to_hash, path, rel, from_data, digest, force
            )
            print(f"[{i}/{len(pdfs)}] {msg}")
            new_count += 1
            gc.collect()
            if max_new and new_count >= max_new:
                print(f"Reached max-new={max_new}; exiting for clean resume.")
                break
        except Exception as e:
            print(f"[{i}/{len(pdfs)}] FAILED  {rel}: {e}")
            gc.collect()

    pending = 0
    # cheap pending estimate: paths not yet in tracker by hash would need another pass
    print(f"Batch done. {len(tracker)} unique hashes → {PERSIST_DIR} (new this run: {new_count})")
    if max_new and new_count >= max_new:
        print("MORE")  # signal for reindex_loop
    else:
        print(f"Done. {len(tracker)} unique content hashes → {PERSIST_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDFs into scoped local RAG DB")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed even when content hash is already in the tracker",
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=None,
        help="Embed at most N new files then exit (crash-safe batching)",
    )
    args = parser.parse_args()
    run_ingest(force=args.force, max_new=args.max_new)


if __name__ == "__main__":
    main()
