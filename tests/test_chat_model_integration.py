"""Chat must answer with the selected model - and change nothing on disk."""

from __future__ import annotations

from pathlib import Path

from giscode.agentic import capabilities
from giscode.agentic.core_tasks import execute_request
from giscode.inference.errors import InferenceUnavailableError
from giscode.inference.router import Target
from giscode.inference.runtime import ChatResponse


class _Router:
    """A router whose model answers predictably."""

    def __init__(self, reply="Here is the explanation.", fail: Exception | None = None):
        self.reply = reply
        self.fail = fail
        self.requests = []

    def active_target(self):
        return Target(
            kind="ollama",
            model="qwen3:14b",
            provider_id="ollama",
            profile_id="profile-1",
            profile_label="Fast coding",
            purpose="chat",
        )

    def can_chat(self):
        return True

    def chat_generate(self, request):
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return ChatResponse(text=self.reply)

    def describe_active(self, purpose=None):
        return "ollama · qwen3:14b (Fast coding profile)"

    def fallback_target(self, purpose=None):
        return None


def test_without_a_model_the_contract_still_refuses(tmp_path):
    """A user with nothing configured gets the same answer as before this change."""
    result = execute_request("Please refactor the parser", str(tmp_path))

    assert result["status"] == capabilities.MODEL_REQUIRED
    assert "cannot carry out that request without GisAI" in result["response"]
    assert list(tmp_path.iterdir()) == []


def test_a_selected_model_answers_a_coding_question(tmp_path):
    router = _Router(reply="Rename `parse` to `parse_header` and update its two callers.")
    (tmp_path / "app.py").write_text("def parse():\n    pass\n", encoding="utf-8")

    result = execute_request("Please refactor the parser", str(tmp_path), model_router=router)

    assert result["status"] == capabilities.COMPLETED
    assert "parse_header" in result["response"]
    assert "no files were changed" in result["response"].lower()
    # The prompt is the Chat contract: a system message plus the user's turn.
    assert router.requests[0].messages[0].role == "system"
    assert "read-only" in router.requests[0].messages[0].content.lower()
    # And the project is untouched.
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def parse():\n    pass\n"


def test_an_ordinary_question_reaches_the_model(tmp_path):
    router = _Router(reply="A dataclass is a generated class for holding data.")

    result = execute_request("What is a dataclass?", str(tmp_path), model_router=router)

    assert result["status"] == capabilities.COMPLETED
    assert "dataclass" in result["response"]
    assert len(router.requests) == 1


def test_a_pro_only_request_is_never_sent_to_the_model(tmp_path):
    router = _Router()

    result = execute_request("package the release", str(tmp_path), model_router=router)

    assert result["status"] == capabilities.PRO_ONLY
    assert router.requests == []
    assert "GisCode Pro" in result["response"]


def test_a_named_unsupported_capability_stays_refused_with_a_model(tmp_path):
    """Connecting a model must not widen what Core is allowed to do."""
    router = _Router()

    result = execute_request("run the tests", str(tmp_path), model_router=router)

    assert result["status"] == capabilities.UNSUPPORTED
    assert router.requests == []
    assert "cannot carry out that request without GisAI" in result["response"]


def test_a_provider_failure_is_reported_honestly(tmp_path):
    router = _Router(fail=InferenceUnavailableError("Ollama cannot answer: connection refused"))

    result = execute_request("Please refactor the parser", str(tmp_path), model_router=router)

    assert result["status"] == capabilities.FAILED
    assert "could not answer" in result["response"]
    assert "Ollama cannot answer" in result["response"]
    assert "no files were changed" not in result["response"].lower()


def test_an_empty_answer_is_a_failure_not_a_success(tmp_path):
    result = execute_request("Please refactor the parser", str(tmp_path), model_router=_Router(reply="   "))

    assert result["status"] == capabilities.FAILED
    assert "empty answer" in result["response"]


def test_the_named_file_is_inlined_and_bounded(tmp_path):
    body = "\n".join(f"line-{number}" for number in range(1, 201))
    (tmp_path / "app.py").write_text(body, encoding="utf-8")
    router = _Router()

    execute_request("Explain what app.py does", str(tmp_path), model_router=router)

    prompt = router.requests[0].messages[1].content
    assert "line-1\n" in prompt or "line-1" in prompt
    assert "line-150" in prompt
    assert "line-200" not in prompt
    assert "more lines" in prompt


def test_context_never_escapes_the_project(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("SECRET = 'do not send'\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    router = _Router()

    execute_request("Explain ../outside_secret.py", str(project), model_router=router)

    prompt = router.requests[0].messages[1].content
    assert "do not send" not in prompt
