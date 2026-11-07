"""Image loading and honest image description shared by the GUI panels.

This module separates the four states the rest of GisCode needs to keep
distinct, and never blurs them:

``Attached``
    A path was chosen by the user and the GUI is showing it. Nothing has
    been read from disk yet.

``Loaded``
    :func:`load_image` proved the file exists, has a supported image
    extension, and genuinely decodes as an image (it is opened and read
    by Pillow, so a renamed text file is rejected here, not later).

``Processed``
    :func:`describe_images` ran the image through
    :class:`giscode.vision.service.VisionService`, which found a
    vision-capable model, loaded it (with its ``*.mmproj`` projector),
    and asked it about the picture.

``Interpreted``
    The vision model returned real text about the image's actual
    contents. This is only ever true when a real model produced it.

Every failure is reported through a real exception or an explicit
status. Nothing here fabricates a description, and no path silently
downgrades an image to text.
"""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence

from giscode.vision.types import VisionRequest, VisionResult, VisionStatus


class ImageStage(str, Enum):
    """How far an attached image actually got.

    The stages are ordered and must not be collapsed. ``ATTACHED`` only
    means the GUI accepted a path; ``INTERPRETED`` is the only stage
    that means a real model described the actual picture.
    """

    NONE = "none"
    ATTACHED = "attached"
    LOADED = "loaded"
    PROCESSED = "processed"
    INTERPRETED = "interpreted"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"



# Formats the runtime can actually hand to a vision model. Kept in step
# with ``giscode.vision.engine._SUPPORTED_SUFFIXES``.
SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
)

IMAGE_FILETYPES = (
    ("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
    ("All files", "*.*"),
)

# The default question asked about an attached picture. It asks for
# concrete, checkable content so a vision model cannot answer with a
# generic caption that would be indistinguishable from a guess.
DEFAULT_IMAGE_PROMPT = (
    "Describe what is actually visible in this image. Report concrete "
    "details: any text shown, user-interface elements, colours, "
    "diagrams, error messages, or layout. If the image is blank or "
    "unreadable, say so explicitly."
)


class ImageLoadError(Exception):
    """The file could not be loaded as an image."""


class ImageDescriptionError(Exception):
    """The image could not be described by a vision model."""


@dataclass
class LoadedImage:
    """A proven-loadable image file (the ``Loaded`` state)."""

    path: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    mode: str
    format: str
    suffix: str

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def dimensions(self) -> str:
        return f"{self.width}x{self.height}"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "format": self.format,
        }

    def summary(self) -> str:
        """One short factual line, e.g. ``shot.png (PNG, 448x448, 12.3 KB)``."""
        size_kb = self.size_bytes / 1024
        kind = self.suffix.lstrip(".").upper()
        return f"{self.name} ({kind}, {self.dimensions}, {size_kb:.1f} KB)"


@dataclass
class ImageDescription:
    """The outcome of running an image through the vision pipeline."""

    status: VisionStatus
    text: str = ""
    model_used: Optional[str] = None
    error: Optional[str] = None
    images: list[LoadedImage] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == VisionStatus.COMPLETE and bool(self.text.strip())

    @property
    def interpreted(self) -> bool:
        """True only when a real model returned real text about the image."""
        return self.ok


def guess_mime_type(path: str) -> str:
    """Best-effort MIME type, falling back to ``application/octet-stream``."""
    guessed = mimetypes.guess_type(path)[0]
    if guessed:
        return guessed
    suffix = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


def load_image(path: str) -> LoadedImage:
    """Read and prove ``path`` is a real image (the ``Loaded`` state).

    Raises :class:`ImageLoadError` with a user-readable reason for a
    missing path, a directory, an unsupported extension, a file that is
    not actually an image, or a truncated/corrupt file. It never
    returns a partially-validated result.
    """
    if path is None or not str(path).strip():
        raise ImageLoadError("No image path was given.")

    text = str(path)
    candidate = Path(text).expanduser()
    if not candidate.exists():
        raise ImageLoadError(f"Image file not found: {text}")
    if candidate.is_dir():
        raise ImageLoadError(f"Not a file (it is a folder): {text}")
    if not candidate.is_file():
        raise ImageLoadError(f"Not a readable file: {text}")

    suffix = candidate.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        supported = ", ".join(sorted(s.lstrip(".").upper() for s in SUPPORTED_IMAGE_SUFFIXES))
        raise ImageLoadError(
            f"Unsupported image format '{suffix or 'none'}'. Supported: {supported}."
        )

    try:
        from PIL import Image  # imported lazily: only needed when an image is used
    except ImportError as exc:  # pragma: no cover - Pillow is a hard requirement in practice
        raise ImageLoadError(
            "Pillow (PIL) is not installed, so image files cannot be read."
        ) from exc

    try:
        with Image.open(candidate) as probe:
            probe.verify()  # structural check; catches renamed non-images
        with Image.open(candidate) as probe:
            width, height = probe.size
            mode = probe.mode
            image_format = probe.format or suffix.lstrip(".").upper()
    except ImageLoadError:
        raise
    except Exception as exc:
        raise ImageLoadError(
            f"'{candidate.name}' could not be read as an image: {type(exc).__name__}: {exc}"
        ) from exc

    if width <= 0 or height <= 0:
        raise ImageLoadError(f"'{candidate.name}' reports an empty image ({width}x{height}).")

    return LoadedImage(
        path=str(candidate),
        mime_type=guess_mime_type(str(candidate)),
        size_bytes=candidate.stat().st_size,
        width=int(width),
        height=int(height),
        mode=str(mode),
        format=str(image_format),
        suffix=suffix,
    )


def load_images(paths: Iterable[str]) -> list[LoadedImage]:
    """Load every path, or raise for the first one that is not usable."""
    return [load_image(path) for path in (paths or [])]


def build_attachment_dicts(images: Sequence[LoadedImage]) -> list[dict]:
    """Attachment metadata for a chat message ``meta`` payload."""
    return [image.to_dict() for image in images]


def stage_for(description: "ImageDescription") -> ImageStage:
    """The stage a finished :class:`ImageDescription` represents."""
    if description.interpreted:
        return ImageStage.INTERPRETED
    if description.status == VisionStatus.UNAVAILABLE:
        return ImageStage.UNAVAILABLE
    return ImageStage.FAILED


def describe_images(
    image_paths: Sequence[str],
    vision_service,
    *,
    prompt: Optional[str] = None,
    request_id: str = "planer-image",
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
    max_tokens: int = 512,
) -> ImageDescription:
    """Run ``image_paths`` through the real vision pipeline.

    This is the ``Processed``/``Interpreted`` step. The images are
    loaded and proven readable first, then handed to
    :class:`~giscode.vision.service.VisionService`, which loads a
    vision-capable model on demand and returns its real description.

    Failures are surfaced honestly:

    * a bad file raises :class:`ImageLoadError`;
    * a missing vision model, a failed load, or a model error returns
      an :class:`ImageDescription` whose ``status`` is ``UNAVAILABLE``
      or ``FAILED`` with the underlying reason, and whose ``text`` is
      empty. The caller must show that reason rather than continuing
      as though the image had been read.
    """
    if vision_service is None:
        raise ImageDescriptionError(
            "No vision service is available, so the image cannot be read. "
            "The image was NOT described, and its contents were not guessed."
        )

    loaded = load_images(image_paths)
    if not loaded:
        raise ImageLoadError("No image was attached.")

    result: VisionResult = vision_service.analyze(
        VisionRequest(
            image_paths=[image.path for image in loaded],
            prompt=prompt or DEFAULT_IMAGE_PROMPT,
            request_id=request_id,
            project_id=project_id,
            task_id=task_id,
            max_tokens=max_tokens,
        )
    )

    status = result.status
    if status == VisionStatus.COMPLETE and not (result.text or "").strip():
        status = VisionStatus.FAILED

    return ImageDescription(
        status=status,
        text=(result.text or "").strip(),
        model_used=result.model_used,
        error=result.error,
        images=loaded,
        elapsed=result.elapsed,
    )


def format_observation(description: ImageDescription) -> str:
    """Render a description as planner-readable context.

    Returns an empty string when nothing was genuinely interpreted, so a
    caller can never accidentally present a failure as an observation.
    """
    if not description.interpreted:
        return ""
    lines = []
    for image in description.images:
        lines.append(f"- {image.name} ({image.format}, {image.dimensions})")
    body = "\n".join(lines)
    return (
        "The user attached an image. A local vision model examined it and "
        f"reported (model: {description.model_used or 'unknown'}):\n"
        f"{body}\n"
        f"{description.text}\n"
        "Use these observed details when answering. The description above "
        "came from the actual pixels of the attached image."
    )
