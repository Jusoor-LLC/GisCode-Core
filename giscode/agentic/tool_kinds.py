"""The one authoritative set of tools that mutate project files.

Kept import-free on purpose: the same classification is needed by the
verification path and by the GUI change report, and the report must not import
GisAI (GisAI is optional). A second copy drifts: a locally duplicated list once
dropped ``set_docstring`` and the report said "No files were modified" for a
file that had changed.
"""

WRITE_ACTION_TOOLS = frozenset(
    {"write_file", "edit_file", "replace_symbol", "add_symbol", "set_docstring"}
)
