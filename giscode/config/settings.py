import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from giscode.config.manager import ConfigManager

DEFAULT_CONFIG = {
    "ask before applying edits": True,
    "edits": {
        "ask_before_applying": True,
    },
    "app": {
        "name": "GisCode",
        "version": "0.1.0",
    },
    "support": {
        # The owner's sponsorship page. Shipped as the owner's PayPal page; set
        # it empty to disable the button, which then explains that no page is
        # configured instead of sending users to an address nobody chose. The
        # GISCODE_SPONSORS_URL environment variable wins over this value.
        "sponsors_url": "https://paypal.me/Jusoor1948",
    },
    "paths": {
        "projects_dir": "~/giscode_projects",
        "models_dir": "~/.giscode/models",
        "logs_dir": "~/.giscode/logs",
        "packaging_output_dir": "dist",
        "packaging_artifacts_dir": "build",
    },
    "inference": {
        "backend": "llama.cpp",
        "context_length": 4096,
        "threads": 4,
        # Optional pin for a specific llama-server.exe (same effect as the
        # GISCODE_LLAMA_SERVER_PATH environment variable, which wins over this).
        # Empty = discover every build, run --version on each and use the newest
        # working CUDA build (then Vulkan, then CPU) on this machine.
        "llama_server_path": "",
        # False = automatic: use the best working build (CUDA, Vulkan, then CPU)
        # and step a model down to zero GPU layers when it does not fit. True =
        # refuse CPU-only execution instead of silently degrading: a CPU build
        # is not selected and a model that cannot fully fit in graphics memory
        # is reported as refused rather than loaded on the CPU.
        "require_gpu": False,
    },
    "model": {
        "selected_model": "",
        "context_size": 4096,
        "gpu_layers": 35,
        "vram_budget_bytes": 0,
        # Reasoning/thinking budget for model-backed calls (planner, model
        # fill): "off" (no thinking), "low" (small thinking budget) or
        # "default" (template default). Code-only edits never call the model.
        "reasoning_budget": "off",
        "generation_defaults": {
            "max_tokens": 512,
            "temperature": 0.7,
        },
    },
    "task": {
        "default_max_duration": 300,
        # Task time limit in minutes (the window's task clock). Overridable per
        # model name via time_limit_overrides, e.g. {"Qwen3.8-27B": 30}; models
        # whose file is larger than 8 GB use large_model_time_limit_minutes
        # unless they have an explicit override.
        "time_limit_minutes": 15,
        "large_model_time_limit_minutes": 30,
        "time_limit_overrides": {},
        "auto_checkpoint": True,
    },
    "packaging": {
        "enabled": False,
        "package_type": "msix",
        "publisher": "GisCode",
        "identity_name": "GisCode.LocalAI",
        "min_windows_build": "10.0.17763.0",
    },
    "signing": {
        "enabled": False,
        "timestamp_server": "http://timestamp.digicert.com",
        "signature_algorithm": "sha256",
    },
    "monitoring": {
        "enabled": True,
        "namespace": "default",
        "max_events": 1000,
        "knowledge_store_integration": True,
    },
    "self_improvement": {
        "enabled": True,
        "namespace": "default",
        "max_retrieved_patterns": 5,
        "min_verification_status": "verified_pair",
        "dataset_export_path": "build/self_improvement_dataset.json",
        "knowledge_store_integration": True,
    },
    "research": {
        "enabled": True,
        "provider": "mock",
        "max_results": 5,
        "timeout": 30.0,
        "max_query_length": 500,
        "max_response_size": 8192,
        "knowledge_store_integration": True,
        "monitoring_integration": True,
    },
    "iosxtool": {
        "wsl_distro": "Ubuntu",
        "wsl_exe": "wsl.exe",
        "workspace_root": "/root/giscode_ios",
        "project_name": "HelloWorldTest",
        "xcode_xip_path": "",
        # Apple credentials are NEVER stored here. This file contains only
        # paths and non-sensitive configuration.
    },
    "hosted": {
        # Hosted writer: swap the local planner/writer for a hosted LLM.
        # The API key is NEVER stored here - it is read only from the
        # environment variable GISCODE_HOSTED_API_KEY, at call time.
        "enabled": False,
        "model": "minimax-m3",
        "base_url": "https://opencode.ai/zen/v1",
        # "auto" picks the endpoint shape from the model id (claude* ->
        # /v1/messages, gpt* -> /v1/responses, else /v1/chat/completions);
        # set to "chat" | "messages" | "responses" to force a shape.
        "api_style": "auto",
        "timeout_seconds": 180.0,
        "max_request_chars": 500000,
        # Hard cap on hosted calls for a run (0 = unlimited). The evidence
        # harness sets 300 for the whole run.
        "max_calls": 0,
        # Per-project enable overrides: {project_root: bool}.
        "project_overrides": {},
    },
}


@dataclass
class Settings:
    data: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self.data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        keys = key.split(".")
        target = self.data
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value

    def to_dict(self) -> dict:
        return self.data


class SettingsManager:
    def __init__(self, config_path: Path | None = None):
        self.manager = ConfigManager(config_path)
        self.settings = Settings(self.manager.load())

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.settings.set(key, value)
        self.manager.save(self.settings.to_dict())

    def save(self) -> None:
        self.manager.save(self.settings.to_dict())

    @property
    def raw(self) -> dict:
        return self.settings.to_dict()
