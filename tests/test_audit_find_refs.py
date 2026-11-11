"""Tests for the audit symbol-reference finder."""
from giscode.audit.tools.find_refs import find_references


def test_find_references_reports_definition_usage_and_string(tmp_path):
    (tmp_path / "app.py").write_text(
        "def _stub_handler():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def call():\n"
        "    return _stub_handler()\n"
        "\n"
        "\n"
        "LABEL = '_stub_handler'\n",
        encoding="utf-8",
    )
    result = find_references("_stub_handler", tmp_path)
    assert len(result["definitions"]) == 1
    assert len(result["usages"]) == 1
    assert len(result["string_occurrences"]) == 1


def test_find_references_ignores_unrelated_symbols(tmp_path):
    (tmp_path / "app.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    result = find_references("_stub_handler", tmp_path)
    assert result == {"definitions": [], "usages": [], "string_occurrences": []}
