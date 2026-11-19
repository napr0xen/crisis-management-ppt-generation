"""Guardrail 1: only `vector/qdrant_store.py` may talk to Qdrant (docs/testing-strategy.md § 4)."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "doc_intel"
ALLOWED = SRC / "vector" / "qdrant_store.py"


def _imports_qdrant(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(n == "qdrant_client" or n.startswith("qdrant_client.") for n in names):
            return True
    return False


def test_only_qdrant_store_imports_qdrant_client():
    offenders = [
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if p != ALLOWED and _imports_qdrant(p)
    ]
    assert offenders == []


def test_qdrant_store_is_the_importer():
    assert _imports_qdrant(ALLOWED)
