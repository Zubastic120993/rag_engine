"""rag-engine — local scoped RAG tool over a client document library."""

from rag_engine.query import answer

__all__ = ["answer"]


def __getattr__(name: str):
    if name == "answer":
        from rag_engine.query import answer as _answer

        return _answer
    raise AttributeError(name)
