#!/usr/bin/env python3
"""Thin wrapper — prefer `rag-engine ask` after `pip install -e .`."""

from rag_engine.cli import cmd_ask

if __name__ == "__main__":
    import sys

    raise SystemExit(cmd_ask(sys.argv[1:]))
