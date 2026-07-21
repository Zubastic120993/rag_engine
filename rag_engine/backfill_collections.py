"""One-shot backfill for collection metadata on an existing DB."""

from __future__ import annotations

import argparse
from collections import Counter

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

from rag_engine.config import (
    collection_from_relpath,
    embed_model,
    library_root,
    persist_dir,
)


def _collection_for_source(source: str) -> str:
    src = (source or "").replace("\\", "/")
    if src.startswith("/") or (len(src) > 2 and src[1] == ":"):
        try:
            rel = str(__import__("pathlib").Path(src).resolve().relative_to(library_root()))
        except ValueError:
            rel = src
        return collection_from_relpath(rel)
    # Strip legacy data/ prefix if present in old tracker paths
    if src.startswith("data/"):
        return "me-c"
    return collection_from_relpath(src)


def backfill(batch_size: int = 500, dry_run: bool = False) -> Counter:
    embeddings = OllamaEmbeddings(model=embed_model())
    db = Chroma(persist_directory=str(persist_dir()), embedding_function=embeddings)
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
    parser = argparse.ArgumentParser(description="Backfill collection metadata")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Backfilling collections in {persist_dir()} (dry_run={args.dry_run})")
    counts = backfill(batch_size=args.batch_size, dry_run=args.dry_run)
    total = sum(counts.values())
    print(f"Done. {total} chunks:")
    for name, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {name:16s} {n:7d}")


if __name__ == "__main__":
    main()
