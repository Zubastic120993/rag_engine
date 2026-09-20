import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR))

from promotion_controls import (  # noqa: E402
    CertifiedBaselineError,
    EnvSelectionError,
    FakeHermesSupervisor,
    JournalPathError,
    RollbackError,
    atomic_switch,
    rollback_switch,
    validate_versioned_baseline_artifact,
    verify_current_state_matches_certified_baseline,
    validate_env_selection,
    validate_generation_target,
)
import promotion_controls  # noqa: E402
from synthetic_e2e_driver import (  # noqa: E402
    build_failure_report,
    cleanup_fixture_after_result,
    ensure_work_directory,
)


OLD = "/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604"
NEW = "/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_synthetic_target"
CERT_SHA = "09b69ba17c559e0ee535f4b724ed7482a71578608e7699f2eff260e21ef86b70"
CONTENT_CHECKSUM = "004737754f216a38494eeca739590e2535560ed21ed0159580bed48e43ed56e3"
PROVENANCE_CHECKSUM = "b98465e70b9a8226cca41ab40882e15463cd4591ede67ccdb16aa8f688f8d9df"


def certified_baseline_payload() -> dict:
    return {
        "schema": "lc6-versioned-evolved-pre-repair-baseline-certification-v1",
        "certification_result": "CERTIFIED",
        "baseline_identity": {
            "version": "LC6-EVOLVED-PRE-REPAIR-CURRENT-20260919T180213Z-CERT-V1",
            "active_generation_path": OLD,
            "selection_mechanism": "/Users/vladymyrzub/.hermes/.env RAG_DB_PATH assignment",
        },
        "historical_disclosure": {
            "old_append_continuity_chain_incomplete": True,
            "rolled_back_append_not_retrospectively_proven_clean": True,
            "certification_scope": "independent current-state certification only; not repair or rewrite of historical append evidence",
        },
        "inventories": {
            "source_database_content": {
                "boundary": "active generation files excluding certified_append_journal/** and transient WAL/SHM/lock files",
                "row_count": 11,
                "checksum_sha256": CONTENT_CHECKSUM,
                "rows": [{"path": "embedded.json", "type": "file", "bytes": 1, "sha256": "a" * 64}],
            },
            "certified_append_journal_provenance": {
                "boundary": "certified_append_journal/** provenance files only",
                "row_count": 20,
                "checksum_sha256": PROVENANCE_CHECKSUM,
                "rows": [{"path": "certified_append_journal/capp.json", "type": "file", "bytes": 1, "sha256": "b" * 64}],
            },
        },
        "semantic_state": {
            "collection_names": ["langchain"],
            "tracker_entries": 1761,
            "tracker_paths": 1849,
            "tracker_chunk_ids": 126076,
            "sqlite_embeddings": 126076,
            "sqlite_embedding_metadata": 1386836,
            "sqlite_embedding_fulltext_search": 126076,
            "tracker_sqlite_exact_id_set_equal": True,
            "duplicate_chunk_id_count": 0,
            "wal_shm_lock_state": {
                "chroma.sqlite3-wal": False,
                "chroma.sqlite3-shm": False,
                "ingest.lock": False,
                ".ingest.lock": False,
                "write.lock": False,
                ".rag_state": False,
            },
            "active_writer_candidates": "",
        },
        "authoritative_55_missing_orphan_binding": {
            "all_55_match_authoritative_pre_repair_lc6_orphan_set": True,
            "set_comparison_totals": {"exact_matches": 55, "authoritative_only": 0, "current_only": 0},
        },
    }


def current_state_payload() -> dict:
    baseline = certified_baseline_payload()
    return {
        "certification_sha256": CERT_SHA,
        "active_generation_path": OLD,
        "selection_mechanism": baseline["baseline_identity"]["selection_mechanism"],
        "source_content": {"row_count": 11, "checksum_sha256": CONTENT_CHECKSUM, "unknown_extra_files": []},
        "provenance": {"row_count": 20, "checksum_sha256": PROVENANCE_CHECKSUM, "unknown_extra_files": []},
        "semantic_state": dict(baseline["semantic_state"]),
        "orphan_binding": dict(baseline["authoritative_55_missing_orphan_binding"]),
    }


def make_generation(root: Path, name: str) -> Path:
    gen = root / ".rag_db_generations" / name
    gen.mkdir(parents=True)
    (gen / "chroma.sqlite3").write_bytes(b"synthetic sqlite placeholder")
    return gen


class PromotionControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lc6_promotion_controls_test_", dir="/private/tmp"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.gen_root = self.tmp / "CE_Library"
        self.old = make_generation(self.gen_root, "raggen_old")
        self.new = make_generation(self.gen_root, "raggen_new")
        self.env_file = self.tmp / ".env"
        self.env_file.write_bytes(
            b"# comment\n"
            b"API_TOKEN=do-not-log\r\n"
            + f"RAG_DB_PATH={self.old}\n".encode()
            + b"OTHER=value\n"
        )
        os.chmod(self.env_file, 0o600)
        self.before = self.env_file.read_bytes()

    def test_validate_requires_exactly_one_active_assignment(self):
        result = validate_env_selection(self.env_file)
        self.assertEqual(result.line_number, 3)
        self.assertEqual(result.value, str(self.old))
        self.assertIn(b"API_TOKEN=do-not-log", result.original_bytes)

        self.env_file.write_text("A=1\n")
        with self.assertRaises(EnvSelectionError):
            validate_env_selection(self.env_file)

        self.env_file.write_text(f"RAG_DB_PATH={self.old}\nRAG_DB_PATH={self.new}\n")
        with self.assertRaises(EnvSelectionError):
            validate_env_selection(self.env_file)

    def test_rejects_malformed_relative_forbidden_and_quoted_values(self):
        bad_values = [
            "relative/path",
            f"'{self.old}'",
            f'"{self.old}"',
            "/Users/vladymyrzub/CE_Library/.rag_db",
            "/Users/vladymyrzub/CE_Library/.rag_db_generations",
            "/Users/vladymyrzub/CE_Library/.rag_db_generations/missing",
        ]
        for value in bad_values:
            with self.subTest(value=value):
                self.env_file.write_text(f"RAG_DB_PATH={value}\n")
                with self.assertRaises(EnvSelectionError):
                    validate_env_selection(self.env_file)

    def test_atomic_switch_preserves_unrelated_bytes_mode_and_records_journal(self):
        journal = self.tmp / "switch.journal.json"
        result = atomic_switch(
            env_file=self.env_file,
            target_value=str(self.new),
            journal_file=journal,
            run_id="run-001",
            generation_validator=validate_generation_target,
        )
        after = self.env_file.read_bytes()
        self.assertEqual(result.old_value, str(self.old))
        self.assertEqual(result.new_value, str(self.new))
        self.assertIn(f"RAG_DB_PATH={self.new}\n".encode(), after)
        self.assertNotIn(f"RAG_DB_PATH={self.old}\n".encode(), after)
        self.assertEqual(after.replace(str(self.new).encode(), str(self.old).encode()), self.before)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        payload = json.loads(journal.read_text())
        self.assertEqual(payload["state"], "COMMITTED")
        self.assertEqual(payload["run_id"], "run-001")
        self.assertEqual(payload["old_value"], str(self.old))
        self.assertEqual(payload["new_value"], str(self.new))
        self.assertNotIn("API_TOKEN", json.dumps(payload))

    def test_rollback_restores_original_bytes_and_prevents_double_rollback(self):
        journal = self.tmp / "switch.journal.json"
        atomic_switch(self.env_file, str(self.new), journal, "run-rollback", validate_generation_target)
        rollback = rollback_switch(self.env_file, journal, "run-rollback")
        self.assertEqual(rollback.restored_value, str(self.old))
        self.assertEqual(self.env_file.read_bytes(), self.before)
        payload = json.loads(journal.read_text())
        self.assertEqual(payload["state"], "ROLLED_BACK")
        with self.assertRaises(RollbackError):
            rollback_switch(self.env_file, journal, "run-rollback")

    def test_rollback_refuses_wrong_run_id_wrong_current_value_and_wrong_file(self):
        journal = self.tmp / "switch.journal.json"
        atomic_switch(self.env_file, str(self.new), journal, "run-guard", validate_generation_target)
        with self.assertRaises(RollbackError):
            rollback_switch(self.env_file, journal, "wrong-run")
        self.env_file.write_bytes(self.before)
        with self.assertRaises(RollbackError):
            rollback_switch(self.env_file, journal, "run-guard")
        other = self.tmp / "other.env"
        other.write_bytes(self.env_file.read_bytes())
        with self.assertRaises(RollbackError):
            rollback_switch(other, journal, "run-guard")

    def test_failure_injection_recovery_and_no_unrelated_byte_change(self):
        points = [
            "before_journal",
            "after_prepared_journal",
            "after_temp_write",
            "before_atomic_replace",
            "after_atomic_replace",
            "during_parent_fsync",
        ]
        for point in points:
            with self.subTest(point=point):
                env = self.tmp / f"{point}.env"
                journal = self.tmp / f"{point}.journal.json"
                env.write_bytes(self.before)
                os.chmod(env, 0o600)
                try:
                    atomic_switch(env, str(self.new), journal, point, validate_generation_target, fail_at=point)
                except Exception:
                    pass
                content = env.read_bytes()
                if point in {"after_atomic_replace", "during_parent_fsync"}:
                    self.assertIn(f"RAG_DB_PATH={self.new}".encode(), content)
                else:
                    self.assertEqual(content, self.before)
                self.assertIn(b"API_TOKEN=do-not-log", content)
                if journal.exists():
                    state = json.loads(journal.read_text()).get("state")
                    self.assertIn(state, {"PREPARED", "COMMITTED", "RESIDUAL"})

    def test_fake_supervisor_restart_verifies_new_pid_and_inherited_target(self):
        supervisor = FakeHermesSupervisor(initial_rag_db_path=str(self.old))
        old_identity = supervisor.identity()
        result = supervisor.restart_and_verify(expected_rag_db_path=str(self.new), readiness_timeout_s=1.0)
        self.assertEqual(result.old_pid, old_identity.pid)
        self.assertNotEqual(result.new_pid, old_identity.pid)
        self.assertEqual(result.new_rag_db_path, str(self.new))
        self.assertGreaterEqual(result.interruption_ms, 0)
        self.assertFalse(result.overlap_ambiguous)

    def test_fake_supervisor_refuses_overlap_wrong_inheritance_and_readiness_failure(self):
        supervisor = FakeHermesSupervisor(initial_rag_db_path=str(self.old), refuse_stop=True)
        with self.assertRaises(RuntimeError):
            supervisor.restart_and_verify(str(self.new), readiness_timeout_s=0.1)

        supervisor = FakeHermesSupervisor(initial_rag_db_path=str(self.old), wrong_inheritance=True)
        with self.assertRaises(RuntimeError):
            supervisor.restart_and_verify(str(self.new), readiness_timeout_s=0.1)

        supervisor = FakeHermesSupervisor(initial_rag_db_path=str(self.old), readiness_delay_s=0.5)
        with self.assertRaises(RuntimeError):
            supervisor.restart_and_verify(str(self.new), readiness_timeout_s=0.01)

    def test_fake_supervisor_refuses_new_backend_start_failure(self):
        supervisor = FakeHermesSupervisor(initial_rag_db_path=str(self.old), refuse_start=True)
        with self.assertRaisesRegex(RuntimeError, "new backend failed to start"):
            supervisor.restart_and_verify(str(self.new), readiness_timeout_s=0.1)
        self.assertEqual(supervisor.identity().rag_db_path, str(self.old))
        self.assertEqual(supervisor.last_failure_state["phase"], "start")
        self.assertEqual(supervisor.last_failure_state["old_pid"], supervisor.identity().pid)
        self.assertTrue(supervisor.last_failure_state["rollback_requested"])

    def test_atomic_switch_refuses_second_switch_with_existing_committed_journal(self):
        journal = self.tmp / "switch.journal.json"
        second = make_generation(self.gen_root, "raggen_second")
        atomic_switch(self.env_file, str(self.new), journal, "run-first", validate_generation_target)
        with self.assertRaisesRegex(EnvSelectionError, "existing committed journal"):
            atomic_switch(self.env_file, str(second), journal, "run-second", validate_generation_target)
        rollback = rollback_switch(self.env_file, journal, "run-first")
        self.assertEqual(rollback.state, "ROLLED_BACK")

    def test_atomic_switch_refuses_each_unresolved_journal_state_and_allows_finalized(self):
        second = make_generation(self.gen_root, "raggen_second")
        unresolved_states = ["PREPARED", "COMMITTED", "RECOVERY_REQUIRED", "RESIDUAL", "UNKNOWN"]
        for state in unresolved_states:
            with self.subTest(state=state):
                env = self.tmp / f"{state}.env"
                env.write_bytes(self.before)
                os.chmod(env, 0o600)
                journal = self.tmp / f"{state}.journal.json"
                journal.write_text(json.dumps({
                    "schema": "lc6-promotion-selection-journal-v1",
                    "state": state,
                    "env_file": str(env),
                }))
                with self.assertRaisesRegex(EnvSelectionError, "unresolved journal"):
                    atomic_switch(env, str(second), journal, f"run-{state}", validate_generation_target)

        for state in ["ROLLED_BACK", "FINALIZED_ARCHIVED"]:
            with self.subTest(state=state):
                env = self.tmp / f"{state}.env"
                env.write_bytes(self.before)
                os.chmod(env, 0o600)
                journal = self.tmp / f"{state}.journal.json"
                journal.write_text(json.dumps({
                    "schema": "lc6-promotion-selection-journal-v1",
                    "state": state,
                    "env_file": str(env),
                }))
                result = atomic_switch(env, str(second), journal, f"run-{state}", validate_generation_target)
                self.assertEqual(result.state, "COMMITTED")

    def test_atomic_switch_detects_concurrent_env_modification_before_commit(self):
        journal = self.tmp / "switch.journal.json"
        original_writer = promotion_controls._write_json_durable

        def concurrent_writer(path, payload):
            original_writer(path, payload)
            if payload.get("state") == "PREPARED":
                self.env_file.write_bytes(self.before + b"CONCURRENT=1\n")

        promotion_controls._write_json_durable = concurrent_writer
        try:
            with self.assertRaisesRegex(EnvSelectionError, "concurrent"):
                atomic_switch(self.env_file, str(self.new), journal, "run-concurrent", validate_generation_target)
        finally:
            promotion_controls._write_json_durable = original_writer

    def test_rollback_records_residual_state_on_atomic_restore_failure(self):
        journal = self.tmp / "switch.journal.json"
        atomic_switch(self.env_file, str(self.new), journal, "run-restore-fail", validate_generation_target)
        original_replace = promotion_controls._atomic_replace_preserve_metadata

        def failing_replace(*args, **kwargs):
            raise OSError("synthetic rollback failure")

        promotion_controls._atomic_replace_preserve_metadata = failing_replace
        try:
            with self.assertRaises(RollbackError):
                rollback_switch(self.env_file, journal, "run-restore-fail")
        finally:
            promotion_controls._atomic_replace_preserve_metadata = original_replace
        payload = json.loads(journal.read_text())
        self.assertEqual(payload["state"], "RECOVERY_REQUIRED")
        self.assertEqual(payload["residual_reason"], "rollback-failure")
        self.assertEqual(payload["current_env_selection"], str(self.new))
        self.assertEqual(payload["intended_restored_selection"], str(self.old))
        self.assertIn("manual_recovery_action", payload)

    def test_rollback_refuses_stale_or_malformed_journal(self):
        journal = self.tmp / "stale.journal.json"
        journal.write_text(json.dumps({"schema": "lc6-promotion-selection-journal-v1", "state": "PREPARED"}))
        with self.assertRaises(RollbackError):
            rollback_switch(self.env_file, journal, "run-stale")
        journal.write_text(json.dumps({"schema": "wrong", "state": "COMMITTED"}))
        with self.assertRaises(RollbackError):
            rollback_switch(self.env_file, journal, "run-stale")

    def test_subprocess_persist_dir_inheritance_probe_is_environment_only(self):
        env = os.environ.copy()
        env["RAG_DB_PATH"] = str(self.new)
        import subprocess
        completed = subprocess.run(
            [sys.executable, "-c", "import os; print(os.environ['RAG_DB_PATH'])"],
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), str(self.new))

    def test_atomic_switch_creates_missing_journal_parent_and_lock_safely(self):
        journal = self.tmp / "work" / "nested" / "switch.journal.json"
        result = atomic_switch(self.env_file, str(self.new), journal, "run-missing-parent", validate_generation_target)
        self.assertEqual(result.state, "COMMITTED")
        self.assertTrue(journal.exists())
        self.assertTrue((journal.parent / "switch.journal.json.lock").exists())
        self.assertFalse(journal.parent.is_symlink())

    def test_atomic_switch_refuses_out_of_fixture_or_symlinked_journal_parent(self):
        outside = Path(tempfile.mkdtemp(prefix="lc6_outside_journal_", dir="/private/tmp"))
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        with self.assertRaises(JournalPathError):
            atomic_switch(self.env_file, str(self.new), outside / "switch.journal.json", "run-outside", validate_generation_target)

        real_parent = self.tmp / "real_work"
        real_parent.mkdir()
        symlink_parent = self.tmp / "symlink_work"
        symlink_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(JournalPathError):
            atomic_switch(self.env_file, str(self.new), symlink_parent / "switch.journal.json", "run-symlink", validate_generation_target)

    def test_synthetic_driver_creates_work_directory_inside_fixture(self):
        journal = self.tmp / "work" / "switch.journal.json"
        created = ensure_work_directory(journal, self.tmp)
        self.assertEqual(created, journal.parent.resolve())
        self.assertTrue(created.is_dir())
        outside = Path(tempfile.mkdtemp(prefix="lc6_driver_outside_", dir="/private/tmp"))
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        with self.assertRaises(ValueError):
            ensure_work_directory(outside / "switch.journal.json", self.tmp)

    def test_synthetic_driver_failure_report_preserves_traceback_stage_and_target(self):
        target = self.tmp / "work" / "missing.lock"
        try:
            raise FileNotFoundError(2, "No such file or directory", str(target))
        except FileNotFoundError as exc:
            report = build_failure_report(
                exc=exc,
                stage="atomic-switch",
                operation="open-lock",
                target_path=target,
                fixture_root=self.tmp,
            )
        self.assertEqual(report["stage"], "atomic-switch")
        self.assertEqual(report["operation"], "open-lock")
        self.assertEqual(report["target_path"], str(target))
        self.assertIn("Traceback", report["traceback"])
        self.assertIn("FileNotFoundError", report["traceback"])
        self.assertTrue(report["fixture"]["exists_at_report_build"])

    def test_synthetic_driver_preserves_failed_fixture_and_cleans_successful_fixture(self):
        failed = Path(tempfile.mkdtemp(prefix="lc6_failed_fixture_", dir="/private/tmp"))
        successful = Path(tempfile.mkdtemp(prefix="lc6_success_fixture_", dir="/private/tmp"))
        self.addCleanup(lambda: shutil.rmtree(failed, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(successful, ignore_errors=True))
        self.assertFalse(cleanup_fixture_after_result(failed, success=False, preserve_failed_fixture=True))
        self.assertTrue(failed.exists())
        self.assertTrue(cleanup_fixture_after_result(successful, success=True, preserve_failed_fixture=True))
        self.assertFalse(successful.exists())

    def test_versioned_certified_baseline_accepts_valid_artifact_and_current_state(self):
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        result = verify_current_state_matches_certified_baseline(baseline, current_state_payload())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["baseline_version"], "LC6-EVOLVED-PRE-REPAIR-CURRENT-20260919T180213Z-CERT-V1")

    def test_versioned_certified_baseline_rejects_wrong_certification_hash(self):
        with self.assertRaisesRegex(CertifiedBaselineError, "certification hash"):
            validate_versioned_baseline_artifact(certified_baseline_payload(), "0" * 64)

    def test_versioned_certified_baseline_rejects_missing_or_failed_status(self):
        payload = certified_baseline_payload()
        payload.pop("certification_result")
        with self.assertRaisesRegex(CertifiedBaselineError, "certification status"):
            validate_versioned_baseline_artifact(payload, CERT_SHA)

        payload = certified_baseline_payload()
        payload["certification_result"] = "FAILED_PRESERVED"
        with self.assertRaisesRegex(CertifiedBaselineError, "certification status"):
            validate_versioned_baseline_artifact(payload, CERT_SHA)

    def test_versioned_certified_baseline_rejects_altered_content_rows_or_checksum(self):
        payload = certified_baseline_payload()
        payload["inventories"]["source_database_content"]["row_count"] = 12
        with self.assertRaisesRegex(CertifiedBaselineError, "source content"):
            validate_versioned_baseline_artifact(payload, CERT_SHA)

        state = current_state_payload()
        state["source_content"]["checksum_sha256"] = "c" * 64
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        with self.assertRaisesRegex(CertifiedBaselineError, "source content"):
            verify_current_state_matches_certified_baseline(baseline, state)

    def test_versioned_certified_baseline_rejects_altered_provenance_rows_or_checksum(self):
        payload = certified_baseline_payload()
        payload["inventories"]["certified_append_journal_provenance"]["row_count"] = 19
        with self.assertRaisesRegex(CertifiedBaselineError, "provenance"):
            validate_versioned_baseline_artifact(payload, CERT_SHA)

        state = current_state_payload()
        state["provenance"]["checksum_sha256"] = "d" * 64
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        with self.assertRaisesRegex(CertifiedBaselineError, "provenance"):
            verify_current_state_matches_certified_baseline(baseline, state)

    def test_versioned_certified_baseline_rejects_unknown_extra_content_or_provenance_file(self):
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        state = current_state_payload()
        state["source_content"]["unknown_extra_files"] = ["unexpected.bin"]
        with self.assertRaisesRegex(CertifiedBaselineError, "unknown extra content"):
            verify_current_state_matches_certified_baseline(baseline, state)

        state = current_state_payload()
        state["provenance"]["unknown_extra_files"] = ["certified_append_journal/unexpected.json"]
        with self.assertRaisesRegex(CertifiedBaselineError, "unknown extra provenance"):
            verify_current_state_matches_certified_baseline(baseline, state)

    def test_versioned_certified_baseline_rejects_changed_selection(self):
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        state = current_state_payload()
        state["active_generation_path"] = str(self.new)
        with self.assertRaisesRegex(CertifiedBaselineError, "selection"):
            verify_current_state_matches_certified_baseline(baseline, state)

    def test_versioned_certified_baseline_rejects_changed_semantic_counts_or_orphans(self):
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        state = current_state_payload()
        state["semantic_state"]["sqlite_embeddings"] = 126075
        with self.assertRaisesRegex(CertifiedBaselineError, "semantic"):
            verify_current_state_matches_certified_baseline(baseline, state)

        state = current_state_payload()
        state["orphan_binding"]["set_comparison_totals"]["current_only"] = 1
        with self.assertRaisesRegex(CertifiedBaselineError, "55-orphan"):
            verify_current_state_matches_certified_baseline(baseline, state)

    def test_versioned_certified_baseline_rejects_unresolved_lock_or_writer_state(self):
        baseline = validate_versioned_baseline_artifact(certified_baseline_payload(), CERT_SHA)
        state = current_state_payload()
        state["semantic_state"]["wal_shm_lock_state"]["chroma.sqlite3-wal"] = True
        with self.assertRaisesRegex(CertifiedBaselineError, "WAL/SHM/lock"):
            verify_current_state_matches_certified_baseline(baseline, state)

        state = current_state_payload()
        state["semantic_state"]["active_writer_candidates"] = "123 writer"
        with self.assertRaisesRegex(CertifiedBaselineError, "writer"):
            verify_current_state_matches_certified_baseline(baseline, state)

    def test_versioned_certified_baseline_rejects_missing_historical_disclosure(self):
        payload = certified_baseline_payload()
        payload["historical_disclosure"]["rolled_back_append_not_retrospectively_proven_clean"] = False
        with self.assertRaisesRegex(CertifiedBaselineError, "historical disclosure"):
            validate_versioned_baseline_artifact(payload, CERT_SHA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
