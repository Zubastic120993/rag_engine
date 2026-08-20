"""Tests for user-facing governed quarantine DELETE CLI - isolated fixtures only."""

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

from rag_engine.governed_delete import (
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    QuarantineDeleteApprovalContext,
    QuarantineDeleteResult,
    collect_quarantine_delete_evidence,
    compute_approval_digest,
    plan_digest,
)
from rag_engine.governed_delete import quarantine_executor as qd_module
from rag_engine.governed_delete.quarantine_journal import JOURNAL_DIR_NAME, PHASE_VERIFIED
from rag_engine.governed_delete.__main__ import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_OUTCOME,
    UNSUPPORTED_BEHAVIOR_MSG,
    main,
)
from rag_engine.library_state.contract import EMBEDDING_NONE, INTENT_PLAN_DELETE
from rag_engine.metadata_registry import (
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    append_source_file_event,
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

TARGET = "manuals/MAN_duplicate_old.pdf"
RETAINED = "manuals/MAN_duplicate_retained.pdf"
QUARANTINE = "quarantine/MAN_duplicate_old.pdf"
QUARANTINE_PARENT = "quarantine"

BYTES = b"%PDF-1.4\ngoverned delete cli fixture\n"
HASH = source_hash_from_bytes(BYTES)
DOC = document_id_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "governed-delete-cli")
FP = "ab" * 32
ID1 = chunk_id(DOC, FP, 0)
ID2 = chunk_id(DOC, FP, 1)
VECTOR_IDS = (ID1, ID2)
COLLECTION = "maker-manuals"
OPERATION_ID = "quarantine-delete-cli-op-001"

ISSUED_AT = "2026-08-19T12:00:00Z"
EXPIRES_AT = "2026-08-19T12:15:00Z"
NOW_VALID = "2026-08-19T12:05:00Z"


@pytest.fixture(autouse=True)
def fixed_clock():
    with mock.patch("rag_engine.governed_delete.__main__._default_now_utc", return_value=NOW_VALID):
        with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
            yield


UNSUPPORTED_FLAGS = (
    "--delete",
    "--permanent",
    "--purge",
    "--bulk",
    "--approve",
    "--route",
    "--force",
    "--skip-verify",
    "--no-lock",
)

FORBIDDEN_CALL_TARGETS = (
    "rag_engine.cli.main",
    "rag_engine.reconcile_path.cmd_reconcile_path",
)

HERMES_SKILL_PATH = (
    Path(__file__).resolve().parents[3]
    / "Hermes_Skills"
    / "ce_library_governed_delete"
    / "SKILL.md"
)


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


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


def _write_tracker(path: Path, *, paths: list[str], chunk_ids: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        HASH: {
            "paths": paths,
            "chunk_ids": chunk_ids or list(VECTOR_IDS),
            "collection": COLLECTION,
            "document_id": DOC,
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_env(env: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    lock = env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME
    journal_root = env["persist"] / JOURNAL_DIR_NAME
    for path in (
        env["library"],
        env["persist"],
        env["registry"],
        env["tracker"],
        env["chroma"],
        lock,
        journal_root,
    ):
        if path.is_file():
            out[str(path)] = _file_sha(path)
        elif path.is_dir():
            out[str(path)] = json.dumps(sorted(p.name for p in path.iterdir()))
        else:
            out[str(path)] = None
    for rel in (TARGET, RETAINED, QUARANTINE):
        p = env["library"] / rel
        out[str(p)] = _file_sha(p) if p.is_file() else None
    return out


def _seed_env(env: dict) -> dict:
    _write(env["library"], RETAINED, BYTES)
    _write(env["library"], TARGET, BYTES)
    (env["library"] / QUARANTINE_PARENT).mkdir(parents=True, exist_ok=True)
    registry = env["registry"].resolve()
    initialize_registry(registry)
    with open_registry(registry) as conn:
        migrate_connection(conn, target_version=5)
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=DOC, subject_id=SUBJECT, source_hash=HASH
            )
            retained_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=RETAINED,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            append_source_file_event(
                conn,
                source_file_id=retained_sf,
                document_id=DOC,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("retained-alias"),
            )
            target_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=TARGET,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            append_source_file_event(
                conn,
                source_file_id=target_sf,
                document_id=DOC,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("target-alias"),
            )
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
        for sf in (retained_sf, target_sf):
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=sf,
                document_id=DOC,
                source="test_fixture",
            )
        conn.commit()
    _write_tracker(env["tracker"], paths=[RETAINED])
    _make_chroma(env["chroma"], source=RETAINED)
    env["retained_sf"] = retained_sf
    env["target_sf"] = target_sf
    return env


def _build_artifact(evidence, context: QuarantineDeleteApprovalContext) -> dict:
    artifact = {
        "schema_version": 1,
        "approval_id": "quarantine-delete-cli-approval-001",
        "operation": "QUARANTINE_DELETE",
        "intent": INTENT_PLAN_DELETE,
        "request_id": evidence.plan.request_id,
        "plan_digest": plan_digest(evidence.plan),
        "target_path": evidence.target_path,
        "retained_path": evidence.retained_path,
        "quarantine_path": evidence.quarantine_path,
        "document_id": evidence.document_id,
        "source_hash": evidence.source_hash,
        "resolver_classification": evidence.resolver_classification,
        "proposed_operation": evidence.proposed_operation,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "approved_vector_ids": list(evidence.approved_vector_ids),
        "retained_aliases": list(evidence.retained_aliases),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
        "tracker_path": context.tracker_path,
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
    seeded = _seed_env(base)
    evidence = collect_quarantine_delete_evidence(
        library_root=seeded["library"],
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        tracker_path=seeded["tracker"],
        target_path=TARGET,
        retained_path=RETAINED,
        quarantine_path=QUARANTINE,
    )
    tracker_payload = json.loads(seeded["tracker"].read_text(encoding="utf-8"))
    tracker_payload[HASH]["chunk_ids"] = list(evidence.approved_vector_ids)
    seeded["tracker"].write_text(
        json.dumps(tracker_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    seeded["approved_vector_ids"] = tuple(evidence.approved_vector_ids)
    return seeded


@pytest.fixture()
def delete_context(env: dict) -> QuarantineDeleteApprovalContext:
    return QuarantineDeleteApprovalContext(
        registry_db_path=str(env["registry"]),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
        tracker_path=str(env["tracker"]),
    )


@pytest.fixture()
def preflight_evidence(env: dict):
    return collect_quarantine_delete_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        target_path=TARGET,
        retained_path=RETAINED,
        quarantine_path=QUARANTINE,
    )


@pytest.fixture()
def approval_file(env: dict, preflight_evidence, delete_context, tmp_path: Path) -> Path:
    artifact = _build_artifact(preflight_evidence, delete_context)
    path = tmp_path / "quarantine_delete_approval.json"
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    return path


def _quarantine_args(
    env: dict,
    approval_file: Path,
    *,
    execute: bool = False,
    extra: list[str] | None = None,
) -> list[str]:
    vector_ids = env.get("approved_vector_ids", VECTOR_IDS)
    args = [
        "quarantine",
        "--library-root",
        str(env["library"].resolve()),
        "--persist-dir",
        str(env["persist"].resolve()),
        "--registry-db",
        str(env["registry"]),
        "--tracker-path",
        str(env["tracker"]),
        "--target",
        TARGET,
        "--retained",
        RETAINED,
        "--quarantine",
        QUARANTINE,
        "--approval-file",
        str(approval_file.resolve()),
        "--target-source-file-id",
        env["target_sf"],
        "--retained-source-file-id",
        env["retained_sf"],
        "--registry-collection",
        COLLECTION,
        "--operation-id",
        OPERATION_ID,
        "--vector-id",
        vector_ids[0],
        "--vector-id",
        vector_ids[1],
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
        [sys.executable, "-m", "rag_engine.governed_delete", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_quarantine_dry_run_succeeds_without_lock_journal_or_store_writes(
    env,
    approval_file,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _snapshot_env(env)
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        code = main(_quarantine_args(env, approval_file, execute=False))
    after = _snapshot_env(env)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == EXIT_OK
    assert after == before
    assert not (env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME).exists()
    assert not (env["persist"] / JOURNAL_DIR_NAME).exists()
    assert payload["outcome"] == OUTCOME_DRY_RUN
    assert payload["preview"] is not None
    assert payload["preview"]["target_relative_path"] == TARGET


def test_quarantine_execute_happy_path_reports_verified(
    env,
    approval_file,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        code = main(_quarantine_args(env, approval_file, execute=True))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == EXIT_OK
    assert not (env["library"] / TARGET).exists()
    assert (env["library"] / QUARANTINE).is_file()
    assert (env["library"] / RETAINED).is_file()
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
        "--target",
        "--retained",
        "--quarantine",
        "--approval-file",
        "--target-source-file-id",
        "--retained-source-file-id",
        "--registry-collection",
        "--operation-id",
    ],
)
def test_missing_required_explicit_args_exits_2(env, approval_file, omit_flag: str) -> None:
    args = _quarantine_args(env, approval_file)
    idx = args.index(omit_flag)
    del args[idx : idx + 2]
    assert main(args) == EXIT_INPUT


def test_missing_vector_ids_exits_2(env, approval_file) -> None:
    args = _quarantine_args(env, approval_file)
    args = [a for i, a in enumerate(args) if not (a == "--vector-id" or (i > 0 and args[i - 1] == "--vector-id"))]
    assert main(args) == EXIT_INPUT


def test_duplicate_vector_ids_exits_2(env, approval_file) -> None:
    vector_ids = env.get("approved_vector_ids", VECTOR_IDS)
    args = _quarantine_args(env, approval_file)
    args.extend(["--vector-id", vector_ids[0]])
    assert main(args) == EXIT_INPUT


def test_missing_approval_file_exits_2(env) -> None:
    args = _quarantine_args(env, Path("/nonexistent/approval.json"))
    assert main(args) == EXIT_INPUT


def test_malformed_approval_file_exits_2(env, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    before = _snapshot_env(env)
    assert main(_quarantine_args(env, bad)) == EXIT_INPUT
    assert _snapshot_env(env) == before


def test_invalid_approval_artifact_exits_2(env, approval_file, tmp_path: Path) -> None:
    artifact = json.loads(approval_file.read_text(encoding="utf-8"))
    artifact["plan_digest"] = "0" * 64
    bad = tmp_path / "invalid_approval.json"
    bad.write_text(json.dumps(artifact) + "\n", encoding="utf-8")
    before = _snapshot_env(env)
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        assert main(_quarantine_args(env, bad)) == EXIT_INPUT
    assert _snapshot_env(env) == before


@pytest.mark.parametrize("flag", UNSUPPORTED_FLAGS)
def test_unsupported_flags_rejected(flag: str) -> None:
    assert main(["quarantine", flag]) == EXIT_INPUT


def test_unsupported_flag_error_mentions_not_supported() -> None:
    proc = _run_module(["quarantine", "--permanent"])
    assert proc.returncode == EXIT_INPUT
    assert "permanent deletion" in proc.stderr
    assert "approval issuance" in proc.stderr
    assert UNSUPPORTED_BEHAVIOR_MSG.split(";")[0] in proc.stderr


def test_blocked_result_exits_3(env, approval_file) -> None:
    lock = env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME
    lock.write_text("held\n", encoding="utf-8")
    before = _snapshot_env(env)
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        code = main(_quarantine_args(env, approval_file, execute=True))
    assert code == EXIT_OUTCOME
    assert not (env["library"] / QUARANTINE).exists()
    assert (env["library"] / TARGET).is_file()
    assert _snapshot_env(env) == before


@mock.patch("rag_engine.governed_delete.__main__.execute_quarantine_delete")
def test_recovery_required_result_exits_3(mock_execute, env, approval_file) -> None:
    mock_execute.return_value = QuarantineDeleteResult(
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
    code = main(_quarantine_args(env, approval_file, execute=True))
    assert code == EXIT_OUTCOME


def test_recover_requires_exact_operation_id(env) -> None:
    args = _recover_args(env)
    idx = args.index("--operation-id")
    del args[idx : idx + 2]
    assert main(args) == EXIT_INPUT


@pytest.mark.parametrize("bad_operation_id", ["../evil", "bad/id"])
def test_recover_rejects_invalid_operation_id(env, bad_operation_id: str) -> None:
    assert main(_recover_args(env, operation_id=bad_operation_id)) == EXIT_INPUT


def test_recover_rejects_blank_operation_id(env) -> None:
    args = _recover_args(env, operation_id=" ")
    assert main(args) == EXIT_INPUT


def test_recover_never_lists_journal_directories(env, approval_file) -> None:
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        main(_quarantine_args(env, approval_file, execute=True))
    journal_root = env["persist"] / JOURNAL_DIR_NAME

    listdir_calls: list[str] = []
    original_iterdir = Path.iterdir

    def _tracking_iterdir(self: Path):
        if self.resolve() == journal_root.resolve():
            listdir_calls.append(str(self))
        return original_iterdir(self)

    with mock.patch.object(Path, "iterdir", _tracking_iterdir):
        code = main(_recover_args(env))
    assert code in {EXIT_OK, EXIT_OUTCOME}
    assert listdir_calls == []


def test_recover_missing_journal_exits_3(env) -> None:
    code = main(_recover_args(env, operation_id="missing-journal-op-001"))
    assert code == EXIT_OUTCOME


def test_recover_verified_journal_exits_0(env, approval_file, capsys: pytest.CaptureFixture[str]) -> None:
    main(_quarantine_args(env, approval_file, execute=True))
    capsys.readouterr()
    with mock.patch.object(Path, "iterdir", lambda self: iter(())):
        code = main(_recover_args(env))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == EXIT_OK
    assert payload["command"] == "recover"
    assert payload["journal_phase"] == PHASE_VERIFIED


@pytest.mark.parametrize("target", FORBIDDEN_CALL_TARGETS)
def test_cli_never_calls_forbidden_modules(target: str, env, approval_file) -> None:
    with mock.patch(target) as forbidden:
        with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
            main(_quarantine_args(env, approval_file, execute=False))
        forbidden.assert_not_called()


def test_cli_output_json_includes_outcome_journal_preview_and_residual(
    env,
    approval_file,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        code = main(_quarantine_args(env, approval_file, execute=False))
    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(captured.out)
    assert payload["command"] == "quarantine"
    assert payload["outcome"] == OUTCOME_DRY_RUN
    assert "journal_path" in payload
    assert "journal_phase" in payload
    assert "residual_unrecovered" in payload
    assert "preview" in payload
    json.dumps(payload)


def test_cli_text_format_is_readable(env, approval_file, capsys: pytest.CaptureFixture[str]) -> None:
    with mock.patch.object(qd_module, "_default_now_utc", return_value=NOW_VALID):
        code = main(_quarantine_args(env, approval_file, extra=["--format", "text"]))
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "### Governed quarantine DELETE" in captured.out
    assert "Dry-run only" in captured.out


def test_governed_delete_main_source_never_imports_forbidden_modules() -> None:
    source = (ROOT / "rag_engine/governed_delete/__main__.py").read_text(encoding="utf-8")
    assert "from rag_engine.cli" not in source
    assert "import rag_engine.cli" not in source
    assert "from rag_engine.reconcile_path" not in source
    assert "import rag_engine.reconcile_path" not in source
    assert "resolve_library_state" not in source
    assert "compute_approval_digest" not in source
    assert "permanent deletion" in source


def test_hermes_skill_contains_operator_boundaries() -> None:
    assert HERMES_SKILL_PATH.is_file(), f"missing Hermes skill at {HERMES_SKILL_PATH}"
    text = HERMES_SKILL_PATH.read_text(encoding="utf-8")
    required = [
        "quarantine-first",
        "never permanent delete",
        "dry-run",
        "--execute",
        "retained copy",
        "never auto-selected",
        "exact operation ID",
        "no bulk delete",
        "no approval creation",
        "no automatic routing",
        "ce-library-manager",
    ]
    missing = [phrase for phrase in required if phrase.lower() not in text.lower()]
    assert not missing, f"Hermes skill missing phrases: {missing}"
