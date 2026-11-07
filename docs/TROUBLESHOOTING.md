# Troubleshooting

Start with the [Quick start](QUICKSTART.md). These checks separate an
installation problem from a model/server problem and from a project problem.

## Collect the first useful failure

Run the GUI check from the checkout:

```bat
.venv\Scripts\python.exe -m giscode.main --gui-check
```

It exits non-zero with `GUI_CHECK_FAIL: ...` when the window cannot be built.
Startup and runtime messages are also written under:

```text
%USERPROFILE%\.giscode\logs\giscode.log
```

If `GISCODE_HOME` is set, look under that directory instead. Do not paste API
keys, tokens, private project contents or personal paths into a public issue.

## Python or Tk is missing

Check the interpreter and Tk before installing anything else:

```bat
py -3.13 --version
py -3.13 -c "import tkinter; print('Tk', tkinter.TkVersion)"
```

If Tk is missing, re-run the python.org installer and enable **tcl/tk and
IDLE**. The test suite skips most GUI tests without Tk, but the application
needs Tk to open its window.

## Installation fails

Use the repository's supported Python and a fresh virtual environment:

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The runtime requirement is `psutil`; optional image and YAML features have
their own extras in `pyproject.toml`. Run `python -m pip check` in the
environment if you suspect a damaged install.

## The window opens but no model can be loaded

This is usually a server or model-discovery problem, not a GUI failure.

1. Confirm `llama-server.exe` exists on `PATH`, or that
   `GISCODE_LLAMA_SERVER_PATH` points to an existing executable.
2. Put a GGUF model in `%USERPROFILE%\.giscode\models`, or select a model found
   under LM Studio.
3. Open **Model Manager** and load the model explicitly.
4. Read the status line and `giscode.log`; “no model loaded” is an honest
   refusal, not a hidden fallback.

GisCode does not download models or install `llama-server` for you. A missing
model must not be reported as a successful run.

## The server is already running

GisCode reports a pre-existing `llama-server` but does not kill it. It only
stops a process it started. If another application owns the server, close that
application or point GisCode at a different executable/model combination.

## Tests fail or write to the wrong profile

Run the documented command from the checkout:

```bat
.venv\Scripts\python.exe -m pytest tests --ignore=tests/fixtures
```

`tests/fixtures` contains sample projects, not GisCode tests; excluding it is
required. The suite redirects profile writes to a temporary directory. For a
manual isolated run, set `GISCODE_HOME` to a disposable path and optionally set
`GISCODE_TEST_MODE=1`.

## A change or command was refused

GisCode is designed to fail closed: paths outside the selected project,
unapproved writes, unsupported operations and missing models are refused with a
reason. Do not disable workspace confinement or approval to make a task pass.
Report the exact refusal text and the smallest reproducible case.

## Cleanup after an experiment

Stop GisCode, then remove only the disposable profile you created:

```bat
rmdir /s /q "%TEMP%\giscode-profile"
```

Do not delete a real `%USERPROFILE%\.giscode` directory until you have backed up
the chats, history and snapshots you want to keep. Uninstalling the source
checkout does not remove that profile automatically.

## Reporting a bug

Use the repository's bug-report template and include the GisCode version, OS,
Python version, install method, exact command, expected/actual result and the
smallest non-sensitive log excerpt. For a security issue, follow
[SECURITY.md](../SECURITY.md) instead of opening a public issue.
