# GisCode Core — Windows executable

## Current release candidate

The current reproducible Windows candidate is:

```text
build\release-candidate-2026-11-11\GisCode.exe
```

- Size: `30,986,209` bytes
- SHA-256: `5b8cb9c3389eb1b16a39c1b00b72d8f7cb6fd37cdefa5fc79f56f54dee058962`
- Build: Python 3.13.15, PyInstaller 6.22.2, `GisCodeCore.spec`
- Build log: `build\release-candidate-2026-11-11\build.log`
- Built from the exact source snapshot recorded in
  `build\acceptance\source-snapshot-2026-11-11-v2\` (snapshot manifest
  SHA-256 `1bdf2e75b8db279fd8fd40d6945adb39599bd357f99ea22b512d12e8ba293883`).

The previous `build\release-candidate-2026-11-10\GisCode.exe` and the older
`dist\GisCode.exe` are preserved. The acceptance executable in
`dist-acceptance\` predates the H1 repair and must not be used for H1 validation.
The rebuilt acceptance executable from the same snapshot is
`build\acceptance\core-acceptance-2026-11-11\GisCodeAcceptanceHardened.exe`
(SHA-256 `c3d4b67884560682029e79741530dd67c6fb31abcbbf14024eafc32234d9817f`).

## Requirements

- Windows 10/11 x64.
- Microsoft Edge WebView2 Runtime for the integrated **AI Web Chat** browser.
  It is not bundled.
- A separately installed local model server (for example LM Studio's
  `llama-server.exe`) and model files for local inference. Neither is bundled.
- Python 3.13 is required only when a model-backed coding task runs project
  tests. The frozen UI starts without it and fails closed with a visible reason
  if the test runner cannot find Python.
- AI Web Chat is manual and user-mediated. ChatGPT is the embedded provider;
  other providers open in the user's own browser and keep no GisCode profile.
- No account, token, password, model weight or API key is bundled.

## User data

Normal use stores configuration, logs, chats, project databases and credentials
under `%USERPROFILE%\.giscode`. The ChatGPT browser profile is isolated under
`%USERPROFILE%\.giscode\chatgpt_browser`.

`GISCODE_HOME` is for isolated tests. Do not point packaging or acceptance tests
at the real profile.

## Verification performed on this candidate

- `GisCode.exe --gui-check` with an isolated `GISCODE_HOME`: exit `0`,
  `GUI_CHECK_OK`, edition `core`, six tabs (`Chat`, `AI Web Chat`, `Projects`,
  `Model Manager`, `Settings`, `AdSpace`), required controls and branding.
- Fresh packaged launch reached a real `GisCode - No model loaded` window; the
  project was selected through the actual Windows folder picker. `WM_CLOSE`
  closed the exact candidate process and no candidate process remained.
- A real GUI task entered `WORKING`; the visible red Stop control was clicked;
  the UI showed `CANCELLED`; the chat recorded
  `Task stopped: stopped by the user, no changes were made.`; the project file
  hashes before and after were identical.
- H1 non-disposable-profile refusal was revalidated with the rebuilt acceptance executable:
  exit `2`, `SCENARIO_REFUSED: GISCODE_ACCEPTANCE_DISPOSABLE_HOME is not set`,
  and the sentinel profile survived.

## Known limitations

- The disposable-profile H1 scenario did not complete in the qualification
  window. The real-profile refusal is proven; the disposable-deletion half and
  traversal/forged-path cases remain covered by source tests, not by a completed
  acceptance scenario on this binary.
- The post-completion `View Diff` viewer, approval/rejection and rollback paths
  were not re-exercised on this final binary. Prior evidence exists on the
  2026-11-10 candidate only.
- The full-suite result is recorded in
  `reports\CORE_RELEASE_QUALIFICATION_2026-11-11-final.md`; the refresh
  concurrency test passed 12/12 isolated repetitions after the cancellation fix,
  but a suite-context order-dependent failure was observed earlier and the final
  two-run gate is not yet established.
- No installer, code signature, clean-VM/account installation, upgrade test or
  SmartScreen validation was performed.
- The executable is unsigned; Windows SmartScreen may warn on another machine.

## Build command

```bat
py -3.13 -m PyInstaller --noconfirm --clean ^
  --workpath build\pyinstaller-rc-2026-11-11 ^
  --distpath build\release-candidate-2026-11-11 GisCodeCore.spec
```

## Desktop shortcut

1. Open `build\release-candidate-2026-11-11` in File Explorer.
2. Right-click `GisCode.exe` and choose **Show more options → Send to → Desktop
   (create shortcut)**.
3. Optionally rename the shortcut to `GisCode Core`.

Do not publish this candidate without the owner's explicit approval.
