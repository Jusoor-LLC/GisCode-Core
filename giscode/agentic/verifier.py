import ast
import re
import sys
from pathlib import Path
from typing import List, Optional

from giscode.agentic.test_runner import (
    BaselineComparison,
    Classification,
    build_gradle_test_command,
    build_test_command,
    classify_could_not_run,
    classify_gradle_result,
    classify_python_result,
    collection_error_signature,
    compare_baseline,
    detect_framework,
    detect_gradle_project,
    discover_test_files,
    pytest_missing_from_output,
    resolve_python,
)
from giscode.agentic.tool_kinds import WRITE_ACTION_TOOLS as _WRITE_ACTION_TOOLS
from giscode.agentic.types import ExecutionPlan, VerificationResult
from giscode.agentic.verification_budget import (
    GRADLE_PASS_TIMEOUT_S,
    MIN_USEFUL_PASS_S,
    PYTHON_PASS_TIMEOUT_S,
)


_MODIFICATION_KEYWORDS = (
    "fix", "add", "modify", "create", "implement", "repair", "change",
    "update", "write", "improve", "refactor", "remove", "delete", "patch",
    "replace", "migrate", "rename", "generate", "build", "ensure", "validate",
    "enforce",
)


def _task_requires_modification(task: any) -> bool:
    text = ""
    raw = getattr(task, "raw_request", "") or ""
    intent = getattr(task, "intent", "") or ""
    text = (raw + " " + intent).lower()
    return any(k in text for k in _MODIFICATION_KEYWORDS)


_FROZEN_TESTS_UNAVAILABLE = object()


def is_test_command_action(action) -> bool:
    """True when ``action`` is a pytest/unittest-style ``run_command``.

    Success is irrelevant: the caller decides whether a non-zero exit is a
    failure or evidence to compare against the baseline.
    """
    if not isinstance(action, dict) or action.get("tool") != "run_command":
        return False
    args = action.get("args") or {}
    command = str(args.get("command") or "").lower()
    arguments = [str(value).lower() for value in (args.get("arguments") or [])]
    tokens = [command, *arguments]
    return (
        command in {"pytest", "pytest.exe", "unittest"}
        or "pytest" in tokens
        or "unittest" in tokens
    )

# A Gradle verification run compiles the project and runs its tests, so it
# needs a longer budget than a pytest invocation. Bounded all the same. Both
# caps live in the budget module, so a pass and the pot that funds it cannot
# disagree about how long a run may take.
GRADLE_TIMEOUT_S = GRADLE_PASS_TIMEOUT_S
PROJECT_TEST_TIMEOUT_S = PYTHON_PASS_TIMEOUT_S

# Status values surfaced through ``test_execution_status``. Kept as module
# constants so tests and the verifier never disagree on spelling.
TEST_STATUS_PASSED = "passed"
TEST_STATUS_FAILED = "failed"
TEST_STATUS_NO_TESTS = "no_tests_collected"
TEST_STATUS_COULD_NOT_RUN = "could_not_run"
TEST_STATUS_UNAVAILABLE = "unavailable"

# ---------------------------------------------------------------------------
# Protected target test (anti-gaming gate)
#
# A fix that edits the test it was meant to satisfy does not prove anything:
# the suite goes green because the check was weakened, not because the code was
# repaired. The named target test is therefore protected. A test file may only
# change when the REQUEST is about changing or adding tests; every other edit
# to it - deleting it, cutting its assertions or its size by more than 30
# percent, or replacing its body with pass / a trivial assert - fails
# verification so the caller can roll the edit back.
# ---------------------------------------------------------------------------

PROTECTED_TEST_ERROR = "the fix changed the test it was meant to satisfy"

# A protected test must keep at least this share of its pre-edit assertions and
# size. Below it the file was gutted rather than revised.
_PROTECTED_MIN_KEEP_RATIO = 0.7
_PROTECTED_MIN_SIZE_CHARS = 40

# Phrasings that make a test edit the REQUESTED deliverable ("add a test",
# "update the assertions", "the test is wrong"). Only these may touch tests.
_TEST_CHANGE_PHRASING = re.compile(
    r"\b(?:add(?:ing|ed)?|append(?:ing|ed)?|writ(?:e|ing)|cre(?:ate|ating)|"
    r"updat(?:e|ing)|chang(?:e|ing)|modif(?:y|ying)|rewrit(?:e|ing)|replac(?:e|ing)|"
    r"delet(?:e|ing)|remov(?:e|ing)|adjust(?:ing|ed)?|correct(?:ing|ed)?|"
    r"expand(?:ing|ed)?|strengthen(?:ing|ed)?|extend(?:ing|ed)?|improv(?:e|ing)|"
    r"port(?:ing|ed)?|convert(?:ing|ed)?|renam(?:e|ing)|relax(?:ing|ed)?|"
    r"tighten(?:ing|ed)?|adapt(?:ing|ed)?|align(?:ing|ed)?|revis(?:e|ing))\b"
    r"[^.]{0,60}\btests?\b"
    r"|\btests?\b[^.]{0,60}\b(?:is|are)\b[^.]{0,25}"
    r"\b(?:wrong|incorrect|outdated|obsolete|stale|too strict)\b",
    re.IGNORECASE,
)

_NAMED_TEST_FILE = re.compile(r"(?<![\w.])(?:[A-Za-z0-9_]+[./\\])*test_[A-Za-z0-9_]+\.py\b")
_NAMED_DOTTED_TEST = re.compile(r"\btests[./\\](test_[A-Za-z0-9_]+)\b")
_BARE_TEST_NAME = re.compile(r"\btest_[A-Za-z0-9_]+\b")
_TEST_FILE_WALK_LIMIT = 4000
_TEST_WALK_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "site-packages", ".tox", ".eggs", "build", "dist",
    }
)


def request_allows_test_edits(request_text: str) -> bool:
    """True only when the request itself is about changing or adding tests."""
    return bool(_TEST_CHANGE_PHRASING.search(request_text or ""))


def named_test_files_in_request(request_text: str) -> List[str]:
    """Test files the request names, as workspace-relative POSIX paths."""
    names: List[str] = []
    text = request_text or ""
    for match in _NAMED_TEST_FILE.findall(text):
        normalized = match.replace("\\", "/")
        if normalized not in names:
            names.append(normalized)
    for module in _NAMED_DOTTED_TEST.findall(text):
        normalized = f"tests/{module}.py"
        if normalized not in names:
            names.append(normalized)
    return names


def bare_test_stems_in_request(request_text: str) -> List[str]:
    """Bare ``test_x`` mentions that were not already part of a named path."""
    residual = _NAMED_TEST_FILE.sub(" ", request_text or "")
    residual = _NAMED_DOTTED_TEST.sub(" ", residual)
    stems: List[str] = []
    for token in _BARE_TEST_NAME.findall(residual):
        if token not in stems:
            stems.append(token)
    return stems


def iter_project_test_files(root: Path, limit: int = _TEST_FILE_WALK_LIMIT):
    """Yield ``test_*.py`` files under ``root``, skipping junk directories."""
    stack = [root]
    seen = 0
    while stack and seen < limit:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        seen += 1
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in _TEST_WALK_SKIP_DIRS:
                        stack.append(entry)
                elif entry.is_file() and entry.name.startswith("test_") and entry.suffix == ".py":
                    yield entry
            except OSError:
                continue


def _test_file_shape(text: str) -> tuple[int, int]:
    """``(assert count, top-level test function count)`` for a test file."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return (
            len(re.findall(r"\bassert\b", text)),
            len(re.findall(r"(?m)^\s*(?:async\s+)?def\s+test_", text)),
        )
    asserts = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))
    functions = sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )
    return asserts, functions


def protected_test_gutted(pre: str, post: str) -> Optional[str]:
    """Why ``post`` guts the pre-edit test ``pre``; ``None`` when it does not."""
    if not post.strip():
        return "was deleted: nothing is left of the test"
    pre_asserts, pre_functions = _test_file_shape(pre)
    post_asserts, post_functions = _test_file_shape(post)
    if pre_functions and post_functions == 0:
        return "lost every test function (body replaced with pass or a trivial assert)"
    if pre_asserts and post_asserts < pre_asserts * _PROTECTED_MIN_KEEP_RATIO:
        return f"cut its assertions from {pre_asserts} to {post_asserts}"
    pre_size = len(pre.strip())
    post_size = len(post.strip())
    if pre_size >= _PROTECTED_MIN_SIZE_CHARS and post_size < pre_size * _PROTECTED_MIN_KEEP_RATIO:
        return f"cut its size from {pre_size} to {post_size} characters"
    return None


def _pass_timeout(cap_s: float, budget_s: Optional[float]) -> Optional[float]:
    """The wait this test run may use, or ``None`` when it cannot be funded.

    ``None`` means "no aggregate budget supplied" - every caller from before the
    budget existed - so the run keeps its own cap unchanged. A budget below
    ``MIN_USEFUL_PASS_S`` also returns ``None``: a run started with almost no
    time is killed before it can report anything, and returning a small number
    would be worse than refusing, because ``CommandExecutor._resolve_timeout``
    turns any non-positive timeout back into its 60 s default.
    """
    if budget_s is None:
        return cap_s
    try:
        left = float(budget_s)
    except (TypeError, ValueError):
        left = 0.0
    if left != left or left < MIN_USEFUL_PASS_S:  # NaN or too little to run
        return None
    return min(float(cap_s), left)


def _interpreter_detail(workspace_root: Path) -> str:
    """Name the interpreter and version the project's tests would use.

    Every verification says which interpreter it chose, not only the ones that
    could not find one: a run made with an unexpected Python is the difference
    between a real test failure and a wrong-environment result.
    """
    try:
        resolution = resolve_python(workspace_root)
    except Exception:  # noqa: BLE001 - a detail line must never fail a verdict
        return ""
    if not resolution.executable:
        return f"no Python interpreter ({resolution.reason})"
    version = (
        f"Python {getattr(resolution, 'version', '')}"
        if getattr(resolution, "version", "")
        else "unknown version"
    )
    return f"{resolution.executable} ({version}; {resolution.reason})"


class Verifier:
    def __init__(self, workspace_root: Path, terminal_executor=None):
        self._workspace_root = workspace_root.resolve()
        self._terminal_executor = terminal_executor

    def baseline(self, budget_s: Optional[float] = None) -> Optional[Classification]:
        """Run the project's tests *before* any edit and record the outcome.

        Returns ``None`` when no baseline can be established (packaged runtime
        without an interpreter, no terminal executor, Python unresolvable). A
        ``None`` baseline means callers must fall back to the pre-baseline rules
        rather than assume "all tests passed".
        """
        if self._terminal_executor is None:
            return None
        if getattr(sys, "frozen", False):
            resolution = resolve_python(self._workspace_root)
            if resolution.executable is None:
                return None
        return self._run_project_tests(budget_s=budget_s)

    def verify_project(self, budget_s: Optional[float] = None) -> VerificationResult:
        """Run the project's whole test suite with no task plan attached.

        Used when the thing being judged is the project itself (a finished build)
        rather than one edit. It is the same discovery, interpreter resolution,
        classification and honest status as the plan path's final test phase;
        only the plan-specific file checks are skipped, because there are none.
        """
        classification = self._run_project_tests(budget_s=budget_s)
        details = ["Project verification: running the project's own test suite"]
        return self._build_classification_result(classification, details)

    def verify(
        self,
        plan: ExecutionPlan,
        actions: list,
        baseline: Optional[Classification] = None,
        target_tests: Optional[set] = None,
        request_text: Optional[str] = None,
        budget_s: Optional[float] = None,
    ) -> VerificationResult:
        errors: list[str] = []
        details: list[str] = []

        # Anti-gaming gate: a fix may not rewrite the test it was meant to
        # satisfy unless the request itself asked for a test change. This runs
        # before anything else because no amount of green tests can redeem a
        # check that was weakened instead of the code being repaired.
        request = (request_text or getattr(plan, "objective", "") or "")
        violations = self._protected_target_test_violations(actions, request, target_tests)
        if violations:
            details.extend(violations)
            return VerificationResult(
                success=False,
                errors=[f"{PROTECTED_TEST_ERROR}: " + "; ".join(violations)],
                details=details,
                test_execution_status=TEST_STATUS_FAILED,
            )

        for index, step in enumerate(plan.steps):
            action_type = getattr(step.action_type, "value", step.action_type)
            action = actions[index] if index < len(actions) else None

            if action_type == "read":
                continue

            if action_type == "run_command":
                command = (self._action_value(action, "args", {}) or {}).get("command", "")
                label = command or step.description or "run_command"
                if self._action_value(action, "skipped_repeat", False):
                    # A throttled repeat is not a command result: it says the
                    # identical command already ran, so it carries no signal
                    # about the code. It must never become an error that
                    # short-circuits verification.
                    details.append("Skipped repeat command (throttled; no signal)")
                    continue
                action_success = self._action_value(action, "success", False)
                if action_success:
                    details.append(f"Command executed successfully: {label}")
                elif self._is_test_command_action(action):
                    # The plan ran the tests and they did not pass. That is
                    # evidence, not an error: whether it is the task's fault
                    # is decided by the baseline comparison below, so keep
                    # going instead of short-circuiting.
                    details.append(
                        f"Plan's test command did not pass: {label}; "
                        "comparing against the pre-edit baseline"
                    )
                else:
                    error = self._action_value(action, "error", None)
                    errors.append(
                        f"Command execution failed: {label}"
                        + (f": {error}" if error else "")
                    )

                # A command may optionally declare the file it was meant to
                # create or modify. Only verify that file when a real target
                # exists; never turn an empty target into the workspace root.
                if step.target_path:
                    self._verify_file_target(step, errors, details)
                continue

            self._verify_file_target(step, errors, details)

        # An early action-level error short-circuits the test phase.
        if errors:
            return VerificationResult(success=False, errors=errors, details=details)

        # Path 1 -- a run_command in the plan executed a pytest/unittest-style
        # tool already, end-to-end. Trust that signal only when it proves what
        # this task has to prove:
        #   * a captured baseline means we can (and must) compare the whole
        #     suite after the edit, otherwise a focused subset can mask a
        #     regression the edit introduced;
        #   * explicit target tests mean the task requires certain tests to be
        #     green, and a focused command that happened to pass does not show
        #     that they are.
        ran_test_command = any(self._is_test_command(action) for action in actions)
        if ran_test_command and baseline is None and not target_tests:
            return VerificationResult(
                success=True,
                errors=[],
                details=details + ["Focused test command passed"],
                test_execution_status=TEST_STATUS_PASSED,
            )
        if ran_test_command:
            details.append(
                "Focused test command passed; running the full suite to compare "
                "against the pre-edit baseline"
            )

        # Path 2 -- a packaged runtime has no embedded Python interpreter for
        # project tests. It may still use an external interpreter found by
        # ``resolve_python``; without one or without a command executor the
        # result is explicitly unavailable. The report names what was missing
        # and where it looked, instead of a bare "tests unavailable".
        if getattr(sys, "frozen", False):
            resolution = resolve_python(self._workspace_root)
            if resolution.executable is None or self._terminal_executor is None:
                reason = (
                    "Frozen (packaged) build: test execution unavailable - "
                    + resolution.reason
                    if resolution.executable is None
                    else "Frozen (packaged) build: test execution unavailable - "
                    "no terminal executor for external Python."
                )
                return VerificationResult(
                    success=False,
                    errors=[reason],
                    details=details + [reason],
                    test_execution_status=TEST_STATUS_UNAVAILABLE,
                )

        # Path 3 -- the plan did not modify any project file. There is
        # nothing to verify beyond what the plan already executed, so "no
        # tests" is not a failed verification: it is "tests were not
        # relevant to this plan".
        plan_wrote_files = any(
            isinstance(action, dict)
            and action.get("tool") in _WRITE_ACTION_TOOLS
            for action in actions
        )
        # Ask the same helper the test command uses: ``tests/test_*.py`` is the
        # project convention, but a project that keeps ``test_*.py`` beside its
        # code has tests too. Looking only in ``tests/`` used to report
        # ``no_tests_collected`` - and never execute anything - for those.
        has_tests = self._python_tests_present()
        # A Kotlin/Gradle project has tests even though it has no ``test_*.py``.
        # Verifying its edit with the project's own wrapper keeps the honest
        # status: without this a Kotlin edit was reported as "no tests
        # collected" before it had been run at all.
        if plan_wrote_files and not has_tests:
            gradle_project = self._gradle_project()
            if gradle_project is not None:
                classification = self._run_gradle_tests(
                    gradle_project, budget_s=budget_s
                )
                details.append(
                    "Gradle verification: "
                    + " ".join(gradle_project.tasks)
                    + f" via {gradle_project.wrapper.name}"
                )
                return self._build_classification_result(
                    classification, details, baseline=baseline, target_tests=None
                )
        if not has_tests and not plan_wrote_files:
            return VerificationResult(
                success=True,
                errors=[],
                details=details + [
                    "Plan did not write files; test execution was not attempted."
                ],
                test_execution_status=TEST_STATUS_UNAVAILABLE,
            )

        # Path 4 -- there is no test directory to run against and the plan
        # DID attempt to modify files. That is the honest "no tests
        # collected" outcome.
        if not has_tests:
            return VerificationResult(
                success=True,
                errors=["No test files were found in tests/ or the project root."],
                details=details + [
                    "Test execution was not attempted because there is no test_*.py "
                    "under tests/ or the project root."
                ],
                test_execution_status=TEST_STATUS_NO_TESTS,
            )

        # Path 5 -- no terminal executor means we cannot spawn Python.
        if self._terminal_executor is None:
            return VerificationResult(
                success=False,
                errors=["No terminal executor bound to the verifier."],
                details=details + [
                    "Python test execution unavailable: no terminal executor."
                ],
                test_execution_status=TEST_STATUS_UNAVAILABLE,
            )

        # Path 6 -- try to run the project's tests, classify honestly.
        classification = self._run_project_tests(budget_s=budget_s)
        return self._build_classification_result(
            classification, details, baseline=baseline, target_tests=target_tests
        )

    # ----- protected target test --------------------------------------------

    def _protected_target_tests(
        self, request_text: str, target_tests: Optional[set]
    ) -> dict:
        """Protected test files mapped to their absolute path.

        A file is protected when the task named it (``tests/test_x.py``,
        ``tests.test_x``, or a bare ``test_x``) or when it holds a test the
        task is required to make pass (``target_tests`` nodeids).
        """
        root = self._workspace_root
        protected: dict = {}

        def _add(candidate: str) -> None:
            path = Path(candidate)
            absolute = path if path.is_absolute() else (root / path)
            try:
                key = absolute.resolve().relative_to(root).as_posix()
            except ValueError:
                key = path.as_posix()
            protected.setdefault(key, absolute)

        for nodeid in sorted(target_tests or ()):
            file_part = str(nodeid).split("::", 1)[0].replace("\\", "/")
            if file_part.endswith(".py"):
                _add(file_part)
        for named in named_test_files_in_request(request_text):
            _add(named)
        for stem in bare_test_stems_in_request(request_text):
            try:
                matches = [
                    candidate
                    for candidate in iter_project_test_files(root)
                    if candidate.stem == stem
                ]
            except OSError:
                matches = []
            for candidate in matches[:3]:
                _add(str(candidate))
        return protected

    def _match_protected_test(self, protected: dict, raw_path: str):
        """Resolve an action path against the protected set, or ``None``."""
        path = Path(raw_path)
        absolute = path if path.is_absolute() else (self._workspace_root / path)
        try:
            key = absolute.resolve().relative_to(self._workspace_root).as_posix()
        except ValueError:
            key = path.as_posix()
        if key in protected:
            return key, protected[key]
        name = path.name
        for candidate_key, candidate_path in protected.items():
            if candidate_key == name or candidate_key.endswith("/" + name):
                return candidate_key, candidate_path
        return None

    @staticmethod
    def _read_post_edit(path: Path) -> str:
        try:
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        return ""

    def _protected_target_test_violations(
        self, actions: list, request_text: str, target_tests: Optional[set]
    ) -> list[str]:
        """Reasons this run weakened the test it was meant to satisfy."""
        if request_allows_test_edits(request_text):
            return []
        protected = self._protected_target_tests(request_text, target_tests)
        if not protected:
            return []

        pre_by_path: dict = {}
        written: dict = {}
        for action in actions or ():
            if isinstance(action, dict):
                tool = action.get("tool")
                success = action.get("success", True)
                args = action.get("args") or {}
                raw_path = str(args.get("path") or "").strip()
                original = action.get("original_content")
            else:
                action_type = getattr(getattr(action, "action_type", None), "value", "")
                tool = "write_file" if action_type in {"create", "modify"} else action_type
                success = getattr(action, "success", False)
                raw_path = str(getattr(action, "target_path", "") or "").strip()
                original = getattr(action, "original_content", None)
            if tool not in _WRITE_ACTION_TOOLS or not success:
                continue
            if not raw_path:
                continue
            hit = self._match_protected_test(protected, raw_path)
            if hit is None:
                continue
            key, absolute = hit
            if key not in pre_by_path:
                if isinstance(original, str):
                    pre_by_path[key] = original
            written[key] = absolute

        violations: list[str] = []
        for key in sorted(written):
            pre = pre_by_path.get(key)
            if pre is None:
                # The file did not exist before this task, so there was no
                # check to gut and nothing to protect yet.
                continue
            post = self._read_post_edit(written[key])
            if post == pre:
                continue
            reason = protected_test_gutted(pre, post)
            if reason:
                violations.append(f"{key} {reason}")
            else:
                violations.append(
                    f"{key} was rewritten although the request did not ask for a "
                    "test change"
                )
        return violations

    @staticmethod
    def _is_test_command(action) -> bool:
        return is_test_command_action(action) and bool(
            Verifier._action_value(action, "success", False)
        )

    @staticmethod
    def _is_test_command_action(action) -> bool:
        return is_test_command_action(action)

    @staticmethod
    def _action_value(action, key: str, default=None):
        if isinstance(action, dict):
            return action.get(key, default)
        return getattr(action, key, default)

    def _verify_file_target(self, step, errors: list[str], details: list[str]) -> None:
        target_path = step.target_path
        if not target_path:
            return
        target = Path(step.target_path)
        if target.is_absolute():
            resolved = target.resolve()
        else:
            resolved = (self._workspace_root / target).resolve()

        try:
            resolved.relative_to(self._workspace_root)
        except ValueError:
            errors.append(f"Path escapes workspace: {step.target_path}")
            return

        if not resolved.exists():
            errors.append(f"Expected file missing: {step.target_path}")
        else:
            details.append(f"Verified file exists: {step.target_path}")

            if step.target_path.endswith(".py"):
                try:
                    ast.parse(resolved.read_text(encoding="utf-8-sig"))
                    details.append(f"Python file parses: {step.target_path}")
                except SyntaxError as e:
                    errors.append(f"Python syntax error in {step.target_path}: {e}")

    # ----- test execution path ---------------------------------------------

    def _run_python_tests(self):
        """Legacy entrypoint kept for callers that have not been updated.

        Returns the same shape as before but routes through
        :mod:`giscode.agentic.test_runner`.
        """
        classification = self._run_project_tests()
        if classification.status in (TEST_STATUS_PASSED, TEST_STATUS_FAILED, TEST_STATUS_NO_TESTS):
            return (
                0 if classification.status == TEST_STATUS_PASSED else
                (classification.exit_code if classification.exit_code is not None else 1),
                classification.stdout_tail,
                classification.stderr_tail,
            )
        if classification.status == TEST_STATUS_UNAVAILABLE:
            return _FROZEN_TESTS_UNAVAILABLE
        return None

    def _run_project_tests(self, budget_s: Optional[float] = None) -> Classification:
        """Run the project's own tests and classify them honestly.

        A Kotlin/Gradle project has tests even though it has no ``test_*.py``.
        Routing it to Python discovery reported "no tests collected" without
        running anything, so a detected Gradle project with no Python tests is
        verified by its own wrapper (approved tasks only) and judged from the
        JUnit XML it writes. Python projects are untouched: a Gradle project is
        only ever detected when the wrapper, the build file and a Kotlin/Java/
        Android module are all really there.
        """
        if not self._python_tests_present():
            gradle_project = self._gradle_project()
            if gradle_project is not None:
                return self._run_gradle_tests(gradle_project, budget_s=budget_s)
        try:
            resolution = resolve_python(self._workspace_root)
        except Exception as exc:
            return classify_could_not_run(f"python resolution raised {type(exc).__name__}: {exc}")
        if resolution.executable is None:
            if getattr(sys, "frozen", False):
                # A packaged build has no embedded interpreter. That is an
                # "unavailable" test run (what _FROZEN_TESTS_UNAVAILABLE
                # signals to legacy callers), not a transient "could not run".
                # The message says Python was not found and where it looked.
                return Classification(
                    TEST_STATUS_UNAVAILABLE,
                    "packaged runtime: " + resolution.reason,
                    None,
                    "",
                    "",
                )
            return classify_could_not_run(resolution.reason)

        try:
            framework = detect_framework(self._workspace_root)
        except Exception as exc:
            return classify_could_not_run(
                f"framework detection raised {type(exc).__name__}: {exc}"
            )
        if not framework.importable:
            return classify_could_not_run(framework.reason)

        try:
            command = build_test_command(self._workspace_root, framework)
        except RuntimeError as exc:
            return classify_could_not_run(str(exc))

        pass_timeout = _pass_timeout(PROJECT_TEST_TIMEOUT_S, budget_s)
        if pass_timeout is None:
            return classify_could_not_run(
                "verification budget exhausted before the project tests could run "
                "(the change was never judged)"
            )

        try:
            from giscode.terminal.types import CommandRequest
            request = CommandRequest(
                command=command.executable,
                arguments=list(command.arguments),
                working_directory=str(command.working_directory),
                environment=dict(command.environment),
                timeout=pass_timeout,
            )
            result = self._terminal_executor.execute(request)
        except Exception as exc:
            return classify_could_not_run(
                f"terminal executor raised {type(exc).__name__}: {exc}"
            )

        if getattr(result, "timed_out", False):
            # A run that never finished judged nothing. Saying so is the only
            # honest verdict; a timeout is never a pass.
            return classify_could_not_run(
                f"tests timed out after {int(getattr(request, 'timeout', 0) or 0)} s "
                "(the change was never judged)"
            )

        classification = classify_python_result(
            command.framework,
            result.exit_code,
            result.stdout or "",
            result.stderr or "",
        )
        if classification.status == TEST_STATUS_FAILED and pytest_missing_from_output(
            result.stdout or "", result.stderr or ""
        ):
            # pytest is absent from the interpreter that was chosen. The tests
            # were never judged, so this is a could-not-run, and the interpreter
            # is the actionable cause -- never a test failure.
            return classify_could_not_run(
                "pytest is not installed in the interpreter chosen for this "
                f"project: {resolution.executable} "
                f"(Python {getattr(resolution, 'version', '') or 'unknown version'}); "
                "the tests were never judged"
            )
        return classification

    # ----- Kotlin / Gradle test execution path ------------------------------

    def _python_tests_present(self) -> bool:
        try:
            return bool(discover_test_files(self._workspace_root))
        except OSError:
            return False

    def _gradle_project(self):
        """The verified Gradle project under the workspace root, or ``None``.

        Detection is delegated to :func:`detect_gradle_project`, which only
        accepts a wrapper *inside* the root and a fixed set of approved tasks,
        so verification can never be pointed at an arbitrary executable.
        """
        try:
            project = detect_gradle_project(self._workspace_root)
        except Exception:  # noqa: BLE001 - detection never fails a verdict
            return None
        try:
            project.root.resolve().relative_to(self._workspace_root)
        except (AttributeError, ValueError, OSError):
            return None
        return project

    def _run_gradle_tests(self, project, budget_s: Optional[float] = None) -> Classification:
        """Run the project's own wrapper with its approved tasks, then classify."""
        if self._terminal_executor is None:
            return Classification(
                TEST_STATUS_UNAVAILABLE,
                "gradle verification unavailable: no terminal executor",
                None,
                "",
                "",
            )
        try:
            command = build_gradle_test_command(project)
        except (RuntimeError, ValueError) as exc:
            return classify_could_not_run(f"gradle command rejected: {exc}")
        pass_timeout = _pass_timeout(GRADLE_TIMEOUT_S, budget_s)
        if pass_timeout is None:
            return classify_could_not_run(
                "verification budget exhausted before the Gradle tests could run "
                "(the change was never judged)"
            )
        try:
            from giscode.terminal.types import CommandRequest

            request = CommandRequest(
                command=command.executable,
                arguments=list(command.arguments),
                working_directory=str(command.working_directory),
                environment=dict(command.environment),
                timeout=pass_timeout,
            )
            result = self._terminal_executor.execute(request)
        except Exception as exc:  # noqa: BLE001 - a runner error is not a verdict
            return classify_could_not_run(
                f"gradle runner raised {type(exc).__name__}: {exc}"
            )
        if getattr(result, "timed_out", False):
            return classify_could_not_run(
                f"tests timed out after {int(pass_timeout)} s "
                "(the change was never judged)"
            )
        return classify_gradle_result(
            project.root,
            project.tasks,
            int(getattr(result, "exit_code", -1)),
            getattr(result, "stdout", "") or "",
            getattr(result, "stderr", "") or "",
        )

    def _build_classification_result(
        self,
        classification: Classification,
        details: list[str],
        baseline: Optional[Classification] = None,
        target_tests: Optional[set] = None,
    ) -> VerificationResult:
        details = list(details) + classification_details(classification)
        ran = classification.status in (
            TEST_STATUS_PASSED,
            TEST_STATUS_FAILED,
            TEST_STATUS_NO_TESTS,
        )
        interpreter = _interpreter_detail(self._workspace_root)
        if interpreter:
            details.append(
                ("Tests ran with " if ran else "Test runner considered ") + interpreter
            )
        comparison = compare_baseline(baseline, classification)
        details.append(comparison.summary_line())

        # The comparison is already computed here; expose it as data instead
        # of only as prose so callers can rely on it. The defaults of
        # VerificationResult describe "no baseline evidence", so attaching
        # them changes no existing verdict, message or status.
        evidence = {
            "baseline_available": comparison.available,
            "newly_passing": list(comparison.newly_passing),
            "tests_before": getattr(baseline, "status", "unknown") if baseline is not None else "unknown",
            "tests_after": classification.status,
            # The runner already parsed these identities from its own output;
            # carrying them is what lets a later comparison tell "fewer of the
            # same failures" from "a different set of failures".
            "per_test": dict(classification.per_test or {}),
        }

        def _vr(**fields):
            return VerificationResult(**{**evidence, **fields})
        post_collect = collection_error_signature(classification)
        if post_collect is not None:
            base_collect = (
                collection_error_signature(baseline) if baseline is not None else None
            )
            if base_collect == post_collect:
                details.append(
                    "Collection error is identical to the pre-edit baseline "
                    "(environmental, not caused by this edit)"
                )
                return _vr(
                    success=False,
                    errors=[f"Could not run tests (environmental): {post_collect}"],
                    details=details,
                    test_execution_status=TEST_STATUS_COULD_NOT_RUN,
                )
            details.append(
                "Collection error differs from the pre-edit baseline "
                "(introduced or changed by this edit)"
            )
            return _vr(
                success=False,
                errors=[
                    "Tests could not collect after this edit "
                    f"(baseline was clean or differed): {post_collect}"
                ],
                details=details,
                test_execution_status=TEST_STATUS_FAILED,
            )
        if comparison.available and comparison.preexisting_failures:
            details.append(
                "Pre-existing failures (unchanged by this task): "
                + ", ".join(comparison.preexisting_failures)
            )
        if comparison.available and comparison.newly_passing:
            details.append(
                "Newly passing: " + ", ".join(comparison.newly_passing)
            )
        if comparison.available and comparison.new_failures:
            return _vr(
                success=False,
                errors=[
                    "New test failure(s) introduced by this task: "
                    + ", ".join(comparison.new_failures)
                ],
                details=details,
                test_execution_status=TEST_STATUS_FAILED,
            )
        if comparison.available and target_tests:
            failed_targets = sorted(
                nodeid
                for nodeid in (target_tests or set())
                if classification.per_test.get(nodeid) in {"failed", "error"}
            )
            if failed_targets:
                return _vr(
                    success=False,
                    errors=[
                        "Target test(s) failed: " + ", ".join(failed_targets)
                    ],
                    details=details,
                    test_execution_status=TEST_STATUS_FAILED,
                )
        if comparison.available:
            # Regression-free against a recorded baseline: only genuinely new
            # failures or failed target tests block task success. Pre-existing
            # failures are reported honestly and the factual status is kept
            # ("failed"), but they are not this task's fault, so the task may
            # still be reported as completed.
            if classification.status == TEST_STATUS_FAILED:
                return _vr(
                    success=True,
                    errors=[],
                    details=details,
                    test_execution_status=TEST_STATUS_FAILED,
                )
        if classification.status == TEST_STATUS_PASSED:
            return _vr(
                success=True,
                errors=[],
                details=details,
                test_execution_status=TEST_STATUS_PASSED,
            )
        if classification.status == TEST_STATUS_FAILED:
            return _vr(
                success=False,
                errors=[
                    f"{_label_for(classification.status)}: {classification.reason}"
                ],
                details=details,
                test_execution_status=TEST_STATUS_FAILED,
            )
        if classification.status == TEST_STATUS_NO_TESTS:
            return _vr(
                # Keep the legacy task lifecycle from treating an absent
                # test suite as an implementation failure. The report layer
                # still renders this status as NOT RUN, never PASS or FAIL.
                success=True,
                errors=[
                    f"{_label_for(classification.status)}: {classification.reason}"
                ],
                details=details,
                test_execution_status=TEST_STATUS_NO_TESTS,
            )
        return _vr(
            success=False,
            errors=[
                f"{_label_for(classification.status)}: {classification.reason}"
            ],
            details=details,
            test_execution_status=TEST_STATUS_COULD_NOT_RUN,
        )


def _label_for(status: str) -> str:
    return {
        TEST_STATUS_PASSED: "Tests passed",
        TEST_STATUS_FAILED: "Tests failed",
        TEST_STATUS_NO_TESTS: "No tests collected",
        TEST_STATUS_COULD_NOT_RUN: "Could not run tests",
        TEST_STATUS_UNAVAILABLE: "Test execution unavailable",
    }.get(status, status.replace("_", " ").capitalize())


def classification_details(classification: Classification) -> List[str]:
    """Build the detail lines a verifier should attach to a classification."""
    lines: List[str] = []
    if classification.exit_code is not None:
        lines.append(f"Test exit_code: {classification.exit_code}")
    if classification.stdout_tail:
        lines.append(f"Test stdout (tail): {classification.stdout_tail}")
    if classification.stderr_tail:
        lines.append(f"Test stderr (tail): {classification.stderr_tail}")
    if classification.reason and classification.exit_code is None:
        lines.append(f"Reason: {classification.reason}")
    return lines


class MockVerifier:
    def __init__(self, file_exists: bool = True, parse_success: bool = True):
        self._file_exists = file_exists
        self._parse_success = parse_success

    def verify(
        self,
        plan: ExecutionPlan,
        actions: list,
        baseline: Optional[Classification] = None,
        target_tests: Optional[set] = None,
        request_text: Optional[str] = None,
    ) -> VerificationResult:
        errors: list[str] = []
        details: list[str] = []

        for step in plan.steps:
            if step.action_type == "read":
                continue
            if self._file_exists:
                details.append(f"Verified file exists: {step.target_path}")
                if step.target_path.endswith(".py") and self._parse_success:
                    details.append(f"Python file parses: {step.target_path}")
            else:
                errors.append(f"Expected file missing: {step.target_path}")

        if errors:
            return VerificationResult(success=False, errors=errors, details=details)
        return VerificationResult(success=True, errors=[], details=details, test_execution_status="unknown")
