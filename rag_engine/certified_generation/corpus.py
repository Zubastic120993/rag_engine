"""Frozen corpus manifest + alias dedup by source bytes."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from rag_engine.certified_generation.exceptions import CorpusManifestError
from rag_engine.stable_identity.hashing import source_hash_from_file
from rag_engine.stable_identity.ids import document_id_from_source_hash
from rag_engine.stable_identity.paths import normalize_relative_path

MANIFEST_SCHEMA = "b5i-corpus-manifest-v1"


def sha256_bytes_file(path: Path) -> str:
    return source_hash_from_file(path)


def canonical_manifest_payload(entries: list[Mapping[str, Any]]) -> str:
    compact = []
    for e in sorted(entries, key=lambda r: str(r.get("relative_path") or "")):
        compact.append(
            {
                "relative_path": e["relative_path"],
                "absolute_path": e["absolute_path"],
                "sha256": e["sha256"],
                "size_bytes": int(e["size_bytes"]),
            }
        )
    return json.dumps(
        {"schema": MANIFEST_SCHEMA, "entries": compact},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def manifest_sha256(entries: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_manifest_payload(entries).encode("utf-8")).hexdigest()


def build_manifest_from_paths(
    pairs: Iterable[tuple[Path, str]],
) -> dict[str, Any]:
    """pairs = (absolute_path, relative_path). Hashes raw bytes."""
    entries = []
    for abs_path, rel in pairs:
        path = Path(abs_path)
        if not path.is_file():
            raise CorpusManifestError(f"source file missing: {path}")
        digest = sha256_bytes_file(path)
        st = path.stat()
        entries.append(
            {
                "relative_path": normalize_relative_path(rel),
                "absolute_path": str(path.resolve()),
                "sha256": digest,
                "size_bytes": st.st_size,
                "document_id": document_id_from_source_hash(digest),
            }
        )
    entries.sort(key=lambda r: r["relative_path"])
    return {
        "schema": MANIFEST_SCHEMA,
        "identity_rule": "document_id = docrev:<sha256(raw bytes)>; path is NOT identity",
        "manifest_sha256": manifest_sha256(entries),
        "source_count": len(entries),
        "entries": entries,
    }


def load_corpus_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise CorpusManifestError(f"corpus manifest not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusManifestError(f"corpus manifest unreadable: {exc}") from exc
    if not isinstance(data, dict) or "entries" not in data:
        raise CorpusManifestError("corpus manifest must be an object with entries")
    entries = data["entries"]
    if not isinstance(entries, list) or not entries:
        raise CorpusManifestError("corpus manifest entries must be a non-empty list")
    for e in entries:
        for key in ("relative_path", "absolute_path", "sha256", "size_bytes"):
            if key not in e:
                raise CorpusManifestError(f"manifest entry missing {key}")
        if len(str(e["sha256"])) != 64:
            raise CorpusManifestError("manifest sha256 must be 64 hex")
    computed = manifest_sha256(entries)
    stored = data.get("manifest_sha256")
    if stored and stored != computed:
        raise CorpusManifestError(
            "manifest_sha256 does not match entries",
            details={"stored": stored, "computed": computed},
        )
    data = dict(data)
    data["manifest_sha256"] = computed
    data["schema"] = data.get("schema") or MANIFEST_SCHEMA
    return data


def verify_manifest_against_disk(manifest: Mapping[str, Any]) -> None:
    """B6 drift gate: every listed source must still match frozen bytes."""
    for e in manifest["entries"]:
        path = Path(e["absolute_path"])
        if not path.is_file():
            raise CorpusManifestError(
                f"source removed since freeze: {e['relative_path']}",
                details={"path": str(path)},
            )
        digest = sha256_bytes_file(path)
        if digest != e["sha256"]:
            raise CorpusManifestError(
                f"source bytes drifted: {e['relative_path']}",
                details={"expected": e["sha256"], "actual": digest},
            )
        size = path.stat().st_size
        if int(size) != int(e["size_bytes"]):
            raise CorpusManifestError(
                f"source size drifted: {e['relative_path']}",
                details={"expected": e["size_bytes"], "actual": size},
            )


def dedup_by_document_id(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One rebuild unit per unique bytes. Display source = lex-smallest alias."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in manifest["entries"]:
        digest = str(e["sha256"])
        groups[digest].append(dict(e))
    units = []
    for digest, aliases in groups.items():
        aliases_sorted = sorted(aliases, key=lambda r: r["relative_path"])
        display = aliases_sorted[0]["relative_path"]
        units.append(
            {
                "source_hash": digest,
                "document_id": document_id_from_source_hash(digest),
                "display_source": display,
                "display_source_role": "display_compatibility_only",
                "canonical": False,
                "aliases": [a["relative_path"] for a in aliases_sorted],
                "absolute_path": aliases_sorted[0]["absolute_path"],
                "alias_records": aliases_sorted,
            }
        )
    units.sort(key=lambda u: u["document_id"])
    return units
