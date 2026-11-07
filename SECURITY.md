# Security policy

GisCode runs commands and edits files in your project, so security reports are
welcome and taken seriously.

## Reporting a vulnerability

Please **do not report a security vulnerability through a public issue, pull
request, discussion, or social-media thread**.

Send a private report by email to:

`gis1948@gmail.com`

This is the owner-approved security contact for GisCode. Include enough
information for the maintainers to reproduce and assess the report:

- the affected GisCode version or commit;
- the operating system and relevant environment;
- the smallest steps that reproduce the issue;
- what an attacker could gain or what protection fails;
- any safe mitigation or workaround you have identified;
- a way for the maintainers to contact you privately.

Do not send secrets, API keys, access tokens, personal data, or a working
exploit against a system you do not own. If sensitive material is necessary,
say so first and wait for the maintainers to arrange a private channel.

The maintainers will coordinate investigation, remediation, and disclosure
with the reporter. Please allow reasonable time for a fix before public
disclosure.

## Scope

Reports are especially useful for:

- a path that escapes the selected project root, including `..`, symlinks,
  Windows short names and UNC paths;
- command injection through plan actions, task text or file names;
- secrets (API keys, `.env` values, tokens) appearing in logs, history, learning
  records, GUI text or exported reports;
- a timeout, cancellation or failed verification being reported as success;
- cleanup that terminates a process GisCode did not start.

## Out of scope

- The accuracy or safety of a model's suggestions by itself. GisCode gates edits
  behind approval; a bad suggestion is a bug report, not a vulnerability.
- Third-party servers or binaries you install (llama.cpp, xtool, browsers).
- Running the alpha on a machine where you would not run arbitrary generated
  code: it executes commands in your project by design.

## Supported versions

There are no releases yet. Only the current `main` branch is maintained.
