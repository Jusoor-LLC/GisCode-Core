# GisCode Core — Task 2 final qualification (2026-11-13)

**Verdict: RELEASE-QUALIFIED within the stated scope.**
All Task 2 gates that were previously blocked are now executed and pass: a
genuine TPM 2.0 Windows 11 guest, the real rebuilt installer lifecycle, the
Ubuntu 24.04/Python 3.13 suite, artifact integrity, and dependency checks.
Remaining limitations are listed in §7 and are not release blockers.

**Source:** branch `feat/provider-streaming-cancellation`, HEAD `de097f6`
(dirty tree, diff fingerprint `892a9696…`), remote `main` `dc45392`.
**Task 1 inventory:** `reports/CORE_COMPLETION_2026-11-13/TASK1_CHANGE_INVENTORY.md`.

## 1. Windows qualification environment

- Hypervisor: VirtualBox 7.1.18 r173720.
- VM: `GisCodeClean-VB2`, Windows 11 Enterprise Evaluation, build **26300**,
  EFI firmware, 6 GB RAM, 2 CPUs, fresh 64 GB VDI.
- TPM: `<TrustedPlatformModule type="v2_0"/>` in the VM configuration.
  In-guest `tpmtool getdeviceinformation` reports `TPM Present: True`,
  manufacturer IBM, `TPM Spec Version: 1.64` (the TPM 2.0 library revision
  used by VirtualBox), `TPM Has Vulnerable Firmware: False`.
- The Windows 11 installer completed normally with the TPM present; no TPM
  bypass, registry workaround, or security weakening was used.
- The old preserved VM `GisCodeClean-VB1` and its evidence were left untouched;
  the old `.vbox` was backed up before the TPM probe.
- Secure Boot was **not** enabled on this VM. Windows 11 installed with TPM 2.0
  and EFI; Secure Boot capability/state is therefore not claimed (limitation).

Evidence: `evidence/clean-windows-evidence.txt`.

## 2. Actual installer lifecycle in the clean guest

The rebuilt `GisCodeSetup.exe` (42,144,846 bytes,
`c34436f8…`) was copied into the guest and exercised directly, not through the
simulated unit tests. The harness ran 51 checks; **51 passed, 0 failed**:

| Area | Result |
| --- | --- |
| Fresh install to `C:\GisCodeLifecycle` | exit 0; app, setup copy, Start-Menu shortcut and HKCU uninstall entry present; `DisplayVersion=0.1.1`, publisher `Jusoor LLC`, correct InstallLocation |
| Payload identity | installed `GisCode.exe` SHA-256 `ef36daa3…`, exactly the verified build |
| GUI startup | `GisCode.exe --gui-check` → `GUI_CHECK_OK`; the installed app was also launched from a scheduled task and showed its onboarding wizard on the clean desktop (`evidence/clean-windows-onboarding.png`) |
| Reinstall / same-version repair-equivalent | exit 0; user workspace and profile retained; registry/shortcut retained |
| Real upgrade | previous candidate installer (37,921,556 bytes) installed over the new build, then the rebuilt installer installed over it: both reported `UPGRADED True`; rebuilt `GisCode.exe` restored; user data kept |
| Locked-file uninstall | exit 2, `CLEANUP INCOMPLETE`, `LEFTOVER GisCode.exe`, registry retained; truthful failure |
| Recovery after unlock | exit 0, `CLEANUP COMPLETE`; registry/shortcut/program files removed; user workspace kept |
| Running-process uninstall | app running from install dir; uninstaller stopped 2 processes, exit 0, `CLEANUP COMPLETE`; no process left |
| Self-uninstall from installed copy | exit 0, `CLEANUP DEFERRED`; deferred helper removed the running uninstaller, registry and shortcut; user data kept |
| User data | `workspace\user-notes.txt` and `%USERPROFILE%\.giscode\user-profile.txt` preserved through every uninstall |
| Accurate reporting | incomplete and complete outcomes matched actual leftovers; cleanup log written |

Repair is intentionally not offered by the product (`NoRepair=1`); reinstall
over an existing installation is the supported repair-equivalent and passed.
Creating a new repair feature was outside Task 2 and was not done.

Evidence: `evidence/windows-lifecycle-result.txt`.

The disposable Windows Sandbox was also trialled first, but its Device Guard
policy blocks unsigned executables. That evidence is preserved in
`evidence/sandbox-device-guard-blocked.txt` and was **not** treated as a pass;
the VM was used instead without weakening policy.

## 3. Linux process-tree qualification

The earlier two failures were reproduced only in a container without an init
reaper. With a real reaper (`docker run --init`) the same tests pass:

```
tests/test_command_bounding.py: 10 passed, 2 skipped
```

The original failing log is preserved at `logs/core_linux_container.log`; the
targeted pass is `logs/core_linux_process_tree_init_pass.log`. Root cause:
the killed grandchild was reparented to an unreaping PID 1, remained a zombie,
and `os.kill(pid, 0)` still answered, so the test's liveness probe was fooled.
No application defect was found and no test was weakened.

Full Ubuntu 24.04 matrix (Python 3.13, Tk, Xvfb, `--init`):

```
2143 passed, 100 skipped, 4 warnings in 190.35s
```

Log: `logs/core_ubuntu_24_04_py313.log`.

## 4. Windows full suite

```
2203 passed, 40 skipped, 0 failed in 282.42s
```

Log: `logs/core_full.log`. The 40 skips are declared platform/optional skips
(Tk, absent GisAI package, non-Windows paths).

## 5. Artifacts and integrity

Built with PyInstaller 6.22.2 from the verified working tree:

| Artifact | Bytes | SHA-256 | Version |
| --- | ---: | --- | --- |
| `GisCode.exe` | 31,063,339 | `ef36daa3e030866ea33dc9cc84d516cc473458a2da94f4b21b2de2900b81ebf3` | 0.1.1 |
| `GisCodeAcceptanceHardened.exe` | 31,068,468 | `c9344d461fc40e46c95310a590b271a37287ab4d9cb64e78d4b652f6130f039e` | 0.1.1 |
| `GisCodeSetup.exe` | 42,144,846 | `c34436f818c9cea2095836217946a106d1669589fa841d9352bf31e5a68bea1c` | 0.1.1 |

`verify_release_artifacts.py` passed again after all testing: product/file
metadata and original filenames are correct, the installer payload is
byte-identical to the built application, the binaries are unsigned (signing was
not authorized), and no signature table is present.

Version agreement: `giscode.__version__ = pyproject = packaging_config = 0.1.1`.
Installer `--version` → `GisCode Core Setup 0.1.1`.

Dependencies/security:

- `pip check` → no broken requirements.
- `pip download --require-hashes --no-deps -r requirements-lock.txt` →
  18/18 locked artifacts downloaded and hash-verified; no stale hashes.
- No critical/high security defect found in the reviewed scope. `pip-audit`
  is not installed and was not added as a dependency.
- The ad path makes no network request; Microsoft Advertising is reported
  unavailable with sources; Entra ID is not treated as an ad server.

## 6. Acceptance criteria

| Criterion | Status |
| --- | --- |
| No unexplained test failures | **PASS** — Windows 0 failures; Ubuntu 0 failures; process-tree root cause explained |
| Full Windows + Ubuntu matrix | **PASS** — Windows local suite + Ubuntu 24.04/Python 3.13 suite |
| No compatibility regressions | **PASS** — Windows and Linux suites green |
| No unresolved critical/high security defects | **PASS in reviewed scope** |
| Rebuilt installer passes Windows lifecycle | **PASS** — actual installer, 51/0 in clean TPM VM |
| Clean-Windows qualification passes | **PASS** — fresh Windows 11 + TPM 2.0, startup and GUI verified |
| All release artifacts hash-verified | **PASS** |
| Remaining limitations documented | **PASS** — §7 |

## 7. Remaining limitations

1. The clean guest had no Ollama runtime or model, so actual inference and a
   real coding task were not run there; GUI startup and the onboarding wizard
   were verified visually, and the full suites cover the logic.
2. Secure Boot was not enabled/verified on the VM; TPM 2.0 was present and
   Windows 11 installed without bypass.
3. The installer is unsigned, as authorized for this stage.
4. “Repair” is not a product feature (`NoRepair=1`); reinstall is the supported
   repair path and passed.
5. GUI interaction was limited to launch/onboarding and `--gui-check`; the
   remaining tabs were not clicked through on the clean guest.
6. The Ubuntu run is an Ubuntu 24.04 container with Python 3.13, not the hosted
   GitHub runner; the remote CI on `main` is green (`Core CI` run `42968289429`).

## 8. Upload gate

Task 2 acceptance is met, so the remaining verified Core changes are cleared
for upload to `Jusoor-LLC/GisCode-Core` for PR review. No merge, tag, signing,
publication, deployment, or production change is authorized or performed.
GisAI Phase 3.11 remains gated on verified upload completion.
