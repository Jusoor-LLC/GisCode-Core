"""End-to-end proof that the Planner panel can really read a real image.

Unlike ``tests/test_planer_image_attachments.py`` (which uses a spy to
prove the handoff), this test drives the actual local vision model.
It is skipped automatically when no vision-capable model or runtime is
available, so it never silently passes on a machine that cannot run it.

It is also opt-in: it loads a real, GB-scale model and needs free VRAM/RAM, so
by default it skips with a reason and runs only under
``GISCODE_RUN_REAL_MODEL_TESTS=1``. When the model is present but the machine
cannot fit it, it skips rather than fails - that is an environment fact, not a
defect. Any other failure still fails.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from giscode.config.settings import SettingsManager
from giscode.inference.api import InferenceAPI
from giscode.models.manager import ModelManager
from giscode.planer import PlanerAI, PlanerEngine
from giscode.projects import ProjectStore
from giscode.planer.chat import PlanerChatPersistence
from giscode.projects.workspace import ProjectWorkspaceSession
from giscode.projects import Project
from giscode.storage import ChatStore
from giscode.vision.capabilities import find_vision_model
from giscode.vision.images import describe_images
from giscode.vision.service import VisionService
from giscode.vision.types import VisionStatus


def _real_vision_service() -> VisionService:
    settings = SettingsManager()
    inference = InferenceAPI(n_ctx=4096, n_threads=4, n_gpu_layers=0)
    manager = ModelManager(settings=settings, inference=inference)
    # Populate the registry with the models actually installed on disk,
    # exactly as the application does at startup.
    manager.auto_register_discovered()
    return VisionService(manager, inference, keep_model_loaded=True)


RUN_REAL_MODEL_TESTS_ENV = "GISCODE_RUN_REAL_MODEL_TESTS"


def _real_model_tests_enabled() -> bool:
    """Real-model tests are opt-in: they load a GB-scale model and need VRAM."""
    return os.environ.get(RUN_REAL_MODEL_TESTS_ENV) == "1"


def _vision_model_present() -> bool:
    try:
        settings = SettingsManager()
        inference = InferenceAPI(n_ctx=4096, n_threads=4, n_gpu_layers=0)
        if not inference.available:
            return False
        manager = ModelManager(settings=settings, inference=inference)
        manager.auto_register_discovered()
        return find_vision_model(manager.get_registry()) is not None
    except Exception:
        return False


def _skip_reason() -> str:
    if not _real_model_tests_enabled():
        return (
            "real-model test: opt-in only, set "
            f"{RUN_REAL_MODEL_TESTS_ENV}=1 to run it (it loads a real vision model "
            "and needs free VRAM/RAM)"
        )
    if not _vision_model_present():
        return "no local vision-capable model/runtime available on this machine"
    return ""


_SKIP_REASON = _skip_reason()

requires_vision = pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "")


def _skip_if_resources_are_missing(result) -> None:
    """Turn a budget refusal into a skip; everything else still fails.

    ``VisionService`` reports a model that cannot fit as a failed description
    whose error mentions insufficient resources. Only that one case is a skip:
    a crash, a wrong answer or a missing image all keep failing the test.
    """
    error = getattr(result, "error", "") or ""
    if getattr(result, "status", None) is not VisionStatus.COMPLETE and (
        "insufficient resources" in error.lower()
    ):
        pytest.skip(f"not enough free VRAM/RAM for the real vision model: {error}")


def _make_image(path: Path, colour, size=(256, 256), text=None) -> Path:
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, colour)
    if text:
        draw = ImageDraw.Draw(image)
        draw.text((10, 10), text, fill=(0, 0, 0))
    image.save(path)
    return path


@requires_vision
class TestRealLocalImageUnderstanding:
    """The full Planner path against the real local vision model."""

    def test_planner_really_reads_an_image(self, tmp_path):
        from giscode.inference.runtime import ChatResponse

        red = _make_image(tmp_path / "red.png", (220, 20, 20))
        blue = _make_image(tmp_path / "blue.png", (20, 20, 220))
        service = _real_vision_service()

        # --- the image is really described by the local model ---------
        red_description = describe_images(
            [str(red)], service, prompt="What single colour fills this image? Answer with one word."
        )
        _skip_if_resources_are_missing(red_description)
        assert red_description.status is VisionStatus.COMPLETE, red_description.error
        assert red_description.interpreted is True
        assert "red" in red_description.text.lower(), red_description.text

        blue_description = describe_images(
            [str(blue)], service, prompt="What single colour fills this image? Answer with one word."
        )
        _skip_if_resources_are_missing(blue_description)
        assert "blue" in blue_description.text.lower(), blue_description.text

        # --- and the observation really reaches the planner prompt ----
        captured: list[str] = []

        class _CapturingInference:
            def chat_generate(self, request):
                blob = json.dumps(
                    [getattr(m, "content", "") for m in request.messages], default=str
                )
                captured.append(blob)
                return ChatResponse(
                    text=json.dumps({"assistant_message": "The image is red."}),
                    request_id="real-test",
                )

        store = ProjectStore(db_path=str(tmp_path / "p.db"))
        project = Project.create("Real", str(tmp_path / "demo"))
        store.create_project(project)
        chat_store = ChatStore(tmp_path / "chat.db")
        engine = PlanerEngine(project_id=project.id)
        ai = PlanerAI(engine=engine, inference=_CapturingInference())
        session = ProjectWorkspaceSession(
            project=project,
            project_store=store,
            chat_store=chat_store,
            engine=engine,
            ai=ai,
            chat=PlanerChatPersistence(project=project, chat_store=chat_store),
        )

        result = session.chat_with_planer(
            "What colour is shown in this picture?",
            attachments=[str(red)],
            vision_service=service,
        )

        assert result.success is True, result.parse_error or result.inference_error
        assert result.image_status == "interpreted"
        assert result.image_model
        assert "red" in result.image_report.lower()
        # The planner prompt genuinely carried the model's observation.
        assert captured, "the planner was never called"
        assert "red" in captured[0].lower()
        # The stored turn records which image it referred to.
        user_turn = next(
            t for t in session.planer_chat.load_history() if t.role == "user"
        )
        assert user_turn.meta["attachments"][0]["name"] == "red.png"

    def test_real_model_reads_text_rendered_in_an_image(self, tmp_path):
        """A vision model should be able to report visible text."""
        image = _make_image(
            tmp_path / "label.png", (255, 255, 255), size=(320, 120), text="HELLO"
        )
        service = _real_vision_service()
        description = describe_images(
            [str(image)],
            service,
            prompt="Read any text visible in this image. Reply with the text only.",
        )
        _skip_if_resources_are_missing(description)
        assert description.status is VisionStatus.COMPLETE, description.error
        assert "hello" in description.text.lower(), description.text
