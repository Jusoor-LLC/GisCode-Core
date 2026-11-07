import os
from pathlib import Path
from typing import Optional


class Workspace:
    def __init__(
        self,
        root_path: Optional[Path] = None,
        *,
        ensure_structure: bool = True,
    ):
        if root_path is None:
            root_path = Path.cwd() / "workspace"
        self.root_path = Path(root_path).expanduser().resolve()
        self.root_path.mkdir(parents=True, exist_ok=True)
        if ensure_structure:
            self._ensure_structure()

    def _ensure_structure(self) -> None:
        dirs = ["src", "tests", "docs", "build", "logs"]
        for d in dirs:
            (self.root_path / d).mkdir(exist_ok=True)

    def path(self, *parts: str) -> Path:
        return self.root_path.joinpath(*parts)

    def exists(self, *parts: str) -> bool:
        return self.path(*parts).exists()

    def write(self, *parts: str, content: str) -> Path:
        target = self.path(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read(self, *parts: str) -> str:
        return self.path(*parts).read_text(encoding="utf-8")

    def __repr__(self) -> str:
        return f"Workspace(root_path={self.root_path!s})"
