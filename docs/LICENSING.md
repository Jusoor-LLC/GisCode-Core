# Licensing

GisCode Core is published under the **Apache License, Version 2.0**. This page
explains what that means for you, where the decision is recorded, and what is
still outstanding. It is general information, not legal advice.

## What the licence grants

You may use, modify, redistribute and sell the code, including commercially,
provided you:

- keep the `LICENSE` file and the `NOTICE` file with any copy or derived work;
- state the changes you made in modified files;
- do not imply that the original author endorsed your fork.

The licence covers the **code**, not the brand. The GisCode name and the Gis
logo are not granted as trademark or branding rights, and no ownership of those
names or the logo is asserted here. `TRADEMARK.md` records that position.

Third-party components keep their own licences. They are listed, with their
authors, in `NOTICE`; nothing in the Apache-2.0 grant relicenses them.

## Where the decision is recorded

Five places must agree, and the private release pipeline refuses a release when
they do not:

| File | What it records |
| --- | --- |
| `LICENSE` | the standard, unmodified Apache-2.0 text |
| `NOTICE` | copyright, attribution and third-party components |
| `TRADEMARK.md` | the names and logo; no trademark-rights grant is asserted |
| `README.md` | the `## License` section: Apache-2.0, a link, the copyright line |
| `pyproject.toml` | `[project] license` declaring Apache-2.0 |

The check itself lives in the private release tooling and is not part of this
repository; the files above are the public record you can verify by reading
them.

## Pro and Core

GisCode Pro is built from a separate private tree with its own licensing
metadata. Public Core is Apache-2.0; being in the same upstream repository does
not relicense Pro, and nothing here grants rights to Pro source.

## What is still open

- **A qualified legal review** of the Apache-2.0 decision, the branding and
  logo wording, and the third-party notices before the first public push. The
  text is standard; the review is a project decision, not a formality.
- **A verified vulnerability-reporting route.** `SECURITY.md` describes how to
  report a problem privately. Until the published repository has that channel
  switched on, the release gate keeps release mode blocked.

Until both are settled, this tree is a reviewed release candidate, not a
released product.
