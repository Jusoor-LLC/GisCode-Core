from __future__ import annotations

import hashlib
import time
from pathlib import Path

from giscode.agentic.guarded_writer import GuardedWriter
from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import Planner
from giscode.agentic.types import ActionResult, ActionType, ExecutionPlan, ExecutionStep, TaskRequest
from giscode.agentic.verifier import Verifier
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.projects.approval import BuildHandoff
from giscode.projects.build_launcher import BuildLauncher
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _long_test_body() -> str:
    return "\n".join(
        ["def test_target():"] + [f"    value_{i} = {i}" for i in range(100)] + ["    assert value_99 == 99", ""]
    )


def test_j2_like_existing_test_shrink_is_blocked_without_approval(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "tests" / "test_target.py"
    target.parent.mkdir()
    before = _long_test_body()
    target.write_text(before, encoding="utf-8")
    writer = GuardedWriter(root)

    result = writer.write(target, "def test_target():\n    pass\n", ActionType.MODIFY)

    assert not result.success
    assert "half" in (result.error or "")
    assert target.read_text(encoding="utf-8") == before


def test_j8_like_existing_non_test_shrink_is_blocked_without_approval(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "tools" / "rerun_harness.py"
    target.parent.mkdir()
    before = "\n".join(f"line_{i} = {i}" for i in range(1006)) + "\n"
    target.write_text(before, encoding="utf-8")
    writer = GuardedWriter(root)

    result = writer.write(target, "\n".join(f"line_{i} = {i}" for i in range(55)) + "\n", ActionType.MODIFY)

    assert not result.success
    assert "half" in (result.error or "")
    assert target.read_text(encoding="utf-8") == before


def test_protected_test_gate_accepts_action_result_records(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "tests" / "test_target.py"
    target.parent.mkdir()
    before = _long_test_body()
    target.write_text("def test_target():\n    pass\n", encoding="utf-8")
    action = ActionResult(
        action_type=ActionType.MODIFY,
        target_path=str(target),
        success=True,
    )
    action.original_content = before
    plan = ExecutionPlan(
        objective="repair",
        files_expected_to_change=["tests/test_target.py"],
        steps=[ExecutionStep("edit", "edit", ActionType.MODIFY, str(target), "")],
    )

    result = Verifier(root).verify(
        plan,
        [action],
        request_text="Fix tests/test_target.py",
    )

    assert not result.success
    assert "changed the test it was meant to satisfy" in result.errors[0]


def test_noop_existing_write_does_not_call_approval(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    calls = []
    writer = GuardedWriter(root, approve=lambda path, diff: calls.append((path, diff)) or True)

    result = writer.write(target, "value = 1\n", ActionType.MODIFY)

    assert result.success
    assert "No change" in result.message
    assert calls == []


def test_rejected_agentic_task_preserves_tree_hash(tmp_path):
    workspace = Workspace(tmp_path / "project")
    target = workspace.root_path / "tests" / "test_target.py"
    target.write_text(_long_test_body(), encoding="utf-8")
    before = _tree_hash(workspace.root_path)
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class PlannerForTest(Planner):
        def create_plan(self, request):
            return ExecutionPlan(
                objective="edit",
                files_expected_to_change=["tests/test_target.py"],
                steps=[ExecutionStep("edit", "edit", ActionType.MODIFY, str(target), "def test_target():\n    pass\n")],
            )

    result = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=PlannerForTest(inference=inference),
    ).start(TaskRequest("reject", "edit tests/test_target.py"))

    assert result.status.value == "failed"
    assert _tree_hash(workspace.root_path) == before
    assert "approval" in (result.error or "").lower() or "half" in (result.error or "").lower()


def test_build_launcher_uses_the_same_safe_coding_loop_for_new_files(tmp_path):
    workspace = Workspace(tmp_path / "project")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class BuildPlanner(Planner):
        def create_plan(self, request):
            return ExecutionPlan(
                objective="build",
                files_expected_to_change=["src/generated.py"],
                steps=[ExecutionStep("create", "create", ActionType.CREATE, "src/generated.py", "VALUE = 1\n")],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=BuildPlanner(inference=inference))
    launcher = BuildLauncher(loop)
    handoff = BuildHandoff(
        project_id="project-1",
        project_name="Project",
        root_path=str(workspace.root_path),
        plan={"roadmap": []},
        approved_at="now",
        started_at="now",
    )

    launcher.launch(handoff, max_duration=5)
    deadline = time.time() + 5
    result = None
    while time.time() < deadline:
        result = launcher.get_result("project-1")
        if result is not None:
            break
        time.sleep(0.02)

    assert result is not None
    assert result.status.value == "complete"
    assert (workspace.root_path / "src" / "generated.py").read_text(encoding="utf-8") == "VALUE = 1\n"
