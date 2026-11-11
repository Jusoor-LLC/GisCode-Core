# Direct OpenAI communication — consolidated qualification report

Date: 2026-11-08
Target: the repository root (Core edition)
Provider under test: official OpenAI API, `https://api.openai.com`

---

## 1. Verdict

**Implemented and mock-qualified. Not live-verified.**

GisCode Core's coding agent can now plan, implement and repair through the
official OpenAI API using a provider profile, with billing consent, redacted
outbound context, usage accounting, streaming, cancellation and attribution.
The full wire contract is exercised against a deterministic loopback server.

No live OpenAI request was made, because this machine has no authorized
`OPENAI_API_KEY` (checked: the variable is absent) and no OpenAI API account
was provided. Nothing in this report should be read as evidence of a live
model response. Section 6 states exactly what is and is not proven.

---

## 2. Architecture delivered

Provider-neutral, with OpenAI as the first live remote integration. Local
`llama.cpp`, Ollama, LM Studio and OpenAI-compatible endpoints are untouched in
behaviour.

| Layer | File | Change |
| --- | --- | --- |
| Provider kind and endpoint | `giscode/providers/base.py` | New `OPENAI` kind, official default endpoint, `secret_values()` hook. |
| Official provider | `giscode/providers/openai.py` | **New.** Endpoint pinning, consent gate, `GET /v1/models`, `POST /v1/chat/completions`, SSE streaming, usage, redacted errors, profile setup helper. |
| Credentials | `giscode/providers/credentials.py` | **New.** Environment, in-memory and Windows DPAPI stores; masked status. |
| Usage | `giscode/providers/usage.py` | **New.** Thread-safe counters, optional JSONL, cost only when prices are configured. |
| Redaction | `giscode/providers/redaction.py` | **New.** Known secrets, `api_key=…`, bearer tokens, `sk-…`, PEM keys. |
| Registry | `giscode/providers/registry.py` | OpenAI defaults (off), builder, consent, pricing, credential injection, removal. |
| Inference adapter | `giscode/inference/provider_backend.py` | Request-id forwarding, remote redaction, scrubbed errors, provider/model attribution. |
| Router / loop | `giscode/inference/router.py`, `giscode/agentic/loop.py` | `is_provider_backend`; planning, implementation and repair skip legacy GGUF routing when a provider owns the model. |
| Response metadata | `giscode/inference/runtime.py` | Optional `provider` / `model` on chat and generation responses. |
| SSE helper | `giscode/providers/lmstudio.py` | `token_field` so OpenAI's `max_completion_tokens` and `stream_options` are used without forking the stream reader. |
| Settings UI | `giscode/gui/main_window.py` | "OpenAI API (optional, billable)" section: profile, consent, model id, masked key entry, DPAPI save/remove, usage line. |
| Tests | `tests/test_openai_provider.py` | **New.** 15 tests (see section 5). |
| Docs | `docs/OPENAI_DIRECT.md` (new), `docs/ARCHITECTURE.md`, `docs/LOCAL_AI_SETUP.md`, `docs/USER_GUIDE.md` | Setup, architecture, security boundary, troubleshooting, roadmap. |

Diff for tracked files touched by this mission:

```
 docs/ARCHITECTURE.md                  |  39 +-
 docs/LOCAL_AI_SETUP.md                |   8 +
 docs/USER_GUIDE.md                    |  30 +-
 giscode/agentic/loop.py               |  34 +-
 giscode/gui/main_window.py            | 819 ++++++++++++++++++++++++++++++++--
 giscode/inference/provider_backend.py | 370 +++++++++++++--
 giscode/inference/router.py           |  46 +-
 giscode/inference/runtime.py          |   8 +
 giscode/providers/base.py             |  40 +-
 giscode/providers/lmstudio.py         | 127 ++++++
 giscode/providers/registry.py         |  39 +-
 11 files changed, 1493 insertions(+), 67 deletions(-)
```

(The `main_window.py` and `provider_backend.py` counts include earlier work in
this checkout; this mission added the OpenAI settings section and the
request-id/redaction/attribution paths.)

Design decisions worth recording:

- **Separate provider kind.** `openai` is not `openai_compat`. The official
  provider refuses any host except `api.openai.com`, so a third-party or local
  OpenAI-shaped server keeps using the compatible route and its own rules.
- **Consent is a first-class setting.** `paid_calls_allowed` defaults to false
  and gates `list_models()`, `chat()`, `stream_chat()` and `status()`. No
  socket is opened while it is off.
- **Credentials never enter settings.** The key is resolved at call time from
  the environment or the DPAPI store; `config.json` carries only consent, model
  and optional pricing.
- **No automatic retry of a billable completion.** A timeout or 5xx is reported
  once. Retrying is a user decision, because a retry may be a second charge.
- **Guidance stays unverified.** A provider answer is parsed by the same
  planner, edited through the same approval gate, and judged by the same
  verifier as a local model. Nothing is trusted because it is remote.

---

## 3. Security results

| Requirement | Implementation | Evidence |
| --- | --- | --- |
| Secure credential storage | `OPENAI_API_KEY`, or DPAPI ciphertext at `%USERPROFILE%\.giscode\credentials\openai.bin`, bound to the Windows user; `os.replace` atomic write; `0o600` best effort. Non-Windows cannot fall back to a plaintext file. | `test_windows_dpapi_round_trip` (value not present as plaintext, survives a fresh store instance, delete works). DPAPI reported available on this host. |
| No credential in settings | `configure_openai_profile` writes consent/model only. | `test_configure_profile_persists_consent_without_a_secret` asserts the serialized settings contain no key. |
| No credential in logs/usage | Usage entries carry tokens and ids only; provider errors are redacted with the live key. | `test_usage_ledger_file_never_contains_prompt_text`; `test_invalid_credential_is_reported_without_echoing_the_key` (server echoed the key, message was scrubbed). |
| Outbound redaction | Patterns for `api_key`/`password`/`secret`/tokens, bearer tokens, `sk-…`, PEM private keys, plus exact secret replacement. Applied in `OpenAIProvider._prepare_messages` and again in `ProviderChatBackend` for any remote endpoint. | `test_outbound_secrets_are_redacted_before_the_request` inspects the actual request body. |
| Request size bound | `max_request_chars` (200 000 default) refuses an oversized request locally. | Code path; no separate test. |
| Project-context control | GisCode does not attach a project automatically; the existing context assembler decides content; workspace confinement and the approval gate are unchanged. | `test_openai_provider.py::test_agent_planning_uses_provider_and_skips_legacy_routing` proves the provider receives only the assembled planner messages. Full-suite context/isolation tests also pass. |
| Project isolation | Provider config, consent, credentials and usage live under `GISCODE_HOME`; no shared cross-project state; no Pro modules. | `test_registry_defaults_keep_local_providers_and_openai_off`, `tests/test_core_independence.py`, `tests/test_import_boundary.py`. |
| Explicit authorization for paid actions | `paid_calls_allowed` defaults false; every network entry point raises `ProviderError` with a concrete action while off. | `test_billable_consent_is_required_before_listing_or_chat` asserts zero requests. |
| Usage and cost visibility | In-memory counters plus optional JSONL; GUI "Usage this process" line; cost only when input/output prices are configured. | `test_successful_mock_round_trip_uses_official_shape_and_records_usage`, `test_streaming_collects_text_and_usage`. |
| Safe disconnect / removal | "Remove saved key" deletes the DPAPI file; unchecking consent stops calls; switching to a local profile takes effect next request. | `test_windows_dpapi_round_trip` delete assertion; consent tests. |
| No subscription/cookie harvesting | No browser integration in this path; only an API key is accepted. ChatGPT subscription explicitly documented as not API access. | Code review of `openai.py`; `docs/OPENAI_DIRECT.md` §1. |
| Images stay local | Provider declares no vision capability; remote attachments require separate consent in `ProviderChatBackend`. | `provider_backend` refusal path; covered by the existing remote-attachment tests in the full suite (`test_remote_attachment_consent.py`). |

---

## 4. Agent communication behaviour

| Capability | Status |
| --- | --- |
| Send task context and engineering questions | Implemented: normal planner/implementation messages through `ModelRouter`. |
| Complete responses | Implemented: `/v1/chat/completions`, first choice content. |
| Streaming responses | Implemented: SSE with `stream_options.include_usage`; same event shape as local providers. |
| Continue a conversation | Implemented by the existing message history path; the provider is stateless and receives the assembled history. |
| Cancel safely | Implemented: pre-start cancel sends nothing; mid-stream cancel keeps partial text and labels it incomplete; a late non-streaming answer is discarded. |
| Timeout / interruption recovery | Explicit: idle and request limits are reported with the partial text kept; no silent retry. |
| Provider/model/request attribution | `ChatResponse.provider`, `.model`, `.request_id`, and the same request id in usage records. |
| Suggestions marked unverified | Enforced by workflow: planner output is parsed, edits are approved, verifier decides success. |
| Guidance used in planning/implementation | Implemented; proven by the planner test through the provider. |

---

## 5. Qualification matrix (mock/local)

Command:

```bat
python -m pytest -q -p no:cacheprovider tests/test_openai_provider.py tests/test_provider_streaming.py
```

Result: **49 passed** (15 OpenAI + 34 provider-streaming).

| Test | What it proves | External service |
| --- | --- | --- |
| `test_official_host_guard_rejects_other_remote_endpoints` | Non-official hosts are refused at construction. | None |
| `test_missing_credential_is_refused_before_any_network` | Missing key fails with zero requests. | None |
| `test_billable_consent_is_required_before_listing_or_chat` | Consent gate blocks list and chat; status is unavailable; zero requests. | None |
| `test_successful_mock_round_trip_uses_official_shape_and_records_usage` | Model list, chat shape, auth header, usage, cost math. | Loopback mock |
| `test_invalid_credential_is_reported_without_echoing_the_key` | 401 is clear; server echo and key are scrubbed. | Loopback mock |
| `test_timeout_is_reported_without_a_silent_retry` | One POST only; timeout is surfaced. | Loopback mock |
| `test_streaming_collects_text_and_usage` | SSE deltas, finish reason, token count, `stream_options`. | Loopback mock |
| `test_cancel_before_stream_sends_no_post` | Pre-start cancellation sends nothing. | Loopback mock |
| `test_outbound_secrets_are_redacted_before_the_request` | Key, password and bearer token absent from the wire body. | Loopback mock |
| `test_response_attributes_provider_model_and_request` | Provider, model and request id on the response. | Loopback mock |
| `test_usage_ledger_file_never_contains_prompt_text` | Ledger holds tokens, not prompts or keys. | Loopback mock |
| `test_registry_defaults_keep_local_providers_and_openai_off` | Ollama/LM Studio stay enabled; OpenAI off; construction is offline. | None |
| `test_configure_profile_persists_consent_without_a_secret` | Profile + consent persisted; no key in settings. | None |
| `test_agent_planning_uses_provider_and_skips_legacy_routing` | Real `CodingAgentLoop._run_planning` plans through the provider; legacy routing is never called. | Loopback mock |
| `test_windows_dpapi_round_trip` | Encrypt, read, restart-read, delete; no plaintext. | Windows DPAPI (local OS) |

The 34 streaming tests exercise the shared provider stream path used by local
providers and by OpenAI (cancellation, idle timeout, partial text, isolation).

---

## 6. Live communication evidence — none

- `OPENAI_API_KEY` is **not set** in this environment (checked without reading a
  value; none exists).
- No OpenAI API account or key was supplied for this mission.
- No request was sent to `api.openai.com` at any point. There is therefore no
  live round-trip, no live streaming sample, no live cancellation and no live
  latency measurement to report.
- A ChatGPT consumer session is not used, and was not used, as API
  authorization. No cookie, password, MFA code or subscription token was read.

To produce live evidence later, an authorized key must be present, consent
enabled, and a single small prompt sent deliberately. That run would need to be
recorded separately; the mock results above cannot substitute for it.

---

## 7. Regression results

| Suite | Command | Result |
| --- | --- | --- |
| Focused provider | `pytest -q tests/test_openai_provider.py tests/test_provider_streaming.py` | **49 passed**, exit 0 |
| GUI + edition + boundaries | `pytest -q tests/test_gui_*.py tests/test_core_independence.py tests/test_core_edition_export.py tests/test_import_boundary.py tests/test_core_source_inventory.py` | **250 passed, 12 skipped**, exit 0 |
| Full Core suite | `pytest -q -p no:cacheprovider` | **1804 passed, 1 failed, 38 skipped**, exit 1 (217 s) |
| Compile check | `python -m py_compile` on every changed module | exit 0 |

The single full-suite failure is pre-existing and unrelated:

```
tests/fixtures/audit/fixture1/fixture1_project/tests/test_stub.py::test_stub_handler_identifiable
ModuleNotFoundError: No module named 'giscode.gisai'
```

The fixture imports the optional Pro/GisAI package, which is absent from this
Core checkout by design. It was not weakened, skipped or edited. No new failure
appeared in any suite.

Environment note: the interpreter first on `PATH` here is Python 3.11 without
`tkinter`, so Tk tests cannot even collect under it. All results above were
produced with
`%LOCALAPPDATA%\Programs\Python\Python313\python.exe`
(Python 3.13, Tk available), which is the interpreter this checkout targets.

---

## 8. Remaining limitations

1. **No live OpenAI verification** — the central limitation; see section 6.
2. **No automatic retry** of a billable completion (intentional; documented).
3. **No TCP-level stream abort** — cancellation stops presentation and discards
   a late answer; the server may finish generating a request you cancelled.
4. **Cost estimates require user-supplied prices**; GisCode ships no price table.
5. **Redaction is pattern-based**, not a DLP guarantee.
6. **Vision is not enabled** for OpenAI; images remain local.
7. **Only OpenAI is implemented** as a remote provider. Anthropic, Gemini, xAI,
   Meta and DeepSeek are design targets, not code.
8. **Usage counters are per process** unless `usage_path` is configured; they are
   not a billing statement.
9. **Placeholder APIs that already existed** (Project Doctor) are unchanged
   and outside this mission.

---

## 9. Integrity

`docs/OPENAI_DIRECT_MANIFEST.sha256` contains SHA-256 digests for every file
added or changed by this mission, plus this report. Verify from the checkout
root with:

```bat
sha256sum -c docs/OPENAI_DIRECT_MANIFEST.sha256
```

No commit, push, release or publication was performed. The working tree keeps
all earlier mission changes intact.

---

## 10. Related documents

- [`OPENAI_DIRECT.md`](OPENAI_DIRECT.md) — setup, billing, security boundary,
  troubleshooting.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — provider gateway in the system.
- [`LOCAL_AI_SETUP.md`](LOCAL_AI_SETUP.md) — local providers.
- [`CHATGPT_BROWSER.md`](CHATGPT_BROWSER.md) — separate browser handoff.
