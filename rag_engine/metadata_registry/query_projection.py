"""Read-only HYBRID provenance projection for retrieval results (B7).

Compact Chroma metadata supplies document_id / source_hash / chunk_id when
trustworthy. Optional registry enrichment supplies subject/aliases/title when
rows exist. Fail-open: registry absence or errors never invent fields and
never block retrieval packaging.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rag_engine.metadata_registry.connection import open_registry
from rag_engine.metadata_registry.exceptions import MissingDatabaseError
from rag_engine.metadata_registry.paths import production_registry_path

# Bounded alias projection - avoid exploding multi-path docs into huge arrays.
MAX_ALIASES_EXPOSED = 8

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

PROVENANCE_REGISTRY_ENRICHED = "REGISTRY_ENRICHED"
PROVENANCE_COMPACT_ONLY = "COMPACT_PROVENANCE_ONLY"
PROVENANCE_LEGACY_LIMITED = "LEGACY_PROVENANCE_LIMITED"


@dataclass(frozen=True)
class DocumentEnrichment:
    document_id: str
    subject_id: str | None = None
    source_hash: str | None = None
    document_type: str | None = None
    document_number: str | None = None
    title: str | None = None
    scope: str | None = None
    aliases: tuple[str, ...] = ()
    alias_count: int = 0
    subject_status: str | None = None


@dataclass
class ProvenanceBundle:
    """Per-answer enrichment context (one batch lookup)."""

    by_document_id: dict[str, DocumentEnrichment] = field(default_factory=dict)
    registry_status: str = "skipped"  # ok | absent | error | skipped | empty
    provenance_level: str = PROVENANCE_LEGACY_LIMITED
    embedding_generation_id: str | None = None

    def for_document(self, document_id: str | None) -> DocumentEnrichment | None:
        if not document_id:
            return None
        return self.by_document_id.get(document_id)


def resolve_registry_db_path(
    *,
    library_root: str | Path | None = None,
    explicit: str | Path | None = None,
) -> Path | None:
    """Resolve registry path without creating anything.

    Precedence: explicit arg ? RAG_METADATA_REGISTRY_PATH ? production path
    under library_root (when provided).
    """
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("RAG_METADATA_REGISTRY_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if library_root is not None:
        return production_registry_path(library_root).resolve()
    return None


def trustworthy_document_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("docrev:") and len(text) > len("docrev:"):
        return text
    return None


def trustworthy_source_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _HEX64.fullmatch(text):
        return text.lower()
    return None


def trustworthy_chunk_id(value: Any) -> str | None:
    """Accept only stable certified chunk IDs - never UUID vector handles."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("chunk:") and len(text) > len("chunk:"):
        # Reject accidental UUID-shaped suffixes presented as chunk ids.
        rest = text[len("chunk:") :]
        if _UUID_RE.fullmatch(rest):
            return None
        return text
    return None


def compact_provenance_from_meta(meta: Mapping[str, Any] | None) -> dict[str, str]:
    """Extract certified compact Chroma provenance; omit inventable gaps."""
    meta = meta or {}
    out: dict[str, str] = {}
    did = trustworthy_document_id(meta.get("document_id"))
    if did:
        out["document_id"] = did
    sh = trustworthy_source_hash(meta.get("source_hash"))
    if sh:
        out["source_hash"] = sh
    cid = trustworthy_chunk_id(meta.get("chunk_id"))
    if cid:
        out["chunk_id"] = cid
    return out


def _subject_status(subject_id: str | None) -> str | None:
    if not subject_id:
        return None
    if subject_id.startswith("subj:pending:"):
        return "pending"
    return "registered"


def batch_lookup_documents(
    conn: sqlite3.Connection,
    document_ids: Sequence[str],
    *,
    max_aliases: int = MAX_ALIASES_EXPOSED,
) -> dict[str, DocumentEnrichment]:
    """Read-only batch enrichment keyed by document_id."""
    ids = sorted({d for d in document_ids if isinstance(d, str) and d.startswith("docrev:")})
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
            dv.document_id AS document_id,
            dv.subject_id AS subject_id,
            dv.source_hash AS source_hash,
            d.document_type AS document_type,
            d.document_number AS document_number,
            d.canonical_title AS canonical_title,
            d.scope AS scope
        FROM document_versions dv
        LEFT JOIN documents d ON d.subject_id = dv.subject_id
        WHERE dv.document_id IN ({placeholders})
        """,
        ids,
    ).fetchall()

    alias_rows = conn.execute(
        f"""
        SELECT document_id, relative_path
        FROM source_files
        WHERE document_id IN ({placeholders})
        ORDER BY relative_path ASC
        """,
        ids,
    ).fetchall()

    aliases_by_doc: dict[str, list[str]] = {i: [] for i in ids}
    for row in alias_rows:
        did = row["document_id"] if isinstance(row, sqlite3.Row) else row[0]
        path = row["relative_path"] if isinstance(row, sqlite3.Row) else row[1]
        if did in aliases_by_doc and isinstance(path, str) and path:
            aliases_by_doc[did].append(path)

    out: dict[str, DocumentEnrichment] = {}
    for row in rows:
        if isinstance(row, sqlite3.Row):
            did = row["document_id"]
            subject_id = row["subject_id"]
            source_hash = row["source_hash"]
            document_type = row["document_type"]
            document_number = row["document_number"]
            title = row["canonical_title"]
            scope = row["scope"]
        else:
            did, subject_id, source_hash, document_type, document_number, title, scope = row
        all_aliases = aliases_by_doc.get(did, [])
        out[did] = DocumentEnrichment(
            document_id=did,
            subject_id=subject_id if isinstance(subject_id, str) and subject_id else None,
            source_hash=trustworthy_source_hash(source_hash),
            document_type=document_type if isinstance(document_type, str) and document_type else None,
            document_number=(
                document_number if isinstance(document_number, str) and document_number else None
            ),
            title=title if isinstance(title, str) and title else None,
            scope=scope if isinstance(scope, str) and scope else None,
            aliases=tuple(all_aliases[:max_aliases]),
            alias_count=len(all_aliases),
            subject_status=_subject_status(
                subject_id if isinstance(subject_id, str) else None
            ),
        )
    return out


def load_provenance_bundle(
    pairs: Sequence[tuple[Any, float]] | None,
    *,
    registry_db: str | Path | None = None,
    library_root: str | Path | None = None,
    max_aliases: int = MAX_ALIASES_EXPOSED,
) -> ProvenanceBundle:
    """Build enrichment for retrieval pairs - fail-open, never invents."""
    bundle = ProvenanceBundle()
    metas: list[Mapping[str, Any]] = []
    document_ids: list[str] = []
    generation_ids: set[str] = set()

    for item in pairs or ():
        doc = item[0]
        meta = getattr(doc, "metadata", None) or {}
        if not isinstance(meta, Mapping):
            continue
        metas.append(meta)
        did = trustworthy_document_id(meta.get("document_id"))
        if did:
            document_ids.append(did)
        gen = meta.get("embedding_generation_id")
        if isinstance(gen, str) and gen.startswith("raggen:"):
            generation_ids.add(gen)

    if len(generation_ids) == 1:
        bundle.embedding_generation_id = next(iter(generation_ids))

    compact_hits = sum(1 for m in metas if compact_provenance_from_meta(m))
    if not document_ids and compact_hits == 0:
        bundle.provenance_level = PROVENANCE_LEGACY_LIMITED
        bundle.registry_status = "skipped"
        return bundle

    if not document_ids:
        bundle.provenance_level = (
            PROVENANCE_COMPACT_ONLY if compact_hits else PROVENANCE_LEGACY_LIMITED
        )
        bundle.registry_status = "skipped"
        return bundle

    path = resolve_registry_db_path(library_root=library_root, explicit=registry_db)
    if path is None or not path.exists():
        bundle.provenance_level = PROVENANCE_COMPACT_ONLY
        bundle.registry_status = "absent"
        return bundle

    try:
        conn = open_registry(path, readonly=True, create=False)
    except MissingDatabaseError:
        bundle.provenance_level = PROVENANCE_COMPACT_ONLY
        bundle.registry_status = "absent"
        return bundle
    except Exception:  # noqa: BLE001 - fail-open
        bundle.provenance_level = PROVENANCE_COMPACT_ONLY
        bundle.registry_status = "error"
        return bundle

    try:
        enriched = batch_lookup_documents(conn, document_ids, max_aliases=max_aliases)
        bundle.by_document_id = enriched
        if enriched:
            bundle.provenance_level = PROVENANCE_REGISTRY_ENRICHED
            bundle.registry_status = "ok"
        else:
            bundle.provenance_level = PROVENANCE_COMPACT_ONLY
            bundle.registry_status = "empty"
    except Exception:  # noqa: BLE001 - fail-open
        bundle.provenance_level = PROVENANCE_COMPACT_ONLY
        bundle.registry_status = "error"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return bundle


def registry_fields_for_document(enrichment: DocumentEnrichment | None) -> dict[str, Any]:
    """Optional registry-backed fields; omit rather than invent."""
    if enrichment is None:
        return {}
    out: dict[str, Any] = {}
    if enrichment.subject_id:
        out["subject_id"] = enrichment.subject_id
    if enrichment.subject_status:
        out["subject_status"] = enrichment.subject_status
    if enrichment.document_type:
        out["document_type"] = enrichment.document_type
    if enrichment.document_number:
        out["document_number"] = enrichment.document_number
    if enrichment.title:
        out["title"] = enrichment.title
    if enrichment.scope:
        out["scope"] = enrichment.scope
    if enrichment.alias_count:
        out["alias_count"] = int(enrichment.alias_count)
        if enrichment.aliases:
            out["aliases"] = list(enrichment.aliases)
    # canonical_path / revision / edition intentionally never projected -
    # schema has no governed canonical_path or revision/edition columns.
    return out


def apply_provenance_to_entry(
    entry: dict[str, Any],
    meta: Mapping[str, Any] | None,
    bundle: ProvenanceBundle | None,
    *,
    include_chunk_id: bool = True,
) -> dict[str, Any]:
    """Mutate/return entry with compact + optional registry fields (omit-absent)."""
    compact = compact_provenance_from_meta(meta)
    if not include_chunk_id:
        compact.pop("chunk_id", None)
    for key, value in compact.items():
        entry[key] = value
    if bundle is not None:
        reg = registry_fields_for_document(bundle.for_document(compact.get("document_id")))
        for key, value in reg.items():
            entry[key] = value
    return entry


def document_ids_from_pairs(pairs: Iterable[tuple[Any, float]]) -> list[str]:
    ids: list[str] = []
    for doc, _ in pairs:
        meta = getattr(doc, "metadata", None) or {}
        did = trustworthy_document_id(meta.get("document_id") if isinstance(meta, Mapping) else None)
        if did:
            ids.append(did)
    return ids
