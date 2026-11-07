"""Tests for the AI-backed Planer.

These tests use a fake inference backend; they do **not** require a
real model. The real-model smoke test is in
``workspace/tests/planer_ai_real.py`` and is opt-in.
"""
import json
import os
from pathlib import Path
from typing import Optional

import pytest

from giscode.inference.errors import InferenceError
from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    GenerationRequest,
)
from giscode.planer import (
    OfflineResearchProvider,
    PlanerAI,
    PlanerAIResult,
    PlanerEngine,
    ProjectPlan,
    engine_does_not_reference_coding_agent,
    is_plan_ready_for_review,
    plan_to_project_dict,
)
from giscode.planer.ai import parse_planer_json
from giscode.planer.types import ResearchResultRef
from giscode.projects import Project, ProjectLifecycle, ProjectStore
from giscode.storage import ChatStore


# ----------------------------------------------------------------- fakes


class _FakeInference:
    """Test double that mimics :class:`InferenceAPI` enough for
    :class:`PlanerAI` to consume it.

    ``responses`` is a list of strings (or ``None`` to simulate
    empty / error). When the list is exhausted, the last value is
    reused. ``raise_inference_error`` makes the next call raise
    :class:`InferenceError` instead of returning a response.
    """

    def __init__(self, responses: Optional[list] = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[ChatRequest] = []
        self.raise_inference_error: Optional[Exception] = None
        self.fail_with: Optional[Exception] = None

    def chat_generate(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        if self.raise_inference_error is not None:
            err = self.raise_inference_error
            self.raise_inference_error = None
            raise err
        if self.fail_with is not None:
            err = self.fail_with
            self.fail_with = None
            raise err
        if not self.responses:
            return ChatResponse(text="")
        text = self.responses.pop(0)
        if text is None:
            return ChatResponse(text="")
        return ChatResponse(text=str(text))


class _FakeResearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    def research(self, query: str, purpose: str = "", project_id: str = "") -> ResearchResultRef:
        self.calls.append({"query": query, "purpose": purpose, "project_id": project_id})
        return ResearchResultRef(
            query=query,
            summary=f"summary of {query}",
            sources=[{"url": "https://example.com", "title": "Example", "domain": "example.com"}],
            confidence=0.7,
            is_current=True,
            purpose=purpose,
        )


# ----------------------------------------------------------------- parser


def test_parser_handles_plain_json():
    assert parse_planer_json('{"assistant_message": "hi"}') == {"assistant_message": "hi"}


def test_parser_handles_prose_wrapped_json():
    text = 'Here is my plan:\n{"assistant_message": "hi"}\nThanks!'
    out = parse_planer_json(text)
    assert out == {"assistant_message": "hi"}


def test_parser_handles_markdown_fenced_json():
    text = '```json\n{"assistant_message": "hi"}\n```'
    out = parse_planer_json(text)
    assert out == {"assistant_message": "hi"}


def test_parser_returns_none_for_garbage():
    assert parse_planer_json("") is None
    assert parse_planer_json("not json at all") is None
    assert parse_planer_json("here is a partial { broken") is None


def test_parser_returns_none_for_non_object():
    assert parse_planer_json("[1, 2, 3]") is None
    assert parse_planer_json("42") is None
    assert parse_planer_json('"hello"') is None


# ----------------------------------------------------------------- AI basics


def test_ai_requires_inference_to_be_configured():
    e = PlanerEngine(project_id="proj-1")
    ai = PlanerAI(engine=e, inference=None)
    r = ai.chat("hello")
    assert r.success is False
    assert "no inference backend" in r.inference_error
    # Plan is untouched
    assert e.plan.understanding.original_idea == ""


def test_ai_handles_inference_error_without_corrupting_plan(tmp_path: Path):
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("Build X")
    snap_before = e.plan.to_dict()
    fake = _FakeInference(responses=[])
    fake.raise_inference_error = InferenceError("boom")
    ai = PlanerAI(engine=e, inference=fake)
    r = ai.chat("anything")
    assert r.success is False
    assert "boom" in r.inference_error
    assert e.plan.to_dict() == snap_before


def test_ai_handles_empty_model_response():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    snap_before = e.plan.to_dict()
    fake = _FakeInference(responses=[""])
    ai = PlanerAI(engine=e, inference=fake)
    r = ai.chat("anything")
    assert r.success is False
    assert "empty" in r.inference_error.lower()
    assert e.plan.to_dict() == snap_before


def test_ai_handles_malformed_json_without_corrupting_plan():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    snap_before = e.plan.to_dict()
    fake = _FakeInference(responses=["this is not JSON at all"])
    ai = PlanerAI(engine=e, inference=fake)
    r = ai.chat("anything")
    assert r.success is False
    assert r.parse_error
    assert e.plan.to_dict() == snap_before


def test_ai_handles_json_without_known_keys():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    fake = _FakeInference(responses=['{"foo": "bar", "baz": 1}'])
    ai = PlanerAI(engine=e, inference=fake)
    r = ai.chat("anything")
    assert r.success is False
    assert "no recognised" in r.parse_error or r.parse_error


def test_ai_ignores_unknown_top_level_keys():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "approve_project": True,            # smuggling attempt
        "start_building": True,             # smuggling attempt
        "run_command": "rm -rf /",          # smuggling attempt
        "agent": "CodingAgentLoop",         # smuggling attempt
        "features": [{"name": "Login"}],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    ai = PlanerAI(engine=e, inference=fake)
    r = ai.chat("X")
    assert r.success is True
    # The features field was applied…
    assert e.plan.requirements.features and e.plan.requirements.features[0].name == "Login"
    # …but the smuggling keys were dropped.
    assert "approve_project" not in r.applied
    assert "start_building" not in r.applied
    assert "run_command" not in r.applied
    assert "agent" not in r.applied


# ----------------------------------------------------------------- prompt content


def test_ai_sends_system_prompt_with_safety_rules():
    e = PlanerEngine(project_id="proj-1")
    fake = _FakeInference(responses=['{"assistant_message": "ok"}'])
    ai = PlanerAI(engine=e, inference=fake)
    ai.chat("hello")
    assert fake.calls, "inference should have been called"
    messages = fake.calls[0].messages
    sys_msgs = [m for m in messages if m.role == "system"]
    assert len(sys_msgs) == 1
    text = sys_msgs[0].content
    # Safety rules
    for phrase in (
        "planning-only",
        "do",
        "not",
        "implement",  # "must not implement"
        "approve",
        "build",  # "must not build"
    ):
        assert phrase in text.lower(), f"system prompt missing {phrase!r}"
    # The CodingAgent boundary is explicit
    assert "CodingAgent" in text


def test_ai_sends_user_prompt_with_current_plan():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("Build a calculator")
    fake = _FakeInference(responses=['{"assistant_message": "ok"}'])
    ai = PlanerAI(engine=e, inference=fake)
    ai.chat("help me think about screens")
    messages = fake.calls[0].messages
    user_msgs = [m for m in messages if m.role == "user"]
    assert any("Build a calculator" in m.content for m in user_msgs)
    # The user prompt must reference the open questions / current understanding
    assert any("current_understanding" in m.content for m in user_msgs)


def test_ai_sends_chat_request_with_reasonable_sampling():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    fake = _FakeInference(responses=['{"assistant_message": "ok"}'])
    ai = PlanerAI(engine=e, inference=fake, max_tokens=300, temperature=0.1)
    ai.chat("hi")
    assert fake.calls[0].max_tokens == 300
    assert fake.calls[0].temperature == 0.1


# ----------------------------------------------------------------- structured output


def test_ai_applies_features_from_model():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "features": [
            {"name": "Login", "description": "Sign in", "priority": "high", "rationale": "needed"},
            {"name": "Logout"},
        ],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    names = [f.name for f in e.plan.requirements.features]
    assert "Login" in names
    assert "Logout" in names
    assert e.plan.requirements.features[0].priority == "high"


def test_ai_does_not_duplicate_existing_features():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    e.add_feature("Login", priority="high")
    payload = {"assistant_message": "ok", "features": [{"name": "Login", "description": "x"}]}
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert len(e.plan.requirements.features) == 1


def test_ai_applies_screens_from_model():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "screens": [
            {"name": "Home", "purpose": "main", "major_elements": ["btn", "list"]},
            {"name": "Settings", "purpose": "config"},
        ],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    names = [s.name for s in e.plan.screens]
    assert names == ["Home", "Settings"]
    assert e.plan.screens[0].major_elements == ["btn", "list"]


def test_ai_applies_requirements():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "functional_requirements": ["persist data", "support search"],
        "non_functional_requirements": ["< 200ms response"],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert "persist data" in e.plan.requirements.functional
    assert "< 200ms response" in e.plan.requirements.non_functional


def test_ai_applies_design():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "design": {
            "application_type": "web app",
            "major_components": ["frontend", "api"],
            "architecture_notes": "REST",
        },
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert e.plan.design.application_type == "web app"
    assert "frontend" in e.plan.design.major_components
    assert e.plan.design.architecture_notes == "REST"


def test_ai_applies_branding():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "branding": {"name": "MyApp", "tagline": "Daily companion"},
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert e.plan.branding.name == "MyApp"
    assert e.plan.branding.tagline == "Daily companion"


def test_ai_applies_roadmap():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "roadmap": [
            {"title": "MVP", "description": "first version", "priority": "high"},
        ],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert e.plan.roadmap and e.plan.roadmap[0].title == "MVP"


def test_ai_applies_questions_but_caps_them():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "questions": [
            {"category": "users", "text": f"Q{i}", "rationale": "r"} for i in range(20)
        ],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake, max_questions=3).chat("ok")
    # Only 3 of the 20 questions were added
    added = [q for q in e.plan.open_questions if q.text.startswith("Q")]
    assert len(added) == 3


def test_ai_consumes_answers_for_existing_questions():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    # Seed an open question
    e.add_question_or_skip = None  # placeholder — we'll use generate_questions
    from giscode.planer.engine import PlanerQuestion
    e.plan.open_questions.append(PlanerQuestion(id="q-1", category="users", text="Who?"))
    payload = {
        "assistant_message": "ok",
        "answers_consumed": [{"category": "users", "text": "just me"}],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert "just me" in e.plan.understanding.target_users
    assert e.plan.open_questions[0].answered is True


# ----------------------------------------------------------------- preserved plan


def test_ai_failure_leaves_plan_completely_unchanged():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    e.add_feature("Existing", priority="high")
    e.add_screen("Existing screen")
    e.set_application_type("desktop")
    snap_before = json.dumps(e.plan.to_dict(), sort_keys=True)
    fake = _FakeInference(responses=["not valid json at all"])
    PlanerAI(engine=e, inference=fake).chat("more")
    snap_after = json.dumps(e.plan.to_dict(), sort_keys=True)
    assert snap_before == snap_after


# ----------------------------------------------------------------- research


def test_ai_research_offline_returns_no_findings():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    payload = {
        "assistant_message": "ok",
        "research_requests": [{"query": "current version of Tauri", "purpose": "tech"}],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")  # offline by default
    # The engine recorded no research (offline provider returns None)
    assert e.plan.research_log == []


def test_ai_research_with_real_provider_records_findings():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    research = _FakeResearch()
    payload = {
        "assistant_message": "ok",
        "research_requests": [{"query": "current Tauri version", "purpose": "tech"}],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    ai = PlanerAI(engine=e, inference=fake, research_provider=research)
    r = ai.chat("ok")
    assert r.success
    assert research.calls and research.calls[0]["query"] == "current Tauri version"
    assert e.plan.research_log
    assert e.plan.research_log[0].query == "current Tauri version"


def test_research_remains_opt_in_when_model_omits_it():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    research = _FakeResearch()
    fake = _FakeInference(responses=['{"assistant_message": "ok"}'])
    PlanerAI(engine=e, inference=fake, research_provider=research).chat("ok")
    # No research call was made
    assert research.calls == []


# ----------------------------------------------------------------- safety


def test_ai_does_not_expose_coding_agent_symbols():
    e = PlanerEngine(project_id="proj-1")
    ai = PlanerAI(engine=e, inference=_FakeInference())
    for forbidden in (
        "CodingAgentLoop", "BuildTestOrchestrator", "WorkspaceActionExecutor",
        "Verifier", "InferenceAPI", "SubprocessLlamaCppRuntime",
        "approve_project", "start_building", "run_command", "execute",
        "shell", "subprocess", "Popen",
    ):
        assert not hasattr(ai, forbidden), f"PlanerAI exposes {forbidden}"


def test_ai_engine_safety_invariant_holds():
    e = PlanerEngine(project_id="proj-1")
    ai = PlanerAI(engine=e, inference=_FakeInference())
    assert engine_does_not_reference_coding_agent(ai.engine)


def test_ai_cannot_invoke_coding_agent_via_model_payload():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    snap_before = e.plan.to_dict()
    # Try to get the model to call the agent.
    payload = {
        "assistant_message": "ok",
        "CodingAgentLoop": {"invoke": "now"},
        "agent": "CodingAgentLoop",
        "approve_project": True,
        "start_building": True,
        "run_command": "echo pwned",
        "subprocess": ["rm", "-rf", "/"],
    }
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("ok")
    # None of the smuggling keys were applied; engine plan intact
    assert e.plan.to_dict() == snap_before
    # No project lifecycle mutation
    # (the engine doesn't have a project reference, but the test
    # below proves it never touches the project store either)


def test_ai_does_not_touch_project_store(tmp_path: Path):
    from giscode.projects.store import DEFAULT_DB_PATH
    real = Path(DEFAULT_DB_PATH)
    if real.exists():
        pytest.skip("user projects.db already exists")
    e = PlanerEngine(project_id="proj-1")
    fake = _FakeInference(responses=[json.dumps({
        "assistant_message": "ok",
        "features": [{"name": "F1"}],
        "screens": [{"name": "Home"}],
    })])
    PlanerAI(engine=e, inference=fake).chat("ok")
    assert not real.exists()


def test_ai_model_payload_cannot_modify_project_lifecycle(tmp_path: Path):
    store = ProjectStore(db_path=str(tmp_path / "p.db"))
    p = Project.create("Demo", str(tmp_path / "demo"))
    store.create_project(p)
    # A PlanerAI never receives a ProjectStore reference. Even if
    # the model tries to smuggle an "approve_project" key, the
    # parser drops it and the engine has no way to reach the store.
    e = PlanerEngine(project_id=p.id)
    fake = _FakeInference(responses=[json.dumps({
        "assistant_message": "ok",
        "approve_project": True,
        "start_building": True,
        "lifecycle": "approved",
    })])
    PlanerAI(engine=e, inference=fake).chat("ok")
    # Project lifecycle is unchanged
    assert store.get_project(p.id).lifecycle == ProjectLifecycle.PLANNING
    store.close()


# ----------------------------------------------------------------- ChatStore compatibility


def test_ai_does_not_create_or_modify_chats(tmp_path: Path):
    """PlanerAI does not write to the ChatStore. The future GUI
    will use ChatStore.append_message to record planning turns; the
    AI layer itself never touches it."""
    cstore = ChatStore(db_path=str(tmp_path / "chats.db"))
    try:
        # Pre-create a project chat
        chat = cstore.create_chat("project:proj-1", "proj-1", "Planer chat")
        before = len(cstore.load_history(chat.id))
        e = PlanerEngine(project_id="proj-1")
        e.start_with_idea("X")
        fake = _FakeInference(responses=[json.dumps({
            "assistant_message": "ok",
            "features": [{"name": "F1"}],
        })])
        PlanerAI(engine=e, inference=fake).chat("ok")
        # Chat history is unchanged — the AI layer did not write
        after = len(cstore.load_history(chat.id))
        assert before == after
    finally:
        cstore.close()


def test_ai_chat_history_can_be_persisted_externally(tmp_path: Path):
    """Demonstrate the integration shape: the caller is responsible
    for persisting turns via ChatStore.append_message; the AI layer
    exposes the transcript for that purpose."""
    cstore = ChatStore(db_path=str(tmp_path / "chats.db"))
    try:
        chat = cstore.create_chat("project:proj-1", "proj-1", "Planer chat")
        e = PlanerEngine(project_id="proj-1")
        fake = _FakeInference(responses=[json.dumps({"assistant_message": "hi"})])
        ai = PlanerAI(engine=e, inference=fake)
        ai.chat("user said hi")
        # Caller persists the conversation
        for turn in ai.transcript:
            role = "assistant" if turn["role"] == "assistant" else "user"
            cstore.append_message(chat.id, role, turn["content"])
        history = cstore.load_history(chat.id)
        assert [m.role for m in history] == ["user", "assistant"]
    finally:
        cstore.close()


# ----------------------------------------------------------------- multi-turn


def test_ai_conversation_accumulates_state():
    e = PlanerEngine(project_id="proj-1")
    fake = _FakeInference(responses=[
        json.dumps({"assistant_message": "Got it.", "features": [{"name": "F1"}]}),
        json.dumps({"assistant_message": "More.", "features": [{"name": "F2"}]}),
        json.dumps({"assistant_message": "Final.", "screens": [{"name": "Home"}]}),
    ])
    ai = PlanerAI(engine=e, inference=fake)
    e.start_with_idea("Build X")
    ai.chat("first turn")
    ai.chat("second turn")
    ai.chat("third turn")
    names = [f.name for f in e.plan.requirements.features]
    assert "F1" in names and "F2" in names
    assert e.plan.screens and e.plan.screens[0].name == "Home"


def test_ai_conversation_handles_persistent_failures():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    fake = _FakeInference(responses=[
        "garbage",
        "more garbage",
        json.dumps({"assistant_message": "ok"}),
    ])
    ai = PlanerAI(engine=e, inference=fake)
    r1 = ai.chat("turn 1")
    r2 = ai.chat("turn 2")
    r3 = ai.chat("turn 3")
    assert r1.success is False and r2.success is False and r3.success is True


# ----------------------------------------------------------------- plan structure


def test_ai_plan_remains_json_serializable_through_conversation():
    e = PlanerEngine(project_id="proj-1")
    fake = _FakeInference(responses=[
        json.dumps({
            "assistant_message": "ok",
            "features": [{"name": "F1"}],
            "screens": [{"name": "Home"}],
            "design": {"application_type": "desktop"},
            "roadmap": [{"title": "MVP"}],
        })
    ])
    ai = PlanerAI(engine=e, inference=fake)
    e.start_with_idea("X")
    ai.chat("go")
    envelope = plan_to_project_dict(e.plan)
    blob = json.dumps(envelope)
    # And round-trips
    assert "F1" in blob
    from giscode.planer import plan_from_project_dict
    again = plan_from_project_dict(envelope)
    assert again.requirements.features[0].name == "F1"
    assert again.screens[0].name == "Home"
    assert again.design.application_type == "desktop"
    assert again.roadmap[0].title == "MVP"


# ----------------------------------------------------------------- ready_for_review


def test_ai_signals_ready_for_review_via_payload():
    e = PlanerEngine(project_id="proj-1")
    e.start_with_idea("X")
    e.set_application_type("desktop")
    e.add_feature("F1")
    e.add_screen("Home")
    e.add_roadmap_item("MVP")
    for q in list(e.plan.open_questions):
        e.answer_question(q.id, "ok")
    assert e.is_ready_for_review()
    payload = {"assistant_message": "Done.", "ready_for_review": True}
    fake = _FakeInference(responses=[json.dumps(payload)])
    PlanerAI(engine=e, inference=fake).chat("done")
    # Engine phase is REVIEW_READY (still the engine's own phase, not
    # the project lifecycle)
    assert e.phase.value == "review_ready"
    # And the plan is still ready
    assert is_plan_ready_for_review(e.plan)
