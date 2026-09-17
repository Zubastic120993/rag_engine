from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))
import lc6_operational_controls as c


class FakeCollection:
    def __init__(self, rows, *, total=None, order=None):
        self.rows = [(str(i), list(v)) for i, v in rows]
        self.total = len(self.rows) if total is None else total
        self.order = order or list(range(len(self.rows)))

    def count(self):
        return self.total

    def get(self, *, include, limit, offset):
        assert include == ["embeddings"]
        ordered = [self.rows[i] for i in self.order]
        page = ordered[offset:offset + limit]
        return {"ids": [i for i, _v in page], "embeddings": [v for _i, v in page]}


def provider_from_rows(rows, *, total=None, order=None, names=("langchain",)):
    def provider(_gen, expected_collection):
        if list(names) != [expected_collection]:
            raise c.ControlRefusal(f"expected exactly collection {expected_collection!r}, got {sorted(names)}")
        return FakeCollection(rows, total=total, order=order), {"provider": "test-public-collection-api", "collection_names": list(names)}
    return provider


def sqlite_test_provider(gen, expected_collection):
    conn = sqlite3.connect(Path(gen) / "chroma.sqlite3")
    try:
        rows = [(r[0], list(struct.unpack("<" + "f" * (len(r[1]) // 4), bytes(r[1])))) for r in conn.execute("select embedding_id, embedding from embeddings order by id").fetchall()]
    finally:
        conn.close()
    return provider_from_rows(rows)(gen, expected_collection)


def make_generation(root: Path, lib: Path, name: str = "prod_gen") -> Path:
    gen = root / name
    gen.mkdir()
    live = lib / "doc.txt"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("live", encoding="utf-8")
    (gen / "embedded.json").write_text(json.dumps({"digest1": {"paths": ["doc.txt"], "chunk_ids": ["c1"]}, "digest2": {"paths": ["missing.txt"], "chunk_ids": ["c2"]}}), encoding="utf-8")
    db = sqlite3.connect(gen / "chroma.sqlite3")
    try:
        db.execute("create table collections (id text, name text)")
        db.execute("insert into collections values ('1','langchain')")
        db.execute("create table embeddings (id integer primary key, embedding_id text, embedding blob)")
        db.execute("insert into embeddings (embedding_id, embedding) values ('c1', ?)", (struct.pack('<ff', 1.0, 2.0),))
        db.execute("insert into embeddings (embedding_id, embedding) values ('c2', ?)", (struct.pack('<ff', 3.0, 4.0),))
        db.execute("create table embedding_metadata (id integer, key text, string_value text)")
        db.execute("insert into embedding_metadata values (1,'source_hash','digest1')")
        db.commit()
    finally:
        db.close()
    return gen


@pytest.fixture()
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    private = Path("/private/tmp") / f"lc6_controls_test_{os.getpid()}_{tmp_path.name}"
    private.mkdir(parents=True, exist_ok=True)
    gen = make_generation(private, lib)
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(gen))
    monkeypatch.setattr(c, "EMBEDDING_COLLECTION_PROVIDER", sqlite_test_provider)
    return private, lib, gen


def test_embedding_provider_deterministic_pagination_and_page_order_independence(tmp_path):
    gen = Path("/private/tmp") / f"lc6_provider_test_{os.getpid()}_{tmp_path.name}"
    gen.mkdir(parents=True, exist_ok=True)
    rows = [("b", [2.0, 3.0]), ("a", [1.0, 4.0]), ("c", [5.0, 6.0])]
    forward = c.embedding_payload_digest(gen, page_size=2, collection_provider=provider_from_rows(rows))
    shuffled = c.embedding_payload_digest(gen, page_size=1, collection_provider=provider_from_rows(rows, order=[1, 2, 0]))
    assert forward["sha256"] == shuffled["sha256"]
    assert forward["ordered_vector_ids"] == ["a", "b", "c"]
    assert forward["vector_count"] == 3
    assert forward["ordered_id_digest"] == shuffled["ordered_id_digest"]
    assert forward["pagination"]["fetched"] == 3


def test_embedding_provider_one_float_changes_digest_with_ids_and_count_fixed(tmp_path):
    gen = Path("/private/tmp") / f"lc6_provider_float_{os.getpid()}_{tmp_path.name}"
    gen.mkdir(parents=True, exist_ok=True)
    before = c.embedding_payload_digest(gen, collection_provider=provider_from_rows([("a", [1.0, 2.0]), ("b", [3.0, 4.0])]))
    after = c.embedding_payload_digest(gen, collection_provider=provider_from_rows([("a", [1.5, 2.0]), ("b", [3.0, 4.0])]))
    assert after["ordered_vector_ids"] == before["ordered_vector_ids"]
    assert after["vector_count"] == before["vector_count"]
    assert after["dtype"] == before["dtype"] == "float32-le"
    assert after["dimensions"] == before["dimensions"]
    assert after["sha256"] != before["sha256"]


def test_embedding_provider_duplicate_missing_and_nonfinite_fail(tmp_path):
    gen = Path("/private/tmp") / f"lc6_provider_fail_{os.getpid()}_{tmp_path.name}"
    gen.mkdir(parents=True, exist_ok=True)
    with pytest.raises(c.ControlRefusal, match="duplicate ID"):
        c.embedding_payload_digest(gen, collection_provider=provider_from_rows([("a", [1.0]), ("a", [2.0])]))
    with pytest.raises(c.ControlRefusal, match="incomplete pagination"):
        c.embedding_payload_digest(gen, collection_provider=provider_from_rows([("a", [1.0])], total=2))
    with pytest.raises(c.ControlRefusal, match="non-finite"):
        c.embedding_payload_digest(gen, collection_provider=provider_from_rows([("a", [math.inf])]))


def test_embedding_provider_refuses_production_and_certified_baseline_chroma(monkeypatch, tmp_path):
    lib = tmp_path / "lib"; lib.mkdir()
    prod = Path("/private/tmp") / f"lc6_provider_prod_{os.getpid()}_{tmp_path.name}"; prod.mkdir(parents=True)
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(prod))
    with pytest.raises(c.ControlRefusal, match="protected generation"):
        c.embedding_payload_digest(prod)
    with pytest.raises(c.ControlRefusal, match="protected generation"):
        c._assert_chroma_public_api_allowed(c.CERTIFIED_EXTERNAL_BASELINE)


def test_inventory_v1_schema_digest_is_canonical(env):
    _tmp, _lib, gen = env
    rows = c.inventory_rows(gen)
    assert all(set(r) == {"path", "type", "bytes", "sha256"} for r in rows)
    assert c.inventory_digest(rows)
    with pytest.raises(c.ControlRefusal, match="missing/null"):
        c.validate_inventory_schema(None)
    with pytest.raises(c.ControlRefusal, match="empty"):
        c.validate_inventory_schema("")
    with pytest.raises(c.ControlRefusal, match="unknown"):
        c.validate_inventory_schema("unknown")


def test_b1_v2_process_scan_excludes_self_and_harmless_reader(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    ps_output = f"100 python b1_inspection {baseline}\n200 python read-only-check {baseline}\n"
    scan = c.process_scan([baseline], ps_output=ps_output, lsof_output="", current_pid=100, ancestor_pids={100})
    assert [m["activity"] for m in scan["matches"]] == ["self_or_wrapper", "reader"]
    assert scan["active_writers_or_ambiguous"] == []


def test_b1_v2_process_scan_detects_genuine_writer_fd(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    db = baseline / "chroma.sqlite3"
    ps_output = f"300 harmless-tool {baseline}\n"
    lsof_output = f"p300\ncpython\nf3u\ntREG\nn{db}\n"
    scan = c.process_scan([baseline], ps_output=ps_output, lsof_output=lsof_output, current_pid=100, ancestor_pids={100})
    assert scan["active_writers_or_ambiguous"][0]["activity"] == "writer"
    assert "file descriptor for writing" in scan["active_writers_or_ambiguous"][0]["reason"]


def test_b1_v2_process_scan_fails_closed_on_writer_token_and_ambiguous_path(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    ps_output = f"301 chroma ingest {baseline}\n302 unknown-binary {baseline}\n"
    scan = c.process_scan([baseline], ps_output=ps_output, lsof_output="", current_pid=100, ancestor_pids={100})
    active = scan["active_writers_or_ambiguous"]
    assert [m["activity"] for m in active] == ["writer", "ambiguous"]


def test_b1_v2_certified_baseline_inventory_separates_marker(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "chroma.sqlite3").write_text("db", encoding="utf-8")
    (baseline / "embedded.json").write_text("{}", encoding="utf-8")
    (baseline / c.CERTIFIED_BASELINE_MARKER).write_text(json.dumps({"clone_id": "baseline-marker"}), encoding="utf-8")
    expected_rows = c.inventory_rows(baseline, exclude={c.CERTIFIED_BASELINE_MARKER})
    expected_digest = c.inventory_digest(expected_rows)
    marker_sha = c.sha256_file(baseline / c.CERTIFIED_BASELINE_MARKER)
    monkeypatch.setattr(c, "EXPECTED_CERTIFIED_BASELINE_CONTENT_ROWS", 2)
    monkeypatch.setattr(c, "EXPECTED_CERTIFIED_BASELINE_CONTENT_INVENTORY_V1", expected_digest)
    monkeypatch.setattr(c, "EXPECTED_CERTIFIED_BASELINE_MARKER_SHA256", marker_sha)
    report = c.certified_baseline_content_inventory(baseline)
    assert report["content_file_count"] == 2
    assert c.CERTIFIED_BASELINE_MARKER not in report["content_file_hashes"]
    assert report["content_inventory_sha256"] == expected_digest
    assert report["marker_sha256"] == marker_sha
    c.assert_certified_baseline_content_inventory(report)
    bad = dict(report)
    bad["content_file_count"] = 1
    with pytest.raises(c.ControlRefusal, match="content row count"):
        c.assert_certified_baseline_content_inventory(bad)


def _fake_python(tmp_path: Path, *, ok: bool = True, executable_echo: str | None = None, real_target_echo: str | None = None) -> Path:
    p = tmp_path / ("runtime_ok.py" if ok else "runtime_bad.py")
    if ok:
        fake_prefix = str(p.parents[1])
        p.write_text(
            "#!/usr/bin/env python3\n"
            "import json,sys\n"
            f"print(json.dumps({{'executable': {executable_echo!r} or sys.executable, 'symlink_real_executable_target': {real_target_echo!r} or sys.executable, 'prefix': {fake_prefix!r}, 'base_prefix': '/base-python', 'is_virtual_environment': True, 'version': sys.version, 'modules': {{'chromadb': '0.5.23'}}}}))\n",
            encoding="utf-8",
        )
    else:
        p.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('No module named chromadb\\n')\nsys.exit(1)\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def test_b2_runtime_preflight_missing_chromadb_refuses_before_copy(tmp_path):
    before = set(tmp_path.iterdir())
    bad = _fake_python(tmp_path, ok=False)
    with pytest.raises(c.ControlRefusal, match="dependency preflight failed"):
        c.runtime_preflight_for_batch_b2(bad, current_python=bad, authoritative_runtime=bad)
    after = set(tmp_path.iterdir())
    assert after - before == {bad}


def test_b2_authoritative_runtime_preflight_valid_interpreter_and_journal_binding(tmp_path):
    good = _fake_python(tmp_path, ok=True, executable_echo=str(tmp_path / "selected-python"))
    journal = tmp_path / "runtime_journal.json"
    out = c.runtime_preflight_for_batch_b2(good, journal=journal, current_python=good, authoritative_runtime=good)
    assert out["status"] == "RUNTIME_PREFLIGHT_OK"
    assert out["modules"]["chromadb"] == "0.5.23"
    assert out["authoritative_runtime"] == str(good)
    assert out["requested_lexical_executable"] == str(good)
    assert out["launcher_realpath"] == str(good.resolve())
    assert out["is_virtual_environment"] is True
    payload = json.loads(journal.read_text())
    assert payload["executable"] == str(tmp_path / "selected-python")
    assert payload["requested_executable"] == str(good)


def test_b2_venv_symlink_target_may_resolve_to_base_without_losing_launcher_identity(tmp_path):
    venv_launcher = tmp_path / "venv" / "bin" / "python"
    venv_launcher.parent.mkdir(parents=True)
    venv_launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"print(json.dumps({{'executable': {str(venv_launcher)!r}, 'symlink_real_executable_target': '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12', 'prefix': {str(tmp_path / 'venv')!r}, 'base_prefix': '/Library/Frameworks/Python.framework/Versions/3.12', 'is_virtual_environment': True, 'version': sys.version, 'modules': {{'chromadb': '0.5.23'}}}}))\n",
        encoding="utf-8",
    )
    venv_launcher.chmod(0o755)
    out = c.runtime_preflight_for_batch_b2(venv_launcher, current_python=venv_launcher, authoritative_runtime=venv_launcher)
    assert out["requested_lexical_executable"] == str(venv_launcher)
    assert out["launcher_realpath"] == str(venv_launcher.resolve())
    assert out["symlink_real_executable_target"].endswith("python3.12")
    assert out["sys_prefix"] == str(tmp_path / "venv")


def test_b2_dotvenv_runtime_refused(monkeypatch, tmp_path):
    dotvenv = tmp_path / ".venv" / "bin" / "python"
    dotvenv.parent.mkdir(parents=True)
    dotvenv.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    dotvenv.chmod(0o755)
    monkeypatch.setattr(c, "FORBIDDEN_RUNTIME_PYTHONS", {dotvenv})
    with pytest.raises(c.ControlRefusal, match="forbidden runtime python"):
        c.runtime_preflight_for_batch_b2(dotvenv, current_python=dotvenv, authoritative_runtime=dotvenv)


def test_b2_runtime_mismatch_refuses_unless_documented_separation(tmp_path):
    good = _fake_python(tmp_path, ok=True, executable_echo=str(tmp_path / "selected-python"))
    with pytest.raises(c.ControlRefusal, match="subprocess/runtime mismatch"):
        c.runtime_preflight_for_batch_b2(good, current_python=tmp_path / "other-python", authoritative_runtime=good)
    out = c.runtime_preflight_for_batch_b2(good, current_python=tmp_path / "other-python", authoritative_runtime=good, allow_separate=True)
    assert out["allow_separate_runtime"] is True


def test_b2_non_authoritative_runtime_refused(tmp_path):
    good = _fake_python(tmp_path, ok=True, executable_echo=str(tmp_path / "selected-python"))
    (tmp_path / "other").mkdir()
    other = _fake_python(tmp_path / "other", ok=True, executable_echo=str(tmp_path / "other" / "selected-python"))
    with pytest.raises(c.ControlRefusal, match="differs from sealed LC6 authoritative runtime"):
        c.runtime_preflight_for_batch_b2(other, current_python=other, authoritative_runtime=good)


def test_b2_resolved_base_interpreter_execution_path_refused(tmp_path):
    venv_launcher = tmp_path / "venv" / "bin" / "python"
    venv_launcher.parent.mkdir(parents=True)
    venv_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    venv_launcher.chmod(0o755)
    base_python = tmp_path / "base" / "python3.12"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    base_python.chmod(0o755)
    with pytest.raises(c.ControlRefusal, match="differs from sealed LC6 authoritative runtime"):
        c.runtime_preflight_for_batch_b2(base_python, current_python=base_python, authoritative_runtime=venv_launcher)


def test_b2_selection_normalization_proves_same_run_unchanged():
    before = {"CE_LIBRARY_ROOT": "/lib", "RAG_DB_PATH": "/gen", "mechanism": "RAG_DB_PATH environment override", "owner": "runtime"}
    after = {"CE_LIBRARY_ROOT": "/lib", "RAG_DB_PATH": "/gen", "active_generation": "/gen", "mechanism": "RAG_DB_PATH environment override", "owner": "runtime"}
    comp = c.compare_selection_snapshots(before, after)
    assert comp["match"] is True
    assert comp["before_normalized"]["active_generation"] == "/gen"
    assert comp["before_normalized"]["extra_fields"] == {"owner": "runtime"}
    changed = dict(after, unexpected="kept")
    comp2 = c.compare_selection_snapshots(before, changed)
    assert comp2["match"] is False
    assert comp2["after_normalized"]["extra_fields"] == {"owner": "runtime", "unexpected": "kept"}


def test_source_content_hash_v1_matches_19_content_files_with_separate_markers(env):
    tmp_path, _lib, gen = env
    backup = tmp_path / "backup_sc"
    candidate = tmp_path / "candidate_sc"
    jr = tmp_path / "journals_sc"
    c.backup_create(gen, backup, jr / "backup.json")
    cand = c.candidate_create(backup, candidate, jr / "candidate.json", "source content boundary")
    pre = c.validate_candidate_pre_mutation(candidate, jr / "pre.json")
    content = pre["source_content_hash"]
    assert content["schema"] == c.SOURCE_CONTENT_HASH_SCHEMA_V1
    assert c.CANDIDATE_MARKER not in content["content_file_hashes"]
    assert c.SEALED_DISPOSABLE_MARKER not in content["content_file_hashes"]
    assert content["content_file_count"] == len(cand["marker"]["source_content_hash"]["content_file_hashes"])
    assert content["marker_artifacts"][c.CANDIDATE_MARKER]["schema"] == "lc6-disposable-candidate-marker-v1"
    assert content["marker_artifacts"][c.SEALED_DISPOSABLE_MARKER]["schema"] is None or content["marker_artifacts"][c.SEALED_DISPOSABLE_MARKER]["schema"] != "source-content-hash-v1"


def test_source_content_normal_file_change_unknown_file_and_marker_tamper_fail(env):
    tmp_path, _lib, gen = env
    backup = tmp_path / "backup_boundary"
    candidate = tmp_path / "candidate_boundary"
    jr = tmp_path / "journals_boundary"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "source content boundary")
    c.validate_candidate_pre_mutation(candidate, jr / "pre_ok.json")
    (candidate / "embedded.json").write_text("{}", encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="changed_paths"):
        c.validate_candidate_pre_mutation(candidate, jr / "pre_changed.json")
    shutil_target = candidate / "embedded.json"
    shutil_target.write_text((backup / "embedded.json").read_text(encoding="utf-8"), encoding="utf-8")
    (candidate / ".unknown_control").write_text("unknown", encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="actual_only"):
        c.validate_candidate_pre_mutation(candidate, jr / "pre_unknown.json")
    (candidate / ".unknown_control").unlink()
    sealed_marker = candidate / c.SEALED_DISPOSABLE_MARKER
    marker_payload = json.loads(sealed_marker.read_text(encoding="utf-8"))
    marker_payload["clone_id"] = "tampered"
    sealed_marker.write_text(json.dumps(marker_payload), encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="sealed disposable marker provenance mismatch"):
        c.validate_candidate_pre_mutation(candidate, jr / "pre_marker_tamper.json")


def test_preserved_v3_old_logic_reproduces_expected_only_marker_difference():
    tree = Path("/private/tmp/lc6_b2_v3_20260915T082836Z_a8b8beb8")
    if not tree.is_dir():
        pytest.skip("preserved V3 tree not available")
    candidate = tree / "candidate_v3"
    marker_path = candidate / c.CANDIDATE_MARKER
    if not marker_path.is_file():
        pytest.skip("preserved V3 candidate marker not available")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    expected = dict(marker["source_file_hashes"])
    actual = dict(c.source_content_hash_report(candidate)["content_file_hashes"])
    diff = c.source_content_hash_diff(expected, actual)
    assert diff["expected_only"] == [c.SEALED_DISPOSABLE_MARKER]
    assert diff["actual_only"] == []
    assert diff["changed_paths"] == []


def test_backup_restore_candidate_postcheck_and_synthetic_switch_rollback(env):
    tmp_path, _lib, gen = env
    backup = tmp_path / "backup"
    restore = tmp_path / "restore"
    candidate = tmp_path / "candidate"
    jr = tmp_path / "journals"
    before_digest = c.embedding_payload_digest(gen)
    b = c.backup_create(gen, backup, jr / "backup.json", acquire_lock=True)
    assert b["status"] == "BACKUP_OK"
    assert b["journal_fsync_before_first_mutation"] is True
    assert not (gen / "ingest.lock").exists()
    r = c.restore_verify(backup, restore, jr / "restore.json")
    assert r["status"] == "RESTORE_VERIFY_OK"
    cand = c.candidate_create(backup, candidate, jr / "candidate.json", "synthetic validation")
    assert cand["status"] == "CANDIDATE_CREATE_OK"
    assert c.embedding_payload_digest(candidate) == before_digest
    p = c.postcheck(candidate, jr / "postcheck.json", expected_orphans=1, expected_embedding_digest=before_digest["sha256"])
    assert p["status"] == "POSTCHECK_OK"
    selection_file = tmp_path / "selection.env"
    selection_file.write_text(str(gen) + "\n", encoding="utf-8")
    sw = c.switch_selection(candidate, jr / "switch.json", selection_file=selection_file, synthetic=True)
    assert sw["status"] == "SWITCH_OK_SYNTHETIC_ONLY"
    assert selection_file.read_text(encoding="utf-8").strip() == str(candidate.resolve())
    rb = c.rollback_selection(jr / "switch.json", jr / "rollback.json", selection_file=selection_file, synthetic=True)
    assert rb["status"] == "ROLLBACK_OK_SYNTHETIC_ONLY"
    assert selection_file.read_text(encoding="utf-8").strip() == str(gen)
    with pytest.raises(c.ControlRefusal, match="double rollback"):
        c.rollback_selection(jr / "switch.json", jr / "rollback2.json", selection_file=selection_file, synthetic=True)


@pytest.mark.parametrize("fn,args", [
    ("backup_create", ["GEN", "backup", "j/backup.json"]),
    ("restore_verify", ["GEN", "restore", "j/restore.json"]),
    ("candidate_create", ["GEN", "candidate", "j/candidate.json", "purpose"]),
])
@pytest.mark.parametrize("inject", ["before_mutation", "during_mutation", "after_mutation"])
def test_failure_boundaries_preserve_journals(env, fn, args, inject):
    tmp, _lib, gen = env
    call_args = [gen if a == "GEN" else tmp / a for a in args]
    kwargs = {"inject": inject}
    if fn == "candidate_create":
        source = tmp / "source_backup"
        c.backup_create(gen, source, tmp / "j/source_backup.json")
        call_args[0] = source
    with pytest.raises(c.ControlRefusal):
        getattr(c, fn)(*call_args, **kwargs)
    journal = call_args[2] if fn != "restore_verify" else call_args[2]
    assert journal.is_file()
    payload = json.loads(journal.read_text())
    assert payload["journal_fsync_before_first_mutation"] is True
    assert "REJECTED" in payload["status"] or payload["status"] == "RESTORE_VERIFY_FAIL"


def test_switch_and_rollback_failure_boundaries(env):
    tmp, _lib, gen = env
    backup = tmp / "backup"; candidate = tmp / "candidate"; jr = tmp / "j"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "synthetic validation")
    selection_file = tmp / "selection.env"; selection_file.write_text(str(gen) + "\n")
    for inject in ["before_mutation", "during_mutation", "after_mutation"]:
        sj = jr / f"switch_{inject}.json"
        with pytest.raises(c.ControlRefusal):
            c.switch_selection(candidate, sj, selection_file=selection_file, synthetic=True, inject=inject)
        assert sj.is_file()
    good = c.switch_selection(candidate, jr / "switch_good.json", selection_file=selection_file, synthetic=True)
    assert good["status"] == "SWITCH_OK_SYNTHETIC_ONLY"
    for inject in ["before_mutation", "during_mutation", "after_mutation"]:
        swj = jr / f"switch_rb_{inject}.json"
        swj.write_text(json.dumps(good), encoding="utf-8")
        rj = jr / f"rollback_{inject}.json"
        with pytest.raises(c.ControlRefusal):
            c.rollback_selection(swj, rj, selection_file=selection_file, synthetic=True, inject=inject)
        assert rj.is_file()


def test_negative_guards(env, tmp_path, monkeypatch):
    tmp, lib, gen = env
    with pytest.raises(c.ControlRefusal, match="source must resolve"):
        c.backup_create(tmp / "other", tmp / "backup", tmp / "j.json")
    with pytest.raises(c.ControlRefusal, match="CE Library"):
        c.backup_create(gen, lib / "bad_backup", tmp / "j.json")
    with pytest.raises(c.ControlRefusal, match="insufficient free space"):
        c.backup_create(gen, tmp / "backup2", tmp / "j2.json", min_free_bytes=10**30)
    candidate = tmp / "not_candidate"; candidate.mkdir()
    with pytest.raises(c.ControlRefusal, match="candidate marker missing"):
        c.postcheck(candidate, tmp / "j3.json")
    bad = tmp / "bad_candidate"; bad.mkdir()
    (bad / c.CANDIDATE_MARKER).write_text(json.dumps({"schema":"wrong"}), encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="missing fields"):
        c.validate_candidate_marker(bad)


def test_wrong_source_hash_marker_fails_separately(env):
    tmp, _lib, gen = env
    backup = tmp / "backup"; candidate = tmp / "candidate"; jr = tmp / "j"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "synthetic validation")
    marker_path = candidate / c.CANDIDATE_MARKER
    marker = json.loads(marker_path.read_text())
    marker["source_file_hashes"]["embedded.json"] = "0" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="source hashes"):
        c.validate_candidate_marker(candidate)


def test_wrong_source_hash_marker_and_embedding_float_change_fail(env):
    tmp, _lib, gen = env
    backup = tmp / "backup"; candidate = tmp / "candidate"; jr = tmp / "j"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "synthetic validation")
    marker_path = candidate / c.CANDIDATE_MARKER
    marker = json.loads(marker_path.read_text())
    pre_journal = jr / "pre_embedding.json"
    pre_validation = c.validate_candidate_pre_mutation(candidate, pre_journal)
    assert pre_validation["status"] == "CANDIDATE_PRE_MUTATION_VALIDATED"
    assert pre_validation["journal_fsync_before_first_mutation"] is True
    before_payload = c.embedding_payload_digest(candidate)
    before_ids = before_payload["ordered_ids"]
    before_count = before_payload["row_count"]
    before_dtype = before_payload["dtype"]
    before_dimensions = before_payload["dimensions"]
    before = before_payload["sha256"]
    conn = sqlite3.connect(candidate / "chroma.sqlite3")
    try:
        conn.execute("update embeddings set embedding=? where embedding_id='c1'", (struct.pack('<ff', 1.5, 2.0),))
        conn.commit()
    finally:
        conn.close()
    after_payload = c.embedding_payload_digest(candidate)
    assert after_payload["ordered_ids"] == before_ids
    assert after_payload["row_count"] == before_count
    assert after_payload["dtype"] == before_dtype
    assert after_payload["dimensions"] == before_dimensions
    assert after_payload["sha256"] != before
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="retained_vector_payload_changed|unauthorized_removed_vector_ids"):
        c.validate_candidate_post_mutation(candidate, jr / "postcheck_float.json", pre_journal, expected_orphans=1, authorized_retired_ids=set(), authorized_retired_digest="none", manifest={"operations": []})
    payload = json.loads((jr / "postcheck_float.json").read_text())
    assert "retained_vector_payload_changed" in payload["failure_reasons"]


def _make_three_vector_candidate(env):
    tmp, lib, gen = env
    live = lib / "doc3.txt"
    live.write_text("live3", encoding="utf-8")
    tracker = {
        "retire_digest": {"paths": ["missing-a.txt", "missing-b.txt"], "chunk_ids": ["c1", "c2"]},
        "digest3": {"paths": ["doc3.txt"], "chunk_ids": ["c3"]},
    }
    (gen / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")
    conn = sqlite3.connect(gen / "chroma.sqlite3")
    try:
        conn.execute("delete from embeddings")
        conn.execute("delete from embedding_metadata")
        rows = [("c1", (1.0, 2.0), "retire_digest"), ("c2", (3.0, 4.0), "retire_digest"), ("c3", (5.0, 6.0), "digest3")]
        for idx, (eid, vec, digest) in enumerate(rows, start=1):
            conn.execute("insert into embeddings (id, embedding_id, embedding) values (?, ?, ?)", (idx, eid, struct.pack('<ff', *vec)))
            conn.execute("insert into embedding_metadata values (?, 'source_hash', ?)", (idx, digest))
        conn.commit()
    finally:
        conn.close()
    backup = tmp / "b2v4_backup"; candidate = tmp / "b2v4_candidate"; jr = tmp / "b2v4_j"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "b2v4 set-aware test")
    pre = c.validate_candidate_pre_mutation(candidate, jr / "pre.json")
    manifest = {"operations": [{"op_type": "RETIRE_UNRECOVERABLE", "digest": "retire_digest", "chunk_ids": ["c1", "c2"]}]}
    return tmp, lib, candidate, jr, pre, manifest


def _retire_c1_c2(candidate: Path):
    conn = sqlite3.connect(candidate / "chroma.sqlite3")
    try:
        conn.execute("delete from embedding_metadata where id in (select id from embeddings where embedding_id in ('c1','c2'))")
        conn.execute("delete from embeddings where embedding_id in ('c1','c2')")
        conn.commit()
    finally:
        conn.close()
    tracker = json.loads((candidate / "embedded.json").read_text())
    tracker.pop("retire_digest", None)
    (candidate / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")


def test_b2v4_set_aware_correct_two_vector_retirement_passes(env):
    _tmp, _lib, candidate, jr, _pre, manifest = _make_three_vector_candidate(env)
    _retire_c1_c2(candidate)
    out = c.validate_candidate_post_mutation(candidate, jr / "post_ok.json", jr / "pre.json", expected_orphans=0, authorized_retired_ids={"c1", "c2"}, authorized_retired_digest="retire_digest", manifest=manifest)
    assert out["status"] == "POSTCHECK_OK"
    assert out["set_aware_vector_comparison"]["removed_ids"] == ["c1", "c2"]
    assert out["set_aware_vector_comparison"]["retained_payload_mismatches"] == []


def test_b2v4_retained_float_mutation_fails(env):
    _tmp, _lib, candidate, jr, _pre, manifest = _make_three_vector_candidate(env)
    _retire_c1_c2(candidate)
    conn = sqlite3.connect(candidate / "chroma.sqlite3")
    try:
        conn.execute("update embeddings set embedding=? where embedding_id='c3'", (struct.pack('<ff', 5.5, 6.0),))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(c.ControlRefusal, match="retained_vector_payload_changed"):
        c.validate_candidate_post_mutation(candidate, jr / "post_float.json", jr / "pre.json", expected_orphans=0, authorized_retired_ids={"c1", "c2"}, authorized_retired_digest="retire_digest", manifest=manifest)


def test_b2v4_wrong_removed_id_and_added_vector_fail(env):
    _tmp, _lib, candidate, jr, _pre, manifest = _make_three_vector_candidate(env)
    conn = sqlite3.connect(candidate / "chroma.sqlite3")
    try:
        conn.execute("delete from embedding_metadata where id in (select id from embeddings where embedding_id in ('c1','c3'))")
        conn.execute("delete from embeddings where embedding_id in ('c1','c3')")
        conn.execute("insert into embeddings (id, embedding_id, embedding) values (4, 'c4', ?)", (struct.pack('<ff', 7.0, 8.0),))
        conn.execute("insert into embedding_metadata values (4, 'source_hash', 'digest4')")
        conn.commit()
    finally:
        conn.close()
    tracker = json.loads((candidate / "embedded.json").read_text()); tracker.pop("retire_digest", None); (candidate / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="unauthorized_removed_vector_ids"):
        c.validate_candidate_post_mutation(candidate, jr / "post_wrong_ids.json", jr / "pre.json", expected_orphans=0, authorized_retired_ids={"c1", "c2"}, authorized_retired_digest="retire_digest", manifest=manifest)
    payload = json.loads((jr / "post_wrong_ids.json").read_text())
    assert "unexpected_added_vector_ids" in payload["failure_reasons"]


def test_b2v4_unauthorized_tracker_metadata_and_file_change_fail(env):
    _tmp, _lib, candidate, jr, _pre, manifest = _make_three_vector_candidate(env)
    _retire_c1_c2(candidate)
    tracker = json.loads((candidate / "embedded.json").read_text())
    tracker["digest3"]["paths"] = ["unauthorized.txt"]
    (candidate / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")
    (candidate / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(c.ControlRefusal, match="unrelated_file_set_change"):
        c.validate_candidate_post_mutation(candidate, jr / "post_tracker_file.json", jr / "pre.json", expected_orphans=0, authorized_retired_ids={"c1", "c2"}, authorized_retired_digest="retire_digest", manifest=manifest)
    payload = json.loads((jr / "post_tracker_file.json").read_text())
    assert "tracker_metadata_transition_mismatch" in payload["failure_reasons"]


def test_b2v4_after_snapshots_run_after_postcheck_failure(env, monkeypatch):
    tmp, _lib, candidate, jr, _pre, manifest = _make_three_vector_candidate(env)
    calls = {"baseline": 0, "production": 0}
    monkeypatch.setattr(c, "verify_baseline_identity", lambda _p: calls.__setitem__("baseline", calls["baseline"] + 1) or {"status": "baseline_after"})
    monkeypatch.setattr(c, "generation_report", lambda *_a, **_k: calls.__setitem__("production", calls["production"] + 1) or {"status": "production_after"})
    monkeypatch.setattr(c, "active_generation", lambda: candidate)
    monkeypatch.setattr(c, "postcheck", lambda *_a, **_k: (_ for _ in ()).throw(c.ControlRefusal("simulated postcheck failure")))
    with pytest.raises(c.ControlRefusal):
        c.postcheck_with_final_snapshots(candidate, jr / "post_fail.json", jr / "pre.json", jr / "after_snapshots.json", expected_orphans=0)
    snap = json.loads((jr / "after_snapshots.json").read_text())
    assert snap["postcheck_error"]["type"] == "ControlRefusal"
    assert calls["baseline"] == 1
    assert calls["production"] >= 1


def test_sealed_package_match_reports_ok_status_for_sealing_logic(tmp_path, monkeypatch):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    payload = sealed / "artifact.txt"
    payload.write_text("sealed evidence", encoding="utf-8")
    manifest = f"{c.sha256_file(payload)}  artifact.txt\n"
    sums = sealed / "SHA256SUMS"
    sums.write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(c, "SEALED_SHA256SUMS_HASH", c.sha256_file(sums))

    out = c.verify_sealed_package(sealed)
    sealed_ok = out.get("sha256sums_sha256") == c.SEALED_SHA256SUMS_HASH and (
        out.get("status") in {"SEALED_PACKAGE_OK", "PASS", "OK"} or out.get("ok") is True
    )

    assert out["sha256sums_sha256"] == c.SEALED_SHA256SUMS_HASH
    assert out["status"] == "SEALED_PACKAGE_OK"
    assert out["ok"] is True
    assert sealed_ok is True


def test_real_switch_blocks_without_selection_owner(env):
    tmp, _lib, gen = env
    backup = tmp / "backup"; candidate = tmp / "candidate"; jr = tmp / "j"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "synthetic validation")
    out = c.switch_selection(candidate, jr / "switch.json")
    assert out["status"] == "BLOCKED_MISSING_SELECTION_OWNER"


def test_repair_wrapper_positive_with_synthetic_executor(env):
    tmp, _lib, gen = env
    backup = tmp / "backup"; candidate = tmp / "candidate"; jr = tmp / "j"; work = tmp / "work"
    c.backup_create(gen, backup, jr / "backup.json")
    c.candidate_create(backup, candidate, jr / "candidate.json", "synthetic validation")
    fake_pkg = tmp / "fake_pkg"; fake_pkg.mkdir()
    (fake_pkg / "SHA256SUMS").write_text("", encoding="utf-8")
    exe = tmp / "fake_executor.py"
    exe.write_text("import json,sys; print(json.dumps({'status':'APPLY_OK'}))", encoding="utf-8")
    old_hash = c.SEALED_SHA256SUMS_HASH
    def fake_preflight(_runtime_python=None, *, journal=None, current_python=None, allow_separate=False, authoritative_runtime=None):
        return {"status": "RUNTIME_PREFLIGHT_OK", "executable": sys.executable, "modules": {"chromadb": "test"}, "requested_executable": sys.executable, "requested_lexical_executable": sys.executable}
    old_preflight = c.runtime_preflight_for_batch_b2
    try:
        c.runtime_preflight_for_batch_b2 = fake_preflight
        c.SEALED_SHA256SUMS_HASH = c.sha256_file(fake_pkg / "SHA256SUMS")
        out = c.repair_candidate(candidate, work, jr / "repair.json", package_dir=fake_pkg, executor=exe, expected_status="APPLY_OK")
    finally:
        c.SEALED_SHA256SUMS_HASH = old_hash
        c.runtime_preflight_for_batch_b2 = old_preflight
    assert out["status"] == "REPAIR_WRAPPER_OK"


def test_cli_help_all_subcommands():
    with pytest.raises(SystemExit) as exc:
        c.main(["--help"])
    assert exc.value.code == 0
