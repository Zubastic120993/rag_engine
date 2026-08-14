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
PRODUCTION_REGISTRY_DB = (
    PRODUCTION_RAG_STATE / "metadata_registry" / "metadata_registry_v1.sqlite3"
)
GOVERNED_REGISTRY_RELATIVE_PARTS = (".rag_state", "metadata_registry", "metadata_registry_v1.sqlite3")
GOVERNED_REGISTRY_FILENAME = "metadata_registry_v1.sqlite3"
GENERATIONS_ROOT_NAME = ".rag_db_generations"

FORBIDDEN_DIR_NAMES = frozenset(
    {
        ".rag_db",
        ".rag_state",
        ".intake_state",
        ".obsidian",
        "_Inbox",
    }
)

_REGISTRY_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".rag_db",
        ".intake_state",
        GENERATIONS_ROOT_NAME,
    }
)


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def production_rag_db() -> Path:
    return PRODUCTION_RAG_DB.resolve()


def production_registry_db() -> Path:
    return PRODUCTION_REGISTRY_DB.resolve()


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


def is_contained_generation_child(
    path: str | Path,
    *,
    generations_root: str | Path | None = None,
) -> bool:
    """True iff ``path`` resolves to a proper descendant of ``generations_root``.

    The generations root itself is not a child. Symlinks are resolved first, so
    an escape into ``.rag_db`` is not classified as contained.
    """
    target = _resolve(path)
    root = _resolve(generations_root) if generations_root is not None else PRODUCTION_GENERATIONS_ROOT.resolve()
    if target == root:
        return False
    try:
        rel = target.relative_to(root)
    except ValueError:
        return False
    return len(rel.parts) >= 1


def _is_generations_root_dir(target: Path) -> bool:
    return target.name == GENERATIONS_ROOT_NAME


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
    """Refuse a generations ROOT as persist-dir; allow a contained child.

    Distinguishes ``<library>/.rag_db_generations`` (forbidden) from
    ``<library>/.rag_db_generations/<raggen_...>`` (allowed). Isolated
    tmp fixtures outside the production library remain allowed. Arbitrary
    siblings under the production library are not accepted merely because
    they resemble a generation name.
    """
    target = _resolve(path)
    prod_root = PRODUCTION_GENERATIONS_ROOT.resolve()
    prod_lib = PRODUCTION_LIBRARY_ROOT.resolve()

    if target == prod_root or _is_generations_root_dir(target):
        raise UnsafeGenerationPathError(
            "certified generation must not use the .rag_db_generations root as persist directory",
            details={"path": str(target)},
        )

    if is_contained_generation_child(target, generations_root=prod_root):
        return target

    try:
        target.relative_to(prod_lib)
    except ValueError:
        return target

    raise UnsafeGenerationPathError(
        "certified production generation must be a contained child of .rag_db_generations",
        details={"path": str(target), "generations_root": str(prod_root)},
    )


def assert_certified_persist_dir(persist_dir: str | Path | None) -> Path:
    """Shared persist-dir policy for validate-target / init / build / v1."""
    persist = require_explicit_persist_dir(persist_dir)
    persist = assert_safe_generation_path(persist)
    persist = assert_not_production_generations_root(persist)
    return persist


def is_governed_registry_shape(path: str | Path) -> bool:
    """True if resolved trailing parts are the B5/B6 governed registry path."""
    target = _resolve(path)
    parts = target.parts
    return len(parts) >= 3 and parts[-3:] == GOVERNED_REGISTRY_RELATIVE_PARTS


def registry_path_is_production(path: str | Path) -> bool:
    """True if path is the governed production registry or otherwise under production .rag_state."""
    target = _resolve(path)
    prod = production_registry_db()
    if target == prod:
        return True
    try:
        target.relative_to(PRODUCTION_RAG_STATE.resolve())
        return True
    except ValueError:
        return False


def _registry_forbidden_component(target: Path) -> str | None:
    for part in target.parts:
        if part in _REGISTRY_FORBIDDEN_COMPONENTS:
            return part
    return None


def assert_certified_registry_path(registry_db: str | Path) -> Path:
    """Allow the governed production registry target and isolated tmp registries.

    The exact B5/B6 path
    ``<library>/.rag_state/metadata_registry/metadata_registry_v1.sqlite3``
    is legal when explicitly passed. Registry files inside ``.rag_db``,
    ``.intake_state``, a Chroma generation directory, a wrong ``.rag_state``
    sub-tree, or a source-library location using the governed filename are
    refused. Does not create directories.
    """
    path = _resolve(registry_db)
    forbidden = _registry_forbidden_component(path)
    if forbidden is not None:
        raise UnsafeGenerationPathError(
            f"certified registry must not resolve inside {forbidden}",
            details={"path": str(path)},
        )

    if is_governed_registry_shape(path):
        return path

    if path.name == GOVERNED_REGISTRY_FILENAME:
        raise UnsafeGenerationPathError(
            "metadata_registry_v1.sqlite3 is governed only at "
            ".rag_state/metadata_registry/metadata_registry_v1.sqlite3",
            details={"path": str(path)},
        )

    if ".rag_state" in path.parts:
        raise UnsafeGenerationPathError(
            "certified registry path uses a wrong .rag_state sub-tree",
            details={"path": str(path)},
        )

    return path
