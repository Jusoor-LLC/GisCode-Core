# The optional hosted writer

GisCode can send its planner/writer model calls to a hosted LLM service instead
of the local model. File edits, command execution and test verification still
run locally in your project, exactly as before; only the model calls move.

**This is off by default.** Nothing leaves the machine until you enable it and
supply your own API key.

## Requirements

- An API key for the hosted provider, placed in the environment variable
  `GISCODE_HOSTED_API_KEY` for the **GisCode process** (set it in the shell you
  start GisCode from, then restart GisCode).
- The key is read only from that variable, only when a call is made. GisCode
  does not store it in its configuration and does not print it: health output
  shows a masked proof such as `key present (sk..., 83 chars)`.

## Enabling it

1. Settings -> Hosted writer.
2. Tick **Enabled** and choose a model id.
3. Save, then restart GisCode. The provider is built at startup.

Defaults (also in `giscode/config/settings.py`):

- base URL of the provider's OpenAI-compatible endpoint;
- request timeout 180 s;
- request size cap 500 000 characters;
- `hosted.max_calls = 0` (no cap). Set it to a positive number to limit total
  calls for a run;
- `hosted.project_overrides` maps a project folder to true/false and wins over
  the global switch for that project.

## What you should see

- The Send gate opens without loading a local model; health shows
  `hosted: true`, `model_state: hosted`, `key present`.
- If the key is missing, Send replies with
  `[Hosted writer not ready] ... Set the GISCODE_HOSTED_API_KEY environment
  variable and restart GisCode.`
- Tasks then run the normal loop (plan -> edit -> verify -> repair) with every
  planner/writer call going to the hosted endpoint. Repairs stop early when the
  same failure repeats three times.

## Troubleshooting

- **`hosted endpoint rejected the key (HTTP 401/403)`** — the variable is set
  but the key is wrong or expired. Fix it in the shell and restart.
- **`hosted call budget exhausted`** — `hosted.max_calls` reached its cap.
  Raise the setting or reset it to 0.
- **Switching back to local** — untick Enabled and restart. The local model path
  is unchanged.

## Honest limits

The hosted writer is a convenience for people who already have a provider
account. It was developed against one provider's OpenAI-compatible endpoint;
other providers may differ in the model ids they accept and in how they report
errors. It is not a way to run GisCode without an account, and it is never
enabled implicitly. The rest of this repository — including the test suite —
runs without it.
