#!/usr/bin/env python3
"""AI Chief Engineer v1 Alpha — thin Gradio shell over rag_engine.answer."""

from __future__ import annotations

from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

from rag_engine.chief_ui import ask, format_health_markdown, health_snapshot
from rag_engine.config import library_root

# Existing env handling: load local .env if present; never store keys in UI code.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


def _ask_click(question: str, _pending: str):
    payload = ask(question or "")
    clarification = bool(payload["clarification_required"])
    pending = payload["pending_question"] if clarification else ""
    return (
        payload["answer"],
        payload["status"],
        payload["sources_md"],
        payload["sources_copy"],
        gr.update(visible=clarification),
        payload["clarification_prompt"] if clarification else "",
        "",
        pending,
        format_health_markdown(),
    )


def _continue_click(confirmation: str, pending_question: str):
    q = (pending_question or "").strip()
    if not q:
        return (
            "No pending question to clarify. Ask a new question first.",
            "error",
            "_No sources._",
            "",
            gr.update(visible=False),
            "",
            "",
            "",
            format_health_markdown(),
        )
    payload = ask(q, confirmation_text=confirmation or "")
    clarification = bool(payload["clarification_required"])
    pending = payload["pending_question"] if clarification else ""
    return (
        payload["answer"],
        payload["status"],
        payload["sources_md"],
        payload["sources_copy"],
        gr.update(visible=clarification),
        payload["clarification_prompt"] if clarification else "",
        confirmation if clarification else "",
        pending,
        format_health_markdown(),
    )


def _refresh_health():
    return format_health_markdown(health_snapshot())


def build_app() -> gr.Blocks:
    with gr.Blocks(title="AI Chief Engineer") as demo:
        gr.Markdown("# AI Chief Engineer")
        gr.Markdown(
            "Local Alpha shell over `rag_engine.answer()` — retrieval, "
            "clarification, generation, and citations stay inside the engine."
        )

        health_box = gr.Markdown(value=format_health_markdown())
        refresh_btn = gr.Button("Refresh health", size="sm")

        question = gr.Textbox(
            label="Ask your engineering question...",
            lines=3,
            placeholder="e.g. What is the exhaust valve spindle tightening torque for M 1.3?",
        )
        ask_btn = gr.Button("Ask", variant="primary")

        status_box = gr.Textbox(label="Status", lines=1, interactive=False)
        answer_box = gr.Textbox(
            label="Answer",
            lines=12,
            interactive=False,
            show_copy_button=True,
        )

        with gr.Group(visible=False) as clarification_group:
            gr.Markdown("### Clarification required")
            clarification_prompt = gr.Textbox(
                label="AI Chief Engineer asks",
                lines=2,
                interactive=False,
            )
            confirmation = gr.Textbox(
                label="Your confirmation",
                lines=2,
                placeholder="e.g. MAN / M 1.3 main engine",
            )
            continue_btn = gr.Button("Continue", variant="primary")

        sources_box = gr.Markdown(label="Sources")
        sources_copy = gr.Textbox(
            label="Sources (copy)",
            lines=4,
            interactive=False,
            show_copy_button=True,
        )

        pending = gr.State("")

        outputs = [
            answer_box,
            status_box,
            sources_box,
            sources_copy,
            clarification_group,
            clarification_prompt,
            confirmation,
            pending,
            health_box,
        ]

        ask_btn.click(_ask_click, [question, pending], outputs)
        question.submit(_ask_click, [question, pending], outputs)
        continue_btn.click(_continue_click, [confirmation, pending], outputs)
        refresh_btn.click(_refresh_health, outputs=[health_box])

        gr.Markdown(
            "_Alpha limitations: single-user local UI; no PMS/defect/correspondence "
            "modules; no admin reindex controls; no multi-user auth._"
        )
    return demo


if __name__ == "__main__":
    build_app().launch(
        server_name="127.0.0.1",
        server_port=7861,
        share=False,
        allowed_paths=[str(library_root())],
    )
