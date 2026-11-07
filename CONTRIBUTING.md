# Contributing

Thanks for looking. GisCode is early alpha, so small, well-explained changes are
much easier to review than large ones.

## Setup

1. Python **3.13** with tkinter (`import tkinter` must work).
2. `py -3.13 -m pip install -r requirements.txt`
3. `py -3.13 -m pip install pytest "PyYAML>=6" "Pillow>=10"`

No model, server or network access is needed for the test suite. For a clean
Windows install, first run and local model setup, see
[`docs/QUICKSTART.md`](docs/QUICKSTART.md); for failures, see
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

## Running the tests

```bat
py -3.13 -m pytest tests --ignore=tests/fixtures
```

- The suite redirects every profile write to a temporary `GISCODE_HOME`, so it
  does not touch your real `~/.giscode`.
- `--ignore=tests/fixtures` matters: those directories are sample *projects*
  that the inspector tests read, and they contain their own stub tests.
- Do not add tests that need a real model, a live server, real credentials or
  fixed ports. Tests that do need a local model are opt-in behind
  `GISCODE_RUN_REAL_MODEL_TESTS=1` and must skip by default.
- GUI tests must skip, not fail, on a machine without a usable Tk.

## Editions

Two editions are built from one code base, and the split is enforced by
`giscode/edition.py`, not by hiding widgets after the fact:

- **Core** (the default) has Chat, Projects, Model Manager, essential settings
  and the AdSpace panel.
- **Pro** (`GISCODE_EDITION=pro`) adds the developer and release tools and is
  ad-free.

If you add a surface, decide which edition it belongs to and add it to the
registry. A feature that is not in the registry is disabled, so an unknown name
cannot leak a tool into Core. Do not import a Pro-only module at module level in
shared code: import it inside the Pro branch (`giscode/gui/main_window.py` shows
the pattern), and keep `tests/test_core_independence.py` and `tests/test_import_boundary.py` green — they assert
that a Core launch imports no Pro module.

Run the shipped Core checks before sending a GUI change:

```bat
py -3.13 -m pytest tests/test_core_independence.py tests/test_import_boundary.py tests/test_adspace_integration.py
:: the Pro-only separation test is private; it is not published here
```

Advertising: `docs/ADVERTISING.md`. No provider is integrated, the service must
decide "no ad" for every unavailable case, and it must never open a socket to
decide.

## Reporting a problem

Use the bug-report or feature-request template in the repository. Include the
version, Windows and Python versions, the exact command, and a non-sensitive
log excerpt. Security issues must follow [`SECURITY.md`](SECURITY.md), not a
public issue.

## Making a change

1. One logical change per commit, with a message that says why.
2. Add or update a test that fails on the old behaviour. Do not weaken
   assertions or turn failures into skips to get a green run.
3. If the change touches the executor, verification or the task deadline, say
   in the pull request how the failure path now reports itself.
4. Never add a broad `taskkill` / `kill` by process name. Cleanup may only stop
   processes the application or the test started itself, identified by PID.

## Style

Match the surrounding code: type hints where they already exist, comments that
explain *why*, and no unrelated reformatting.
