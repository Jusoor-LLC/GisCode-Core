# First run: getting to a working local model

A new GisCode Core installation has no model yet. On first launch GisCode shows a
short setup window instead of dropping you into a chat with nothing to select.
Setup finishes when a real local coding task has passed - not when a file exists
on disk.

## What setup does

1. **Finds Ollama.** It checks for the `ollama` executable *and* whether the
   service answers on `127.0.0.1:11434`. Those are reported separately, because
   installed-but-not-running is the common case.
2. **Measures this computer.** RAM, VRAM and GPU name come from the same
   resource budget the model loader uses; free disk space is measured on the
   profile drive.
3. **Recommends a model** from a small catalogue, with its download size and the
   free space it needs. A model that does not fit is shown as refused, with the
   reason, rather than hidden.
4. **Downloads only after you confirm.** The transfer is Ollama's own
   `ollama pull`, so it resumes in Ollama's blob store. Cancel and Retry are
   explicit; a closed window leaves the state *interrupted*, never *done*.
5. **Test My AI** sends one real request and records the answer, the latency and
   which checks passed.
6. **The first coding task** hands the model a broken `greeting.py` in a
   disposable folder, applies its single-file fix, shows the diff, and runs the
   assertion. Setup completes only when that assertion passes.

## What GisCode will not do

- It will not install Ollama for you. You get the official page
  (<https://ollama.com/download>) and steps; you run the installer.
- It will not download a model without your confirmation.
- It will not substitute a different model because a pull was slow or a test
  failed.
- It will not touch an existing installation's configuration. If a profile
  already has a selected model, first-run setup is marked complete and skipped.

## Headless use

```bash
py -3.13 -m giscode.main --onboarding-check
py -3.13 -m giscode.main --onboarding-run --model qwen2.5-coder:7b --yes
```

`--onboarding-check` prints the persisted state as JSON and exits 0. The run form
performs the whole sequence and exits 0 only after the first coding task passes;
`--yes` is the confirmation for the download, and without it nothing is
transferred.

## Where state lives

`onboarding` in `~/.giscode/config.json` (or `GISCODE_HOME/config.json`),
written atomically and versioned, so a crash resumes at the last finished stage.
Drafts, conversations, providers and profiles are never modified by setup.
