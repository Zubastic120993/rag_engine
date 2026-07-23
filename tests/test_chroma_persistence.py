"""chroma_client_settings() must force is_persistent=True.

chromadb.config.Settings defaults is_persistent to False, which routes
chromadb's SqliteDB to an in-memory "file::memory:?cache=shared" connection
instead of the real persist_directory — silently, with no error, and
verifiable as correct within the same process (a same-process .get() right
after .add() finds it). It only vanishes once the process exits.

chromadb.PersistentClient() forces is_persistent=True itself regardless of
what Settings it's given, so doctor.py/diagnostics.py (which call it
directly) were never at risk. langchain_chroma.Chroma(client_settings=...)
does NOT force it — it only copies persist_directory onto whatever Settings
object it's given. This bug shipped for one real-world cycle: it caused
90_CE_Wiki/00_Source_Map.md's re-embedded chunks to never reach the durable
on-disk index, while its stale pre-edit chunks stayed there as untouched
orphans, entirely underneath a tracker that believed everything was current.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_chroma_client_settings_is_persistent():
    from rag_engine.config import chroma_client_settings

    settings = chroma_client_settings()
    assert settings.is_persistent is True


def test_chroma_client_settings_survives_cross_process(tmp_path: Path):
    """Real reproduction of the bug: add in one OS process, read in a
    genuinely separate one, using a deterministic fake embedding function so
    this doesn't depend on a live Ollama server."""
    db_path = tmp_path / "db"
    db_path.mkdir()

    fake_embeddings = dedent(
        """
        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [[float(len(t) % 7)] * 8 for t in texts]
            def embed_query(self, text):
                return [float(len(text) % 7)] * 8
        """
    )

    add_script = fake_embeddings + dedent(
        f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from langchain_chroma import Chroma
        from langchain_core.documents import Document
        from rag_engine.config import chroma_client_settings

        db = Chroma(
            persist_directory={str(db_path)!r},
            embedding_function=FakeEmbeddings(),
            client_settings=chroma_client_settings(),
        )
        ids = db.add_documents(
            [Document(page_content="persistence test content", metadata={{"source": "x.md"}})]
        )
        print(ids[0])
        """
    )
    added = subprocess.run(
        [sys.executable, "-c", add_script],
        capture_output=True, text=True, timeout=30,
    )
    assert added.returncode == 0, added.stderr
    added_id = added.stdout.strip().splitlines()[-1]

    read_script = fake_embeddings + dedent(
        f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from langchain_chroma import Chroma
        from rag_engine.config import chroma_client_settings

        db = Chroma(
            persist_directory={str(db_path)!r},
            embedding_function=FakeEmbeddings(),
            client_settings=chroma_client_settings(),
        )
        got = db.get(where={{"source": "x.md"}})
        print(",".join(got["ids"]))
        """
    )
    read = subprocess.run(
        [sys.executable, "-c", read_script],
        capture_output=True, text=True, timeout=30,
    )
    assert read.returncode == 0, read.stderr
    seen_ids = read.stdout.strip().splitlines()[-1].split(",")

    assert added_id in seen_ids, (
        "data added in one process was not visible in a separate process — "
        "chroma_client_settings() is not persistent"
    )
