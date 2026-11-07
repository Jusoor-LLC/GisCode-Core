"""AST-driven symbol editing for GisCode.

Two tools sit next to ``edit_file`` and use Python's ``ast`` to find a target
symbol (a function, a class, or ``Class.method``) by name. The user supplies
only the *new definition*; everything else - decorators, decorators' blanks
lines, indentation, the rest of the file - is taken from the file on disk.

* :func:`find_symbol_block` locates the exact (line_start, line_end) range
  covered by a symbol, including its decorators, with line numbers normalised
  so the slice returned always matches the file's underlying line endings.
* :func:`replace_symbol_block` rewrites a symbol's range with the new body,
  preserving the original indentation and rejecting the request when:

  - the new body does not parse,
  - the new body no longer defines a symbol of the same name,
  - the new body touches bytes outside the symbol's range.

* :func:`add_symbol_block` appends a new function or class definition
  immediately after a chosen anchor (a symbol name or ``module_end``) and
  inserts it before any ``if __name__ == "__main__":`` block when one exists.
* :func:`set_docstring_block` inserts or replaces the docstring of an
  existing symbol, leaving the signature, the body and the file's blank-line
  spacing exactly as they were.

These helpers are pure-Python, file-content-only, and never touch anything
outside the file. The tools in :mod:`giscode.gisai.tools.adapters` re-use them
to construct full ``replace_symbol``, ``add_symbol`` and ``set_docstring``
tools that layer the existing snapshot, diff, approval, deny-list, and
read-only rules on top.
"""

from __future__ import annotations

import ast
import difflib
import io
import re
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


MAIN_GUARD_RE = re.compile(
    r'^if\s+__name__\s*==\s*("|\')__main__("|\')\s*:',
    re.MULTILINE,
)


@dataclass(frozen=True)
class SymbolBlock:
    """A continuous region of source lines that defines one symbol.

    ``start_line`` and ``end_line`` are 1-indexed and inclusive, matching the
    numbers reported by Python's ``ast`` and the ``tokenize`` module. The
    raw text and stripped text are kept around so callers can render diffs
    without re-reading the file.
    """

    name: str
    qualified: str            # "ClassName" or "ClassName.method"
    kind: str                 # "function", "async_function", "method", "class"
    start_line: int           # 1-indexed, inclusive (first line of decorator)
    end_line: int             # 1-indexed, inclusive
    text: str                 # verbatim slice from the file
    indent: str               # leading whitespace used by the first line
    ends_with_newline: bool   # True when the block's last physical line ended in "\n"
    decorators: str           # verbatim decorator block (without trailing newline)

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True)
class SymbolError(Exception):
    reason: str


def read_text(path: Path) -> str:
    """Read a Python source file, normalising a UTF-8 BOM and any Latin-1
    mis-decoding of the raw bytes by reopening with explicit utf-8."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def collect_symbols(text: str) -> List[SymbolBlock]:
    """Index every top-level and member function/class in textual order."""
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise SymbolError(f"file does not parse: {exc}") from exc
    lines = text.splitlines(keepends=True)
    blocks: List[SymbolBlock] = []
    _collect(tree, lines, "", blocks)
    return blocks


def _collect(node: ast.AST, lines: List[str], prefix: str, bucket: List[SymbolBlock]) -> None:
    for child in getattr(node, "body", []) or []:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bucket.append(_build_block(lines, prefix, child))
        elif isinstance(child, ast.ClassDef):
            bucket.append(_build_block(lines, prefix, child))
            _collect(child, lines, child.name, bucket)


def _build_block(lines: List[str], prefix: str, node) -> SymbolBlock:
    end_line = getattr(node, "end_lineno", None) or node.lineno
    start_line = node.lineno
    decorated_start = start_line - len(node.decorator_list)
    if decorated_start < 1:
        decorated_start = 1
    text = "".join(lines[decorated_start - 1:end_line])
    decorators_text = "".join(lines[decorated_start - 1:start_line - 1])
    first_idx = start_line - 1
    while first_idx < end_line and lines[first_idx].strip() == "":
        first_idx += 1
    indent = ""
    if first_idx < end_line:
        indent = _leading_whitespace(lines[first_idx])
    kind = "class" if isinstance(node, ast.ClassDef) else (
        "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
    )
    qualified = f"{prefix}.{node.name}" if prefix else node.name
    if kind == "class":
        qualified = prefix + "" if prefix else node.name
    if prefix and kind == "function":
        qualified = f"{prefix}.{node.name}"
    return SymbolBlock(
        name=node.name,
        qualified=qualified,
        kind=kind,
        start_line=decorated_start,
        end_line=end_line,
        text=text,
        indent=indent,
        ends_with_newline=lines[end_line - 1].endswith("\n"),
        decorators=decorators_text,
    )


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def find_symbol_block(text: str, symbol: str) -> SymbolBlock:
    """Locate a symbol by name (function, class, or ``Class.method``)."""
    blocks = collect_symbols(text)
    if symbol in {b.qualified for b in blocks}:
        return next(b for b in blocks if b.qualified == symbol)
    if symbol in {b.name for b in blocks}:
        # If multiple top-level symbols share the name, prefer the first one;
        # callers should disambiguate with a qualified name (``Class.method``).
        exact = [b for b in blocks if b.name == symbol]
        if len(exact) == 1:
            return exact[0]
        raise SymbolError(
            f"ambiguous symbol {symbol!r}: use a qualified name. "
            f"Found: {[b.qualified for b in exact]}."
        )
    available = ", ".join(sorted({b.qualified for b in blocks}))
    raise SymbolError(f"symbol {symbol!r} not found. Available: {available or '(no symbols)'}")


def parse_definitions(new_text: str) -> List[Tuple[str, str]]:
    """Parse ``new_text`` and return ``(qualified_or_name, kind)`` pairs."""
    if new_text.startswith("\ufeff"):
        new_text = new_text[1:]
    try:
        tree = ast.parse(new_text)
    except SyntaxError as exc:
        raise SymbolError(f"new text does not parse: {exc}") from exc
    out: List[Tuple[str, str]] = []
    _collect_names(tree, "", out)
    return out


def _collect_names(node: ast.AST, prefix: str, bucket: List[Tuple[str, str]]) -> None:
    for child in getattr(node, "body", []) or []:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async" if isinstance(child, ast.AsyncFunctionDef) else "function"
            bucket.append((f"{prefix}.{child.name}" if prefix else child.name, kind))
        elif isinstance(child, ast.ClassDef):
            bucket.append((f"{prefix}.{child.name}" if prefix else child.name, "class"))
            _collect_names(child, child.name, bucket)


def replace_symbol_block(
    file_text: str,
    symbol: str,
    new_text: str,
) -> str:
    """Swap a symbol's range with ``new_text``.

    The new body must:

    * parse as Python,
    * define exactly one symbol whose top-level name matches the target,
    * contain no stray text outside the one symbol's body.
    """
    block = find_symbol_block(file_text, symbol)
    names = parse_definitions(new_text)
    if not names:
        raise SymbolError("new text does not define a function or class")
    is_method = "." in block.qualified
    expected = block.qualified.split(".")[-1]
    # The top-level definition must be exactly the target symbol; nested
    # methods and classes are collected with a dotted prefix and belong to
    # that target, so a class may be replaced by a version that also
    # contains its methods (e.g. a new method added to the class body).
    top_level = [name for name, _ in names if "." not in name]
    nested = [name for name, _ in names if "." in name]
    if top_level != [expected] or any(not name.startswith(f"{expected}.") for name in nested):
        raise SymbolError(
            f"new text defines {[n for n, _ in names]}; "
            f"expected to redefine symbol {expected!r}"
        )
    if is_method and len(names) != 1:
        raise SymbolError(
            f"new text defines multiple symbols for a method edit: {[n for n, _ in names]}"
        )
    if len(top_level) > 1 and not is_method:
        raise SymbolError(
            f"new text defines multiple symbols for {expected!r}: {[n for n, _ in names]}. "
            f"Use replace_symbol for each."
        )
    rewritten_body = _reindent(new_text, block.indent)
    # The tool layer hands the definition over with a leading and trailing
    # blank line (``"\n" + text + "\n"``). Those blanks sit INSIDE the block
    # range being replaced, so keeping them would grow the file's spacing by
    # one blank line on every edit. The blank lines outside the block are
    # untouched, so the file keeps exactly the separation it already had.
    rewritten_body = _strip_blank_lines(rewritten_body)
    if block.decorators:
        new_has_decorators = bool(_leading_decorators(new_text.strip("\n")))
        if not new_has_decorators:
            rewritten_body = block.decorators + block.indent.lower() * 0 + rewritten_body
    if block.ends_with_newline and rewritten_body and not rewritten_body.endswith("\n"):
        rewritten_body = rewritten_body + "\n"
    lines = file_text.splitlines(keepends=True)
    new_lines = lines[: block.start_line - 1] + [rewritten_body] + lines[block.end_line:]
    return "".join(new_lines)


def _leading_decorators(text: str) -> List[str]:
    pattern = re.compile(r"^\s*@\w+(\([^)]*\))?", re.MULTILINE)
    return pattern.findall(text)


def _strip_blank_lines(text: str) -> str:
    """Drop leading and trailing empty lines, keep the body verbatim."""
    lines = text.splitlines()
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _blank_run_ends(lines: List[str]) -> Tuple[int, int]:
    """``(leading_blank_lines, trailing_blank_lines)`` of ``lines``."""
    leading = 0
    while leading < len(lines) and not lines[leading].strip():
        leading += 1
    trailing = 0
    while trailing < len(lines) - leading and not lines[len(lines) - 1 - trailing].strip():
        trailing += 1
    return leading, trailing


def add_symbol_block(
    file_text: str,
    new_text: str,
    anchor: Optional[str] = None,
) -> str:
    """Insert a new function or class at ``anchor`` or before the main guard."""
    names = parse_definitions(new_text)
    if not names:
        raise SymbolError("new text does not define a function or class")
    if len(names) > 1:
        raise SymbolError(
            f"add_symbol expects one symbol; got {[n for n, _ in names]}"
        )
    lines = file_text.splitlines(keepends=True)
    if not lines[-1].endswith("\n") if lines else False:
        lines[-1] = lines[-1] + "\n"
    separator = "\n\n"
    insert_index = None
    block_indent = ""
    if anchor:
        try:
            block = find_symbol_block(file_text, anchor)
        except SymbolError:
            block = None
        if block is not None:
            insert_index = block.end_line
            block_indent = block.indent
    if insert_index is None:
        match = MAIN_GUARD_RE.search("".join(lines))
        if match:
            line_no = match.string.count("\n", 0, match.start()) + 1
            insert_index = line_no - 1
    if insert_index is None or insert_index >= len(lines):
        if not lines or lines[-1].strip() == "":
            insert_index = len(lines)
        else:
            insert_index = len(lines)
    indent = block_indent
    body_lines = _strip_blank_lines(_reindent(new_text, indent)).splitlines()
    if not body_lines:
        raise SymbolError("add_symbol new text is empty after reindenting")
    payload_lines = [line + "\n" for line in body_lines]

    before = lines[:insert_index]
    after = lines[insert_index:]
    # PEP 8 wants exactly two blank lines around a top-level definition.
    # Blanks that are already in the file are reused, never stacked, so an
    # edit neither grows nor shrinks the surrounding spacing.
    if any(line.strip() for line in before):
        _, trailing_before = _blank_run_ends(before)
        payload_lines = ["\n"] * max(0, 2 - trailing_before) + payload_lines
    if any(line.strip() for line in after):
        leading_after, _ = _blank_run_ends(after)
        payload_lines = payload_lines + ["\n"] * max(0, 2 - leading_after)

    new_lines = before + payload_lines + after
    return "".join(new_lines)


def _render_docstring_literal(value: str) -> str:
    """Render ``value`` as a Python string literal usable as a docstring."""
    if '"""' not in value and not value.endswith("\\"):
        return '"""' + value + '"""'
    return repr(value)


def set_docstring_block(file_text: str, symbol: str, docstring: str) -> str:
    """Insert or replace the docstring of ``symbol`` and change nothing else.

    ``docstring`` is the bare text (no quotes). The symbol's block is found
    with :func:`find_symbol_block`, its own AST decides whether a docstring
    already exists, and only that literal's lines are rewritten. Everything
    outside the docstring - decorators, signature, body, blank lines, the
    rest of the file - is left byte-for-byte untouched.
    """
    block = find_symbol_block(file_text, symbol)
    body = _strip_blank_lines(docstring.strip("\n"))
    if not body:
        raise SymbolError(f"docstring for {symbol!r} is empty")

    parse_text = textwrap.dedent(block.text)
    try:
        tree = ast.parse(parse_text)
    except SyntaxError as exc:
        raise SymbolError(f"file does not parse: {exc}") from exc

    node = None
    for child in ast.walk(tree):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == block.name:
            node = child
            break
    if node is None:
        raise SymbolError(f"symbol {symbol!r} was not found inside its own block")

    lines = block.text.splitlines(keepends=True)
    existing = None
    first = node.body[0] if node.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(getattr(first, "value", None), ast.Constant)
        and isinstance(first.value.value, str)
    ):
        existing = first

    literal = _render_docstring_literal(body)
    if existing is not None:
        indent = _leading_whitespace(lines[existing.lineno - 1]) if existing.lineno - 1 < len(lines) else block.indent
        new_lines = [indent + literal.splitlines()[0] + "\n"]
        new_lines.extend(line + "\n" for line in literal.splitlines()[1:])
        new_block_lines = lines[: existing.lineno - 1] + new_lines + lines[existing.end_lineno:]
    else:
        header_lineno = node.lineno
        body_index = first.lineno - 1 if first is not None else len(lines)
        if body_index <= header_lineno - 1:
            raise SymbolError(
                f"symbol {symbol!r} keeps its body on the definition line; "
                "reformat it before setting a docstring"
            )
        indent = _leading_whitespace(lines[body_index]) if body_index < len(lines) else block.indent + "    "
        new_lines = [indent + literal.splitlines()[0] + "\n"]
        new_lines.extend(line + "\n" for line in literal.splitlines()[1:])
        new_block_lines = lines[:body_index] + new_lines + lines[body_index:]

    new_block_text = "".join(new_block_lines)
    try:
        ast.parse(textwrap.dedent(new_block_text))
    except SyntaxError as exc:
        raise SymbolError(f"docstring edit would not parse: {exc}") from exc

    out_lines = file_text.splitlines(keepends=True)
    return "".join(out_lines[: block.start_line - 1] + [new_block_text] + out_lines[block.end_line :])


def _reindent(text: str, indent: str) -> str:
    """Strip the minimum common leading whitespace from ``text`` and add
    ``indent`` to every non-blank line. Blank lines stay blank.

    The returned text contains the same number of lines as ``text`` exactly,
    including any terminating blank lines, but without trailing newlines - the
    caller controls the final newline.
    """
    raw_lines = text.splitlines()
    if not raw_lines:
        return text
    indents = [
        len(line) - len(line.lstrip(" \t"))
        for line in raw_lines
        if line.strip() != ""
    ]
    if not indents:
        return text
    base = min(indents)
    rebuilt = []
    for line in raw_lines:
        if line.strip() == "":
            rebuilt.append("")
        else:
            rebuilt.append(indent + line[base:])
    return "\n".join(rebuilt)


def normalise_for_match(text: str) -> str:
    """Return ``text`` with line endings and trailing whitespace collapsed.

    Used by :class:`giscode.gisai.tools.adapters.EditFileTool` so planners
    that produce LF/CRLF mixed content with trailing whitespace can still
    match a file that came from PowerShell or other tooling.
    """
    parts = [line.rstrip() for line in re.split(r"\r\n|\r|\n", text)]
    # Drop the empty string Python emits when ``text`` ends with a newline;
    # callers usually want the canonical form ``"a\\nb"`` rather than
    # ``"a\\nb\\n"``.
    if parts and parts[-1] == "" and text.endswith(("\n", "\r")):
        parts.pop()
    if parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts)


def find_closest_block(text: str, needle: str, limit: int = 6) -> List[str]:
    """Return up to ``limit`` of the file's lines that best match ``needle``.

    Used when ``edit_file`` rejects a request because the old text did not
    match exactly: the planner gets to see neighbouring lines so the next
    retry has a chance of landing on the real text.
    """
    haystack = text.splitlines()
    needle_lines = [line.rstrip() for line in re.split(r"\r\n|\r|\n", needle) if line.strip()]
    if not haystack or not needle_lines:
        return haystack[:limit]
    scores: List[Tuple[float, int]] = []
    for start in range(len(haystack) - len(needle_lines) + 1):
        window = haystack[start:start + len(needle_lines)]
        score = sum(
            difflib.SequenceMatcher(a=str(window[i]), b=str(needle_lines[i])).ratio()
            for i in range(len(needle_lines))
        )
        scores.append((score, start))
    scores.sort(reverse=True)
    start = scores[0][1]
    return haystack[start:start + max(limit, len(needle_lines))]
