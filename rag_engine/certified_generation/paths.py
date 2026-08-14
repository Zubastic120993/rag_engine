"""Explicit persist-dir guards for certified generations.

Never defaults to persist_dir() / production ``.rag_db``.
Never creates production ``.rag_db_generations`` on import.
"""

from __future__ import annotations

from pathlib import Path

from rag_engine.certified_generation.exceptions import (
    ExplicitTargetRequiredError,
    LegacyPathForbiddenError,
    UnsafeGenerationPathError,
)

# Hard production paths (B5 closed). Resolve at call time for symlink tests.
PRODUCTION_LIBRARY_ROOT = Path("/Users/vladymyrzub/CE_Library")
PRODUCTION_RAG_DB = PRODUCTION_LIBRARY_ROOT / ".rag_db"
PRODUCTION_RAG_STATE = PRODUCTION_LIBRARY_ROOT / ".rag_state"
PRODUCTION_INTAKE_STATE = PRODUCTION_LIBRARY_ROOT / ".intake_state"
PRODUCTION_GENERATIONS_ROOT = PRODUCTION_LIBRARY_ROOT / ".rag_db_generations"

FORBIDDEN_DIR_NAMES = frozenset(
    {
        ".rag_db",
        ".rag_state",
        ".intake_state",
        ".obsidian",
        "_Inbox",
    }
)


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def production_rag_db() -> Path:
    return PRODUCTION_RAG_DB.resolve()


def require_explicit_persist_dir(persist_dir: str | Path | None) -> Path:
    """Fail closed if certified APIs omit the target persist directory."""
    if persist_dir is None or str(persist_dir).strip() == "":
        raise ExplicitTargetRequiredError(
            "certified generation requires an explicit persist directory; "
            "refusing persist_dir() / production .rag_db default"
        )
    return _resolve(persist_dir)


def is_legacy_production_persist(path: str | Path) -> bool:
    """True if path is (or lives inside) the live production Chroma persist dir."""
    target = _resolve(path)
    prod = production_rag_db()
    if target == prod:
        return True
    try:
        target.relative_to(prod)
        return True
    except ValueError:
        return False


def assert_not_legacy_production(path: str | Path) -> Path:
    target = _resolve(path)
    if is_legacy_production_persist(target):
        raise LegacyPathForbiddenError(
            "certified generation must not target production .rag_db",
            details={"path": str(target), "production": str(production_rag_db())},
        )
    return target


def assert_safe_generation_path(path: str | Path) -> Path:
    """Refuse production index, registry, intake, corpus-as-docs, and .rag_db itself."""
    target = assert_not_legacy_production(path)
    prod_state = PRODUCTION_RAG_STATE.resolve()
    prod_intake = PRODUCTION_INTAKE_STATE.resolve()
    for forbidden, label in (
        (prod_state, ".rag_state"),
        (prod_intake, ".intake_state"),
    ):
        if target == forbidden:
            raise UnsafeGenerationPathError(
                f"certified generation must not target {label}",
                details={"path": str(target)},
            )
        try:
            target.relative_to(forbidden)
            raise UnsafeGenerationPathError(
                f"certified generation must not resolve inside {label}",
                details={"path": str(target)},
            )
        except ValueError:
            pass
        except UnsafeGenerationPathError:
            raise

    parts = {p.lower() for p in target.parts}
    if ".rag_db" in parts and target != production_rag_db():
        # A path component named .rag_db is the live persist convention.
        # Certified gens live under .rag_db_generations, never .rag_db.
        raise UnsafeGenerationPathError(
            "certified generation path must not use .rag_db as a path component",
            details={"path": str(target)},
        )
    if ".intake_state" in parts:
        raise UnsafeGenerationPathError(
            "certified generation path must not resolve into .intake_state",
            details={"path": str(target)},
        )
    if ".rag_state" in parts:
        raise UnsafeGenerationPathError(
            "certified generation path must not resolve into .rag_state",
            details={"path": str(target)},
        )
    return target


def assert_not_production_generations_root(path: str | Path) -> Path:
    """B5I tests must not create the production generations root."""
    target = _resolve(path)
    prod_root = PRODUCTION_GENERATIONS_ROOT.resolve()
    if target == prod_root:
        raise UnsafeGenerationPathError(
            "B5I must not create production .rag_db_generations",
            details={"path": str(target)},
        )
    try:
        target.relative_to(prod_root)
        raise UnsafeGenerationPathError(
            "B5I must not write under production .rag_db_generations",
            details={"path": str(target)},
        )
    except ValueError:
        return target
    except UnsafeGenerationPathError:
        raise


def registry_path_is_production(path: str | Path) -> bool:
    target = _resolve(path)
    prod = (PRODUCTION_RAG_STATE / "metadata_registry" / "metadata_registry_v1.sqlite3").resolve()
    return target == prod or str(target).startswith(str(PRODUCTION_RAG_STATE.resolve()))
