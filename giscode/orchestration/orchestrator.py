import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from giscode.orchestration.checkpoint import CheckpointError, CheckpointManager
from giscode.orchestration.failure import FailureCapture
from giscode.orchestration.types import (
    BuildRequest,
    BuildResult,
    BuildStatus,
    FixAttempt,
    FailureRecord,
    OrchestrationPhase,
    OrchestrationResult,
    TestRequest,
    TestResult,
    TestStatus,
)
from giscode.sandbox.executor import SandboxExecutor
from giscode.sandbox.types import SandboxRequest, SandboxStatus
from giscode.self_improvement.service import SelfImprovementService
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


class OrchestrationError(Exception):
    pass


class BuildTestOrchestrator:
    def __init__(
        self,
        workspace: Workspace,
        sandbox: SandboxExecutor,
        task_manager: Optional[TaskManager] = None,
        max_attempts: int = 5,
        self_improvement_service: Optional[SelfImprovementService] = None,
    ):
        self._workspace = workspace
        self._sandbox = sandbox
        self._task_manager = task_manager
        self._max_attempts = max_attempts
        self._failure_capture = FailureCapture()
        self._checkpoint_manager = CheckpointManager(workspace)
        self._current_result: Optional[OrchestrationResult] = None
        self._cancelled = False
        self._self_improvement_service = self_improvement_service

    def build(self, request: BuildRequest) -> BuildResult:
        sandbox_request = SandboxRequest(
            command=request.command,
            arguments=request.arguments,
            working_directory=request.working_directory,
            timeout=request.timeout,
            environment=request.environment,
        )
        result = self._sandbox.execute(sandbox_request)
        return self._to_build_result(result)

    def test(self, request: TestRequest) -> TestResult:
        sandbox_request = SandboxRequest(
            command=request.command,
            arguments=request.arguments,
            working_directory=request.working_directory,
            timeout=request.timeout,
            environment=request.environment,
        )
        result = self._sandbox.execute(sandbox_request)
        return self._to_test_result(result)

    def build_and_test(self, task_id: str, build_request: BuildRequest, test_request: TestRequest) -> OrchestrationResult:
        self._cancelled = False
        result = OrchestrationResult(
            task_id=task_id,
            status=OrchestrationPhase.BUILDING,
            max_attempts=self._max_attempts,
        )
        self._current_result = result

        try:
            build_result = self.build(build_request)
            result.build_result = build_result

            if build_result.status == BuildStatus.SANDBOX_UNAVAILABLE:
                result.status = OrchestrationPhase.FAILED
                result.error = "Sandbox unavailable"
                result.failure_info = {"reason": "sandbox_unavailable"}
                return result

            if self._is_cancelled(result):
                return result

            if build_result.status != BuildStatus.SUCCESS:
                failure = self._failure_capture.capture(
                    failure_type="build_failure",
                    command=build_request.command,
                    exit_code=build_result.exit_code,
                    stderr=build_result.stderr,
                    stdout=build_result.stdout,
                    attempt_number=1,
                )
                return self._handle_failure(result, failure, build_request, test_request)

            result.status = OrchestrationPhase.TESTING
            test_result = self.test(test_request)
            result.test_result = test_result

            if test_result.status == TestStatus.SANDBOX_UNAVAILABLE:
                result.status = OrchestrationPhase.FAILED
                result.error = "Sandbox unavailable"
                result.failure_info = {"reason": "sandbox_unavailable"}
                return result

            if self._is_cancelled(result):
                return result

            if test_result.status == TestStatus.PASSED:
                result.status = OrchestrationPhase.COMPLETE
                return result

            failure = self._failure_capture.capture(
                failure_type="test_failure",
                command=test_request.command,
                exit_code=test_result.exit_code,
                stderr=test_result.stderr,
                stdout=test_result.stdout,
                attempt_number=1,
            )
            return self._handle_failure(result, failure, build_request, test_request)

        except Exception as e:
            result.status = OrchestrationPhase.FAILED
            result.error = str(e)
            result.failure_info = {"reason": "exception", "message": str(e)}
            return result

    def _handle_failure(self, result: OrchestrationResult, failure: FailureRecord, build_request: BuildRequest, test_request: TestRequest) -> OrchestrationResult:
        if failure.failure_type == "sandbox_unavailable":
            result.status = OrchestrationPhase.FAILED
            result.error = "Sandbox unavailable"
            result.failure_info = {"reason": "sandbox_unavailable"}
            return result

        failure_pair = None
        if self._self_improvement_service is not None:
            failure_pair = self._self_improvement_service.record_failure(
                failure_type=failure.failure_type,
                command=failure.command,
                exit_code=failure.exit_code,
                error_text=failure.stderr or failure.summary,
                namespace="default",
                project_id=None,
                task_id=result.task_id,
                metadata={"fingerprint": failure.fingerprint},
            )

        for attempt in range(1, self._max_attempts + 1):
            if self._is_cancelled(result):
                return result

            result.current_attempt = attempt
            result.status = OrchestrationPhase.FIXING

            fix = FixAttempt(
                attempt_number=attempt,
                failure=failure,
                fix_proposal=f"Fix attempt {attempt} for {failure.failure_type}",
            )
            result.attempts.append(fix)

            if self._self_improvement_service is not None and failure_pair is not None:
                self._self_improvement_service.record_candidate_fix(
                    failure_pair_id=failure_pair.pair_id,
                    fix_proposal=fix.fix_proposal,
                    files_changed=fix.files_changed,
                    namespace="default",
                    project_id=None,
                    task_id=result.task_id,
                )

            checkpoint = self._checkpoint_manager.create(f"attempt-{attempt}")
            result.checkpoint_label = str(checkpoint)

            try:
                fix.applied = True
                build_result = self.build(build_request)
                fix.build_result = build_result

                if build_result.status == BuildStatus.SANDBOX_UNAVAILABLE:
                    result.status = OrchestrationPhase.FAILED
                    result.error = "Sandbox unavailable"
                    result.failure_info = {"reason": "sandbox_unavailable"}
                    self._restore_checkpoint(result, checkpoint)
                    return result

                if self._is_cancelled(result):
                    self._restore_checkpoint(result, checkpoint)
                    return result

                if build_result.status != BuildStatus.SUCCESS:
                    failure = self._failure_capture.capture(
                        failure_type="build_failure",
                        command=build_request.command,
                        exit_code=build_result.exit_code,
                        stderr=build_result.stderr,
                        stdout=build_result.stdout,
                        attempt_number=attempt + 1,
                    )
                    if self._failure_capture.is_duplicate(failure):
                        continue
                    continue

                test_result = self.test(test_request)
                fix.test_result = test_result

                if test_result.status == TestStatus.SANDBOX_UNAVAILABLE:
                    result.status = OrchestrationPhase.FAILED
                    result.error = "Sandbox unavailable"
                    result.failure_info = {"reason": "sandbox_unavailable"}
                    self._restore_checkpoint(result, checkpoint)
                    return result

                if self._is_cancelled(result):
                    self._restore_checkpoint(result, checkpoint)
                    return result

                if test_result.status == TestStatus.PASSED:
                    fix.success = True
                    if self._self_improvement_service is not None and failure_pair is not None:
                        self._self_improvement_service.record_verified_fix(
                            failure_pair_id=failure_pair.pair_id,
                            verification_result="build_and_test_passed",
                            fix_succeeded=True,
                        )
                    result.status = OrchestrationPhase.COMPLETE
                    return result

            except Exception as e:
                fix.build_result = BuildResult(
                    command=build_request.command,
                    status=BuildStatus.FAILED,
                    error=str(e),
                )
                self._restore_checkpoint(result, checkpoint)

        if self._self_improvement_service is not None and failure_pair is not None:
            self._self_improvement_service.record_verified_fix(
                failure_pair_id=failure_pair.pair_id,
                verification_result="retry_limit_exhausted",
                fix_succeeded=False,
            )

        result.status = OrchestrationPhase.ESCALATED
        result.error = "Maximum retry attempts exhausted"
        result.failure_info = {
            "reason": "retry_limit_exhausted",
            "max_attempts": self._max_attempts,
            "attempts": len(result.attempts),
        }
        return result

    def cancel(self) -> Optional[OrchestrationResult]:
        self._cancelled = True
        if self._current_result is not None:
            self._current_result.status = OrchestrationPhase.CANCELLED
            return self._current_result
        result = OrchestrationResult(
            task_id="",
            status=OrchestrationPhase.CANCELLED,
        )
        self._current_result = result
        return result

    def _restore_checkpoint(
        self, result: OrchestrationResult, checkpoint: Path
    ) -> None:
        """Undo this attempt's edits, keeping anything the user changed.

        The produced fingerprint has to be taken immediately before the
        restore: a build step is an arbitrary command, and the checkpoint copy
        alone cannot tell its output from a save made while it ran. Without
        that evidence the manager refuses to touch the tree, so a failure here
        reports the edits as left in place instead of deleting files.
        """
        produced = None
        try:
            produced = self._checkpoint_manager.capture_produced()
        except Exception:  # noqa: BLE001 - no evidence means no undo, not a crash
            produced = None

        try:
            outcome = self._checkpoint_manager.restore(checkpoint, produced=produced)
        except CheckpointError as exc:
            info = result.failure_info if isinstance(result.failure_info, dict) else {}
            info["checkpoint_restore"] = {"status": "blocked", "blocked": str(exc)}
            result.failure_info = info
            note = f"could not roll the attempt back: {exc}"
            result.error = f"{result.error}; {note}" if result.error else note
            return

        info = result.failure_info if isinstance(result.failure_info, dict) else {}
        info["checkpoint_restore"] = outcome.as_dict()
        if outcome.complete:
            result.failure_info = info
            return
        # Never let an attempt that was not undone read as one that was.
        info["checkpoint_restore_incomplete"] = True
        result.failure_info = info
        note = outcome.summary()
        result.error = f"{result.error}; {note}" if result.error else note

    def get_status(self) -> OrchestrationPhase:
        if self._current_result is None:
            return OrchestrationPhase.IDLE
        return self._current_result.status

    def _is_cancelled(self, result: OrchestrationResult) -> bool:
        if self._cancelled:
            result.status = OrchestrationPhase.CANCELLED
            if self._task_manager is not None:
                self._task_manager.cancel()
            return True
        if self._task_manager is not None:
            active = self._task_manager.get_active_task()
            if active is None or active.status.value == "timeout":
                result.status = OrchestrationPhase.TIMED_OUT
                result.failure_info = {"reason": "timeout"}
                return True
        return False

    def _to_build_result(self, sandbox_result) -> BuildResult:
        status = BuildStatus.SUCCESS if sandbox_result.exit_code == 0 else BuildStatus.FAILED
        if sandbox_result.status.value == "unavailable":
            status = BuildStatus.SANDBOX_UNAVAILABLE
        return BuildResult(
            command=sandbox_result.command,
            arguments=sandbox_result.arguments,
            working_directory=sandbox_result.working_directory,
            exit_code=sandbox_result.exit_code,
            stdout=sandbox_result.stdout,
            stderr=sandbox_result.stderr,
            elapsed_time=sandbox_result.elapsed_time,
            status=status,
            timed_out=sandbox_result.timed_out,
            cancelled=sandbox_result.cancelled,
            error=sandbox_result.error,
            sandbox_status=sandbox_result.status.value,
        )

    def _to_test_result(self, sandbox_result) -> TestResult:
        passed = 0
        failed = 0
        if sandbox_result.exit_code == 0:
            passed = 1
        else:
            failed = 1

        status = TestStatus.PASSED if sandbox_result.exit_code == 0 else TestStatus.FAILED
        if sandbox_result.status.value == "unavailable":
            status = TestStatus.SANDBOX_UNAVAILABLE

        return TestResult(
            command=sandbox_result.command,
            arguments=sandbox_result.arguments,
            working_directory=sandbox_result.working_directory,
            exit_code=sandbox_result.exit_code,
            stdout=sandbox_result.stdout,
            stderr=sandbox_result.stderr,
            elapsed_time=sandbox_result.elapsed_time,
            passed=passed,
            failed=failed,
            status=status,
            timed_out=sandbox_result.timed_out,
            cancelled=sandbox_result.cancelled,
            error=sandbox_result.error,
            sandbox_status=sandbox_result.status.value,
        )
