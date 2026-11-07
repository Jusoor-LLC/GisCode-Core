"""Tests for image attachments in the lower project/chat Planner panel.

These tests cover the four states the workflow must keep distinct:

``Attached``
    The GUI accepted a path.
``Loaded``
    The file was really decoded and proven to be an image.
``Processed`` / ``Interpreted``
    A real vision model was consulted and returned real text.

The unit tests use a *spy* ``VisionService`` only to prove that the
handoff happens and that failures propagate honestly. A spy never
invents a description: it either returns a canned real-model response
or an explicit failure. Genuine interpretation is proven separately by
``tests/test_vision_real_local.py`` against the real local model.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from giscode.inference.runtime import ChatResponse
from giscode.planer import PlanerAI, PlanerEngine, ProjectPlan
from giscode.planer.ai import PlanerAIResult
from giscode.planer.chat import PlanerChatPersistence
from giscode.projects import Project, ProjectLifecycle, ProjectStore
from giscode.projects.workspace import ProjectWorkspaceSession
from giscode.storage import ChatStore
from giscode.vision.images import (
    ImageDescriptionError,
    ImageLoadError,
    ImageStage,
    describe_images,
    format_observation,
    load_image,
    load_images,
    stage_for,
)
from giscode.vision.types import VisionRequest, VisionResult, VisionStatus
from tests.conftest import requires_gisai


# ----------------------------------------------------------------- helpers


def make_image(path: Path, colour=(200, 30, 30), size=(64, 48)) -> Path:
    """Write a real, decodable PNG to ``path``."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def write_text_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def truncate(path: Path) -> Path:
    """Corrupt a real image by cutting off its tail."""
    data = path.read_bytes()
    path.write_bytes(data[: max(1, len(data) // 3)])
    return path


class SpyVisionService:
    """Records what it was asked to read.

    ``response`` is returned verbatim, so a test can prove the honest
    failure paths. It is never used to claim a real model ran; that is
    covered by the real-model test.
    """

    def __init__(self, response: VisionResult) -> None:
        self.response = response
        self.requests: list[VisionRequest] = []

    def analyze(self, request: VisionRequest) -> VisionResult:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def complete_result(text: str, model: str = "Qwen3-VL-4B-Instruct") -> VisionResult:
    return VisionResult(
        status=VisionStatus.COMPLETE,
        request_id="test-image",
        text=text,
        model_used=model,
    )


def unavailable_result(error: str) -> VisionResult:
    return VisionResult(
        status=VisionStatus.UNAVAILABLE,
        request_id="test-image",
        text="",
        error=error,
    )


# ----------------------------------------------------------------- Loaded


class TestImageLoading:
    """The ``Loaded`` state: prove the file really is an image."""

    def test_real_png_is_loaded(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        loaded = load_image(str(path))
        assert loaded.width == 64
        assert loaded.height == 48
        assert loaded.format == "PNG"
        assert loaded.suffix == ".png"
        assert loaded.mime_type == "image/png"
        assert loaded.size_bytes == path.stat().st_size
        assert loaded.name == "shot.png"
        assert "shot.png" in loaded.summary()
        assert "64x48" in loaded.summary()

    @pytest.mark.parametrize("suffix", [".jpg", ".jpeg", ".webp", ".bmp", ".gif"])
    def test_every_advertised_format_loads(self, tmp_path, suffix):
        path = make_image(tmp_path / f"img{suffix}")
        assert load_image(str(path)).suffix == suffix

    def test_missing_file_is_rejected(self, tmp_path):
        with pytest.raises(ImageLoadError, match="not found"):
            load_image(str(tmp_path / "nope.png"))

    def test_directory_is_rejected(self, tmp_path):
        folder = tmp_path / "folder.png"
        folder.mkdir()
        with pytest.raises(ImageLoadError, match="folder"):
            load_image(str(folder))

    def test_unsupported_extension_is_rejected(self, tmp_path):
        path = write_text_file(tmp_path / "notes.txt", "hello")
        with pytest.raises(ImageLoadError, match="Unsupported image format"):
            load_image(str(path))

    def test_renamed_text_file_is_rejected(self, tmp_path):
        """A .png that is really text must not pass as an image."""
        path = write_text_file(tmp_path / "fake.png", "this is not a png")
        with pytest.raises(ImageLoadError, match="could not be read as an image"):
            load_image(str(path))

    def test_truncated_image_is_rejected(self, tmp_path):
        path = truncate(make_image(tmp_path / "cut.png"))
        with pytest.raises(ImageLoadError):
            load_image(str(path))

    def test_empty_path_is_rejected(self):
        with pytest.raises(ImageLoadError, match="No image path"):
            load_image("")

    def test_load_images_collects_all(self, tmp_path):
        a = make_image(tmp_path / "a.png")
        b = make_image(tmp_path / "b.png", size=(10, 10))
        loaded = load_images([str(a), str(b)])
        assert [i.name for i in loaded] == ["a.png", "b.png"]

    def test_load_images_is_all_or_nothing(self, tmp_path):
        good = make_image(tmp_path / "good.png")
        with pytest.raises(ImageLoadError):
            load_images([str(good), str(tmp_path / "missing.png")])


# ----------------------------------------------------------------- Processed / Interpreted


class TestDescribeImages:
    """The ``Processed``/``Interpreted`` handoff into ``VisionService``."""

    def test_hands_paths_to_vision_service(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        spy = SpyVisionService(complete_result("a red rectangle"))
        description = describe_images([str(path)], spy)
        assert len(spy.requests) == 1
        assert spy.requests[0].image_paths == [str(path)]
        assert description.interpreted is True
        assert description.text == "a red rectangle"
        assert description.model_used == "Qwen3-VL-4B-Instruct"
        assert stage_for(description) is ImageStage.INTERPRETED

    def test_unavailable_is_not_interpreted(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        spy = SpyVisionService(unavailable_result("no vision-capable model found"))
        description = describe_images([str(path)], spy)
        assert description.interpreted is False
        assert description.text == ""
        assert description.error == "no vision-capable model found"
        assert stage_for(description) is ImageStage.UNAVAILABLE

    def test_complete_but_empty_is_treated_as_failure(self, tmp_path):
        """A model that says nothing has not interpreted anything."""
        path = make_image(tmp_path / "shot.png")
        spy = SpyVisionService(complete_result("   "))
        description = describe_images([str(path)], spy)
        assert description.status is VisionStatus.FAILED
        assert description.interpreted is False
        assert stage_for(description) is ImageStage.FAILED

    def test_missing_vision_service_raises(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        with pytest.raises(ImageDescriptionError, match="No vision service"):
            describe_images([str(path)], None)

    def test_bad_image_raises_before_calling_service(self, tmp_path):
        path = write_text_file(tmp_path / "fake.png", "not an image")
        spy = SpyVisionService(complete_result("unused"))
        with pytest.raises(ImageLoadError):
            describe_images([str(path)], spy)
        assert spy.requests == []


class TestFormatObservation:
    """Only a real description may become planner context."""

    def test_observation_names_model_and_text(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        spy = SpyVisionService(complete_result("a red rectangle"))
        text = format_observation(describe_images([str(path)], spy))
        assert "Qwen3-VL-4B-Instruct" in text
        assert "a red rectangle" in text
        assert "shot.png" in text

    def test_failure_produces_empty_observation(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        spy = SpyVisionService(unavailable_result("no model"))
        assert format_observation(describe_images([str(path)], spy)) == ""


# ----------------------------------------------------------------- PlanerAI


class _FakeInference:
    """Captures the prompt so a test can prove the observation reached it."""

    def __init__(self, reply: dict | None = None) -> None:
        self.prompts: list[list] = []
        self._reply = reply or {
            "assistant_message": "Thanks, I can see the image.",
        }

    def chat_generate(self, request):
        self.prompts.append(request.messages)
        return ChatResponse(text=json.dumps(self._reply), request_id="fake-1")


def _engine_and_ai(inference):
    plan = ProjectPlan(project_id="p1")
    engine = PlanerEngine(plan)
    return engine, PlanerAI(engine=engine, inference=inference)


class TestPlanerAIImageHandoff:
    """``PlanerAI.chat`` must really read the image and then plan."""

    def test_interpreted_image_reaches_planner_prompt(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        spy = SpyVisionService(complete_result("a red rectangle on white"))

        result = ai.chat("What does this show?", attachments=[str(path)], vision_service=spy)

        assert result.success is True
        assert result.image_status == ImageStage.INTERPRETED.value
        assert result.image_model == "Qwen3-VL-4B-Instruct"
        assert "a red rectangle on white" in result.image_report
        assert len(spy.requests) == 1
        prompt = json.dumps(inference.prompts[0], default=str)
        assert "a red rectangle on white" in prompt
        assert "shot.png" in prompt

    def test_unreadable_image_fails_honestly(self, tmp_path):
        path = write_text_file(tmp_path / "fake.png", "not an image")
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        spy = SpyVisionService(complete_result("should never be used"))

        result = ai.chat("What is this?", attachments=[str(path)], vision_service=spy)

        assert result.success is False
        assert result.image_status == ImageStage.FAILED.value
        assert "could not be read as an image" in result.image_error
        # The planner must NOT be asked to answer about a picture it never saw.
        assert inference.prompts == []
        assert spy.requests == []

    def test_no_vision_service_fails_instead_of_guessing(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)

        result = ai.chat("What is this?", attachments=[str(path)], vision_service=None)

        assert result.success is False
        assert result.image_status == ImageStage.UNAVAILABLE.value
        assert "no vision service" in result.image_error.lower()
        assert inference.prompts == []

    def test_unavailable_model_fails_instead_of_guessing(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        spy = SpyVisionService(unavailable_result("no vision-capable model found"))

        result = ai.chat("What is this?", attachments=[str(path)], vision_service=spy)

        assert result.success is False
        assert result.image_status == ImageStage.UNAVAILABLE.value
        assert "no vision-capable model found" in result.image_error
        assert inference.prompts == []

    def test_vision_crash_is_reported_as_image_failure(self, tmp_path):
        path = make_image(tmp_path / "shot.png")
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        spy = SpyVisionService(RuntimeError("llama-server died"))

        result = ai.chat("What is this?", attachments=[str(path)], vision_service=spy)

        assert result.success is False
        assert result.image_status == ImageStage.FAILED.value
        assert "llama-server died" in result.image_error
        assert inference.prompts == []

    def test_loaded_metadata_is_reported(self, tmp_path):
        path = make_image(tmp_path / "shot.png", size=(32, 32))
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        spy = SpyVisionService(complete_result("a solid colour"))

        result = ai.chat("Describe", attachments=[str(path)], vision_service=spy)

        assert result.success is True
        assert len(result.images) == 1
        meta = result.images[0]
        assert meta["name"] == "shot.png"
        assert meta["width"] == 32
        assert meta["format"] == "PNG"
        assert meta["mime_type"] == "image/png"

    def test_text_only_path_is_unchanged(self):
        """A message with no image never consults vision."""
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        result = ai.chat("Just text")
        assert result.success is True
        assert result.image_status == ImageStage.NONE.value
        assert result.image_report == ""
        assert result.images == []

    @requires_gisai
    def test_attachment_objects_are_accepted(self, tmp_path):
        """``Attachment``-like objects work, not just strings."""
        from giscode.gisai.types import Attachment

        path = make_image(tmp_path / "shot.png")
        inference = _FakeInference()
        _, ai = _engine_and_ai(inference)
        spy = SpyVisionService(complete_result("a red rectangle"))
        attachment = Attachment(path=str(path), mime_type="image/png", size_bytes=1)

        result = ai.chat("What is this?", attachments=[attachment], vision_service=spy)

        assert result.success is True
        assert result.image_status == ImageStage.INTERPRETED.value

    def test_empty_message_is_rejected(self, tmp_path):
        _, ai = _engine_and_ai(_FakeInference())
        assert ai.chat("   ").success is False
        assert ai.chat("").success is False


# ----------------------------------------------------------------- persistence


class TestImageTurnPersistence:
    """The stored turn must still say which image it referred to."""

    def _session(self, tmp_path):
        store = ProjectStore(db_path=str(tmp_path / "p.db"))
        project = Project.create("Demo", str(tmp_path / "demo"))
        store.create_project(project)
        chat_store = ChatStore(tmp_path / "chat.db")
        engine = PlanerEngine(project_id=project.id)
        ai = PlanerAI(engine=engine, inference=_FakeInference())
        chat = PlanerChatPersistence(project=project, chat_store=chat_store)
        return ProjectWorkspaceSession(
            project=project,
            project_store=store,
            chat_store=chat_store,
            engine=engine,
            ai=ai,
            chat=chat,
        )

    def test_attachment_meta_is_persisted(self, tmp_path):
        session = self._session(tmp_path)
        image = make_image(tmp_path / "shot.png")
        spy = SpyVisionService(complete_result("a red rectangle"))

        result = session.chat_with_planer(
            "What is this?",
            attachments=[str(image)],
            vision_service=spy,
        )
        assert result.success is True
        assert result.images

        user_turn = next(t for t in session.planer_chat.load_history() if t.role == "user")
        assert user_turn.meta["attachments"][0]["name"] == "shot.png"

    def test_text_only_turn_has_no_attachment_meta(self, tmp_path):
        session = self._session(tmp_path)
        result = session.chat_with_planer("plain text question")
        assert result.success is True
        user_turn = next(t for t in session.planer_chat.load_history() if t.role == "user")
        assert not (user_turn.meta or {}).get("attachments")

    def test_image_failure_is_not_persisted_as_a_good_turn(self, tmp_path):
        """A failed image read must not silently record a normal turn."""
        session = self._session(tmp_path)
        bad = write_text_file(tmp_path / "fake.png", "not an image")
        spy = SpyVisionService(complete_result("never used"))

        result = session.chat_with_planer(
            "What is this?",
            attachments=[str(bad)],
            vision_service=spy,
        )
        assert result.success is False
        assert session.planer_chat.load_history() == []
