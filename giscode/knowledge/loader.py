from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from giscode.knowledge.errors import ValidationError, UnsupportedFormatError


SUPPORTED_EXTENSIONS = {
    ".txt": "text",
    ".md": "markdown",
    ".py": "python",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass
class LoadedDocument:
    source_path: str
    source_type: str
    content: str
    content_hash: str
    metadata: dict = field(default_factory=dict)


def _normalize_content(content: str) -> str:
    """Normalize document content deterministically."""
    if content.startswith("\ufeff"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return content


def _compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_document(file_path: Path) -> LoadedDocument:
    """Load a document from the filesystem."""
    if not file_path.exists():
        raise ValidationError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise ValidationError(f"Path is not a file: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"Unsupported file format: {suffix}")

    try:
        raw_content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValidationError(f"File is not valid UTF-8: {file_path}") from e

    source_type = SUPPORTED_EXTENSIONS[suffix]
    normalized = _normalize_content(raw_content)
    content_hash = _compute_content_hash(normalized)

    return LoadedDocument(
        source_path=str(file_path),
        source_type=source_type,
        content=normalized,
        content_hash=content_hash,
        metadata={
            "extension": suffix,
            "original_size": len(raw_content),
            "normalized_size": len(normalized),
        },
    )


def get_supported_extensions() -> list[str]:
    return sorted(SUPPORTED_EXTENSIONS.keys())