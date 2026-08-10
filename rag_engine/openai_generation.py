"""OpenAI Responses API answer generation with lazy SDK import.

ORCH_104: archived / unused by the production ask path.
rag_engine is retrieval-only; Hermes owns final answer generation.
This module is retained for rollback and unit tests — do not wire it
back into query.answer() without an explicit operator decision.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


class OpenAIGenerationError(RuntimeError):
    """Generation failed before a usable answer was returned."""


class OpenAIMisconfiguredError(OpenAIGenerationError):
    """Generation cannot start because required local configuration is missing."""


@dataclass(frozen=True)
class GenerationResult:
    text: str
    usage: dict[str, int] | None = None
    response_id: str | None = None


DEFAULT_OPENAI_TIMEOUT_S = 60.0


def openai_timeout_s() -> float:
    return float(os.environ.get("RAG_OPENAI_TIMEOUT", str(DEFAULT_OPENAI_TIMEOUT_S)))


@lru_cache(maxsize=1)
def _openai_class():
    try:
        module = importlib.import_module("openai")
    except ModuleNotFoundError as e:
        raise OpenAIMisconfiguredError(
            "OpenAI SDK not installed; install the `openai` package in this environment"
        ) from e
    return module.OpenAI


@lru_cache(maxsize=8)
def _get_client(timeout: float):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIMisconfiguredError("OPENAI_API_KEY is not set")
    openai_cls = _openai_class()
    return openai_cls(api_key=api_key, timeout=timeout)


def clear_caches() -> None:
    _openai_class.cache_clear()
    _get_client.cache_clear()


def _extract_usage(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    payload: dict[str, int] = {}
    for attr in ("input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            payload[attr] = value
    return payload or None


def _safe_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message


def invoke_openai_response(
    model: str,
    prompt: str,
    *,
    timeout: float | None = None,
) -> GenerationResult:
    timeout_s = openai_timeout_s() if timeout is None else float(timeout)
    client = _get_client(timeout_s)
    try:
        response = client.responses.create(model=model, input=prompt)
    except Exception as e:  # noqa: BLE001
        raise OpenAIGenerationError(_safe_message(e)) from e

    text = str(getattr(response, "output_text", "") or "").strip()
    return GenerationResult(
        text=text,
        usage=_extract_usage(response),
        response_id=getattr(response, "id", None),
    )
