"""Local scoped RAG over CE_Library + project manuals."""

__all__ = ["answer"]


def __getattr__(name: str):
    if name == "answer":
        from rag.query import answer

        return answer
    raise AttributeError(name)
