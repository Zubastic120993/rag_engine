"""User-facing governed single-file MOVE CLI.

Calls public MOVE executor APIs only. Default is dry-run; filesystem/store mutation
requires explicit ``--execute``. Never creates approvals, routes automatically, or
invokes DELETE/bulk paths or the cli adapter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from rag_engine.governed_move import (
    OUTCOME_BLOCKED,
    OUTCOME_COMPENSATED_BEFORE_COMMIT,
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    SingleFileMoveRecoveryResult,
    SingleFileMoveRequest,
    SingleFileMoveResult,
    execute_single_file_move,
    recover_single_file_move,
)
from rag_engine.governed_move.move_journal import (
    PHASE_VERIFIED,
    journal_path_for_operation,
    read_journal_exact,
)
from rag_engine.library_state.move_approval import MoveApprovalContext
from rag_engine.library_state.move_preflight import (
    PreMoveEvidenceError,
    collect_pre_move_evidence,
)

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_OUTCOME = 3

FORBIDDEN_FLAGS = frozenset(
    {
        "--delete",
        "--bulk",
        "--approve",
        "--route",
        "--force",
        "--skip-verify",
        "--no-lock",
    }
)

UNSUPPORTED_BEHAVIOR_MSG = (
    "governed MOVE CLI does not support approval creation, automatic routing, or DELETE; "
    "use explicit approved artifacts and bounded single-file MOVE only"
)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _forbidden_flag(argv: Sequence[str]) -> str | None:
    for token in argv:
        base = token.split("=", 1)[0]
        if base in FORBIDDEN_FLAGS:
            return base
    return None


def _require_absolute(path_value: str, name: str) -> Path:
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError(f"{name} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {raw!r}")
    return path


def _validate_relative_path(raw: str, *, field: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise ValueError(f"{field} is required")
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError(f"{field} must be relative to library_root, not absolute: {raw!r}")
    parts = value.split("/")
    if ".." in parts:
        raise ValueError(f"{field} must not contain traversal segments: {raw!r}")
    return value


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _load_approval_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"approval file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"approval file unreadable: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"approval file is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("approval file must contain a JSON object")
    return payload


def _journal_info(persist_dir: str, operation_id: str) -> tuple[str | None, str | None]:
    """Return (journal_path, phase) when a journal file exists."""
    try:
        journal_path = journal_path_for_operation(persist_dir, operation_id, create_root=False)
    except (ValueError, OSError):
        return None, None
    if not journal_path.is_file():
        return str(journal_path), None
    try:
        journal = read_journal_exact(persist_dir, operation_id)
    except (ValueError, OSError):
        return str(journal_path), None
    phase = str(journal.get("phase") or "") or None
    return str(journal_path), phase


def _move_exit_code(result: SingleFileMoveResult) -> int:
    if result.outcome in {
        OUTCOME_BLOCKED,
        OUTCOME_COMPENSATED_BEFORE_COMMIT,
        OUTCOME_RECOVERY_REQUIRED,
    }:
        return EXIT_OUTCOME
    if result.outcome in {OUTCOME_DRY_RUN, OUTCOME_SUCCESS} and result.success:
        return EXIT_OK
    if result.success:
        return EXIT_OK
    return EXIT_OUTCOME


def _recover_exit_code(result: SingleFileMoveRecoveryResult) -> int:
    if result.verified and result.success:
        return EXIT_OK
    if result.recovery_required or not result.success:
        return EXIT_OUTCOME
    return EXIT_OK


def _move_payload(
    result: SingleFileMoveResult,
    *,
    persist_dir: str,
    execute: bool,
) -> dict[str, Any]:
    journal_path, journal_phase = _journal_info(persist_dir, result.operation_id)
    payload: dict[str, Any] = {
        "command": "move",
        "execute": execute,
        "operation_id": result.operation_id,
        "outcome": result.outcome,
        "success": result.success,
        "dry_run": result.dry_run,
        "compensated": result.compensated,
        "recovery_required": result.recovery_required,
        "registry_committed": result.registry_committed,
        "filesystem_moved": result.filesystem_moved,
        "tracker_updated": result.tracker_updated,
        "chroma_updated": result.chroma_updated,
        "journal_path": journal_path,
        "journal_phase": journal_phase,
        "residual_unrecovered": list(result.residual_unrecovered),
        "residual_recovery_notes": list(result.residual_unrecovered),
        "error_message": result.error_message,
    }
    if result.approval is not None:
        payload["approval_id"] = result.approval.approval_id
    return payload


def _recover_payload(
    result: SingleFileMoveRecoveryResult,
    *,
    persist_dir: str,
) -> dict[str, Any]:
    journal_path, _ = _journal_info(persist_dir, result.operation_id)
    return {
        "command": "recover",
        "operation_id": result.operation_id,
        "success": result.success,
        "verified": result.verified,
        "compensated": result.compensated,
        "recovery_required": result.recovery_required,
        "journal_path": journal_path,
        "journal_phase": result.phase or None,
        "residual_recovery_notes": list(result.residual_recovery_notes),
        "error_message": result.error_message,
    }


def _print_move_text(payload: dict[str, Any]) -> None:
    print("### Governed MOVE")
    print(f"- Execute: {payload['execute']}")
    print(f"- Operation ID: {payload['operation_id']}")
    print(f"- Outcome: {payload['outcome']}")
    print(f"- Success: {payload['success']}")
    if payload.get("approval_id"):
        print(f"- Approval ID: {payload['approval_id']}")
    if payload.get("journal_path"):
        print(f"- Journal path: {payload['journal_path']}")
    if payload.get("journal_phase"):
        print(f"- Journal phase: {payload['journal_phase']}")
    residual = payload.get("residual_unrecovered") or payload.get("residual_recovery_notes") or []
    if residual:
        print("- Residual recovery notes:")
        for note in residual:
            print(f"  - {note}")
    if payload.get("error_message"):
        print(f"- Error: {payload['error_message']}")
    if payload["outcome"] == OUTCOME_DRY_RUN and payload["success"]:
        print()
        print("Dry-run only; no lock, journal, or store mutation occurred.")
    elif payload.get("journal_phase") == PHASE_VERIFIED:
        print()
        print("MOVE verified; journal reached VERIFIED.")


def _print_recover_text(payload: dict[str, Any]) -> None:
    print("### Governed MOVE recovery")
    print(f"- Operation ID: {payload['operation_id']}")
    print(f"- Success: {payload['success']}")
    print(f"- Verified: {payload['verified']}")
    print(f"- Compensated: {payload['compensated']}")
    print(f"- Recovery required: {payload['recovery_required']}")
    if payload.get("journal_path"):
        print(f"- Journal path: {payload['journal_path']}")
    if payload.get("journal_phase"):
        print(f"- Journal phase: {payload['journal_phase']}")
    notes = payload.get("residual_recovery_notes") or []
    if notes:
        print("- Residual recovery notes:")
        for note in notes:
            print(f"  - {note}")
    if payload.get("error_message"):
        print(f"- Error: {payload['error_message']}")


def _build_move_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_engine.governed_move move",
        description=(
            "Bounded governed single-file MOVE. Dry-run by default; "
            "pass --execute for filesystem/store mutation."
        ),
    )
    parser.add_argument("--library-root", required=True, help="Explicit absolute library root")
    parser.add_argument("--persist-dir", required=True, help="Explicit absolute RAG persist directory")
    parser.add_argument("--registry-db", required=True, help="Explicit absolute registry sqlite path")
    parser.add_argument("--tracker-path", required=True, help="Explicit absolute tracker JSON path")
    parser.add_argument(
        "--source",
        required=True,
        help="Source path relative to library_root",
    )
    parser.add_argument(
        "--destination",
        required=True,
        help="Destination path relative to library_root",
    )
    parser.add_argument(
        "--approval-file",
        required=True,
        help="Explicit path to approved MOVE approval artifact JSON",
    )
    parser.add_argument(
        "--old-source-file-id",
        required=True,
        help="Explicit old source_file_id locator",
    )
    parser.add_argument(
        "--new-source-file-id",
        required=True,
        help="Explicit placeholder/future destination source_file_id locator",
    )
    parser.add_argument(
        "--registry-collection",
        required=True,
        help="Explicit registry collection name",
    )
    parser.add_argument(
        "--operation-id",
        required=True,
        help="Explicit bounded MOVE operation identifier",
    )
    parser.add_argument(
        "--vector-id",
        action="append",
        required=True,
        dest="vector_ids",
        metavar="VECTOR_ID",
        help="Approved vector/chunk id (repeatable)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform filesystem/store mutation (default is dry-run)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    return parser


def _build_recover_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_engine.governed_move recover",
        description=(
            "Recover one bounded single-file MOVE by exact operation_id only. "
            "Never scans journal directories."
        ),
    )
    parser.add_argument("--persist-dir", required=True, help="Explicit absolute RAG persist directory")
    parser.add_argument("--operation-id", required=True, help="Exact MOVE operation identifier")
    parser.add_argument("--library-root", required=True, help="Explicit absolute library root")
    parser.add_argument("--registry-db", required=True, help="Explicit absolute registry sqlite path")
    parser.add_argument("--tracker-path", required=True, help="Explicit absolute tracker JSON path")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    return parser


def _parse_subcommand(argv: list[str]) -> tuple[str | None, list[str]]:
    if not argv:
        return None, argv
    if argv[0] in {"move", "recover"}:
        return argv[0], argv[1:]
    return None, argv


def _cmd_move(argv: list[str], *, output_format: str | None = None) -> int:
    parser = _build_move_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return EXIT_OK
        return EXIT_INPUT

    fmt = output_format or args.format

    try:
        library_root = _require_absolute(args.library_root, "library_root")
        persist_dir = _require_absolute(args.persist_dir, "persist_dir")
        registry_db = _require_absolute(args.registry_db, "registry_db")
        tracker_path = _require_absolute(args.tracker_path, "tracker_path")
        approval_path = _require_absolute(args.approval_file, "approval_file")
        source = _validate_relative_path(args.source, field="source")
        destination = _validate_relative_path(args.destination, field="destination")
        approval_artifact = _load_approval_file(approval_path)
    except ValueError as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    old_sf = str(args.old_source_file_id or "").strip()
    new_sf = str(args.new_source_file_id or "").strip()
    if not old_sf or not new_sf:
        _stderr("old-source-file-id and new-source-file-id are required")
        return EXIT_INPUT
    if old_sf == new_sf:
        _stderr("old-source-file-id and new-source-file-id must differ")
        return EXIT_INPUT

    vector_ids = tuple(str(v).strip() for v in args.vector_ids if str(v).strip())
    if not vector_ids:
        _stderr("at least one --vector-id is required")
        return EXIT_INPUT

    operation_id = str(args.operation_id or "").strip()
    if not operation_id:
        _stderr("operation-id is required")
        return EXIT_INPUT

    registry_collection = str(args.registry_collection or "").strip()
    if not registry_collection:
        _stderr("registry-collection is required")
        return EXIT_INPUT

    approval_context = MoveApprovalContext(
        affected_source_file_ids=(old_sf, new_sf),
        registry_db_path=str(registry_db.resolve()),
        library_root=str(library_root.resolve()),
        persist_dir=str(persist_dir.resolve()),
    )

    try:
        pre_move_evidence = collect_pre_move_evidence(
            library_root=library_root,
            persist_dir=persist_dir,
            registry_db=registry_db,
            tracker_path=tracker_path,
            source_path=source,
            destination_path=destination,
            operation_id=operation_id,
        )
    except (PreMoveEvidenceError, ValueError, OSError) as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    request = SingleFileMoveRequest(
        approval_artifact=approval_artifact,
        pre_move_evidence=pre_move_evidence,
        approval_context=approval_context,
        source_relative_path=source,
        destination_relative_path=destination,
        library_root=str(library_root.resolve()),
        persist_dir=str(persist_dir.resolve()),
        registry_db=str(registry_db.resolve()),
        tracker_path=str(tracker_path.resolve()),
        old_source_file_id=old_sf,
        approved_vector_ids=vector_ids,
        registry_collection=registry_collection,
        operation_id=operation_id,
        execute=bool(args.execute),
    )

    result = execute_single_file_move(request)
    payload = _move_payload(
        result,
        persist_dir=str(persist_dir.resolve()),
        execute=bool(args.execute),
    )
    if fmt == "json":
        _print_json(payload)
    else:
        _print_move_text(payload)
    return _move_exit_code(result)


def _cmd_recover(argv: list[str], *, output_format: str | None = None) -> int:
    parser = _build_recover_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return EXIT_OK
        return EXIT_INPUT

    fmt = output_format or args.format

    try:
        persist_dir = _require_absolute(args.persist_dir, "persist_dir")
        library_root = _require_absolute(args.library_root, "library_root")
        registry_db = _require_absolute(args.registry_db, "registry_db")
        tracker_path = _require_absolute(args.tracker_path, "tracker_path")
        operation_id = str(args.operation_id or "").strip()
        if not operation_id:
            raise ValueError("operation-id is required")
    except ValueError as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    result = recover_single_file_move(
        persist_dir=str(persist_dir.resolve()),
        operation_id=operation_id,
        library_root=str(library_root.resolve()),
        registry_db=str(registry_db.resolve()),
        tracker_path=str(tracker_path.resolve()),
    )
    payload = _recover_payload(result, persist_dir=str(persist_dir.resolve()))
    if fmt == "json":
        _print_json(payload)
    else:
        _print_recover_text(payload)
    return _recover_exit_code(result)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forbidden = _forbidden_flag(argv)
    if forbidden is not None:
        _stderr(f"{UNSUPPORTED_BEHAVIOR_MSG} (flag {forbidden!r} rejected)")
        return EXIT_INPUT

    command, rest = _parse_subcommand(argv)
    if command is None:
        _stderr("subcommand required: move or recover")
        return EXIT_INPUT

    if command == "move":
        return _cmd_move(rest)
    if command == "recover":
        return _cmd_recover(rest)
    _stderr(f"unsupported subcommand: {command!r}")
    return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
