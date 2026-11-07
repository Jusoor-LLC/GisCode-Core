# Model compatibility, memory and model choice

This guide explains which model files GisCode Core can load, how much memory a
model really needs, and how to pick one that fits your machine.

Numbers here are **estimates to plan with, not guarantees**. Actual memory use
depends on the model architecture, the quantization, the context length you use,
the runtime build, and how much of the model is offloaded to a GPU.

---

## 1. Which formats GisCode can load

| Format | Supported | Notes |
| --- | --- | --- |
| **GGUF** (text / chat / coder) | Yes | The only model format GisCode loads. |
| **GGUF + `mmproj`** (vision) | Yes | Attachment analysis needs the matching projector file next to the model. |
| safetensors | No | Typical for PyTorch/Hugging Face repositories. Convert to GGUF first. |
| PyTorch `.bin`, `.pt`, `.pth` | No | Not read by GisCode. |
| ONNX | No | Not read by GisCode. |
| GGML legacy (non-GGUF) | No | Superseded format. |
| GPTQ / AWQ / EXL2 | No | These are formats for other runtimes, not GGUF. |
| Ollama's internal blobs | No | See [`LOCAL_AI_SETUP.md`](LOCAL_AI_SETUP.md#3-ollama--not-supported). |

GisCode does not convert models. If a publisher only offers safetensors, you need
a GGUF build of the same model — most popular models have one.

### Where files must be

`.gguf` files are discovered only in these folders:

- `~/.giscode/models`
- `~/.lmstudio/models`
- `C:\Users\Public\lmstudio\models`

or in the folder set by `paths.models_dir` in `~/.giscode/config.json`.

### Vision models

A vision model has two parts: the model itself and a projector (`mmproj`) file.
Both must be GGUF, and both must be present. With the model but without the
projector, chat works and **Analyze Attachment** reports that vision is
unavailable. That is expected behaviour, not a bug.

---

## 2. Quantization: the one setting that matters most

Quantization compresses the model's weights. The filename usually carries the
level, for example `Qwen3-8B-Q4_K_M.gguf`.

| Quant | Bits per weight (approx.) | Quality | When to use it |
| --- | --- | --- | --- |
| `Q8_0` | ~8.5 | Near-original | You have memory to spare and want the best quality. |
| `Q6_K` | ~6.6 | Very good | A comfortable middle ground for larger machines. |
| `Q5_K_M` | ~5.7 | Good | Slightly better than Q4 at a modest cost. |
| **`Q4_K_M`** | **~4.8** | **Good, the usual default** | **Start here for almost everything.** |
| `Q4_0` / `IQ4_XS` | ~4.5 | Fair | Tight memory; prefer `IQ4_XS` over `Q4_0` at the same size. |
| `Q3_K_M` | ~3.9 | Noticeably degraded | Only when nothing larger fits. |
| `Q2_K` | ~3.0 | Poor, often incoherent for coding | Last resort; prefer a smaller model at Q4. |

The practical rule: **a smaller model at Q4_K_M beats a larger model at Q2_K.**
Quantizing harder to fit a bigger model rarely pays off for coding tasks.

### Estimating weight size

```
weights in GB  ≈  parameters in billions × bits per weight ÷ 8
```

| Model size | Q4_K_M weights | Q5_K_M weights | Q8_0 weights | F16 weights |
| --- | --- | --- | --- | --- |
| 3–4B | ~2.0–2.5 GB | ~2.5–3.0 GB | ~3.5–4.5 GB | ~7–8 GB |
| 7–8B | ~4.5–5.0 GB | ~5.5–6.0 GB | ~7.5–8.5 GB | ~14–16 GB |
| 13–14B | ~8–9 GB | ~10–11 GB | ~14–15 GB | ~26–28 GB |
| 24–32B | ~15–20 GB | ~18–24 GB | ~26–34 GB | ~48–64 GB |

Add the runtime's overhead and the KV cache (next section) on top of this.

---

## 3. Context length is a memory cost, not a free setting

Every token of context the model holds costs memory in the KV cache, and the
cost grows linearly with the context window.

Rough guide for a 7–8B model with grouped-query attention at 16-bit cache:

| Context | KV cache (approx.) |
| --- | --- |
| 4K tokens | ~0.5 GB |
| 8K tokens | ~1 GB |
| 32K tokens | ~4 GB |
| 128K tokens | ~16 GB |

Larger models, more layers, or larger head counts multiply this. Some builds
support a quantized KV cache, which roughly halves it at a small quality cost.

Practical consequences:

- A model that loads fine at 4K context can fail at 32K on the same machine.
- Long chats and large attachments consume context. If replies start failing
  after a long session, start a new chat before assuming the model is broken.
- GisCode reports the context window it loaded; the window title shows it with
  the model name.

---

## 4. GPU, CPU, and partial offload

- **GPU (VRAM)** is much faster. If the model and its KV cache fit in VRAM, you
  get interactive speeds.
- **CPU** works, and is the fallback when there is no supported GPU. Expect
  several times slower generation; a 7B Q4 model on a modern desktop CPU is
  usable for chat and slow for agent loops.
- **Partial offload** puts some layers on the GPU and the rest in system RAM. It
  is often the best compromise: considerably faster than pure CPU, and it lets a
  model larger than VRAM run at all.
- **System RAM** matters even with a GPU: the loader needs room to page weights in.

GisCode decides what it can and cannot offload based on the backend it found. If
it loads a model CPU-only, it says so in Model Manager as a grey note. That is a
statement about your machine, not an error.

---

## 5. What happens when the model does not fit

Symptoms, roughly in order of severity:

1. **Slow start.** The loader pages weights from disk; a model that barely fits
   can take minutes to become ready.
2. **Load failure with an allocation error.** The server exits; Model Manager
   shows a load error.
3. **Loads, then very slow generation.** Weights overflow into system RAM and
   every token pays for it.
4. **System-wide thrashing.** The machine becomes unresponsive because the OS is
   swapping. Close GisCode and pick a smaller model or a smaller context.
5. **Crash or silent exit of the server process.** Check `~/.giscode/logs`.

When any of these happens, change one variable at a time: a lower quant, then a
smaller model, then a shorter context.

---

## 6. Choosing a model that fits

Work through this in order:

1. **Find your memory.** Windows Task Manager → Performance → GPU shows
   *Dedicated GPU memory*; the same tab's Memory panel shows system RAM.
2. **Reserve headroom.** Leave at least 1–2 GB of VRAM for the desktop and
   display, and 2–4 GB of system RAM for Windows and applications.
3. **Budget = (VRAM, or RAM if no GPU) − headroom − KV cache.**
4. **Pick the largest quantized model that fits the budget**, preferring
   `Q4_K_M`. If the model you wanted needs more, drop one size class instead of
   dropping below Q4.
5. **Start with a small context** (4K–8K) to confirm the setup works, then raise
   it only if you need long sessions.

### Worked example

A laptop with 8 GB VRAM and 16 GB RAM, using `Q4_K_M`:

- 1.5 GB reserved for the display → ~6.5 GB usable.
- At 8K context a 7–8B model needs ~5 GB of weights plus ~1 GB of cache →
  about 6 GB. It fits, with little room to spare.
- A 13–14B model at Q4 needs ~9 GB of weights. It does not fit in VRAM; it would
  run partly on the CPU, or fail. For this machine, a 7–8B coder model at
  `Q4_K_M` is the sensible choice.

### A cheap way to evaluate a model

Do this before trusting any model in real work:

1. Load it and let it become ready (window title starts with `Loaded:`).
2. In **Projects → Planer**, give it one small, well-scoped request against a
   throwaway folder.
3. Watch how long the plan takes and whether it names the right file and the
   right change. If no plan appears at all, the model is too weak or too slow for
   this build's planning path — try a smaller, stronger coder model.
4. Try a second, slightly larger request.
5. Keep the model only if the plans are usable; a model that chats fluently can
   still plan badly.

This mirrors what the project's own notes say: model quality is the single
biggest limit on GisCode's usefulness, and small models often fail at agentic
work even when they chat fluently.

---

## 7. Special cases

- **Coding models.** Prefer models trained for code for project work; a general
  chat model is fine for questions.
- **Vision models.** Need the `mmproj` companion file. Vision models are also
  usually slower per token than their text-only equivalents.
- **"Thinking" / reasoning models.** Some produce long internal reasoning before
  answering, which costs time and context. If a model seems to hang, it may be
  reasoning; try a non-reasoning variant.
- **Mixture-of-experts models.** Total parameter count overstates what must sit
  in VRAM, but the file size still bounds disk and RAM use. Let the file size,
  not the parameter count, drive your planning.
- **Very long filenames or unusual characters** can prevent discovery. Rename to
  plain ASCII if a model will not appear.

---

## 8. Related reading

- [`LOCAL_AI_SETUP.md`](LOCAL_AI_SETUP.md) — where GisCode looks for files and
  servers, and how to test that inference works.
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#the-window-opens-but-no-model-can-be-loaded)
  — when a model will not load at all.
- [`USER_GUIDE.md`](USER_GUIDE.md) — loading and switching models in the GUI.
