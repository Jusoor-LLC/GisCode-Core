# Acceptance build runbook

The acceptance executable exercises the packaged GUI from inside the frozen
bundle. It is **not** a product build: it bakes a runtime marker that makes the
scenario entry point reachable, and scenarios can modify and delete files inside
a disposable profile.

## Artifacts

| Artifact | Purpose |
|---|---|
| `dist/GisCode.exe` | Production build. Contains no marker, no runtime hook and no acceptance entry point. |
| `dist-acceptance/GisCodeAcceptanceHardened.exe` | Hardened acceptance build (`GisCodeAcceptanceHardened.spec`). |
| `dist-acceptance/GisCodeAcceptance.exe` | Earlier acceptance build, kept for the previous audit. |

Build the hardened executable with:

```bash
py -3.13 -m PyInstaller --noconfirm \
  --workpath build/pyinstaller-acceptance-hardened \
  --distpath dist-acceptance GisCodeAcceptanceHardened.spec
```

The recipe generates its marker hook in a temporary directory; nothing is
written into the repository.

## Environment

| Variable | Required | Meaning |
|---|---|---|
| `GISCODE_HOME` | yes | Profile directory the run may write to. |
| `GISCODE_ACCEPTANCE_OPT_IN=1` | yes | Explicit opt-in; the marker alone is not enough. |
| `GISCODE_ACCEPTANCE_SCENARIO` | yes | `interactive` or `resume`. |
| `GISCODE_ACCEPTANCE_EVIDENCE` | yes | Bare file name under `%TEMP%\giscode_acceptance`. |
| `GISCODE_ACCEPTANCE_DISPOSABLE_HOME` | yes | Must canonicalize to `GISCODE_HOME`. |
| `GISCODE_ACCEPTANCE_DISPOSABLE_TOKEN` | yes | At least 16 characters; must equal the marker file. |
| `GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE=1` | for `interactive` | Separate consent for the delete phase. |
| `GISCODE_TEST_MODE=1` | recommended | Stops model discovery and autoload. |

### Disposable-home contract

A scenario refuses to run (exit code 2, `SCENARIO_REFUSED: <reason>`, no
evidence written and nothing modified) unless all of the following hold:

1. the home exists, is a directory, and is not a symlink or junction;
2. no component of its path is a reparse point;
3. it lies strictly inside the temporary root;
4. it is not the real `%USERPROFILE%\.giscode`, not inside it, and not the user
   home directory itself;
5. it does not look like a project checkout (no `.git`);
6. it contains `.giscode-acceptance-disposable` whose contents equal the token;
7. for `interactive`, `GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE=1` is set.

The guard runs **before** logging, the settings store, the knowledge database
and the GUI are initialised, so a refused run writes nothing to the profile -
not even a log line or a SQLite journal file. The scenario re-validates when it
is armed on the event loop; the early gate is what makes a refusal read-only.

The delete target is re-verified inside the home immediately before the handler
runs. The runner must create the marker; a real profile does not have one.

### Example

```bash
HOME_ROOT="$TEMP/giscode-acceptance-home"
TOKEN=$(python -c "import secrets;print(secrets.token_hex(16))")
mkdir -p "$HOME_ROOT"
printf '%s' "$TOKEN" > "$HOME_ROOT/.giscode-acceptance-disposable"

GISCODE_HOME="$HOME_ROOT" \
GISCODE_ACCEPTANCE_DISPOSABLE_HOME="$HOME_ROOT" \
GISCODE_ACCEPTANCE_DISPOSABLE_TOKEN="$TOKEN" \
GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE=1 \
GISCODE_ACCEPTANCE_OPT_IN=1 \
GISCODE_ACCEPTANCE_SCENARIO=interactive \
GISCODE_ACCEPTANCE_EVIDENCE=interactive.json \
GISCODE_TEST_MODE=1 \
  ./dist-acceptance/GisCodeAcceptanceHardened.exe
```

## Scenarios

- **interactive** — provider selection and persistence, self-created project
  fixture, context build, redaction, containment, import attribution, real
  `WebChatBrowserHost` lifecycle with a doubled process, contained profile
  deletion, Settings state from the account store.
- **resume** — second process reads the persisted provider, reopens the fixture
  with redacted context, re-checks settings and profile isolation.

Each phase writes JSON evidence containing `exe_sha256` and `exe_size`; the
driver rejects evidence that does not match the executable it launched.

## Driver

```bash
py -3.13 build/acceptance/frozen_driver_hardened.py
```

The driver creates the disposable home and marker, runs both phases, verifies
hash binding, proves four refusal cases (outside temp, unmarked home, missing
destructive opt-in, real user profile), runs `--gui-check`, and writes
`build/acceptance/hardened_acceptance.json`.

## Cleanup

`build/acceptance/hardened/` and `%TEMP%\giscode_acceptance` are disposable.
The driver deletes and recreates them on each run. No product profile is
touched; verify with a fingerprint of `%USERPROFILE%\.giscode` before and after.
