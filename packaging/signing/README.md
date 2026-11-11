# Signing workflow (owner-controlled, $0)

Signing is a **separate step from building**, performed by the owner with the
SignPath Foundation certificate. Nothing in this directory can sign on its own:
there is no private key, no PFX, no token and no local certificate store path.

## Route

SignPath Foundation issues the certificate to SignPath Foundation, so the
publisher shown in the signature and in Windows dialogs is **SignPath
Foundation**, not Jusoor Company LLC. The certificate costs nothing and requires
an OSI-licensed project built from a public repository.

## Order of operations

1. `py -3.13 packaging/signing/make_manifest.py --artifacts <dir> --out signing/manifest.json`
   Records size and SHA-256 of every artifact plus the toolchain.
2. Owner reviews the manifest and the release gate, then writes
   `signing/APPROVED.txt` containing `release=<version> artifacts=<dir>`.
3. Owner runs `sign_release.ps1`. It aborts on an approval-marker or hash
   mismatch, prints `SIGNING SKIPPED` when `SIGNPATH_API_TOKEN` is absent, and
   otherwise prepares the submission ZIP. It never signs and never stores the
   token.
4. Owner submits the payload and approves the signing request in SignPath.
   Approval is the only action that produces a signature.
5. Owner downloads the signed files into `signed/` and runs
   `verify_signed.ps1`, which requires `Status = Valid`, a timestamp, and the
   expected publisher, then writes `signing-record.json`.

## Build order for the two executables

`GisCode.exe` is embedded inside `GisCodeSetup.exe` as stored (uncompressed)
data, so sign in this order:

1. sign `GisCode.exe`;
2. rebuild the installer with `GISCODE_APP_EXE` pointing at the **signed** app;
3. sign `GisCodeSetup.exe` (it now contains a signed payload);
4. verify both, and confirm the embedded `GisCode.exe` hash equals the signed
   one. Never modify a file after it has been signed.

## Rules this workflow enforces

- Unsigned originals stay in their own directory (`unsigned/` or `dist/`) and
  are never overwritten by the signed copies.
- Any hash mismatch aborts before submission.
- No self-signed fallback and no paid service: if the free identity is
  unavailable, the workflow stops with an explicit diagnostic.
- Timestamps come from SignPath's RFC 3161 authority; the record stores the
  timestamp subject and time.
- Credentials live only in the owner's environment or secret store. They are
  never written to this repository, to logs, or to a report.

## Not covered here

Publishing, uploading to a release page, and distributing the signed binaries
remain manual owner actions outside this workflow.
