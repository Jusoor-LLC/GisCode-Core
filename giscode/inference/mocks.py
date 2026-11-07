import threading
import time
from typing import Optional

from giscode.inference.errors import (
    InferenceError,
    LoadFailureError,
    RequestCancelledError,
)
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.inference.runtime import GenerationRequest, GenerationResponse, LlamaCppRuntime


class MockLlamaCppRuntime(LlamaCppRuntime):
    def __init__(self, available: bool = True, load_succeeds: bool = True):
        self._available = available
        self._load_succeeds = load_succeeds
        self._loaded_model: Optional[str] = None
        self._server_running = False

    def is_available(self) -> bool:
        return self._available

    def start_server(self, model_path: str, port: int = 8080, n_ctx: Optional[int] = None, n_threads: Optional[int] = None, n_gpu_layers: Optional[int] = None, mmproj_path: Optional[str] = None, cpu_moe: bool = False) -> bool:
        if not self._load_succeeds:
            return False
        self._loaded_model = model_path
        self._server_running = True
        return True

    def stop_server(self) -> None:
        self._loaded_model = None
        self._server_running = False

    def generate(self, request: GenerationRequest, port: int = 8080, cancel_event: Optional[threading.Event] = None) -> GenerationResponse:
        if not self._server_running:
            raise InferenceError("Server not running")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Generation cancelled by user")
        time.sleep(0.01)
        return GenerationResponse(
            text=f"Mock response to: {request.prompt[:20]}",
            tokens_generated=len(request.prompt.split()),
        )

    def chat_generate(self, request, port: int = 8080, cancel_event: Optional[threading.Event] = None):
        if not self._server_running:
            raise InferenceError("Server not running")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Generation cancelled by user")
        from giscode.inference.runtime import ChatResponse
        return ChatResponse(
            text=f"Mock response to: {request.messages[-1].content[:20]}",
            tokens_generated=len(request.messages),
        )

    def is_running(self, port: int = 8080) -> bool:
        return self._server_running
