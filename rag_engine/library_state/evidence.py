"""Bounded read-only evidence collectors for library state resolution.

Never writes. Never lists journal directories. Never loads a full Chroma snapshot.
Chroma is queried only by an explicit embedding/chunk ID union.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rag_engine.certified_generation.append_recovery import JOURNAL_DIR_NAME, read_journal
from rag_engine.index_compatibility.constants import SIDECAR_V1_NAME
from rag_engine.library_state.contract import (
    GAP_CHROMA_ABSENT,
    GAP_CHROMA_QUERY_IDS_EMPTY,
    GAP_CHROMA_READ_ERROR,
    GAP_GENERATION_FINGERPRINT_ABSENT,
    GAP_JOURNAL_EXACT_LOCATOR_MISSING,
    GAP_JOURNAL_NO_EXACT_LOCATOR,
    GAP_REGISTRY_ABSENT,
    GAP_REGISTRY_READ_ERROR,
    GAP_REGISTRY_VECTOR_MAP_ABSENT,
    GAP_TRACKER_ABSENT,
    GAP_TRACKER_READ_ERROR,
)
from rag_engine.metadata_registry.connection import open_registry
from rag_engine.metadata_registry.exceptions import MissingDatabaseError, RegistryError
from rag_engine.reconciliation.chroma_reader import ChromaReadError, open_chroma_sqlite_readonly
from rag_engine.reconciliation.models import ChromaRecord, TrackerRecord
from rag_engine.reconciliation.tracker_reader import TrackerReadError, load_tracker_readonly
from rag_engine.stable_identity import (
    document_id_from_source_hash,
    source_hash_from_file,
)

CHROMA_ID_BATCH_SIZE = 500

_CHROMA_META_KEYS = (
    "source",
    "page",
    "collection",
    "probe",
    "document_id",
    "source_hash",
    "chunk_id",
)


def _batches(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _meta_value(row: Any) -> Any:
    if row["string_value"] is not None:
        return row["string_value"]
    if row["int_value"] is not None:
        return int(row["int_value"])
    if row["float_value"] is not None:
        return float(row["float_value"])
    if row["bool_value"] is not None:
        return bool(row["bool_value"])
    return None


def _sorted_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(v) for v in values if v is not None and str(v) != ""}))


@dataclass(frozen=True)
class ChromaIdLookup:
    records: tuple[ChromaRecord, ...]
    requested_ids: tuple[str, ...]
    found_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    requested: int
    found: int
    missing: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_ids": list(self.requested_ids),
            "found_ids": list(self.found_ids),
            "missing_ids": list(self.missing_ids),
            "requested": self.requested,
            "found": self.found,
            "missing": self.missing,
            "records": [r.to_dict() for r in self.records],
        }


def empty_chroma_lookup() -> ChromaIdLookup:
    return ChromaIdLookup(
        records=(),
        requested_ids=(),
        found_ids=(),
        missing_ids=(),
        requested=0,
        found=0,
        missing=0,
    )


def lookup_chroma_by_embedding_ids(
    chroma_sqlite: str | Path,
    embedding_ids: Sequence[str],
    *,
    batch_size: int = CHROMA_ID_BATCH_SIZE,
) -> ChromaIdLookup:
    """Read-only lookup of Chroma rows by embedding_id.

    Processes every sorted unique supplied ID in SQLite-safe batches.
    Never truncates the ID set. Never queries by source path.
    """
    requested = _sorted_unique(embedding_ids)
    if not requested:
        return empty_chroma_lookup()
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    conn = open_chroma_sqlite_readonly(chroma_sqlite)
    try:
        collections = {
            str(r["id"]): str(r["name"])
            for r in conn.execute("SELECT id, name FROM collections")
        }
        fallback_default = next(iter(collections.values()), "unknown")
        segment_to_collection: dict[str, str] = {}
        try:
            for r in conn.execute("SELECT id, collection FROM segments"):
                coll_id = str(r["collection"])
                segment_to_collection[str(r["id"])] = collections.get(coll_id, coll_id)
        except Exception:  # noqa: BLE001 - segments table may be missing
            segment_to_collection = {}

        rows_by_eid: dict[str, Any] = {}
        for batch in _batches(requested, batch_size):
            placeholders = ",".join("?" * len(batch))
            for r in conn.execute(
                "SELECT id, segment_id, embedding_id FROM embeddings "
                f"WHERE embedding_id IN ({placeholders})",
                tuple(batch),
            ):
                rows_by_eid[str(r["embedding_id"])] = r

        found_ids = tuple(sorted(rows_by_eid))
        missing_ids = tuple(eid for eid in requested if eid not in rows_by_eid)
        row_ids = [int(rows_by_eid[eid]["id"]) for eid in found_ids]
        meta_by_row_id: dict[int, dict[str, Any]] = {}
        key_placeholders = ",".join("?" * len(_CHROMA_META_KEYS))
        for id_batch in _batches([str(i) for i in row_ids], batch_size):
            id_placeholders = ",".join("?" * len(id_batch))
            params = tuple(int(x) for x in id_batch) + tuple(_CHROMA_META_KEYS)
            for r in conn.execute(
                "SELECT id, key, string_value, int_value, float_value, bool_value "
                "FROM embedding_metadata "
                f"WHERE id IN ({id_placeholders}) AND key IN ({key_placeholders})",
                params,
            ):
                rid = int(r["id"])
                meta_by_row_id.setdefault(rid, {})[str(r["key"])] = _meta_value(r)

        records: list[ChromaRecord] = []
        for eid in found_ids:
            r = rows_by_eid[eid]
            rid = int(r["id"])
            seg = str(r["segment_id"])
            physical = segment_to_collection.get(seg, fallback_default)
            meta = meta_by_row_id.get(rid, {})
            records.append(
                ChromaRecord(
                    chroma_embedding_id=eid,
                    physical_collection_name=physical,
                    source_path=(
                        str(meta["source"]) if meta.get("source") is not None else None
                    ),
                    page=meta.get("page"),
                    collection_meta=(
                        str(meta["collection"])
                        if meta.get("collection") is not None
                        else None
                    ),
                    metadata=dict(meta),
                )
            )
        return ChromaIdLookup(
            records=tuple(records),
            requested_ids=requested,
            found_ids=found_ids,
            missing_ids=missing_ids,
            requested=len(requested),
            found=len(found_ids),
            missing=len(missing_ids),
        )
    except ChromaReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChromaReadError(f"chroma id lookup failed: {exc}") from exc
    finally:
        conn.close()


def pairwise_id_comparison(
    left_name: str,
    left_ids: Sequence[str],
    right_name: str,
    right_ids: Sequence[str],
) -> dict[str, Any]:
    left = set(_sorted_unique(left_ids))
    right = set(_sorted_unique(right_ids))
    missing_in_right = tuple(sorted(left - right))
    unexpected_in_right = tuple(sorted(right - left))
    return {
        "left": left_name,
        "right": right_name,
        f"missing_in_{right_name}": list(missing_in_right),
        f"unexpected_in_{right_name}": list(unexpected_in_right),
        "counts": {
            "requested": len(left),
            "found": len(left & right),
            "missing": len(missing_in_right),
            "unexpected": len(unexpected_in_right),
        },
    }


def id_pairs_disagree(comparison: Mapping[str, Any] | None) -> bool:
    if not comparison:
        return False
    counts = comparison.get("counts") or {}
    return int(counts.get("missing") or 0) > 0 or int(counts.get("unexpected") or 0) > 0


@dataclass
class RegistryIdentity:
    document_id: str
    source_hash: str
    subject_id: str | None
    aliases: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    vector_ids: tuple[str, ...]
    vector_map_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_hash": self.source_hash,
            "subject_id": self.subject_id,
            "aliases": list(self.aliases),
            "chunk_ids": list(self.chunk_ids),
            "vector_ids": list(self.vector_ids),
            "vector_map_present": self.vector_map_present,
        }


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def load_registry_identity(
    conn: Any,
    *,
    source_hash: str | None = None,
    document_id: str | None = None,
    relative_path: str | None = None,
) -> tuple[list[RegistryIdentity], list[dict[str, Any]]]:
    """Targeted registry reads by hash, document_id, or exact path. No full scan."""
    identities: dict[str, RegistryIdentity] = {}
    path_rows: list[dict[str, Any]] = []

    def _load_document(doc_id: str) -> RegistryIdentity | None:
        if doc_id in identities:
            return identities[doc_id]
        ver = conn.execute(
            "SELECT document_id, subject_id, source_hash FROM document_versions "
            "WHERE document_id = ?",
            (doc_id,),
        ).fetchone()
        if ver is None:
            return None
        aliases = tuple(
            str(r["relative_path"])
            for r in conn.execute(
                "SELECT relative_path FROM source_files WHERE document_id = ? "
                "ORDER BY relative_path",
                (doc_id,),
            )
            if r["relative_path"]
        )
        chunk_ids = tuple(
            str(r["chunk_id"])
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE document_id = ? ORDER BY chunk_id",
                (doc_id,),
            )
            if r["chunk_id"]
        )
        vector_ids: tuple[str, ...] = ()
        vector_map_present = False
        if chunk_ids:
            vrows: list[Any] = []
            for batch in _batches(chunk_ids, CHROMA_ID_BATCH_SIZE):
                placeholders = ",".join("?" * len(batch))
                vrows.extend(
                    conn.execute(
                        "SELECT chroma_embedding_id FROM chunk_vector_map "
                        f"WHERE chunk_id IN ({placeholders}) "
                        "ORDER BY chroma_embedding_id",
                        tuple(batch),
                    ).fetchall()
                )
            vector_ids = _sorted_unique(
                str(_row_get(r, "chroma_embedding_id") or r[0]) for r in vrows
            )
            vector_map_present = len(vrows) > 0
        ident = RegistryIdentity(
            document_id=str(ver["document_id"]),
            source_hash=str(ver["source_hash"]),
            subject_id=(
                str(ver["subject_id"])
                if ver["subject_id"] is not None and str(ver["subject_id"])
                else None
            ),
            aliases=aliases,
            chunk_ids=chunk_ids,
            vector_ids=vector_ids,
            vector_map_present=vector_map_present,
        )
        identities[doc_id] = ident
        return ident

    if source_hash:
        rows = conn.execute(
            "SELECT document_id FROM document_versions WHERE source_hash = ? "
            "ORDER BY document_id",
            (source_hash,),
        ).fetchall()
        for row in rows:
            _load_document(str(row["document_id"]))
    if document_id:
        _load_document(document_id)
    if relative_path:
        rows = conn.execute(
            "SELECT document_id, relative_path, source_hash FROM source_files "
            "WHERE relative_path = ? ORDER BY document_id",
            (relative_path,),
        ).fetchall()
        for row in rows:
            path_rows.append(
                {
                    "document_id": str(row["document_id"]),
                    "relative_path": str(row["relative_path"]),
                    "source_hash": (
                        str(row["source_hash"]) if row["source_hash"] else None
                    ),
                }
            )
            _load_document(str(row["document_id"]))
    return list(identities.values()), path_rows


@dataclass
class FilesystemObservation:
    relative_path: str
    absolute_path: str
    exists: bool
    is_dir: bool
    source_hash: str | None
    document_id: str | None
    hashed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "source_hash": self.source_hash,
            "document_id": self.document_id,
            "hashed": self.hashed,
            "error": self.error,
        }


@dataclass
class EvidenceBundle:
    filesystem: dict[str, FilesystemObservation] = field(default_factory=dict)
    hashed_paths: list[str] = field(default_factory=list)
    inspected_paths: list[str] = field(default_factory=list)
    registry_by_hash: dict[str, RegistryIdentity] = field(default_factory=dict)
    registry_path_rows: list[dict[str, Any]] = field(default_factory=list)
    tracker_by_hash: dict[str, TrackerRecord] = field(default_factory=dict)
    tracker_by_path: dict[str, list[str]] = field(default_factory=dict)
    chroma: ChromaIdLookup | None = None
    query_ids: tuple[str, ...] = ()
    id_comparisons: dict[str, Any] = field(default_factory=dict)
    journal: dict[str, Any] | None = None
    generation: dict[str, Any] | None = None
    gaps: list[str] = field(default_factory=list)
    stores_configured: list[str] = field(default_factory=list)
    stores_read: list[str] = field(default_factory=list)

    def add_gap(self, gap: str) -> None:
        if gap not in self.gaps:
            self.gaps.append(gap)


def observe_file(
    relative_path: str,
    *,
    library_root: Path,
    hash_if_exists: bool,
) -> FilesystemObservation:
    abs_path = library_root / relative_path
    exists = abs_path.is_file()
    is_dir = abs_path.is_dir()
    digest: str | None = None
    document_id: str | None = None
    hashed = False
    err: str | None = None
    if exists and hash_if_exists:
        try:
            digest = source_hash_from_file(abs_path)
            hashed = True
            document_id = document_id_from_source_hash(digest)
        except OSError as exc:
            err = f"hash_error: {exc}"
    return FilesystemObservation(
        relative_path=relative_path,
        absolute_path=str(abs_path),
        exists=exists,
        is_dir=is_dir,
        source_hash=digest,
        document_id=document_id,
        hashed=hashed,
        error=err,
    )


import re as _re

# Allowed certified-append operation ID pattern.
# Accepts the current format  capp:YYYYMMDDTHHMMSSZ:<hex>  and any opaque
# identifier that contains no path separators, traversal sequences, NUL bytes,
# or other control characters.  The pattern is anchored to the full string.
_SAFE_OPERATION_ID_RE = _re.compile(r'^[A-Za-z0-9:._\-]+$')


def _is_safe_operation_id(value: str) -> bool:
    """Return True only when *value* cannot escape the journal directory.

    Rejects slashes, backslashes, traversal segments (``..``), NUL / control
    characters, and anything that does not match the safe-opaque-identifier
    pattern above.
    """
    if not value:
        return False
    # Fast reject: any slash or backslash at any position.
    if '/' in value or '\\' in value:
        return False
    # Fast reject: control characters (including NUL).
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return False
    # Fast reject: bare traversal segment.
    if value == '..' or value.startswith('../') or value.endswith('/..'):
        return False
    # Pattern match: only safe opaque characters allowed.
    return bool(_SAFE_OPERATION_ID_RE.match(value))


def _safe_journal_path(
    journal_path: str | Path,
    *,
    persist_dir: Path | None,
) -> tuple[Path | None, str | None]:
    """Validate *journal_path* and return (resolved_path, gap_or_None).

    Accepts only a regular file that resolves inside the expected journal
    directory.  Requires ``persist_dir`` to be configured; rejects the path
    without any filesystem access when it is absent.  Also rejects absolute
    external paths, traversal, symlink escapes, directories, and non-journal
    paths.
    """
    # Require a configured persist_dir; without it there is no known journal
    # root, so no path can be safely validated.
    if persist_dir is None:
        return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
    journal_root = (persist_dir / JOURNAL_DIR_NAME).resolve()
    path = Path(journal_path).resolve()
    # Containment check first - before any other filesystem access on path.
    try:
        path.relative_to(journal_root)
    except ValueError:
        return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
    # Only accept files with the exact .json extension (no .bak, .txt, etc.).
    if path.suffix.lower() != ".json":
        return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
    # Reject certified-append tracker snapshots written by snapshot_tracker().
    # Those follow the pattern  <op_id>.tracker.bak.json  and are recovery
    # evidence, not append journal records.  The predicate checks the stem
    # (filename without the final .json) to catch the embedded ".tracker.bak"
    # marker regardless of the operation ID prefix.
    if path.stem.endswith(".tracker.bak"):
        return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
    if not path.is_file():
        return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
    return path, None


def read_journal_if_exactly_located(
    *,
    persist_dir: Path | None,
    journal_path: str | Path | None,
    operation_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one journal file only when an exact locator is already known.

    Never lists a journal directory.

    Security constraints
    --------------------
    * ``journal_path``: must resolve inside ``persist_dir/certified_append_journal``
      when persist_dir is known; directories and symlink escapes are rejected.
    * ``operation_id``: validated against ``_SAFE_OPERATION_ID_RE``; slashes,
      backslashes, traversal segments, and control characters are rejected.
      The final constructed path is verified to be inside the journal root.
    """
    if journal_path is not None and str(journal_path).strip():
        resolved, gap = _safe_journal_path(journal_path, persist_dir=persist_dir)
        if resolved is not None:
            return read_journal(resolved), None
        return None, gap
    if operation_id and persist_dir is not None:
        if not _is_safe_operation_id(operation_id):
            return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
        journal_root = (persist_dir / JOURNAL_DIR_NAME).resolve()
        exact = (journal_root / f"{operation_id}.json").resolve()
        # Final containment check after path construction.
        try:
            exact.relative_to(journal_root)
        except ValueError:
            return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
        if exact.is_file():
            return read_journal(exact), None
        return None, GAP_JOURNAL_EXACT_LOCATOR_MISSING
    return None, GAP_JOURNAL_NO_EXACT_LOCATOR


def maybe_evaluate_generation(
    persist_dir: Path | None,
    *,
    registry_db: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Evaluate generation compatibility, optionally comparing against registry.

    Pass *registry_db* to detect registry/sidecar fingerprint conflicts.
    """
    if persist_dir is None:
        return None, None
    sidecar = persist_dir / SIDECAR_V1_NAME
    if not sidecar.is_file():
        return None, GAP_GENERATION_FINGERPRINT_ABSENT
    try:
        from rag_engine.index_compatibility.compatibility import evaluate_compatibility

        result = evaluate_compatibility(persist_dir, registry_db=registry_db)
        return result.to_dict(), None
    except Exception as exc:  # noqa: BLE001 - optional store
        return {"error": str(exc)}, GAP_GENERATION_FINGERPRINT_ABSENT


def chroma_union_query_ids(
    *,
    tracker_chunk_ids: Sequence[str],
    registry_vector_ids: Sequence[str] | None,
    registry_vector_map_present: bool,
) -> tuple[str, ...]:
    ids = list(tracker_chunk_ids)
    if registry_vector_map_present:
        ids.extend(registry_vector_ids or ())
    return _sorted_unique(ids)


def gather_store_handles(
    *,
    persist_dir: str | Path | None,
    registry_db: str | Path | None,
    tracker_path: str | Path | None,
) -> dict[str, Path | None]:
    persist = Path(persist_dir).resolve() if persist_dir is not None else None
    tracker = Path(tracker_path).resolve() if tracker_path is not None else None
    if tracker is None and persist is not None:
        tracker = persist / "embedded.json"
    chroma = (persist / "chroma.sqlite3") if persist is not None else None
    registry = Path(registry_db).resolve() if registry_db is not None else None
    return {
        "persist_dir": persist,
        "tracker_path": tracker,
        "chroma_sqlite": chroma,
        "registry_db": registry,
    }


def open_optional_registry(path: Path | None, bundle: EvidenceBundle) -> Any | None:
    if path is None:
        return None
    bundle.stores_configured.append("registry")
    if not path.is_file():
        bundle.add_gap(GAP_REGISTRY_ABSENT)
        return None
    try:
        conn = open_registry(path, readonly=True, create=False)
        bundle.stores_read.append("registry")
        return conn
    except MissingDatabaseError:
        bundle.add_gap(GAP_REGISTRY_ABSENT)
        return None
    except RegistryError as exc:
        bundle.add_gap(GAP_REGISTRY_READ_ERROR)
        bundle.add_gap(f"{GAP_REGISTRY_READ_ERROR}:{exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        bundle.add_gap(GAP_REGISTRY_READ_ERROR)
        bundle.add_gap(f"{GAP_REGISTRY_READ_ERROR}:{exc}")
        return None


def load_optional_tracker(path: Path | None, bundle: EvidenceBundle) -> dict[str, TrackerRecord]:
    if path is None:
        return {}
    bundle.stores_configured.append("tracker")
    if not path.is_file():
        bundle.add_gap(GAP_TRACKER_ABSENT)
        return {}
    try:
        records = load_tracker_readonly(path)
        bundle.stores_read.append("tracker")
        return records
    except TrackerReadError as exc:
        bundle.add_gap(GAP_TRACKER_READ_ERROR)
        bundle.add_gap(f"{GAP_TRACKER_READ_ERROR}:{exc}")
        return {}


def load_optional_chroma(
    path: Path | None,
    query_ids: Sequence[str],
    bundle: EvidenceBundle,
) -> ChromaIdLookup | None:
    if path is None:
        return None
    bundle.stores_configured.append("chroma")
    if not path.is_file():
        bundle.add_gap(GAP_CHROMA_ABSENT)
        return None
    if not query_ids:
        bundle.add_gap(GAP_CHROMA_QUERY_IDS_EMPTY)
        return empty_chroma_lookup()
    try:
        lookup = lookup_chroma_by_embedding_ids(path, query_ids)
        bundle.stores_read.append("chroma")
        return lookup
    except ChromaReadError as exc:
        bundle.add_gap(GAP_CHROMA_READ_ERROR)
        bundle.add_gap(f"{GAP_CHROMA_READ_ERROR}:{exc}")
        return None


def note_vector_map_gap(identities: Sequence[RegistryIdentity], bundle: EvidenceBundle) -> None:
    if "registry" not in bundle.stores_read:
        return
    if not identities:
        return
    if any(i.vector_map_present for i in identities):
        return
    bundle.add_gap(GAP_REGISTRY_VECTOR_MAP_ABSENT)
