"""Package-local helpers for LC6 synthetic E2E report handling.

The full E2E workflow is intentionally not executed by this module. These helpers
support governed driver behavior and evidence preservation tests.
"""

from __future__ import annotations

import json
import shutil
import traceback
from pathlib import Path
from typing import Any


def ensure_work_directory(journal_file: str | Path, fixture_root: str | Path) -> Path:
    """Create a synthetic work directory inside the fixture before switching."""
    journal = Path(journal_file)
    fixture = Path(fixture_root).resolve(strict=True)
    parent = journal.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved = parent.resolve(strict=True)
    try:
        resolved.relative_to(fixture)
    except ValueError as exc:
        raise ValueError(f"journal work directory is outside fixture: {resolved}") from exc
    return resolved


def build_failure_report(
    *,
    exc: BaseException,
    stage: str,
    operation: str,
    target_path: str | Path | None,
    fixture_root: str | Path,
    preserve_failed_fixture: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a failure report with full traceback and fixture-preservation state."""
    fixture = Path(fixture_root)
    payload: dict[str, Any] = {
        "schema": "lc6-promotion-synthetic-e2e-failure-report-v1",
        "verdict": "LC6_PROMOTION_CONTROLS_SYNTHETIC_E2E_FAIL_PRESERVED",
        "stage": stage,
        "operation": operation,
        "target_path": str(target_path) if target_path is not None else None,
        "exception_type": type(exc).__name__,
        "exception_repr": repr(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "fixture": {
            "path": str(fixture),
            "preserve_failed_fixture": preserve_failed_fixture,
            "exists_at_report_build": fixture.exists(),
            "removed_after_report": False,
        },
    }
    if extra:
        payload["extra"] = extra
    return payload


def write_json_report(path: str | Path, payload: dict[str, Any]) -> None:
    report = Path(path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def cleanup_fixture_after_result(fixture_root: str | Path, *, success: bool, preserve_failed_fixture: bool = True) -> bool:
    """Remove fixture only for success, unless failed-fixture preservation is disabled."""
    fixture = Path(fixture_root)
    if success or not preserve_failed_fixture:
        shutil.rmtree(fixture, ignore_errors=True)
    return not fixture.exists()
