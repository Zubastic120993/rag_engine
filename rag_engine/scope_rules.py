"""Deterministic path→scope assignment with explainable rules."""

from __future__ import annotations

from typing import Any

from rag_engine.config import hermes_aliases, known_scopes, load_registry


class RegistryError(ValueError):
    """Invalid scopes.yaml (duplicate alias, collision, etc.)."""


def validate_registry(reg: dict[str, Any] | None = None) -> None:
    """Raise RegistryError on duplicate aliases or alias/scope-name collisions."""
    reg = reg or load_registry()
    scopes = list((reg.get("scopes") or {}).keys())
    scope_set = set(scopes)
    seen: dict[str, str] = {}
    for scope, meta in (reg.get("scopes") or {}).items():
        for alias in meta.get("hermes_aliases") or []:
            key = str(alias).strip().lower().replace("-", "_")
            if not key:
                continue
            if key in scope_set or key.replace("_", "-") in scope_set:
                # also compare dash form of scope names
                colliding = key if key in scope_set else key.replace("_", "-")
                for s in scopes:
                    if s.lower().replace("-", "_") == key:
                        colliding = s
                        break
                raise RegistryError(
                    f"Alias {alias!r} collides with scope name {colliding!r}"
                )
            if key in seen:
                raise RegistryError(
                    f"Duplicate alias {alias!r}: maps to both {seen[key]!r} and {scope!r}"
                )
            seen[key] = scope


def explain_path_assignment(rel: str) -> dict[str, Any]:
    """Return assigned scope and the rule that selected it."""
    norm = rel.replace("\\", "/")
    upper = norm.upper()
    reg = load_registry()
    scopes: dict[str, Any] = reg["scopes"]
    prefix_order = list(reg.get("prefix_order") or list(scopes.keys()))
    catch_all = {"maker-manuals", "career", "other"}

    def prefix_hit(scope_name: str) -> str | None:
        meta = scopes.get(scope_name) or {}
        for p in meta.get("path_prefixes") or []:
            if norm.startswith(p):
                return p
        return None

    def hint_hit(scope_name: str) -> str | None:
        meta = scopes.get(scope_name) or {}
        for h in meta.get("path_hints") or []:
            if h.upper() in upper:
                return h
        return None

    for scope_name in prefix_order:
        if scope_name in catch_all:
            continue
        hit = prefix_hit(scope_name)
        if hit:
            return {
                "path": norm,
                "scope": scope_name,
                "rule": "path_prefix",
                "matched": hit,
                "aliases": list((scopes.get(scope_name) or {}).get("hermes_aliases") or []),
            }

    for hint_scope in ("me-c", "inspection", "regulatory", "maker-manuals"):
        hit = hint_hit(hint_scope)
        if hit:
            return {
                "path": norm,
                "scope": hint_scope,
                "rule": "path_hint",
                "matched": hit,
                "aliases": list((scopes.get(hint_scope) or {}).get("hermes_aliases") or []),
            }

    for scope_name in prefix_order:
        if scope_name not in catch_all:
            continue
        hit = prefix_hit(scope_name)
        if hit:
            return {
                "path": norm,
                "scope": scope_name,
                "rule": "path_prefix",
                "matched": hit,
                "aliases": list((scopes.get(scope_name) or {}).get("hermes_aliases") or []),
            }

    return {
        "path": norm,
        "scope": "other",
        "rule": "default",
        "matched": None,
        "aliases": [],
    }


def explain_alias(alias: str) -> dict[str, Any]:
    """Reverse lookup: Hermes alias → scope + resolution rule."""
    raw = (alias or "").strip()
    if not raw:
        raise ValueError("empty alias")
    key = raw.lower().replace("-", "_")
    scopes = known_scopes()
    if raw in scopes:
        return {
            "alias": raw,
            "resolved_scope": raw,
            "rule": "scope_name",
            "aliases": list(
                (load_registry()["scopes"].get(raw) or {}).get("hermes_aliases") or []
            ),
        }
    for scope in scopes:
        if scope.replace("-", "_") == key:
            return {
                "alias": raw,
                "resolved_scope": scope,
                "rule": "scope_name_normalized",
                "aliases": list(
                    (load_registry()["scopes"].get(scope) or {}).get("hermes_aliases")
                    or []
                ),
            }
    aliases = hermes_aliases()
    if key in aliases:
        scope = aliases[key]
        return {
            "alias": raw,
            "resolved_scope": scope,
            "rule": "hermes_alias",
            "aliases": list(
                (load_registry()["scopes"].get(scope) or {}).get("hermes_aliases") or []
            ),
        }
    raise ValueError(f"Unknown alias or scope {alias!r}")


def scope_allows_candidate(scope: str | None, metadata: dict[str, Any] | None) -> bool:
    """Query-time scope-selection filter for already indexed candidates."""
    if not scope:
        return True
    reg = load_registry()
    meta = (reg.get("scopes") or {}).get(scope) or {}
    candidate = metadata or {}
    source = str(candidate.get("source") or "")
    doc_type = str(candidate.get("document_type") or "")

    allowed_prefixes = list(meta.get("retrieval_allowed_path_prefixes") or [])
    if allowed_prefixes and not any(source.startswith(prefix) for prefix in allowed_prefixes):
        return False

    excluded_doc_types = set(meta.get("retrieval_excluded_document_types") or [])
    if doc_type and doc_type in excluded_doc_types:
        return False

    return True
