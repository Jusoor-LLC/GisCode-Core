"""Find a symbol's definitions, usages and string mentions in a project.

Used by the audit fixtures and by anyone checking whether a handler is really
referenced. The scan is read-only and bounded to Python files under *root*.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Union

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}


def find_references(symbol: str, root: Union[str, Path] = ".") -> dict:
    """Return definitions, usages and string occurrences of *symbol*.

    ``definitions`` names function/class/assignment definitions, ``usages``
    names load references, and ``string_occurrences`` names string literals
    that mention the symbol.
    """
    name = str(symbol or "").strip()
    if not name:
        raise ValueError("symbol must not be empty")
    base = Path(root)
    definitions: list[str] = []
    usages: list[str] = []
    string_occurrences: list[str] = []
    if not base.is_dir():
        return {
            "definitions": definitions,
            "usages": usages,
            "string_occurrences": string_occurrences,
        }
    for path in sorted(base.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        relative = str(path.relative_to(base)).replace("\\", "/")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    definitions.append(f"{relative}:{node.lineno}")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        definitions.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Name) and node.id == name:
                if isinstance(node.ctx, ast.Load):
                    usages.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == name:
                usages.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if name in node.value:
                    string_occurrences.append(f"{relative}:{node.lineno}")
    return {
        "definitions": definitions,
        "usages": usages,
        "string_occurrences": string_occurrences,
    }
