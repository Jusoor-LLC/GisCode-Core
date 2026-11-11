# Download GisCode Core

**GisCode Core v0.1.1** — free, Apache-2.0, local-first coding agent for Windows.

> **Not published yet.** This file is the prepared content for the project's
> download page. Intended location: this page in the repository, with the files
> themselves attached to the GitHub release (`Releases → v0.1.1`). If the project
> enables GitHub Pages, publish from `/docs` so this URL stays canonical.

## What it does

GisCode Core is a desktop coding agent for Windows. Point it at a project
folder and describe a change: it plans the work, edits files only after you
approve the diff, runs commands and tests, and verifies the result. It reports
what it actually did, and refuses work it cannot do instead of approximating it.

Core includes: Chat, Projects, the Model Manager, attachment analysis, essential
settings, and the GisCode AdSpace panel. GisCode Pro — terminal, iOS/xtool,
GisLogo, release tooling — is a separate private product and is **not** part of
this download.

## Windows requirements

- Windows 10 or 11, 64-bit.
- A local inference server. GisCode ships none and downloads no models:
  - `llama-server` from llama.cpp, with `GISCODE_LLAMA_SERVER_PATH` set, or
  - [Ollama](https://ollama.com) on its default port, or
  - an LM Studio server, or any OpenAI-compatible endpoint you run yourself.
- One or more GGUF models of your choosing.

No account, no API key and no cloud service are required for local use. The
optional hosted writer is disabled by default.

## Install

1. Download `GisCodeSetup.exe` from the release page.
2. Verify its SHA-256 against the release notes (see below).
3. Run it. Installation is per user: files go to
   `%LOCALAPPDATA%\Programs\GisCode`, a Start-menu shortcut is created, and no
   administrator rights, service or driver are involved.
4. Launch **GisCode** from the Start menu and choose your model server in
   Settings.

`GisCode.exe` on its own is for people who prefer no installer; it needs no
installation and keeps its data in the same profile folder.

## Uninstall

Use **Settings → Apps → Installed apps → GisCode → Uninstall**, or run
`GisCodeSetup.exe --uninstall`. The uninstaller removes the program files, the
shortcut and the registry entry. It **never** deletes your data in
`%USERPROFILE%\.giscode`; remove that folder yourself if you want a clean slate.

Upgrade by running a newer installer over the installed copy — your chats,
projects and settings are preserved.

## Signature status (read this)

**These binaries are not code-signed.** The project is preparing free code
signing through [SignPath Foundation](https://signpath.org/); once active, the
publisher shown by Windows will be **SignPath Foundation** (the certificate is
issued to the Foundation, not to a Jusoor legal entity).

Consequences while unsigned:

- Windows Defender SmartScreen may show "Windows protected your PC" on first
  run. Reputation is earned from download volume and is **not guaranteed** for
  any release, signed or not; a new signed build can still warn.
- The SHA-256 values below are the only integrity check available. Verify them.

## Verify your download

```powershell
Get-FileHash .\GisCodeSetup.exe -Algorithm SHA256
```

Compare with the value published in the release notes:

| File | SHA-256 (qualification build) |
|---|---|
| `GisCodeSetup.exe` | `94232d8af53f1e93cf1fe0110c1dc3ef786bc5a15a16683e41e363cd5efa6705` |
| `GisCode.exe` | `72eae67a5f38b59b4c91b72df2a61f69a10c145a05ec45c22f4ea26ccd920435` |

These are **qualification hashes** for the locked 0.1.1 build that passed the
release checks on the owner's machine. They are **not** evidence that a
downloadable asset has been published: no release asset exists yet. The files
attached to `Releases → v0.1.1` will be produced by the trusted release
workflow from the tagged commit; publish *their* hashes here (and in the release
notes) before pointing anyone at the download. The build is not byte-for-byte
reproducible, so hashes cannot be regenerated from source alone.
Build instructions are in [`BUILD_AND_RELEASE.md`](BUILD_AND_RELEASE.md).

## Security and privacy

- Security reports: [`SECURITY.md`](../SECURITY.md) — private channel, please do
  not open a public issue for a vulnerability.
- Signing policy: [`CODE_SIGNING_POLICY.md`](../CODE_SIGNING_POLICY.md) —
  required disclosure of the free signing service and team roles.
- Privacy: GisCode is local-first and sends nothing to other systems unless you
  configure an endpoint yourself.

## Support

GisCode is free, Apache-2.0 and has no paid tier, no account and no in-app
purchase. The project's only donation destination is the owner's PayPal page:
<https://paypal.me/Jusoor1948>. Donations are unrelated to downloading or using
the software and unlock nothing.

## Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io),
certificate by [SignPath Foundation](https://signpath.org/). See
[`CODE_SIGNING_POLICY.md`](../CODE_SIGNING_POLICY.md) for the team roles and the
release process.
