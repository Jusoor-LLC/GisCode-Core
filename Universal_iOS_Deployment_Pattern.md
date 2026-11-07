# Project Pattern: Universal Windows → WSL2 → Apple SDK → xtool → Physical iPhone Deployment

## 1. Purpose

Reuse a proven Windows/WSL2/Apple/Xcode/xtool/USBMUX/physical-iPhone infrastructure instead of rebuilding it for every new iOS project. This pattern enables GisAi to recognize when the pattern applies, evaluate prerequisites, separate reusable principles from environment-specific facts, and adapt or reject the pattern based on evidence.

---

## 2. Applicability

This pattern applies when **all** of the following are true:
- Target platform: Windows 11 (64-bit) with WSL2
- Target device: Physical iPhone (not simulator-only)
- Build toolchain: Swift + xtool on Linux (WSL2)
- Apple SDKs available via xtool's established setup
- USB connectivity via Windows Apple stack + USBMUX relay into WSL2
- A new iOS application project exists (Xcode project, workspace, SwiftPM, or hybrid)

Does NOT apply when:
- Building for macOS target
- Using simulator-only workflows
- Running on non-Windows host
- Infrastructure components missing or known broken

---

## 3. Prerequisites

| Prerequisite | Verification Command | Evidence Required |
|--------------|---------------------|-------------------|
| WSL2 operational | `wsl --status` / `uname -a` in WSL | Ubuntu version confirmed |
| Swift toolchain | `swift --version` | Version matches expected |
| xtool | `xtool --version` | Version confirmed |
| Apple SDKs available | `xtool list-sdks` or SDK path check | SDK paths listed |
| USBMUX relay | `xtool devices` | Physical iPhone serial number visible |
| Windows Apple stack | AppleMobileDeviceProcess running | Process present |
| Port proxy | `netsh interface portproxy show all` | 127.0.0.1:27015 → WSL IP:27015 |
| Developer Mode | iPhone Settings → Privacy & Security | Enabled on device |

All prerequisites are **environment-specific facts** — they must be verified on each machine.

---

## 4. Proven Infrastructure

The following architecture has been **proven on the source machine** with a real physical iPhone and an actual iOS application:

```
Physical iPhone
       ↓
Windows Apple USB stack
       ↓
AppleMobileDeviceProcess
       ↓
127.0.0.1:27015
       ↓
Windows portproxy (netsh)
       ↓
172.27.80.1:27015 (WSL2 host IP)
       ↓
WSL2 Ubuntu
       ↓
USBMUXD_SOCKET_ADDRESS=172.27.80.1:27015
       ↓
xtool
       ↓
Physical iPhone
```

**Critical invariant**: iPhone remains owned by Windows. Do NOT use `usbipd attach --wsl`. The Linux-local `usbmuxd` must remain stopped.

This architecture is **environment-specific fact** — IPs, paths, versions, and process names apply only to the source machine. The **reusable principle** is: *reuse and verify the proven USBMUX relay architecture instead of rebuilding it*.

---

## 5. Operational Workflow

For ANY new iOS application, follow this order:

### Step 1 — Identify the Project (inspect only)
Determine without modifying:
- Project path
- `.xcodeproj` or `.xcworkspace` or `Package.swift`
- App target, bundle identifier, deployment target
- Swift version, build configurations
- Dependencies, Apple frameworks, resources, asset catalogs
- Localization, privacy manifest, signing configuration

### Step 2 — Verify Infrastructure
Check:
- `swift --version`
- `xtool --version`
- `xtool devices` → **must show physical iPhone**
- Apple SDK availability

If `xtool devices` cannot see the phone: **STOP**. Fix infrastructure first. Do not blame application code.

### Step 3 — Determine Build Method
Inspect existing project structure. Possible types: `.xcodeproj`, `.xcworkspace`, SwiftPM, hybrid, CocoaPods, other.
- **Reusable principle**: Prefer preserving existing structure. Do not convert to SwiftPM merely for tool convenience.
- Determine what xtool can build directly.

### Step 4 — Classify Failure Types
When a build fails, classify **before** fixing:
- **Infrastructure failure**: Swift/xtool unavailable, SDK missing, iPhone not visible, USBMUX/portproxy/WSL networking failure → fix infrastructure first
- **Project/build failure**: Compilation errors, missing sources, invalid settings, dependency failure, unsupported framework, resource processing, deployment-target conflict → fix application/project
- **Signing/provisioning failure**: Treat separately. Do not confuse with USB connectivity
- **Runtime failure**: Only investigate after successful install

### Step 5 — Asset Catalog Limitation
Linux lacks macOS `actool`. If `Assets.xcassets` processing fails:
- First check if xtool has appropriate Apple SDK/toolchain path
- Use disposable proof project (temporary mirror) for workarounds
- **Never** silently transfer experimental changes to real application

### Step 6 — Build
Use least invasive method supported by project structure and xtool. Record:
- Exact command, build result, target, configuration, SDK, architecture, bundle identifier, minimum iOS version
- **Do not claim success** merely because compilation completed — inspect/validate the artifact

### Step 7 — Package
Verify `.app` or package:
- Mach-O architecture, bundle identifier, minimum iOS version, Info.plist
- Required resources, localization, privacy manifest, application icon, executable presence
- Record package result before installation

### Step 8 — Install to Physical iPhone
Use xtool's physical-device installation path. Success = explicit "Successfully installed!" message. Record separately from launch.

### Step 9 — Launch Caveat
**Known behavior**: Installation may succeed but xtool reports "Could not find an installed app with bundle ID '...'". **Do not conclude installation failed.** Instead:
- Verify install result
- Verify app appears on iPhone
- Manually launch from iPhone
- Verify it runs
- Launch/enumeration problem may be separate from installation

### Step 10 — Physical Runtime Verification
After installation:
- Locate app on iPhone
- Launch manually if necessary
- Verify opens, expected startup screen, major initial functionality
- Record crashes/errors separately from infrastructure problems
- Only then begin runtime debugging

---

## 6. Failure Classification

| Category | Examples | Response |
|----------|----------|----------|
| **Infrastructure** | Swift/xtool unavailable, SDK missing, iPhone invisible, USBMUX failure, AppleMobileDeviceProcess down, portproxy broken, WSL networking down | Fix infrastructure FIRST. Stop application troubleshooting. |
| **Project/Build** | Swift compilation errors, missing sources, invalid project settings, dependency failure, unsupported framework, resource processing failure, deployment-target conflict | Fix application/project code/config |
| **Signing/Provisioning** | Code signing errors, profile mismatch, certificate issues | Treat separately from USB/connectivity |
| **Runtime** | App crashes, UI failures, logic errors | Only after install + launch succeed |

**Reusable principle**: Distinguish infrastructure, project/build, signing, installation, and runtime failures. Never conflate them.

---

## 7. Security Constraints

**Never** put into shell commands, source code, logs, or reports:
- Apple ID password
- Apple account recovery information
- iPhone passcode
- Private signing keys
- Provisioning secrets
- Authentication tokens
- Firebase/private API credentials
- Any passwords

If Apple/device authentication is required, the user must enter credentials directly into the appropriate interactive prompt.

---

## 8. Evidence Requirements

For every failure, report:
- **WHAT FAILED**
- **EXACT ERROR**
- **WHAT WAS TESTED**
- **WHAT CHANGED**
- **RESULT**
- **ROOT CAUSE**
- **NEXT ACTION**

Do not repeatedly retry the same failed operation without changing or verifying the underlying condition.
Do not invent missing historical commands. If exact command unknown: "Exact command not preserved."

**Universal Success Criteria** — mark PASS only with evidence:

| Stage | Evidence Required |
|-------|-------------------|
| WSL | Working (command runs) |
| Swift | `swift --version` output |
| Apple SDK | `xtool list-sdks` shows expected SDKs |
| xtool | `xtool --version` output |
| USBMUX | Relay established, `xtool devices` shows iPhone |
| xtool devices | Physical iPhone serial number visible |
| Build | Compilation succeeds + artifact validated |
| Package | .app verified (Mach-O, bundle ID, Info.plist, resources, privacy manifest, icon) |
| Install | Explicit "Successfully installed!" message |
| Launch | App opens on physical device |
| Runtime | App operates on physical device |

---

## 9. Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| Linux lacks macOS `actool` | Asset catalogs (`.xcassets`) may fail to process | Verify xtool SDK path; use disposable mirror for experiments |
| xtool launch enumeration | May report "Could not find installed app" after successful install | Manually launch from iPhone; verify runtime separately |
| Port proxy IP stability | WSL2 host IP (172.27.80.1) may change on reboot | Re-verify `netsh` proxy after restart |
| USB relay dependency | Requires Windows Apple stack + AppleMobileDeviceProcess | Do not replace working relay architecture |
| Developer Mode | Required on physical device for development deployment | User must enable on device |

These are **environment-specific facts** — limitations observed on the source machine. Other environments may differ.

---

## 10. Environment-Specific Facts (Source Machine)

> **Do not generalize these. They are NOT reusable principles.**

| Fact | Value |
|------|-------|
| Windows version | Windows 11 64-bit |
| WSL2 distribution | Ubuntu 26.04 |
| Swift path | `/opt/swift/usr/bin/swift` |
| Swift version | 6.4 |
| xtool path | `/usr/local/bin/xtool` |
| xtool version | 1.20.1 |
| Xcode SDKs | Xcode 27 (iPhoneOS 27.0, iPhoneSimulator 27.0, macOS 27.0) |
| usbipd-win version | 5.3.0 |
| Port proxy mapping | 127.0.0.1:27015 → 172.27.80.1:27015 |
| USBMUXD_SOCKET_ADDRESS | `172.27.80.1:27015` |
| Primary reference document | `<workspace>\XTOL_XCODE_WINDOWS_IPHONE_HANDOFF.md` |

---

## 11. Adaptation Rules

When applying this pattern to a new environment/project:

1. **Verify prerequisites first** — run all verification commands; record actual versions/paths
2. **Map infrastructure** — identify the actual USBMUX relay (may differ: IP, port, proxy method)
3. **Identify project structure** — do not assume SwiftPM; preserve existing build system
4. **Test infrastructure independently** — `xtool devices` before touching application
5. **Use disposable mirrors** for any experimental workarounds (asset catalogs, signing, etc.)
6. **Separate failure classes** — do not fix application code for infrastructure errors
7. **Require evidence for each stage** — no PASS without proof
8. **Document adaptations** — record what changed from the source pattern

---

## 12. Source / Provenance

- **Source document**: `XTOL_XCODE_WINDOWS_IPHONE_HANDOFF.md` (detailed historical record of infrastructure setup)
- **Source location**: `<workspace>\XTOL_XCODE_WINDOWS_IPHONE_HANDOFF.md`
- **Operational layer**: This pattern (universal procedure)
- **Reference layer**: The detailed handoff document (troubleshooting/reference)
- **Proven with**: Actual iOS application + physical iPhone on the source Windows PC
- **Author**: GisCode Contributors
- **Date**: 2025 (from document context)

---

## 13. Confidence / Evidence Status

| Element | Confidence | Evidence |
|---------|------------|----------|
| Infrastructure architecture | HIGH | Proven with real app + physical iPhone |
| USBMUX relay (Windows → WSL) | HIGH | Verified `xtool devices` shows iPhone |
| `xtool devices` as gate | HIGH | Explicit rule: STOP if device not visible |
| Asset catalog limitation | HIGH | Encountered in practice; documented workaround |
| Launch enumeration caveat | HIGH | Observed: install succeeds but xtool reports missing app |
| Failure classification | HIGH | Derived from actual troubleshooting history |
| Security constraints | HIGH | Explicitly documented requirements |
| Environment-specific facts | MEDIUM | Tied to specific machine; must be re-verified per environment |
| Version numbers (Swift 6.4, xtool 1.20.1, SDK 27) | LOW for reuse | Specific to source machine; do not generalize |

---

## VALIDATION CHECKLIST

| Criterion | Status | Notes |
|-----------|--------|-------|
| Pattern retrievable from library | ✅ | Written as standalone Markdown |
| Knowledge/fact distinction preserved | ✅ | Sections 4, 10 separate principles from facts |
| Provenance preserved | ✅ | Source document path, author, date recorded |
| Evidence requirements preserved | ✅ | Section 8 lists required evidence per stage |
| Environment-specific facts NOT universalized | ✅ | Section 10 isolated; Section 11 adaptation rules |
| No unrestricted autonomous execution | ✅ | Pattern requires verification, classification, user credentials |
| Boundary respected (no GisCode modification) | ✅ | Pattern only; no code changes |

---

## FILES CHANGED

- Created: `<repo root>\Universal_iOS_Deployment_Pattern.md`

---

## FILES NOT CHANGED

- No GisCode source, tests, configuration, architecture, EXE modified
- No files outside the target project were modified
- No unrelated projects modified

---

## BOUNDARY CHECK

- ✅ GisAi Training only
- ✅ No GisCode modification
- ✅ No execution of deployment procedure
- ✅ Pattern extraction only
- ✅ Reusable principles separated from environment-specific facts

---

## BLOCKERS

**None.** The document was provided, analyzed, and transformed into the required pattern structure. The pattern library can store this Markdown file for future retrieval.

---

## STATUS

**COMPLETE** — Pattern created and validated.