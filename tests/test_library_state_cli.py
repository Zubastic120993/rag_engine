"""Read-only ce-library-manager package CLI tests - isolated fixtures only."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rag_engine.library_state import resolve_library_state
from rag_engine.library_state.contract import (
    CLASS_AMBIGUOUS,
    CLASS_NOT_INDEXED,
    INTENT_CHECK,
    INTENT_PLAN_ADD,
    INTENT_PLAN_DELETE,
    INTENT_PLAN_MOVE,
    INTENT_PLAN_RECONCILE,
    INTENT_PLAN_RENAME,
    INTENT_VERIFY,
    OP_MANUAL_REVIEW,
    OP_NO_OP,
    SUPPORTED_INTENTS,
)
from rag_engine.library_state.__main__ import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_RESOLVER,
    NOT_AUTHORIZED,
    READ_ONLY_BOUNDARY,
    main,
)
from rag_engine.stable_identity import source_hash_from_bytes

ROOT = Path(__file__).resolve().parents[1]
NEW = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_new_name.pdf"
BYTES_A = b"%PDF-1.4\nMAN Academy LGIP A\n"
HASH_A = source_hash_from_bytes(BYTES_A)

MUTATION_FLAGS = ("--execute", "--apply", "--approve", "--route", "--repair")


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(
    *,
    library: Path,
    persist: Path,
    registry: Path,
) -> dict:
    files: dict[str, str] = {}
    dirs: dict[str, list[str] | None] = {}

    def add_file(path: Path) -> None:
        if path.is_file():
            files[str(path)] = _file_sha(path)

    def add_dir(path: Path) -> None:
        if path.is_dir():
            dirs[str(path)] = sorted(p.name for p in path.iterdir())
        else:
            dirs[str(path)] = None

    add_file(library / NEW)
    add_dir(library)
    add_file(registry)
    add_dir(registry.parent)
    add_file(persist / "embedded.json")
    add_file(persist / "chroma.sqlite3")
    add_file(persist / "ingest.lock")
    add_file(persist / "certified_append.lock")
    add_dir(persist)
    journal_dir = persist / "certified_append_journal"
    add_dir(journal_dir)
    if persist.is_dir():
        for p in sorted(persist.rglob("*")):
            if p.is_file():
                add_file(p)
            elif p.is_dir():
                add_dir(p)
    return {"files": files, "dirs": dirs}


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    registry = tmp_path / "reg" / "metadata_registry_v1.sqlite3"
    library.mkdir()
    persist.mkdir()
    registry.parent.mkdir()
    return {
        "library": library,
        "persist": persist,
        "registry": registry,
        "tracker": persist / "embedded.json",
    }


def _base_args(env: dict) -> list[str]:
    _write(env["library"], NEW, BYTES_A)
    return [
        "--intent",
        INTENT_CHECK,
        "--library-root",
        str(env["library"].resolve()),
        "--target",
        NEW,
        "--persist-dir",
        str(env["persist"].resolve()),
        "--registry-db",
        str(env["registry"].resolve()),
    ]


def _run_module(args: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rag_engine.library_state", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _run_with_snapshot(env: dict, args: list[str]) -> tuple[int, str, str]:
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    proc = _run_module(args)
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    assert after == before, "CLI mutated isolated fixture stores"
    return proc.returncode, proc.stdout, proc.stderr


def test_package_help() -> None:
    proc = _run_module(["--help"])
    assert proc.returncode == 0
    assert "--intent" in proc.stdout
    assert "--library-root" in proc.stdout
    assert "read-only" in proc.stdout.lower() or "Read-only" in proc.stdout


def test_json_output_contains_required_fields(env) -> None:
    code, out, err = _run_with_snapshot(env, _base_args(env))
    assert code == EXIT_OK, err
    payload = json.loads(out)
    for key in (
        "request",
        "classification",
        "result",
        "evidence_summary",
        "evidence_gaps",
        "proposed_operation",
        "approval",
        "embedding_action",
        "verification_contract",
        "execution_boundary",
    ):
        assert key in payload, key
    assert payload["classification"] == CLASS_NOT_INDEXED
    assert payload["proposed_operation"] == OP_NO_OP
    assert payload["execution_boundary"].startswith("Read-only completion")
    assert payload["read_only"] is True
    assert payload["mutation_performed"] is False


def test_text_output_contains_operator_sections(env) -> None:
    args = _base_args(env) + ["--format", "text"]
    code, out, err = _run_with_snapshot(env, args)
    assert code == EXIT_OK, err
    for section in (
        "### Request",
        "### Observed state",
        "### Evidence and gaps",
        "### Proposed operation",
        "### Execution authority",
        "### Stop boundary",
    ):
        assert section in out
    assert "Read-only completion" in out or NOT_AUTHORIZED in out


@pytest.mark.parametrize(
    "intent",
    sorted(SUPPORTED_INTENTS),
)
def test_all_supported_intents_accepted(env, intent: str) -> None:
    args = _base_args(env)
    args[args.index("--intent") + 1] = intent
    code, _, err = _run_with_snapshot(env, args)
    assert code == EXIT_OK, err


def test_unsupported_intent_fails(env) -> None:
    args = _base_args(env)
    args[args.index("--intent") + 1] = "plan_ingest"
    code, _, err = _run_with_snapshot(env, args)
    assert code == EXIT_INPUT
    assert "plan_ingest" in err or "invalid choice" in err.lower()


def test_missing_required_input_fails(env) -> None:
    code = main(["--intent", INTENT_CHECK])
    assert code == EXIT_INPUT


def test_relative_library_root_rejected_before_resolver(env, monkeypatch) -> None:
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("resolver must not run for relative library_root")

    monkeypatch.setattr(
        "rag_engine.library_state.__main__.resolve_library_state",
        _boom,
    )
    code = main(
        [
            "--intent",
            INTENT_CHECK,
            "--library-root",
            "relative/lib",
            "--target",
            NEW,
        ]
    )
    assert code == EXIT_INPUT
    assert called["n"] == 0


def test_relative_persist_dir_rejected(env, monkeypatch) -> None:
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("resolver must not run for relative persist_dir")

    monkeypatch.setattr(
        "rag_engine.library_state.__main__.resolve_library_state",
        _boom,
    )
    code = main(
        [
            "--intent",
            INTENT_CHECK,
            "--library-root",
            str(env["library"].resolve()),
            "--target",
            NEW,
            "--persist-dir",
            "relative/persist",
        ]
    )
    assert code == EXIT_INPUT
    assert called["n"] == 0


def test_relative_registry_db_rejected(env, monkeypatch) -> None:
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("resolver must not run for relative registry_db")

    monkeypatch.setattr(
        "rag_engine.library_state.__main__.resolve_library_state",
        _boom,
    )
    code = main(
        [
            "--intent",
            INTENT_CHECK,
            "--library-root",
            str(env["library"].resolve()),
            "--target",
            NEW,
            "--registry-db",
            "relative/registry.sqlite",
        ]
    )
    assert code == EXIT_INPUT
    assert called["n"] == 0


def test_absolute_target_rejected(env, monkeypatch) -> None:
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("resolver must not run for absolute target")

    monkeypatch.setattr(
        "rag_engine.library_state.__main__.resolve_library_state",
        _boom,
    )
    target = str((env["library"] / NEW).resolve())
    code = main(
        [
            "--intent",
            INTENT_CHECK,
            "--library-root",
            str(env["library"].resolve()),
            "--target",
            target,
        ]
    )
    assert code == EXIT_INPUT
    assert called["n"] == 0


def test_traversal_target_rejected(env, monkeypatch) -> None:
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("resolver must not run for traversal target")

    monkeypatch.setattr(
        "rag_engine.library_state.__main__.resolve_library_state",
        _boom,
    )
    code = main(
        [
            "--intent",
            INTENT_CHECK,
            "--library-root",
            str(env["library"].resolve()),
            "--target",
            "../escape.pdf",
        ]
    )
    assert code == EXIT_INPUT
    assert called["n"] == 0


@pytest.mark.parametrize("flag", MUTATION_FLAGS)
def test_mutation_like_flags_fail_closed(env, flag: str) -> None:
    args = _base_args(env) + [flag]
    code, _, err = _run_with_snapshot(env, args)
    assert code == EXIT_INPUT
    assert READ_ONLY_BOUNDARY in err


def test_ambiguous_plan_is_proposal_only_with_exit_zero(env) -> None:
    _write(env["library"], NEW, BYTES_A)
    args = [
        "--intent",
        INTENT_PLAN_MOVE,
        "--library-root",
        str(env["library"].resolve()),
        "--target",
        NEW,
    ]
    code, out, err = _run_with_snapshot(env, args)
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["classification"] == CLASS_AMBIGUOUS
    assert payload["proposed_operation"] == OP_MANUAL_REVIEW
    assert NOT_AUTHORIZED in payload["execution_boundary"]


def test_missing_library_root_directory_returns_resolver_exit(env) -> None:
    missing = env["library"].parent / "missing_lib"
    args = [
        "--intent",
        INTENT_CHECK,
        "--library-root",
        str(missing.resolve()),
        "--target",
        NEW,
    ]
    code, _, err = _run_with_snapshot(env, args)
    assert code == EXIT_RESOLVER
    assert "not a directory" in err


def test_direct_main_json_and_text_invocations(env) -> None:
    args = _base_args(env)
    assert main(args) == EXIT_OK
    assert main(args + ["--format", "text"]) == EXIT_OK


def test_no_write_via_direct_resolver_parity(env) -> None:
    """Sanity: resolver remains read-only; CLI wraps the same entry point."""
    _write(env["library"], NEW, BYTES_A)
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    resolve_library_state(
        INTENT_VERIFY,
        [NEW],
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
    )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    assert after == before
