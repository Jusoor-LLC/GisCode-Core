"""Tests for fixture1 - stub handler detection."""
import sys

import pytest


def test_stub_handler_identifiable():
    """Test that stub handlers can be identified via find_references."""
    # A stub handler should be findable via find_references
    # It shows a messagebox but doesn't do real work
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c", 
         "from giscode.gisai.audit.tools.find_refs import find_references; "
         "r = find_references('_stub_handler'); print(f'defs={len(r[\"definitions\"])}, "
         "usages={len(r[\"usages\"])}, string={len(r[\"string_occurrences\"])}')"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert "defs=1" in output, f"Expected 1 definition, got: {output}"