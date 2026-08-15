"""Deterministic certified-append manifest (authorization input, not identity)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from rag_engine.certified_generation.exceptions import AppendManifestError
from rag_engine.stable_identity.hashing import source_hash_from_file
from rag_engine.stable_identity.ids import document_id_from_source_hash
from rag_engine.stable_identity.paths import PathNormalizationError, normalize_relative_path

APPEND_MANIFEST_SCHEMA = "b8ii-certified-append-manifest-v1"
COMPATIBLE_SCHEMAS = frozenset(
    {
        APPEND_MANIFEST_SCHEMA,
        "b8ic-eligible-ingest-manifest-v1",
        "b5i-corpus-manifest-v1",
    }
)

REQUIRED_ENTRY_FIELDS = (
    "relative_path",
    "absolute_path",
    "sha256",
    "size_bytes",
)

ELIGIBLE_CLASSIFICATIONS = frozenset(
    {
        "ELIGIBLE_NEW_VECTOR_INGEST",
        "ALIAS_ONLY",
        "NEW_REVISION",
        "APPROVED_APPEND",
        # Empty-manifest / future-compatible placeholders
        "eligible",
        "alias_only",
        "new_revision",
    }
)

FORBIDDEN_PATH_MARKERS = (
    "_Inbox",
    ".rag_db/",
    ".rag_state/",
    ".intake_state/",
    ".obsidian/",
    "/Hold/",
    "/discarded/",
    "\\Hold\\",
    "\\discarded\\",
)


def canonical_append_entries_payload(entries: list[Mapping[str, Any]]) -> str:
    compact = []
    for e in sorted(entries, key=lambda r: str(r.get("relative_path") or "")):
        compact.append(
            {
                "absolute_path": e["absolute_path"],
                "classification": e.get("classification"),
                "eligibility_basis": e.get("eligibility_basis"),
                "relative_path": e["relative_path"],
                "sha256": e["sha256"],
                "size_bytes": int(e["size_bytes"]),
            }
        )
    return json.dumps(
        {"entries": compact, "schema": APPEND_MANIFEST_SCHEMA},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def append_manifest_sha256(entries: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_append_entries_payload(entries).encode("utf-8")).hexdigest()


def _reject_forbidden_path(rel: str, abs_path: str) -> None:
    combined = f"{rel}\n{abs_path}"
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in combined:
            raise AppendManifestError(
                f"manifest path hits forbidden marker {marker!r}",
                details={"relative_path": rel, "absolute_path": abs_path},
            )
    # Path traversal
    parts = Path(rel).parts
    if ".." in parts or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise AppendManifestError(
            "manifest relative_path must be corpus-relative without traversal",
            details={"relative_path": rel},
        )


def load_append_manifest(path: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Load and validate a certified-append (or compatible empty) manifest."""
    if isinstance(path, Mapping):
        data = dict(path)
    else:
        p = Path(path)
        if not p.is_file():
            raise AppendManifestError(f"append manifest not found: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppendManifestError(f"append manifest unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise AppendManifestError("append manifest root must be object")
        data = raw

    schema = data.get("schema") or data.get("schema_version") or APPEND_MANIFEST_SCHEMA
    if schema not in COMPATIBLE_SCHEMAS and not str(schema).endswith("append-manifest-v1"):
        raise AppendManifestError(
            f"unsupported append manifest schema: {schema!r}",
            details={"schema": schema},
        )

    entries = data.get("entries")
    if not isinstance(entries, list):
        raise AppendManifestError("append manifest entries must be a list")

    seen_paths: set[str] = set()
    path_hashes: dict[str, str] = {}
    normalized: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            raise AppendManifestError("manifest entry must be object")
        for key in REQUIRED_ENTRY_FIELDS:
            if key not in e:
                raise AppendManifestError(f"manifest entry missing {key}")
        try:
            rel = normalize_relative_path(str(e["relative_path"]))
        except PathNormalizationError as exc:
            raise AppendManifestError(
                f"invalid relative_path: {exc}",
                details={"relative_path": e.get("relative_path")},
            ) from exc
        abs_path = str(Path(e["absolute_path"]).expanduser().resolve())
        sha = str(e["sha256"]).lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise AppendManifestError("manifest sha256 must be 64 lowercase hex")
        size = int(e["size_bytes"])
        classification = str(e.get("classification") or "APPROVED_APPEND")
        if classification not in ELIGIBLE_CLASSIFICATIONS:
            raise AppendManifestError(
                f"unsupported classification: {classification!r}",
                details={"relative_path": rel},
            )
        _reject_forbidden_path(rel, abs_path)
        if rel in seen_paths:
            raise AppendManifestError(f"duplicate relative_path: {rel}")
        seen_paths.add(rel)
        prev = path_hashes.get(abs_path)
        if prev is not None and prev != sha:
            raise AppendManifestError(
                "duplicate absolute_path with conflicting hash",
                details={"absolute_path": abs_path, "a": prev, "b": sha},
            )
        path_hashes[abs_path] = sha
        normalized.append(
            {
                "relative_path": rel,
                "absolute_path": abs_path,
                "sha256": sha,
                "size_bytes": size,
                "classification": classification,
                "eligibility_basis": e.get("eligibility_basis"),
                "document_id": document_id_from_source_hash(sha),
            }
        )

    normalized.sort(key=lambda r: r["relative_path"])
    computed = append_manifest_sha256(normalized)
    stored = data.get("manifest_sha256")
    if stored and stored != computed:
        raise AppendManifestError(
            "manifest_sha256 does not match entries",
            details={"stored": stored, "computed": computed},
        )

    out = dict(data)
    out["schema"] = APPEND_MANIFEST_SCHEMA
    out["entries"] = normalized
    out["manifest_sha256"] = computed
    out["source_count"] = len(normalized)
    if "generation_id" in data and data["generation_id"]:
        out["generation_id"] = str(data["generation_id"])
    return out


def verify_append_manifest_against_disk(manifest: Mapping[str, Any]) -> None:
    """Rehash live bytes; drift ? fail closed."""
    for e in manifest["entries"]:
        path = Path(e["absolute_path"])
        if not path.is_file():
            raise AppendManifestError(
                f"source missing: {e['relative_path']}",
                details={"path": str(path)},
            )
        digest = source_hash_from_file(path)
        if digest != e["sha256"]:
            raise AppendManifestError(
                f"source hash drift: {e['relative_path']}",
                details={"expected": e["sha256"], "actual": digest},
            )
        size = path.stat().st_size
        if int(size) != int(e["size_bytes"]):
            raise AppendManifestError(
                f"source size drift: {e['relative_path']}",
                details={"expected": e["size_bytes"], "actual": size},
            )


def build_append_manifest_from_paths(
    pairs: list[tuple[Path, str]],
    *,
    classification: str = "APPROVED_APPEND",
    generation_id: str | None = None,
    eligibility_basis: str = "operator_approved_fixture",
) -> dict[str, Any]:
    entries = []
    for abs_path, rel in pairs:
        path = Path(abs_path)
        if not path.is_file():
            raise AppendManifestError(f"source file missing: {path}")
        digest = source_hash_from_file(path)
        entries.append(
            {
                "relative_path": normalize_relative_path(rel),
                "absolute_path": str(path.resolve()),
                "sha256": digest,
                "size_bytes": path.stat().st_size,
                "classification": classification,
                "eligibility_basis": eligibility_basis,
            }
        )
    payload: dict[str, Any] = {"schema": APPEND_MANIFEST_SCHEMA, "entries": entries}
    if generation_id:
        payload["generation_id"] = generation_id
    return load_append_manifest(payload)
