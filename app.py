#!/usr/bin/env python3
"""Thin Gradio UI over rag.query.answer."""

from __future__ import annotations

import gradio as gr

from rag.config import DEFAULT_K, KNOWN_SCOPES
from rag.query import answer

SCOPE_CHOICES = ["(all)"] + list(KNOWN_SCOPES)


def _run(question: str, scope_label: str, k: int):
    scope = None if not scope_label or scope_label == "(all)" else scope_label
    text, sources = answer(question, scope=scope, k=int(k))
    if sources:
        lines = [
            f"- [{s['collection']}] {s['source']} — page {s['page']}" for s in sources
        ]
        sources_md = "\n".join(lines)
    else:
        sources_md = "_No sources._"
    return text, sources_md


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Local Scoped RAG") as demo:
        gr.Markdown(
            "## Local manuals RAG\n"
            "One Chroma DB over CE_Library + project `data/`. "
            "Use **scope** to filter collections (e.g. `me-c`)."
        )
        question = gr.Textbox(label="Question", lines=2, placeholder="Ask about a manual…")
        with gr.Row():
            scope = gr.Dropdown(SCOPE_CHOICES, value="(all)", label="Scope")
            k = gr.Slider(1, 12, value=DEFAULT_K, step=1, label="Top-k")
        ask_btn = gr.Button("Ask", variant="primary")
        answer_box = gr.Textbox(label="Answer", lines=10)
        sources_box = gr.Markdown(label="Sources")
        ask_btn.click(_run, [question, scope, k], [answer_box, sources_box])
        question.submit(_run, [question, scope, k], [answer_box, sources_box])
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="127.0.0.1", server_port=7861, share=False)
