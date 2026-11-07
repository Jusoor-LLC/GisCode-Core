"""Kotlin function editing for the ``replace_symbol`` tool.

``symbol_editor`` finds symbols with Python's ``ast``. That is the right tool
for a ``.py`` file and the wrong one for a ``.kt`` file: it raised "file does
not parse" for every Kotlin source, so a repository-grounded Kotlin plan could
be produced but never applied.

This module handles two operations and nothing else: replacing the definition
of an existing Kotlin *function*, and adding a new top-level or member
function. Classes, interfaces, objects and properties are never edited:

* :func:`collect_kotlin_symbols` indexes ``fun`` declarations (top-level and
  members) with their real line ranges, using a scanner that blanks comments
  and string literals before it looks for braces, so braces inside strings,
  raw strings and comments cannot shift a range.
* :func:`replace_kotlin_symbol_block` swaps one function's lines for the new
  definition, preserving the surrounding indentation, line endings and the
  rest of the file.

Anything it cannot establish with certainty is refused with a
:class:`~giscode.agentic.symbol_editor.SymbolError` instead of guessed:
missing or ambiguous names, expression-bodied functions, a new definition
that does not carry the same name, unbalanced braces, or text before the
declaration (annotations/KDoc above the target are preserved untouched, so a
new copy of them would silently duplicate).

The tool layer in :mod:`giscode.gisai.tools.adapters` keeps every existing
safety rule: read-only refusal, deny-list, approval callback, snapshot and
diff. This module only computes the new file content.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from giscode.agentic.symbol_editor import SymbolBlock, SymbolError

__all__ = [
    "ReplaceKotlinSymbolError",
    "add_kotlin_symbol_block",
    "collect_kotlin_symbols",
    "find_kotlin_symbol",
    "replace_kotlin_symbol_block",
]


class ReplaceKotlinSymbolError(SymbolError):
    """A Kotlin edit that cannot be applied safely.

    Subclasses ``SymbolError`` so the existing tool layer's ``except
    SymbolError`` handling (rejection metadata, unchanged file) applies
    without modification.
    """


# ``fun`` with optional modifiers, an optional type-parameter list and an
# optional receiver (``fun String.foo()``). Backticked names are captured too.
_MODIFIERS = (
    r"(?:(?:public|private|internal|protected|open|override|suspend|inline|"
    r"operator|infix|tailrec|external|actual|expect|const|abstract|final|"
    r"sealed)\s+)*"
)

_FUN = re.compile(
    r"\bfun\s+"
    r"(?:<[^<>{}()]*>\s*)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)*"
    r"(?:`([^`\n]+)`|([A-Za-z_][A-Za-z0-9_]*))\s*\("
)

_TYPE_DECL = re.compile(
    r"\b(class|interface|object)\s+(?:`([^`\n]+)`|([A-Za-z_][A-Za-z0-9_]*))"
)

# A declaration must start the definition text; annotations and KDoc above the
# target function belong to it and are left in place by this module.
_DECLARATION_START = re.compile(
    r"^[ \t]*" + _MODIFIERS + r"fun\s", re.MULTILINE
)

# One annotation line such as ``@Test`` or ``@Suppress("x")``. Multi-line
# annotation arguments are not matched, so those payloads are refused.
_ANNOTATION_LINE = re.compile(
    r"^[ \t]*@[A-Za-z_][A-Za-z0-9_.]*(?:\([^()\n]*\))?[ \t]*$"
)


def _mask(text: str) -> str:
    """Return ``text`` with comments and string/char literals blanked.

    The result has the same length and the same newlines, so offsets and line
    numbers computed on it are valid for the original. Kotlin's nested block
    comments and raw strings are handled; ``$`` interpolation inside a literal
    is masked with the literal, which only means a brace there can never close
    a function body - exactly the conservative behaviour we want.
    """
    out = list(text)
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if char == "/" and i + 1 < length and text[i + 1] == "*":
            depth = 0
            while i < length:
                if text.startswith("/*", i):
                    depth += 1
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if text.startswith("*/", i):
                    depth -= 1
                    out[i] = out[i + 1] = " "
                    i += 2
                    if depth == 0:
                        break
                    continue
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        if char == '"' or char == "'":
            triple = text.startswith('"""', i)
            quote = '"""' if triple else char
            i += len(quote)
            while i < length:
                if triple:
                    if text.startswith('"""', i):
                        for k in range(i, i + 3):
                            out[k] = " "
                        i += 3
                        break
                    if text[i] != "\n":
                        out[i] = " "
                    i += 1
                    continue
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < length and text[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _match_brace(masked: str, open_index: int) -> int:
    """Index of the ``}`` that closes the ``{`` at ``open_index``."""
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ReplaceKotlinSymbolError(
        "unbalanced braces: no closing '}' for the declaration body"
    )


def _match_paren(masked: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ReplaceKotlinSymbolError("unbalanced parentheses in the declaration")


def _body_start(masked: str, after: int) -> Tuple[str, int]:
    """Find ``{`` or ``=`` that starts the body after the parameter list.

    Return type annotations may contain parentheses (``(Int) -> Unit``) and
    generics (``Map<String, Int>``), so they are skipped by depth rather than
    by a fixed pattern.
    """
    depth = 0
    index = after
    length = len(masked)
    while index < length:
        char = masked[index]
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth = max(0, depth - 1)
        elif depth == 0 and char == "{":
            return "{", index
        elif depth == 0 and char == "=":
            return "=", index
        elif depth == 0 and char in ";\n":
            # A declaration with no body on its own line: abstract/interface
            # member or unusual formatting. Refuse rather than scan into the
            # next declaration looking for a brace.
            return "", index
        index += 1
    return "", index


# A line ending in one of these continues the expression on the next line.
_CONTINUATION = set("+-*/%=<>&|.?:,(")


def _expression_end(masked: str, eq_index: int) -> Tuple[int, bool]:
    """End offset of an expression body, and whether it is on one line.

    The second value is False when the expression wraps (bracket depth is
    still open at the end of the line) or continues after a trailing
    operator. Callers refuse those instead of guessing a line range.
    """
    depth = 0
    index = eq_index + 1
    last_significant = eq_index
    length = len(masked)
    while index < length:
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return last_significant, False
            depth -= 1
        elif char == "\n" and depth == 0:
            line = masked[eq_index + 1 : index].rstrip()
            if line and line[-1] not in _CONTINUATION:
                return last_significant, True
            return last_significant, False
        if not char.isspace():
            last_significant = index
        index += 1
    return last_significant, True


def _body_brace(masked: str, after_name: int) -> int:
    """Offset of the ``{`` that opens a class/object body, or ``-1``.

    The constructor parameter list may span lines and may carry a default
    lambda (``nextId: () -> String = { ... }``), so a plain ``find("{")``
    lands inside the constructor instead of on the body.
    """
    index = after_name
    length = len(masked)
    while index < length and masked[index].isspace():
        index += 1
    if index < length and masked[index] == "(":
        try:
            index = _match_paren(masked, index) + 1
        except ReplaceKotlinSymbolError:
            return -1
    depth = 0
    while index < length:
        char = masked[index]
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth = max(0, depth - 1)
        elif char == "{" and depth == 0:
            return index
        elif char in "};" and depth == 0:
            return -1
        index += 1
    return -1


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _type_ranges(masked: str) -> List[Tuple[str, int, int]]:
    """``(name, start_offset, end_offset)`` for every class/interface/object."""
    ranges: List[Tuple[str, int, int]] = []
    for match in _TYPE_DECL.finditer(masked):
        name = match.group(2) or match.group(3) or ""
        if not name:
            continue
        brace = _body_brace(masked, match.end())
        if brace < 0:
            continue
        try:
            close = _match_brace(masked, brace)
        except ReplaceKotlinSymbolError:
            continue
        ranges.append((name, match.start(), close))
    return ranges


def _qualified(name: str, offset: int, types: List[Tuple[str, int, int]]) -> str:
    outer = [entry for entry in types if entry[1] < offset < entry[2]]
    outer.sort(key=lambda entry: entry[1])
    return ".".join([entry[0] for entry in outer] + [name])


def collect_kotlin_symbols(text: str) -> List[SymbolBlock]:
    """Index every ``fun`` declaration in ``text``."""
    masked = _mask(text)
    types = _type_ranges(masked)
    lines = text.splitlines(keepends=True)
    blocks: List[SymbolBlock] = []
    for match in _FUN.finditer(masked):
        name = match.group(1) or match.group(2) or ""
        if not name:
            continue
        paren = masked.find("(", match.end() - 1)
        if paren < 0:
            continue
        try:
            params_end = _match_paren(masked, paren)
        except ReplaceKotlinSymbolError:
            continue
        kind, body = _body_start(masked, params_end + 1)
        if kind == "{":
            try:
                close = _match_brace(masked, body)
            except ReplaceKotlinSymbolError:
                continue
            end_index = close
            block_kind = "function"
        elif kind == "=":
            # ``fun f(): T = expression``. Only a body that ends on the
            # declaration's own line is indexed as editable; a wrapped or
            # continuation expression is reported as unsupported so the
            # caller never guesses where the declaration stops.
            end_index, single_line = _expression_end(masked, body)
            block_kind = (
                "expression_function" if single_line
                else "expression_function_multiline"
            )
        else:
            # Abstract declaration or interface signature: indexed so the
            # caller gets "unsupported" instead of "not found", but never
            # edited here.
            end_index = params_end
            block_kind = "declaration_only"
        start_line = _line_of(text, match.start())
        end_line = _line_of(text, end_index)
        indent = ""
        for line in lines[start_line - 1 : start_line]:
            indent = line[: len(line) - len(line.lstrip(" \t"))]
            break
        block_text = "".join(lines[start_line - 1 : end_line])
        blocks.append(
            SymbolBlock(
                name=name,
                qualified=_qualified(name, match.start(), types),
                kind=block_kind,
                start_line=start_line,
                end_line=end_line,
                text=block_text,
                indent=indent,
                ends_with_newline=lines[end_line - 1].endswith("\n")
                if end_line - 1 < len(lines)
                else False,
                decorators="",
            )
        )
    return blocks


def find_kotlin_symbol(text: str, symbol: str) -> SymbolBlock:
    """Locate a Kotlin function by ``name`` or ``Class.name``."""
    blocks = collect_kotlin_symbols(text)
    if not blocks:
        raise ReplaceKotlinSymbolError(
            "no Kotlin function declarations were found in the file"
        )
    exact = [block for block in blocks if block.qualified == symbol]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Same class/name more than once: overloads. Their bodies cannot be
        # told apart by name alone, so refuse instead of editing the first.
        raise ReplaceKotlinSymbolError(
            f"ambiguous symbol {symbol!r}: {len(exact)} declarations share that "
            "name (overloaded functions are not supported)"
        )
    qualified = {block.qualified for block in blocks}
    same_name = [block for block in blocks if block.name == symbol]
    if len(same_name) == 1:
        return same_name[0]
    if len(same_name) > 1:
        raise ReplaceKotlinSymbolError(
            f"ambiguous symbol {symbol!r}: use a qualified name. "
            f"Found: {[block.qualified for block in same_name]}."
        )
    available = ", ".join(sorted(qualified))
    raise ReplaceKotlinSymbolError(
        f"symbol {symbol!r} not found. Available: {available}"
    )


def _declared_name(new_text: str) -> str:
    """Name of the function the replacement defines, or raise."""
    masked = _mask(new_text)
    match = _FUN.search(masked)
    if match is None:
        raise ReplaceKotlinSymbolError(
            "new text does not define a Kotlin function (expected 'fun name(...)')"
        )
    prefix = masked[: match.start()]
    if prefix.strip():
        raise ReplaceKotlinSymbolError(
            "new text must contain only the function definition; annotations and "
            "KDoc above the target are preserved, so do not repeat them"
        )
    return match.group(1) or match.group(2) or ""


def _balanced(text: str) -> bool:
    masked = _mask(text)
    pairs = {"{": "}", "(": ")"}
    stack: List[str] = []
    for char in masked:
        if char in pairs:
            stack.append(pairs[char])
        elif char in "})":
            if not stack or stack.pop() != char:
                return False
    return not stack


def _strip_blank_lines(text: str) -> str:
    lines = text.splitlines()
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _reindent(text: str, indent: str) -> str:
    lines = text.splitlines()
    widths = [
        len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()
    ]
    base = min(widths) if widths else 0
    rebuilt = [
        "" if not line.strip() else indent + line[base:] for line in lines
    ]
    return "\n".join(rebuilt)


def replace_kotlin_symbol_block(file_text: str, symbol: str, new_text: str) -> str:
    """Replace one Kotlin function with ``new_text`` and change nothing else."""
    block = find_kotlin_symbol(file_text, symbol)
    if block.kind not in ("function", "expression_function"):
        raise ReplaceKotlinSymbolError(
            f"unsupported Kotlin symbol {block.qualified!r} ({block.kind}): only a "
            "function with a brace body or a single-line expression body can be "
            "replaced"
        )
    body = _strip_blank_lines(new_text)
    if not body:
        raise ReplaceKotlinSymbolError("new text is empty")
    if not _DECLARATION_START.match(body):
        raise ReplaceKotlinSymbolError(
            "new text must start with a Kotlin function declaration"
        )
    if not _balanced(body):
        raise ReplaceKotlinSymbolError("new text has unbalanced braces or parentheses")
    defined = _declared_name(body)
    if defined != block.name:
        raise ReplaceKotlinSymbolError(
            f"new text defines {defined!r}; expected to redefine symbol "
            f"{block.name!r}"
        )
    rewritten = _reindent(body, block.indent)
    if block.ends_with_newline and not rewritten.endswith("\n"):
        rewritten += "\n"
    lines = file_text.splitlines(keepends=True)
    head = lines[: block.start_line - 1]
    tail = lines[block.end_line :]
    # The last replaced line normally ends with "\n"; the replacement already
    # carries one, so drop the duplicate only when the tail starts a new line.
    return "".join(head + [rewritten] + tail)


def _anchor_insert_line(file_text: str, anchor: str) -> Tuple[int, str, bool]:
    """Line index (0-based) to insert at, the indent, and whether it is inside a type."""
    try:
        block = find_kotlin_symbol(file_text, anchor)
    except ReplaceKotlinSymbolError as exc:
        if "not found" not in str(exc):
            raise
        block = None
    if block is not None:
        # A member anchor keeps the new function inside its type, at the same
        # indentation; a top-level anchor gets a column-0 declaration.
        indent = block.indent if "." in block.qualified else ""
        return block.end_line, indent, "." in block.qualified

    name = anchor.split(".")[-1]
    masked = _mask(file_text)
    types = [entry for entry in _type_ranges(masked) if entry[0] == name]
    if len(types) != 1:
        raise ReplaceKotlinSymbolError(
            f"anchor {anchor!r} not found: expected one Kotlin function or one "
            "class/interface/object with that name"
        )
    # A type anchor means "add this as a member of that type": the function
    # goes last in the body, before the closing brace, indented like the
    # members already there. That is what adding a test case to an existing
    # test class needs.
    _name, start, close = types[0]
    close_line = _line_of(file_text, close)
    start_line = _line_of(file_text, start)
    indent = ""
    for block in collect_kotlin_symbols(file_text):
        if block.start_line > start_line and block.end_line < close_line:
            indent = block.indent
            break
    if not indent:
        line = file_text.splitlines(keepends=True)[start_line - 1]
        indent = line[: len(line) - len(line.lstrip(" \t"))] + "    "
    return close_line - 1, indent, True


def _split_leading_annotations(body: str) -> Tuple[str, str]:
    """Split a leading run of annotation lines from the declaration itself."""
    lines = body.splitlines()
    index = 0
    while index < len(lines) and _ANNOTATION_LINE.match(lines[index]):
        index += 1
    if index == 0:
        return "", body
    return "\n".join(lines[:index]), "\n".join(lines[index:])


def _insert_declaration(
    lines: List[str], insert_at: int, body: str, *, pad_after: bool = True
) -> str:
    """Insert ``body`` after ``insert_at`` lines, one blank line before it.

    ``pad_after`` adds the blank line that separates a top-level declaration
    from what follows; inside a type body the closing brace comes straight
    after the new member instead.
    """
    head = list(lines[:insert_at])
    tail = list(lines[insert_at:])
    if head and not head[-1].endswith("\n"):
        head[-1] = head[-1] + "\n"
    while head and not head[-1].strip():
        head.pop()
    if pad_after:
        while tail and not tail[0].strip():
            tail.pop(0)
    parts = list(head)
    if parts:
        parts.append("\n")
    parts.append(body + "\n")
    if pad_after and tail:
        parts.append("\n")
    parts.extend(tail)
    return "".join(parts)


def add_kotlin_symbol_block(
    file_text: str,
    new_text: str,
    anchor: str | None = None,
) -> str:
    """Insert one new Kotlin function and change nothing else.

    The payload must be exactly one ``fun`` declaration with a brace body, a
    single-line expression body or no body at all (interface/abstract member).
    With an ``anchor`` the function goes directly after that function when it
    is top-level, directly after it at the same indentation when it is a
    member, and last inside the body when the anchor names a
    class/interface/object. Without an anchor it is appended at the end of the
    file.

    Leading annotation lines (``@Test``) travel with the function; any other
    text before the declaration is refused. Every uncertain case is refused:
    an unbalanced file or payload, a class/interface/object payload, a name
    that already exists, or an anchor that is missing or ambiguous.
    """
    if not _balanced(file_text):
        raise ReplaceKotlinSymbolError(
            "file has unbalanced braces or parentheses; refusing to insert"
        )
    body = _strip_blank_lines(new_text)
    if not body:
        raise ReplaceKotlinSymbolError("new text is empty")
    # A new test function carries its annotations (``@Test``) with it; they are
    # part of the declaration being inserted, unlike a comment or stray prose.
    _annotations, declaration = _split_leading_annotations(body)
    if not _DECLARATION_START.match(declaration):
        raise ReplaceKotlinSymbolError(
            "new text must start with a Kotlin function declaration"
        )
    if not _balanced(body):
        raise ReplaceKotlinSymbolError(
            "new text has unbalanced braces or parentheses"
        )
    masked_body = _mask(declaration)
    if _TYPE_DECL.search(masked_body):
        raise ReplaceKotlinSymbolError(
            "adding a Kotlin class, interface or object is not supported; "
            "only function declarations can be added"
        )
    defined = _declared_name(declaration)
    if any(block.name == defined for block in collect_kotlin_symbols(file_text)):
        raise ReplaceKotlinSymbolError(
            f"symbol {defined!r} already exists; use replace_symbol to change it"
        )
    lines = file_text.splitlines(keepends=True)
    inside_type = False
    if anchor is None:
        insert_at, indent = len(lines), ""
    else:
        insert_at, indent, inside_type = _anchor_insert_line(file_text, anchor)
    return _insert_declaration(
        lines, insert_at, _reindent(body, indent), pad_after=not inside_type
    )
