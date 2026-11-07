"""What this public repository is: GisCode Core, without the Pro surfaces.

This test ships only in the generated public export (it lives in
``release/templates/tests/``). The private repository keeps the full
``tests/test_edition_separation.py``; here the Pro implementations are not
present at all, so the checks are about their absence rather than their
behaviour.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from giscode import edition

ROOT = Path(__file__).resolve().parents[1]

PRO_MODULES = (
    "giscode.developer_release",
    "giscode.gui.developer_release_workspace",
    "giscode.gui.gislogo_view",
    "giscode.gui.ios_xtool_view",
    "giscode.agent_registry",
    "giscode.gui.agent_registry_view",
    "giscode.gui.pro_tabs",
)


def test_the_public_repository_is_the_core_edition(monkeypatch):
    monkeypatch.delenv(edition.EDITION_ENV, raising=False)
    assert edition.edition() == edition.CORE
    assert edition.tabs() == edition.CORE_TABS


def test_core_keeps_the_model_manager_and_the_adspace():
    assert "Model Manager" in edition.tabs()
    assert "Chat" in edition.tabs()
    assert "Projects" in edition.tabs()
    assert "AdSpace" in edition.tabs()


@pytest.mark.parametrize("name", PRO_MODULES)
def test_the_pro_implementations_are_not_in_the_public_tree(name):
    assert importlib.util.find_spec(name) is None, f"{name} was published"


def test_the_core_launch_imports_none_of_the_pro_modules():
    code = (
        "import sys;"
        "import giscode.gui.main_window;"
        "print(sum(1 for m in ("
        "'giscode.developer_release',"
        "'giscode.gui.developer_release_workspace',"
        "'giscode.gui.gislogo_view',"
        "'giscode.gui.ios_xtool_view',"
        "'giscode.agent_registry',"
        "'giscode.gui.agent_registry_view',"
        "'giscode.gui.pro_tabs') if m in sys.modules))"
    )
    env = dict(os.environ)
    env.pop(edition.EDITION_ENV, None)
    env["GISCODE_HOME"] = str(Path(os.environ.get("TEMP", ".")) / "giscode_core_export_import")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "0"


def test_the_public_repository_is_apache_licensed():
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in licence
    assert "Version 2.0, January 2004" in licence
    assert "END OF TERMS AND CONDITIONS" in licence
    assert "http://www.apache.org/licenses/LICENSE-2.0" in licence


def test_the_attribution_and_brand_notice_ship_with_the_licence():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Jusoor Company LLC" in notice
    assert "Apache License, Version 2.0" in notice
    assert "TRADEMARK.md" in notice

    trademark = (ROOT / "TRADEMARK.md").read_text(encoding="utf-8")
    assert "does **not** grant" in trademark or "does not" in trademark.lower()
    assert "Apache License" in trademark


def test_packaging_and_readme_agree_with_the_licence():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Apache-2.0" in pyproject
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Apache License, Version 2.0" in readme
    assert "all rights reserved" not in readme.lower()


def test_the_public_tree_is_lf():
    """Git's checkout policy and the artifact must agree, or clones differ."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attributes
    for name in ("README.md", "LICENSE", "NOTICE", "TRADEMARK.md"):
        assert b"\r\n" not in (ROOT / name).read_bytes(), name


def test_the_public_repository_can_validate_itself():
    """CI must stay runnable on a clean machine: no GPU, model or credentials."""
    workflow = ROOT / ".github" / "workflows" / "core-ci.yml"
    assert workflow.is_file(), "the public repository has no CI workflow"
    text = workflow.read_text(encoding="utf-8")
    assert "--ignore=tests/fixtures" in text
    # Prose may mention these; a job step must not depend on them.
    for forbidden in (
        "secrets.",
        "CUDA_VISIBLE_DEVICES",
        "OPENAI_API_KEY",
        "--api-key",
        "lmstudio",
        "ollama serve",
    ):
        assert forbidden.lower() not in text.lower(), (
            f"the CI workflow depends on {forbidden}, which the public repo has none of"
        )


def test_the_adspace_is_off_and_answers_without_a_provider():
    from giscode.ads.service import AdSpace

    state = AdSpace().state()
    assert state.enabled is False
    assert state.creative is None
    assert state.show_ad is False
    assert state.describe() == "Advertising is off."
