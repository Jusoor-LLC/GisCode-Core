# GisCode Architecture

## Overview

GisCode is local-first: by default, planning, inference, execution and verification all run on the host machine through a user-supplied `llama-server`, and no cloud service is required. An **optional** hosted writer can send planner/writer calls to a remote endpoint instead of the local model; it is disabled by default, needs an API key that the user supplies, and is never used unless it is switched on in Settings (`config/settings.py`).

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

## Safety Boundaries

- **No unrestricted shell execution** — All commands go through `CommandExecutor` or `SandboxExecutor`.
- **Workspace confinement** — `WorkspaceActionExecutor` rejects paths outside the workspace root.
- **Resource budget** — `DefaultResourceBudget` prevents loading models that exceed available RAM/VRAM.
- **Cancellation & timeout** — `TaskManager` and `CommandExecutor` support cooperative cancellation.
- **Output caps** — stdout/stderr are truncated to prevent memory exhaustion.
- **No cloud dependency** — All inference is local. No data leaves the machine.
- **Acceptance automation is build-gated** — the GUI task driver behind `GISCODE_ACCEPTANCE_*` exists only in the purpose-built acceptance executable, requires the explicit `GISCODE_ACCEPTANCE_OPT_IN=1`, and writes its evidence only inside a dedicated temporary directory. An ordinary Core launch ignores inherited acceptance variables entirely and never submits a task.
- **Workspace and evidence confinement** — acceptance evidence paths are bare file names resolved inside `%TEMP%/giscode_acceptance`; absolute paths, drive letters, separators and `..` are rejected.

## Model Strategy

- **Primary coding model:** any GGUF coder model the machine can run; Qwen3-Coder (MoE, coding-specialized) is the tested default.
- **Planning/spec models:** Loaded sequentially from local GGUF pool.
- **Vision/OCR models:** Loaded on demand via `OCRService`.
- **Fallback:** If a model cannot be loaded due to budget, the system reports `InsufficientResourceBudgetError` rather than crashing.

## Not Yet Implemented

- The **Project Doctor** tab is a placeholder: `giscode/gui/main_window.py` builds the tab and states that no Project Doctor service is connected in this build.
- iOS build and signing is experimental and depends on external tools (xtool, Xcode).

## Non-Negotiables

- GisCode must NOT depend on Ollama.
- GisCode must own its local model/runtime management.
- Planning models are loaded sequentially and unloaded before the next model.
- Models are user-supplied; Qwen3-Coder is the tested default.
