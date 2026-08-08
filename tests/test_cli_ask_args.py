"""cmd_ask's --scope must survive regardless of where it falls relative to
the question. argparse.REMAINDER used to silently vacuum --scope (and every
other flag) into the question text whenever it was typed after the question
— resolve_scope() then never ran, and retrieval fell back to an unscoped,
corpus-wide search with no error or warning. nargs="*" fixes that; these
tests pin the fix in place."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rag_engine.cli as cli
from rag_engine.query import EXIT_ERROR, EXIT_OK, AskResult


def _ok_result(scope: str) -> AskResult:
    return AskResult(
        status="ok",
        query="q",
        requested_scope=scope,
        resolved_scope=scope,
        answer="the answer",
        sources=[{"path": "a.md", "page": 1, "collection": scope, "distance": 0.1}],
        coverage="full",
    )


def _run(argv, resolved_scope="wiki"):
    """Run cmd_ask with resolve_scope/answer mocked; return (exit_code, captured answer() kwargs)."""
    captured = {}

    def fake_answer(question, **kwargs):
        captured["question"] = question
        captured["scope"] = kwargs.get("scope")
        return _ok_result(kwargs.get("scope"))

    with patch.object(cli, "resolve_scope", return_value=resolved_scope), patch.object(
        cli, "answer", side_effect=fake_answer
    ), patch.object(cli, "log_ask_event"):
        code = cli.cmd_ask(argv)
    return code, captured


def test_scope_before_question_quoted():
    code, captured = _run(["--scope", "wiki", "which folder holds the manual"])
    assert code == EXIT_OK
    assert captured["scope"] == "wiki"
    assert captured["question"] == "which folder holds the manual"


def test_scope_after_question_quoted_no_longer_dropped():
    """The exact bug: --scope typed after a quoted question used to vanish
    into args.question, leaving scope=None (unscoped search)."""
    code, captured = _run(["which folder holds the manual", "--scope", "wiki", "--json"])
    assert code == EXIT_OK
    assert captured["scope"] == "wiki"
    assert captured["question"] == "which folder holds the manual"


def test_scope_after_unquoted_multiword_question():
    code, captured = _run(
        ["which", "folder", "holds", "the", "manual", "--scope", "wiki", "--json"]
    )
    assert code == EXIT_OK
    assert captured["scope"] == "wiki"
    assert captured["question"] == "which folder holds the manual"


def test_flag_token_in_question_position_is_rejected(capsys):
    """Defense-in-depth: a flag-shaped token that still ends up assigned to
    the question positional must error, not silently become question text."""
    with patch.object(cli, "resolve_scope", return_value="wiki"), patch.object(
        cli, "answer"
    ) as fake_answer:
        code = cli.cmd_ask(["--scope", "wiki", "--", "--scope"])
    # argparse's "--" separator forces the literal "--scope" into the
    # positional; the guard must catch it before answer() is ever called.
    assert code == EXIT_ERROR
    fake_answer.assert_not_called()


def test_missing_question_still_handled_cleanly():
    """nargs="*" (not "+") so a question-less invocation still goes through
    the existing JSON-contract error path instead of an uncaught argparse
    SystemExit."""
    code, captured = _run(["--scope", "wiki", "--json"])
    assert code == EXIT_ERROR
    assert captured == {}


def test_cmd_ask_logs_gate_and_score_floor_for_no_coverage():
    result = AskResult(
        status="no_coverage",
        query="q",
        requested_scope="wiki",
        resolved_scope="wiki",
        answer=None,
        sources=[],
        retrieval_evidence=[{"path": "x.pdf", "page": 1, "collection": "wiki", "distance": 0.41}],
        gate="no_chunk_cleared_score_floor",
        coverage="none",
    )
    with patch.object(cli, "resolve_scope", return_value="wiki"), patch.object(
        cli, "answer", return_value=result
    ), patch.object(cli, "log_ask_event") as fake_log:
        code = cli.cmd_ask(["--scope", "wiki", "--json", "missing detail"])

    assert code == 2
    fake_log.assert_called_once()
    kwargs = fake_log.call_args.kwargs
    assert kwargs["gate"] == "no_chunk_cleared_score_floor"
    assert kwargs["score_floor"] is not None
    assert kwargs["best_distance"] == 0.41
