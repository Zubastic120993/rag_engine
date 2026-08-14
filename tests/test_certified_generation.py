"""B5I certified-generation tests - isolated tmp dirs only. Never production .rag_db."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rag_engine.certified_generation import (
    ExplicitTargetRequiredError,
    LegacyPathForbiddenError,
    UnsafeGenerationPathError,
    assert_not_legacy_production,
    assert_safe_generation_path,
    build_certified_generation,
    build_manifest_from_paths,
    certified_chunk_id,
    certified_chunking_fingerprint,
    certified_vector_metadata,
    compare_generations,
    dirname_to_generation_id,
    generation_id_to_dirname,
    init_certified_generation,
    inspect_generation,
    is_legacy_production_persist,
    make_generation_id,
    parse_generation_id,
    require_explicit_persist_dir,
)
from rag_engine.certified_generation.exceptions import (
    CollisionGuardError,
    DimensionGuardError,
    EmptyGenerationGuardError,
    GenerationIdError,
)
from rag_engine.certified_generation.paths import PRODUCTION_RAG_DB
from rag_engine.index_compatibility.constants import SIDECAR_V1_NAME
from rag_engine.stable_identity import document_id_from_bytes, source_hash_from_bytes

PRODUCTION = Path("/Users/vladymyrzub/CE_Library/.rag_db")


class FakeEmbeddings1024:
    def embed_documents(self, texts):
        out = []
        for t in texts:
            seed = float(len(t) % 97)
            out.append([seed] * 1024)
        return out

    def embed_query(self, text):
        return [float(len(text) % 97)] * 1024


class FakeEmbeddings8:
    def embed_documents(self, texts):
        return [[0.0] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 8


def _long(n: int = 80) -> str:
    return ("alpha bravo charlie delta echo foxtrot golf hotel india juliet. " * n).strip()


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = tmp_path / "lib"
    db = tmp_path / "legacy_db"
    lib.mkdir()
    db.mkdir()
    data = {
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
    scopes = tmp_path / "scopes.yaml"
    scopes.write_text(yaml.dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(db))
    monkeypatch.setenv("RAG_EMBED_MODEL", "mxbai-embed-large")
    monkeypatch.setenv("RAG_EMBED_DIMENSION", "1024")
    import rag_engine.config as cfg

    monkeypatch.setattr(cfg, "SCOPES_FILE", scopes)
    cfg.load_registry.cache_clear()
    assert db.resolve() != PRODUCTION.resolve()
    return tmp_path


def _write_md(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest_for(lib: Path, rels: list[str]) -> dict:
    pairs = [(lib / rel, rel) for rel in rels]
    return build_manifest_from_paths(pairs)


# ---------------------------------------------------------------------------
# Path / target guards
# ---------------------------------------------------------------------------


def test_explicit_target_required() -> None:
    with pytest.raises(ExplicitTargetRequiredError):
        require_explicit_persist_dir(None)
    with pytest.raises(ExplicitTargetRequiredError):
        require_explicit_persist_dir("")


def test_legacy_production_path_forbidden(tmp_path: Path) -> None:
    assert is_legacy_production_persist(PRODUCTION)
    with pytest.raises(LegacyPathForbiddenError):
        assert_not_legacy_production(PRODUCTION)
    with pytest.raises(LegacyPathForbiddenError):
        assert_safe_generation_path(PRODUCTION / "subdir")
    with pytest.raises(LegacyPathForbiddenError):
        init_certified_generation(
            persist_dir=str(PRODUCTION),
            corpus_manifest={"schema": "x", "entries": [], "manifest_sha256": "0" * 64},
        )


def test_unsafe_registry_and_intake_paths() -> None:
    with pytest.raises(UnsafeGenerationPathError):
        assert_safe_generation_path("/Users/vladymyrzub/CE_Library/.rag_state/x")
    with pytest.raises(UnsafeGenerationPathError):
        assert_safe_generation_path("/Users/vladymyrzub/CE_Library/.intake_state/x")


def test_tmp_generation_path_allowed(tmp_path: Path) -> None:
    target = tmp_path / "gen"
    target.mkdir()
    assert assert_safe_generation_path(target) == target.resolve()


def test_skip_dir_includes_generations_and_state() -> None:
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/.rag_db_generations/raggen_x")
    assert should_skip_dir("/Users/x/CE_Library/.rag_state/metadata_registry")
    assert should_skip_dir("/Users/x/CE_Library/.intake_state/cache")


# ---------------------------------------------------------------------------
# Generation ID / document / chunk IDs
# ---------------------------------------------------------------------------


def test_generation_id_format_and_dirname() -> None:
    cfp = certified_chunking_fingerprint()
    gid = make_generation_id(
        embedding_provider="ollama",
        embedding_model="mxbai-embed-large",
        embedding_dimension=1024,
        chunking_fingerprint=cfp,
        corpus_manifest_sha256="a" * 64,
        utcstamp="20260814T120000Z",
    )
    parsed = parse_generation_id(gid)
    assert parsed["utcstamp"] == "20260814T120000Z"
    dirname = generation_id_to_dirname(gid)
    assert dirname.startswith("raggen_20260814T120000Z_")
    assert dirname_to_generation_id(dirname) == gid
    with pytest.raises(GenerationIdError):
        parse_generation_id("mxbai-embed-large")
    with pytest.raises(GenerationIdError):
        parse_generation_id("53db66a05b6cc42becfac832848e56329704c5e6")


def test_document_id_is_bytes_not_path() -> None:
    data = b"same-bytes"
    a = document_id_from_bytes(data)
    b = document_id_from_bytes(data)
    assert a == b == f"docrev:{source_hash_from_bytes(data)}"
    assert a != document_id_from_bytes(b"other")


def test_chunk_id_ordinal_and_cfp_sensitive() -> None:
    doc = document_id_from_bytes(b"doc")
    cfp = certified_chunking_fingerprint()
    a = certified_chunk_id(document_id=doc, chunking_fingerprint=cfp, ordinal=0)
    b = certified_chunk_id(document_id=doc, chunking_fingerprint=cfp, ordinal=0)
    c = certified_chunk_id(document_id=doc, chunking_fingerprint=cfp, ordinal=1)
    assert a == b
    assert a != c
    assert a.startswith("chunk:")
    assert len(a) == 38


def test_certified_metadata_excludes_business_fields() -> None:
    doc = document_id_from_bytes(b"x")
    src = source_hash_from_bytes(b"x")
    cfp = certified_chunking_fingerprint()
    cid = certified_chunk_id(document_id=doc, chunking_fingerprint=cfp, ordinal=0)
    meta = certified_vector_metadata(
        document_id=doc,
        source_hash=src,
        chunk_id=cid,
        embedding_generation_id="raggen:20260814T120000Z:aaaaaaaaaaaa",
        chunking_fingerprint=cfp,
        source="90_CE_Wiki/a.md",
        page=1,
        collection="wiki",
    )
    for k in ("subject_id", "acquisition_id", "aliases", "canonical_path"):
        assert k not in meta
    for k in (
        "document_id",
        "source_hash",
        "chunk_id",
        "embedding_generation_id",
        "chunking_fingerprint",
        "source",
        "page",
        "collection",
    ):
        assert k in meta


# ---------------------------------------------------------------------------
# Init / v1 / unknown legacy
# ---------------------------------------------------------------------------


def test_init_writes_v1_only_in_target(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    rel = "90_CE_Wiki/note.md"
    _write_md(lib, rel, _long())
    manifest = _manifest_for(lib, [rel])
    target = isolated_env / "gen_a"
    result = init_certified_generation(
        persist_dir=target,
        corpus_manifest=manifest,
        utcstamp="20260814T164622Z",
    )
    assert (target / SIDECAR_V1_NAME).is_file()
    assert not (PRODUCTION / SIDECAR_V1_NAME).exists()
    envelope = json.loads((target / SIDECAR_V1_NAME).read_text())
    assert envelope["embedding_contract"]["embedding_model"] == "mxbai-embed-large"
    assert envelope["embedding_contract"]["embedding_model_revision"] is None
    assert envelope["embedding_contract"]["embedding_dimension"] == 1024
    assert result["checkpoint"]["state"] == "PREPARED"
    assert result["checkpoint"]["accepted"] is False
    v0 = json.loads((target / "index_fingerprint.json").read_text())
    assert v0["authority"] == "compatibility_snapshot_only"


def test_init_refuses_random_file_in_target(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    rel = "90_CE_Wiki/note.md"
    _write_md(lib, rel, _long())
    manifest = _manifest_for(lib, [rel])
    target = isolated_env / "dirty"
    target.mkdir()
    (target / "noise.txt").write_text("nope")
    with pytest.raises(EmptyGenerationGuardError):
        init_certified_generation(persist_dir=target, corpus_manifest=manifest)


def test_unknown_legacy_production_untouched() -> None:
    from rag_engine.index_compatibility.compatibility import evaluate_compatibility
    from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly

    if not PRODUCTION.exists():
        pytest.skip("production persist missing")
    count = count_vectors_readonly(PRODUCTION)
    result = evaluate_compatibility(PRODUCTION, vector_count=count)
    assert result.state == "UNKNOWN_LEGACY"


# ---------------------------------------------------------------------------
# Build / dedup / reproducibility
# ---------------------------------------------------------------------------


def test_alias_dedup_one_vector_set(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    text = _long(120)
    _write_md(lib, "90_CE_Wiki/a.md", text)
    _write_md(lib, "90_CE_Wiki/z_alias.md", text)
    _write_md(lib, "90_CE_Wiki/other.md", _long(90) + " UNIQUE OTHER DOCUMENT TEXT")
    manifest = _manifest_for(
        lib, ["90_CE_Wiki/z_alias.md", "90_CE_Wiki/a.md", "90_CE_Wiki/other.md"]
    )
    assert manifest["source_count"] == 3
    target = isolated_env / "gen_dedup"
    result = build_certified_generation(
        persist_dir=target,
        corpus_manifest=manifest,
        embedding_function=FakeEmbeddings1024(),
        utcstamp="20260814T164622Z",
    )
    assert result["document_count"] == 2
    tracker = json.loads((target / "embedded.json").read_text())
    dup_hash = source_hash_from_bytes(text.encode("utf-8"))
    assert set(tracker[dup_hash]["paths"]) == {"90_CE_Wiki/a.md", "90_CE_Wiki/z_alias.md"}
    # display source is lex-smallest alias
    from langchain_chroma import Chroma
    from rag_engine.config import chroma_client_settings

    db = Chroma(
        persist_directory=str(target),
        embedding_function=FakeEmbeddings1024(),
        client_settings=chroma_client_settings(),
    )
    got = db.get(ids=tracker[dup_hash]["chunk_ids"])
    sources = {(m or {}).get("source") for m in got.get("metadatas") or []}
    assert sources == {"90_CE_Wiki/a.md"}
    ids = got.get("ids") or []
    assert all(i.startswith("chunk:") for i in ids)


def test_reproducible_chunk_ids_two_dirs(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    _write_md(lib, "90_CE_Wiki/note.md", _long(100))
    manifest = _manifest_for(lib, ["90_CE_Wiki/note.md"])
    a = isolated_env / "gen_r1"
    b = isolated_env / "gen_r2"
    r1 = build_certified_generation(
        persist_dir=a,
        corpus_manifest=manifest,
        embedding_function=FakeEmbeddings1024(),
        utcstamp="20260814T164622Z",
    )
    r2 = build_certified_generation(
        persist_dir=b,
        corpus_manifest=manifest,
        embedding_function=FakeEmbeddings1024(),
        utcstamp="20260814T164622Z",
    )
    t1 = json.loads((a / "embedded.json").read_text())
    t2 = json.loads((b / "embedded.json").read_text())
    ids1 = next(iter(t1.values()))["chunk_ids"]
    ids2 = next(iter(t2.values()))["chunk_ids"]
    assert ids1 == ids2
    assert r1["generation_id"] == r2["generation_id"]
    assert r1["document_count"] == r2["document_count"]


def test_dimension_guard(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    _write_md(lib, "90_CE_Wiki/note.md", _long())
    manifest = _manifest_for(lib, ["90_CE_Wiki/note.md"])
    with pytest.raises(DimensionGuardError):
        build_certified_generation(
            persist_dir=isolated_env / "gen_bad_dim",
            corpus_manifest=manifest,
            embedding_function=FakeEmbeddings8(),
            utcstamp="20260814T164622Z",
        )


def test_inspect_and_compare_read_only(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    _write_md(lib, "90_CE_Wiki/note.md", _long(90))
    manifest = _manifest_for(lib, ["90_CE_Wiki/note.md"])
    target = isolated_env / "gen_ins"
    build_certified_generation(
        persist_dir=target,
        corpus_manifest=manifest,
        embedding_function=FakeEmbeddings1024(),
        utcstamp="20260814T164622Z",
    )
    before = {(p, p.stat().st_mtime_ns) for p in target.rglob("*") if p.is_file()}
    info = inspect_generation(target)
    cmp = compare_generations(isolated_env / "legacy_db", target)
    after = {(p, p.stat().st_mtime_ns) for p in target.rglob("*") if p.is_file()}
    assert before == after
    assert info["mutated"] is False
    assert cmp["mutated"] is False
    assert info["compatibility_state"] == "KNOWN_COMPATIBLE"
    assert info["checkpoint_state"] == "BUILT_UNVALIDATED"
    assert info["accepted"] is False


def test_registry_bootstrap_temp_only(isolated_env: Path) -> None:
    lib = isolated_env / "lib"
    _write_md(lib, "90_CE_Wiki/note.md", _long(90))
    manifest = _manifest_for(lib, ["90_CE_Wiki/note.md"])
    target = isolated_env / "gen_reg"
    reg = isolated_env / ".rag_state" / "metadata_registry" / "metadata_registry_v1.sqlite3"
    result = build_certified_generation(
        persist_dir=target,
        corpus_manifest=manifest,
        embedding_function=FakeEmbeddings1024(),
        registry_db=reg,
        utcstamp="20260814T164622Z",
    )
    assert reg.is_file()
    # POST_B6: production .rag_state may exist; bootstrap target must stay isolated.
    prod_state = Path("/Users/vladymyrzub/CE_Library/.rag_state")
    assert reg.resolve() != (
        prod_state / "metadata_registry" / "metadata_registry_v1.sqlite3"
    ).resolve()
    assert isolated_env in reg.resolve().parents
    assert result["registry"]["counts"]["versions"] == 1
    from rag_engine.stable_identity.ids import subject_id_pending
    from rag_engine.metadata_registry import open_registry

    conn = open_registry(reg, readonly=True)
    try:
        row = conn.execute("SELECT subject_id FROM documents").fetchone()
        src = next(iter(json.loads((target / "embedded.json").read_text())))
        assert row["subject_id"] == subject_id_pending(src)
        assert str(row["subject_id"]).startswith("subj:pending:")
        mmap = conn.execute("SELECT mapping_status, chroma_embedding_id FROM chunk_vector_map").fetchone()
        assert mmap["mapping_status"] == "native_chunk_id"
        assert mmap["chroma_embedding_id"].startswith("chunk:")
    finally:
        conn.close()


def test_cli_requires_persist_dir() -> None:
    from rag_engine.cli import cmd_generation

    with pytest.raises(SystemExit):
        cmd_generation(["init", "--corpus-manifest", "/tmp/nope.json"])


def test_cli_validate_target_rejects_production() -> None:
    from rag_engine.cli import EXIT_ERROR, cmd_generation

    rc = cmd_generation(["validate-target", "--persist-dir", str(PRODUCTION)])
    assert rc == EXIT_ERROR


def test_cli_inspect_json(isolated_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rag_engine.cli import EXIT_OK, cmd_generation

    lib = isolated_env / "lib"
    _write_md(lib, "90_CE_Wiki/note.md", _long(90))
    manifest = _manifest_for(lib, ["90_CE_Wiki/note.md"])
    man_path = isolated_env / "manifest.json"
    man_path.write_text(json.dumps(manifest), encoding="utf-8")
    target = isolated_env / "gen_cli"
    init_certified_generation(persist_dir=target, corpus_manifest=manifest, utcstamp="20260814T164622Z")
    rc = cmd_generation(["inspect", "--persist-dir", str(target), "--json"])
    assert rc == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["v1_present"] is True
    assert payload["mutated"] is False


def test_tracker_id_kind_dual() -> None:
    from rag_engine.certified_generation.tracker import tracker_id_kind

    assert tracker_id_kind("chunk:" + "a" * 32) == "certified_chunk_id"
    assert tracker_id_kind("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == "legacy_uuid"


def test_collision_duplicate_chunk_id() -> None:
    from rag_engine.certified_generation.metadata import assert_no_chunk_id_collision

    doc = document_id_from_bytes(b"x")
    src = source_hash_from_bytes(b"x")
    cfp = certified_chunking_fingerprint()
    cid = certified_chunk_id(document_id=doc, chunking_fingerprint=cfp, ordinal=0)
    meta = certified_vector_metadata(
        document_id=doc,
        source_hash=src,
        chunk_id=cid,
        embedding_generation_id="raggen:20260814T120000Z:aaaaaaaaaaaa",
        chunking_fingerprint=cfp,
        source="a.md",
        page=1,
        collection="other",
    )
    seen: dict = {}
    assert_no_chunk_id_collision(seen, meta)
    with pytest.raises(CollisionGuardError):
        assert_no_chunk_id_collision(seen, meta)


def test_corpus_drift_detected(isolated_env: Path) -> None:
    from rag_engine.certified_generation.corpus import verify_manifest_against_disk
    from rag_engine.certified_generation.exceptions import CorpusManifestError

    lib = isolated_env / "lib"
    p = _write_md(lib, "90_CE_Wiki/note.md", _long())
    manifest = _manifest_for(lib, ["90_CE_Wiki/note.md"])
    p.write_text(_long() + " changed", encoding="utf-8")
    with pytest.raises(CorpusManifestError):
        verify_manifest_against_disk(manifest)


def test_query_py_not_imported_by_certified_package() -> None:
    import rag_engine.certified_generation as cg
    import inspect

    src = inspect.getsource(cg)
    assert "rag_engine.query" not in src
