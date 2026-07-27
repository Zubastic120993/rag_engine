"""F-06: should_skip_dir() did plain case-sensitive substring-of-the-whole-path
matching. That under-excluded ("/Graph" missed "graphify-out", different
case) and over-excluded at once (a component merely *containing* an entry's
text anywhere in the joined path string would match, not just a distinct
folder name). Fixed by splitting into path components and matching each one
individually: exact match for fixed directory names, substring-within-a-
component only for the two entries confirmed (by the F-06 census) to need
it. See F06_skip_dir_case_sensitivity_20260727.md for the full census."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_graphify_out_now_skipped():
    """The under-exclusion this repair fixes: "/Graph" (capital G) never
    matched "graphify-out" (lowercase, different word) under the old
    case-sensitive rule. Confirmed False before this repair; must be True
    after."""
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/00_Career/03_Engine_Knowledge/graphify-out")
    assert should_skip_dir(
        "/Users/x/CE_Library/00_Career/03_Engine_Knowledge/graphify-out/cache/semantic"
    )


def test_obsidian_graph_cache_still_skipped():
    """The entry's original target -- Obsidian's own generated graph-view
    cache folder -- must still match under the new rule."""
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/90_CE_Wiki/Graph")


def test_every_existing_entry_still_skips_what_it_skipped():
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/.rag_db")
    assert should_skip_dir("/Users/x/CE_Library/90_CE_Wiki/.obsidian/plugins")
    assert should_skip_dir("/Users/x/CE_Library/_Inbox")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/rag_engine")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/venv/lib")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/rag_env/lib")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/.git/objects")
    assert should_skip_dir("/Users/x/CE_Library/30_Knowledge/RAG_Engine_Audit")
    assert should_skip_dir("/Users/x/CE_Library/00_Career/_retired")
    assert should_skip_dir("/Users/x/CE_Library/00_Career/_retired/old_manual.pdf")
    # _Backup's broader, intentional substring-within-a-component behaviour
    # (confirmed correct by the census -- these are real pre-edit snapshot
    # dirs, not content): must still be caught.
    assert should_skip_dir(
        "/Users/x/CE_Library/90_CE_Wiki/_Backup_before_equipment_index_update_20260719_162612"
    )
    assert should_skip_dir("/Users/x/CE_Library/Tools_Backups/intake_backup_20260724_1659")


def test_case_insensitive_variants_now_caught():
    """The fix is case-insensitive, not just a targeted patch for
    "graphify-out" specifically."""
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/TOOLS/rag_engine")
    assert should_skip_dir("/Users/x/CE_Library/.RAG_DB")
    assert should_skip_dir("/Users/x/CE_Library/Some/Path/VENV/lib")
    assert should_skip_dir("/Users/x/CE_Library/00_Career/GRAPHIFY-OUT")


def test_battery_of_real_library_paths_not_skipped():
    """An over-broad skip rule silently excluding real documents is worse
    than the bug this repair fixes -- these must all stay False, both
    before and after."""
    from rag_engine.config import should_skip_dir

    real_paths = [
        "/Users/x/CE_Library/20_Vessels/Gaschem_Europe",
        "/Users/x/CE_Library/90_CE_Wiki/Equipment",
        "/Users/x/CE_Library/00_Career/03_Engine_Knowledge",
        "/Users/x/CE_Library/00_Career/07_SDS_Datasheets/01_SDS",
        "/Users/x/CE_Library/10_Company/Hartmann/SMS_IMM",
        "/Users/x/CE_Library/20_Vessels/Gaschem_Africa/10_Reference/Administration/logout_tugout",
    ]
    for p in real_paths:
        assert not should_skip_dir(p), p


def test_cocos_backup_correctly_skipped_by_design():
    """CoCoS_Backup (20_Vessels/Gaschem_Africa/04_Tasks/) matches the
    _Backup substring entry by design -- confirmed empty (0 files) by the
    F-06 census, so this is harmless in practice, but the matching
    behaviour itself is intentional, not a bug: a folder named with
    "_Backup" as part of a longer name is exactly the class of directory
    this entry exists to catch (see the timestamped-snapshot cases in
    test_every_existing_entry_still_skips_what_it_skipped)."""
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/20_Vessels/Gaschem_Africa/04_Tasks/CoCoS_Backup")


def test_over_exclusion_guard_no_partial_word_match():
    """A real folder whose name merely *contains* an entry's text (but is
    not that entry, nor one of the two deliberately-broad substring
    entries) must not be excluded -- this is the over-exclusion direction
    the census was run to check for."""
    from rag_engine.config import should_skip_dir

    assert not should_skip_dir("/Users/x/CE_Library/00_Career/Inventory_Tools_Catalogue")
    assert not should_skip_dir("/Users/x/CE_Library/20_Vessels/Gitmo_Compliance_Notes")
