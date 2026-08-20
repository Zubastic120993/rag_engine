"""Durable single-file quarantine-delete journal (DELETE Phase B).

Journals live only at ``{persist_dir}/governed_quarantine_delete_journal/{operation_id}.json``.
Never list journal directories; never overwrite an existing journal at creation.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

JOURNAL_DIR_NAME = "governed_quarantine_delete_journal"

PHASE_PREPARED = "PREPARED"
PHASE_REGISTRY_PREPARED = "REGISTRY_PREPARED"
PHASE_FILESYSTEM_QUARANTINED = "FILESYSTEM_QUARANTINED"
PHASE_TRACKER_UPDATED = "TRACKER_UPDATED"
PHASE_CHROMA_UPDATED = "CHROMA_UPDATED"
PHASE_REGISTRY_COMMITTED = "REGISTRY_COMMITTED"
PHASE_VERIFIED = "VERIFIED"
PHASE_COMPENSATED = "COMPENSATED"
PHASE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

EXECUTION_PHASES = (
    PHASE_PREPARED,
    PHASE_REGISTRY_PREPARED,
    PHASE_FILESYSTEM_QUARANTINED,
    PHASE_TRACKER_UPDATED,
    PHASE_CHROMA_UPDATED,
    PHASE_REGISTRY_COMMITTED,
    PHASE_VERIFIED,
)

TERMINAL_PHASES = frozenset(
    {PHASE_VERIFIED, PHASE_COMPENSATED, PHASE_RECOVERY_REQUIRED}
)

PRE_COMMIT_PHASES = frozenset(
    {
        PHASE_PREPARED,
        PHASE_REGISTRY_PREPARED,
        PHASE_FILESYSTEM_QUARANTINED,
        PHASE_TRACKER_UPDATED,
        PHASE_CHROMA_UPDATED,
    }
)

_SAFE_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9:._\-]+$")


class QuarantineDeleteJournalError(ValueError):
    """Raised when quarantine-delete journal preconditions fail."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_operation_id(operation_id: str) -> str:
    """Reject path separators, traversal, NUL, and control characters."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise QuarantineDeleteJournalError("operation_id is required")
    op = operation_id.strip()
    if "/" in op or "\\" in op:
        raise QuarantineDeleteJournalError("operation_id must be a safe opaque identifier")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in op):
        raise QuarantineDeleteJournalError("operation_id must not contain control characters")
    if op == ".." or op.startswith("../") or op.endswith("/.."):
        raise QuarantineDeleteJournalError("operation_id must not contain traversal segments")
    if not _SAFE_OPERATION_ID_RE.match(op):
        raise QuarantineDeleteJournalError("operation_id must be a safe opaque identifier")
    return op


def _require_absolute_path(raw: str, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise QuarantineDeleteJournalError(f"{field} must be a non-empty absolute path")
    path = Path(raw)
    if not path.is_absolute():
        raise QuarantineDeleteJournalError(f"{field} must be an absolute path")
    return str(path.resolve())


def _assert_under_root(path: Path, root: Path, *, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise QuarantineDeleteJournalError(
            f"{label} resolves outside expected root {root!s}"
        ) from exc


def validate_persist_dir_path(persist_dir: str | Path) -> str:
    """Validate explicit persist_dir without reading journals or creating directories."""
    persist_entry = Path(persist_dir)
    if not persist_entry.is_absolute():
        raise QuarantineDeleteJournalError("persist_dir must be an absolute path")
    if persist_entry.is_symlink():
        raise QuarantineDeleteJournalError("persist_dir must not be a symlink")
    persist = persist_entry.resolve()
    if not persist.is_dir():
        raise QuarantineDeleteJournalError("persist_dir must exist")
    return str(persist)


def journal_root_dir(persist_dir: str | Path, *, create_root: bool = False) -> Path:
    """Return resolved journal root, rejecting symlink escape outside persist_dir."""
    persist_entry = Path(persist_dir)
    if not persist_entry.is_absolute():
        raise QuarantineDeleteJournalError("persist_dir must be an absolute path")
    if persist_entry.is_symlink():
        raise QuarantineDeleteJournalError("persist_dir must not be a symlink")
    persist = persist_entry.resolve()
    if not persist.is_dir():
        raise QuarantineDeleteJournalError(
            "persist_dir must exist before quarantine-delete journal access"
        )
    root_entry = persist / JOURNAL_DIR_NAME
    if create_root:
        root_entry.mkdir(parents=True, exist_ok=True)
    elif not root_entry.is_dir():
        raise QuarantineDeleteJournalError("quarantine-delete journal directory not found")
    if root_entry.is_symlink():
        raise QuarantineDeleteJournalError("journal root must not be a symlink")
    resolved = root_entry.resolve()
    _assert_under_root(resolved, persist, label="journal root")
    return resolved


def journal_path_for_operation(
    persist_dir: str | Path,
    operation_id: str,
    *,
    create_root: bool = False,
) -> Path:
    """Construct the exact journal path without reading or listing."""
    op = validate_operation_id(operation_id)
    root = journal_root_dir(persist_dir, create_root=create_root)
    candidate = (root / f"{op}.json").resolve()
    _assert_under_root(candidate, root, label="journal path")
    return candidate


def read_journal_exact(persist_dir: str | Path, operation_id: str) -> dict[str, Any]:
    """Read one journal by exact operation_id; never lists directories or creates roots."""
    path = journal_path_for_operation(persist_dir, operation_id, create_root=False)
    if not path.is_file():
        raise QuarantineDeleteJournalError(
            f"quarantine-delete journal not found for operation_id {operation_id!r}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarantineDeleteJournalError(
            f"quarantine-delete journal unreadable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise QuarantineDeleteJournalError(
            "quarantine-delete journal payload must be a JSON object"
        )
    return payload


def _atomic_replace(path: Path, data: str) -> None:
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _serialize_chroma_identity(
    chroma_identity: Mapping[str, Mapping[str, str | None]],
) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for vector_id, identity in chroma_identity.items():
        out[str(vector_id)] = {
            "source_path": identity.get("source_path"),
            "document_id": identity.get("document_id"),
            "source_hash": identity.get("source_hash"),
        }
    return out


def create_prepared_journal(
    *,
    persist_dir: str,
    operation_id: str,
    target_relative_path: str,
    retained_relative_path: str,
    quarantine_relative_path: str,
    expected_sha256: str,
    document_id: str,
    source_hash: str,
    approved_vector_ids: Sequence[str],
    target_source_file_id: str,
    retained_source_file_id: str,
    approval_digest: str,
    tracker_snapshot: Mapping[str, Any],
    chroma_identity_snapshot: Mapping[str, Mapping[str, str | None]],
    initial_registry_state_summary: Mapping[str, Any],
    library_root: str,
    registry_db: str,
    tracker_path: str,
    registry_collection: str,
    created_at: str,
) -> Path:
    """Create immutable-start journal at PREPARED; fail if one already exists."""
    path = journal_path_for_operation(persist_dir, operation_id, create_root=True)
    if path.exists():
        raise QuarantineDeleteJournalError(
            f"quarantine-delete journal already exists for operation_id {operation_id!r}"
        )
    record: dict[str, Any] = {
        "operation_id": validate_operation_id(operation_id),
        "target_relative_path": target_relative_path,
        "retained_relative_path": retained_relative_path,
        "quarantine_relative_path": quarantine_relative_path,
        "expected_sha256": expected_sha256,
        "document_id": document_id,
        "source_hash": source_hash,
        "approved_vector_ids": list(approved_vector_ids),
        "target_source_file_id": target_source_file_id,
        "retained_source_file_id": retained_source_file_id,
        "approval_digest": approval_digest,
        "tracker_snapshot": dict(tracker_snapshot),
        "chroma_identity_snapshot": _serialize_chroma_identity(chroma_identity_snapshot),
        "initial_registry_state_summary": dict(initial_registry_state_summary),
        "library_root": _require_absolute_path(library_root, "library_root"),
        "persist_dir": _require_absolute_path(persist_dir, "persist_dir"),
        "registry_db": _require_absolute_path(registry_db, "registry_db"),
        "tracker_path": _require_absolute_path(tracker_path, "tracker_path"),
        "registry_collection": registry_collection,
        "phase": PHASE_PREPARED,
        "created_at": created_at,
        "updated_at": created_at,
        "residual_recovery_notes": [],
    }
    data = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd: int | None = None
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    except FileExistsError as exc:
        raise QuarantineDeleteJournalError(
            f"quarantine-delete journal already exists for operation_id {operation_id!r}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return path


def advance_journal_phase(
    journal_path: Path,
    phase: str,
    *,
    residual_recovery_notes: Sequence[str] | None = None,
    updated_at: str | None = None,
) -> None:
    """Write the next phase only after the corresponding action succeeded."""
    if phase not in EXECUTION_PHASES and phase not in TERMINAL_PHASES:
        raise QuarantineDeleteJournalError(f"invalid journal phase: {phase!r}")
    try:
        current = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarantineDeleteJournalError(
            f"quarantine-delete journal unreadable: {exc}"
        ) from exc
    if not isinstance(current, dict):
        raise QuarantineDeleteJournalError(
            "quarantine-delete journal payload must be a JSON object"
        )
    current_phase = current.get("phase")
    if current_phase in TERMINAL_PHASES:
        raise QuarantineDeleteJournalError(
            f"terminal journal phase {current_phase!r} is immutable"
        )
    current["phase"] = phase
    current["updated_at"] = updated_at or _utc_now()
    if residual_recovery_notes is not None:
        current["residual_recovery_notes"] = list(residual_recovery_notes)
    data = json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_replace(journal_path, data)


def validate_journal_bindings(
    journal: Mapping[str, Any],
    *,
    persist_dir: str,
    library_root: str,
    registry_db: str,
    tracker_path: str,
) -> None:
    """Ensure caller paths match journal-bound absolute paths."""
    expected = {
        "persist_dir": _require_absolute_path(persist_dir, "persist_dir"),
        "library_root": _require_absolute_path(library_root, "library_root"),
        "registry_db": _require_absolute_path(registry_db, "registry_db"),
        "tracker_path": _require_absolute_path(tracker_path, "tracker_path"),
    }
    for field, value in expected.items():
        bound = journal.get(field)
        if not isinstance(bound, str) or Path(bound).resolve() != Path(value).resolve():
            raise QuarantineDeleteJournalError(f"journal binding mismatch for {field}")
