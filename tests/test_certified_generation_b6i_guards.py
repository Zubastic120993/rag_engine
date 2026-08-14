"""B6I certified production target guards. Isolated tmp / path classification only.

POST_B6: production ``.rag_db_generations`` and ``.rag_state`` exist as legitimate
B6 artifacts. Guards must classify paths without creating test children or mutating
those directories. Do not assert global absence of post-B6 production state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_engine.certified_generation.exceptions import (
    ExplicitTargetRequiredError,
    LegacyPathForbiddenError,
    UnsafeGenerationPathError,
)
from rag_engine.certified_generation.paths import (
    PRODUCTION_GENERATIONS_ROOT,
    PRODUCTION_INTAKE_STATE,
    PRODUCTION_LIBRARY_ROOT,
    PRODUCTION_RAG_DB,
    PRODUCTION_RAG_STATE,
    PRODUCTION_REGISTRY_DB,
    assert_certified_persist_dir,
    assert_certified_registry_path,
    assert_not_production_generations_root,
    assert_safe_generation_path,
    is_contained_generation_child,
    is_governed_registry_shape,
    require_explicit_persist_dir,
)
from rag_engine.certified_generation.registry_bootstrap import assert_temp_registry_path

PROD_CHILD = PRODUCTION_GENERATIONS_ROOT / "raggen_TEST_123"
PROD_SIBLING = PRODUCTION_LIBRARY_ROOT / "raggen_TEST_123"
PLANNED_B6_CHILD = (
    PRODUCTION_GENERATIONS_ROOT / "raggen_20260814T170712Z_698e0df44604"
)
ACCEPTED_B6_CHILD = (
    PRODUCTION_GENERATIONS_ROOT / "raggen_20260814T182037Z_698e0df44604"
)


def _lib(tmp_path: Path) -> Path:
    lib = tmp_path / "library"
    lib.mkdir()
    return lib


def _assert_post_b6_roots_present() -> None:
    """Closed B6 facts: generations root + rag_state are legitimate production state."""
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()
    assert not PRODUCTION_GENERATIONS_ROOT.is_symlink()
    assert PRODUCTION_RAG_STATE.is_dir()
    assert not PRODUCTION_RAG_STATE.is_symlink()
    assert ACCEPTED_B6_CHILD.is_dir()
    assert (ACCEPTED_B6_CHILD / "index_embedding_fingerprint_v1.json").is_file()
    assert PRODUCTION_REGISTRY_DB.is_file()
    assert PRODUCTION_RAG_DB.is_dir()
    assert not (PRODUCTION_RAG_DB / "index_embedding_fingerprint_v1.json").exists()


def _assert_guard_did_not_create_test_child() -> None:
    assert not PROD_CHILD.exists()
    assert not PROD_SIBLING.exists()


def test_explicit_persist_dir_still_required() -> None:
    with pytest.raises(ExplicitTargetRequiredError):
        assert_certified_persist_dir(None)
    with pytest.raises(ExplicitTargetRequiredError):
        assert_certified_persist_dir("")
    with pytest.raises(ExplicitTargetRequiredError):
        require_explicit_persist_dir(None)


def test_allow_production_generation_child_without_creating() -> None:
    allowed = assert_certified_persist_dir(PROD_CHILD)
    assert allowed == PROD_CHILD.resolve()
    assert is_contained_generation_child(PROD_CHILD) is True
    _assert_post_b6_roots_present()
    _assert_guard_did_not_create_test_child()


def test_allow_planned_b6_generation_child_without_creating() -> None:
    allowed = assert_certified_persist_dir(PLANNED_B6_CHILD)
    assert allowed == PLANNED_B6_CHILD.resolve()
    _assert_post_b6_roots_present()
    # Path classification must not mkdir the stopped-B6 planned child.
    assert not PLANNED_B6_CHILD.exists()


def test_reject_production_generations_root() -> None:
    with pytest.raises(UnsafeGenerationPathError, match="root"):
        assert_certified_persist_dir(PRODUCTION_GENERATIONS_ROOT)
    with pytest.raises(UnsafeGenerationPathError, match="root"):
        assert_not_production_generations_root(PRODUCTION_GENERATIONS_ROOT)
    assert is_contained_generation_child(PRODUCTION_GENERATIONS_ROOT) is False
    # POST_B6: root exists but remains forbidden as persist-dir.
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()


def test_reject_production_rag_db() -> None:
    with pytest.raises(LegacyPathForbiddenError):
        assert_certified_persist_dir(PRODUCTION_RAG_DB)
    with pytest.raises(LegacyPathForbiddenError):
        assert_safe_generation_path(PRODUCTION_RAG_DB)


def test_reject_production_rag_state_and_intake_as_generation() -> None:
    with pytest.raises(UnsafeGenerationPathError):
        assert_certified_persist_dir(PRODUCTION_RAG_STATE)
    with pytest.raises(UnsafeGenerationPathError):
        assert_certified_persist_dir(PRODUCTION_INTAKE_STATE)


def test_fixture_library_generation_child_allowed(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    child = lib / ".rag_db_generations" / "raggen_TEST_123"
    child.mkdir(parents=True)
    assert assert_certified_persist_dir(child) == child.resolve()
    assert is_contained_generation_child(
        child, generations_root=lib / ".rag_db_generations"
    )


def test_fixture_library_generations_root_rejected(tmp_path: Path) -> None:
    root = _lib(tmp_path) / ".rag_db_generations"
    root.mkdir()
    with pytest.raises(UnsafeGenerationPathError, match="root"):
        assert_certified_persist_dir(root)


def test_fixture_library_forbidden_siblings(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    for name in (".rag_db", ".rag_state", ".intake_state"):
        target = lib / name
        target.mkdir()
        with pytest.raises((LegacyPathForbiddenError, UnsafeGenerationPathError)):
            assert_certified_persist_dir(target)


def test_nested_child_stays_inside_generations_root(tmp_path: Path) -> None:
    gens = _lib(tmp_path) / ".rag_db_generations"
    nested = gens / "raggen_TEST_123" / "nested"
    nested.mkdir(parents=True)
    assert is_contained_generation_child(nested, generations_root=gens) is True
    assert assert_certified_persist_dir(nested) == nested.resolve()


def test_path_traversal_cannot_escape_to_rag_db() -> None:
    escaped = PRODUCTION_GENERATIONS_ROOT / "raggen_TEST_123" / ".." / ".." / ".rag_db"
    with pytest.raises(LegacyPathForbiddenError):
        assert_certified_persist_dir(escaped)
    assert is_contained_generation_child(escaped) is False
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()
    _assert_guard_did_not_create_test_child()


def test_path_traversal_cannot_escape_to_corpus() -> None:
    escaped = PRODUCTION_GENERATIONS_ROOT / "raggen_TEST_123" / ".." / ".." / "00_Career"
    with pytest.raises(UnsafeGenerationPathError):
        assert_certified_persist_dir(escaped)
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()
    _assert_guard_did_not_create_test_child()


def test_symlink_escape_to_rag_db_rejected(tmp_path: Path) -> None:
    gens = tmp_path / ".rag_db_generations"
    gens.mkdir()
    link = gens / "raggen_TEST_link"
    link.symlink_to(PRODUCTION_RAG_DB)
    with pytest.raises((LegacyPathForbiddenError, UnsafeGenerationPathError)):
        assert_certified_persist_dir(link)
    assert is_contained_generation_child(link, generations_root=gens) is False


def test_arbitrary_sibling_not_a_generation_child() -> None:
    assert is_contained_generation_child(PROD_SIBLING) is False
    with pytest.raises(UnsafeGenerationPathError, match="contained child"):
        assert_certified_persist_dir(PROD_SIBLING)
    assert not PROD_SIBLING.exists()


def test_isolated_tmp_generation_still_allowed(tmp_path: Path) -> None:
    target = tmp_path / "gen"
    target.mkdir()
    assert assert_certified_persist_dir(target) == target.resolve()


def test_allow_exact_governed_production_registry_without_creating() -> None:
    allowed = assert_certified_registry_path(PRODUCTION_REGISTRY_DB)
    assert allowed == PRODUCTION_REGISTRY_DB.resolve()
    assert assert_temp_registry_path(PRODUCTION_REGISTRY_DB) == allowed
    assert is_governed_registry_shape(PRODUCTION_REGISTRY_DB) is True
    # POST_B6: production registry exists; classification must not mkdir test children.
    assert PRODUCTION_RAG_STATE.is_dir()
    assert PRODUCTION_REGISTRY_DB.is_file()
    _assert_guard_did_not_create_test_child()


def test_allow_fixture_governed_registry_shape(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    reg = lib / ".rag_state" / "metadata_registry" / "metadata_registry_v1.sqlite3"
    assert assert_certified_registry_path(reg) == reg.resolve()
    # Fixture classification must not mutate production .rag_state.
    assert PRODUCTION_RAG_STATE.is_dir()
    _assert_guard_did_not_create_test_child()


def test_reject_registry_under_rag_db(tmp_path: Path) -> None:
    with pytest.raises(UnsafeGenerationPathError, match=".rag_db"):
        assert_certified_registry_path(PRODUCTION_RAG_DB / "metadata_registry_v1.sqlite3")
    lib = _lib(tmp_path)
    with pytest.raises(UnsafeGenerationPathError, match=".rag_db"):
        assert_certified_registry_path(
            lib / ".rag_db" / "metadata_registry" / "metadata_registry_v1.sqlite3"
        )


def test_reject_registry_under_intake_state(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(UnsafeGenerationPathError, match=".intake_state"):
        assert_certified_registry_path(
            lib / ".intake_state" / "metadata_registry" / "metadata_registry_v1.sqlite3"
        )


def test_reject_registry_inside_generation(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(UnsafeGenerationPathError, match=".rag_db_generations"):
        assert_certified_registry_path(
            lib
            / ".rag_db_generations"
            / "raggen_TEST_123"
            / "metadata_registry_v1.sqlite3"
        )


def test_reject_registry_in_source_library(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(UnsafeGenerationPathError, match="governed only"):
        assert_certified_registry_path(
            lib / "00_Career" / "metadata_registry_v1.sqlite3"
        )


def test_reject_wrong_rag_state_subtree(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(UnsafeGenerationPathError, match="governed only"):
        assert_certified_registry_path(
            lib / ".rag_state" / "other" / "metadata_registry_v1.sqlite3"
        )
    with pytest.raises(UnsafeGenerationPathError, match="wrong .rag_state"):
        assert_certified_registry_path(lib / ".rag_state" / "other.sqlite3")


def test_reject_wrong_registry_filename_at_governed_dir(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    with pytest.raises(UnsafeGenerationPathError, match="wrong .rag_state"):
        assert_certified_registry_path(
            lib / ".rag_state" / "metadata_registry" / "wrong_name.sqlite3"
        )


def test_registry_symlink_escape_to_rag_db_rejected(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    dest = lib / ".rag_db" / "chroma.sqlite3"
    dest.parent.mkdir()
    dest.write_bytes(b"not-a-registry")
    link = lib / ".rag_state" / "metadata_registry" / "metadata_registry_v1.sqlite3"
    link.parent.mkdir(parents=True)
    link.symlink_to(dest)
    with pytest.raises(UnsafeGenerationPathError, match=".rag_db"):
        assert_certified_registry_path(link)


def test_isolated_tmp_registry_non_governed_filename_allowed(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated_registry.sqlite3"
    assert assert_temp_registry_path(isolated) == isolated.resolve()


def test_production_registry_only_when_explicitly_passed() -> None:
    from rag_engine.certified_generation.registry_bootstrap import bootstrap_registry_db

    # Classification of the production target does not create test children.
    assert_certified_registry_path(PRODUCTION_REGISTRY_DB)
    assert PRODUCTION_RAG_STATE.is_dir()
    _assert_guard_did_not_create_test_child()
    # bootstrap is not invoked here; callers must pass the path explicitly.
    assert bootstrap_registry_db.__code__.co_varnames[0] == "registry_db"


def test_import_does_not_mkdir_rag_state() -> None:
    import importlib

    import rag_engine.certified_generation as cg
    import rag_engine.certified_generation.registry_bootstrap as rb

    # POST_B6: roots exist; reload must not create test children or remove roots.
    state_mtime = PRODUCTION_RAG_STATE.stat().st_mtime_ns
    gens_mtime = PRODUCTION_GENERATIONS_ROOT.stat().st_mtime_ns
    importlib.reload(cg)
    importlib.reload(rb)
    assert PRODUCTION_RAG_STATE.is_dir()
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()
    assert PRODUCTION_RAG_STATE.stat().st_mtime_ns == state_mtime
    assert PRODUCTION_GENERATIONS_ROOT.stat().st_mtime_ns == gens_mtime
    _assert_guard_did_not_create_test_child()


def test_cli_validate_target_allows_generation_child() -> None:
    from rag_engine.cli import EXIT_OK, cmd_generation

    rc = cmd_generation(["validate-target", "--persist-dir", str(PROD_CHILD)])
    assert rc == EXIT_OK
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()
    _assert_guard_did_not_create_test_child()


def test_cli_validate_target_rejects_generations_root() -> None:
    from rag_engine.cli import EXIT_ERROR, cmd_generation

    rc = cmd_generation(["validate-target", "--persist-dir", str(PRODUCTION_GENERATIONS_ROOT)])
    assert rc == EXIT_ERROR
    # POST_B6: root remains present; validate-target still rejects it as persist-dir.
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()


def test_cli_validate_target_rejects_production_rag_db() -> None:
    from rag_engine.cli import EXIT_ERROR, cmd_generation

    rc = cmd_generation(["validate-target", "--persist-dir", str(PRODUCTION_RAG_DB)])
    assert rc == EXIT_ERROR


def test_cli_validate_and_init_share_child_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rag_engine.cli import EXIT_OK, cmd_generation
    from rag_engine.certified_generation.build import init_certified_generation
    from rag_engine.certified_generation.corpus import build_manifest_from_paths
    import yaml
    import rag_engine.config as cfg

    lib = tmp_path / "lib"
    lib.mkdir()
    db = tmp_path / "legacy_db"
    db.mkdir()
    scopes = tmp_path / "scopes.yaml"
    scopes.write_text(
        yaml.dump(
            {
                "defaults": {
                    "library_root_env": "CE_LIBRARY_ROOT",
                    "library_root_default": str(lib),
                    "db_path_env": "RAG_DB_PATH",
                    "db_path_default": None,
                    "embed_model_env": "RAG_EMBED_MODEL",
                    "embed_model_default": "mxbai-embed-large",
                    "llm_model_env": "RAG_LLM_MODEL",
                    "llm_model_default": "gpt-5.6-luna",
                    "chunk_size": 800,
                    "chunk_overlap": 100,
                    "default_k": 5,
                },
                "scopes": {
                    "wiki": {
                        "description": "wiki",
                        "hermes_aliases": [],
                        "path_prefixes": ["90_CE_Wiki/"],
                        "include_extensions": [".md"],
                    },
                    "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
                },
                "prefix_order": ["wiki"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(db))
    monkeypatch.setenv("RAG_EMBED_MODEL", "mxbai-embed-large")
    monkeypatch.setenv("RAG_EMBED_DIMENSION", "1024")
    monkeypatch.setattr(cfg, "SCOPES_FILE", scopes)
    cfg.load_registry.cache_clear()

    note = lib / "90_CE_Wiki" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("alpha bravo charlie delta echo foxtrot golf hotel india juliet. " * 80)
    child = tmp_path / ".rag_db_generations" / "raggen_TEST_123"
    rc = cmd_generation(["validate-target", "--persist-dir", str(child)])
    assert rc == EXIT_OK
    manifest = build_manifest_from_paths([(note, "90_CE_Wiki/note.md")])
    result = init_certified_generation(
        persist_dir=child,
        corpus_manifest=manifest,
        utcstamp="20260814T172132Z",
    )
    assert Path(result["persist_dir"]) == child.resolve()
    assert PRODUCTION_GENERATIONS_ROOT.is_dir()
    assert PRODUCTION_RAG_STATE.is_dir()
    # Isolated init must not create the production test child path.
    _assert_guard_did_not_create_test_child()


def test_registry_bootstrap_apis_share_policy() -> None:
    from rag_engine.certified_generation.registry_bootstrap import (
        bootstrap_certified_documents,
        bootstrap_registry_db,
    )

    for fn in (assert_temp_registry_path, bootstrap_registry_db, bootstrap_certified_documents):
        src = fn.__code__.co_names
        assert "assert_temp_registry_path" in src or fn is assert_temp_registry_path or True
    # Direct: both bootstraps call assert_temp_registry_path which uses certified policy.
    import inspect

    src = inspect.getsource(bootstrap_registry_db)
    src2 = inspect.getsource(bootstrap_certified_documents)
    assert "assert_temp_registry_path" in src
    assert "assert_temp_registry_path" in src2


def test_production_paths_unchanged_by_guard_calls() -> None:
    """POST_B6: production roots exist; guard classification must not mutate them."""
    _assert_post_b6_roots_present()
    gens_mtime = PRODUCTION_GENERATIONS_ROOT.stat().st_mtime_ns
    state_mtime = PRODUCTION_RAG_STATE.stat().st_mtime_ns
    reg_mtime = PRODUCTION_REGISTRY_DB.stat().st_mtime_ns
    assert_certified_persist_dir(PROD_CHILD)
    assert_certified_registry_path(PRODUCTION_REGISTRY_DB)
    _assert_guard_did_not_create_test_child()
    assert PRODUCTION_GENERATIONS_ROOT.stat().st_mtime_ns == gens_mtime
    assert PRODUCTION_RAG_STATE.stat().st_mtime_ns == state_mtime
    assert PRODUCTION_REGISTRY_DB.stat().st_mtime_ns == reg_mtime
    assert not (PRODUCTION_RAG_DB / "index_embedding_fingerprint_v1.json").exists()
