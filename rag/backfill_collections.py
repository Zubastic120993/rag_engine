"""One-shot backfill for an existing DB. Prefer a full rebuild at .rag_db after unicode fix."""

from __future__ import annotations

import argparse
from collections import Counter

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

from rag.config import EMBED_MODEL, LIBRARY_ROOT, PERSIST_DIR, collection_from_relpath


def _collection_for_source(source: str) -> str:
    src = (source or "").replace("\\", "/")
    if src.startswith("data/"):
        return collection_from_relpath(src, from_project_data=True)
    if src.startswith("/") or (len(src) > 2 and src[1] == ":"):
        try:
            rel = str(__import__("pathlib").Path(src).resolve().relative_to(LIBRARY_ROOT))
        except ValueError:
            rel = src
        return collection_from_relpath(rel, from_project_data=False)
    return collection_from_relpath(src, from_project_data=False)


def backfill(batch_size: int = 500, dry_run: bool = False) -> Counter:
    embeddings = OllamaEmbeddings(model=EMBED_MODEL)
    db = Chroma(persist_directory=str(PERSIST_DIR), embedding_function=embeddings)
    raw = db.get(include=["metadatas"])
    ids = raw.get("ids") or []
    metas = raw.get("metadatas") or []

    counts: Counter = Counter()
    pending_ids: list[str] = []
    pending_metas: list[dict] = []

    def flush() -> None:
        nonlocal pending_ids, pending_metas
        if not pending_ids:
            return
        if not dry_run:
            db._collection.update(ids=pending_ids, metadatas=pending_metas)
        pending_ids, pending_metas = [], []

    for i, (doc_id, meta) in enumerate(zip(ids, metas)):
        meta = dict(meta or {})
        source = str(meta.get("source", ""))
        collection = _collection_for_source(source)
        counts[collection] += 1
        if meta.get("collection") == collection:
            continue
        meta["collection"] = collection
        pending_ids.append(doc_id)
        pending_metas.append(meta)
        if len(pending_ids) >= batch_size:
            flush()
            print(f"  updated {i + 1}/{len(ids)}…", flush=True)

    flush()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill collection metadata on .rag_db")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Backfilling collections in {PERSIST_DIR} (dry_run={args.dry_run})")
    print("Note: prefer a full rebuild after the unicode/NFKC ingest change.")
    counts = backfill(batch_size=args.batch_size, dry_run=args.dry_run)
    total = sum(counts.values())
    print(f"Done. {total} chunks:")
    for name, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {name:12s} {n:7d}")


if __name__ == "__main__":
    main()
