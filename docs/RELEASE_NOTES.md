# Release notes

## v0.1.0 — first public Core snapshot

**Status: early alpha, Windows only.** This is the first source snapshot of
GisCode Core. There is no installer, no packaged build and no release binary; you
run it from source.

### What this snapshot contains

- **GisCode Core**, the free edition: Chat, Projects, Model Manager, essential
  settings and the AdSpace panel, in a Tk desktop window.
- **Five deterministic capabilities that need no model** — `create_file`,
  `read_file`, `list_files`, `search_text` and `replace_text`. Each one reports
  exactly which operation it performed, and anything outside that set is refused
  with a specific reason instead of being approximated.
- **Approval before writes**, a pre-edit snapshot kept outside the project, and a
  byte-level read-back that verifies the edit landed.
- **Workspace confinement**: paths that escape the selected project are rejected,
  including `..`, symlinks and UNC forms.
- **A complete, honest refusal vocabulary** — `model_required`, `pro_only`,
  `unsupported`, `invalid_request`, `approval_required`, `rejected` — so Core
  never claims a model ran.
- **Local inference through your own `llama-server`**, plus an optional hosted
  writer that is off by default and reads its key only from the environment.
- **An isolated test suite** that runs without a model, a server, a network
  connection or a GPU, and that redirects every profile write to a temporary
  directory.
- **Support the Developer**, in the Help menu and the About dialog: both open the
  owner's PayPal page (`https://paypal.me/Jusoor1948`) in the system browser.
  GisCode processes no payment and stores no payment data.

### What this snapshot does not contain

- **GisCode Pro**: Terminal, iOS/xtool, GisLogo, Developer & Release, Device
  Preview, Generate Video, Project Doctor, hosted-writer settings and the
  separate status/history tabs are built from a private tree and are absent
  here. Setting `GISCODE_EDITION=pro` unlocks nothing.
- **The GisAI package.** Without it Core performs the five deterministic
  operations above and refuses everything that would need a planner.
- **A model.** No GGUF weights are bundled or downloaded; you supply the server
  binary and the model.
- **Advertising.** No ad provider is integrated: the AdSpace panel reports that
  advertising is off, in every state, and opens no socket.

### Known limitations

- **Reliability on larger tasks is not measured.** Small, well-scoped tasks
  complete end to end; multi-file or long-running jobs often need steering.
- **Model quality is the main constraint.** An October 2026 benchmark with the
  small local models in use scored 0 of 8.
- **Recorded lessons are not yet used**; the agent does not measurably improve
  from its own history.
- **Windows 10/11 is the tested platform.** The code has some POSIX paths, but
  other platforms are untested.
- **The PyInstaller recipe is unverified.** `GisCode.spec` exists; no build is
  provided or supported.

### Requirements

Python 3.13 with tkinter, a `llama-server` binary (MIT licensed, not bundled),
and a GGUF chat/coder model that fits your memory. See `README.md` for the exact
install and run commands, and `docs/CORE_AND_PRO.md` for the edition boundary.

### Reporting problems

Security issues: follow `SECURITY.md` — do not open a public issue. Everything
else: open an issue with the version, operating system and the smallest steps
that reproduce it.
