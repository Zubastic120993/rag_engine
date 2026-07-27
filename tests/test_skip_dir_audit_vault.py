"""F-05: SKIP_DIR_PARTS listed neither 30_Knowledge nor _retired, so the
audit's own vault directory (30_Knowledge/RAG_Engine_Audit/) was protected
from ingestion only by the incidental 90_CE_Wiki/-only markdown gate — any
PDF placed there would be ingested into the corpus it describes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_should_skip_dir_now_prunes_30_knowledge():
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/30_Knowledge/RAG_Engine_Audit")
    assert should_skip_dir("/Users/x/CE_Library/30_Knowledge/RAG_Engine_Audit/README.md")


def test_should_skip_dir_prunes_retired():
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/00_Career/_retired")
    assert should_skip_dir("/Users/x/CE_Library/00_Career/_retired/old_manual.pdf")


def test_existing_skip_entry_unaffected():
    """Guard against a regression in the list or the matcher — an entry that
    already worked before this repair must still work after it."""
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/venv/lib")
    assert should_skip_dir("/Users/x/CE_Library/.git/objects")


def test_normal_library_path_still_not_skipped():
    """An over-broad skip rule silently excluding real documents is worse
    than the bug this repair fixes."""
    from rag_engine.config import should_skip_dir

    assert not should_skip_dir("/Users/x/CE_Library/20_Vessels/Gaschem_Europe")
    assert not should_skip_dir("/Users/x/CE_Library/90_CE_Wiki/Equipment")
    assert not should_skip_dir("/Users/x/CE_Library/00_Career/03_Engine_Knowledge")
