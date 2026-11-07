"""Tests for project-level development mode."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from giscode.projects.model import Project, VALID_MODES, DEFAULT_MODE
from giscode.projects.store import ProjectStore


def test_project_mode_defaults_to_traditional():
    """Project.mode defaults to 'traditional' when not set in plan."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        p = Project.create(name="Test", root_path=str(root))
        assert p.mode == DEFAULT_MODE
        assert p.mode == "traditional"
        assert p.plan.get("mode") == "traditional"


def test_project_mode_can_be_set_to_visual_app_design():
    """Project can be created with visual_app_design mode."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        p = Project.create(name="Test", root_path=str(root), mode="visual_app_design")
        assert p.mode == "visual_app_design"
        assert p.plan.get("mode") == "visual_app_design"
        assert p.plan.get("visual_design") == {}


def test_visual_design_persists_in_plan_json():
    """visual_design key is initialized in plan for visual_app_design mode."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        p = Project.create(name="Test", root_path=str(root), mode="visual_app_design")
        assert "visual_design" in p.plan
        assert p.plan["visual_design"] == {}

        # Also test traditional mode gets visual_design key
        p2 = Project.create(name="Test2", root_path=str(Path(tmp) / "proj2"))
        assert "visual_design" in p2.plan
        assert p2.plan["visual_design"] == {}


def test_mode_roundtrip_through_store():
    """Mode persists through store save/load cycle."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_projects.db"
        store = ProjectStore(db_path=str(db_path))

        root = Path(tmp) / "proj"
        root.mkdir()
        p = Project.create(name="Test", root_path=str(root), mode="visual_app_design")
        store.create_project(p)

        loaded = store.get_project(p.id)
        assert loaded is not None
        assert loaded.mode == "visual_app_design"
        assert loaded.plan.get("mode") == "visual_app_design"
        store.close()


def test_existing_project_without_mode_defaults_to_traditional():
    """Existing projects with plan={} default to traditional mode."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_projects.db"
        store = ProjectStore(db_path=str(db_path))

        # Simulate an old project record without mode in plan
        from giscode.projects.model import Project, ProjectLifecycle, _utc_now_iso
        import uuid

        pid = f"proj-{uuid.uuid4().hex}"
        now = _utc_now_iso()
        old_plan = {}  # No mode key
        proj = Project(
            id=pid,
            name="Old Project",
            root_path="/tmp/old",
            lifecycle=ProjectLifecycle.PLANNING,
            created_at=now,
            updated_at=now,
            description="",
            plan=old_plan,
        )
        store.create_project(proj)

        loaded = store.get_project(pid)
        assert loaded is not None
        assert loaded.mode == "traditional"
        store.close()


def test_invalid_mode_raises_error():
    """Invalid mode values are rejected at creation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        with pytest.raises(ValueError, match="Invalid mode"):
            Project.create(name="Test", root_path=str(root), mode="invalid_mode")

    # Validation happens at creation time via the factory
    # Direct mutation of plan dict bypasses validation (by design)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        p = Project.create(name="Test", root_path=str(root), mode="traditional")
        # Direct mutation bypasses validation - this is expected behavior
        # Validation only occurs at creation via the factory method
        p.plan["mode"] = "invalid_mode"
        assert p.mode == "invalid_mode"  # Property reads whatever is in plan


def test_mode_property_read_only():
    """Mode property is read-only (no setter)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        p = Project.create(name="Test", root_path=str(root))
        # Mode property has no setter - this is a design choice
        # The mode is part of the plan dict and should be modified via plan updates
        assert p.mode == "traditional"
        # Can modify plan directly
        p.plan["mode"] = "visual_app_design"
        assert p.mode == "visual_app_design"


# -----------------------------------------------------------------
# Controller Integration Tests
# -----------------------------------------------------------------


class TestControllerCreateProjectWithMode:
    """Tests for the controller's create_project method with mode parameter."""

    def test_controller_create_project_passes_mode(self, tmp_path):
        """Controller passes mode to ProjectWorkspaceSession.create."""
        from giscode.gui.project_workspace import ProjectWorkspaceController
        from giscode.projects.store import ProjectStore
        from giscode.storage import ChatStore

        store = ProjectStore()
        chat_store = ChatStore()
        controller = ProjectWorkspaceController(store, chat_store)

        # Test traditional mode
        session = controller.create_project(
            name="TestTraditional",
            root_path=str(tmp_path / "trad"),
            mode="traditional",
        )
        assert session.project.mode == "traditional"
        assert session.project.plan.get("mode") == "traditional"

        # Test visual_app_design mode
        controller.close()
        session = controller.create_project(
            name="TestVisual",
            root_path=str(tmp_path / "visual"),
            mode="visual_app_design",
        )
        assert session.project.mode == "visual_app_design"
        assert session.project.plan.get("mode") == "visual_app_design"
        assert session.project.plan.get("visual_design") == {}

    def test_controller_create_project_defaults_to_traditional(self, tmp_path):
        """Controller defaults to traditional mode when not specified."""
        from giscode.gui.project_workspace import ProjectWorkspaceController
        from giscode.projects.store import ProjectStore
        from giscode.storage import ChatStore

        store = ProjectStore()
        chat_store = ChatStore()
        controller = ProjectWorkspaceController(store, chat_store)

        session = controller.create_project(
            name="TestDefault",
            root_path=str(tmp_path / "default"),
        )
        assert session.project.mode == "traditional"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])