#!/usr/bin/env python3
"""Thin Gradio UI over rag_engine.query.answer with safe PDF citations."""

from __future__ import annotations

import gradio as gr

from rag_engine.config import default_k, known_scopes, library_root
from rag_engine.pdf_links import source_open_markdown
from rag_engine.query import answer

SCOPE_CHOICES = ["(all)"] + list(known_scopes())


def _run(question: str, scope_label: str, k: int):
    scope = None if not scope_label or scope_label == "(all)" else scope_label
    result = answer(question, scope=scope, k=int(k), requested_scope=scope_label if scope else None)
    if result.status == "ok" and result.sources:
        lines = []
        for s in result.sources:
            link = source_open_markdown(str(s.get("path") or ""), s.get("page"), root=library_root())
            lines.append(
                f"- [{s.get('collection')}] {link} — stored_page={s.get('page')} "
                f"(distance={s.get('distance')})"
            )
        sources_md = "\n".join(lines)
        sources_md += (
            "\n\n_PDF `#page=N` works in Chrome/Firefox built-in viewers; "
            "Safari’s viewer is unreliable._"
        )
    elif result.hint:
        sources_md = f"_{result.hint}_"
    else:
        sources_md = "_No sources._"
    text = result.answer or (
        "I do not know — not specified in the retrieved documents."
        if result.status == "no_coverage"
        else (result.error or "")
    )
    return text, sources_md, result.status


def build_app() -> gr.Blocks:
    with gr.Blocks(title="rag-engine") as demo:
        gr.Markdown(
            "## rag-engine\n"
            "Local scoped RAG over the client library (`CE_LIBRARY_ROOT`). "
            "Use **scope** to filter collections. Cited PDFs open via a safe "
            "local file route with `#page=N` (1-based viewer page)."
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
    # allowed_paths restricts Gradio file serving to the library root
    build_app().launch(
        server_name="127.0.0.1",
        server_port=7861,
        share=False,
        allowed_paths=[str(library_root())],
    )
