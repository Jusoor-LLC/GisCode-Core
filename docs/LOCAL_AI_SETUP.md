# Connecting a local AI model to GisCode Core

GisCode Core runs a model on your own machine. This guide explains exactly how
that works in this version, what is supported, and what is not — including the
two providers people most often ask about, **Ollama** and **LM Studio**.

Read the short version first, because it prevents most wasted time:

> GisCode Core is **not** a generic OpenAI-API client. It does not take an
> endpoint URL and it does not talk to another app's server. It runs its **own
> `llama-server`** (from llama.cpp) as a child process, loads a **GGUF** file
> into it, and talks to that server over loopback.

Everything else in this guide follows from that one design decision.

---

## 1. The three moving parts

| Part | What it is | Who supplies it |
| --- | --- | --- |
| **Model file** | A `.gguf` file containing the weights | You |
| **`llama-server`** | The llama.cpp binary that runs the file and answers requests | You (or LM Studio's bundled copy) |
| **GisCode** | Starts the server, sends prompts, applies edits, runs tests | This repository |

GisCode does not download models, and it does not bundle a server. It finds
both, starts the server on a loopback port, waits for the server's `/health`
endpoint to answer, and then sends your prompts to it.

---

## 2. What "supported" means here

Supported today:

- **GGUF model files**, for text chat and coding models.
- **GGUF vision models plus their companion projector file** (`mmproj`), for
  attachment analysis.
- Any `llama-server` build you point it at, including the one LM Studio ships.

Not supported in this version:

- **Ollama.** See [section 3](#3-ollama--not-supported).
- **Connecting to LM Studio's local server API.** See
  [section 4](#4-lm-studio--supported-through-its-files-not-its-api).
- **safetensors, PyTorch `.bin`, ONNX, GGML (non-GGUF) or GPTQ files.**
- **Remote or hosted inference endpoints.** A hosted writer exists only in the
  Pro edition and is disabled by default; it is not part of Core.

If you were expecting to paste `http://localhost:11434` or
`http://localhost:1234` into a settings field: there is no such field in Core.
That is a design choice, not a missing screen.

---

## 3. Ollama — not supported

**GisCode Core does not connect to Ollama.** This is explicit in the project's
own architecture notes, which state that GisCode must not depend on Ollama and
that inference goes through `llama-server` / llama.cpp directly.

What this means in practice:

- Pointing GisCode at Ollama's API port (`11434`) does nothing — there is no
  setting for it, and no code path that speaks Ollama's API.
- Models you pulled with `ollama pull` are stored in Ollama's own blob format
  inside `~/.ollama/models`, not as `.gguf` files in a folder GisCode scans. They
  will not appear in GisCode's model list.
- Installing Ollama is not harmful, but it does not make GisCode work.

If you want to use a model you first tried in Ollama, download the **GGUF**
version of that model from its publisher and follow
[section 5](#5-put-the-model-where-giscode-looks). Genuine Ollama support would
require a new provider adapter in the application; it is not something this
documentation changes, and it is not available in v0.1.0.

---

## 4. LM Studio — supported through its files, not its API

LM Studio is **partly** useful with GisCode, in two specific ways. It is not
supported as a server that GisCode connects to.

### What works

1. **Model files.** GisCode scans LM Studio's model folders, so GGUF models you
   downloaded in LM Studio show up in GisCode automatically:
   - `~/.lmstudio/models`
   - `C:\Users\Public\lmstudio\models`

2. **The llama.cpp backend.** LM Studio ships `llama-server` binaries under
   `~/.lmstudio/extensions/backends`, and GisCode searches there. If you have LM
   Studio installed, you may already have everything you need.

### What does not work

- Starting LM Studio's own local server and expecting GisCode to use it. GisCode
  does not connect to an OpenAI-compatible endpoint such as
  `http://localhost:1234/v1`, and it has no field for one.
- LM Studio's chat UI, presets, or per-model settings. GisCode loads the file
  itself with its own launch options.

### Option A — reuse LM Studio's backend

If LM Studio is installed with at least one backend downloaded:

1. Launch GisCode (`py -3.13 -m giscode.main`).
2. Open **Model Manager** and read the backend note. It names the backend
   GisCode resolved.
3. If it reports no server, point GisCode at the binary explicitly. Find it
   under `~/.lmstudio/extensions/backends` (the folder is nested a level or two
   deep, and there may be several builds):

   ```bat
   dir /s /b "%USERPROFILE%\.lmstudio\extensions\backends\llama-server.exe"
   ```

4. Set the path for the current session, then relaunch:

   ```bat
   set GISCODE_LLAMA_SERVER_PATH=%USERPROFILE%\.lmstudio\extensions\backends\<build>\llama-server.exe
   py -3.13 -m giscode.main
   ```

5. Confirm the binary runs on its own:

   ```bat
   "%GISCODE_LLAMA_SERVER_PATH%" --version
   ```

### Option B — install llama.cpp separately

Download a Windows build of llama.cpp, then either put `llama-server.exe` on
`PATH` or set `GISCODE_LLAMA_SERVER_PATH` to its full path as above. This is the
more predictable option, because an LM Studio update can move or replace its
bundled backend.

---

## 5. Put the model where GisCode looks

GisCode discovers `.gguf` files in:

| Folder | Notes |
| --- | --- |
| `~/.giscode/models` | Create it yourself for a GisCode-only library. |
| `~/.lmstudio/models` | Populated by LM Studio downloads. |
| `C:\Users\Public\lmstudio\models` | LM Studio's shared folder. |

To use a different folder, set `paths.models_dir` in `~/.giscode/config.json`.
Restart GisCode after changing it.

Things that stop a model from appearing:

- It is not a `.gguf` file (safetensors and `.bin` files are ignored).
- It is in a nested LM Studio author folder that GisCode's scan reaches, but the
  file itself is a split shard you did not complete.
- It contains unexpected special characters in the filename.
- You added the file while GisCode was running and did not restart.

For vision models, GisCode also needs the matching **`mmproj`** projector file in
the same folder. Without it, the model loads for text but attachment analysis
reports that vision is unavailable.

---

## 6. Choose and load the model

1. Start GisCode.
2. In the header, open the **Model** dropdown. It lists `Auto` plus every model
   GisCode discovered.
3. Pick the model you want, or leave `Auto` to let GisCode choose.
4. Open **Model Manager** and click **Load selected model**.
5. Watch the status line. Loading a multi-gigabyte model the first time can take
   minutes; the status updates as it goes.

![Model Manager with a model loaded](images/model-manager.png)

*Model Manager after loading a model. The status line names the model and its
state; the note line reports facts about the machine, such as CPU-only loading,
which are not errors.*

6. The window title changes to `Loaded: <model name>` when it is ready.
7. **Unload current model** releases it without closing GisCode — useful when
   switching to a larger model.

If the load fails, the error appears in red under the status line. The most
common causes are a model larger than your memory, an incomplete download, or a
server binary that could not start.

---

## 7. Test that GisCode can actually talk to the model

A model that appears in the dropdown is not proof that inference works. Verify
what this build can verify:

1. In **Model Manager**, click **Load selected model**.
2. Expect the status line to name the model and its state, for example
   `Qwen3-VL-4B-Instruct-Q4_K_M (Ready) [n_ctx=16384]`, and the window title to
   start with `Loaded:`.
3. Confirm the log shows the server starting and the model loading:

   ```bat
   type "%USERPROFILE%\.giscode\logs\giscode.log"
   ```

   Look for `Model load succeeded` and a line naming the execution device (GPU
   or CPU).

That is a completed, verified load: GisCode resolved a backend, started its own
`llama-server`, and the server answered `/health` with a usable model.

**What is not a reliable test in this build:**

- The **Planer** (Projects tab) is where Core actually sends a request to the
  model, but planning was slow or did not return an answer during testing for
  this documentation. Treat a plan reply as a bonus, not the acceptance check.
- The **Chat** tab refuses general questions with "GisCode Core cannot carry out
  that request without GisAI." That message is about the missing GisAI engine,
  not about your model.

If the model never becomes ready:

- Check the Model Manager status for a load error.
- Check the newest file in `~/.giscode/logs` for the server start line and any
  `/health` timeout.
- Re-check `llama-server --version` with the same binary path GisCode uses.

There is no separate "test connection" button, because there is no connection to
configure: GisCode starts the server itself.

---

## 8. Endpoints, ports and firewalls

- The server GisCode starts listens on **loopback** (`127.0.0.1`) only. It is not
  reachable from other machines.
- The port is chosen by GisCode. You do not need to know it, open it, or forward
  it.
- **No firewall rule is required for normal use**, and you should not add an
  inbound exception for GisCode. If a security product blocks the child process
  from starting, allow the specific `llama-server.exe` you installed rather than
  disabling the firewall.
- Readiness timeout: GisCode waits for the server's `/health` endpoint, and gives
  up after `GISCODE_LLAMA_READY_TIMEOUT` seconds (default 60). Large models on a
  slow disk can exceed that; see
  [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#the-window-opens-but-no-model-can-be-loaded).

---

## 9. Does it need the internet?

| Activity | Internet needed? |
| --- | --- |
| Installing GisCode's Python dependencies | Yes, once, from PyPI |
| Downloading a model or llama.cpp build | Yes, once, from wherever you get them |
| Installing Ollama | Not applicable — it is not used |
| Running GisCode and chatting with a local model | **No** |
| Attachment analysis with a local vision model | **No** |

Once the pieces are on disk, GisCode works with the network disconnected.

---

## 10. Related reading

- [`INSTALLATION.md`](INSTALLATION.md) — getting Python, the server and the app
  in place.
- [`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md) — formats, quantization,
  memory and model choice.
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — when the server or model will not
  cooperate.
