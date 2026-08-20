"""Tests for user-facing governed MOVE CLI - isolated fixtures only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.governed_move import (
    OUTCOME_BLOCKED,
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    SingleFileMoveResult,
)
from rag_engine.governed_move import single_file_move as sfm_module
from rag_engine.governed_move.move_journal import JOURNAL_DIR_NAME, PHASE_VERIFIED
from rag_engine.governed_move.__main__ import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_OUTCOME,
    UNSUPPORTED_BEHAVIOR_MSG,
    main,
)
from rag_engine.library_state.contract import EMBEDDING_NONE, INTENT_PLAN_MOVE
from rag_engine.library_state.move_approval import (
    MoveApprovalContext,
    compute_approval_digest,
    plan_digest,
)
from rag_engine.library_state.move_preflight import collect_pre_move_evidence
from rag_engine.metadata_registry import (
    initialize_locator_lifecycle_state,
    initialize_registry,
    migrate_connection,
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

ROOT = Path(__file__).resolve().parents[1]

OLD = "manuals/MAN_old_name.pdf"
NEW = "manuals/MAN_new_name.pdf"
THIRD = "manuals/other_alias.pdf"

BYTES = b"%PDF-1.4\ngoverned move cli fixture\n"
HASH = source_hash_from_bytes(BYTES)
DOC = document_id_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "governed-move-cli")
FP = "ef" * 32
ID1 = chunk_id(DOC, FP, 0)
ID2 = chunk_id(DOC, FP, 1)
VECTOR_IDS = (ID1, ID2)
COLLECTION = "maker-manuals"
OPERATION_ID = "governed-move-cli-op-001"
NEW_PLACEHOLDER_SF = "sf-new-placeholder-cli-0002"

ISSUED_AT = "2026-08-19T12:00:00Z"
EXPIRES_AT = "2026-08-19T12:15:00Z"
NOW_VALID = "2026-08-19T12:05:00Z"

UNSUPPORTED_FLAGS = ("--delete", "--bulk", "--approve", "--route", "--force", "--skip-verify", "--no-lock")

FORBIDDEN_CALL_TARGETS = (
    "rag_engine.cli.main",
    "rag_engine.reconcile_path.cmd_reconcile_path",
)


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_chroma(path: Path, *, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE segments (
                id TEXT PRIMARY KEY, type TEXT, scope TEXT,
                collection TEXT REFERENCES collections(id)
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL, seq_id BLOB NOT NULL,
                UNIQUE (segment_id, embedding_id)
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id), key TEXT NOT NULL,
                string_value TEXT, int_value INTEGER, float_value REAL,
                bool_value INTEGER, PRIMARY KEY (id, key)
            );
            """
        )
        coll_id = str(uuid.uuid4())
        seg_id = str(uuid.uuid4())
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (coll_id, "langchain"))
        conn.execute(
            "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
            (seg_id, "vector", "VECTOR", coll_id),
        )
        for i, eid in enumerate(VECTOR_IDS, start=1):
            conn.execute(
                "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) VALUES (?, ?, ?, ?)",
                (i, seg_id, eid, b"\x00"),
            )
            for key, val in (
                ("source", source),
                ("collection", COLLECTION),
                ("document_id", DOC),
                ("source_hash", HASH),
            ):
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i, key, val),
                )
        conn.commit()
    finally:
        conn.close()


def _write_tracker(path: Path, *, paths: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        HASH: {
            "paths": paths,
            "chunk_ids": list(VECTOR_IDS),
            "collection": COLLECTION,
            "document_id": DOC,
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_env(env: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    lock = env["persist"] / sfm_module.MOVE_LOCK_NAME
    journal_root = env["persist"] / JOURNAL_DIR_NAME
    for path in (env["library"], env["persist"], env["registry"], env["tracker"], env["chroma"], lock, journal_root):
        if path.is_file():
            out[str(path)] = _file_sha(path)
        elif path.is_dir():
            out[str(path)] = json.dumps(sorted(p.name for p in path.iterdir()))
        else:
            out[str(path)] = None
    for rel in (OLD, NEW):
        p = env["library"] / rel
        out[str(p)] = _file_sha(p) if p.is_file() else None
    return out


def _seed_env(env: dict) -> dict:
    _write(env["library"], OLD, BYTES)
    registry = env["registry"].resolve()
    initialize_registry(registry)
    with open_registry(registry) as conn:
        migrate_connection(conn, target_version=5)
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=DOC, subject_id=SUBJECT, source_hash=HASH
            )
            old_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=OLD,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            third_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=THIRD,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            for ordinal, cid in enumerate(VECTOR_IDS):
                register_chunk(
                    conn,
                    chunk_id=cid,
                    document_id=DOC,
                    chunking_fingerprint=FP,
                    ordinal=ordinal,
                )
            for cid in VECTOR_IDS:
                register_vector_mapping(
                    conn,
                    chunk_id=cid,
                    chroma_embedding_id=cid,
                    mapping_status="native_chunk_id",
                )
        for sf in (old_sf, third_sf):
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=sf,
                document_id=DOC,
                source="test_fixture",
            )
        conn.commit()
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma(env["chroma"], source=OLD)
    env["old_sf"] = old_sf
    env["third_sf"] = third_sf
    return env


def _build_artifact(env: dict, evidence, context: MoveApprovalContext) -> dict:
    item = next(c for c in evidence.plan.classifications if c.target == OLD)
    artifact = {
        "schema_version": 1,
        "approval_id": "move-approval-cli-001",
        "operation": "MOVE",
        "intent": INTENT_PLAN_MOVE,
        "request_id": evidence.plan.request_id,
        "plan_digest": plan_digest(evidence.plan),
        "source_path": OLD,
        "destination_path": evidence.destination_path,
        "document_id": item.document_id,
        "source_hash": item.source_hash,
        "resolver_classification": evidence.plan.classification,
        "proposed_operation": evidence.plan.proposed_operation,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "affected_source_file_ids": list(context.affected_source_file_ids),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
    }
    artifact["approval_digest"] = compute_approval_digest(artifact)
    return artifact


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    library.mkdir()
    persist.mkdir()
    registry = (tmp_path / "registry" / "metadata.sqlite3").resolve()
    registry.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "library": library,
        "persist": persist,
        "registry": registry,
        "tracker": (persist / "embedded.json").resolve(),
        "chroma": (persist / "chroma.sqlite3").resolve(),
    }
    return _seed_env(base)


@pytest.fixture()
def move_context(env: dict) -> MoveApprovalContext:
    return MoveApprovalContext(
        affected_source_file_ids=(env["old_sf"], NEW_PLACEHOLDER_SF),
        registry_db_path=str(env["registry"]),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
    )


@pytest.fixture()
def pre_move_evidence(env: dict):
    return collect_pre_move_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        source_path=OLD,
        destination_path=NEW,
        operation_id=OPERATION_ID,
    )


@pytest.fixture()
def approval_file(env: dict, pre_move_evidence, move_context: MoveApprovalContext, tmp_path: Path) -> Path:
    artifact = _build_artifact(env, pre_move_evidence, move_context)
    path = tmp_path / "move_approval.json"
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    return path


def _move_args(
    env: dict,
    approval_file: Path,
    *,
    execute: bool = False,
    extra: list[str] | None = None,
) -> list[str]:
    args = [
        "move",
        "--library-root",
        str(env["library"].resolve()),
        "--persist-dir",
        str(env["persist"].resolve()),
        "--registry-db",
        str(env["registry"]),
        "--tracker-path",
        str(env["tracker"]),
        "--source",
        OLD,
        "--destination",
        NEW,
        "--approval-file",
        str(approval_file.resolve()),
        "--old-source-file-id",
        env["old_sf"],
        "--new-source-file-id",
        NEW_PLACEHOLDER_SF,
        "--registry-collection",
        COLLECTION,
        "--operation-id",
        OPERATION_ID,
        "--vector-id",
        ID1,
        "--vector-id",
        ID2,
    ]
    if execute:
        args.append("--execute")
    if extra:
        args.extend(extra)
    return args


def _recover_args(env: dict, *, operation_id: str = OPERATION_ID) -> list[str]:
    return [
        "recover",
        "--persist-dir",
        str(env["persist"].resolve()),
        "--operation-id",
        operation_id,
        "--library-root",
        str(env["library"].resolve()),
        "--registry-db",
        str(env["registry"]),
        "--tracker-path",
        str(env["tracker"]),
    ]


def _run_module(args: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rag_engine.governed_move", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_move_dry_run_succeeds_without_lock_journal_or_store_writes(
    env,
    approval_file,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _snapshot_env(env)
    with mock.patch(
        "rag_engine.governed_move.single_file_move._default_now_utc",
        return_value=NOW_VALID,
    ):
        code = main(_move_args(env, approval_file, execute=False))
    after = _snapshot_env(env)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == EXIT_OK
    assert after == before
    assert not (env["persist"] / sfm_module.MOVE_LOCK_NAME).exists()
    assert not (env["persist"] / JOURNAL_DIR_NAME).exists()
    assert payload["outcome"] == OUTCOME_DRY_RUN


def test_move_execute_happy_path_reports_verified(
    env,
    approval_file,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch(
        "rag_engine.governed_move.single_file_move._default_now_utc",
        return_value=NOW_VALID,
    ):
        code = main(_move_args(env, approval_file, execute=True))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == EXIT_OK
    assert not (env["library"] / OLD).exists()
    assert (env["library"] / NEW).is_file()
    journal = env["persist"] / JOURNAL_DIR_NAME / f"{OPERATION_ID}.json"
    assert journal.is_file()
    assert payload["outcome"] == OUTCOME_SUCCESS
    assert payload["journal_phase"] == PHASE_VERIFIED


@pytest.mark.parametrize(
    "omit_flag",
    [
        "--library-root",
        "--persist-dir",
        "--registry-db",
        "--tracker-path",
        "--source",
        "--destination",
        "--approval-file",
        "--old-source-file-id",
        "--new-source-file-id",
        "--registry-collection",
        "--operation-id",
    ],
)
def test_missing_required_explicit_args_exits_2(env, approval_file, omit_flag: str) -> None:
    args = _move_args(env, approval_file)
    idx = args.index(omit_flag)
    del args[idx : idx + 2]
    assert main(args) == EXIT_INPUT


def test_missing_vector_ids_exits_2(env, approval_file) -> None:
    args = _move_args(env, approval_file)
    args = [a for i, a in enumerate(args) if not (a == "--vector-id" or (i > 0 and args[i - 1] == "--vector-id"))]
    assert main(args) == EXIT_INPUT


def test_missing_approval_file_exits_2(env) -> None:
    args = _move_args(env, Path("/nonexistent/approval.json"))
    assert main(args) == EXIT_INPUT


def test_malformed_approval_file_exits_2(env, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(_move_args(env, bad)) == EXIT_INPUT


@pytest.mark.parametrize("flag", UNSUPPORTED_FLAGS)
def test_unsupported_mutation_routing_flags_rejected(flag: str) -> None:
    code = main(["move", flag])
    assert code == EXIT_INPUT


def test_unsupported_flag_error_mentions_not_supported() -> None:
    proc = _run_module(["move", "--approve"])
    assert proc.returncode == EXIT_INPUT
    assert "approval creation" in proc.stderr
    assert "automatic routing" in proc.stderr
    assert "DELETE" in proc.stderr


def test_blocked_result_exits_3(env, approval_file) -> None:
    lock = env["persist"] / sfm_module.MOVE_LOCK_NAME
    lock.write_text("held\n", encoding="utf-8")
    with mock.patch(
        "rag_engine.governed_move.single_file_move._default_now_utc",
        return_value=NOW_VALID,
    ):
        code = main(_move_args(env, approval_file, execute=True))
    assert code == EXIT_OUTCOME


@mock.patch("rag_engine.governed_move.__main__.execute_single_file_move")
def test_recovery_required_result_exits_3(mock_execute, env, approval_file) -> None:
    mock_execute.return_value = SingleFileMoveResult(
        outcome=OUTCOME_RECOVERY_REQUIRED,
        execute=True,
        operation_id=OPERATION_ID,
        success=False,
        dry_run=False,
        compensated=False,
        recovery_required=True,
        residual_unrecovered=("post_commit_failure: simulated",),
        error_message="post_commit_failure",
    )
    code = main(_move_args(env, approval_file, execute=True))
    assert code == EXIT_OUTCOME


def test_recover_requires_exact_operation_id(env) -> None:
    args = _recover_args(env)
    idx = args.index("--operation-id")
    del args[idx : idx + 2]
    assert main(args) == EXIT_INPUT


def test_recover_never_lists_journal_directories(env, approval_file) -> None:
    with mock.patch(
        "rag_engine.governed_move.single_file_move._default_now_utc",
        return_value=NOW_VALID,
    ):
        main(_move_args(env, approval_file, execute=True))
    journal_root = env["persist"] / JOURNAL_DIR_NAME

    listdir_calls: list[str] = []

    original_listdir = Path.iterdir

    def _tracking_iterdir(self: Path):
        if self.resolve() == journal_root.resolve():
            listdir_calls.append(str(self))
        return original_listdir(self)

    with mock.patch.object(Path, "iterdir", _tracking_iterdir):
        code = main(_recover_args(env))
    assert code in {EXIT_OK, EXIT_OUTCOME}
    assert listdir_calls == []


@pytest.mark.parametrize("target", FORBIDDEN_CALL_TARGETS)
def test_cli_never_calls_forbidden_modules(target: str, env, approval_file) -> None:
    with mock.patch(target) as forbidden:
        with mock.patch(
            "rag_engine.governed_move.single_file_move._default_now_utc",
            return_value=NOW_VALID,
        ):
            main(_move_args(env, approval_file, execute=False))
        forbidden.assert_not_called()


def test_cli_output_json_includes_outcome_journal_and_residual(
    env,
    approval_file,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch(
        "rag_engine.governed_move.single_file_move._default_now_utc",
        return_value=NOW_VALID,
    ):
        code = main(_move_args(env, approval_file, execute=False))
    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(captured.out)
    assert "outcome" in payload
    assert "journal_path" in payload
    assert "journal_phase" in payload
    assert "residual_unrecovered" in payload
    assert payload["command"] == "move"
    assert payload["outcome"] == OUTCOME_DRY_RUN


def test_governed_move_main_source_never_imports_forbidden_modules() -> None:
    source = (ROOT / "rag_engine/governed_move/__main__.py").read_text(encoding="utf-8")
    assert "from rag_engine.cli" not in source
    assert "import rag_engine.cli" not in source
    assert "from rag_engine.reconcile_path" not in source
    assert "import rag_engine.reconcile_path" not in source
    assert "resolve_library_state" not in source
    assert "journal_root_dir" not in source
    assert "compute_approval_digest" not in source
    assert "approval creation, automatic routing, or DELETE" in source
