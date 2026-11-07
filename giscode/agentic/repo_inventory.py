"""Language-aware source inventory for planning.

The code-first planner and the JSON planner prompt were written for Python and
only looked at ``*.py`` files. On a Kotlin/Gradle project that produced a plan
with no real repository paths (and the code-first planner refused the request
outright). This module gives both a small, bounded view of the actual source
tree, plus a regex symbol reader for Kotlin/Java files.

It is deliberately shallow: it lists files, never writes, never executes, and
caps how much it looks at so a huge repository cannot stall planning.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Directories that never contain hand-written source worth planning against.
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        ".kotlin",
        ".venv",
        ".vs",
        "build",
        "dist",
        "node_modules",
        "out",
        "target",
        "venv",
        "__pycache__",
    }
)

SOURCE_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".kt",
    ".kts",
    ".java",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".cs",
    ".go",
    ".rs",
    ".swift",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
)

#: The planner prompt shows at most this many paths.
DEFAULT_FILE_LIMIT = 30

#: Suffixes whose declarations this module can read (Kotlin/Java-style ``fun``).
KOTLIN_SUFFIXES: tuple[str, ...] = (".kt", ".kts")

#: At most this share of the model's context window, in characters, is spent
#: on the declaration block. Characters, not tokens: deliberately conservative.
DECLARATION_CAP_FRACTION = 0.15

#: ... and never more than this, whatever the context window claims.
MAX_DECLARATION_CHARS = 6000

#: Context window assumed when the caller cannot say what the model has.
DEFAULT_CONTEXT_WINDOW = 4096

#: How many request-named files get a declaration block (the request rarely
#: names more than one or two, and the block is not the place for a dump).
DECLARATION_FILE_LIMIT = 3

#: ``fun``/``class``/``object``/``interface`` declarations, with modifiers.
_KOTLIN_DECLARATION = re.compile(
    r"^[ \t]*"
    r"(?:(?:public|private|internal|protected|open|override|suspend|inline|"
    r"operator|data|sealed|abstract|enum|annotation|companion|const|lateinit|"
    r"tailrec|external|actual|expect|value)\s+)*"
    r"(class|interface|object|fun)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _is_excluded(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in _EXCLUDED_DIRS for part in parts[:-1])


def list_source_files(
    root: str | Path,
    limit: int = DEFAULT_FILE_LIMIT,
    *,
    issues: list[str] | None = None,
) -> list[str]:
    """Relative POSIX paths of source files under ``root``, sorted.

    ``limit`` is a hard cap so the planner prompt stays bounded. Excluded
    directories are pruned before they are entered: ``rglob`` used to walk
    into them and then filter, so a dangling junction under ``node_modules``
    raised before the exclusion could apply. A directory that cannot be
    listed is reported through ``issues`` (root-relative path, one entry per
    failure) and traversal continues, so a partial inventory is never
    mistaken for a complete empty one.
    """
    if limit <= 0:
        return []
    base = Path(root)
    if not base.is_dir():
        return []

    def _record(error: OSError) -> None:
        raw = getattr(error, "filename", None)
        relative = "."
        if raw:
            try:
                relative = Path(raw).relative_to(base).as_posix()
            except (OSError, ValueError):
                relative = "."
        if issues is not None:
            issues.append(relative)
        logger.warning(
            "source inventory could not read %s (%s); the inventory is incomplete",
            relative,
            type(error).__name__,
        )

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, onerror=_record):
        dirnames[:] = sorted(name for name in dirnames if name not in _EXCLUDED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if _is_excluded(path, base):
                continue
            found.append(path.relative_to(base).as_posix())
            if len(found) >= limit:
                return sorted(found)
    return sorted(found)


def foreign_source_files(
    root: str | Path, limit: int = 50, *, issues: list[str] | None = None
) -> list[str]:
    """Source files that are not Python - what a Python-only planner cannot read."""
    return [
        rel
        for rel in list_source_files(root, limit=max(limit * 4, limit), issues=issues)
        if not rel.endswith(".py")
    ][:limit]


def read_source(path: str | Path, *, max_bytes: int = 200_000) -> str:
    """Read a source file without ever raising; an unreadable file is ``""``."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[:max_bytes].decode("utf-8", errors="replace")


def declaration_symbols(text: str) -> list[dict[str, object]]:
    """Kotlin/Java declarations as ``{"kind", "name", "line"}`` dictionaries."""
    out: list[dict[str, object]] = []
    for match in _KOTLIN_DECLARATION.finditer(text or ""):
        kind, name = match.group(1), match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        out.append({"kind": kind, "name": name, "line": line})
    return out


def find_declarations(root: str | Path, name: str) -> list[dict[str, object]]:
    """Every ``file``/declaration whose name matches exactly, in repo order."""
    hits: list[dict[str, object]] = []
    base = Path(root)
    if not base.is_dir() or not name:
        return hits
    for rel in list_source_files(base, limit=500):
        if Path(rel).suffix.lower() not in {".kt", ".kts", ".java"}:
            continue
        text = read_source(base / rel)
        for symbol in declaration_symbols(text):
            if symbol["name"] == name:
                hits.append({"path": rel, **symbol})
    return hits


def declaration_char_cap(n_ctx: object) -> int:
    """Characters allowed for the declaration block at this context window.

    Scales with the model's own context (``DECLARATION_CAP_FRACTION``) and is
    hard-capped, so a large ``n_ctx`` cannot push the rest of the prompt out and
    an unusable value falls back to ``DEFAULT_CONTEXT_WINDOW`` instead of
    failing.
    """
    try:
        window = int(n_ctx)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        window = DEFAULT_CONTEXT_WINDOW
    if window <= 0:
        window = DEFAULT_CONTEXT_WINDOW
    return max(1, min(MAX_DECLARATION_CHARS, int(window * DECLARATION_CAP_FRACTION)))


def _signature_of(block_text: str) -> str:
    """The declaration's own first line, with the body brace removed."""
    first = (block_text or "").splitlines()[0] if (block_text or "").strip() else ""
    first = first.strip()
    while first.endswith("{"):
        first = first[:-1].strip()
    return " ".join(first.split())


def kotlin_declarations(text: str) -> list[dict[str, object]]:
    """Kotlin function declarations as ``{qualified, kind, signature, line}``.

    Qualified names come from the real declaration structure (an enclosing
    ``class``/``object``/``interface`` prefixes its members), so the planner is
    offered exactly the names the editing tool can resolve. Never raises: a file
    this reader cannot parse simply yields no declarations.
    """
    if not text:
        return []
    try:
        from giscode.agentic.kotlin_symbols import collect_kotlin_symbols

        blocks = collect_kotlin_symbols(text)
    except Exception:  # noqa: BLE001 - an unparsable file has no declarations
        return []
    out: list[dict[str, object]] = []
    for block in blocks:
        out.append(
            {
                "qualified": block.qualified,
                "kind": block.kind,
                "signature": _signature_of(block.text),
                "line": block.start_line,
            }
        )
    return out


def request_named_files(
    root: str | Path,
    request_text: str,
    limit: int = DECLARATION_FILE_LIMIT,
) -> list[str]:
    """Kotlin source files the request actually names, in repo order.

    Only files that exist on disk match, and only Kotlin ones: a Python project
    gets no declaration block, so its prompt is unchanged. A request naming no
    file matches nothing, and the caller ships no block.
    """
    text = (request_text or "").replace("\\", "/").lower()
    if not text:
        return []
    named: list[str] = []
    for rel in list_source_files(root, limit=500):
        if Path(rel).suffix.lower() not in KOTLIN_SUFFIXES:
            continue
        lowered = rel.lower()
        if lowered in text or Path(lowered).name in text:
            named.append(rel)
            if len(named) >= max(1, limit):
                break
    return named


def render_declaration_block(
    root: str | Path,
    files: Iterable[str],
    cap_chars: int,
) -> tuple[str, dict[str, int]]:
    """A bounded declaration block for ``files``, plus the counts behind it.

    Returns ``(block, stats)`` where ``stats`` reports the files read, the
    declarations actually rendered and the characters used, so the caller can
    log the cap and what it bought. A block that does not fit is empty rather
    than truncated mid-declaration.
    """
    base = Path(root)
    header = (
        "Declarations in the files this request names (the tool needs a "
        "qualified name from this list; use one exactly as written):"
    )
    lines: list[str] = []
    files_read = 0
    declarations = 0
    # The header line plus its newline: ``used`` then equals ``len(block)``
    # once every rendered line is accounted for, so the cap is a real bound.
    used = len(header) + 1
    for rel in list(files)[:DECLARATION_FILE_LIMIT]:
        text = read_source(base / rel)
        if not text:
            continue
        entries = kotlin_declarations(text)
        if not entries:
            continue
        files_read += 1
        for entry in entries:
            line = (
                f"- {entry['qualified']} ({entry['kind']}): "
                f"{entry['signature']}"
            )
            if used + len(line) + 1 > cap_chars:
                continue
            lines.append(line)
            used += len(line) + 1
            declarations += 1
    if not lines:
        return "", {"files": 0, "declarations": 0, "characters": 0}
    block = header + "\n" + "\n".join(lines) + "\n"
    return block, {
        "files": files_read,
        "declarations": declarations,
        "characters": len(block),
    }


def render_source_block(root: str | Path, limit: int = DEFAULT_FILE_LIMIT) -> str:
    """A short, explicitly delimited list of real paths for the planner prompt."""
    files = list_source_files(root, limit=limit)
    if not files:
        return ""
    lines = "\n".join(f"- {path}" for path in files)
    return (
        "Project source files (relative paths, read from disk; use these exact "
        "paths and do not invent any):\n"
        f"{lines}\n"
    )
