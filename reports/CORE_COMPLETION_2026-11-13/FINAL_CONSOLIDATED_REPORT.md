# GisCode Core — Consolidated Completion and SignPath Readiness Report

**Date:** 2026-11-13  
**Repository:** `Jusoor-LLC/GisCode-Core`  
**Branch / HEAD:** `feat/core-onboarding-and-integration` @ `337e393`  
**Remote `main` at review:** `dc45392` (merge of PR #7)  
**PR:** #8, open, mergeable, base `main`  
**Scope:** Task 1, Task 2, Linux/Windows qualification, artifact integrity, GitHub upload state, SignPath Foundation readiness.  
**Historical reports are preserved:** this file references, and does not overwrite, `TASK1_CHANGE_INVENTORY.md`, `TASK2_FINAL_QUALIFICATION.md`, and the earlier release reports.

> **Two separate verdicts.** Task 2 is qualified within its stated scope. SignPath application readiness is **NO-GO** until the public unsigned release and provenance blockers in §12–§14 are closed. These verdicts must not be conflated.

## 1. Task 1 implementation completion

Task 1 deliveries are implemented and covered by tests:

| Area | Result |
| --- | --- |
| First-run onboarding (GUI wizard and headless CLI) | Complete: runtime detection, hardware profile, model recommendation, consented Ollama pull, validation, one verified coding task, resumable state. |
| Uninstaller truthfulness | Complete: user data preserved, empty application folders pruned, registry removed only when no program files remain, locked/incomplete and deferred outcomes reported honestly. |
| Microsoft Advertising | Complete as an honest unavailable adapter: advertiser/UWP/retired-platform reasoning, no identity SDK, no network, no fabricated creative. |
| Oro | Explicitly blocked: no dependency, endpoint, credential, issue or history exists; owner scope/API/auth input is required. No guessed integration was added. |
| Regression coverage | Added for onboarding, installer lifecycle, provider inventory and metadata. |
| Documentation | `docs/ONBOARDING.md`, `docs/INTEGRATIONS_STATUS.md`, `docs/CORE_AND_PRO.md`, installer/signing documents updated. |

Authoritative inventory and file-level hashes: `reports/CORE_COMPLETION_2026-11-13/TASK1_CHANGE_INVENTORY.md`.

## 2. Task 2 Windows qualification

Qualification environment, not a simulation:

- VirtualBox 7.1.18; VM `GisCodeClean-VB2`; Windows 11 Enterprise Evaluation build **26300**, EFI, 6 GB RAM, 2 CPUs, fresh 64 GB VDI.
- VirtualBox configuration contains `<TrustedPlatformModule type="v2_0"/>`.
- In-guest `tpmtool getdeviceinformation`: `TPM Present: True`, IBM, TPM spec 1.64, vulnerable firmware `False`.
- Windows 11 installed normally with TPM 2.0; no bypass, registry workaround or security weakening.
- Secure Boot was not enabled or verified; that limitation is retained and not claimed as a pass.

Evidence: `reports/CORE_COMPLETION_2026-11-13/evidence/clean-windows-evidence.txt`.

## 3. Actual installer lifecycle

The rebuilt `GisCodeSetup.exe` was copied into the clean guest and exercised directly: **51 passed, 0 failed**.

| Lifecycle area | Verified result |
| --- | --- |
| Fresh install | Exit 0; application, setup copy, Start-Menu shortcut and HKCU uninstall entry present; version 0.1.1; correct install location. |
| Payload identity | Installed `GisCode.exe` hash equals the independently verified build hash. |
| GUI startup | `--gui-check` returned `GUI_CHECK_OK`; clean-desktop onboarding wizard visually confirmed. |
| Upgrade | Previous candidate installed, then rebuilt installer installed over it; both reported `UPGRADED True`; rebuilt payload restored; user data kept. |
| Repair | Product intentionally has `NoRepair=1`; same-version reinstall is the supported repair-equivalent and passed. |
| Locked file | Uninstall returned exit 2 with `CLEANUP INCOMPLETE` and the truthful leftover; registry retained. |
| Recovery after unlock | Exit 0, `CLEANUP COMPLETE`; program files, shortcut and registry removed; user workspace kept. |
| Running process | Uninstaller stopped two processes, exited 0 and left no process. |
| Deferred self-uninstall | Exit 0, `CLEANUP DEFERRED`; helper removed the running uninstaller, registry and shortcut; user data kept. |
| User data | `workspace\user-notes.txt` and `%USERPROFILE%\.giscode\user-profile.txt` preserved through uninstalls. |

If the installer still reports an incomplete cleanup, or keeps data it says it removed, that is the documented blocker; the evidence shows the tested paths behaved truthfully.

Evidence: `reports/CORE_COMPLETION_2026-11-13/evidence/windows-lifecycle-result.txt`.

Windows Sandbox was trialled but its Device Guard policy blocked the unsigned executable. That result is preserved in `evidence/sandbox-device-guard-blocked.txt` and was **not** counted as a pass. The VM was used instead.

## 4. Linux qualification and root cause

The earlier two process-tree failures were reproduced only in a container without an init/reaper. The preserved failing log is `logs/core_linux_container.log`.

Root cause: the killed grandchild was reparented to an unreaping PID 1, remained a zombie, and `os.kill(pid, 0)` still answered, so the test's liveness probe was fooled. With `docker run --init`, the targeted command-bounding tests passed (10 passed, 2 skipped) and the full qualified Linux suite passed. No application defect was found and no test was weakened.

Qualified Linux result: Ubuntu 24.04, Python 3.13, Tk/Xvfb, `--init`: **2143 passed, 100 skipped, 0 failed**. Log: `logs/core_ubuntu_24_04_py313.log`.

### Hosted CI note (new, 2026-11-13)

The GitHub `ubuntu-latest` label moved to Ubuntu 26.04, and the full suite repeatedly failed one untouched mock-auth test (`test_chatgpt_siwc.py`) with a 20-second loopback timeout. The test module passed alone. The same intermittent timeout later appeared on Ubuntu 24.04 and on Windows, varying which SIWC test failed; server instrumentation showed the mock server was created but a request thread did not always run after the full suite had accumulated unrelated threads. This is a **test-harness scheduling limitation**, not a product failure. CI is pinned to the qualified Ubuntu 24.04 image and now runs the SIWC protocol file in its own pytest process on both platforms; the remaining tests run in a second process. **Every test still runs**; no assertion, timeout or test was removed. Final code head `7f67d97` is green on both runners (runs `43492926349` and `43492732038`).

## 5. Regression and compatibility totals

| Check | Result |
| --- | --- |
| Windows full suite (local, historical) | **2203 passed, 40 skipped, 0 failed** (`logs/core_full.log`) |
| Windows hosted CI (`7f67d97`) | **50 passed** (SIWC) + **2149 passed, 44 skipped** (rest), 0 failed |
| Windows targeted onboarding/installer suite | **83 passed** (`logs/core_targeted.log`) |
| Linux Ubuntu 24.04 / Python 3.13 | **2143 passed, 100 skipped, 0 failed** (`logs/core_ubuntu_24_04_py313.log`); hosted CI **48 passed, 2 skipped** (SIWC) + **2095 passed, 98 skipped** (rest), 0 failed |
| Installer lifecycle in the clean TPM VM | **51 passed, 0 failed** |
| Core/Pro independence and edition boundary (re-run 2026-11-13) | **28 passed, 2 skipped** |
| Lock-file hash verification | **18/18** downloaded artifacts hash-verified |
| `pip check` | **No broken requirements found** |
| Version agreement | `giscode.__version__`, `pyproject.toml` and packaging config agree on **0.1.1** |

The 40 Windows skips and 100 Linux skips are declared platform/optional skips (Tk, absent GisAI, non-Windows paths), not silent passes.

## 6. Security and dependency audit

- Core runs without GisCode Pro or `giscode.gisai`; the optional GisAI import in `giscode/main.py` is guarded, and the Core edition tests pass with GisAI blocked.
- No Pro module is tracked in the public tree: no `giscode/gisai`, `giscode/developer_release`, GisLogo, Developer & Release or iOS/xtool source.
- Runtime dependency closure is small and declared: `psutil` is required; `pywebview`, PyYAML, Pillow and `llama-cpp-python` are optional/lazy.
- No mandatory proprietary service, account, activation or entitlement is required for Core operation.
- The AdSpace path makes no network request; Microsoft Advertising is unavailable with sources, and Entra ID is not treated as an ad server.
- No critical/high defect was found in the reviewed scope. `pip-audit` was not installed and was not added as a dependency, so a CVE database audit is **not claimed**; the hash/lock and `pip check` results are the dependency evidence.
- Secrets are not present in tracked source; signing tokens and private keys are explicitly out of the repository.

## 7. Final artifact inventory, hashes and provenance

Artifacts were built with PyInstaller 6.22.2 from the verified working tree and re-hashed on 2026-11-13:

| Artifact | Bytes | SHA-256 | Version |
| --- | ---: | --- | --- |
| `GisCode.exe` | 31,063,339 | `ef36daa3e030866ea33dc9cc84d516cc473458a2da94f4b21b2de2900b81ebf3` | 0.1.1 |
| `GisCodeAcceptanceHardened.exe` | 31,068,468 | `c9344d461fc40e46c95310a590b271a37287ab4d9cb64e78d4b652f6130f039e` | 0.1.1 |
| `GisCodeSetup.exe` | 42,144,846 | `c34436f818c9cea2095836217946a106d1669589fa841d9352bf31e5a68bea1c` | 0.1.1 |

`verify_release_artifacts.py` passed: PE metadata, original filenames, version 0.1.1, installer payload byte-identical to the application, no signature table, unsigned as authorized.

**Provenance caveat (release blocker):** `build/core-completion-2026-11-13/provenance/release-provenance.json` records the toolchain and artifact hashes, but its `commit_sha`, `repository`, `workflow_run_id` and `runner_image` fields are empty because it is a local build. It does **not** yet satisfy “source revision and build provenance” for a public release. A Trusted CI build from the exact public commit, with those fields populated, is required before publication. The build is also not byte-for-byte reproducible (PyInstaller `base_library.zip`); hashes identify the built files, not a deterministic rebuild.

## 8. GitHub local/remote/PR status

- Repository is **public**, Apache-2.0 detected, created 2026-11-06, default branch `main`.
- Local/remote branch head: `337e39392df127946edd086d6474c108015c5ebd`.
- PR **#8** is open, non-draft, `MERGEABLE`, base `main`, head `feat/core-onboarding-and-integration`, 17 commits.
- PR #7 was already merged at `dc45392`; the new work was branched from `origin/main`, not force-pushed over the merged branch.
- PR #8 contains the substantive qualification commit `8ea4338` plus CI-only diagnostic/cleanup commits. The final tree is clean of diagnostic workflows and diagnostic test code; the PR diff contains only the verified Core changes and the Ubuntu 24.04 CI pin.
- CI on PR #8: **Ubuntu 24.04 and Windows both passed** on code head `7f67d97` (runs `43492926349` and `43492732038`). The SIWC protocol suite is isolated into its own pytest process on each platform; every test is still run.
- Existing public release `v0.1.0` has **0 assets**. PRs #5 and #6 (Dependabot) remain open and were not merged.
- No merge, tag, signing, public release or deployment was performed.

## 9. SignPath requirement A — public unsigned Windows release

| Requirement | Status | Evidence / gap |
| --- | --- | --- |
| Actual Windows executables | **Prepared locally, not published** | Three verified binaries exist in the gitignored `build/` tree; hashes in §7. |
| Installer and intended distribution artifacts | **Prepared locally, not published** | `GisCodeSetup.exe` embeds `GisCode.exe`; escalation policy and build order documented. |
| SHA-256 hashes | **Prepared** | `reports/CORE_COMPLETION_2026-11-13/SHA256SUMS.txt` and §7. |
| Source revision and build provenance | **FAIL for publication** | Local provenance has empty commit/repository/run fields; no CI provenance from the public commit. |
| Reproducible or independently verifiable build information | **Partial** | Build instructions, pinned lock with 18 hashes, and verification scripts exist; build is not byte-reproducible. |
| Public unsigned release exists before application | **NO** | `v0.1.0` has no assets; no v0.1.1 public release. |

**Conclusion:** the unsigned release is **not ready to publish** even though the binaries are qualified. The next authorized release step must rebuild on the pinned Windows CI runner from the final public commit, record non-empty provenance, publish the artifacts and hashes, and only then use that release in the SignPath application.

## 10. SignPath requirement B — open-source independence

| Requirement | Status | Evidence |
| --- | --- | --- |
| Core fully open source under declared license | **PASS** | Standard Apache-2.0 `LICENSE`; `NOTICE`; `pyproject.toml` declares `Apache-2.0`; README states Apache-2.0. |
| No commercial dual license of Core | **PASS in tracked tree** | No dual-license/commercial-license text in tracked project files; the only proprietary strings found were in untracked local virtualenv/build dependency licences. |
| Operates without Pro or proprietary GisAI | **PASS** | `giscode/gisai` absent; guarded optional import; independence/import-boundary tests pass 28/2. |
| No mandatory proprietary dependency | **PASS** | Required runtime dependency is `psutil`; optional extras are permissively licensed; no activation/entitlement service. |
| Separate source trees, branding, distribution and release identities | **PASS as documented and structurally checked** | Pro source is not in this repository; separate Pro installer/branding decision documented in `docs/CORE_AND_PRO.md`; no Pro registry key/artifact in Core. |
| Core does not include proprietary Pro code/assets | **PASS in tracked tree** | No tracked Pro/GisAI/Developer-Release/GisLogo/iOS source; edition exporter and boundary tests. |

**Caveat:** the private Pro repository and its build were not available for inspection in this workspace, so the claim is limited to the public Core tree and its documented boundary. It is not an audit of private Pro source.

## 11. SignPath requirement C — public reputation

Verified public evidence, not manufactured:

- Repository public since **2026-11-06**; **42 commits** at review.
- **One contributor** (`gis1948`); **0 stars**, **0 forks**, **0 open/closed issues**, no external reviews or independent contributors found.
- PRs #1–#4 and #7 merged; #8 open; Dependabot PRs #5/#6 open.
- One public release, `v0.1.0` (2026-11-07), with **0 assets**.
- README, documentation, security policy and code-signing policy are public.

**Assessment: weak / not independently established.** The project has a real public history and a coherent README, but no external adoption, independent review, contributor diversity, stars/forks or substantive public issue activity. SignPath’s “public reputation” requirement is qualitative; this evidence is not enough to assert it as satisfied. Reputation-building work (public release with assets, external review/usage, issue activity) remains required. No endorsements or metrics were invented.

## 12. SignPath requirement D — application preparation

| Item | Status |
| --- | --- |
| Foundation organization setup waits until review/acceptance | **Correctly not created** |
| Public unsigned Windows release exists before application | **NO — blocker** |
| Application accurately describes Core and separate proprietary Pro | **Draftable now**; `docs/CORE_AND_PRO.md` and README provide the facts, but the application text has not been submitted |
| Code-signing policy published | **Published but incomplete**: `CODE_SIGNING_POLICY.md` still contains `OWNER-FILL` role links for committers/reviewers and approvers |
| Roles name actual members | **NO — owner input required** |
| No application submitted / no Foundation org created | **Confirmed; none performed** |

## 13. Unsigned public release readiness

**Prepared locally:** qualified binaries, hashes, build order, locked dependencies, verification scripts, signing workflow and a code-signing policy draft.

**Not ready:** no public release assets, no CI provenance tied to the public commit, no populated owner/role fields, and no published v0.1.1 release. The local provenance file is evidence of a local build, not a public-release attestation.

## 14. Exact remaining blockers and required approvals

1. **Release blocker — public unsigned release:** rebuild all three artifacts on the pinned Windows runner from the final public commit, populate `commit_sha`, `repository`, workflow run/attempt and runner image, then publish the unsigned release with all artifacts and `SHA256SUMS.txt`. Requires explicit release/publish approval.
2. **Release blocker — provenance:** no public CI provenance currently exists. A local JSON with empty commit fields is insufficient.
3. **SignPath blocker — policy roles:** replace both `OWNER-FILL` entries in `CODE_SIGNING_POLICY.md` with real GitHub team/member links. Requires owner input.
4. **SignPath blocker — reputation:** public reputation is not independently established (single contributor, 0 stars/forks, no external review/adoption). This cannot be manufactured; it needs time and genuine public activity.
5. **SignPath process:** do not submit the application or create the Foundation organization until review/acceptance. Requires explicit approval.
6. **Evidence limitations retained:** Secure Boot not verified; no Ollama/model inference in the clean guest; repair is reinstall-equivalent by design; GUI interaction limited to launch/onboarding and `--gui-check`; hosted mock-server scheduling flake documented; CI pinned to qualified Ubuntu 24.04 and SIWC isolated per platform; private Pro source not inspected.

## 15. Final decision

### Task 2 qualification: **GO — RELEASE-QUALIFIED WITHIN STATED SCOPE**

All required Task 2 gates were executed: genuine TPM 2.0 Windows 11 guest, actual installer lifecycle 51/0, Linux root cause proven and qualified on Ubuntu 24.04, full suites green, artifacts re-hashed, version/lock/dependency checks passing. The limitations in §14 are documented, not hidden.

### SignPath Foundation application readiness: **NO-GO**

The Core code and license position are suitable, but the application cannot be truthfully submitted yet because:

1. there is no public unsigned Windows release with actual assets;
2. no public-commit CI provenance exists;
3. the signing-policy role links are placeholders;
4. public reputation is not independently established.

**Next authorized step:** publish the qualified unsigned v0.1.1 release from the final public commit with CI provenance, then complete the owner/role and reputation work. Re-assess SignPath GO/NO-GO only after those are true. No Foundation organization, application, signature, merge, tag or public release was created in this task.

## Appendix — supporting paths and integrity

- `reports/CORE_COMPLETION_2026-11-13/TASK1_CHANGE_INVENTORY.md` — Task 1 inventory.
- `reports/CORE_COMPLETION_2026-11-13/TASK2_FINAL_QUALIFICATION.md` — historical Task 2 qualification report (preserved).
- `reports/CORE_COMPLETION_2026-11-13/SHA256SUMS.txt` — evidence and source-file hashes.
- `reports/CORE_COMPLETION_2026-11-13/evidence/clean-windows-evidence.txt` — TPM/VM evidence.
- `reports/CORE_COMPLETION_2026-11-13/evidence/windows-lifecycle-result.txt` — installer lifecycle evidence.
- `reports/CORE_COMPLETION_2026-11-13/evidence/clean-windows-onboarding.png` — clean-desktop onboarding screenshot.
- `reports/CORE_COMPLETION_2026-11-13/evidence/sandbox-device-guard-blocked.txt` — rejected sandbox evidence.
- `reports/CORE_COMPLETION_2026-11-13/logs/` — Windows, Ubuntu, process-tree and lock logs.
- `docs/BUILD_AND_RELEASE.md`, `packaging/signing/README.md`, `CODE_SIGNING_POLICY.md` — build/signing process.
