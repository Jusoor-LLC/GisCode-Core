# Quick start (Windows)

This is the shortest path from a clean checkout to the GisCode Core window. It
uses the supported Python version and the repository's own dependency files.
There is no installer, no packaged binary and no bundled model.

## 1. Check Python and Tk

```bat
py -3.13 --version
py -3.13 -c "import tkinter; print('Tk', tkinter.TkVersion)"
```

Use Python 3.13 with tkinter. If the second command fails, re-run the
python.org installer and enable **tcl/tk and IDLE**.

## 2. Clone and create an isolated environment

```bat
git clone https://github.com/Jusoor-LLC/GisCode-Core.git GisCode
cd GisCode
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The repository does not require a system-wide install. Activate the environment
with `.venv\Scripts\activate` if you prefer, or call `.venv\Scripts\python.exe`
directly.

## 3. Check the GUI without loading a model

```bat
.venv\Scripts\python.exe -m giscode.main --gui-check
```

A successful check ends with `GUI_CHECK_OK` and lists the Core tabs:
Chat, Projects, Model Manager, Settings and AdSpace. A failure prints
`GUI_CHECK_FAIL: ...` and exits non-zero; see
[Troubleshooting](TROUBLESHOOTING.md).

## 4. Supply a local model server

GisCode does not download a model. Install a `llama.cpp` `llama-server` build
and place a GGUF chat/coder model where GisCode can find it:

- Put `llama-server.exe` on `PATH`, or set
  `GISCODE_LLAMA_SERVER_PATH` to the executable.
- Put GGUF files under `%USERPROFILE%\.giscode\models`, or select an existing
  LM Studio model folder through discovery.
- Choose the model in **Model Manager** before starting a chat.

The first load can take time because the server must map the model into memory.
An absent server or model is reported in the window; it is not silently treated
as a working model.

## 5. Start GisCode

```bat
.venv\Scripts\python.exe -m giscode.main
```

The window opens first and model autoload is deferred. Use **Chat** for a
conversation, **Projects** to open a folder, and **Model Manager** to load or
unload a model.

## Isolated experiments

Set `GISCODE_HOME` to a disposable folder to keep configuration, history, logs
and snapshots out of your real profile:

```bat
set GISCODE_HOME=%TEMP%\giscode-profile
.venv\Scripts\python.exe -m giscode.main
```

`GISCODE_TEST_MODE=1` additionally limits discovery and disables startup model
autoload. Use it for tests and reproducibility checks, not for normal work.

## Upgrade and removal

- **Upgrade:** stop GisCode, `git pull`, then reinstall the requirements with
  the same virtual environment.
- **Remove the application:** delete the checkout and its `.venv` directory.
- **Remove local data:** only after backing up anything you need, remove the
  profile directory (`%USERPROFILE%\.giscode`, or the `GISCODE_HOME` directory
  you chose). This deletes configuration, chats, history, logs and snapshots.

There is no uninstaller and no background service. GisCode only manages a
`llama-server` process that it started itself.

## Where to go next

- [README](../README.md) — capabilities, requirements and supported scope.
- [Troubleshooting](TROUBLESHOOTING.md) — startup, model and test failures.
- [Release notes](RELEASE_NOTES.md) — what v0.1.0 contains and does not contain.
- [Security policy](../SECURITY.md) — private vulnerability reporting.
