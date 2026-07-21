"""Incremental PDF ingest into the shared Chroma DB with collection metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
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


def _load_tracker() -> dict:
    if TRACK_FILE.exists():
        return json.loads(TRACK_FILE.read_text(encoding="utf-8"))
    return {}


def _save_tracker(done: dict) -> None:
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    TRACK_FILE.write_text(json.dumps(done, indent=2), encoding="utf-8")


def _clean_chunks(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ".", " "],
    )
    chunks = splitter.split_documents(docs)
    valid = []
    for c in chunks:
        text = re.sub(r"[^ -~\n]", "", c.page_content).strip()
        if 50 < len(text) < 3000:
            c.page_content = text
            valid.append(c)
    return valid


def _iter_pdfs() -> list[tuple[Path, str, bool]]:
    """Return list of (abspath, source_rel, from_project_data)."""
    found: list[tuple[Path, str, bool]] = []

    if LIBRARY_ROOT.is_dir():
        for dirpath, dirnames, files in os_walk_filtered(LIBRARY_ROOT):
            for name in files:
                if name.lower().endswith(".pdf"):
                    path = Path(dirpath) / name
                    rel = str(path.relative_to(LIBRARY_ROOT)).replace("\\", "/")
                    found.append((path, rel, False))

    if DATA_ROOT.is_dir():
        for path in sorted(DATA_ROOT.glob("*.pdf")):
            # Stable source key distinct from library paths
            rel = f"data/{path.name}"
            found.append((path, rel, True))

    return found


def os_walk_filtered(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        if should_skip_dir(dirpath):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames if not should_skip_dir(str(Path(dirpath) / d))
        ]
        yield dirpath, dirnames, filenames


def ingest_file(
    db: Chroma,
    path: Path,
    source_rel: str,
    from_project_data: bool,
) -> int:
    docs = PyPDFLoader(str(path)).load()
    collection = collection_from_relpath(source_rel, from_project_data=from_project_data)
    for d in docs:
        d.metadata["source"] = source_rel
        d.metadata["page"] = d.metadata.get("page", "?")
        d.metadata["collection"] = collection
    valid = _clean_chunks(docs)
    if valid:
        # delete existing chunks for this source before re-add (safe re-ingest)
        existing = db.get(where={"source": source_rel})
        if existing and existing.get("ids"):
            db.delete(ids=existing["ids"])
        batch = 100
        for i in range(0, len(valid), batch):
            db.add_documents(valid[i : i + batch])
    return len(valid)


def run_ingest(force: bool = False) -> None:
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    done = _load_tracker()
    embeddings = OllamaEmbeddings(model=EMBED_MODEL)
    db = Chroma(persist_directory=str(PERSIST_DIR), embedding_function=embeddings)

    pdfs = _iter_pdfs()
    todo = []
    for path, rel, from_data in pdfs:
        key = str(path.resolve())
        if force or key not in done:
            todo.append((path, rel, from_data, key))

    print(f"Found {len(pdfs)} PDFs, {len(todo)} to embed.")
    for i, (path, rel, from_data, key) in enumerate(todo, 1):
        try:
            n = ingest_file(db, path, rel, from_data)
            done[key] = True
            _save_tracker(done)
            coll = collection_from_relpath(rel, from_project_data=from_data)
            print(f"[{i}/{len(todo)}] {n:4d} chunks  [{coll}]  {rel}")
        except Exception as e:
            print(f"[{i}/{len(todo)}] FAILED  {rel}: {e}")

    print(f"Done. Index at {PERSIST_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDFs into scoped local RAG DB")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest even if path is already in embedded.json",
    )
    args = parser.parse_args()
    run_ingest(force=args.force)


if __name__ == "__main__":
    main()
