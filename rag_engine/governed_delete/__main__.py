"""User-facing governed quarantine DELETE CLI.

Calls public DELETE executor APIs only. Default is dry-run; filesystem/store mutation
requires explicit ``--execute``. Never creates approvals, routes automatically,
permanently deletes, or invokes the cli adapter.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from rag_engine.governed_delete import (
    OUTCOME_BLOCKED,
    OUTCOME_COMPENSATED_BEFORE_COMMIT,
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    QuarantineDeleteApprovalContext,
    QuarantineDeleteApprovalValidationError,
    QuarantineDeletePreflightError,
    QuarantineDeletePreview,
    QuarantineDeleteRecoveryResult,
    QuarantineDeleteRequest,
    QuarantineDeleteResult,
    collect_quarantine_delete_evidence,
    execute_quarantine_delete,
    recover_quarantine_delete,
    validate_quarantine_delete_approval,
)
from rag_engine.governed_delete.quarantine_journal import (
    PHASE_VERIFIED,
    QuarantineDeleteJournalError,
    journal_path_for_operation,
    read_journal_exact,
    validate_operation_id,
)

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_OUTCOME = 3

FORBIDDEN_FLAGS = frozenset(
    {
        "--delete",
        "--permanent",
        "--purge",
        "--bulk",
        "--approve",
        "--route",
        "--force",
        "--skip-verify",
        "--no-lock",
    }
)

UNSUPPORTED_BEHAVIOR_MSG = (
    "governed DELETE CLI does not support permanent deletion, approval issuance, "
    "automatic routing, or bypass flags; use explicit approved artifacts and "
    "bounded quarantine DELETE only"
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


def _normalize_vector_ids(raw: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            raise ValueError("vector-id values must be non-empty strings")
        if text in seen:
            raise ValueError("duplicate vector-id values are not allowed")
        seen.add(text)
        out.append(text)
    if not out:
        raise ValueError("at least one --vector-id is required")
    return tuple(out)


def _default_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal_info(persist_dir: str, operation_id: str) -> tuple[str | None, str | None]:
    try:
        journal_path = journal_path_for_operation(persist_dir, operation_id, create_root=False)
    except (ValueError, OSError, QuarantineDeleteJournalError):
        return None, None
    if not journal_path.is_file():
        return str(journal_path), None
    try:
        journal = read_journal_exact(persist_dir, operation_id)
    except (ValueError, OSError, QuarantineDeleteJournalError):
        return str(journal_path), None
    phase = str(journal.get("phase") or "") or None
    return str(journal_path), phase


def _preview_payload(preview: QuarantineDeletePreview | None) -> dict[str, Any] | None:
    if preview is None:
        return None
    return {
        "target_relative_path": preview.target_relative_path,
        "retained_relative_path": preview.retained_relative_path,
        "quarantine_relative_path": preview.quarantine_relative_path,
        "document_id": preview.document_id,
        "source_hash": preview.source_hash,
        "target_source_file_id": preview.target_source_file_id,
        "retained_source_file_id": preview.retained_source_file_id,
        "approved_vector_ids": list(preview.approved_vector_ids),
        "operation_id": preview.operation_id,
        "registry_action": preview.registry_action,
    }


def _quarantine_exit_code(result: QuarantineDeleteResult) -> int:
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


def _recover_exit_code(result: QuarantineDeleteRecoveryResult) -> int:
    if result.verified and result.success:
        return EXIT_OK
    if result.recovery_required or not result.success:
        return EXIT_OUTCOME
    return EXIT_OK


def _quarantine_payload(
    result: QuarantineDeleteResult,
    *,
    persist_dir: str,
    execute: bool,
) -> dict[str, Any]:
    journal_path, journal_phase = _journal_info(persist_dir, result.operation_id)
    payload: dict[str, Any] = {
        "command": "quarantine",
        "execute": execute,
        "operation_id": result.operation_id,
        "outcome": result.outcome,
        "success": result.success,
        "dry_run": result.dry_run,
        "compensated": result.compensated,
        "recovery_required": result.recovery_required,
        "registry_committed": result.registry_committed,
        "filesystem_quarantined": result.filesystem_quarantined,
        "tracker_updated": result.tracker_updated,
        "chroma_updated": result.chroma_updated,
        "journal_path": journal_path,
        "journal_phase": journal_phase,
        "residual_unrecovered": list(result.residual_unrecovered),
        "residual_recovery_notes": list(result.residual_unrecovered),
        "error_message": result.error_message,
    }
    preview = _preview_payload(result.preview)
    if preview is not None:
        payload["preview"] = preview
    if result.approval is not None:
        payload["approval_id"] = result.approval.approval_id
    return payload


def _recover_payload(
    result: QuarantineDeleteRecoveryResult,
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


def _print_quarantine_text(payload: dict[str, Any]) -> None:
    print("### Governed quarantine DELETE")
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
    preview = payload.get("preview")
    if preview:
        print("- Preview:")
        print(f"  - Target: {preview['target_relative_path']}")
        print(f"  - Retained: {preview['retained_relative_path']}")
        print(f"  - Quarantine: {preview['quarantine_relative_path']}")
        print(f"  - Registry action: {preview['registry_action']}")
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
        print("Quarantine DELETE verified; journal reached VERIFIED.")


def _print_recover_text(payload: dict[str, Any]) -> None:
    print("### Governed quarantine DELETE recovery")
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


def _build_quarantine_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_engine.governed_delete quarantine",
        description=(
            "Bounded governed quarantine DELETE. Dry-run by default; "
            "pass --execute for filesystem/store mutation. Never permanently deletes."
        ),
    )
    parser.add_argument("--library-root", required=True, help="Explicit absolute library root")
    parser.add_argument("--persist-dir", required=True, help="Explicit absolute RAG persist directory")
    parser.add_argument("--registry-db", required=True, help="Explicit absolute registry sqlite path")
    parser.add_argument("--tracker-path", required=True, help="Explicit absolute tracker JSON path")
    parser.add_argument("--target", required=True, help="Target path relative to library_root")
    parser.add_argument("--retained", required=True, help="Retained path relative to library_root")
    parser.add_argument("--quarantine", required=True, help="Quarantine path relative to library_root")
    parser.add_argument(
        "--approval-file",
        required=True,
        help="Explicit path to approved quarantine-delete approval artifact JSON",
    )
    parser.add_argument(
        "--target-source-file-id",
        required=True,
        help="Explicit target source_file_id locator",
    )
    parser.add_argument(
        "--retained-source-file-id",
        required=True,
        help="Explicit retained source_file_id locator",
    )
    parser.add_argument(
        "--registry-collection",
        required=True,
        help="Explicit registry collection name",
    )
    parser.add_argument(
        "--operation-id",
        required=True,
        help="Explicit bounded quarantine-delete operation identifier",
    )
    parser.add_argument(
        "--vector-id",
        action="append",
        required=True,
        dest="vector_ids",
        metavar="VECTOR_ID",
        help="Approved vector/chunk id (repeatable; duplicates rejected)",
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
        prog="python -m rag_engine.governed_delete recover",
        description=(
            "Recover one bounded quarantine delete by exact operation_id only. "
            "Never scans journal directories."
        ),
    )
    parser.add_argument("--persist-dir", required=True, help="Explicit absolute RAG persist directory")
    parser.add_argument("--operation-id", required=True, help="Exact quarantine-delete operation identifier")
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
    if argv[0] in {"quarantine", "recover"}:
        return argv[0], argv[1:]
    return None, argv


def _cmd_quarantine(argv: list[str], *, output_format: str | None = None) -> int:
    parser = _build_quarantine_parser()
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
        target = _validate_relative_path(args.target, field="target")
        retained = _validate_relative_path(args.retained, field="retained")
        quarantine = _validate_relative_path(args.quarantine, field="quarantine")
        approval_artifact = _load_approval_file(approval_path)
        vector_ids = _normalize_vector_ids(args.vector_ids)
    except ValueError as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    target_sf = str(args.target_source_file_id or "").strip()
    retained_sf = str(args.retained_source_file_id or "").strip()
    if not target_sf or not retained_sf:
        _stderr("target-source-file-id and retained-source-file-id are required")
        return EXIT_INPUT
    if target_sf == retained_sf:
        _stderr("target-source-file-id and retained-source-file-id must differ")
        return EXIT_INPUT

    operation_id = str(args.operation_id or "").strip()
    if not operation_id:
        _stderr("operation-id is required")
        return EXIT_INPUT

    registry_collection = str(args.registry_collection or "").strip()
    if not registry_collection:
        _stderr("registry-collection is required")
        return EXIT_INPUT

    approval_context = QuarantineDeleteApprovalContext(
        registry_db_path=str(registry_db.resolve()),
        library_root=str(library_root.resolve()),
        persist_dir=str(persist_dir.resolve()),
        tracker_path=str(tracker_path.resolve()),
    )

    try:
        preflight_evidence = collect_quarantine_delete_evidence(
            library_root=library_root,
            persist_dir=persist_dir,
            registry_db=registry_db,
            tracker_path=tracker_path,
            target_path=target,
            retained_path=retained,
            quarantine_path=quarantine,
        )
    except (QuarantineDeletePreflightError, ValueError, OSError) as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    try:
        validate_quarantine_delete_approval(
            approval_artifact,
            preflight_evidence.plan,
            target_path=target,
            retained_path=retained,
            quarantine_path=quarantine,
            context=approval_context,
            preflight_evidence=preflight_evidence,
            now_utc=_default_now_utc(),
        )
    except QuarantineDeleteApprovalValidationError as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    request = QuarantineDeleteRequest(
        approval_artifact=approval_artifact,
        preflight_evidence=preflight_evidence,
        approval_context=approval_context,
        target_relative_path=target,
        retained_relative_path=retained,
        quarantine_relative_path=quarantine,
        library_root=str(library_root.resolve()),
        persist_dir=str(persist_dir.resolve()),
        registry_db=str(registry_db.resolve()),
        tracker_path=str(tracker_path.resolve()),
        target_source_file_id=target_sf,
        retained_source_file_id=retained_sf,
        approved_vector_ids=vector_ids,
        registry_collection=registry_collection,
        operation_id=operation_id,
        execute=bool(args.execute),
    )

    result = execute_quarantine_delete(request)
    payload = _quarantine_payload(
        result,
        persist_dir=str(persist_dir.resolve()),
        execute=bool(args.execute),
    )
    if fmt == "json":
        _print_json(payload)
    else:
        _print_quarantine_text(payload)
    return _quarantine_exit_code(result)


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
        validate_operation_id(operation_id)
    except (ValueError, QuarantineDeleteJournalError) as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    result = recover_quarantine_delete(
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
        _stderr("subcommand required: quarantine or recover")
        return EXIT_INPUT

    if command == "quarantine":
        return _cmd_quarantine(rest)
    if command == "recover":
        return _cmd_recover(rest)
    _stderr(f"unsupported subcommand: {command!r}")
    return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
