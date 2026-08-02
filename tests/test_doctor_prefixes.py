from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.doctor import _scope_prefixes_indexed_check  # noqa: E402


def _registry(prefixes):
    return {
        "scopes": {
            "mixed": {"path_prefixes": prefixes},
        }
    }


def test_two_prefixes_both_populated(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    check = _scope_prefixes_indexed_check(root, _registry(["a/", "b/"]), ["a/doc1.pdf", "b/doc2.pdf"])
    assert check["ok"] is True
    assert "mixed:a/ ok (1 docs)" in check["detail"]
    assert "mixed:b/ ok (1 docs)" in check["detail"]



def test_first_populated_second_empty(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    check = _scope_prefixes_indexed_check(root, _registry(["a/", "b/"]), ["a/doc1.pdf"])
    assert check["ok"] is False
    assert "mixed:a/ ok (1 docs)" in check["detail"]
    assert "mixed:b/ dir ok but 0 indexed docs for prefix" in check["detail"]



def test_first_empty_second_populated(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    check = _scope_prefixes_indexed_check(root, _registry(["a/", "b/"]), ["b/doc2.pdf"])
    assert check["ok"] is False
    assert "mixed:a/ dir ok but 0 indexed docs for prefix" in check["detail"]
    assert "mixed:b/ ok (1 docs)" in check["detail"]



def test_path_normalization_accepts_absolute_tracker_paths(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    absolute = str((root / "a" / "doc1.pdf").resolve())
    check = _scope_prefixes_indexed_check(root, _registry(["a"]), [absolute])
    assert check["ok"] is True
    assert "mixed:a ok (1 docs)" in check["detail"]



def test_single_prefix_scope(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "only").mkdir(parents=True)
    check = _scope_prefixes_indexed_check(root, _registry(["only/"]), ["only/doc.pdf"])
    assert check["ok"] is True
    assert check["name"] == "scope_prefixes_indexed"
    assert check["detail"] == "mixed:only/ ok (1 docs)"
