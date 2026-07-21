#!/usr/bin/env python3
"""Thin Gradio UI over rag_engine.query.answer."""

from __future__ import annotations

import gradio as gr

from rag_engine.config import default_k, known_scopes
from rag_engine.query import answer

SCOPE_CHOICES = ["(all)"] + list(known_scopes())


def _run(question: str, scope_label: str, k: int):
    scope = None if not scope_label or scope_label == "(all)" else scope_label
    text, sources, status = answer(question, scope=scope, k=int(k))
    if sources:
        lines = [
            f"- [{s['collection']}] {s['path']} — page {s['page']} (score={s.get('score')})"
            for s in sources
        ]
        sources_md = "\n".join(lines)
    else:
        sources_md = "_No sources._"
    return text, sources_md, status


def build_app() -> gr.Blocks:
    with gr.Blocks(title="rag-engine") as demo:
        gr.Markdown(
            "## rag-engine\n"
            "Local scoped RAG over the client library (`CE_LIBRARY_ROOT`). "
            "Use **scope** to filter collections."
        )
        question = gr.Textbox(label="Question", lines=2)
        with gr.Row():
            scope = gr.Dropdown(SCOPE_CHOICES, value="(all)", label="Scope")
            k = gr.Slider(1, 12, value=default_k(), step=1, label="Top-k")
        ask_btn = gr.Button("Ask", variant="primary")
        answer_box = gr.Textbox(label="Answer", lines=10)
        status_box = gr.Textbox(label="Status", lines=1)
        sources_box = gr.Markdown(label="Sources")
        ask_btn.click(_run, [question, scope, k], [answer_box, sources_box, status_box])
        question.submit(_run, [question, scope, k], [answer_box, sources_box, status_box])
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="127.0.0.1", server_port=7861, share=False)
