# GisCode Architecture

## Overview

GisCode is local-first: by default, planning, inference, execution and verification all run on the host machine through a user-supplied `llama-server`, and no cloud service is required. An **optional** hosted writer can send planner/writer calls to a remote endpoint instead of the local model; it is disabled by default, needs an API key that the user supplies, and is never used unless it is switched on in Settings (`config/settings.py`).

Two optional remote routes exist, and they are deliberately separate. The **official OpenAI API provider** (`giscode/providers/openai.py`) is disabled by default, refuses to open a socket until the user enables billable calls, reads the key from `OPENAI_API_KEY` or a Windows DPAPI-protected store, and is documented in [`OPENAI_DIRECT.md`](OPENAI_DIRECT.md). **ChatGPT plan usage** (`giscode/providers/chatgpt_auth.py`, `chatgpt_plan.py`) is a different authentication and billing path: dynamic client registration, system-browser OAuth with PKCE, a loopback callback, ID-token validation, and `POST /v1/responses` with `store:false` and `stream:true`; it is documented in [`CHATGPT_PLAN.md`](CHATGPT_PLAN.md). Apart from these explicit opt-ins, inference stays on the machine.

## Core Design Principles

1. **Local-first inference** — GisCode owns its model/runtime management through `InferenceAPI` and `ModelManager`.
2. **No Ollama dependency** — The system uses `llama-server.exe` / `llama.cpp` directly via subprocess or Python runtime bindings.
3. **Sequential model loading** — Planning models are loaded, used, and unloaded before the next model is loaded.
4. **Workflow completeness** — The intended lifecycle is `idea → planning → specification → coding → terminal/tools → build → test → repair → verification`. Packaging is partly implemented (a PyInstaller recipe is included); the iOS build path is experimental and needs macOS with Xcode.
5. **Safety boundaries** — All execution is confined to workspaces, with explicit path validation, timeout caps, and output limits.

## System Components

### 1. Model Management

| Component | Responsibility |
|-----------|----------------|
| `InferenceAPI` | Owns the inference runtime, model loading/unloading, health checks, and generation requests. |
| `ModelManager` | Discovers GGUF files, registers models, estimates resource requirements, and enforces the resource budget. |
| `ModelRegistry` / `ModelMetadata` | Catalog of known and discovered models with paths, sizes, quantization, and status. |
| `DefaultResourceBudget` | Enforces RAM/VRAM constraints before loading. Prevents OOM by design. |

**Key rules:**
- Models are discovered from `~/.giscode/models`, `~/.lmstudio/models`, and `C:/Users/Public/lmstudio/models`.
- Only GGUF formats are supported.
- `gpu_layers` and `vram_budget_bytes` are configurable but default to CPU-only safe values.
- Any GGUF coder model the machine can run may be used. **Qwen3-Coder** is the model this project is developed and tested against; GisCode neither bundles nor downloads models.
- Planning models are loaded sequentially and unloaded before the next model.

### 2. Agentic Coding Loop

| Component | Responsibility |
|-----------|----------------|
| `CodingAgentLoop` | Top-level orchestrator for autonomous coding tasks. |
| `Planner` | Converts user requests into structured `ExecutionPlan` JSON via the real `InferenceAPI`. |
| `WorkspaceActionExecutor` | Executes `CREATE` / `MODIFY` / `READ` actions with workspace path validation. |
| `Verifier` | Verifies generated files exist, parse, and (for Python) executes the generated test suite via `CommandExecutor`. |
| `TaskResult` / `TaskRequest` | Structured task lifecycle data. |

**Workflow:**
```
INTAKE → PLANNING → IMPLEMENTATION → VERIFICATION → (REPAIR × 2) → COMPLETE/FAILED
```

### 3. Terminal / Tools

| Component | Responsibility |
|-----------|----------------|
| `CommandExecutor` | Safe subprocess execution bounded to the workspace root. Enforces timeouts, output caps, and environment isolation. |
| `SandboxExecutor` | Optional Docker-backed sandbox for untrusted builds/tests. Falls back to unavailable when Docker is absent. |

**Safety:**
- Commands run with `cwd` locked to the workspace root.
- `CREATE_NO_WINDOW` is set on Windows.
- Output is capped at 1 MB stdout / 1 MB stderr.
- Timeouts are enforced per request.

### 4. Knowledge & Learning

| Component | Responsibility |
|-----------|----------------|
| `KnowledgeService` | Persistent store of `KnowledgeRecord` entries (fixes, failures, patterns). Supports lexical and vector retrieval. |
| `SelfImprovementService` | Records `FailFixPair` events, computes fingerprints, and exports learning datasets. |
| `RetrievalBackend` | Pluggable retrieval (baseline lexical, vector). |

### 5. Research & Vision

| Component | Responsibility |
|-----------|----------------|
| `ResearchService` | Controlled external research with timeout, cancellation, and provider abstraction (mock / real). |
| `OCRService` | Vision/OCR pipeline with preprocessing, engine abstraction, and task-control integration. |

### 6. Routing & Context

| Component | Responsibility |
|-----------|----------------|
| `RoutingService` | Selects the appropriate model/role for a request and assembles context bundles within budget. |
| `ContextAssembler` | Pulls knowledge records into model context without exceeding token budgets. |
| `ModelRouter` | Deterministic routing policies with fallback. |

### 7. IDE & OpenHands

| Component | Responsibility |
|-----------|----------------|
| `OpenHandsAdapter` | Bridges GisCode agentic actions to the OpenHands IDE runtime. |

### 8. Packaging

| Component | Responsibility |
|-----------|----------------|
| Packaging config | Outputs to `dist/` and `build/`. Supports signing, timestamping, and artifact staging. |

### 9. GUI

| Component | Responsibility |
|-----------|----------------|
| `main_window.py` | Tk-based desktop interface. |
| `giscode/gui/` | GUI package. |

### 10. Task Control

| Component | Responsibility |
|-----------|----------------|
| `TaskManager` | Tracks active tasks, phases, cancellation, and timeout. |
| `TaskPhase` / `TaskStatus` | Enumerations for lifecycle tracking. |

### 11. Provider Gateway and Remote AI

| Component | Responsibility |
|-----------|----------------|
| `ProviderRegistry` | Reads provider settings, builds live provider objects, injects credentials. |
| `Provider` subclasses (`OllamaProvider`, `LMStudioProvider`, `OpenAICompatibleProvider`, `OpenAIProvider`) | One remote/local API each; the registry does not special-case any of them in the agent loop. |
| `OpenAIProvider` | Official OpenAI API only (`https://api.openai.com`), billable-call consent, model listing, blocking and streaming chat, usage accounting, redacted errors. |
| `ChatGPTAccountStore` | Sign in with ChatGPT: dynamic client registration, PKCE/state/nonce, loopback callback, RS256 ID-token validation, JWKS caching, protected per-account token storage, serialized refresh rotation, revocation. |
| `ChatGPTPlanProvider` | `POST /v1/responses` with `store:false`, `stream:true`; plan model discovery, streaming, cancellation, plan error codes; never an API-key fallback. |
| `ProviderChatBackend` | Presents a provider model through the `InferenceAPI` surface: health, model status, chat, streaming, per-request cancellation, attribution, remote-attachment consent. |
| `CredentialStore` implementations | Environment, in-memory (tests) and Windows DPAPI protected-file stores; status is always masked. |
| `UsageLedger` | Per-process request/token counters and an optional JSON Lines record with no prompt text. |
| `redaction` | Last-boundary removal of known secrets and credential-shaped text for remote providers. |
| `ModelRouter.is_provider_backend` | Tells `CodingAgentLoop` to skip legacy GGUF loading when a provider owns the model. |

**Rules:**
- A provider is selected by an ordinary purpose profile, exactly like a local one; switching back to a local profile takes effect on the next request.
- A remote, billable provider performs no network request until the user enables it. Consent and credentials are separate decisions.
- ChatGPT plan usage and OpenAI API usage are different authentication and billing paths. Neither falls back to the other, and their credentials are never mixed.
- No credential is written to `config.json`, a log, a usage record or an error message.
- A provider's answer is unverified input: planning still parses it, edits still pass the approval gate, and the verifier still decides whether the task succeeded.
- Workspace confinement and the approval gate apply unchanged; a remote model cannot widen Core's permissions.

## Data Flow

```
User Request
    ↓
TaskManager.start_task()
    ↓
Planner.create_plan() → InferenceAPI → ModelManager → llama-server
    ↓
ExecutionPlan (JSON)
    ↓
WorkspaceActionExecutor → files written to Workspace
    ↓
Verifier.verify() → CommandExecutor → unittest / build / tests
    ↓
[PASS] → COMPLETE
[FAIL] → Repair Loop (bounded ×2)
    ↓
COMPLETE / FAILED / CANCELLED / TIMED_OUT
```

The same lifecycle runs through a provider profile; only the model transport
changes:

```
TaskManager.start_task()
    ↓
Planner.create_plan() → ModelRouter → ProviderChatBackend → provider API
    ↓
ExecutionPlan (JSON) → approval → WorkspaceActionExecutor → Verifier
```

## Safety Boundaries

- **No unrestricted shell execution** — All commands go through `CommandExecutor` or `SandboxExecutor`.
- **Workspace confinement** — `WorkspaceActionExecutor` rejects paths outside the workspace root.
- **Resource budget** — `DefaultResourceBudget` prevents loading models that exceed available RAM/VRAM.
- **Cancellation & timeout** — `TaskManager` and `CommandExecutor` support cooperative cancellation.
- **Output caps** — stdout/stderr are truncated to prevent memory exhaustion.
- **Local by default, remote only on request** — Inference is local unless the user enables a remote provider. The official OpenAI provider is opt-in and billable, sends nothing while consent is off, keeps the key out of settings and logs, and redacts credential-shaped text at the outbound boundary.
- **No paid call without consent** — Billable provider calls require an explicit, persisted user setting; failures are surfaced once and are not retried automatically, to avoid duplicate charges.
- **Acceptance automation is build-gated** — the GUI task driver behind `GISCODE_ACCEPTANCE_*` exists only in the purpose-built acceptance executable, requires the explicit `GISCODE_ACCEPTANCE_OPT_IN=1`, and writes its evidence only inside a dedicated temporary directory. An ordinary Core launch ignores inherited acceptance variables entirely and never submits a task.
- **Workspace and evidence confinement** — acceptance evidence paths are bare file names resolved inside `%TEMP%/giscode_acceptance`; absolute paths, drive letters, separators and `..` are rejected.

## Model Strategy

- **Primary coding model:** any GGUF coder model the machine can run; Qwen3-Coder (MoE, coding-specialized) is the tested default.
- **Planning/spec models:** Loaded sequentially from local GGUF pool.
- **Vision/OCR models:** Loaded on demand via `OCRService`.
- **Fallback:** If a model cannot be loaded due to budget, the system reports `InsufficientResourceBudgetError` rather than crashing.

## Not Yet Implemented

- **Remote providers other than OpenAI.** The gateway accepts them as new `Provider` subclasses, but Anthropic, Gemini, xAI, Meta and DeepSeek have no implementation in this build. See [`OPENAI_DIRECT.md`](OPENAI_DIRECT.md#9-adding-the-next-provider).
- The **Project Doctor** tab is a placeholder: `giscode/gui/main_window.py` builds the tab and states that no Project Doctor service is connected in this build.
- iOS build and signing is experimental and depends on external tools (xtool, Xcode).

## Non-Negotiables

- GisCode must NOT depend on Ollama.
- GisCode must own its local model/runtime management.
- No credential may be written to `config.json`, logs, usage records or error text.
- No billable provider request may be made without explicit user consent.
- Planning models are loaded sequentially and unloaded before the next model.
- Models are user-supplied; Qwen3-Coder is the tested default.
