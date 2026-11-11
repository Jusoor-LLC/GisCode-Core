# Direct OpenAI API access

GisCode Core can talk to OpenAI directly through the **official API**. This is
the one remote provider Core supports. It is optional, billable, disabled by
default, and it never sends a request until you explicitly enable billable
calls.

Everything else in Core is unchanged: the built-in `llama.cpp` runtime, the
Ollama provider, the LM Studio provider and any OpenAI-compatible local
endpoint keep working exactly as before. See
[`LOCAL_AI_SETUP.md`](LOCAL_AI_SETUP.md) for those.

---

## 1. What this is, and what it is not

| | |
| --- | --- |
| **It is** | A connection to `https://api.openai.com` using an API key from your own OpenAI API account. |
| **It is not** | ChatGPT. A ChatGPT Plus/Pro/Business subscription is a different product and does **not** grant API access or include API usage. |
| **It is not** | A browser automation. GisCode does not read cookies, passwords, MFA codes, session tokens or subscription state, and it does not sign in as you. |
| **It is not** | A free allowance. Every request is billed by OpenAI to your API account at OpenAI's current prices. |

If you only have a ChatGPT subscription, use the browser handoff described in
[`CHATGPT_BROWSER.md`](CHATGPT_BROWSER.md) instead. It is a separate, manual
route and it is not API access either.

---

## 2. How it is wired

The gateway is provider-neutral. OpenAI is the first live remote provider, not
a special case in the planner.

```
Chat / Projects
      │
      ▼
CodingAgentLoop  ──►  ModelRouter  ──►  ProviderChatBackend
      │                                      │
      │                                      ▼
      │                              OpenAIProvider  ──►  https://api.openai.com
      │                                      │
      └── approval gate, workspace, verifier │
                                             ├── CredentialStore (env / DPAPI)
                                             ├── UsageLedger (tokens, cost)
                                             └── redaction (last outbound boundary)
```

| Piece | File | Responsibility |
| --- | --- | --- |
| Provider registry | `giscode/providers/registry.py` | Defaults, construction, per-provider settings, credential injection. |
| Official provider | `giscode/providers/openai.py` | Endpoint pinning, consent gate, `GET /v1/models`, `POST /v1/chat/completions`, streaming, usage, safe errors. |
| Credentials | `giscode/providers/credentials.py` | Environment, in-memory (tests) and Windows DPAPI stores; masked status only. |
| Usage | `giscode/providers/usage.py` | Request/token counters and optional JSON Lines ledger; no prompt text. |
| Redaction | `giscode/providers/redaction.py` | Removes known secrets plus credential-shaped text. |
| Inference adapter | `giscode/inference/provider_backend.py` | Presents the provider as an `InferenceAPI`; cancellation, idle timeout, attribution, remote-image consent. |
| Router | `giscode/inference/router.py` | Exposes `is_provider_backend` so the loop does not also try to load a GGUF. |
| Agent loop | `giscode/agentic/loop.py` | Plans, implements and repairs through the provider; keeps parsing, approval and verification. |
| Settings UI | `giscode/gui/main_window.py` | The **OpenAI API (optional, billable)** section. |

A profile of kind `openai` is a normal coding profile: it is selected the same
way as an Ollama or LM Studio profile, and switching back to a local profile is
immediate.

---

## 3. Setup

### 3.1 Get an API key

Create a key in your OpenAI API account (API keys page) and make sure that
account has billing configured. GisCode cannot see or manage your OpenAI
billing.

### 3.2 Give the key to GisCode

Two supported routes, both outside `config.json`:

| Route | How | Notes |
| --- | --- | --- |
| Environment variable | Set `OPENAI_API_KEY` before launching GisCode. | Read at call time; nothing is written to disk by GisCode. |
| Protected local store | **Settings → OpenAI API → API key → Save key securely**. | Encrypted with Windows DPAPI for the current Windows user, under `%USERPROFILE%\.giscode\credentials\openai.bin`. |

Resolution order is: an explicitly injected key (tests/tools), then the
environment variable, then the protected store. The GUI shows only a masked
status such as `API key: set in the protected local store`; it never shows the
value back.

**Remove saved key** deletes the DPAPI file. Removing the key, unchecking the
consent box, or switching to a local profile stops all further OpenAI requests.

### 3.3 Enable billable calls

The billing disclosure shown in Settings is:

> OpenAI API requests are billed to your OpenAI API account. A ChatGPT
> subscription is not API access and does not include these requests. GisCode
> makes no paid request until you explicitly enable billable calls.

Tick **Allow billable OpenAI API calls**, enter a model id that
`GET /v1/models` returns (for example `gpt-4.1`), tick **Use OpenAI for coding
tasks**, and press **Save OpenAI settings**.

Order matters in one direction only: while the consent box is off, GisCode sends
**no** request at all — not even the free model list. `status()`, `list_models()`,
`chat()` and `stream_chat()` all refuse before opening a socket.

### 3.4 Verify

- **Refresh key status** shows whether a key is visible and the usage line for
  this process.
- **Save OpenAI settings** persists the profile and consent; it makes no network
  request.
- The first real request happens when you run a Projects task or a Chat turn
  that uses the `coding` profile.

---

## 4. Billing and usage visibility

GisCode does not guess prices. A cost estimate appears **only** when you
configure both prices for the provider:

```json
{
  "providers": {
    "openai": {
      "pricing": { "input_per_million": 1.25, "output_per_million": 10.0 }
    }
  }
}
```

Otherwise the usage line reports tokens and says `cost not configured`. This is
deliberate: a shipped price table would go stale and turn an estimate into a
false claim. Check your OpenAI dashboard for the authoritative charge.

Every recorded entry contains: timestamp, provider, model, request id, prompt
tokens, completion tokens, total tokens, finish reason, and the optional cost
estimate. It never contains prompt text, completion text or the key. Set
`providers.openai.usage_path` to append the ledger as JSON Lines; otherwise it
stays in memory for the process.

---

## 5. What leaves the machine

| Control | Behaviour |
| --- | --- |
| Redaction | Messages are redacted at the provider boundary **and** again in `ProviderChatBackend` for any remote endpoint. Known secret values, `api_key = …`, bearer tokens, `sk-…` keys and PEM private keys are replaced with `[REDACTED]`. Field names are kept so the request stays readable. |
| Server error echo | An error returned by the service is redacted with the same rules before it reaches a log line, the GUI or an exception message. |
| Request size | Outbound text is capped (`max_request_chars`, 200 000 by default). A larger request is refused locally, not truncated silently. |
| Images | Not sent. The OpenAI provider declares no vision capability, and a remote provider refuses an attachment unless the user separately allows images to leave the PC. |
| Project context | GisCode does not attach a project automatically. The existing context assembler decides what a request contains, and workspace confinement is unchanged: the model cannot read or write outside the workspace. |
| Approval | Remote guidance is still a proposal. Editing actions go through the normal approval gate and the verifier; nothing is "trusted" because it came from a bigger model. |
| Isolation | Provider configuration, credentials and usage live under your `GISCODE_HOME`. There is no cross-project credential or usage state. |

Redaction is pattern-based defence in depth, not a guarantee that no sensitive
text can ever be phrased in a way it misses. Do not paste secrets into a task.

---

## 6. Requests, streaming, cancellation, failures

| Behaviour | Detail |
| --- | --- |
| Non-streaming | `POST /v1/chat/completions` with `stream: false`, `max_completion_tokens`, `temperature`; the first message content is returned. |
| Streaming | `stream: true` plus `stream_options: {"include_usage": true}`; deltas are surfaced as `(text, finish_reason, tokens)` exactly like a local provider, so Chat does not need a special path. |
| Attribution | `ChatResponse` / `GenerationResponse` carry `provider`, `model` and the caller's `request_id`; the usage ledger records the same request id. |
| Cancellation | A Stop before the request starts sends nothing. A Stop during a stream stops consuming and raises a cancellation error with the partial text preserved and labelled as incomplete. An in-flight HTTP call cannot be torn down from the client, so a late answer is discarded rather than presented. |
| Timeouts | A non-streaming call uses at least a 180 s budget by default. Streaming has a caller-set inactivity budget (5 s default in Chat) and reports exactly which limit was hit, keeping partial text. |
| Retry | **No automatic retry of a billable completion.** A timeout or 5xx is surfaced once and nothing is re-sent, because a retry may be a second charge. Recovery is an explicit user action. |
| Ending early | A stream that ends without a completion signal is an error, never an empty success. |

---

## 7. Troubleshooting

| Symptom | Meaning | Fix |
| --- | --- | --- |
| `OpenAI API key is not configured.` | No key in the environment or the protected store. | Set `OPENAI_API_KEY` or save the key in Settings. No request was sent. |
| `OpenAI billable API calls are disabled.` | The consent box is off. | Review the disclosure, then enable it. |
| `... key was rejected ...` | OpenAI returned 401/403. | Create a valid key; the previous key is not retried. |
| `... did not answer within Ns` | Timeout. | Check the network; nothing was retried automatically. |
| `The OpenAI request is too large` | The assembled context exceeded the cap. | Reduce the task context or raise `max_request_chars`. |
| Model missing from the list | Consent or key is missing, or the id is not on your account. | Fix consent/key first, then refresh. |
| `This image was not sent.` | Remote attachments are off. | Enable remote images deliberately, or use a local provider. |

The official provider also refuses any endpoint other than `api.openai.com`. To
use a different OpenAI-shaped server (local or third-party), add an
**OpenAI-compatible** provider instead; that route has its own consent and
remote-endpoint rules.

---

## 8. Honest limits

- Only OpenAI is implemented as a live remote provider in this build.
- No automatic retry of a billable request (by design).
- No streaming abort at the TCP level; cancellation stops presentation and
  discards a late answer.
- Cost estimates need prices supplied by you; GisCode does not maintain a price
  list.
- Redaction is pattern-based, not a data-loss-prevention product.
- Vision through OpenAI is not enabled; images stay local.
- The subscription/browser route is separate and is not API access.

---

## 9. Adding the next provider

The gateway is not OpenAI-specific. A new provider needs:

1. a subclass of `Provider` in `giscode/providers/` with its own endpoint rules,
   `chat`, optional `stream_chat`, `list_models` and `secret_values`;
2. a kind in `giscode/providers/base.py` and a builder plus defaults in
   `giscode/providers/registry.py`;
3. the same consent/credential pattern if it is remote and billable;
4. tests proving the refusal paths happen before any network request.

Planned, but **not implemented**: Anthropic Claude, Google Gemini, xAI Grok,
DeepSeek, and Meta Muse (only if an official developer API exists). Local
Ollama and `llama.cpp` are already supported.

---

## 10. Related reading

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — where the provider layer sits.
- [`LOCAL_AI_SETUP.md`](LOCAL_AI_SETUP.md) — local models and providers.
- [`CHATGPT_BROWSER.md`](CHATGPT_BROWSER.md) — the separate browser handoff.
- [`USER_GUIDE.md`](USER_GUIDE.md) — the interface.
