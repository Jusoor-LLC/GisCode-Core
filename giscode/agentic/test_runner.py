"""Project-aware test runner used by the GisCode verifier.

This module exists so the verifier stops conflating "we tried and Python was
missing" with "all tests passed". The verifier in :mod:`giscode.agentic.verifier`
delegates here to:

* choose a Python interpreter for the project under test
  (project virtual environment first, then the active interpreter);
* decide whether the project is a pytest project or a unittest project;
* construct the exact ``argv`` that should be passed to the
  :class:`CommandExecutor`;
* classify the subprocess result into one of four honest buckets
  (``passed``, ``failed``, ``no_tests_collected``, ``could_not_run``).

The exit codes below were chosen against the real behaviour of pytest and
``python -m unittest discover`` on Python 3.13. They are *values*, not
opinions: a non-zero pytest run that reports "no tests ran" is
``no_tests_collected`` and never mis-reported as ``failed``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import re


# Conservative default timers; the surrounding CommandExecutor owns the real
# timeout (so the verifier does not need a clock of its own).
DEFAULT_TEST_TIMEOUT_S = 120

# Recognised project venv folder names, in priority order. ``.env`` is
# deliberately excluded -- it is too easy to confuse with ``.env`` secrets.
_VENV_DIRECTORY_CANDIDATES: Tuple[str, ...] = (
    ".venv",
    "venv",
    "env",
    ".VENV",
    "VENV",
)

# Project markers that mean ``pytest`` should be the framework. ``conftest.py``
# alone is a strong signal even without a config file; ``pyproject.toml``
# without ``[tool.pytest.ini_options]`` is not (it is used by many non-pytest
# tools).
_PYTEST_MARKERS: Tuple[str, ...] = (
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "conftest.py",
)


# ---------------------------------------------------------------------------
# Python interpreter resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PythonResolution:
    """Result of :func:`resolve_python`."""
    executable: Optional[str]
    source: str          # "venv", "sys", "path", "py_launcher", None
    reason: str          # human-readable explanation; always populated.
    prefix_arguments: Tuple[str, ...] = ()
    version: str = ""    # e.g. "3.13.15"; empty when the probe could not answer


@lru_cache(maxsize=32)
def _interpreter_version(executable: str, prefix: Tuple[str, ...]) -> str:
    """The interpreter's ``major.minor.patch``, or ``""`` if it cannot run.

    Asked once per (executable, prefix) pair: verification resolves the same
    interpreter several times, and the answer cannot change mid-run.
    """
    try:
        completed = subprocess.run(
            [executable, *prefix, "-c", "import sys; print(sys.version.split()[0])"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip().splitlines()[0] if (completed.stdout or "").strip() else ""


def resolve_python(project_root: Optional[Path]) -> PythonResolution:
    """Return the best Python interpreter for running tests.

    Order of preference:

    1. Project venv (``<project_root>/.venv``, ``<project_root>/venv``,
       ``<project_root>/env``).
    2. ``sys.executable`` when the current process is running from source:
       the interpreter GisCode itself runs on, with its dependencies.
    3. ``python`` exactly as found on ``PATH``.

    A packaged build has no interpreter of its own, so there ``PATH`` and then
    the ``py`` launcher are used. When nothing matches, ``executable`` is
    :data:`None` and ``reason`` names the last thing we tried.
    """
    # Every place that is looked at is recorded, so that "no Python was
    # found" can be reported with the locations instead of a vague
    # "tests could not run".
    searched: list[str] = []
    if project_root is not None:
        root = Path(project_root).resolve()
        for name in _VENV_DIRECTORY_CANDIDATES:
            candidate = root / name
            for suffix in ("Scripts", "bin"):
                exe = candidate / suffix / "python.exe" if sys.platform == "win32" else candidate / suffix / "python"
                searched.append(str(exe))
                if exe.exists():
                    return PythonResolution(
                        executable=str(exe),
                        source="venv",
                        reason=f"project virtual environment at {candidate}",
                        version=_interpreter_version(str(exe), ()),
                    )

    if not getattr(sys, "frozen", False) and sys.executable:
        searched.append(f"the running interpreter ({sys.executable})")
        return PythonResolution(
            executable=sys.executable,
            source="sys",
            reason="current Python interpreter (sys.executable)",
            version=_interpreter_version(sys.executable, ()),
        )

    searched.append("'python' on PATH")
    searched.append("'python3' on PATH")
    found = shutil.which("python") or shutil.which("python3")
    if found:
        return PythonResolution(
            executable=found,
            source="path",
            reason=f"python on PATH ({found})",
            version=_interpreter_version(found, ()),
        )

    searched.append("the 'py' launcher on PATH")
    py_launcher = shutil.which("py")
    if py_launcher:
        return PythonResolution(
            executable=py_launcher,
            source="py_launcher",
            reason="py launcher found; will use '-3' to pick Python 3",
            prefix_arguments=("-3",),
            version=_interpreter_version(py_launcher, ("-3",)),
        )

    return PythonResolution(
        executable=None,
        source=None,
        reason=(
            "Python was not found: no interpreter could be located for this "
            "project. Looked in: " + "; ".join(searched)
        ),
    )


# ---------------------------------------------------------------------------
# Test framework detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameworkDetection:
    framework: str          # "pytest", "unittest"
    importable: bool        # True when the framework module is importable.
    reason: str             # human-readable explanation.


def discover_test_files(project_root: Optional[Path]) -> List[Path]:
    """Every eligible ``test_*.py`` in the project, in run order.

    The project convention is ``tests/test_*.py``. A project that keeps its
    tests beside the code (``calc.py`` + ``test_calc.py``) has no ``tests``
    package at all, and those root-level files are just as real: the verifier
    and the test command both have to see them, or such a project can never
    have its tests executed.
    """
    root = Path(project_root).resolve() if project_root else Path(".").resolve()
    files: List[Path] = []
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        files.extend(sorted(tests_dir.glob("test_*.py")))
    files.extend(sorted(root.glob("test_*.py")))
    return files


def test_run_targets(project_root: Optional[Path]) -> List[str]:
    """Paths a test command should be pointed at, relative to the project.

    ``tests`` stays the target whenever it holds tests, so the command for the
    conventional layout is unchanged. Otherwise the root-level test files are
    named explicitly; an empty list means the project has no tests at all.
    """
    root = Path(project_root).resolve() if project_root else Path(".").resolve()
    tests_dir = root / "tests"
    if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
        return ["tests"]
    return [path.name for path in sorted(root.glob("test_*.py"))]


# ---------------------------------------------------------------------------
# Gradle / Kotlin verification
# ---------------------------------------------------------------------------

#: The only Gradle tasks the verifier may run. A task name is taken from this
#: set or not at all - never from a model, and never from build output - so an
#: approved task cannot be turned into an arbitrary one.
APPROVED_GRADLE_TASKS = frozenset(
    {
        "allTests",
        "test",
        "testDebugUnitTest",
        "desktopTest",
        "jvmTest",
        "check",
    }
)

#: Gradle verdicts that mean "the build never judged the change".
_GRADLE_UNAVAILABLE_SENTINELS = (
    "not found in project",
    "could not determine the dependencies",
    "could not resolve all dependencies",
    "could not connect to the gradle daemon",
    "java_home is not set",
    "no java installations",
)

_INCLUDE_CALL = re.compile(r"^\s*include\s*\(([^)\n]*)", re.MULTILINE)
_QUOTED_NAME = re.compile(r"[\"'](:?[A-Za-z0-9_.-]+)[\"']")
_JVM_TARGET = re.compile(r"\bjvm\s*\(\s*[\"']([A-Za-z0-9_-]+)[\"']\s*\)")
_JVM_TARGET_DEFAULT = re.compile(r"\bjvm\s*\(\s*\)")


@dataclass(frozen=True)
class GradleProject:
    """A Kotlin/Gradle project the verifier can test, and how to test it."""

    root: Path
    wrapper: Path
    modules: Tuple[str, ...]
    tasks: Tuple[str, ...]


def _gradle_settings_file(root: Path) -> Optional[Path]:
    for name in ("settings.gradle.kts", "settings.gradle"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def gradle_modules(root: Path) -> Tuple[str, ...]:
    """Module names from ``include(":a", ":b")`` in the settings file."""
    settings = _gradle_settings_file(root)
    if settings is None:
        return ()
    try:
        text = settings.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    names: list[str] = []
    for call in _INCLUDE_CALL.finditer(text):
        for match in _QUOTED_NAME.finditer(call.group(1)):
            name = match.group(1).lstrip(":")
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _module_test_task(module_dir: Path) -> Optional[str]:
    """One approved test task for a module, chosen from its build plugins."""
    build_file = None
    for name in ("build.gradle.kts", "build.gradle"):
        candidate = module_dir / name
        if candidate.is_file():
            build_file = candidate
            break
    if build_file is None:
        return None
    try:
        text = build_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lowered = text.lower()
    if "org.jetbrains.kotlin.multiplatform" in lowered or "kotlin(\"multiplatform\")" in lowered:
        # A named JVM target (``jvm("desktop")``) owns its own test task and
        # needs no Android SDK, so prefer it; ``allTests`` otherwise.
        named = _JVM_TARGET.search(text)
        if named is not None:
            candidate = f"{named.group(1)}Test"
            if candidate in APPROVED_GRADLE_TASKS:
                return candidate
        if _JVM_TARGET_DEFAULT.search(text) and "jvmTest" in APPROVED_GRADLE_TASKS:
            return "jvmTest"
        return "allTests"
    if "com.android.application" in lowered or "com.android.library" in lowered:
        return "testDebugUnitTest"
    if (
        "org.jetbrains.kotlin.jvm" in lowered
        or "kotlin(\"jvm\")" in lowered
        or "java-library" in lowered
        or "kotlin.jvm" in lowered
    ):
        return "test"
    return None


def detect_gradle_project(project_root: Optional[Path]) -> Optional[GradleProject]:
    """A validated Gradle project for verification, or ``None``.

    Detection is deliberately strict: the project needs its own wrapper *inside*
    the workspace, a Gradle build file, and at least one module whose build file
    declares a Kotlin/Java/Android plugin. The wrapper is the only executable
    this path can produce, and the tasks come from :data:`APPROVED_GRADLE_TASKS`.
    """
    if project_root is None:
        return None
    try:
        root = Path(project_root).resolve(strict=False)
    except OSError:
        return None
    if not root.is_dir():
        return None
    wrapper: Optional[Path] = None
    for name in ("gradlew.bat", "gradlew"):
        candidate = root / name
        if candidate.is_file():
            wrapper = candidate
            break
    if wrapper is None:
        return None
    if not any((root / name).is_file() for name in ("build.gradle.kts", "build.gradle")):
        return None
    try:
        resolved_wrapper = wrapper.resolve()
        resolved_wrapper.relative_to(root)
    except (OSError, ValueError):
        return None

    modules = gradle_modules(root)
    tasks: list[str] = []
    for module in modules:
        module_dir = root / module.replace(":", "/")
        task = _module_test_task(module_dir)
        if task in APPROVED_GRADLE_TASKS:
            tasks.append(f":{module}:{task}")
    if not tasks:
        root_task = _module_test_task(root)
        if root_task in APPROVED_GRADLE_TASKS:
            tasks.append(root_task)
    if not tasks:
        return None
    return GradleProject(
        root=root, wrapper=resolved_wrapper, modules=modules, tasks=tuple(tasks)
    )


def build_gradle_test_command(project: GradleProject) -> TestCommand:
    """The argv for the project's own wrapper and the approved tasks only."""
    for task in project.tasks:
        leaf = task.rsplit(":", 1)[-1]
        if leaf not in APPROVED_GRADLE_TASKS:
            raise RuntimeError(f"gradle task {task!r} is not an approved verification task")
    return TestCommand(
        executable=str(project.wrapper),
        arguments=list(project.tasks) + ["--console=plain"],
        working_directory=project.root,
        environment={},
        framework="gradle",
    )


@dataclass(frozen=True)
class GradleTestCounts:
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return (
            f"{self.tests} tests, {self.failures} failures, "
            f"{self.errors} errors, {self.skipped} skipped"
        )


def gradle_test_counts(project_root: Optional[Path], limit: int = 200) -> Optional[GradleTestCounts]:
    """Counts from the JUnit XML Gradle writes, or ``None`` when absent.

    Reading the XML (rather than trusting the exit code) is what lets the
    verifier say *which* run failed and how many tests it judged.
    """
    if project_root is None:
        return None
    root = Path(project_root)
    if not root.is_dir():
        return None
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    seen = False
    for index, report in enumerate(sorted(root.glob("*/build/test-results/**/*.xml"))):
        if index >= limit:
            break
        try:
            import xml.etree.ElementTree as ElementTree

            parsed = ElementTree.parse(report).getroot()
        except Exception:  # noqa: BLE001 - a broken report is not a verdict
            continue
        elements = [parsed] if parsed.tag == "testsuite" else list(parsed.iter("testsuite"))
        for element in elements:
            seen = True
            for key in totals:
                try:
                    totals[key] += int(element.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
    return GradleTestCounts(**totals) if seen else None


def classify_gradle_result(
    project_root: Path,
    tasks: Tuple[str, ...],
    exit_code: int,
    stdout: str,
    stderr: str,
) -> Classification:
    """Classify a Gradle verification run honestly.

    Passed/failed come from the exit code *and* the JUnit XML: a build that
    exited non-zero without any test report (a missing task, offline deps) is
    ``could_not_run`` - the change was never judged - not a test failure.
    """
    text = f"{stdout}\n{stderr}".lower()
    counts = gradle_test_counts(Path(project_root))
    tail_out = _tail_text(stdout, 4000)
    tail_err = _tail_text(stderr, 4000)
    label = " ".join(tasks)
    if exit_code == 0:
        if counts is not None:
            return Classification(
                "passed",
                f"gradle {label} passed ({counts.summary()})",
                exit_code,
                tail_out,
                tail_err,
            )
        return Classification(
            "passed", f"gradle {label} passed", exit_code, tail_out, tail_err
        )
    if counts is None or counts.tests == 0:
        for sentinel in _GRADLE_UNAVAILABLE_SENTINELS:
            if sentinel in text:
                return Classification(
                    "could_not_run",
                    f"gradle {label} could not run: {sentinel}",
                    exit_code,
                    tail_out,
                    tail_err,
                )
    reason = (
        f"gradle {label} failed ({counts.summary()})"
        if counts is not None and counts.tests
        else f"gradle {label} failed (exit {exit_code})"
    )
    return Classification("failed", reason, exit_code, tail_out, tail_err)


def detect_framework(project_root: Optional[Path]) -> FrameworkDetection:
    """Pick pytest vs unittest for the project under test.

    pytest is preferred only when *all three* of these hold:

    * pytest is importable by the project's selected interpreter;
    * the project has a pytest marker or pytest-style test.

    Otherwise we fall back to unittest. Returning ``unittest`` here does not
    by itself prove anything ran -- the executor still records the actual
    outcome.
    """
    root = Path(project_root).resolve() if project_root else Path(".")
    resolution = resolve_python(root)
    pytest_importable = _interpreter_importable(resolution, "pytest")
    unittest_importable = _interpreter_importable(resolution, "unittest")
    if not pytest_importable and not unittest_importable:
        return FrameworkDetection(
            framework="unittest",
            importable=False,
            reason="neither pytest nor unittest could be imported",
        )

    candidate_files: List[Path] = discover_test_files(root)

    if pytest_importable:
        has_marker = _has_pytest_config(root)
        has_pytest_test = any(_file_has_pytest_test(file) for file in candidate_files)
        if has_marker or has_pytest_test:
            return FrameworkDetection(
                framework="pytest",
                importable=True,
                reason="project has pytest markers or pytest-style tests",
            )

    return FrameworkDetection(
        framework="unittest",
        importable=unittest_importable,
        reason="no pytest markers or tests; using unittest",
    )


def _module_importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _interpreter_importable(resolution: PythonResolution, module: str) -> bool:
    if not resolution.executable:
        return False
    command = [resolution.executable, *resolution.prefix_arguments, "-c", f"import {module}"]
    try:
        completed = subprocess.run(
            command,
            cwd=None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _has_pytest_config(root: Path) -> bool:
    for marker in ("pytest.ini", "conftest.py"):
        if (root / marker).exists():
            return True
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            return "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8")
        except OSError:
            pass
    setup_cfg = root / "setup.cfg"
    if setup_cfg.exists():
        try:
            return "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8")
        except OSError:
            pass
    tox_ini = root / "tox.ini"
    if tox_ini.exists():
        try:
            return "[testenv:pytest]" in tox_ini.read_text(encoding="utf-8")
        except OSError:
            pass
    return False


def _file_has_pytest_test(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("def test_") or stripped.startswith("async def test_"):
            return True
    return False


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestCommand:
    executable: str
    arguments: List[str]
    working_directory: Path
    environment: dict
    framework: str

    def as_command_request(self, timeout: int = DEFAULT_TEST_TIMEOUT_S):
        """Return a dict compatible with
        :class:`giscode.terminal.types.CommandRequest`."""
        return {
            "command": self.executable,
            "arguments": list(self.arguments),
            "working_directory": str(self.working_directory),
            "environment": dict(self.environment),
            "timeout": timeout,
        }


def build_test_command(
    project_root: Path,
    framework_detection: FrameworkDetection,
) -> TestCommand:
    """Build the argv + env for the project's test run.

    ``PYTHONPATH`` is set so `src/` layouts work without an extra package in
    the project's own configuration. Both the project root and ``<root>/src``
    are added when both exist.
    """
    resolution = resolve_python(project_root)
    if resolution.executable is None:
        raise RuntimeError(resolution.reason)

    root = Path(project_root).resolve()
    pythonpath_parts = [str(root)]
    src_dir = root / "src"
    if src_dir.is_dir():
        pythonpath_parts.append(str(src_dir))

    separator = ";" if sys.platform == "win32" else ":"
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env = {
        "PYTHONPATH": separator.join(pythonpath_parts),
        # The project under test may have its own autouse cleanup fixture. A
        # verifier run must not let that fixture kill GisCode's live model.
        "GISCODE_KEEP_LLAMA_SERVER": "1",
    }

    framework = framework_detection.framework
    targets = test_run_targets(root)
    if framework == "pytest":
        return TestCommand(
            executable=resolution.executable,
            # ``tests`` whenever it holds tests (unchanged); root-level test
            # files by name otherwise, so a project without a tests package
            # still runs the tests it does have.
            arguments=[*resolution.prefix_arguments, "-m", "pytest", *(targets or ["tests"]), "-v"],
            working_directory=root,
            environment=env,
            framework="pytest",
        )

    # unittest discovery needs a start directory: ``tests`` for the
    # conventional layout, the project root for root-level test files.
    unittest_start = "tests" if not targets or targets == ["tests"] else "."
    return TestCommand(
            executable=resolution.executable,
            arguments=[
            *resolution.prefix_arguments,
            "-m",
            "unittest",
            "discover",
            "-s",
            unittest_start,
            "-p",
            "test_*.py",
            "-v",
        ],
        working_directory=root,
        environment=env,
        framework="unittest",
    )


# ---------------------------------------------------------------------------
# Result classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    status: str            # "passed", "failed", "no_tests_collected", "could_not_run"
    reason: str            # human-readable explanation.
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    # Per-test outcomes (node id -> "passed"/"failed"/"error"/"skipped") parsed
    # from verbose output. Baseline comparison needs to know *which* tests
    # changed, not just that the suite went red.
    per_test: Dict[str, str] = field(default_factory=dict)

    @property
    def is_pass(self) -> bool:
        return self.status == "passed"

    @property
    def is_fail(self) -> bool:
        return self.status == "failed"


# pytest exit-code 5 == "no tests collected" (per pytest docs).
_PYTEST_NO_TESTS_EXIT_CODE = 5

# pytest exit-code 4 == usage/collection error: the run aborted before any test
# executed (e.g. a conftest import failure such as ModuleNotFoundError). The
# suite never judged the change, so this must NOT be reported as a test failure
# or trigger rollback - it is an environmental "tests could not run".
_PYTEST_COLLECTION_ERROR_EXIT_CODE = 4

_COLLECTION_ERROR_SENTINELS: Tuple[str, ...] = (
    "importerror while loading conftest",
    "importerror while importing test module",
    "errors during collection",
    "error collecting",
    "modulenotfounderror",
)

_UNITTEST_NO_TESTS_SENTINELS: Tuple[str, ...] = (
    "Ran 0 tests",
    "NO TESTS RAN",
)


def classify_python_result(
    framework: str,
    exit_code: Optional[int],
    stdout: str,
    stderr: str,
) -> Classification:
    """Bucket a subprocess result into a single honest classification.

    The classifier *only* inspects the recorded subprocess evidence; it never
    inspects the project state. "Could not run" is reserved for callers that
    did not even manage to start a subprocess -- those who did start one and
    got back a non-zero pytest ``5`` get ``no_tests_collected`` instead.
    """
    stdout_tail = _tail_text(stdout, max_chars=2000)
    stderr_tail = _tail_text(stderr, max_chars=2000)
    per_test = parse_test_outcomes(framework, stdout, stderr)

    if framework == "pytest":
        if exit_code == 0:
            return Classification("passed", "pytest exit 0", exit_code, stdout_tail, stderr_tail, per_test)
        if exit_code == _PYTEST_NO_TESTS_EXIT_CODE:
            return Classification("no_tests_collected", "pytest exit 5: no tests collected", exit_code, stdout_tail, stderr_tail, per_test)
        if any(
            sentinel in f"{stdout}\n{stderr}".lower() for sentinel in _COLLECTION_ERROR_SENTINELS
        ):
            return Classification(
                "could_not_run",
                f"pytest exit {_PYTEST_COLLECTION_ERROR_EXIT_CODE}: tests could not run (collection/import/setup error)",
                exit_code, stdout_tail, stderr_tail, per_test,
            )
        return Classification("failed", f"pytest exit {exit_code}", exit_code, stdout_tail, stderr_tail, per_test)

    # unittest branch.
    combined_output = f"{stdout}\n{stderr}"
    if any(sentinel in combined_output for sentinel in _UNITTEST_NO_TESTS_SENTINELS):
        return Classification("no_tests_collected", "unittest did not collect any tests", exit_code, stdout_tail, stderr_tail, per_test)
    if exit_code == 0:
        return Classification("passed", "unittest exit 0", exit_code, stdout_tail, stderr_tail, per_test)
    return Classification("failed", f"unittest exit {exit_code}", exit_code, stdout_tail, stderr_tail, per_test)


def classify_could_not_run(reason: str) -> Classification:
    """Build a ``could_not_run`` classification with a human reason."""
    return Classification("could_not_run", reason, None, "", "")


# The interpreter's own message when a module is absent. ``python -m pytest``
# exits 1 with "No module named pytest" and no traceback, which the generic
# classifier would otherwise report as a test failure -- blaming the change for
# a missing dependency in the interpreter that was chosen.
_PYTEST_MISSING_SENTINELS: Tuple[str, ...] = (
    "no module named pytest",
    "no module named 'pytest'",
)


def pytest_missing_from_output(stdout: str, stderr: str) -> bool:
    """True when the run never started because pytest is not installed.

    Kept out of :func:`classify_python_result` because the message that
    explains this must name the interpreter, which the classifier does not
    receive.
    """
    combined = f"{stdout}\n{stderr}".lower()
    return any(sentinel in combined for sentinel in _PYTEST_MISSING_SENTINELS)


def collection_error_signature(classification: Optional[Classification]) -> Optional[str]:
    """Normalized collection/import error for a run, or None if it collected.

    Two runs "failed the same way" when their signatures match, so the
    verifier can tell an environmental collection failure (present before
    the edit too) apart from one the edit introduced.
    """
    if classification is None:
        return None
    if classification.status != "could_not_run":
        return None
    combined = f"{classification.stdout_tail}\n{classification.stderr_tail}".lower()
    matched = tuple(s for s in _COLLECTION_ERROR_SENTINELS if s in combined)
    if not matched:
        return None
    modules = tuple(sorted(set(re.findall(r"no module named ['\"]?([\w.]+)", combined))))
    return "|".join(matched) + "#" + ",".join(modules)


# ---------------------------------------------------------------------------
# Per-test outcomes and baseline comparison
# ---------------------------------------------------------------------------

_PYTEST_OUTCOME = re.compile(
    r"^(?P<nodeid>\S+\.py::\S+)\s+"
    r"(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_PYTEST_SUMMARY = re.compile(
    r"^(?P<outcome>FAILED|ERROR)\s+(?P<nodeid>\S+\.py::\S+)\s+-\s*(?P<reason>.*)$"
)
_UNITTEST_OUTCOME = re.compile(
    r"^(?P<test>\w+)\s+\((?P<target>[\w.]+)\)\s+\.\.\.\s+"
    r"(?P<outcome>ok|FAIL|ERROR|skipped.*|expected failure.*)$",
    re.IGNORECASE,
)

_PYTEST_OUTCOMES = {
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "SKIPPED": "skipped",
    "XFAIL": "xfailed",
    "XPASS": "xpassed",
}

_FAILURE_OUTCOMES = frozenset({"failed", "error"})


def parse_test_outcomes(framework: str, stdout: str, stderr: str) -> Dict[str, str]:
    """Extract per-test outcomes from a verbose pytest/unittest run.

    Returns a mapping of test id -> one of ``passed``, ``failed``, ``error``,
    ``skipped``, ``xfailed``, ``xpassed``. Returns an empty mapping when the
    output is not verbose enough to enumerate tests, so callers must treat an
    empty result as "no per-test evidence" rather than "everything passed".
    """
    outcomes: Dict[str, str] = {}
    for line in f"{stdout}\n{stderr}".splitlines():
        line = line.strip()
        if not line:
            continue
        if framework == "pytest":
            match = _PYTEST_OUTCOME.match(line)
            if match:
                outcomes[match.group("nodeid")] = _PYTEST_OUTCOMES[match.group("outcome")]
        else:
            match = _UNITTEST_OUTCOME.match(line)
            if match:
                verdict = match.group("outcome").lower()
                if verdict == "ok":
                    status = "passed"
                elif verdict == "fail":
                    status = "failed"
                elif verdict == "error":
                    status = "error"
                elif verdict.startswith("skipped"):
                    status = "skipped"
                else:
                    status = "xfailed"
                outcomes[match.group("target")] = status
    return outcomes


def parse_test_failure_reasons(framework: str, stdout: str, stderr: str) -> Dict[str, str]:
    """Map failing test id -> short reason, from pytest's short summary block."""
    if framework != "pytest":
        return {}
    reasons: Dict[str, str] = {}
    for line in f"{stdout}\n{stderr}".splitlines():
        match = _PYTEST_SUMMARY.match(line.strip())
        if match:
            reasons[match.group("nodeid")] = _reason_signature(match.group("reason"))
    return reasons


def _reason_signature(reason: str) -> str:
    """Normalise a failure reason so 'same failure' comparisons are stable."""
    first = (reason or "").strip().splitlines()[0] if (reason or "").strip() else ""
    return re.sub(r"0x[0-9a-fA-F]+", "0xADDR", first)[:200]


@dataclass(frozen=True)
class BaselineComparison:
    """How a post-edit test run differs from the pre-edit baseline."""

    available: bool                     # False when no usable per-test evidence
    newly_passing: Tuple[str, ...] = ()      # failing before, passing now
    preexisting_failures: Tuple[str, ...] = ()  # failing before and still failing
    new_failures: Tuple[str, ...] = ()        # passing before, failing now
    unchanged_failures: Tuple[str, ...] = ()   # failing before, still failing, not in baseline per-test map
    baseline_passed: int = 0
    baseline_failed: int = 0
    post_passed: int = 0
    post_failed: int = 0

    @property
    def is_regression_free(self) -> bool:
        """True when nothing that used to pass now fails."""
        return self.available and not self.new_failures

    def summary_line(self) -> str:
        if not self.available:
            return "Test comparison vs baseline unavailable (no per-test baseline evidence)"
        return (
            f"Test comparison vs baseline: {len(self.newly_passing)} newly passing, "
            f"{len(self.preexisting_failures) + len(self.unchanged_failures)} pre-existing failure(s), "
            f"{len(self.new_failures)} new failure(s)"
        )


def compare_baseline(
    baseline: Optional[Classification], post: Optional[Classification]
) -> BaselineComparison:
    """Diff two runs. A test that failed in the baseline stays pre-existing even
    when this run fails for a different reason; only a test that was passing
    before and fails now counts as a new failure."""
    if baseline is None or post is None:
        return BaselineComparison(available=False)
    before = baseline.per_test or {}
    after = post.per_test or {}
    if not before or not after:
        return BaselineComparison(
            available=False,
            baseline_passed=len(before),
            baseline_failed=len(before),
            post_passed=len(after),
            post_failed=len(after),
        )

    newly_passing: List[str] = []
    preexisting: List[str] = []
    new_failures: List[str] = []

    for nodeid, status in after.items():
        was = before.get(nodeid)
        if status in _FAILURE_OUTCOMES:
            if was in _FAILURE_OUTCOMES:
                # Already failing in the captured baseline, so it is pre-existing
                # even when the edit changed *how* it fails. A test that was red
                # before this task is not a regression introduced by it, and
                # counting it as new hid the real pre-existing total.
                preexisting.append(nodeid)
            else:
                # Passed before (or is brand new) and fails now: a regression.
                new_failures.append(nodeid)
        elif was in _FAILURE_OUTCOMES:
            newly_passing.append(nodeid)

    # Tests that failed in the baseline and are absent from the post map did not
    # get a chance to run (collection error, skip). Report them honestly.
    unresolved = tuple(
        sorted(nodeid for nodeid, status in before.items() if status in _FAILURE_OUTCOMES and nodeid not in after)
    )

    return BaselineComparison(
        available=True,
        newly_passing=tuple(sorted(newly_passing)),
        preexisting_failures=tuple(sorted(preexisting)),
        new_failures=tuple(sorted(new_failures)),
        unchanged_failures=unresolved,
        baseline_passed=sum(1 for s in before.values() if s not in _FAILURE_OUTCOMES),
        baseline_failed=sum(1 for s in before.values() if s in _FAILURE_OUTCOMES),
        post_passed=sum(1 for s in after.values() if s not in _FAILURE_OUTCOMES),
        post_failed=sum(1 for s in after.values() if s in _FAILURE_OUTCOMES),
    )


def _tail_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:].rstrip()
