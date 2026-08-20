"""Read-only ce-library-manager CLI adapter.

Calls ``resolve_library_state`` only. Never mutates stores or executes plans.
Requires explicit absolute paths; never defaults to production locations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from rag_engine.library_state.contract import (
    OP_CERTIFIED_APPEND_PROPOSAL,
    OP_ALIAS_REGISTER,
    OP_METADATA_ONLY_RECONCILE,
    OP_NO_OP,
    OP_RETIREMENT_PROPOSAL,
    SUPPORTED_INTENTS,
)
from rag_engine.library_state.plan import OperationPlan
from rag_engine.library_state.resolver import resolve_library_state

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_RESOLVER = 3

FORBIDDEN_FLAGS = frozenset(
    {
        "--execute",
        "--apply",
        "--approve",
        "--route",
        "--repair",
    }
)
READ_ONLY_BOUNDARY = (
    "ce-library-manager is read-only and proposal-only; execution is not supported"
)
NOT_AUTHORIZED = (
    "NOT AUTHORIZED FOR EXECUTION -- separate approved governing contract required"
)

_MUTATION_OPERATIONS = frozenset(
    {
        OP_METADATA_ONLY_RECONCILE,
        OP_ALIAS_REGISTER,
        OP_CERTIFIED_APPEND_PROPOSAL,
        OP_RETIREMENT_PROPOSAL,
    }
)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _forbidden_flag(argv: Sequence[str]) -> str | None:
    for token in argv:
        base = token.split("=", 1)[0]
        if base in FORBIDDEN_FLAGS:
            return base
    return None


def _require_absolute(path_value: str, name: str) -> Path:
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError(f"{name} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {raw!r}")
    return path


def _validate_target(raw: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise ValueError("target path is required")
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError(f"target must be relative to library_root, not absolute: {raw!r}")
    parts = value.split("/")
    if ".." in parts:
        raise ValueError(f"target must not contain traversal segments: {raw!r}")
    return value


def _request_dict(args: argparse.Namespace) -> dict[str, Any]:
    req: dict[str, Any] = {
        "intent": args.intent,
        "library_root": str(args.library_root),
        "targets": list(args.target),
    }
    if args.persist_dir is not None:
        req["persist_dir"] = str(args.persist_dir)
    if args.registry_db is not None:
        req["registry_db"] = str(args.registry_db)
    if args.tracker_path is not None:
        req["tracker_path"] = str(args.tracker_path)
    if args.journal_path is not None:
        req["journal_path"] = str(args.journal_path)
    if args.operation_id is not None:
        req["operation_id"] = args.operation_id
    return req


def execution_boundary(plan: OperationPlan) -> str:
    if plan.proposed_operation == OP_NO_OP:
        return "Read-only completion; no execution occurred."
    if plan.proposed_operation in _MUTATION_OPERATIONS:
        return NOT_AUTHORIZED
    return f"{NOT_AUTHORIZED}; manual review required."


def plan_payload(plan: OperationPlan, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "request": request,
        "request_id": plan.request_id,
        "intent": plan.intent,
        "targets": list(plan.targets),
        "classification": plan.classification,
        "result": plan.result,
        "evidence_summary": plan.evidence_summary,
        "evidence_gaps": list(plan.evidence_gaps),
        "proposed_operation": plan.proposed_operation,
        "approval": plan.approval,
        "embedding_action": plan.embedding_action,
        "verification_contract": dict(plan.verification_contract),
        "execution_boundary": execution_boundary(plan),
        "classifications": [c.to_dict() for c in plan.classifications],
        "affected_stores": list(plan.affected_stores),
        "risk_flags": list(plan.risk_flags),
        "ambiguity_flags": list(plan.ambiguity_flags),
        "read_only": True,
        "mutation_performed": False,
    }


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _print_text(payload: dict[str, Any]) -> None:
    req = payload["request"]
    print("### Request")
    print(f"- Intent: {req['intent']}")
    print(f"- Library root: {req['library_root']}")
    print(f"- Target(s): {', '.join(req['targets'])}")
    store_bits: list[str] = []
    for key in ("persist_dir", "registry_db", "tracker_path", "journal_path"):
        if key in req:
            store_bits.append(f"{key}={req[key]}")
    print(f"- Store paths: {', '.join(store_bits) if store_bits else '(none explicit)'}")
    print()
    print("### Observed state")
    print(f"- Classification: {payload['classification']}")
    print(f"- Result: {payload['result']}")
    for item in payload.get("classifications") or []:
        print(
            f"  - {item['target']}: {item['classification']} "
            f"(operation={item['proposed_operation']}, result={item['result']})"
        )
    print()
    print("### Evidence and gaps")
    print(f"- Evidence summary: {payload['evidence_summary']}")
    gaps = payload.get("evidence_gaps") or []
    print(f"- Gaps: {', '.join(gaps) if gaps else 'none'}")
    risk = payload.get("risk_flags") or []
    amb = payload.get("ambiguity_flags") or []
    if risk:
        print(f"- Risk flags: {', '.join(risk)}")
    if amb:
        print(f"- Ambiguity flags: {', '.join(amb)}")
    print()
    print("### Proposed operation")
    print(f"- Operation: {payload['proposed_operation']}")
    print(f"- Approval: {payload['approval']}")
    print(f"- Embedding action: {payload['embedding_action']}")
    vcontract = payload.get("verification_contract") or {}
    require = vcontract.get("require") or []
    print(f"- Verification requirements: {', '.join(require) if require else '(none)'}")
    print()
    print("### Execution authority")
    boundary = payload["execution_boundary"]
    if payload["proposed_operation"] == OP_NO_OP and boundary.startswith("Read-only"):
        print(f"- {boundary}")
    else:
        print(f"- {boundary}")
    print()
    print("### Stop boundary")
    print(
        "- This command observed and proposed only. "
        "No mutation, routing, or execution was performed or authorized."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_engine.library_state",
        description=(
            "Read-only ce-library-manager adapter. "
            "Proposes operation plans only; never executes mutations."
        ),
    )
    parser.add_argument(
        "--intent",
        required=True,
        choices=sorted(SUPPORTED_INTENTS),
        help="Supported resolver intent",
    )
    parser.add_argument(
        "--library-root",
        required=True,
        help="Explicit absolute library root directory",
    )
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        dest="target",
        metavar="REL_PATH",
        help="Target path relative to library_root (repeatable)",
    )
    parser.add_argument(
        "--persist-dir",
        default=None,
        help="Explicit absolute RAG persist directory (optional)",
    )
    parser.add_argument(
        "--registry-db",
        default=None,
        help="Explicit absolute registry sqlite path (optional)",
    )
    parser.add_argument(
        "--tracker-path",
        default=None,
        help="Explicit absolute tracker JSON path (optional)",
    )
    parser.add_argument(
        "--journal-path",
        default=None,
        help="Explicit absolute certified-append journal file (optional)",
    )
    parser.add_argument(
        "--operation-id",
        default=None,
        help="Exact certified-append operation id for journal lookup (optional)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forbidden = _forbidden_flag(argv)
    if forbidden is not None:
        _stderr(f"{READ_ONLY_BOUNDARY} (flag {forbidden!r} rejected)")
        return EXIT_INPUT

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return EXIT_OK
        return EXIT_INPUT

    if args.intent not in SUPPORTED_INTENTS:
        _stderr(f"unsupported intent: {args.intent!r}")
        return EXIT_INPUT

    try:
        library_root = _require_absolute(args.library_root, "library_root")
        persist_dir = (
            _require_absolute(args.persist_dir, "persist_dir")
            if args.persist_dir is not None
            else None
        )
        registry_db = (
            _require_absolute(args.registry_db, "registry_db")
            if args.registry_db is not None
            else None
        )
        tracker_path = (
            _require_absolute(args.tracker_path, "tracker_path")
            if args.tracker_path is not None
            else None
        )
        journal_path = (
            _require_absolute(args.journal_path, "journal_path")
            if args.journal_path is not None
            else None
        )
        targets = [_validate_target(t) for t in args.target]
    except ValueError as exc:
        _stderr(str(exc))
        return EXIT_INPUT

    request = _request_dict(args)

    try:
        plan = resolve_library_state(
            args.intent,
            targets,
            library_root=library_root,
            persist_dir=persist_dir,
            registry_db=registry_db,
            tracker_path=tracker_path,
            journal_path=journal_path,
            operation_id=args.operation_id,
        )
    except ValueError as exc:
        _stderr(str(exc))
        return EXIT_RESOLVER
    except OSError as exc:
        _stderr(f"evidence error: {exc}")
        return EXIT_RESOLVER

    payload = plan_payload(plan, request)
    if args.format == "json":
        _print_json(payload)
    else:
        _print_text(payload)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
