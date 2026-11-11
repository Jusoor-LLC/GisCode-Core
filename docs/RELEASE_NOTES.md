# Release notes

## 2026-11-11 — candidate v8 qualification (unpublished)

**Status: READY FOR OWNER APPROVAL** — unpublished, and code signing remains the
one owner decision. Candidate v8 was rebuilt from the reconciled v9 source
snapshot and requalified end to end.

| Artifact | Bytes | SHA-256 |
| --- | --- | --- |
| `GisCode.exe` | 30,989,926 | `1551b1bdfb7e407d8064ce2ac6cbbe0627c5d8201faaaccdb1e5493180d07845` |
| `GisCodeAcceptanceHardened.exe` | 30,995,565 | `7a147fa6fd637acc5070bbfbe41ddf7584c66a67a9c2c2ad8a2fa618a57f1136` |
| `GisCodeSetup.exe` | 42,069,957 | `a2b4b01dda72e18fa7d42f000a32394992b559a77aa08dbe5063dc4ba11b0679` |

The installer embeds the exact application binary above. Windows uninstall was
fixed for both defects found in independent qualification — a self-deletion crash
when the registered `UninstallString` ran the installed `GisCodeSetup.exe`, and a
locked-file failure while GisCode was still running. Uninstall now closes only
processes running from the install folder, removes program files, shortcut and
registry entry, keeps `%USERPROFILE%\.giscode`, and reports `CLEANUP COMPLETE`,
`CLEANUP DEFERRED` or `CLEANUP INCOMPLETE` honestly.

A fresh Windows Sandbox (11 Enterprise 26100.9818) passed install → launch →
relaunch → uninstall with the app closed, reinstall → uninstall while running,
and recovery from an interrupted cleanup, preserving user data every time. The
full suite passed twice (2100 passed, 40 skipped, exit 0), 13 new real-outcome
installer tests pass, and packaged H1 acceptance reports 27 + 7 checks with
`HARDENED_ACCEPTANCE_OK`.

**Known limitations:** model-backed coding is untested in Sandbox because the
host Ollama service is loopback-only; chat-content persistence was therefore not
exercised (profile-level persistence was); the build is not code-signed.

See `reports/RELEASE_2026-11-11/UNINSTALL_BLOCKER_CLOSURE_2026-11-10.md` and
`reports/RELEASE_2026-11-11/SOURCE_RECONCILIATION_2026-11-10.md`.

## 2026-11-11 — prior qualification update (historical)

**Status: NOT READY.** The 2026-11-10 candidate was exercised through the real
Windows GUI: a two-file local-model task completed, three approvals were
accepted, the project test passed, and a separate rejection changed no files.
However, the second full-suite run failed the ChatGPT concurrent-refresh test
(`1 failed, 2074 passed, 40 skipped`), no clean exit code was obtained, and the
concurrency defect remains unfixed. A visible Stop-button cancellation and the
post-completion diff viewer were not qualified. H1/security and Core
independence pass in source-focused tests, but H1 was not re-run against the
packaged executable. No clean-VM install, upgrade, signing or SmartScreen check
was performed. See `reports/CORE_RELEASE_QUALIFICATION_2026-11-11.md`.

## 2026-11-10 — Windows release candidate (unpublished)

A reproducible one-file Core candidate was built from the authoritative
checkout and passed `--gui-check` plus an isolated real-window launch/close
test. The candidate is an unpublished release candidate, not a public release.
The full suite reports `1 failed, 2074 passed, 40 skipped`; the sole failure is
an order-dependent ChatGPT refresh concurrency test that passes in isolation.
The GUI coding/approval/stop workflow and clean-machine installation were not
qualified. See `reports/CORE_RELEASE_READINESS_2026-11-10.md`.

## v0.1.0 — first public Core snapshot

**Status: early alpha, Windows only.** This is the first source snapshot of
GisCode Core. The v0.1.0 snapshot had no installer and was not code-signed;
v0.1.1 adds a per-user installer that is still unsigned until the free
SignPath Foundation route is approved (see `CODE_SIGNING_POLICY.md`). The
source tree remains the authoritative integration target.

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
- **The Core PyInstaller recipe is verified for this candidate.**
  `GisCodeCore.spec` produced the 2026-11-10 candidate; a clean-VM install and
  the old `GisCode.spec` are not verified or supported.

### Requirements

Python 3.13 with tkinter, a `llama-server` binary (MIT licensed, not bundled),
and a GGUF chat/coder model that fits your memory. See `README.md` for the exact
install and run commands, and `docs/CORE_AND_PRO.md` for the edition boundary.

### Reporting problems

Security issues: follow `SECURITY.md` — do not open a public issue. Everything
else: open an issue with the version, operating system and the smallest steps
that reproduce it.
