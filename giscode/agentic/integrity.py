"""Deterministic, read-only integrity checks for a selected project root.

This complements the existing GisAI artifact discovery without invoking its
clarification/planning lifecycle. It never executes project code or writes into
the project. Only recognized formats are parsed; unknown files are checked for
readability and otherwise left unclassified.
"""
from __future__ import annotations

import ast
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class IntegrityCategory(str, Enum):
    MISSING = "MISSING"
    UNREADABLE = "UNREADABLE"
    MALFORMED = "MALFORMED"
    CORRUPTED_OR_INVALID = "CORRUPTED_OR_INVALID"
    BROKEN_REFERENCE = "BROKEN_REFERENCE"
    SUSPICIOUS = "SUSPICIOUS"
    OK = "OK"
    NOT_CHECKED = "NOT_CHECKED"


@dataclass(frozen=True)
class IntegrityFinding:
    path: str
    category: IntegrityCategory
    evidence: str
    severity: str
    confidence: str
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "category": self.category.value,
            "evidence": self.evidence,
            "severity": self.severity,
            "confidence": self.confidence,
            "source": self.source,
        }


@dataclass
class GitRepositoryEvidence:
    status: str = "NOT_CHECKED"
    evidence: str = ""
    tracked_modified: list[str] = field(default_factory=list)
    tracked_deleted: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "evidence": self.evidence,
            "tracked_modified": list(self.tracked_modified),
            "tracked_deleted": list(self.tracked_deleted),
            "untracked": list(self.untracked),
            "ignored": list(self.ignored),
        }


@dataclass
class ProjectIntegrityReport:
    project_root: str
    inspected_at: str
    directories_examined: int = 0
    files_examined: int = 0
    files_validated: int = 0
    ok_count: int = 0
    unvalidated_files: int = 0
    excluded_directories: list[str] = field(default_factory=list)
    findings: list[IntegrityFinding] = field(default_factory=list)
    git: GitRepositoryEvidence = field(default_factory=GitRepositoryEvidence)

    @property
    def counts(self) -> dict[str, int]:
        counts = {category.value: 0 for category in IntegrityCategory}
        counts[IntegrityCategory.OK.value] = self.ok_count
        for finding in self.findings:
            if (
                finding.category != IntegrityCategory.NOT_CHECKED
                or finding.source != "unvalidated-file"
            ):
                counts[finding.category.value] += 1
        counts[IntegrityCategory.NOT_CHECKED.value] += (
            self.unvalidated_files + len(self.excluded_directories)
        )
        return counts

    @property
    def status(self) -> str:
        definitive = {
            IntegrityCategory.MISSING,
            IntegrityCategory.UNREADABLE,
            IntegrityCategory.MALFORMED,
            IntegrityCategory.CORRUPTED_OR_INVALID,
            IntegrityCategory.BROKEN_REFERENCE,
        }
        if any(finding.category in definitive for finding in self.findings):
            return "ISSUES FOUND"
        if any(
            finding.category == IntegrityCategory.SUSPICIOUS
            for finding in self.findings
        ):
            return "REVIEW"
        if self.unvalidated_files or self.excluded_directories or self.findings:
            return "PARTIAL — NO ISSUES DETECTED IN CHECKED ITEMS"
        return "NO ISSUES DETECTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "inspected_at": self.inspected_at,
            "status": self.status,
            "directories_examined": self.directories_examined,
            "files_examined": self.files_examined,
            "files_validated": self.files_validated,
            "ok_count": self.ok_count,
            "unvalidated_files": self.unvalidated_files,
            "excluded_directories": list(self.excluded_directories),
            "counts": self.counts,
            "findings": [finding.to_dict() for finding in self.findings],
            "git": self.git.to_dict(),
        }


@dataclass(frozen=True)
class _FileReference:
    source: str
    location: str
    value: str
    kind: str = "file"
    implicit_node_resolution: bool = False


class ProjectIntegrityInspector:
    """Inspect one explicitly selected project root without modifying it."""

    MAX_FILES = 20_000
    MAX_DIRECTORIES = 10_000
    MAX_FILE_BYTES = 8 * 1024 * 1024
    MAX_NOT_CHECKED_SAMPLES = 100

    _GENERATED_DIRECTORIES = {
        ".git",
        ".next",
        ".nuxt",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "deriveddata",
        "dist",
        "node_modules",
        "target",
        "venv",
        # .NET compilation and deployment output. A .NET project writes its
        # build products into "bin" and "obj", and self-updating/publish
        # deployments stage and copy their runtime into "publish" and
        # ".update-stage". These trees are regenerated from source, so their
        # contents are deliberately not treated as project source.
        "bin",
        "obj",
        "publish",
        ".update-stage",
    }
    # Generated directory names recognised by prefix. Self-updating builds
    # emit sibling deployment folders such as "publish-portable", so the
    # "publish-" family is classified as generated output as well.
    _GENERATED_DIRECTORY_PREFIXES = ("publish-",)
    _XML_SUFFIXES = {".xml", ".csproj", ".props", ".targets", ".xcscheme"}
    _NODE_EXTENSIONS = (".js", ".mjs", ".cjs", ".json")

    def __init__(
        self,
        project_root: str | Path,
        *,
        project_store: Any = None,
        project_id: Optional[str] = None,
        max_files: int = MAX_FILES,
        max_directories: int = MAX_DIRECTORIES,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self._requested_root = Path(project_root).expanduser()
        self._project_store = project_store
        self._project_id = project_id
        self.max_files = max(1, int(max_files))
        self.max_directories = max(1, int(max_directories))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self._reset()

    def _reset(self) -> None:
        self._root: Optional[Path] = None
        self._report_root = str(self._requested_root.absolute())
        self._directories_examined = 0
        self._files_examined = 0
        self._files_validated = 0
        self._ok_count = 0
        self._unvalidated_files = 0
        self._excluded_directories: list[str] = []
        self._findings: list[IntegrityFinding] = []
        self._finding_keys: set[tuple[str, str, str, str]] = set()
        self._inventory: dict[str, str] = {}
        self._symlink_paths: set[str] = set()
        self._unreadable_directories: set[str] = set()
        self._excluded_paths: set[str] = set()
        self._references: list[_FileReference] = []
        self._git = GitRepositoryEvidence()
        self._git_executable: Optional[str] = None
        self._git_deleted_paths: set[str] = set()
        self._git_ignored_paths: set[str] = set()
        self._scan_truncated = False

    def inspect(self) -> ProjectIntegrityReport:
        self._reset()
        requested = Path(os.path.abspath(str(self._requested_root)))
        self._report_root = str(requested)
        self._inspect_selected_project_metadata()
        try:
            root_stat = requested.lstat()
        except FileNotFoundError:
            self._record(
                IntegrityCategory.MISSING,
                ".",
                "The selected project root does not exist.",
                severity="error",
            )
            return self._report()
        except OSError as exc:
            self._record(
                IntegrityCategory.UNREADABLE,
                ".",
                f"Cannot inspect the selected project root: {type(exc).__name__}: {exc}",
                severity="error",
            )
            return self._report()

        if self._is_reparse_point(root_stat):
            self._record(
                IntegrityCategory.NOT_CHECKED,
                ".",
                "The selected project root is a symlink/reparse point; it was not followed.",
                severity="info",
                confidence="not_checked",
            )
            return self._report()
        if not stat.S_ISDIR(root_stat.st_mode):
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                ".",
                "The selected project root exists but is not a directory.",
                severity="error",
            )
            return self._report()

        try:
            self._root = requested.resolve(strict=True)
        except OSError as exc:
            self._record(
                IntegrityCategory.UNREADABLE,
                ".",
                f"Cannot resolve the selected project root: {type(exc).__name__}: {exc}",
                severity="error",
            )
            return self._report()

        self._report_root = str(self._root)
        self._inspect_repository_state()
        self._scan_project_tree()
        self._check_references()
        self._add_git_deletions()
        self._findings.sort(key=lambda f: (f.path.casefold(), f.category.value))
        return self._report()

    def _inspect_selected_project_metadata(self) -> None:
        """Check only the selected project's persisted plan JSON, if provided."""
        if self._project_store is None or not self._project_id:
            return
        record_path = f"ProjectStore.projects[{self._project_id}].plan_json"
        try:
            raw_plan = self._project_store.get_project_plan_json_raw(self._project_id)
        except Exception as exc:
            self._record(
                IntegrityCategory.UNREADABLE,
                record_path,
                f"Cannot read the selected project plan record: {type(exc).__name__}: {exc}",
                severity="error",
            )
            return
        if raw_plan is None:
            self._record(
                IntegrityCategory.MISSING,
                f"ProjectStore.projects[{self._project_id}]",
                "The selected project record is no longer present in ProjectStore.",
                severity="error",
            )
            return
        try:
            plan = json.loads(raw_plan)
        except json.JSONDecodeError as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                record_path,
                f"JSON parser failed at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                severity="error",
            )
            return
        except (TypeError, ValueError) as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                record_path,
                f"Project plan is not valid JSON: {type(exc).__name__}: {exc}",
                severity="error",
            )
            return
        if not isinstance(plan, dict):
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                record_path,
                "Project plan JSON parsed, but its root value is not an object.",
                severity="error",
            )
            return

        if "mode" in plan:
            from giscode.projects.model import VALID_MODES

            if plan["mode"] not in VALID_MODES:
                self._record(
                    IntegrityCategory.CORRUPTED_OR_INVALID,
                    record_path,
                    f"Project mode {plan['mode']!r} is not a supported mode.",
                    severity="error",
                )

        if "visual_design" not in plan:
            return
        visual_design = plan["visual_design"]
        visual_path = f"{record_path}.visual_design"
        if not isinstance(visual_design, dict):
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                visual_path,
                "visual_design must be an object when present.",
                severity="error",
            )
            return
        screens = visual_design.get("screens", [])
        connections = visual_design.get("connections", [])
        if not isinstance(screens, list):
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                f"{visual_path}.screens",
                "screens must be a list.",
                severity="error",
            )
            return
        if not isinstance(connections, list):
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                f"{visual_path}.connections",
                "connections must be a list.",
                severity="error",
            )
            return

        screen_ids: set[str] = set()
        for index, screen in enumerate(screens):
            if not isinstance(screen, dict) or not isinstance(screen.get("id"), str) or not screen["id"]:
                self._record(
                    IntegrityCategory.CORRUPTED_OR_INVALID,
                    f"{visual_path}.screens[{index}]",
                    "Every screen must be an object with a non-empty string id.",
                    severity="error",
                )
                continue
            if screen["id"] in screen_ids:
                self._record(
                    IntegrityCategory.CORRUPTED_OR_INVALID,
                    f"{visual_path}.screens[{index}].id",
                    f"Duplicate screen ID {screen['id']!r}.",
                    severity="error",
                )
            screen_ids.add(screen["id"])
            properties = screen.get("properties", {})
            if not isinstance(properties, dict):
                self._record(
                    IntegrityCategory.CORRUPTED_OR_INVALID,
                    f"{visual_path}.screens[{index}].properties",
                    "screen properties must be an object.",
                    severity="error",
                )
            else:
                if "description" in properties and not isinstance(properties["description"], str):
                    self._record(
                        IntegrityCategory.CORRUPTED_OR_INVALID,
                        f"{visual_path}.screens[{index}].properties.description",
                        "Screen description must be a string when present.",
                        severity="error",
                    )
                if "links" in properties:
                    links = properties["links"]
                    if not isinstance(links, list):
                        self._record(
                            IntegrityCategory.CORRUPTED_OR_INVALID,
                            f"{visual_path}.screens[{index}].properties.links",
                            "Screen links must be a list when present.",
                            severity="error",
                        )
                    else:
                        for link_index, link in enumerate(links):
                            link_path = (
                                f"{visual_path}.screens[{index}].properties.links[{link_index}]"
                            )
                            if (
                                not isinstance(link, dict)
                                or not isinstance(link.get("label"), str)
                                or not isinstance(link.get("url"), str)
                                or not link["url"].strip()
                            ):
                                self._record(
                                    IntegrityCategory.CORRUPTED_OR_INVALID,
                                    link_path,
                                    "Link must have a string label and a non-empty string url.",
                                    severity="error",
                                )

        seen_connections: set[tuple[str, str]] = set()
        for index, connection in enumerate(connections):
            path = f"{visual_path}.connections[{index}]"
            if not isinstance(connection, dict):
                self._record(
                    IntegrityCategory.CORRUPTED_OR_INVALID,
                    path,
                    "Connection must be an object.",
                    severity="error",
                )
                continue
            source_id = connection.get("from", connection.get("source_id"))
            destination_id = connection.get("to", connection.get("destination_id"))
            if not isinstance(source_id, str) or source_id not in screen_ids:
                self._record(
                    IntegrityCategory.BROKEN_REFERENCE,
                    path,
                    f"Connection source {source_id!r} does not reference a screen ID in this document.",
                    severity="error",
                )
            if not isinstance(destination_id, str) or destination_id not in screen_ids:
                self._record(
                    IntegrityCategory.BROKEN_REFERENCE,
                    path,
                    f"Connection destination {destination_id!r} does not reference a screen ID in this document.",
                    severity="error",
                )
            if isinstance(source_id, str) and isinstance(destination_id, str):
                pair = (source_id, destination_id)
                if source_id == destination_id:
                    self._record(
                        IntegrityCategory.CORRUPTED_OR_INVALID,
                        path,
                        "A screen connection cannot point from a screen to itself.",
                        severity="error",
                    )
                elif pair in seen_connections:
                    self._record(
                        IntegrityCategory.CORRUPTED_OR_INVALID,
                        path,
                        "Duplicate directed connection endpoints are not allowed.",
                        severity="error",
                    )
                seen_connections.add(pair)
            for property_name in ("label", "condition"):
                if property_name in connection and not isinstance(connection[property_name], str):
                    self._record(
                        IntegrityCategory.CORRUPTED_OR_INVALID,
                        f"{path}.{property_name}",
                        f"Connection {property_name} must be a string when present.",
                        severity="error",
                    )
        entry_id = visual_design.get("entry_screen_id")
        if entry_id is not None and (
            not isinstance(entry_id, str) or entry_id not in screen_ids
        ):
            self._record(
                IntegrityCategory.BROKEN_REFERENCE,
                f"{visual_path}.entry_screen_id",
                f"Entry screen ID {entry_id!r} does not reference a screen in this document.",
                severity="error",
            )

    def _report(self) -> ProjectIntegrityReport:
        return ProjectIntegrityReport(
            project_root=self._report_root,
            inspected_at=datetime.now(timezone.utc).isoformat(),
            directories_examined=self._directories_examined,
            files_examined=self._files_examined,
            files_validated=self._files_validated,
            ok_count=self._ok_count,
            unvalidated_files=self._unvalidated_files,
            excluded_directories=list(self._excluded_directories),
            findings=list(self._findings),
            git=self._git,
        )

    @staticmethod
    def _is_reparse_point(file_stat: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(file_stat.st_mode) or bool(
            getattr(file_stat, "st_file_attributes", 0) & reparse_flag
        )

    @staticmethod
    def _path_key(relative: str | Path) -> str:
        value = str(relative).replace("\\", "/").strip("/")
        if not value:
            return "."
        return os.path.normcase(value).replace("\\", "/")

    def _record(
        self,
        category: IntegrityCategory,
        path: str,
        evidence: str,
        *,
        severity: Optional[str] = None,
        confidence: Optional[str] = None,
        source: str = "",
    ) -> None:
        defaults = {
            IntegrityCategory.MISSING: ("error", "definitive"),
            IntegrityCategory.UNREADABLE: ("error", "definitive"),
            IntegrityCategory.MALFORMED: ("error", "definitive"),
            IntegrityCategory.CORRUPTED_OR_INVALID: ("error", "definitive"),
            IntegrityCategory.BROKEN_REFERENCE: ("error", "definitive"),
            IntegrityCategory.SUSPICIOUS: ("warning", "suspicious"),
            IntegrityCategory.OK: ("info", "definitive"),
            IntegrityCategory.NOT_CHECKED: ("info", "not_checked"),
        }
        default_severity, default_confidence = defaults[category]
        finding = IntegrityFinding(
            path=path or ".",
            category=category,
            evidence=evidence,
            severity=severity or default_severity,
            confidence=confidence or default_confidence,
            source=source,
        )
        key = (finding.category.value, finding.path, finding.evidence, finding.source)
        if key not in self._finding_keys:
            self._finding_keys.add(key)
            self._findings.append(finding)

    def _inspect_repository_state(self) -> None:
        assert self._root is not None
        marker = self._root / ".git"
        try:
            marker_stat = marker.lstat()
        except FileNotFoundError:
            self._git = GitRepositoryEvidence(
                status="NOT_A_REPOSITORY",
                evidence="No .git marker exists at the selected project root; filesystem inspection was used.",
            )
            return
        except OSError as exc:
            self._git = GitRepositoryEvidence(
                status="UNAVAILABLE",
                evidence=f"Cannot inspect the selected root's .git marker: {type(exc).__name__}: {exc}",
            )
            return

        if self._is_reparse_point(marker_stat):
            self._git = GitRepositoryEvidence(
                status="NOT_CHECKED",
                evidence="The selected root's .git marker is a symlink/reparse point; Git metadata was not followed.",
            )
            return

        self._git_executable = shutil.which("git")
        if not self._git_executable:
            self._git = GitRepositoryEvidence(
                status="UNAVAILABLE",
                evidence="A .git marker exists, but the Git executable is unavailable.",
            )
            return

        top_result = self._run_git(["rev-parse", "--show-toplevel"])
        if top_result is None or top_result.returncode != 0:
            self._git = GitRepositoryEvidence(
                status="UNAVAILABLE",
                evidence="Git could not identify the selected project repository root.",
            )
            return
        try:
            git_root = Path(top_result.stdout.strip()).resolve(strict=True)
        except OSError:
            self._git = GitRepositoryEvidence(
                status="UNAVAILABLE",
                evidence="Git returned a repository root that could not be resolved.",
            )
            return
        if git_root != self._root:
            self._git = GitRepositoryEvidence(
                status="NOT_CHECKED",
                evidence=(
                    "Git reports a repository root outside the selected project boundary; "
                    "no parent repository status was inspected."
                ),
            )
            return

        status_result = self._run_git(
            ["status", "--porcelain=v1", "--untracked-files=normal", "--ignored=matching"]
        )
        if status_result is None or status_result.returncode != 0:
            self._git = GitRepositoryEvidence(
                status="UNAVAILABLE",
                evidence="Git repository metadata exists but read-only status inspection failed.",
            )
            return

        modified: list[str] = []
        deleted: list[str] = []
        untracked: list[str] = []
        ignored: list[str] = []
        for line in status_result.stdout.splitlines():
            if len(line) < 4:
                continue
            state, path = line[:2], line[3:]
            if state == "??":
                untracked.append(path)
            elif state == "!!":
                ignored.append(path.rstrip("/\\"))
            elif "D" in state:
                deleted.append(path)
                self._git_deleted_paths.add(self._path_key(path))
            elif state.strip():
                modified.append(path)
        self._git_ignored_paths = {
            self._path_key(path) for path in ignored if path
        }
        self._git = GitRepositoryEvidence(
            status="AVAILABLE",
            evidence=(
                "Read-only git status was collected for the selected repository root. "
                "It supports tracked-change reporting but does not prove project completeness."
            ),
            tracked_modified=modified,
            tracked_deleted=deleted,
            untracked=untracked,
            ignored=ignored,
        )

    def _run_git(self, arguments: list[str]) -> Optional[subprocess.CompletedProcess[str]]:
        if not self._git_executable or self._root is None:
            return None
        environment = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            environment.pop(key, None)
        environment.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        )
        try:
            return subprocess.run(
                [
                    self._git_executable,
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    f"core.excludesFile={os.devnull}",
                    "-C",
                    str(self._root),
                    *arguments,
                ],
                cwd=str(self._root),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _scan_project_tree(self) -> None:
        assert self._root is not None
        self._inventory[self._path_key(".")] = "directory"
        pending = [self._root]
        entries_seen = 0
        while pending:
            directory = pending.pop()
            directory_rel = self._relative(directory)
            self._directories_examined += 1
            if self._directories_examined > self.max_directories:
                self._scan_truncated = True
                self._record(
                    IntegrityCategory.NOT_CHECKED,
                    ".",
                    f"Directory scan limit ({self.max_directories}) reached; remaining paths were not examined.",
                )
                break
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(list(iterator), key=lambda entry: entry.name.casefold())
            except OSError as exc:
                if directory_rel != ".":
                    self._unreadable_directories.add(self._path_key(directory_rel))
                self._record(
                    IntegrityCategory.UNREADABLE,
                    directory_rel,
                    f"Cannot list directory: {type(exc).__name__}: {exc}",
                    severity="error",
                )
                continue

            for entry in entries:
                entries_seen += 1
                if entries_seen > self.max_files + self.max_directories:
                    self._scan_truncated = True
                    self._record(
                        IntegrityCategory.NOT_CHECKED,
                        ".",
                        "Entry scan safety limit reached; remaining paths were not examined.",
                    )
                    pending.clear()
                    break
                path = Path(entry.path)
                relative = self._relative(path)
                key = self._path_key(relative)
                try:
                    file_stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    self._record(
                        IntegrityCategory.MISSING,
                        relative,
                        "Path disappeared during the read-only directory scan.",
                        severity="error",
                    )
                    continue
                except OSError as exc:
                    self._record(
                        IntegrityCategory.UNREADABLE,
                        relative,
                        f"Cannot inspect path metadata: {type(exc).__name__}: {exc}",
                        severity="error",
                    )
                    continue

                if self._is_reparse_point(file_stat):
                    self._symlink_paths.add(key)
                    self._unvalidated_files += 1
                    self._inventory[key] = "link"
                    self._record(
                        IntegrityCategory.NOT_CHECKED,
                        relative,
                        "Symlink/reparse point was not followed to keep inspection within the selected project boundary.",
                        source="unvalidated-file",
                    )
                    continue

                if stat.S_ISDIR(file_stat.st_mode):
                    self._inventory[key] = "directory"
                    if self._is_generated_directory_name(entry.name):
                        self._excluded_paths.add(key)
                        # Git metadata is summarized separately below and is
                        # not part of the project's source-file completeness
                        # count.
                        if entry.name.casefold() != ".git":
                            self._excluded_directories.append(relative)
                        continue
                    if self._is_git_ignored(key):
                        self._excluded_paths.add(key)
                        self._excluded_directories.append(relative)
                        continue
                    pending.append(path)
                    continue

                if not stat.S_ISREG(file_stat.st_mode):
                    self._files_examined += 1
                    self._unvalidated_files += 1
                    self._inventory[key] = "special"
                    self._record(
                        IntegrityCategory.NOT_CHECKED,
                        relative,
                        "Special filesystem object was not opened or interpreted.",
                        source="unvalidated-file",
                    )
                    continue

                if key == ".git":
                    # A worktree may use a .git file that points at external
                    # repository metadata. Git is queried separately; do not
                    # read or treat that pointer file as project content.
                    self._excluded_paths.add(key)
                    continue

                if self._files_examined >= self.max_files:
                    self._scan_truncated = True
                    self._record(
                        IntegrityCategory.NOT_CHECKED,
                        ".",
                        f"File scan limit ({self.max_files}) reached; remaining paths were not examined.",
                    )
                    pending.clear()
                    break
                self._files_examined += 1
                self._inventory[key] = "file"
                if self._is_git_ignored(key):
                    self._unvalidated_files += 1
                    self._record(
                        IntegrityCategory.NOT_CHECKED,
                        relative,
                        "File is ignored by the selected repository; contents were not validated.",
                        source="unvalidated-file",
                    )
                    continue
                self._inspect_file(path, relative, file_stat.st_size)

    def _inspect_file(self, path: Path, relative: str, size: int) -> None:
        kind = self._validator_kind(path)
        if kind is None:
            try:
                with path.open("rb"):
                    pass
            except FileNotFoundError:
                self._record(
                    IntegrityCategory.MISSING,
                    relative,
                    "File disappeared before its readability check.",
                    severity="error",
                )
            except OSError as exc:
                self._record(
                    IntegrityCategory.UNREADABLE,
                    relative,
                    f"Cannot open file for reading: {type(exc).__name__}: {exc}",
                    severity="error",
                )
            else:
                self._unvalidated_files += 1
                self._record_not_checked_sample(
                    relative,
                    "File was readable, but its format has no configured safe validator; contents were not read.",
                )
            return

        if size > self.max_file_bytes:
            try:
                with path.open("rb"):
                    pass
            except OSError as exc:
                self._record(
                    IntegrityCategory.UNREADABLE,
                    relative,
                    f"Cannot open file for reading: {type(exc).__name__}: {exc}",
                    severity="error",
                )
            else:
                self._unvalidated_files += 1
                self._record(
                    IntegrityCategory.NOT_CHECKED,
                    relative,
                    f"File size ({size} bytes) exceeds the validation limit ({self.max_file_bytes}); contents were not read.",
                    source="unvalidated-file",
                )
            return

        try:
            with path.open("rb") as stream:
                content = stream.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            self._record(
                IntegrityCategory.MISSING,
                relative,
                "File disappeared before its format could be validated.",
                severity="error",
            )
            return
        except OSError as exc:
            self._record(
                IntegrityCategory.UNREADABLE,
                relative,
                f"Cannot read file: {type(exc).__name__}: {exc}",
                severity="error",
            )
            return

        self._files_validated += 1
        prior_invalids = sum(
            1
            for finding in self._findings
            if finding.path == relative
            and finding.category
            in {IntegrityCategory.MALFORMED, IntegrityCategory.CORRUPTED_OR_INVALID}
        )
        self._validate_content(path, relative, content)
        later_invalids = sum(
            1
            for finding in self._findings
            if finding.path == relative
            and finding.category
            in {IntegrityCategory.MALFORMED, IntegrityCategory.CORRUPTED_OR_INVALID}
        )
        if later_invalids == prior_invalids:
            self._ok_count += 1

    @staticmethod
    def _validator_kind(path: Path) -> Optional[str]:
        suffix = path.suffix.casefold()
        if suffix == ".json":
            return "json"
        if suffix == ".toml":
            return "toml"
        if suffix == ".py":
            return "python"
        if suffix == ".plist":
            return "plist"
        if suffix in ProjectIntegrityInspector._XML_SUFFIXES:
            return "xml"
        return None

    def _validate_content(self, path: Path, relative: str, content: bytes) -> None:
        kind = self._validator_kind(path)
        try:
            if kind == "json":
                data = json.loads(content.decode("utf-8-sig"))
                if path.name.casefold() in {
                    "package.json", "tsconfig.json", "jsconfig.json"
                } and not isinstance(data, dict):
                    self._record(
                        IntegrityCategory.CORRUPTED_OR_INVALID,
                        relative,
                        f"{path.name} parsed as JSON but its root value is not an object.",
                        severity="error",
                    )
                elif isinstance(data, dict):
                    self._inspect_json_references(path, relative, data)
            elif kind == "toml":
                data = tomllib.loads(content.decode("utf-8"))
                self._inspect_toml_references(path, relative, data)
            elif kind == "python":
                ast.parse(content, filename=relative)
            elif kind == "xml":
                element = ET.fromstring(content)
                self._inspect_xml_references(relative, element)
            elif kind == "plist":
                plistlib.loads(content)
        except json.JSONDecodeError as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                relative,
                f"JSON parser failed at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                severity="error",
            )
        except UnicodeDecodeError as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                relative,
                f"File is not valid UTF text for its declared format: {exc}",
                severity="error",
            )
        except tomllib.TOMLDecodeError as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                relative,
                f"TOML parser rejected the file: {exc}",
                severity="error",
            )
        except SyntaxError as exc:
            location = (
                f"line {exc.lineno}, column {exc.offset}"
                if exc.lineno is not None
                else "unknown location"
            )
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                relative,
                f"Python syntax parser failed at {location}: {exc.msg}",
                severity="error",
            )
        except ET.ParseError as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                relative,
                f"XML parser rejected the file: {exc}",
                severity="error",
            )
        except plistlib.InvalidFileException as exc:
            self._record(
                IntegrityCategory.MALFORMED,
                relative,
                f"Property-list parser rejected the file: {exc or 'invalid plist structure'}",
                severity="error",
            )
        except (ValueError, TypeError, KeyError) as exc:
            self._record(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                relative,
                f"Recognized project structure is invalid: {type(exc).__name__}: {exc}",
                severity="error",
            )

    def _invalid_configuration(self, path: str, location: str, expected: str) -> None:
        self._record(
            IntegrityCategory.CORRUPTED_OR_INVALID,
            path,
            f"{location} must be {expected}.",
            severity="error",
        )

    def _queue_reference(
        self,
        source: str,
        location: str,
        value: Any,
        *,
        kind: str = "file",
        implicit_node_resolution: bool = False,
    ) -> None:
        if not isinstance(value, str):
            self._invalid_configuration(source, location, "a path string")
            return
        if not value.strip():
            self._invalid_configuration(source, location, "a non-empty path string")
            return
        self._references.append(
            _FileReference(
                source=source,
                location=location,
                value=value,
                kind=kind,
                implicit_node_resolution=implicit_node_resolution,
            )
        )

    def _inspect_json_references(
        self, path: Path, relative: str, data: dict[str, Any]
    ) -> None:
        basename = path.name.casefold()
        if basename == "package.json":
            for key in (
                "scripts",
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            ):
                value = data.get(key)
                if key in data and not isinstance(value, dict):
                    self._invalid_configuration(relative, key, "an object")
                elif key == "scripts" and isinstance(value, dict):
                    for script_name, script_value in value.items():
                        if not isinstance(script_value, str):
                            self._invalid_configuration(
                                relative,
                                f"scripts.{script_name}",
                                "a string",
                            )
            for key in ("main", "module", "types", "typings"):
                if key in data:
                    self._queue_reference(
                        relative,
                        key,
                        data[key],
                        implicit_node_resolution=key in {"main", "module"},
                    )
            browser = data.get("browser")
            if isinstance(browser, str):
                self._queue_reference(relative, "browser", browser)
            elif browser is not None and not isinstance(browser, dict):
                self._invalid_configuration(relative, "browser", "a string or object")
            elif isinstance(browser, dict):
                for key, value in browser.items():
                    if isinstance(value, str) and value.startswith("."):
                        self._queue_reference(relative, f"browser.{key}", value)
            bins = data.get("bin")
            if isinstance(bins, str):
                self._queue_reference(relative, "bin", bins, implicit_node_resolution=True)
            elif isinstance(bins, dict):
                for name, value in bins.items():
                    self._queue_reference(relative, f"bin.{name}", value)
            elif bins is not None:
                self._invalid_configuration(relative, "bin", "a string or object")
            self._queue_export_paths(relative, "exports", data.get("exports"))
            self._queue_export_paths(relative, "imports", data.get("imports"))

        if basename in {"tsconfig.json", "jsconfig.json"}:
            extends = data.get("extends")
            extends_values = extends if isinstance(extends, list) else [extends]
            for index, value in enumerate(extends_values):
                if value is None:
                    continue
                if isinstance(value, str) and value.startswith(("./", "../", ".\\", "..\\")):
                    self._queue_reference(relative, f"extends[{index}]", value)
                elif isinstance(value, str):
                    self._record(
                        IntegrityCategory.NOT_CHECKED,
                        relative,
                        f"{relative}: extends[{index}] uses an external/package configuration reference ({value!r}); it was not resolved.",
                        source=relative,
                    )
                else:
                    self._invalid_configuration(relative, f"extends[{index}]", "a string")
            references = data.get("references", [])
            if not isinstance(references, list):
                self._invalid_configuration(relative, "references", "a list")
            else:
                for index, reference in enumerate(references):
                    if not isinstance(reference, dict) or "path" not in reference:
                        self._invalid_configuration(
                            relative, f"references[{index}]", "an object with a path string"
                        )
                    else:
                        self._queue_reference(
                            relative,
                            f"references[{index}].path",
                            reference["path"],
                            kind="directory_or_tsconfig",
                        )
            files = data.get("files", [])
            if not isinstance(files, list):
                self._invalid_configuration(relative, "files", "a list")
            else:
                for index, value in enumerate(files):
                    self._queue_reference(relative, f"files[{index}]", value)
            includes = data.get("include", [])
            if not isinstance(includes, list):
                self._invalid_configuration(relative, "include", "a list")
            else:
                for index, value in enumerate(includes):
                    if isinstance(value, str) and any(c in value for c in "*?["):
                        self._record(
                            IntegrityCategory.NOT_CHECKED,
                            relative,
                            f"{relative}: include[{index}] is a glob pattern and was not used to infer missing files.",
                            source=relative,
                        )
                    else:
                        self._queue_reference(
                            relative, f"include[{index}]", value, kind="file_or_directory"
                        )

    def _queue_export_paths(self, source: str, location: str, value: Any) -> None:
        if isinstance(value, str):
            if value.startswith("."):
                self._queue_reference(source, location, value, implicit_node_resolution=True)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                self._queue_export_paths(source, f"{location}.{key}", child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._queue_export_paths(source, f"{location}[{index}]", child)

    def _inspect_toml_references(
        self, path: Path, relative: str, data: dict[str, Any]
    ) -> None:
        basename = path.name.casefold()
        if basename == "pyproject.toml":
            project = data.get("project", {})
            if not isinstance(project, dict):
                self._invalid_configuration(relative, "project", "a table")
            else:
                readme = project.get("readme")
                if isinstance(readme, str):
                    self._queue_reference(relative, "project.readme", readme)
                elif isinstance(readme, dict) and "file" in readme:
                    self._queue_reference(relative, "project.readme.file", readme["file"])
            tool = data.get("tool", {})
            if isinstance(tool, dict):
                setuptools = tool.get("setuptools", {})
                if isinstance(setuptools, dict):
                    package_dir = setuptools.get("package-dir", {})
                    if isinstance(package_dir, dict):
                        for package, directory in package_dir.items():
                            self._queue_reference(
                                relative,
                                f"tool.setuptools.package-dir.{package}",
                                directory,
                                kind="directory",
                            )
                    packages = setuptools.get("packages", {})
                    finder = packages.get("find", {}) if isinstance(packages, dict) else {}
                    if isinstance(finder, dict):
                        where = finder.get("where", [])
                        if isinstance(where, list):
                            for index, directory in enumerate(where):
                                self._queue_reference(
                                    relative,
                                    f"tool.setuptools.packages.find.where[{index}]",
                                    directory,
                                    kind="directory",
                                )
                pytest_config = tool.get("pytest", {})
                ini_options = (
                    pytest_config.get("ini_options", {})
                    if isinstance(pytest_config, dict)
                    else {}
                )
                if isinstance(ini_options, dict):
                    testpaths = ini_options.get("testpaths", [])
                    if isinstance(testpaths, list):
                        for index, testpath in enumerate(testpaths):
                            if isinstance(testpath, str) and any(c in testpath for c in "*?["):
                                self._record(
                                    IntegrityCategory.NOT_CHECKED,
                                    relative,
                                    f"{relative}: testpaths[{index}] is a glob and was not used to infer missing files.",
                                    source=relative,
                                )
                            else:
                                self._queue_reference(
                                    relative,
                                    f"tool.pytest.ini_options.testpaths[{index}]",
                                    testpath,
                                    kind="file_or_directory",
                                )
        elif basename == "cargo.toml":
            dependency_tables = [
                ("dependencies", data.get("dependencies", {})),
                ("dev-dependencies", data.get("dev-dependencies", {})),
                ("build-dependencies", data.get("build-dependencies", {})),
            ]
            workspace = data.get("workspace", {})
            if isinstance(workspace, dict):
                dependency_tables.append(
                    ("workspace.dependencies", workspace.get("dependencies", {}))
                )
            target_tables = data.get("target", {})
            if isinstance(target_tables, dict):
                for target_name, target_config in target_tables.items():
                    if isinstance(target_config, dict):
                        for dep_key in ("dependencies", "dev-dependencies", "build-dependencies"):
                            dependency_tables.append(
                                (
                                    f"target.{target_name}.{dep_key}",
                                    target_config.get(dep_key, {}),
                                )
                            )
            for table_name, table in dependency_tables:
                if not isinstance(table, dict):
                    continue
                for dependency, config in table.items():
                    if isinstance(config, dict) and "path" in config:
                        self._queue_reference(
                            relative,
                            f"{table_name}.{dependency}.path",
                            config["path"],
                            kind="directory",
                        )

    def _inspect_xml_references(self, relative: str, root_element: ET.Element) -> None:
        for element in root_element.iter():
            tag = element.tag.rsplit("}", 1)[-1].casefold()
            if tag == "projectreference" and "include" in element.attrib:
                self._queue_reference(
                    relative, "ProjectReference Include", element.attrib["include"]
                )
            elif tag == "import" and "project" in element.attrib:
                self._queue_reference(
                    relative, "Import Project", element.attrib["project"]
                )

    def _check_references(self) -> None:
        for reference in self._references:
            self._check_reference(reference)

    def _check_reference(self, reference: _FileReference) -> None:
        raw = reference.value.strip()
        if not raw:
            self._reference_finding(
                IntegrityCategory.CORRUPTED_OR_INVALID,
                reference,
                "The reference path is empty.",
                severity="error",
            )
            return
        if any(token in raw for token in ("$", "%")):
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The reference uses an environment/build variable ({raw!r}); it was not expanded or executed.",
            )
            return
        if any(token in raw for token in "*?["):
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The reference is a glob pattern ({raw!r}); no files were inferred missing from it.",
            )
            return

        # URL or package references are not local file paths. Avoid probing them.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw) or raw.startswith("#"):
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The reference is external ({raw!r}) and was not accessed.",
            )
            return

        assert self._root is not None
        source_parent = self._root / Path(reference.source).parent
        raw_path = Path(raw)
        target = raw_path if raw_path.is_absolute() else source_parent / raw_path
        target = Path(os.path.abspath(str(target)))
        try:
            relative_path = target.relative_to(self._root)
        except ValueError:
            self._reference_finding(
                IntegrityCategory.SUSPICIOUS,
                reference,
                f"The configured path {raw!r} is outside the selected project root; the target was not inspected.",
                severity="warning",
                confidence="suspicious",
            )
            return

        relative = relative_path.as_posix()
        key = self._path_key(relative)
        prefixes = self._prefix_keys(relative)
        if any(prefix in self._symlink_paths for prefix in prefixes):
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The reference {raw!r} passes through a symlink/reparse point that was not followed.",
                finding_path=relative,
            )
            return
        excluded_prefix = next(
            (prefix for prefix in prefixes if prefix in self._excluded_paths), None
        )
        if excluded_prefix:
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The reference {raw!r} is under an excluded generated/dependency directory ({excluded_prefix}).",
                finding_path=relative,
            )
            return
        if any(prefix in self._unreadable_directories for prefix in prefixes):
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The parent directory for reference {raw!r} could not be listed.",
                finding_path=relative,
            )
            return
        if self._matches_reference(relative, reference):
            return
        if self._scan_truncated:
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The scan reached a safety limit before reference {raw!r} could be checked.",
                finding_path=relative,
            )
            return
        if self._is_generated_relative(relative):
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The absent reference {raw!r} is under a commonly generated/dependency directory; absence is not treated as damage.",
                finding_path=relative,
            )
            return
        if key in self._git_deleted_paths:
            self._reference_finding(
                IntegrityCategory.BROKEN_REFERENCE,
                reference,
                f"The configured reference {raw!r} is missing; Git also records it as tracked/deleted.",
                severity="error",
                finding_path=relative,
            )
            return
        tracked_state = self._git_is_tracked(relative)
        if tracked_state is True:
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The Git index lists {raw!r}, but the working-tree scan did not find it; sparse/skip-worktree state was not inferred.",
                finding_path=relative,
            )
            return
        if tracked_state is None and self._git.status == "AVAILABLE":
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"Could not determine whether absent reference {raw!r} is tracked; no missing-file claim was made.",
                finding_path=relative,
            )
            return
        ignored = self._git_check_ignore(relative)
        if ignored is True:
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"The absent reference {raw!r} is ignored by this project’s Git rules; it was not declared missing.",
                finding_path=relative,
            )
            return
        if ignored is None and self._git.status == "AVAILABLE":
            self._reference_finding(
                IntegrityCategory.NOT_CHECKED,
                reference,
                f"Could not determine whether absent reference {raw!r} is ignored; no missing-file claim was made.",
                finding_path=relative,
            )
            return
        self._reference_finding(
            IntegrityCategory.BROKEN_REFERENCE,
            reference,
            f"Configured reference {raw!r} does not exist under the selected project root.",
            severity="error",
            finding_path=relative,
        )

    def _matches_reference(self, relative: str, reference: _FileReference) -> bool:
        key = self._path_key(relative)
        kind = self._inventory.get(key)
        if reference.kind == "directory":
            return kind == "directory"
        if reference.kind == "directory_or_tsconfig":
            if kind == "directory":
                return True
            return self._inventory.get(self._path_key(f"{relative.rstrip('/')}/tsconfig.json")) == "file"
        if reference.kind == "file_or_directory" and kind in {"file", "directory"}:
            return True
        if kind == "file":
            return True
        if reference.implicit_node_resolution and not Path(relative).suffix:
            candidates = [relative + ext for ext in self._NODE_EXTENSIONS]
            candidates.extend(
                f"{relative.rstrip('/')}/index{ext}" for ext in self._NODE_EXTENSIONS
            )
            candidates.append(f"{relative.rstrip('/')}/package.json")
            return any(self._inventory.get(self._path_key(item)) == "file" for item in candidates)
        if reference.implicit_node_resolution and kind == "directory":
            return any(
                self._inventory.get(self._path_key(f"{relative.rstrip('/')}/index{ext}")) == "file"
                for ext in self._NODE_EXTENSIONS
            ) or self._inventory.get(self._path_key(f"{relative.rstrip('/')}/package.json")) == "file"
        return False

    def _reference_finding(
        self,
        category: IntegrityCategory,
        reference: _FileReference,
        evidence: str,
        *,
        severity: Optional[str] = None,
        confidence: Optional[str] = None,
        finding_path: Optional[str] = None,
    ) -> None:
        self._record(
            category,
            finding_path or reference.source,
            f"{reference.location}: {evidence}",
            severity=severity,
            confidence=confidence,
            source=reference.source,
        )

    def _git_check_ignore(self, relative: str) -> Optional[bool]:
        if self._git.status != "AVAILABLE":
            return False if self._git.status == "NOT_A_REPOSITORY" else None
        result = self._run_git(["check-ignore", "--no-index", "--quiet", "--", relative])
        if result is None:
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def _git_is_tracked(self, relative: str) -> Optional[bool]:
        if self._git.status != "AVAILABLE":
            return None
        result = self._run_git(
            ["ls-files", "--error-unmatch", "--", relative]
        )
        if result is None:
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def _add_git_deletions(self) -> None:
        for path in self._git.tracked_deleted:
            self._record(
                IntegrityCategory.MISSING,
                path,
                "Git read-only status reports this tracked path as deleted from the working tree.",
                severity="error",
            )

    def _record_not_checked_sample(self, relative: str, evidence: str) -> None:
        self._unvalidated_files += 1
        sample_count = sum(
            1
            for finding in self._findings
            if finding.category == IntegrityCategory.NOT_CHECKED
            and finding.source == "unvalidated-file"
        )
        if sample_count < self.MAX_NOT_CHECKED_SAMPLES:
            self._record(
                IntegrityCategory.NOT_CHECKED,
                relative,
                evidence,
                source="unvalidated-file",
            )

    def _relative(self, path: Path) -> str:
        if self._root is None:
            return "."
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            return "."
        return relative.as_posix() or "."

    def _prefix_keys(self, relative: str) -> list[str]:
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        return [self._path_key("/".join(parts[:index])) for index in range(1, len(parts) + 1)]

    def _is_generated_relative(self, relative: str) -> bool:
        return any(
            self._is_generated_directory_name(part)
            for part in relative.replace("\\", "/").split("/")
        )

    @classmethod
    def _is_generated_directory_name(cls, name: str) -> bool:
        """Return whether a directory name denotes generated build/runtime output.

        This is the single classification rule for generated output. Both the
        read-only tree scan (which prunes the whole subtree) and
        :meth:`_is_generated_relative` (which keeps absent-reference handling
        consistent) call it, so the two cannot drift apart.
        """
        folded = name.casefold()
        return folded in cls._GENERATED_DIRECTORIES or folded.startswith(
            cls._GENERATED_DIRECTORY_PREFIXES
        )

    def _is_git_ignored(self, key: str) -> bool:
        return any(
            key == prefix or key.startswith(prefix + "/")
            for prefix in self._git_ignored_paths
        )

def _render_integrity_report(
    report: ProjectIntegrityReport,
    *,
    findings_limit: int | None,
    paths_limit: int | None,
) -> str:
    """Render the report text, optionally capping per-section line counts.

    ``findings_limit``/``paths_limit`` of ``None`` render every entry, which is
    what the Copy/Save actions use so the exported text is never truncated.
    The on-screen widget passes explicit limits to keep the visible report
    bounded.
    """
    counts = report.counts
    lines = [
        "PROJECT INTEGRITY INSPECTION",
        f"Project: {report.project_root}",
        f"Inspected: {report.inspected_at}",
        f"Result: {report.status}",
        "",
        f"Directories examined: {report.directories_examined}",
        f"Files examined: {report.files_examined}",
        f"Files format-validated: {report.files_validated}",
        f"OK: {counts[IntegrityCategory.OK.value]}",
        f"Missing: {counts[IntegrityCategory.MISSING.value]}",
        f"Unreadable: {counts[IntegrityCategory.UNREADABLE.value]}",
        f"Malformed: {counts[IntegrityCategory.MALFORMED.value]}",
        f"Invalid: {counts[IntegrityCategory.CORRUPTED_OR_INVALID.value]}",
        f"Broken references: {counts[IntegrityCategory.BROKEN_REFERENCE.value]}",
        f"Suspicious: {counts[IntegrityCategory.SUSPICIOUS.value]}",
        f"Not checked: {counts[IntegrityCategory.NOT_CHECKED.value]}",
        f"Generated/environment directories skipped: {len(report.excluded_directories)}",
        "",
        f"Git metadata: {report.git.status} — {report.git.evidence}",
        f"Git tracked modified: {len(report.git.tracked_modified)}; "
        f"tracked deleted: {len(report.git.tracked_deleted)}; "
        f"untracked: {len(report.git.untracked)}",
    ]
    if paths_limit is None:
        tracked_modified = list(report.git.tracked_modified)
        untracked = list(report.git.untracked)
        excluded = list(report.excluded_directories)
    else:
        tracked_modified = list(report.git.tracked_modified[:paths_limit])
        untracked = list(report.git.untracked[:paths_limit])
        excluded = list(report.excluded_directories[:paths_limit])
    if findings_limit is None:
        findings = list(report.findings)
    else:
        findings = list(report.findings[:findings_limit])
    if tracked_modified:
        lines.append("Tracked modifications (Git evidence only):")
        lines.extend(f"  {path}" for path in tracked_modified)
    if untracked:
        lines.append("Untracked files (not treated as missing):")
        lines.extend(f"  {path}" for path in untracked)
    if excluded:
        lines.append("Skipped generated/dependency/cache directories:")
        lines.extend(f"  {path}" for path in excluded)
    if findings:
        lines.extend(["", "Findings:"])
        lines.extend(
            f"[{finding.category.value}] {finding.path} — {finding.evidence} "
            f"(severity: {finding.severity}; confidence: {finding.confidence})"
            for finding in findings
        )
        if findings_limit is not None and len(report.findings) > findings_limit:
            lines.append(
                f"Additional findings omitted from display: {len(report.findings) - findings_limit}"
            )
    else:
        lines.extend(["", "No integrity problems were detected in the checked files."])
    lines.extend(
        [
            "",
            "Scope note: unknown formats and skipped generated/environment content are not certified intact.",
            "Git state is supporting evidence and is not proof that a transferred project is complete.",
        ]
    )
    return "\n".join(lines)


def format_integrity_report(report: ProjectIntegrityReport) -> str:
    """Render the bounded report shown in the Integrity widget."""
    return _render_integrity_report(report, findings_limit=500, paths_limit=100)


def format_full_integrity_report(report: ProjectIntegrityReport) -> str:
    """Render the complete, untruncated report for Copy/Save export."""
    return _render_integrity_report(report, findings_limit=None, paths_limit=None)


__all__ = [
    "GitRepositoryEvidence",
    "IntegrityCategory",
    "IntegrityFinding",
    "ProjectIntegrityInspector",
    "ProjectIntegrityReport",
    "format_integrity_report",
    "format_full_integrity_report",
]
