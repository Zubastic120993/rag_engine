"""Synthetic LC6 production-selection promotion controls.

This module is intentionally package-local and synthetic-fixture oriented. It is
not wired to the real Hermes environment and does not restart/reload Hermes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

KEY = b"RAG_DB_PATH="
FORBIDDEN_EXACT_NAMES = {".rag_db", ".rag_db_generations"}
UNRESOLVED_JOURNAL_STATES = {"PREPARED", "COMMITTED", "RECOVERY_REQUIRED", "RESIDUAL"}
FINALIZED_JOURNAL_STATES = {"ROLLED_BACK", "FINALIZED_ARCHIVED"}


class EnvSelectionError(RuntimeError):
    """The selection file is unsafe or ambiguous."""


class RollbackError(RuntimeError):
    """Rollback cannot be performed safely from the supplied journal."""


class CertifiedBaselineError(RuntimeError):
    """Versioned certified baseline evidence is missing or has drifted."""


class JournalPathError(EnvSelectionError):
    """The governed journal path is unsafe for synthetic validation."""


CERTIFIED_BASELINE_SCHEMA = "lc6-versioned-evolved-pre-repair-baseline-certification-v1"
CERTIFIED_BASELINE_SHA256 = "09b69ba17c559e0ee535f4b724ed7482a71578608e7699f2eff260e21ef86b70"
CERTIFIED_BASELINE_VERSION = "LC6-EVOLVED-PRE-REPAIR-CURRENT-20260919T180213Z-CERT-V1"
CERTIFIED_BASELINE_RESULT = "CERTIFIED"
CERTIFIED_ACTIVE_GENERATION = "/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604"
CERTIFIED_SELECTION_MECHANISM = "/Users/vladymyrzub/.hermes/.env RAG_DB_PATH assignment"
CERTIFIED_SOURCE_CONTENT_ROWS = 11
CERTIFIED_SOURCE_CONTENT_CHECKSUM = "004737754f216a38494eeca739590e2535560ed21ed0159580bed48e43ed56e3"
CERTIFIED_PROVENANCE_ROWS = 20
CERTIFIED_PROVENANCE_CHECKSUM = "b98465e70b9a8226cca41ab40882e15463cd4591ede67ccdb16aa8f688f8d9df"
CERTIFIED_SEMANTIC_COUNTS = {
    "collection_names": ["langchain"],
    "tracker_entries": 1761,
    "tracker_paths": 1849,
    "tracker_chunk_ids": 126076,
    "sqlite_embeddings": 126076,
    "sqlite_embedding_metadata": 1386836,
    "sqlite_embedding_fulltext_search": 126076,
    "tracker_sqlite_exact_id_set_equal": True,
    "duplicate_chunk_id_count": 0,
}
CERTIFIED_CLEAR_STATE_FILES = {
    "chroma.sqlite3-wal",
    "chroma.sqlite3-shm",
    "ingest.lock",
    ".ingest.lock",
    "write.lock",
    ".rag_state",
}


@dataclass(frozen=True)
class EnvSelection:
    env_file: Path
    line_number: int
    value: str
    original_bytes: bytes
    line_start: int
    value_start: int
    value_end: int
    line_ending: bytes
    mode: int
    uid: int
    gid: int
    sha256: str


@dataclass(frozen=True)
class SwitchResult:
    env_file: str
    journal_file: str
    run_id: str
    old_value: str
    new_value: str
    state: str


@dataclass(frozen=True)
class RollbackResult:
    env_file: str
    journal_file: str
    run_id: str
    restored_value: str
    state: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at_ns: int
    command: str
    rag_db_path: str


@dataclass(frozen=True)
class RestartResult:
    old_pid: int
    new_pid: int
    old_rag_db_path: str
    new_rag_db_path: str
    interruption_ms: int
    overlap_ambiguous: bool


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split_line_ending(line: bytes) -> tuple[bytes, bytes]:
    if line.endswith(b"\r\n"):
        return line[:-2], b"\r\n"
    if line.endswith(b"\n"):
        return line[:-1], b"\n"
    if line.endswith(b"\r"):
        return line[:-1], b"\r"
    return line, b""


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _prepare_governed_journal_parent(journal: Path, env_file: Path) -> Path:
    """Create and fsync a safe synthetic journal parent before lock creation."""
    env_root = env_file.parent.resolve(strict=True)
    parent = journal.parent
    if parent.exists() and parent.is_symlink():
        raise JournalPathError(f"journal parent is symlinked: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise JournalPathError(f"journal parent is symlinked: {parent}")
    resolved_parent = parent.resolve(strict=True)
    if not _is_relative_to(resolved_parent, env_root):
        raise JournalPathError(
            f"journal parent is outside synthetic fixture: parent={resolved_parent} fixture={env_root}"
        )
    protected = {Path('/'), Path('/Users'), Path('/Users/vladymyrzub'), Path('/Users/vladymyrzub/.hermes')}
    if resolved_parent in protected:
        raise JournalPathError(f"journal parent is protected: {resolved_parent}")
    _fsync_dir(resolved_parent)
    _fsync_dir(resolved_parent.parent)
    return resolved_parent


def _write_json_durable(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _path_metadata(path: Path) -> dict:
    st = path.stat()
    return {
        "mode": st.st_mode & 0o777,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "inode": st.st_ino,
    }


def _metadata_diff(expected: dict, observed: dict) -> dict:
    keys = ("mode", "uid", "gid", "size", "mtime_ns", "inode")
    return {
        key: {"expected": expected.get(key), "observed": observed.get(key)}
        for key in keys
        if expected.get(key) != observed.get(key)
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CertifiedBaselineError(message)


def _inventory(payload: dict, key: str) -> dict:
    inventories = payload.get("inventories") or {}
    value = inventories.get(key)
    _require(isinstance(value, dict), f"missing {key} inventory")
    return value


def _require_content_boundary(text: str) -> None:
    _require("excluding certified_append_journal/**" in text, "source content boundary does not exclude certified_append_journal/**")
    _require("transient" in text and "lock" in text, "source content boundary does not exclude transient state files")


def _require_provenance_boundary(text: str) -> None:
    _require("certified_append_journal/**" in text, "provenance boundary does not bind certified_append_journal/**")


def validate_versioned_baseline_artifact(payload: dict, complete_file_sha256: str) -> dict:
    """Validate the certified evolved pre-repair baseline contract fail-closed."""
    _require(complete_file_sha256 == CERTIFIED_BASELINE_SHA256, "certification hash mismatch")
    _require(payload.get("schema") == CERTIFIED_BASELINE_SCHEMA, "baseline schema mismatch")
    _require(payload.get("certification_result") == CERTIFIED_BASELINE_RESULT, "baseline certification status mismatch")
    identity = payload.get("baseline_identity") or {}
    _require(identity.get("version") == CERTIFIED_BASELINE_VERSION, "baseline identity/version mismatch")
    _require(identity.get("active_generation_path") == CERTIFIED_ACTIVE_GENERATION, "baseline active production path mismatch")
    _require(identity.get("selection_mechanism") == CERTIFIED_SELECTION_MECHANISM, "baseline selection mechanism mismatch")

    disclosure = payload.get("historical_disclosure") or {}
    _require(disclosure.get("old_append_continuity_chain_incomplete") is True, "historical disclosure missing append-chain gap")
    _require(
        disclosure.get("rolled_back_append_not_retrospectively_proven_clean") is True,
        "historical disclosure missing rolled-back append limitation",
    )
    scope = str(disclosure.get("certification_scope") or "")
    _require("independent current-state certification" in scope and "not repair" in scope, "historical disclosure scope is not explicit")

    content = _inventory(payload, "source_database_content")
    _require_content_boundary(str(content.get("boundary") or ""))
    _require(content.get("row_count") == CERTIFIED_SOURCE_CONTENT_ROWS, "source content row count mismatch")
    _require(content.get("checksum_sha256") == CERTIFIED_SOURCE_CONTENT_CHECKSUM, "source content checksum mismatch")
    for row in content.get("rows") or []:
        path = str(row.get("path") or "")
        _require(not path.startswith("certified_append_journal/"), "source content inventory includes provenance file")
        _require(path not in CERTIFIED_CLEAR_STATE_FILES, "source content inventory includes transient state file")

    provenance = _inventory(payload, "certified_append_journal_provenance")
    _require_provenance_boundary(str(provenance.get("boundary") or ""))
    _require(provenance.get("row_count") == CERTIFIED_PROVENANCE_ROWS, "provenance row count mismatch")
    _require(provenance.get("checksum_sha256") == CERTIFIED_PROVENANCE_CHECKSUM, "provenance checksum mismatch")
    for row in provenance.get("rows") or []:
        path = str(row.get("path") or "")
        _require(path.startswith("certified_append_journal/"), "provenance inventory contains non-journal file")

    semantic = payload.get("semantic_state") or {}
    for key, value in CERTIFIED_SEMANTIC_COUNTS.items():
        _require(semantic.get(key) == value, f"semantic baseline mismatch for {key}")
    locks = semantic.get("wal_shm_lock_state") or {}
    for name in CERTIFIED_CLEAR_STATE_FILES:
        _require(locks.get(name) is False, f"WAL/SHM/lock baseline is unresolved for {name}")
    _require(not semantic.get("active_writer_candidates"), "writer baseline is not clear")

    orphan = payload.get("authoritative_55_missing_orphan_binding") or {}
    totals = orphan.get("set_comparison_totals") or {}
    _require(orphan.get("all_55_match_authoritative_pre_repair_lc6_orphan_set") is True, "55-orphan binding not certified")
    _require(totals.get("exact_matches") == 55, "55-orphan binding exact match count mismatch")
    _require(totals.get("authoritative_only") == 0 and totals.get("current_only") == 0, "55-orphan binding has path drift")
    return {
        "baseline_version": CERTIFIED_BASELINE_VERSION,
        "active_generation_path": CERTIFIED_ACTIVE_GENERATION,
        "selection_mechanism": CERTIFIED_SELECTION_MECHANISM,
        "source_content_rows": CERTIFIED_SOURCE_CONTENT_ROWS,
        "source_content_checksum": CERTIFIED_SOURCE_CONTENT_CHECKSUM,
        "provenance_rows": CERTIFIED_PROVENANCE_ROWS,
        "provenance_checksum": CERTIFIED_PROVENANCE_CHECKSUM,
        "semantic_state": dict(CERTIFIED_SEMANTIC_COUNTS),
    }


def verify_current_state_matches_certified_baseline(certified: dict, current: dict) -> dict:
    """Compare current read-only evidence with the certified baseline boundary."""
    _require(current.get("certification_sha256") == CERTIFIED_BASELINE_SHA256, "certification hash mismatch in current-state check")
    _require(current.get("active_generation_path") == certified.get("active_generation_path"), "selection active generation drift")
    _require(current.get("selection_mechanism") == certified.get("selection_mechanism"), "selection mechanism drift")

    content = current.get("source_content") or {}
    _require(not content.get("unknown_extra_files"), "unknown extra content file detected")
    _require(content.get("row_count") == certified.get("source_content_rows"), "source content row count drift")
    _require(content.get("checksum_sha256") == certified.get("source_content_checksum"), "source content checksum drift")

    provenance = current.get("provenance") or {}
    _require(not provenance.get("unknown_extra_files"), "unknown extra provenance file detected")
    _require(provenance.get("row_count") == certified.get("provenance_rows"), "provenance row count drift")
    _require(provenance.get("checksum_sha256") == certified.get("provenance_checksum"), "provenance checksum drift")

    semantic = current.get("semantic_state") or {}
    for key, expected in CERTIFIED_SEMANTIC_COUNTS.items():
        _require(semantic.get(key) == expected, f"semantic current-state drift for {key}")
    locks = semantic.get("wal_shm_lock_state") or {}
    for name in CERTIFIED_CLEAR_STATE_FILES:
        _require(locks.get(name) is False, f"WAL/SHM/lock state unresolved for {name}")
    _require(not semantic.get("active_writer_candidates"), "writer state is not clear")

    orphan = current.get("orphan_binding") or {}
    totals = orphan.get("set_comparison_totals") or {}
    _require(orphan.get("all_55_match_authoritative_pre_repair_lc6_orphan_set") is True, "55-orphan current-state binding missing")
    _require(totals.get("exact_matches") == 55, "55-orphan current-state exact match count mismatch")
    _require(totals.get("authoritative_only") == 0 and totals.get("current_only") == 0, "55-orphan current-state drift")
    return {"status": "ok", "baseline_version": certified.get("baseline_version")}


def _journal_blocks_new_switch(journal_file: Path, env_file: Path) -> tuple[bool, str | None]:
    if not journal_file.exists():
        return False, None
    try:
        payload = json.loads(journal_file.read_text())
    except Exception:
        return True, "unreadable existing journal"
    if payload.get("env_file") != str(env_file):
        return False, None
    state = payload.get("state")
    if state in FINALIZED_JOURNAL_STATES:
        return False, None
    if state in UNRESOLVED_JOURNAL_STATES or state not in FINALIZED_JOURNAL_STATES:
        return True, f"existing {state!r} journal is unresolved"
    return False, None


def validate_generation_target(value: str | Path) -> Path:
    """Validate a generation target path without opening Chroma."""
    raw = str(value)
    if not raw or raw.strip() != raw:
        raise EnvSelectionError("target value is empty or whitespace-padded")
    if raw.startswith(("'", '"')) or raw.endswith(("'", '"')):
        raise EnvSelectionError("quoted RAG_DB_PATH values are ambiguous")
    p = Path(raw)
    if not p.is_absolute():
        raise EnvSelectionError("RAG_DB_PATH target must be absolute")
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EnvSelectionError(f"RAG_DB_PATH target does not resolve: {p}") from exc
    if resolved.name in FORBIDDEN_EXACT_NAMES:
        raise EnvSelectionError("RAG_DB_PATH target is a forbidden root/legacy path")
    if resolved.parent.name != ".rag_db_generations":
        raise EnvSelectionError("RAG_DB_PATH target must be one generation below .rag_db_generations")
    if not resolved.is_dir():
        raise EnvSelectionError("RAG_DB_PATH target must be a directory")
    return resolved


def validate_env_selection(env_file: str | Path) -> EnvSelection:
    path = Path(env_file)
    if not path.is_file():
        raise EnvSelectionError(f"selection file not found: {path}")
    data = path.read_bytes()
    st = path.stat()
    matches: list[tuple[int, int, int, bytes]] = []
    pos = 0
    for n, raw_line in enumerate(data.splitlines(keepends=True), 1):
        body, ending = _split_line_ending(raw_line)
        if body.startswith(KEY):
            if body.count(b"=") < 1 or b"\x00" in body:
                raise EnvSelectionError("malformed RAG_DB_PATH assignment")
            value_b = body[len(KEY):]
            if not value_b:
                raise EnvSelectionError("empty RAG_DB_PATH assignment")
            matches.append((n, pos + len(KEY), pos + len(body), ending))
        elif body.strip().startswith(b"RAG_DB_PATH") and not body.lstrip().startswith(b"#"):
            raise EnvSelectionError("malformed RAG_DB_PATH assignment")
        pos += len(raw_line)
    if len(matches) != 1:
        raise EnvSelectionError(f"expected exactly one active RAG_DB_PATH assignment, found {len(matches)}")
    line_no, value_start, value_end, ending = matches[0]
    raw_value = data[value_start:value_end].decode("utf-8", "strict")
    validate_generation_target(raw_value)
    return EnvSelection(
        env_file=path,
        line_number=line_no,
        value=raw_value,
        original_bytes=data,
        line_start=value_start - len(KEY),
        value_start=value_start,
        value_end=value_end,
        line_ending=ending,
        mode=st.st_mode & 0o777,
        uid=st.st_uid,
        gid=st.st_gid,
        sha256=_sha256(data),
    )


def _replace_selection_bytes(selection: EnvSelection, new_value: str) -> bytes:
    new_b = new_value.encode("utf-8")
    return selection.original_bytes[: selection.value_start] + new_b + selection.original_bytes[selection.value_end :]


def _atomic_replace_preserve_metadata(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        try:
            os.chown(tmp_path, uid, gid)
        except PermissionError:
            cur = tmp_path.stat()
            if cur.st_uid != uid or cur.st_gid != gid:
                raise
        _fsync_file(tmp_path)
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _base_journal(selection: EnvSelection, journal_file: Path, run_id: str, new_value: str) -> dict:
    return {
        "schema": "lc6-promotion-selection-journal-v1",
        "state": "PREPARED",
        "run_id": run_id,
        "env_file": str(selection.env_file),
        "journal_file": str(journal_file),
        "line_number": selection.line_number,
        "old_value": selection.value,
        "new_value": new_value,
        "before_sha256": selection.sha256,
        "prepared_at_ns": time.time_ns(),
        "env_file_metadata": _path_metadata(selection.env_file),
        "lock_note": "Advisory lock serializes cooperating synthetic switch operations only; ungoverned editors are controlled by mandatory final pre-replace comparison.",
    }


def _record_recovery(journal: Path, prepared: dict, reason: str, observed: dict) -> None:
    recovery = dict(prepared)
    recovery.update(
        {
            "state": "RECOVERY_REQUIRED",
            "recovery_reason": reason,
            "observed_difference": observed,
            "manual_recovery_action": "Review synthetic .env and journal; do not retry switch until the previous operation is finalized or archived.",
            "recovery_required_at_ns": time.time_ns(),
        }
    )
    _write_json_durable(journal, recovery)


def _assert_env_unchanged_before_replace(path: Path, selection: EnvSelection, prepared: dict, journal: Path) -> None:
    current_bytes = path.read_bytes()
    current_meta = _path_metadata(path)
    meta_diff = _metadata_diff(prepared["env_file_metadata"], current_meta)
    if current_bytes != selection.original_bytes or meta_diff:
        observed = {
            "bytes_changed": current_bytes != selection.original_bytes,
            "expected_sha256": selection.sha256,
            "observed_sha256": _sha256(current_bytes),
            "metadata_diff": meta_diff,
        }
        _record_recovery(journal, prepared, "concurrent-env-modification-before-atomic-replace", observed)
        raise EnvSelectionError("concurrent synthetic .env modification detected before atomic replace")


def atomic_switch(
    env_file: str | Path,
    target_value: str,
    journal_file: str | Path,
    run_id: str,
    generation_validator: Callable[[str], Path] = validate_generation_target,
    *,
    fail_at: str | None = None,
) -> SwitchResult:
    if fail_at == "before_journal":
        raise RuntimeError("injected failure before journal")
    path = Path(env_file)
    journal = Path(journal_file)
    _prepare_governed_journal_parent(journal, path)
    blocks, reason = _journal_blocks_new_switch(journal, path)
    if blocks:
        raise EnvSelectionError(
            f"existing committed journal or unresolved journal blocks new switch: {reason}; "
            "finalize/archive or rollback the previous operation first"
        )
    selection = validate_env_selection(path)
    target = generation_validator(target_value)
    new_value = str(target)
    replacement = _replace_selection_bytes(selection, new_value)
    prepared = _base_journal(selection, journal, run_id, new_value)
    lock_path = journal.with_suffix(journal.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    tmp_path: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _assert_env_unchanged_before_replace(path, selection, prepared, journal)
        _write_json_durable(journal, prepared)
        if fail_at == "after_prepared_journal":
            raise RuntimeError("injected failure after prepared journal")
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(replacement)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, selection.mode)
        try:
            os.chown(tmp_path, selection.uid, selection.gid)
        except PermissionError:
            cur = tmp_path.stat()
            if cur.st_uid != selection.uid or cur.st_gid != selection.gid:
                raise
        _fsync_file(tmp_path)
        if fail_at == "after_temp_write":
            raise RuntimeError("injected failure after temporary-file write")
        _assert_env_unchanged_before_replace(path, selection, prepared, journal)
        if fail_at == "before_atomic_replace":
            raise RuntimeError("injected failure before atomic replace")
        os.replace(tmp_path, path)
        tmp_path = None
        if fail_at == "after_atomic_replace":
            residual = dict(prepared)
            residual.update({"state": "RESIDUAL", "residual_reason": fail_at, "residual_at_ns": time.time_ns()})
            _write_json_durable(journal, residual)
            raise RuntimeError("injected failure after atomic replace")
        if fail_at == "during_parent_fsync":
            residual = dict(prepared)
            residual.update({"state": "RESIDUAL", "residual_reason": fail_at, "residual_at_ns": time.time_ns()})
            _write_json_durable(journal, residual)
            raise RuntimeError("injected failure during parent directory fsync")
        _fsync_dir(path.parent)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    verified = validate_env_selection(path)
    if verified.value != new_value:
        raise EnvSelectionError("post-switch verification did not observe target value")
    if _replace_selection_bytes(verified, selection.value) != selection.original_bytes:
        raise EnvSelectionError("unrelated selection-file bytes changed")
    committed = dict(prepared)
    committed.update(
        {
            "state": "COMMITTED",
            "committed_at_ns": time.time_ns(),
            "after_sha256": verified.sha256,
            "after_metadata": _path_metadata(path),
        }
    )
    _write_json_durable(journal, committed)
    return SwitchResult(str(path), str(journal), run_id, selection.value, new_value, "COMMITTED")


def rollback_switch(env_file: str | Path, journal_file: str | Path, run_id: str) -> RollbackResult:
    path = Path(env_file)
    journal = Path(journal_file)
    try:
        payload = json.loads(journal.read_text())
    except Exception as exc:
        raise RollbackError(f"cannot read rollback journal: {exc}") from exc
    if payload.get("schema") != "lc6-promotion-selection-journal-v1":
        raise RollbackError("unsupported journal schema")
    if payload.get("state") != "COMMITTED":
        raise RollbackError(f"journal is not rollback-ready: {payload.get('state')}")
    if payload.get("run_id") != run_id:
        raise RollbackError("wrong run id for rollback journal")
    if payload.get("env_file") != str(path):
        raise RollbackError("journal belongs to a different selection file")
    selection = validate_env_selection(path)
    old_value = payload.get("old_value")
    new_value = payload.get("new_value")
    if selection.value != new_value:
        residual = dict(payload)
        residual.update({"state": "RESIDUAL", "residual_reason": "wrong-current-value", "residual_at_ns": time.time_ns()})
        _write_json_durable(journal, residual)
        raise RollbackError("current selection does not match journal new_value")
    replacement = _replace_selection_bytes(selection, old_value)
    try:
        _atomic_replace_preserve_metadata(path, replacement, selection.mode, selection.uid, selection.gid)
    except Exception as exc:
        try:
            current = validate_env_selection(path).value
        except Exception:
            current = "UNKNOWN_OR_INVALID"
        recovery = dict(payload)
        recovery.update(
            {
                "state": "RECOVERY_REQUIRED",
                "residual_reason": "rollback-failure",
                "current_env_selection": current,
                "intended_restored_selection": old_value,
                "temp_replacement_exists": False,
                "old_backend_state": "not controlled by synthetic rollback",
                "new_backend_state": "not controlled by synthetic rollback",
                "manual_recovery_action": "Review synthetic .env and journal; restore old_value only after verifying current selection and backend state; then archive/finalize journal.",
                "recovery_required_at_ns": time.time_ns(),
                "rollback_error": str(exc),
            }
        )
        _write_json_durable(journal, recovery)
        raise RollbackError(f"rollback atomic restore failed: {exc}") from exc
    if path.read_bytes() != replacement:
        residual = dict(payload)
        residual.update({"state": "RESIDUAL", "residual_reason": "rollback-verify-failed", "residual_at_ns": time.time_ns()})
        _write_json_durable(journal, residual)
        raise RollbackError("rollback verification failed")
    rolled = dict(payload)
    rolled.update({"state": "ROLLED_BACK", "rolled_back_at_ns": time.time_ns(), "rollback_sha256": _sha256(replacement)})
    _write_json_durable(journal, rolled)
    return RollbackResult(str(path), str(journal), run_id, old_value, "ROLLED_BACK")


class FakeHermesSupervisor:
    """Synthetic restart adapter for governed tests."""

    def __init__(
        self,
        *,
        initial_rag_db_path: str,
        refuse_stop: bool = False,
        wrong_inheritance: bool = False,
        refuse_start: bool = False,
        readiness_delay_s: float = 0.0,
    ) -> None:
        self._next_pid = 1000
        self._active = ProcessIdentity(self._alloc_pid(), time.time_ns(), "synthetic-hermes-backend", initial_rag_db_path)
        self.refuse_stop = refuse_stop
        self.wrong_inheritance = wrong_inheritance
        self.refuse_start = refuse_start
        self.readiness_delay_s = readiness_delay_s
        self.old_still_running = False
        self.last_failure_state: dict | None = None

    def _alloc_pid(self) -> int:
        self._next_pid += 1
        return self._next_pid

    def identity(self) -> ProcessIdentity:
        return self._active

    def restart_and_verify(self, expected_rag_db_path: str, readiness_timeout_s: float) -> RestartResult:
        old = self._active
        start_ns = time.time_ns()
        if self.refuse_stop:
            self.old_still_running = True
            self.last_failure_state = {"phase": "stop", "old_pid": old.pid, "new_pid": None, "rollback_requested": True}
            raise RuntimeError("old backend failed to stop")
        self.old_still_running = False
        new_pid = self._alloc_pid()
        if self.refuse_start:
            self._active = old
            self.last_failure_state = {"phase": "start", "old_pid": old.pid, "new_pid": new_pid, "rollback_requested": True}
            raise RuntimeError("new backend failed to start")
        if self.readiness_delay_s > readiness_timeout_s:
            self._active = old
            self.last_failure_state = {"phase": "readiness", "old_pid": old.pid, "new_pid": new_pid, "rollback_requested": True}
            raise RuntimeError("new backend readiness probe failed")
        if self.readiness_delay_s:
            time.sleep(self.readiness_delay_s)
        inherited = old.rag_db_path if self.wrong_inheritance else expected_rag_db_path
        new = ProcessIdentity(new_pid, time.time_ns(), old.command, inherited)
        if new.pid == old.pid or self.old_still_running:
            self._active = old
            self.last_failure_state = {"phase": "overlap", "old_pid": old.pid, "new_pid": new.pid, "rollback_requested": True}
            raise RuntimeError("backend overlap ambiguous")
        self._active = new
        if new.rag_db_path != expected_rag_db_path:
            self._active = old
            self.last_failure_state = {"phase": "inheritance", "old_pid": old.pid, "new_pid": new.pid, "rollback_requested": True}
            raise RuntimeError("new backend inherited wrong RAG_DB_PATH")
        self.last_failure_state = None
        return RestartResult(
            old_pid=old.pid,
            new_pid=new.pid,
            old_rag_db_path=old.rag_db_path,
            new_rag_db_path=new.rag_db_path,
            interruption_ms=max(0, int((time.time_ns() - start_ns) / 1_000_000)),
            overlap_ambiguous=False,
        )
