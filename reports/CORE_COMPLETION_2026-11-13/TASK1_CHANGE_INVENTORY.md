# GisCode Core — Task 1 change inventory (2026-11-13)

**Repository:** `Jusoor-LLC/GisCode-Core`
**Checkout:** `C:\Users\Gtabyah\Desktop\Projects\GisCode-Core` (junction to `GisCode-Core-STAB-01-2026-11-07\clean-checkout`)
**Branch / HEAD:** `feat/provider-streaming-cancellation` @ `de097f6805dbadb2de7ff8d678d38780a908d50b`
**Remote `main`:** `dc4539272813b50fb62fc05c5f3458353e0bced0` (merge of PR #7)
**PR #7:** `MERGED` (2026-11-11) — the branch is no longer an open PR head.
**Working tree:** uncommitted, 32 status entries before this task; no commit, push, merge, tag, sign, publish or deploy was performed.
**Diff fingerprint:** `sha256(git diff)` = `892a96967024b0a06e174633cb3935b7f2779271d79b2ae64c3239d925662a12`

## 1. What was already implemented but uncommitted (verified, not rewritten)

These changes pre-dated this task and were preserved; the tests below exercise them.

| Area | Files | Verification |
| --- | --- | --- |
| Clean-Windows onboarding | `giscode/onboarding/*` (10 modules), `giscode/gui/onboarding_wizard.py`, wiring in `giscode/main.py`, `docs/ONBOARDING.md`, `docs/LOCAL_AI_SETUP.md` | Full suite; onboarding-specific tests |
| Core uninstaller truthfulness | `packaging/windows_installer/giscode_setup.py`, `tests/test_windows_installer_uninstall.py` | 5 new workspace/user-data/deferred tests; full suite |
| Windows Core→Pro parity | `tests/test_windows_installer_pro_parity.py`, `docs/CORE_AND_PRO.md` | Parity tests on Windows; no Pro installer created |
| Pro installer decision | `reports/PRO_INSTALLER_DECISION_2026-11-11/` | Documentation only |

No Pro installer was created, and Core/Pro architectural independence was preserved
(the installer imports nothing from `giscode`; the parity test pins that).

## 2. New work in this task

| File | Change | sha256 |
| --- | --- | --- |
| `giscode/ads/providers.py` | Added `MicrosoftAdvertisingProvider` (listed in `PROVIDERS`): reports unavailable because Microsoft Advertising is an advertiser platform whose Windows ad SDK was UWP-only and retired on 2020-06-01; explicitly distinguishes Entra ID (identity for the advertiser API) from an ad server. No network, no fabricated creative. | `a07849c9c100349c4fb45f5670588bb9345ea838e63dbfb6bdebe5c1389bfbde` |
| `giscode/ads/service.py` | Default no-provider explanation now names Microsoft Advertising alongside Unity Ads/AppLovin. | `788a12f0dd444aafa240449d12a1d2d40f2ec3c27e9b2e12a92a2858d4503bd2` |
| `tests/test_adspace_integration.py` | Regression tests: Microsoft Advertising unavailable with advertiser/UWP/Entra/ad-server reasoning; no identity SDK import; provider inventory includes it. | `d5bb99d273f02dc553c1e73196693c824a8dba80ac021bf6f6d02a756f3c31ff` |
| `tests/test_packaging_metadata.py` | Regression test: every lock entry is `name==version` with at least one 64-hex `sha256` hash — catches stale/unhashed download entries. | `09063a7a6252cedff36eaa30b9d56059761aecda7d934ce9c8a2154cca522591` |
| `tests/test_windows_version_metadata.py` | Regression test: standalone installer `VERSION` literal equals `giscode.__version__` — prevents installer/package version drift. | `e5c71ad3e916f409dee2ade0957bfe3b349cb10229d070689941f4327f20ea0d` |
| `docs/INTEGRATIONS_STATUS.md` | Evidence and owner-input list for Microsoft Advertising and the Oro blocker. | `9752fce04e1d56c731747f72d99c00c47976102985ea12811e3a2c398ccde70c` |

## 3. Workstream status

| # | Workstream | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Task-queue production integration | **Complete (verified)** | `TaskQueue` persistent/single-flight/ordered with pause, cancel, retry, recovery; `MainWindow(QueueHost)` wires it; `tests/test_task_queue.py`, `tests/test_task_queue_concurrency.py` pass. |
| 2 | Oro integration | **Blocked — documented** | No mention, dependency, issue, PR or commit in the repo (`grep`, `git log`, `gh issue/pr`, requirements). Requires owner scope/API/auth/credentials; documented in `docs/INTEGRATIONS_STATUS.md`. No guessed endpoint was added. |
| 3 | Windows advertising provider | **Complete (honest blocker implemented)** | `MicrosoftAdvertisingProvider` reports unavailable with Microsoft sources; Entra ID is not treated as an ad server. |
| 4 | Core uninstaller | **Complete (verified)** | Program files removed; empty app workspace folders pruned; user files kept; registry entry removed only when no program files remain; locked/uninstall-while-running/deferred cleanup tested. |
| 5 | Ubuntu CI / platform tests / stale hashes / workflow | **Complete (verified)** | Ubuntu 24.04 + Python 3.13 + Tk/Xvfb with `--init`: **2143 passed, 100 skipped, 0 failed**. The earlier 2 failures were reproduced as an unreaped-container-zombie artifact; with a real reaper the process-tree tests pass 10/2 skipped. Lock hashes verified by a real `pip download --require-hashes` (18/18 artifacts). Actions pins unchanged; dependabot PRs #5/#6 remain open and were not merged. |
| 6 | Windows Core-to-Pro parity | **Complete (verified)** | Shared installer logic proven under Pro branding; parity test passes; docs record Pro installer lives in the private Pro repo. |
| 7 | Security / dependencies / errors / versioning | **Reviewed** | `pip check` → no broken requirements; 18/18 locked artifacts download and hash-verify; `verify_release_artifacts --check-source-version` → `0.1.1` agreement; no critical/high defect found in reviewed scope. `pip-audit` is not installed and was not added (no new dependency). |

## 4. Test evidence

| Check | Command | Result |
| --- | --- | --- |
| Targeted Core components | `pytest tests/test_adspace_integration.py tests/test_packaging_metadata.py tests/test_windows_version_metadata.py tests/test_task_queue.py tests/test_task_queue_concurrency.py tests/test_windows_installer_uninstall.py tests/test_windows_installer_pro_parity.py` | **83 passed** (`sha256(log) 48e95a40…`) |
| Full Windows suite | `pytest tests --ignore=tests/fixtures` | **2203 passed, 40 skipped, 0 failed** (`sha256(log) b12afd74…`) |
| Dependency hashes | `pip download --require-hashes --no-deps -r requirements-lock.txt` | **18 artifacts downloaded and hash-verified** (`sha256(log) b9d08f5f…`) |
| Version consistency | `python packaging/verify_release_artifacts.py --check-source-version` | `0.1.1` in package, pyproject, packaging config |
| Package health | `python -m pip check` | no broken requirements |

The 40 skips are declared platform/optional skips (Tk unavailable, `GisAI` package absent, non-Windows paths); there are no failures or unexplained skips.

## 5. Limits of this task

- No actual Pro installer was created (forbidden).
- Oro cannot be implemented without owner input; this is a blocker, not an omission.
- Microsoft Advertising cannot serve ads in this Win32/Tk application; the adapter reports that with sources rather than pretending.
- Nothing was committed, pushed, signed, published or deployed.
