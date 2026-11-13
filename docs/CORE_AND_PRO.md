# GisCode Core and GisCode Pro

GisCode ships in two editions built from one code base. **Core** is the free
edition, and it is what this public repository contains; **Pro** adds the
developer and release tools and is built from the private repository.

The edition is a launch-time configuration, not a licence check. This build
contains no activation or entitlement mechanism.

Setting `GISCODE_EDITION=pro` in this public tree has no Pro tools to start: the
Pro modules are not published here, so the application reports them as
unavailable and runs the Core tools. Nothing crashes and nothing is unlocked.

```bat
py -3.13 -m giscode.main                     :: Core (default)
set GISCODE_EDITION=pro && py -3.13 -m giscode.main
```

The value is read from `GISCODE_EDITION`. `pro`, `professional` and `full`
select Pro (case-insensitive); anything else, including unset, is Core.

## What each edition contains

| Surface | Core | Pro |
| --- | --- | --- |
| Chat, attachments and attachment analysis | yes | yes |
| Projects, planning, approval and build execution | yes | yes |
| Model Manager: browse, choose, switch models | yes | yes |
| Essential settings (model, profile) | yes | yes |
| GisCode AdSpace panel | yes | no — Pro is ad-free |
| Terminal | — | yes |
| Conversation history as its own tab | — | yes (Core has it inside Chat) |
| Status tab | — | yes (Core shows status in the status bar) |
| iOS / xtool | — | yes |
| GisLogo | — | yes |
| Developer & Release | — | yes |
| Device Preview | — | yes |
| Generate Video | — | yes |
| Project Doctor | — | yes |
| Hosted-writer and self-improvement settings | — | yes |
| Custom logo | — | yes |

Core is a complete product for the work its capability contract covers. Without
the GisAI package it performs five deterministic operations and says exactly which
one it used; anything outside that set is refused with a specific reason, never
approximated. Only the developer and release tools are withheld, and they are
withheld by not building them: Core does not import
`giscode.developer_release`, `giscode.gui.gislogo_view`,
`giscode.gui.developer_release_workspace` or `giscode.gui.ios_xtool_view` at
all.

## What Core can do without GisAI

These are the deterministic capabilities, declared in
`giscode/agentic/capabilities.py` and executed by
`giscode/agentic/core_tasks.py`. None of them uses a model.

| Capability | What it does | Writes | Approval | Model |
| --- | --- | --- | --- | --- |
| `create_file` | create one new text file whose exact content is supplied | yes | none | none |
| `read_file` | show one file's text, size, line count and SHA-256 | no | none | none |
| `list_files` | list a project folder's entries, sorted and bounded | no | none | none |
| `search_text` | find a literal term in the project's text files, bounded | no | none | none |
| `replace_text` | replace exactly one literal occurrence in an existing text file | yes | required | none |

Every write stays inside the open project, records a pre-edit snapshot outside
the project tree, and is verified by reading the bytes back. `replace_text` shows
the diff and changes nothing unless a person approves it. A capability that
cannot be verified is not offered at all.

## What Core refuses, and why

| Status | Meaning | Example |
| --- | --- | --- |
| `model_required` | needs a model and the GisAI planner, which Core does not ship | "refactor the parser" |
| `pro_only` | a Pro surface this build does not contain | "package the release" |
| `unsupported` | deterministic work Core does not implement | "delete app.py" |
| `invalid_request` | a known capability with unsafe or incomplete arguments | a path outside the project |
| `approval_required`, `rejected` | the edit needs a person; none was available, or they declined | reject the diff |

A refusal names the status, the reason and what would be needed. Core never
claims a model ran and never substitutes a heuristic for inference.

## Where the decision lives

`giscode/edition.py` is the single registry:

- `SHARED_FEATURES` — in both editions;
- `CORE_ONLY_FEATURES` — Core only (`adspace`);
- `PRO_ONLY_FEATURES` — Pro only;
- `TAB_FEATURES` — navigation label to feature id;
- `tabs()` — the navigation tabs the running edition builds, in order.

An unknown feature is **disabled**, so a typo cannot expose a surface, and a
feature added to the registry is Pro until it is deliberately shared. The main
window builds tabs by iterating `edition.tabs()`; it does not call every builder
and hide the results.

## What the GUI looks like in Core

- Top bar: GisCode branding, GisAI state, project selector, **+ New Project**,
  **Open Existing…**, project **Settings**, model selector and **Model
  Manager**, **Stop**, and the status line.
- Navigation: **Chat**, **Projects**, **Model Manager**, **Settings**,
  **AdSpace**.
- Opening and closing a project are project actions and live in the Projects
  tab beside the workspace, not in the header.
- Conversation history opens from **History** inside Chat, with the same list,
  detail pane, rename/delete/new-chat actions and persistence as the Pro tab.

## Testing the separation

```bat
py -3.13 -m pytest tests/test_core_independence.py tests/test_import_boundary.py tests/test_adspace_integration.py
```

`tests/test_core_independence.py` runs the Core feature set in a fresh
interpreter with every `giscode.gisai` import blocked, and
`tests/test_import_boundary.py` fails if a module imports it at import time.
`tests/test_adspace_integration.py` checks that Core shows the AdSpace panel and
Pro does not.

The Pro-side test that builds both editions and compares every tab is
`tests/test_edition_separation.py`. It exists only in the private development
repository and is not part of this tree.

## Producing the public Core tree

The private development tree produces it with its own exporter (`--profile
core`), which removes every path on the private pro-only list and fails closed
if a Pro path survives. That exporter, its `release/` folder and the
`tests/test_core_boundary.py` check that keeps the list complete are not part of
this repository. What is published here is `tests/test_core_edition_export.py`,
which asserts from inside the Core tree that no Pro module is present.

Whether the public repository publishes Pro implementations at all (Core only,
a compiled Pro build, or the full source) is still the owner's decision; the
mechanism above makes the Core-only choice a one-flag run rather than a manual
edit.

## Windows distribution of Pro (decided 2026-11-11)

The owner has approved Windows distribution of GisCode Pro as a product goal.
The decision fixes these points:

- **A separate, Pro-branded installer**, not the Core installer with a switched
  label: `GisCodePro.exe` inside a Pro-branded setup with its own product name,
  version-resource identity, Start Menu shortcut and uninstall registry key.
- **Pro packaging lives in the private Pro repository.** This public Core tree
  stays Core-only; nothing here creates, brands or ships a Pro installer.
- **Installer logic is shared where practical.** The per-user install/uninstall
  behaviour proven for Core (empty-folder pruning, user-data preservation,
  registration removal only when no program files remain, truthful incomplete
  reporting, deferred cleanup) is the behaviour Pro must exhibit; Pro carries
  the same logic under its own brand rather than a re-implementation.
- **Core and Pro stay independent.** Core never imports or depends on Pro, and
  the Pro build never depends on Core's branding, registry key or artifact
  names.

This records product direction only. It authorises no installer implementation,
build, signing, publication or deployment; each of those needs its own approval.
The implementation plan and its prerequisites are tracked in
`reports/PRO_INSTALLER_DECISION_2026-11-11/`.
