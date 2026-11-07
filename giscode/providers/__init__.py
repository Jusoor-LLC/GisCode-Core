"""Local model providers for GisCode Core.

The built-in ``llama.cpp`` runtime is still the default way a model runs. This
package adds *other* local servers - Ollama, LM Studio and any OpenAI-compatible
endpoint - so users can select a model that already lives there, and adds the
pieces needed to choose one deliberately: provider status, model profiles,
resumable downloads, hardware guidance and two honest tests.

See ``docs/LOCAL_AI_SETUP.md`` for what each provider supports.
"""
from giscode.providers.base import (
    DEFAULT_ENDPOINTS,
    LLAMA_CPP,
    LMSTUDIO,
    OLLAMA,
    OPENAI_COMPAT,
    PROVIDER_KINDS,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
    is_loopback,
    normalize_endpoint,
    validate_endpoint,
)
from giscode.providers.downloads import DownloadManager, DownloadError
from giscode.providers.hardware import Compatibility, Hardware, UNKNOWN, assess, detect_hardware
from giscode.providers.profiles import PURPOSES, ModelProfile, ProfileStore
from giscode.providers.registry import ProviderRegistry
from giscode.providers.testing import TestOutcome, coding_test, connection_test

__all__ = [
    "Compatibility",
    "DEFAULT_ENDPOINTS",
    "DownloadError",
    "DownloadManager",
    "Hardware",
    "LLAMA_CPP",
    "LMSTUDIO",
    "ModelProfile",
    "OLLAMA",
    "OPENAI_COMPAT",
    "PURPOSES",
    "PROVIDER_KINDS",
    "ProfileStore",
    "Provider",
    "ProviderError",
    "ProviderModel",
    "ProviderRegistry",
    "ProviderStatus",
    "TestOutcome",
    "UNKNOWN",
    "assess",
    "coding_test",
    "connection_test",
    "detect_hardware",
    "is_loopback",
    "normalize_endpoint",
    "validate_endpoint",
]
