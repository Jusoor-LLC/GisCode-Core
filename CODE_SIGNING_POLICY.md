# Code signing policy

**Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org/).**

GisCode Core is an Apache-2.0 open-source project. Release executables are built
from this repository and, when a signing request is approved, signed by
SignPath Foundation. The certificate is issued to SignPath Foundation, so the
publisher shown in the Windows signature and in UAC prompts is **SignPath
Foundation** — not a Jusoor legal entity.

<!-- OWNER: complete the two role lines below with real GitHub team links before
     publishing this page. SignPath requires the roles to name actual members. -->
## Team roles

- **Committers and reviewers:** OWNER-FILL — GitHub team link, for example
  `https://github.com/orgs/Jusoor-LLC/teams/members`
- **Approvers** (must approve every signing request): OWNER-FILL — for example
  `https://github.com/orgs/Jusoor-LLC/people?query=role%3Aowner`

## Privacy

This program will not transfer any information to other networked systems unless
specifically requested by the user or the person installing or operating it.
GisCode is local-first: it talks to a `llama-server` on your own machine, stores
profiles under `%USERPROFILE%\.giscode`, and needs no account. The optional
hosted writer is disabled by default; when a user enables it, requests go only
to the endpoint that user configures (see `docs/HOSTED_WRITER.md` and
`docs/LOCAL_AI_SETUP.md`).

## Release process

Unsigned binaries are built from a clean extraction of the released source, hashed
into a manifest, and then submitted for signing only after an owner approval step
(`packaging/signing/README.md`). No code-signing private key exists on any build
machine or in this repository. Build instructions are in
[`docs/BUILD_AND_RELEASE.md`](docs/BUILD_AND_RELEASE.md).

## Verification

Every release publishes SHA-256 hashes. Verify a downloaded file with
`Get-FileHash .\GisCodeSetup.exe -Algorithm SHA256` and compare it with the value
in the release notes, then check the signature with
`Get-AuthenticodeSignature .\GisCodeSetup.exe`.

## Reporting a violation

Reports about a signed GisCode binary can go to `support@signpath.io` as well as
to this project's [security policy](SECURITY.md).
