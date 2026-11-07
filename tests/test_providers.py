"""Provider layer tests: validation, parsing, registry, profiles, guidance.

Every network call is replaced with a fake, so these tests never need a running
Ollama or LM Studio and never touch the network.
"""
from __future__ import annotations

import pytest

from giscode.providers import base as pbase
from giscode.providers import hardware as hw
from giscode.providers import lmstudio, ollama, openai_compat, testing
from giscode.providers.profiles import ProfileStore
from giscode.providers.registry import ProviderRegistry


class FakeSettings:
    """Minimal stand-in for SettingsManager (dot-path get/set/save)."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key, value):
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self):
        return None


class FakeProvider(pbase.Provider):
    kind = pbase.OPENAI_COMPAT
    label = "Fake"

    def __init__(self, reply="ready", fail=None):
        super().__init__("http://127.0.0.1:9999")
        self._reply = reply
        self._fail = fail

    def status(self):
        return pbase.ProviderStatus(
            kind=self.kind, label=self.label, endpoint=self.endpoint, available=True
        )

    def list_models(self):
        return [pbase.ProviderModel(id="m", provider=self.kind, name="m")]

    def chat(self, model, messages, *, max_tokens=256, temperature=0.2, timeout=None):
        if self._fail:
            raise self._fail
        return self._reply


# ------------------------------------------------------------------ endpoints

def test_endpoint_accepts_bare_host_port_and_strips_v1():
    assert pbase.normalize_endpoint("127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert pbase.normalize_endpoint("http://localhost:1234/v1") == "http://localhost:1234"


def test_remote_endpoint_requires_opt_in():
    with pytest.raises(ValueError):
        pbase.validate_endpoint("http://192.168.1.20:11434")
    assert pbase.validate_endpoint("http://192.168.1.20:11434", allow_remote=True)


def test_non_http_scheme_is_rejected():
    with pytest.raises(ValueError):
        pbase.validate_endpoint("ftp://127.0.0.1:21")


def test_loopback_detection_includes_ip_and_name():
    assert pbase.is_loopback("http://127.0.0.1:1234")
    assert pbase.is_loopback("http://localhost:11434")
    assert not pbase.is_loopback("http://10.0.0.5:11434")


def test_secret_free_endpoint_drops_userinfo():
    provider = openai_compat.OpenAICompatibleProvider(
        "http://user:secret@127.0.0.1:8080", label="Local"
    )
    assert "secret" not in provider.secret_free_endpoint()
    assert "user" not in provider.secret_free_endpoint()


# --------------------------------------------------------------------- ollama

def test_ollama_lists_models_with_quantization(monkeypatch):
    payload = {
        "models": [
            {
                "name": "qwen2.5-coder:7b",
                "size": 4_700_000_000,
                "details": {
                    "quantization_level": "Q4_K_M",
                    "families": ["qwen2", "clip"],
                    "parameter_size": "7.6B",
                },
            }
        ]
    }
    monkeypatch.setattr(ollama, "request_json", lambda *a, **k: payload)
    models = ollama.OllamaProvider().list_models()
    assert [m.id for m in models] == ["qwen2.5-coder:7b"]
    assert models[0].quantization == "Q4_K_M"
    assert "vision" in models[0].capabilities


def test_ollama_chat_returns_content(monkeypatch):
    monkeypatch.setattr(
        ollama, "request_json", lambda *a, **k: {"message": {"content": "ready"}}
    )
    assert ollama.OllamaProvider().chat("m", [{"role": "user", "content": "hi"}]) == "ready"


def test_ollama_reports_unavailable_instead_of_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise pbase.ProviderError("Nothing is listening on that endpoint.")

    monkeypatch.setattr(ollama, "request_json", boom)
    status = ollama.OllamaProvider().status()
    assert status.available is False
    assert "listening" in status.detail


def test_ollama_empty_reply_is_an_error(monkeypatch):
    monkeypatch.setattr(ollama, "request_json", lambda *a, **k: {"message": {}})
    with pytest.raises(pbase.ProviderError):
        ollama.OllamaProvider().chat("m", [{"role": "user", "content": "hi"}])


# ------------------------------------------------------------------ lm studio

def test_lmstudio_lists_openai_models(monkeypatch):
    monkeypatch.setattr(
        lmstudio, "request_json", lambda *a, **k: {"data": [{"id": "qwen3-coder-7b"}]}
    )
    models = lmstudio.LMStudioProvider().list_models()
    assert models[0].id == "qwen3-coder-7b"
    assert models[0].provider == pbase.LMSTUDIO


def test_lmstudio_chat_parses_choices(monkeypatch):
    body = {"choices": [{"message": {"content": "ready"}}]}
    monkeypatch.setattr(lmstudio, "request_json", lambda *a, **k: body)
    assert lmstudio.LMStudioProvider().chat("m", [{"role": "user", "content": "x"}]) == "ready"


def test_lmstudio_missing_content_raises(monkeypatch):
    monkeypatch.setattr(lmstudio, "request_json", lambda *a, **k: {"choices": []})
    with pytest.raises(pbase.ProviderError):
        lmstudio.LMStudioProvider().chat("m", [{"role": "user", "content": "x"}])


# ------------------------------------------------------------------- registry

def test_registry_defaults_include_both_builtin_providers():
    registry = ProviderRegistry(FakeSettings())
    assert set(registry.enabled_ids()) == {pbase.OLLAMA, pbase.LMSTUDIO}
    providers = registry.list()
    assert {p.kind for p in providers} == {pbase.OLLAMA, pbase.LMSTUDIO}


def test_registry_adds_custom_endpoint_and_persists():
    settings = FakeSettings()
    registry = ProviderRegistry(settings)
    provider_id = registry.add_custom("Work server", "127.0.0.1:8080")
    assert provider_id == "work-server"
    stored = settings.get("providers")
    assert stored[provider_id]["endpoint"] == "127.0.0.1:8080"
    assert provider_id in ProviderRegistry(settings).enabled_ids()


def test_registry_rejects_remote_custom_endpoint_by_default():
    registry = ProviderRegistry(FakeSettings())
    with pytest.raises(ValueError):
        registry.add_custom("Remote", "http://203.0.113.9:8000")


def test_registry_remove_disables_builtin_but_deletes_custom():
    settings = FakeSettings()
    registry = ProviderRegistry(settings)
    registry.remove(pbase.OLLAMA)
    assert pbase.OLLAMA not in registry.enabled_ids()
    custom = registry.add_custom("Mine", "127.0.0.1:8099")
    registry.remove(custom)
    assert custom not in settings.get("providers")


def test_registry_statuses_survive_unreachable_provider(monkeypatch):
    registry = ProviderRegistry(FakeSettings())

    def boom(*args, **kwargs):
        raise pbase.ProviderError("Nothing is listening on that endpoint.")

    monkeypatch.setattr(ollama, "request_json", boom)
    statuses = registry.statuses()
    assert len(statuses) == 2
    by_kind = {s.kind: s for s in statuses}
    assert by_kind[pbase.OLLAMA].available is False
    assert by_kind[pbase.LMSTUDIO].available is False


# ------------------------------------------------------------------- profiles

def test_profile_migration_creates_coding_profile_from_legacy_setting():
    settings = FakeSettings({"model": {"selected_model": "Qwen3-Coder-30B-A3B-Instruct"}})
    store = ProfileStore(settings)
    note = store.migrate()
    assert "Qwen3-Coder" in note
    profile = store.active_for("coding")
    assert profile is not None
    assert profile.provider == pbase.LLAMA_CPP
    assert profile.model == "Qwen3-Coder-30B-A3B-Instruct"


def test_profile_migration_is_idempotent():
    store = ProfileStore(FakeSettings({"model": {"selected_model": "m"}}))
    store.migrate()
    assert store.migrate() == ""
    assert len(store.all()) == 1


def test_profile_create_rename_and_switch():
    store = ProfileStore(FakeSettings())
    coding = store.create("coding", provider=pbase.OLLAMA, model="qwen2.5-coder:7b")
    other = store.create(
        "coding", provider=pbase.LMSTUDIO, model="qwen3-coder-7b", label="LM coding"
    )
    assert store.active_for("coding").id == coding.id
    store.set_active(other.id)
    assert store.active_for("coding").id == other.id
    store.rename(other.id, "LM Studio coding")
    assert store.get(other.id).label == "LM Studio coding"
    assert store.get(other.id).model == "qwen3-coder-7b"


def test_profile_restore_last_good_undoes_a_failed_switch():
    store = ProfileStore(FakeSettings())
    good = store.create("coding", provider=pbase.OLLAMA, model="good")
    store.mark_good(good.id)
    bad = store.create("coding", provider=pbase.OLLAMA, model="bad", make_active=True)
    assert store.active_for("coding").id == bad.id
    restored = store.restore_last_good("coding")
    assert restored.id == good.id
    assert store.active_for("coding").id == good.id


def test_profile_delete_clears_active_selection():
    store = ProfileStore(FakeSettings())
    profile = store.create("chat", provider=pbase.OPENAI_COMPAT, model="m")
    assert store.delete(profile.id) is True
    assert store.active_id("chat") == ""
    assert store.delete(profile.id) is False


def test_profile_rejects_unknown_purpose():
    store = ProfileStore(FakeSettings())
    with pytest.raises(ValueError):
        store.create("telepathy", provider=pbase.OLLAMA, model="m")


# ------------------------------------------------------------------- hardware

GB = 1024 ** 3


def test_hardware_suitable_when_estimate_fits_vram():
    hardware = hw.Hardware(total_ram_bytes=32 * GB, total_vram_bytes=16 * GB, available_vram_bytes=15 * GB)
    verdict = hw.assess(int(4.5 * GB), context_length=4096, hardware=hardware)
    assert verdict.level == hw.SUITABLE
    assert "VRAM" in verdict.detail


def test_hardware_may_fit_when_only_ram_fits():
    hardware = hw.Hardware(total_ram_bytes=64 * GB, total_vram_bytes=8 * GB, available_vram_bytes=7 * GB)
    verdict = hw.assess(int(20 * GB), context_length=4096, hardware=hardware)
    assert verdict.level == hw.MAY_FIT


def test_hardware_insufficient_when_nothing_fits():
    hardware = hw.Hardware(total_ram_bytes=8 * GB, total_vram_bytes=4 * GB, available_vram_bytes=3 * GB)
    verdict = hw.assess(int(40 * GB), context_length=8192, hardware=hardware)
    assert verdict.level == hw.INSUFFICIENT


def test_hardware_unknown_without_size_or_detection():
    assert hw.assess(0, hardware=hw.Hardware()).level == hw.UNKNOWN
    assert hw.assess(5 * GB, hardware=hw.Hardware()).level == hw.UNKNOWN


def test_hardware_uses_existing_budget_estimator():
    class Budget:
        def get_system_resources(self):
            return type("R", (), {"total_ram_bytes": 32 * GB, "total_vram_bytes": 24 * GB, "available_vram_bytes": 22 * GB})()

        def estimate_model_memory(self, size, ctx):
            return size + 4096  # deliberately tiny

    verdict = hw.assess(int(10 * GB), context_length=4096, budget=Budget())
    assert verdict.level == hw.SUITABLE


# -------------------------------------------------------------------- testing

def test_code_reply_analysis_accepts_a_good_answer():
    reply = "def add(a, b):\n    return a + b\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    checks = testing.analyse_code_reply(reply)
    assert checks == {"parses": True, "has_add": True, "has_test": True, "add_takes_two": True}


def test_code_reply_analysis_strips_markdown_fences():
    reply = "```python\ndef add(a, b):\n    return a + b\n```\n"
    checks = testing.analyse_code_reply(reply)
    assert checks["parses"] and checks["has_add"]


def test_code_reply_analysis_flags_broken_python():
    checks = testing.analyse_code_reply("def add(a, b:\n    return a + b")
    assert checks["parses"] is False


def test_connection_test_distinguishes_reply_from_failure():
    ok = testing.connection_test(FakeProvider("ready"), "m")
    assert ok.ok and "ready" in ok.sample
    failed = testing.connection_test(
        FakeProvider(fail=pbase.ProviderError("nothing")), "m"
    )
    assert not failed.ok
    assert "nothing" in failed.detail


def test_coding_test_reports_structure_not_just_reply():
    good = "def add(a, b):\n    return a + b\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    assert testing.coding_test(FakeProvider(good), "m").ok is True
    vague = testing.coding_test(FakeProvider("Sure! Here is how you might start."), "m")
    assert vague.ok is False
    assert "not valid Python" in vague.detail
