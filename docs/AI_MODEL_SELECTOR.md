# AI Model Selector

GisCode can install and run a coding model without Ollama, a terminal, a model
website or manual file placement. Open **Model Manager** and use the **AI Model
Selector** section.

## What the selector does

1. Detects system RAM, available RAM, GPU/VRAM and free disk space.
2. Recommends only a model that is both **qualified by GisCode** and eligible for
   the detected hardware. An unqualified model is never selected silently.
3. Shows the download size, quantization, context length, RAM/VRAM requirement,
   publisher, source, licence, commercial-use status and expected performance.
4. Downloads the model from its publisher, verifies the pinned SHA-256 and
   stores it in the shared model folder.
5. Downloads the pinned llama.cpp runtime for the selected backend (CUDA,
   Vulkan or CPU) on first load.
6. Loads the model through GisCode's existing `ModelManager` and tests it with a
   real inference request and coding-correctness check.

Expected performance is labelled as an estimate unless it is explicitly marked
as measured. A measured value belongs to the machine and runtime named in the
qualification record; it is not a promise for other hardware.

## Catalogue and qualification

| Model | Quantization | Minimum RAM | VRAM for full offload | Status |
| --- | --- | ---: | ---: | --- |
| Qwen2.5-Coder-1.5B-Instruct | Q4_K_M | 6 GB | 2 GB | **Qualified** |
| Qwen2.5-Coder-7B-Instruct | Q4_K_M | 9 GB | 6 GB | Not qualified |
| Qwen3-Coder-30B-A3B-Instruct | Q4_K_M | 24 GB | 18 GB | Not qualified |

All three are Apache-2.0. The selector opens the publisher's model page and the
licence page. The model file's SHA-256 is pinned in
`giscode/ai_module/manifest.py`; the runtime is pinned to llama.cpp `b12373`
and its official release assets.

A size, RAM or VRAM mismatch is reported before download/load. On this
qualification PC the 7B model was refused as too large for the memory available
at the time. The 30B model was not downloaded or run, so it must not be
described as hardware-tested.

## Download, cancel and resume

- **Download model** fetches the selected file only; it does not load it.
- Progress is shown in the panel and the shared status line.
- **Cancel** stops the transfer. Pressing **Download model** again resumes from
  the partial `.part` file.
- A completed file is checked against the expected size and SHA-256. A mismatch
  is deleted, never loaded.
- **Load / switch model** activates the downloaded model. **Test model** runs a
  real inference and coding check. **Remove model** deletes the model but leaves
  the runtime installed.

Removal is refused when the other installed GisCode edition still selects or
has verified the same model. The model cache is intentionally shared because
the files are immutable and digest-verified; runtime binaries and AI lifecycle
state are edition-scoped.

## Real coding work

The model is connected to GisCode's existing `CodingAgentLoop`, not a separate
chat sandbox. The loop can inspect the project, plan edits, write through the
guarded workspace, run project tests, repair failures and roll back a failed
task with recovery copies.

Measured on the qualification PC with Qwen2.5-Coder-1.5B-Instruct:

- A real single-file repair changed `maths.py` from subtraction to addition and
  passed all 4 project tests.
- A more ambitious two-file change was **not** completed by the 1.5B model. It
  tried to replace `app.py` with incomplete content; GisCode rejected the
  invalid edit, rolled both files back and retained recovery copies.
- Planning and generation are not deterministic. A request that the model
  cannot complete must be reported as failed, never as a successful edit.

The 1.5B model is a starter for small, focused changes. Multi-file work needs a
stronger qualified model; the 7B and 30B entries remain unqualified until they
pass real inference and coding checks on supported hardware.

## Storage

- Models: `~/.giscode/models` (shared, immutable, digest-verified)
- Core runtime: `~/.giscode/ai/core/runtime`
- Pro runtime: `~/.giscode/ai/pro/runtime`
- Core AI state: `~/.giscode/ai/core/ai_module.json`
- Pro AI state: `~/.giscode/ai/pro/ai_module.json`

Inference listens on `127.0.0.1` only. The model and runtime are not uploaded
anywhere.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Download interrupted | Press **Download model** again; it resumes the partial file. |
| SHA-256 mismatch | GisCode deletes the bad file. Download again; do not reuse the file. |
| "No qualified model fits" | Free memory/disk, or select a model explicitly and run Test. |
| Load reports too large | Close other applications or choose a smaller model. |
| Test fails | Read the message; the runtime, model file or coding check is named. |
| Model marked not qualified | It may be selected deliberately, but GisCode will not call it ready without a real test. |
| Other edition still uses model | Remove it from that edition first; the shared file is protected. |

The runtime panel reports the resolved backend. CUDA requires a compatible
NVIDIA driver; Vulkan is used for supported AMD/Intel/NVIDIA devices when
available; CPU is the portable fallback.
