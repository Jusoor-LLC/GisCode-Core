"""Approve & Start and Retry Build must match the real lifecycle.

The tab used to offer approval whenever a session was open, and never refreshed
the buttons when only the lifecycle changed. These tests pin eligibility to the
stored lifecycle, check that it is re-read without any build-status change, and
check that Retry Build goes through the controller's controlled operation.
"""
from __future__ import annotations

import gc

import pytest

from giscode.gui.project_workspace import (
    BuildStartResult,
    BuildStatus,
    ProjectWorkspaceController,
    ProjectWorkspaceView,
)
from giscode.planer import plan_to_project_dict
from giscode.planer.engine import PlanerEngine
from giscode.projects import ProjectLifecycle, ProjectStore
from giscode.projects.approval import clear_in_flight_registry
from giscode.projects.workspace import ProjectWorkspaceError
from giscode.storage import ChatStore
from tests.conftest import tk_available, tk_skip_reason

pytestmark = pytest.mark.skipif(not tk_available(), reason=tk_skip_reason())


@pytest.fixture(autouse=True)
def _reset_in_flight():
    clear_in_flight_registry()
    yield
    clear_in_flight_registry()


@pytest.fixture
def stores(tmp_path):
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    chat_store = ChatStore(str(tmp_path / "chats.db"))
    yield project_store, chat_store
    project_store.close()
    chat_store.close()


def _ready_plan(project_id: str) -> dict:
    engine = PlanerEngine(project_id=project_id)
    engine.start_with_idea("Build a small calculator with care")
    for _ in range(15):
        open_questions = [q for q in engine.plan.open_questions if not q.answered]
        if not open_questions:
            break
        for question in open_questions:
            engine.answer_question(question.id, "a-" + question.id)
    engine.set_application_type("web app")
    for name in "abc":
        engine.add_feature(name)
    for name in "xy":
        engine.add_component(name)
    for name in "s1s2s3":
        engine.add_screen(name)
    for name in "m1m2m3":
        engine.add_roadmap_item(name)
    engine.set_branding("br")
    assert engine.is_ready_for_review()
    return plan_to_project_dict(engine.build_plan())


def _opened(controller, store, tmp_path, name):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    session = controller.create_project(name=name, root_path=str(root))
    project_id = session.project_id
    store.set_plan(project_id, _ready_plan(project_id))
    controller.close()
    controller.open_project(project_id)
    return project_id


def _enabled(button) -> bool:
    return "disabled" not in button.state()


def _walk(store, project_id, *targets):
    for target in targets:
        store.transition_lifecycle(project_id, target)


def _destroy(tk_root, view) -> None:
    try:
        view._parent.destroy()
    except Exception:
        return
    try:
        tk_root.update_idletasks()
    except Exception:
        pass
    gc.collect()


def test_approve_and_retry_eligibility_follows_the_stored_lifecycle(tk_root, stores, tmp_path):
    project_store, chat_store = stores
    controller = ProjectWorkspaceController(project_store, chat_store)
    view = None
    try:
        project_id = _opened(controller, project_store, tmp_path, "Eligibility")
        view = ProjectWorkspaceView(tk_root, controller)
        tk_root.update()
        view.show_active_project(
            "Eligibility", project_id, project_store.get_project(project_id).root_path
        )
        tk_root.update()

        # PLANNING: approval is the offered action.
        assert _enabled(view._approve_button)
        assert not _enabled(view._retry_button)

        # BUILDING: nothing may be approved or retried, and the reason is shown.
        _walk(project_store, project_id, ProjectLifecycle.REVIEW, ProjectLifecycle.APPROVED,
              ProjectLifecycle.BUILDING)
        view._poll_build_state()
        tk_root.update()
        assert not _enabled(view._approve_button)
        assert not _enabled(view._retry_button)
        assert "BUILDING" in view._action_hint_var.get()

        # FAILED: retry becomes the offered action.
        _walk(project_store, project_id, ProjectLifecycle.FAILED)
        view._poll_build_state()
        tk_root.update()
        assert not _enabled(view._approve_button)
        assert _enabled(view._retry_button)
        assert "retry" in view._action_hint_var.get().lower()

        # COMPLETE and CANCELLED stay terminal for both actions.
        _walk(project_store, project_id, ProjectLifecycle.REVIEW, ProjectLifecycle.APPROVED,
              ProjectLifecycle.BUILDING, ProjectLifecycle.TESTING,
              ProjectLifecycle.VERIFICATION, ProjectLifecycle.COMPLETE)
        view._poll_build_state()
        tk_root.update()
        assert not _enabled(view._approve_button)
        assert not _enabled(view._retry_button)
        assert "COMPLETE" in view._action_hint_var.get()

        second_id = _opened(controller, project_store, tmp_path, "Cancelled")
        project_store.transition_lifecycle(second_id, ProjectLifecycle.CANCELLED)
        view.show_active_project(
            "Cancelled", second_id, project_store.get_project(second_id).root_path
        )
        view._poll_build_state()
        tk_root.update()
        assert not _enabled(view._approve_button)
        assert not _enabled(view._retry_button)
        assert "CANCELLED" in view._action_hint_var.get()
    finally:
        if view is not None:
            _destroy(tk_root, view)
            del view
        gc.collect()
        controller.close()


def test_retry_button_uses_the_controller_and_never_edits_the_store(
    tk_root, stores, tmp_path, monkeypatch
):
    project_store, chat_store = stores
    controller = ProjectWorkspaceController(project_store, chat_store)
    view = None
    try:
        project_id = _opened(controller, project_store, tmp_path, "RetryAction")
        _walk(project_store, project_id, ProjectLifecycle.REVIEW, ProjectLifecycle.APPROVED,
              ProjectLifecycle.FAILED)
        view = ProjectWorkspaceView(tk_root, controller)
        tk_root.update()
        view.show_active_project(
            "RetryAction", project_id, project_store.get_project(project_id).root_path
        )
        tk_root.update()
        assert _enabled(view._retry_button)

        calls = []

        def retry():
            calls.append(project_id)
            return BuildStartResult(
                status=BuildStatus.RUNNING,
                message="Retry accepted; the build is running.",
                approved=True,
                launched=True,
                project_id=project_id,
            )

        monkeypatch.setattr(controller, "retry_build", retry)
        view._on_retry_build()
        tk_root.update()

        assert calls == [project_id]
        assert "running" in view._status_var.get().lower()
        # The view did not touch the lifecycle itself.
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.FAILED
        )
    finally:
        if view is not None:
            _destroy(tk_root, view)
            del view
        gc.collect()
        controller.close()


def test_a_refused_retry_is_reported_and_changes_nothing(
    tk_root, stores, tmp_path, monkeypatch
):
    project_store, chat_store = stores
    controller = ProjectWorkspaceController(project_store, chat_store)
    view = None
    try:
        project_id = _opened(controller, project_store, tmp_path, "RetryRefused")
        _walk(project_store, project_id, ProjectLifecycle.REVIEW, ProjectLifecycle.APPROVED,
              ProjectLifecycle.FAILED)
        view = ProjectWorkspaceView(tk_root, controller)
        tk_root.update()
        view.show_active_project(
            "RetryRefused", project_id, project_store.get_project(project_id).root_path
        )

        def refuse():
            raise ProjectWorkspaceError(
                "the project is COMPLETE; only a FAILED project can be retried"
            )

        monkeypatch.setattr(controller, "retry_build", refuse)
        view._on_retry_build()
        tk_root.update()

        assert "Retry refused" in view._plan_reply_var.get()
        assert "COMPLETE" in view._plan_reply_var.get()
        assert view._status_var.get() == "Retry not started"
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.FAILED
        )
    finally:
        if view is not None:
            _destroy(tk_root, view)
            del view
        gc.collect()
        controller.close()
