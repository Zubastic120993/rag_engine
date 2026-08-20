"""Phase 0/1 library state resolver tests - isolated fixtures only.

Never mutates production filesystem, registry, tracker, Chroma, locks, or journals.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from rag_engine.library_state import (
    CHROMA_ID_BATCH_SIZE,
    lookup_chroma_by_embedding_ids,
    resolve_library_state,
)
from rag_engine.library_state.contract import (
    ABSENCE_CLASS_BY_INTENT,
    APPROVAL_BLOCKED,
    APPROVAL_CATEGORIES,
    APPROVAL_REQUIRED,
    APPROVAL_SEPARATE_INDEX_APPROVAL,
    CLASS_ALIAS_ONLY,
    CLASS_AMBIGUOUS,
    CLASS_DIFFERENT_REVISION,
    CLASS_EXACT_DUPLICATE,
    CLASS_INDEXED_OK,
    CLASS_NEW_DOCUMENT,
    CLASS_NOT_INDEXED,
    CLASS_SAME_BYTES_MOVED,
    CLASS_STALE_METADATA,
    CLASSIFICATIONS,
    EMBEDDING_ACTIONS,
    EMBEDDING_NONE,
    EMBEDDING_PENDING_SEPARATE_APPROVAL,
    GAP_JOURNAL_NO_EXACT_LOCATOR,
    GAP_REGISTRY_VECTOR_MAP_ABSENT,
    INTENT_CHECK,
    INTENT_PLAN_ADD,
    INTENT_PLAN_DELETE,
    INTENT_PLAN_MOVE,
    INTENT_PLAN_RECONCILE,
    INTENT_PLAN_RENAME,
    INTENT_VERIFY,
    OP_ALIAS_REGISTER,
    OP_CERTIFIED_APPEND_PROPOSAL,
    OP_MANUAL_REVIEW,
    OP_METADATA_ONLY_RECONCILE,
    OP_NO_OP,
    OP_RETIREMENT_PROPOSAL,
    PROPOSED_OPERATIONS,
    RESULT_AMBIGUOUS,
    RESULT_BLOCKED,
    RESULT_PARTIAL,
    RESULT_VERIFIED,
    RESULT_VOCABULARY,
    SUPPORTED_INTENTS,
)
from rag_engine.library_state.evidence import empty_chroma_lookup
from rag_engine.library_state.plan import make_request_id
from rag_engine.metadata_registry import (
    initialize_registry,
    open_registry,
    register_chunk,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
)
from rag_engine.stable_identity import (
    chunk_id,
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)

OLD = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_old_name.pdf"
NEW = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_new_name.pdf"
ALIAS = "00_Career/03_Engine_Knowledge/MAN_Academy/copy/MAN_ME-C_LGIP_new_name.pdf"
REV2 = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_course_variant.pdf"
OUTSIDE = "00_Career/03_Engine_Knowledge/Other_Maker/should_not_be_inspected.pdf"
FOLDER = "00_Career/03_Engine_Knowledge/MAN_Academy"

BYTES_A = b"%PDF-1.4\nMAN Academy LGIP A\n"
BYTES_B = b"%PDF-1.4\nMAN Academy LGIP B variant\n"
HASH_A = source_hash_from_bytes(BYTES_A)
HASH_B = source_hash_from_bytes(BYTES_B)
DOC_A = document_id_from_bytes(BYTES_A)
FP = "ab" * 32
ID1 = chunk_id(DOC_A, FP, 0)
ID2 = chunk_id(DOC_A, FP, 1)
ID3 = chunk_id(DOC_A, FP, 2)
CHUNK_IDS = (ID1, ID2)
SUBJECT = subject_id_from_key("maker_doc", "man-academy-lgip")


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_chroma_sqlite(path: Path, records: list[dict], *, collection_name: str = "langchain") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                type TEXT,
                scope TEXT,
                collection TEXT REFERENCES collections(id)
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                UNIQUE (segment_id, embedding_id)
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id),
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            """
        )
        coll_id = str(uuid.uuid4())
        seg_id = str(uuid.uuid4())
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (coll_id, collection_name))
        conn.execute(
            "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
            (seg_id, "vector", "VECTOR", coll_id),
        )
        for i, rec in enumerate(records):
            eid = rec["id"]
            conn.execute(
                "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) VALUES (?, ?, ?, ?)",
                (i + 1, seg_id, eid, b"\x00"),
            )
            if rec.get("source") is not None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i + 1, "source", rec["source"]),
                )
            if rec.get("collection") is not None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i + 1, "collection", rec["collection"]),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _seed_registry(
    db: Path,
    *,
    aliases: list[str],
    chunk_ids: list[str] | None = None,
    vector_ids: list[str] | None = None,
    source_hash: str = HASH_A,
    document_id: str = DOC_A,
) -> None:
    initialize_registry(db)
    conn = open_registry(db)
    try:
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=document_id, subject_id=SUBJECT, source_hash=source_hash
            )
            for alias in aliases:
                register_source_file(
                    conn,
                    document_id=document_id,
                    relative_path=alias,
                    source_hash=source_hash,
                    collection="maker-manuals",
                )
            for cid in chunk_ids or []:
                ordinal = {ID1: 0, ID2: 1, ID3: 2}[cid]
                register_chunk(
                    conn,
                    chunk_id=cid,
                    document_id=document_id,
                    chunking_fingerprint=FP,
                    ordinal=ordinal,
                )
            for cid, vid in zip(chunk_ids or [], vector_ids or []):
                register_vector_mapping(
                    conn,
                    chunk_id=cid,
                    chroma_embedding_id=vid,
                    mapping_status="native_chunk_id",
                )
    finally:
        conn.close()


def _write_tracker(path: Path, *, paths: list[str], chunk_ids: list[str], digest: str = HASH_A) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        digest: {
            "paths": paths,
            "chunk_ids": chunk_ids,
            "collection": "maker-manuals",
            "document_id": DOC_A,
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(
    *,
    library: Path,
    persist: Path,
    registry: Path,
    extra_files: list[Path],
    extra_dirs: list[Path],
) -> dict:
    files: dict[str, str] = {}
    dirs: dict[str, list[str] | None] = {}
    existence: dict[str, bool] = {}

    def add_file(path: Path) -> None:
        existence[str(path)] = path.exists()
        if path.is_file():
            files[str(path)] = _file_sha(path)

    def add_dir(path: Path) -> None:
        if path.is_dir():
            dirs[str(path)] = sorted(p.name for p in path.iterdir())
        else:
            dirs[str(path)] = None

    for rel in (OLD, NEW, ALIAS, REV2, OUTSIDE):
        add_file(library / rel)
    add_dir(library / FOLDER)
    add_dir(library / "00_Career/03_Engine_Knowledge")
    add_file(registry)
    add_dir(registry.parent)
    add_file(persist / "embedded.json")
    add_file(persist / "chroma.sqlite3")
    add_file(persist / "ingest.lock")
    add_file(persist / "certified_append.lock")
    add_dir(persist)
    journal_dir = persist / "certified_append_journal"
    add_dir(journal_dir)
    if persist.is_dir():
        for p in sorted(persist.rglob("*")):
            if p.is_file():
                add_file(p)
            elif p.is_dir():
                add_dir(p)
    for p in extra_files:
        add_file(p)
    for d in extra_dirs:
        add_dir(d)
    return {"files": files, "dirs": dirs, "existence": existence}


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    registry = tmp_path / "reg" / "metadata_registry_v1.sqlite3"
    library.mkdir()
    persist.mkdir()
    registry.parent.mkdir()
    return {
        "library": library,
        "persist": persist,
        "registry": registry,
        "tracker": persist / "embedded.json",
        "chroma": persist / "chroma.sqlite3",
    }


def _resolve(env: dict, intent: str, targets: list[str], **kwargs):
    extra_files = kwargs.pop("extra_files", [])
    extra_dirs = kwargs.pop("extra_dirs", [])
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
        extra_files=extra_files,
        extra_dirs=extra_dirs,
    )
    plan = resolve_library_state(
        intent,
        targets,
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"] if env["registry"].exists() else kwargs.pop("registry_db", env["registry"]),
        tracker_path=env["tracker"] if env["tracker"].exists() else kwargs.pop("tracker_path", None),
        **kwargs,
    )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
        extra_files=extra_files,
        extra_dirs=extra_dirs,
    )
    assert after == before, "resolver mutated isolated fixture stores"
    return plan


def _seed_moved_or_indexed(
    env: dict,
    *,
    live_rel: str,
    governed_paths: list[str],
    chroma_source: str,
    extra_live: dict[str, bytes] | None = None,
    vector_ids: list[str] | None = None,
    chunk_ids: list[str] | None = None,
    chroma_ids: list[str] | None = None,
) -> None:
    _write(env["library"], live_rel, BYTES_A)
    for rel, data in (extra_live or {}).items():
        _write(env["library"], rel, data)
    ids = list(chunk_ids or CHUNK_IDS)
    vids = list(vector_ids if vector_ids is not None else ids)
    _seed_registry(
        env["registry"],
        aliases=governed_paths,
        chunk_ids=ids if vids else None,
        vector_ids=vids if vids else None,
    )
    _write_tracker(env["tracker"], paths=governed_paths, chunk_ids=ids)
    chroma_eids = list(chroma_ids or ids)
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": eid, "source": chroma_source, "collection": "maker-manuals"} for eid in chroma_eids],
    )


# ---------------------------------------------------------------------------
# Phase 0 contract
# ---------------------------------------------------------------------------


def test_contract_closed_sets() -> None:
    assert SUPPORTED_INTENTS == {
        "check",
        "plan_move",
        "plan_rename",
        "plan_delete",
        "plan_add",
        "plan_reconcile",
        "verify",
    }
    assert APPROVAL_CATEGORIES == {
        "NOT_REQUIRED",
        "REQUIRED",
        "SEPARATE_INDEX_APPROVAL",
        "BLOCKED",
    }
    assert RESULT_VOCABULARY == {"VERIFIED", "PARTIAL", "FAILED", "BLOCKED", "AMBIGUOUS"}
    assert CLASS_SAME_BYTES_MOVED in CLASSIFICATIONS
    assert OP_METADATA_ONLY_RECONCILE in PROPOSED_OPERATIONS
    assert EMBEDDING_NONE in EMBEDDING_ACTIONS
    assert ABSENCE_CLASS_BY_INTENT[INTENT_PLAN_ADD] == CLASS_NEW_DOCUMENT
    assert ABSENCE_CLASS_BY_INTENT[INTENT_CHECK] == CLASS_NOT_INDEXED
    assert ABSENCE_CLASS_BY_INTENT[INTENT_PLAN_MOVE] == CLASS_AMBIGUOUS


def test_library_state_does_not_use_subject_id_pending() -> None:
    import rag_engine.library_state as pkg
    import rag_engine.library_state.contract as contract
    import rag_engine.library_state.evidence as evidence
    import rag_engine.library_state.plan as plan
    import rag_engine.library_state.resolver as resolver

    for mod in (pkg, contract, evidence, plan, resolver):
        src = inspect.getsource(mod)
        assert "subject_id_pending" not in src


# ---------------------------------------------------------------------------
# Classification fixtures
# ---------------------------------------------------------------------------


def test_same_byte_moved_file_proposes_metadata_only_reconcile(env) -> None:
    _seed_moved_or_indexed(
        env, live_rel=NEW, governed_paths=[OLD], chroma_source=OLD
    )
    plan = _resolve(env, INTENT_PLAN_MOVE, [NEW])
    assert plan.classification == CLASS_SAME_BYTES_MOVED
    assert plan.proposed_operation == OP_METADATA_ONLY_RECONCILE
    assert plan.embedding_action == EMBEDDING_NONE
    assert plan.classifications[0].expected_new_vectors == 0
    assert plan.classification != CLASS_NEW_DOCUMENT
    chroma = plan.authority_snapshot["chroma"]
    assert chroma["found"] == 2
    assert chroma["requested"] == 2


def test_same_byte_additional_path_is_alias_only(env) -> None:
    _seed_moved_or_indexed(
        env,
        live_rel=OLD,
        governed_paths=[OLD],
        chroma_source=OLD,
        extra_live={ALIAS: BYTES_A},
    )
    plan = _resolve(env, INTENT_CHECK, [ALIAS])
    assert plan.classification == CLASS_ALIAS_ONLY
    assert plan.proposed_operation == OP_ALIAS_REGISTER
    assert plan.embedding_action == EMBEDDING_NONE
    assert plan.classifications[0].expected_new_vectors == 0


def test_exact_duplicate_with_retained_alias_requires_approval(env) -> None:
    _seed_moved_or_indexed(
        env,
        live_rel=OLD,
        governed_paths=[OLD],
        chroma_source=OLD,
        extra_live={ALIAS: BYTES_A},
    )
    plan = _resolve(env, INTENT_PLAN_DELETE, [ALIAS])
    assert plan.classification == CLASS_EXACT_DUPLICATE
    assert plan.proposed_operation == OP_RETIREMENT_PROPOSAL
    assert plan.approval == APPROVAL_REQUIRED
    assert plan.embedding_action == EMBEDDING_NONE


def _seed_governed_duplicate_delete_fixture(env: dict) -> None:
    """Target and retained are governed aliases; lower stores retain the kept copy."""
    _write(env["library"], OLD, BYTES_A)
    _write(env["library"], ALIAS, BYTES_A)
    _seed_registry(env["registry"], aliases=[OLD, ALIAS])
    _write_tracker(env["tracker"], paths=[OLD], chunk_ids=list(CHUNK_IDS))
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": eid, "source": OLD, "collection": "maker-manuals"} for eid in CHUNK_IDS],
    )


def test_plan_delete_governed_target_with_live_retained_is_exact_duplicate_not_stale(
    env,
) -> None:
    _seed_governed_duplicate_delete_fixture(env)
    plan = _resolve(env, INTENT_PLAN_DELETE, [ALIAS])
    item = plan.classifications[0]
    assert plan.classification == CLASS_EXACT_DUPLICATE
    assert plan.classification != CLASS_STALE_METADATA
    assert plan.proposed_operation == OP_RETIREMENT_PROPOSAL
    assert plan.approval == APPROVAL_REQUIRED
    assert plan.embedding_action == EMBEDDING_NONE
    assert plan.result == RESULT_PARTIAL
    assert OLD in (item.evidence.get("retained_aliases") or [])


def test_check_governed_duplicate_fixture_remains_stale_metadata(env) -> None:
    _seed_governed_duplicate_delete_fixture(env)
    plan = _resolve(env, INTENT_CHECK, [ALIAS])
    assert plan.classification == CLASS_STALE_METADATA
    assert plan.proposed_operation == OP_METADATA_ONLY_RECONCILE


def test_plan_reconcile_governed_duplicate_fixture_remains_stale_metadata(env) -> None:
    _seed_governed_duplicate_delete_fixture(env)
    plan = _resolve(env, INTENT_PLAN_RECONCILE, [ALIAS])
    assert plan.classification == CLASS_STALE_METADATA
    assert plan.proposed_operation == OP_METADATA_ONLY_RECONCILE


def test_plan_delete_without_live_retained_alias_is_blocked_not_exact_duplicate(env) -> None:
    _seed_moved_or_indexed(env, live_rel=ALIAS, governed_paths=[ALIAS], chroma_source=ALIAS)
    plan = _resolve(env, INTENT_PLAN_DELETE, [ALIAS])
    assert plan.classification != CLASS_EXACT_DUPLICATE
    assert plan.classification in {CLASS_AMBIGUOUS, CLASS_INDEXED_OK}
    assert plan.proposed_operation != OP_RETIREMENT_PROPOSAL


def test_plan_delete_id_mismatch_does_not_become_exact_duplicate(env) -> None:
    _write(env["library"], OLD, BYTES_A)
    _write(env["library"], ALIAS, BYTES_A)
    _seed_registry(env["registry"], aliases=[OLD, ALIAS])
    _write_tracker(env["tracker"], paths=[OLD, ALIAS], chunk_ids=list(CHUNK_IDS))
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": ID1, "source": OLD, "collection": "maker-manuals"}],
    )
    plan = _resolve(env, INTENT_PLAN_DELETE, [ALIAS])
    assert plan.classification == CLASS_AMBIGUOUS
    assert plan.proposed_operation == OP_MANUAL_REVIEW
    assert plan.classification != CLASS_EXACT_DUPLICATE


def test_plan_delete_compat_conflict_does_not_become_exact_duplicate(env) -> None:
    _seed_governed_duplicate_delete_fixture(env)
    from rag_engine.index_compatibility.builders import stored_envelope_from_specs
    from rag_engine.index_compatibility.state import write_registry_fingerprint, write_sidecar_v1

    try:
        emb_a, corp, idx_a = _make_compat_specs("model-conflict-a", env["library"])
        emb_b, _, idx_b = _make_compat_specs("model-conflict-b", env["library"])
    except Exception:
        pytest.skip("fingerprint spec builders unavailable in this environment")

    write_sidecar_v1(env["persist"], stored_envelope_from_specs(emb_a, corp, idx_a))
    write_registry_fingerprint(
        env["registry"], stored_envelope_from_specs(emb_b, corp, idx_b)
    )

    plan = _resolve(env, INTENT_PLAN_DELETE, [ALIAS])
    assert plan.classification == CLASS_AMBIGUOUS
    assert plan.proposed_operation == OP_MANUAL_REVIEW
    assert plan.classification != CLASS_EXACT_DUPLICATE


def test_different_byte_similar_document_is_not_exact_duplicate(env) -> None:
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW, REV2], chroma_source=NEW)
    _write(env["library"], REV2, BYTES_B)
    plan = _resolve(env, INTENT_CHECK, [REV2])
    assert plan.classification == CLASS_DIFFERENT_REVISION
    assert plan.classification != CLASS_EXACT_DUPLICATE
    assert plan.embedding_action == EMBEDDING_PENDING_SEPARATE_APPROVAL


def test_plan_add_without_governed_identity_is_new_document(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    plan = _resolve(env, INTENT_PLAN_ADD, [NEW])
    assert plan.classification == CLASS_NEW_DOCUMENT
    assert plan.proposed_operation == OP_CERTIFIED_APPEND_PROPOSAL
    assert plan.approval == APPROVAL_SEPARATE_INDEX_APPROVAL


def test_check_verify_plan_reconcile_without_mapping_are_not_indexed(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    for intent in (INTENT_CHECK, INTENT_VERIFY, INTENT_PLAN_RECONCILE):
        plan = _resolve(env, intent, [NEW])
        assert plan.classification == CLASS_NOT_INDEXED, intent
        assert plan.proposed_operation == OP_NO_OP
        assert plan.classification != CLASS_NEW_DOCUMENT


def test_plan_move_rename_delete_without_identity_are_ambiguous(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    for intent in (INTENT_PLAN_MOVE, INTENT_PLAN_RENAME, INTENT_PLAN_DELETE):
        plan = _resolve(env, intent, [NEW])
        assert plan.classification == CLASS_AMBIGUOUS, intent
        assert plan.proposed_operation == OP_MANUAL_REVIEW
        assert plan.classification not in {CLASS_NEW_DOCUMENT, CLASS_NOT_INDEXED}


def test_indexed_ok_verified_requires_registry_vector_map_agreement(env) -> None:
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)
    plan = _resolve(env, INTENT_VERIFY, [NEW])
    assert plan.classification == CLASS_INDEXED_OK
    assert plan.proposed_operation == OP_NO_OP
    assert plan.result == RESULT_VERIFIED
    assert plan.classifications[0].subject_id == SUBJECT
    comps = plan.classifications[0].evidence["id_comparisons"]
    for key in ("tracker_vs_registry", "tracker_vs_chroma", "registry_vs_chroma"):
        assert comps[key]["counts"]["missing"] == 0
        assert comps[key]["counts"]["unexpected"] == 0


def test_tracker_chroma_coherent_without_vector_map_is_partial(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    _seed_registry(env["registry"], aliases=[NEW], chunk_ids=None, vector_ids=None)
    _write_tracker(env["tracker"], paths=[NEW], chunk_ids=list(CHUNK_IDS))
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": eid, "source": NEW, "collection": "maker-manuals"} for eid in CHUNK_IDS],
    )
    plan = _resolve(env, INTENT_CHECK, [NEW])
    assert plan.classification == CLASS_INDEXED_OK
    assert plan.result == RESULT_PARTIAL
    assert plan.result != RESULT_VERIFIED
    assert GAP_REGISTRY_VECTOR_MAP_ABSENT in plan.evidence_gaps


def test_chroma_missing_id_is_ambiguous(env) -> None:
    _seed_moved_or_indexed(
        env,
        live_rel=NEW,
        governed_paths=[NEW],
        chroma_source=NEW,
        chroma_ids=[ID1],
    )
    plan = _resolve(env, INTENT_CHECK, [NEW])
    assert plan.classification == CLASS_AMBIGUOUS
    assert plan.proposed_operation == OP_MANUAL_REVIEW
    assert plan.classification != CLASS_INDEXED_OK
    comps = plan.classifications[0].evidence["id_comparisons"]["tracker_vs_chroma"]
    assert comps["counts"]["missing"] == 1
    assert comps["counts"]["unexpected"] == 0


def test_union_lookup_reports_pairwise_disagreement(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    _seed_registry(
        env["registry"],
        aliases=[NEW],
        chunk_ids=[ID1, ID3],
        vector_ids=[ID1, ID3],
    )
    _write_tracker(env["tracker"], paths=[NEW], chunk_ids=[ID1, ID2])
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": eid, "source": NEW, "collection": "maker-manuals"} for eid in (ID1, ID2, ID3)],
    )
    plan = _resolve(env, INTENT_CHECK, [NEW])
    assert plan.classification == CLASS_AMBIGUOUS
    assert plan.proposed_operation == OP_MANUAL_REVIEW
    assert plan.classification != CLASS_INDEXED_OK
    chroma = plan.authority_snapshot["chroma"]
    assert chroma["requested"] == 3
    assert chroma["found"] == 3
    assert chroma["missing"] == 0
    comps = plan.classifications[0].evidence["id_comparisons"]
    assert ID2 in comps["tracker_vs_registry"]["missing_in_registry"]
    assert ID3 in comps["tracker_vs_registry"]["unexpected_in_registry"]
    assert ID3 in comps["tracker_vs_chroma"]["unexpected_in_chroma"]
    assert ID2 in comps["registry_vs_chroma"]["unexpected_in_chroma"]


def test_registry_vector_map_disagrees_is_ambiguous(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    _seed_registry(
        env["registry"],
        aliases=[NEW],
        chunk_ids=[ID1, ID3],
        vector_ids=[ID1, ID3],
    )
    _write_tracker(env["tracker"], paths=[NEW], chunk_ids=[ID1, ID2])
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": eid, "source": NEW, "collection": "maker-manuals"} for eid in (ID1, ID2)],
    )
    plan = _resolve(env, INTENT_CHECK, [NEW])
    assert plan.classification == CLASS_AMBIGUOUS
    assert plan.proposed_operation == OP_MANUAL_REVIEW


def test_optional_stores_absent_do_not_write_or_scan(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    plan = _resolve(env, INTENT_CHECK, [NEW])
    assert plan.classification == CLASS_NOT_INDEXED
    assert GAP_JOURNAL_NO_EXACT_LOCATOR in plan.evidence_gaps
    assert "registry_absent" in plan.evidence_gaps
    assert "tracker_absent" in plan.evidence_gaps
    assert "chroma_absent" in plan.evidence_gaps
    assert not (env["persist"] / "ingest.lock").exists()
    assert not (env["persist"] / "certified_append.lock").exists()
    assert not (env["persist"] / "certified_append_journal").exists()
    assert not env["registry"].exists()


def test_no_write_with_planted_locks_and_journal(env) -> None:
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)
    (env["persist"] / "ingest.lock").write_text("pid\n", encoding="utf-8")
    (env["persist"] / "certified_append.lock").write_text("pid\n", encoding="utf-8")
    journal_dir = env["persist"] / "certified_append_journal"
    journal_dir.mkdir()
    planted = journal_dir / "capp:fixture.json"
    planted.write_text("{}", encoding="utf-8")
    extra = env["persist"] / "sidecar.dat"
    extra.write_bytes(b"keep")
    plan = _resolve(
        env,
        INTENT_VERIFY,
        [NEW],
        extra_files=[planted, extra, env["persist"] / "ingest.lock"],
        extra_dirs=[journal_dir],
    )
    assert plan.classification == CLASS_INDEXED_OK
    assert planted.read_text(encoding="utf-8") == "{}"
    assert extra.read_bytes() == b"keep"


def test_determinism_identical_fixtures(env) -> None:
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[OLD], chroma_source=OLD)
    first = _resolve(env, INTENT_PLAN_RECONCILE, [NEW])
    second = _resolve(env, INTENT_PLAN_RECONCILE, [NEW])
    assert first.to_canonical_json() == second.to_canonical_json()
    assert first.request_id == make_request_id(INTENT_PLAN_RECONCILE, [NEW])


def test_bounded_folder_inspection_does_not_hash_outside(env) -> None:
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[OLD], chroma_source=OLD)
    _write(env["library"], OUTSIDE, b"outside-bytes-not-in-scope")
    plan = _resolve(env, INTENT_PLAN_MOVE, [FOLDER])
    hashed = plan.authority_snapshot["hashed_paths"]
    inspected = plan.authority_snapshot["inspected_paths"]
    assert OUTSIDE not in hashed
    assert OUTSIDE not in inspected
    assert OLD in inspected
    assert NEW in hashed
    assert plan.classification == CLASS_SAME_BYTES_MOVED


def test_journal_exact_locator_only(env) -> None:
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)
    journal_dir = env["persist"] / "certified_append_journal"
    journal_dir.mkdir()
    known = journal_dir / "known_id.json"
    other = journal_dir / "other_id.json"
    known.write_text(json.dumps({"operation_id": "known_id", "state": "SUCCESS"}), encoding="utf-8")
    other.write_text(json.dumps({"operation_id": "other_id"}), encoding="utf-8")
    plan_a = _resolve(env, INTENT_CHECK, [NEW], extra_dirs=[journal_dir], extra_files=[known, other])
    assert GAP_JOURNAL_NO_EXACT_LOCATOR in plan_a.evidence_gaps
    assert plan_a.authority_snapshot["journal"] is None
    plan_b = _resolve(
        env,
        INTENT_CHECK,
        [NEW],
        operation_id="known_id",
        extra_dirs=[journal_dir],
        extra_files=[known, other],
    )
    assert plan_b.authority_snapshot["journal"]["operation_id"] == "known_id"
    assert GAP_JOURNAL_NO_EXACT_LOCATOR not in plan_b.evidence_gaps


# ---------------------------------------------------------------------------
# Chroma ID lookup
# ---------------------------------------------------------------------------


def test_chroma_id_lookup_empty_ids_does_not_query(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sqlite3"
    result = lookup_chroma_by_embedding_ids(missing, [])
    assert result == empty_chroma_lookup()
    assert not missing.exists()


def test_chroma_id_lookup_batches_beyond_sqlite_in_limit(tmp_path: Path) -> None:
    n = CHROMA_ID_BATCH_SIZE * 2 + 50
    ids = [f"chunk:{i:032x}" for i in range(n)]
    db = _make_chroma_sqlite(
        tmp_path / "chroma.sqlite3",
        [{"id": eid, "source": NEW} for eid in ids],
    )
    result = lookup_chroma_by_embedding_ids(db, list(reversed(ids)))
    assert result.requested == n
    assert result.found == n
    assert result.missing == 0
    assert result.found_ids == tuple(sorted(ids))
    assert [r.chroma_embedding_id for r in result.records] == list(result.found_ids)


def test_chroma_id_lookup_reports_missing_without_truncation(tmp_path: Path) -> None:
    n = CHROMA_ID_BATCH_SIZE * 2 + 50
    ids = [f"chunk:{i:032x}" for i in range(n)]
    hole = ids[CHROMA_ID_BATCH_SIZE + 3]
    present = [i for i in ids if i != hole]
    db = _make_chroma_sqlite(
        tmp_path / "chroma.sqlite3",
        [{"id": eid, "source": NEW} for eid in present],
    )
    result = lookup_chroma_by_embedding_ids(db, ids)
    assert result.requested == n
    assert result.found == n - 1
    assert result.missing == 1
    assert result.missing_ids == (hole,)


# ---------------------------------------------------------------------------
# P0 adversarial regression tests
# ---------------------------------------------------------------------------


def _resolve_no_mutation(env: dict, intent: str, targets: list[str], **kwargs):
    """Like _resolve but accepts extra_files/extra_dirs through to snapshot."""
    extra_files = kwargs.pop("extra_files", [])
    extra_dirs = kwargs.pop("extra_dirs", [])
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
        extra_files=extra_files,
        extra_dirs=extra_dirs,
    )
    plan = resolve_library_state(
        intent,
        targets,
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=kwargs.pop("registry_db", env["registry"] if env["registry"].exists() else None),
        tracker_path=kwargs.pop("tracker_path", env["tracker"] if env["tracker"].exists() else None),
        **kwargs,
    )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
        extra_files=extra_files,
        extra_dirs=extra_dirs,
    )
    assert after == before, "resolver mutated isolated fixture stores"
    return plan


def test_tracker_absolute_path_outside_library_root_is_not_observed(env) -> None:
    """Tracker source path containing an absolute external path must not be hashed or read.

    The registry enforces relative paths at write time, so the absolute-path
    containment risk arises through the tracker (embedded.json), which stores
    raw path strings with no validation at write time.
    """
    _write(env["library"], NEW, BYTES_A)
    sentinel = env["library"].parent / "sentinel_external.pdf"
    sentinel.write_bytes(b"%PDF-EXTERNAL-SENTINEL")

    tracker_path = env["persist"] / "embedded.json"
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_external = str(sentinel)
    payload = {
        HASH_A: {
            "paths": [NEW, absolute_external],
            "chunk_ids": list(CHUNK_IDS),
            "collection": "maker-manuals",
            "document_id": DOC_A,
        }
    }
    tracker_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    plan = _resolve_no_mutation(env, INTENT_CHECK, [NEW], extra_files=[sentinel])

    hashed = plan.authority_snapshot["hashed_paths"]
    inspected = plan.authority_snapshot["inspected_paths"]

    assert absolute_external not in hashed
    assert absolute_external not in inspected
    for p in hashed + inspected:
        assert not p.startswith("/"), f"absolute external path leaked into hashed_paths: {p}"

    gaps = plan.evidence_gaps
    assert any("candidate_path_containment" in g for g in gaps), (
        f"expected containment gap, got: {gaps}"
    )


def test_registry_alias_outside_library_root_is_not_observed(env, tmp_path) -> None:
    """Registry DB with a malformed absolute alias must not cause that path to be hashed.

    The Python registry helpers reject absolute paths, so we inject the bad alias
    directly into the SQLite database to simulate a corrupt / adversarial registry.
    The resolver must detect that the resolved candidate escapes library_root and
    record a deterministic containment gap without hashing/reading the external file.
    """
    _write(env["library"], NEW, BYTES_A)
    sentinel = env["library"].parent / "registry_alias_sentinel.pdf"
    sentinel.write_bytes(b"%PDF-REGISTRY-ALIAS-SENTINEL")

    # Seed a valid registry entry, then inject an absolute external alias directly
    # into the SQLite source_files table, bypassing Python-layer validation.
    _seed_registry(
        env["registry"],
        aliases=[NEW],
        chunk_ids=list(CHUNK_IDS),
        vector_ids=list(CHUNK_IDS),
    )
    conn = sqlite3.connect(str(env["registry"]))
    try:
        # Fetch the document_id registered for NEW.
        row = conn.execute(
            "SELECT document_id FROM source_files WHERE relative_path = ? LIMIT 1",
            (NEW,),
        ).fetchone()
        assert row is not None, "expected source_files row for NEW"
        doc_id = row[0]
        # Insert the absolute external path directly, supplying all NOT NULL fields.
        import uuid as _uuid
        import datetime as _dt
        now = _dt.datetime.utcnow().isoformat()
        fake_id = str(_uuid.uuid4())
        conn.execute(
            "INSERT OR IGNORE INTO source_files "
            "(source_file_id, document_id, relative_path, filename, "
            " first_seen_at, last_seen_at, source_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fake_id, doc_id, str(sentinel), "registry_alias_sentinel.pdf", now, now, HASH_A),
        )
        conn.commit()
    finally:
        conn.close()

    plan = _resolve_no_mutation(env, INTENT_CHECK, [NEW], extra_files=[sentinel])

    hashed = plan.authority_snapshot["hashed_paths"]
    inspected = plan.authority_snapshot["inspected_paths"]

    assert str(sentinel) not in hashed, "external registry alias was hashed"
    assert str(sentinel) not in inspected, "external registry alias was inspected"
    for p in hashed + inspected:
        assert not p.startswith("/"), f"absolute external path leaked: {p}"

    gaps = plan.evidence_gaps
    assert any("candidate_path_containment" in g for g in gaps), (
        f"expected containment gap for registry alias, got: {gaps}"
    )


def test_tracker_path_traversal_is_not_observed(env) -> None:
    """Tracker record with a traversal source path must not be hashed or read."""
    _write(env["library"], NEW, BYTES_A)
    sentinel = env["library"].parent / "traversal_sentinel.pdf"
    sentinel.write_bytes(b"%PDF-TRAVERSAL-SENTINEL")

    # Write a tracker record whose 'paths' list contains a traversal string.
    traversal_path = "../traversal_sentinel.pdf"
    tracker_path = env["persist"] / "embedded.json"
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        HASH_A: {
            "paths": [NEW, traversal_path],
            "chunk_ids": list(CHUNK_IDS),
            "collection": "maker-manuals",
            "document_id": DOC_A,
        }
    }
    tracker_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    plan = _resolve_no_mutation(
        env,
        INTENT_CHECK,
        [NEW],
        extra_files=[sentinel],
    )

    hashed = plan.authority_snapshot["hashed_paths"]
    inspected = plan.authority_snapshot["inspected_paths"]

    assert traversal_path not in hashed
    assert traversal_path not in inspected
    assert str(sentinel) not in hashed
    assert str(sentinel) not in inspected

    gaps = plan.evidence_gaps
    assert any("candidate_path_containment" in g for g in gaps), (
        f"expected containment gap for traversal, got: {gaps}"
    )


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to"),
    reason="symlink creation not supported on this platform",
)
def test_candidate_symlink_escape_is_not_observed(env) -> None:
    """Symlink inside library_root that resolves outside must be excluded."""
    _write(env["library"], NEW, BYTES_A)
    sentinel = env["library"].parent / "symlink_escape_sentinel.pdf"
    sentinel.write_bytes(b"%PDF-SYMLINK-ESCAPE-SENTINEL")

    # Create an in-root symlink that escapes to the sentinel.
    escaped_rel = "00_Career/symlink_escape.pdf"
    link_path = env["library"] / escaped_rel
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to(sentinel)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation failed on this platform")

    # Seed registry so the escaped path appears as a candidate alias.
    _seed_registry(
        env["registry"],
        aliases=[NEW, escaped_rel],
        chunk_ids=list(CHUNK_IDS),
        vector_ids=list(CHUNK_IDS),
    )

    plan = _resolve_no_mutation(
        env,
        INTENT_CHECK,
        [NEW],
        extra_files=[sentinel, link_path],
    )

    hashed = plan.authority_snapshot["hashed_paths"]
    inspected = plan.authority_snapshot["inspected_paths"]

    # The escaped path must not have been hashed.
    assert escaped_rel not in hashed, (
        f"symlink-escaped path was hashed: {escaped_rel}"
    )

    gaps = plan.evidence_gaps
    assert any("candidate_path_containment" in g for g in gaps), (
        f"expected containment gap for symlink escape, got: {gaps}"
    )


def test_journal_path_outside_root_is_rejected_without_read(env, tmp_path, monkeypatch) -> None:
    """An absolute external journal_path must not be opened/parsed; gap recorded instead."""
    import rag_engine.library_state.evidence as _ev_mod

    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    external_journal = tmp_path / "evil_journal.json"
    # Invalid JSON - would raise if parsed.
    external_journal.write_bytes(b"NOT_JSON_SENTINEL_SHOULD_NOT_BE_PARSED")

    # Monkeypatch read_journal so the test fails if it is called for the external path.
    _original_read_journal = _ev_mod.read_journal

    def _guarded_read_journal(path):
        resolved = Path(path).resolve()
        assert str(resolved) != str(external_journal.resolve()), (
            f"read_journal was called for the rejected external path: {path}"
        )
        return _original_read_journal(path)

    monkeypatch.setattr(_ev_mod, "read_journal", _guarded_read_journal)

    plan = _resolve_no_mutation(
        env, INTENT_VERIFY, [NEW],
        journal_path=str(external_journal),
        extra_files=[external_journal],
    )

    journal_val = plan.authority_snapshot.get("journal")
    assert journal_val is None, f"external journal was parsed: {journal_val}"

    from rag_engine.library_state.contract import GAP_JOURNAL_EXACT_LOCATOR_MISSING
    assert GAP_JOURNAL_EXACT_LOCATOR_MISSING in plan.evidence_gaps


def test_journal_path_traversal_is_rejected_without_read(env, tmp_path, monkeypatch) -> None:
    """A journal_path traversing outside the journal root must never be opened/parsed."""
    import rag_engine.library_state.evidence as _ev_mod

    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    journal_dir = env["persist"] / "certified_append_journal"
    journal_dir.mkdir(exist_ok=True)

    escaped_target = env["persist"] / "escape_target.json"
    # Invalid JSON - would raise if parsed.
    escaped_target.write_bytes(b"NOT_JSON_TRAVERSAL_SENTINEL")

    traversal_journal_path = journal_dir / ".." / "escape_target.json"

    _original_read_journal = _ev_mod.read_journal

    def _guarded_read_journal(path):
        resolved = Path(path).resolve()
        assert str(resolved) != str(escaped_target.resolve()), (
            f"read_journal was called for the rejected traversal target: {path}"
        )
        return _original_read_journal(path)

    monkeypatch.setattr(_ev_mod, "read_journal", _guarded_read_journal)

    plan = _resolve_no_mutation(
        env, INTENT_VERIFY, [NEW],
        journal_path=str(traversal_journal_path),
        extra_files=[escaped_target],
    )

    journal_val = plan.authority_snapshot.get("journal")
    assert journal_val is None, f"traversal journal was parsed: {journal_val}"

    from rag_engine.library_state.contract import GAP_JOURNAL_EXACT_LOCATOR_MISSING
    assert GAP_JOURNAL_EXACT_LOCATOR_MISSING in plan.evidence_gaps


def test_journal_path_without_persist_dir_is_rejected_without_read(tmp_path) -> None:
    """With persist_dir=None, journal_path must be rejected without opening the file."""
    import rag_engine.library_state.evidence as _ev_mod

    library = tmp_path / "lib"
    library.mkdir()
    _write(library, NEW, BYTES_A)

    external_journal = tmp_path / "no_persist_journal.json"
    # Invalid JSON sentinel - would raise a JSON decode error if parsed.
    external_journal.write_bytes(b"INVALID_JSON_NO_PERSIST_SENTINEL")

    _original_read_journal = _ev_mod.read_journal

    def _guarded_read_journal(path):
        raise AssertionError(
            f"read_journal must not be called when persist_dir is None; called with: {path}"
        )

    # We monkeypatch the evidence module directly since that's where the call is made.
    original = _ev_mod.read_journal
    _ev_mod.read_journal = _guarded_read_journal
    try:
        plan = resolve_library_state(
            INTENT_CHECK,
            [NEW],
            library_root=library,
            persist_dir=None,
            journal_path=str(external_journal),
        )
    finally:
        _ev_mod.read_journal = original

    journal_val = plan.authority_snapshot.get("journal")
    assert journal_val is None, f"journal was parsed despite persist_dir=None: {journal_val}"

    from rag_engine.library_state.contract import GAP_JOURNAL_EXACT_LOCATOR_MISSING
    assert GAP_JOURNAL_EXACT_LOCATOR_MISSING in plan.evidence_gaps


def test_journal_non_record_file_is_rejected(env, monkeypatch) -> None:
    """Non-journal files and tracker snapshots in the journal dir must be rejected.

    Three sub-cases:
    1. not_a_journal.txt       - rejected by .json suffix check.
    2. capp:...:deadbeef.tracker.bak.json - real certified-append tracker snapshot
       filename; rejected by the .tracker.bak stem predicate; read_journal must
       never be called for it.
    3. capp:...:deadbeef.json  - valid append journal; must be accepted.
    """
    import rag_engine.library_state.evidence as _ev_mod

    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    journal_dir = env["persist"] / "certified_append_journal"
    journal_dir.mkdir(exist_ok=True)

    op_id = "capp:20260819T090000Z:deadbeef"

    # Case 1: non-JSON extension.
    non_json = journal_dir / "not_a_journal.txt"
    non_json.write_text("plain text, not a journal", encoding="utf-8")

    # Case 2: real tracker snapshot format from snapshot_tracker().
    tracker_snap = journal_dir / f"{op_id}.tracker.bak.json"
    tracker_snap.write_bytes(b"TRACKER_SNAPSHOT_SHOULD_NOT_BE_PARSED")

    # Case 3: valid append journal.
    valid_journal = journal_dir / f"{op_id}.json"
    valid_journal.write_text(
        json.dumps({"operation_id": op_id, "state": "SUCCESS"}), encoding="utf-8"
    )

    all_extra = [non_json, tracker_snap, valid_journal]

    from rag_engine.library_state.contract import GAP_JOURNAL_EXACT_LOCATOR_MISSING

    # --- Case 1: .txt file rejected ---
    plan_txt = _resolve_no_mutation(
        env, INTENT_CHECK, [NEW],
        journal_path=str(non_json),
        extra_files=all_extra,
        extra_dirs=[journal_dir],
    )
    assert plan_txt.authority_snapshot.get("journal") is None
    assert GAP_JOURNAL_EXACT_LOCATOR_MISSING in plan_txt.evidence_gaps

    # --- Case 2: tracker snapshot rejected; read_journal must not be called ---
    _original_read_journal = _ev_mod.read_journal

    def _guarded_read_journal(path):
        resolved = Path(path).resolve()
        assert str(resolved) != str(tracker_snap.resolve()), (
            f"read_journal was called for the tracker snapshot: {path}"
        )
        return _original_read_journal(path)

    monkeypatch.setattr(_ev_mod, "read_journal", _guarded_read_journal)

    plan_snap = _resolve_no_mutation(
        env, INTENT_CHECK, [NEW],
        journal_path=str(tracker_snap),
        extra_files=all_extra,
        extra_dirs=[journal_dir],
    )
    assert plan_snap.authority_snapshot.get("journal") is None, (
        "tracker snapshot must not be parsed as a journal"
    )
    assert GAP_JOURNAL_EXACT_LOCATOR_MISSING in plan_snap.evidence_gaps

    monkeypatch.undo()

    # --- Case 3: valid capp journal still accepted ---
    plan_ok = _resolve_no_mutation(
        env, INTENT_CHECK, [NEW],
        journal_path=str(valid_journal),
        extra_files=all_extra,
        extra_dirs=[journal_dir],
    )
    jval = plan_ok.authority_snapshot.get("journal")
    assert jval is not None, "expected valid append journal to be read"
    assert jval.get("operation_id") == op_id
    assert GAP_JOURNAL_EXACT_LOCATOR_MISSING not in plan_ok.evidence_gaps


def test_journal_operation_id_traversal_is_rejected_without_read(env) -> None:
    """Malicious operation_id values must never escape the journal directory."""
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    journal_dir = env["persist"] / "certified_append_journal"
    journal_dir.mkdir(exist_ok=True)

    # Plant a file the traversal IDs would try to reach.
    escape_target = env["persist"] / "escape_via_opid.json"
    escape_target.write_text(
        json.dumps({"sentinel": "OPID_TRAVERSAL_SHOULD_NOT_READ"}), encoding="utf-8"
    )

    malicious_ids = [
        "../escape_via_opid",         # traversal segment
        "../../etc/passwd",            # deep traversal
        "foo/bar",                     # slash in ID
        "foo\\bar",                    # backslash in ID
        "good\x00bad",                 # NUL byte
        "\x01control",                 # control character
    ]

    from rag_engine.library_state.contract import GAP_JOURNAL_EXACT_LOCATOR_MISSING

    for bad_id in malicious_ids:
        plan = _resolve_no_mutation(
            env,
            INTENT_VERIFY,
            [NEW],
            operation_id=bad_id,
            extra_files=[escape_target],
        )
        journal_val = plan.authority_snapshot.get("journal")
        assert journal_val is None, (
            f"malicious operation_id {bad_id!r} was resolved to a journal: {journal_val}"
        )
        assert GAP_JOURNAL_EXACT_LOCATOR_MISSING in plan.evidence_gaps, (
            f"expected gap for bad operation_id {bad_id!r}, got: {plan.evidence_gaps}"
        )


def test_valid_certified_append_operation_id_reads_exact_journal_only(env) -> None:
    """A valid capp:... operation_id reads exactly its journal file, not others."""
    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    journal_dir = env["persist"] / "certified_append_journal"
    journal_dir.mkdir(exist_ok=True)

    valid_id = "capp:20260819T120000Z:abcdef1234567890"
    target_journal = journal_dir / f"{valid_id}.json"
    other_journal = journal_dir / "capp:20260819T110000Z:aabbccdd.json"

    target_journal.write_text(
        json.dumps({"operation_id": valid_id, "state": "SUCCESS"}), encoding="utf-8"
    )
    other_journal.write_text(
        json.dumps({"operation_id": "other", "state": "SUCCESS"}), encoding="utf-8"
    )

    plan = _resolve_no_mutation(
        env,
        INTENT_CHECK,
        [NEW],
        operation_id=valid_id,
        extra_files=[target_journal, other_journal],
        extra_dirs=[journal_dir],
    )

    journal_val = plan.authority_snapshot.get("journal")
    assert journal_val is not None, "expected journal to be read for valid operation_id"
    assert journal_val.get("operation_id") == valid_id
    # The other journal's content must not appear.
    assert journal_val.get("operation_id") != "other"

    from rag_engine.library_state.contract import GAP_JOURNAL_NO_EXACT_LOCATOR
    assert GAP_JOURNAL_NO_EXACT_LOCATOR not in plan.evidence_gaps


def _make_compat_specs(model: str, library: Path):
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
    )
    emb = build_embedding_spec(embedding_model=model, chunk_size=512, chunk_overlap=50)
    corp = build_corpus_spec(library_root=str(library))
    idx = build_index_spec(embedding_spec=emb, corpus_spec=corp)
    return emb, corp, idx


def test_registry_fingerprint_conflict_is_not_verified(env) -> None:
    """Resolver must surface a conflict and classify as AMBIGUOUS/MANUAL_REVIEW, not INDEXED_OK."""
    from rag_engine.index_compatibility.builders import stored_envelope_from_specs
    from rag_engine.index_compatibility.state import write_registry_fingerprint, write_sidecar_v1
    from rag_engine.library_state.contract import (
        CLASS_AMBIGUOUS,
        CLASS_INDEXED_OK,
        OP_MANUAL_REVIEW,
        RESULT_VERIFIED,
    )

    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    try:
        emb_a, corp, idx_a = _make_compat_specs("model-conflict-a", env["library"])
        emb_b, _, idx_b = _make_compat_specs("model-conflict-b", env["library"])
    except Exception:
        pytest.skip("fingerprint spec builders unavailable in this environment")

    write_sidecar_v1(env["persist"], stored_envelope_from_specs(emb_a, corp, idx_a))
    write_registry_fingerprint(
        env["registry"], stored_envelope_from_specs(emb_b, corp, idx_b)
    )

    plan = _resolve_no_mutation(env, INTENT_VERIFY, [NEW])

    gen = plan.authority_snapshot.get("generation") or {}
    state = gen.get("state", "")
    assert state == "CONFLICT", (
        f"expected generation state CONFLICT, got {state!r}; full gen: {gen}"
    )
    assert plan.result != RESULT_VERIFIED, (
        f"resolver returned RESULT_VERIFIED despite fingerprint conflict; result={plan.result}"
    )
    assert plan.classification != CLASS_INDEXED_OK, (
        f"resolver returned INDEXED_OK despite fingerprint conflict; classification={plan.classification}"
    )
    assert plan.classification == CLASS_AMBIGUOUS, (
        f"expected AMBIGUOUS on conflict, got {plan.classification!r}"
    )
    assert plan.proposed_operation == OP_MANUAL_REVIEW, (
        f"expected MANUAL_REVIEW on conflict, got {plan.proposed_operation!r}"
    )


def test_fingerprint_compatible_preserves_indexed_ok(env) -> None:
    """When sidecar and registry fingerprints agree, INDEXED_OK / VERIFIED must be preserved."""
    from rag_engine.index_compatibility.builders import stored_envelope_from_specs
    from rag_engine.index_compatibility.state import write_registry_fingerprint, write_sidecar_v1
    from rag_engine.library_state.contract import CLASS_INDEXED_OK, RESULT_VERIFIED

    _seed_moved_or_indexed(env, live_rel=NEW, governed_paths=[NEW], chroma_source=NEW)

    try:
        emb, corp, idx = _make_compat_specs("model-ok", env["library"])
    except Exception:
        pytest.skip("fingerprint spec builders unavailable in this environment")

    envelope = stored_envelope_from_specs(emb, corp, idx)
    write_sidecar_v1(env["persist"], envelope)
    write_registry_fingerprint(env["registry"], envelope)

    plan = _resolve_no_mutation(env, INTENT_VERIFY, [NEW])

    gen = plan.authority_snapshot.get("generation") or {}
    assert gen.get("state") == "KNOWN_COMPATIBLE", (
        f"expected KNOWN_COMPATIBLE, got: {gen.get('state')!r}"
    )
    assert plan.classification == CLASS_INDEXED_OK, (
        f"expected INDEXED_OK with compatible fingerprints, got {plan.classification!r}"
    )
    assert plan.result == RESULT_VERIFIED, (
        f"expected VERIFIED with compatible fingerprints, got {plan.result!r}"
    )
