# Core AI Web Chat

## Decision

Core exposes an **AI Web Chat** tab. ChatGPT is the default selected provider
and the only one GisCode embeds in its own window; Claude, Gemini, Grok,
DeepSeek and Meta AI open in the user's default browser. The architecture is
provider-neutral: the registry, browser/session manager, context builder,
handoff and guidance import know a provider only through its metadata.

Three access methods are shown separately in the UI and are never substituted
for one another:

| Method | What it is | Who owns the session |
| --- | --- | --- |
| Browser chat — embedded | ChatGPT in GisCode's isolated WebView2 window | the isolated profile, driven by the user |
| Browser chat — external | the provider's own site in the user's browser | the user's browser; GisCode keeps nothing |
| Direct API | the provider's developer API, own key and per-token bill | the user's API account |
| Local models | GisCode's Ollama / LM Studio / llama.cpp backends | GisCode, via Model Manager |

A chat subscription is never presented as API access. Selecting a web-chat
provider writes only `ai_web_chat.provider`; it never changes the local model,
the inference backend, an API key or a plan login.

This surface is *manual, user-mediated* web chat. It is deliberately separate
from Sign in with ChatGPT (plan OAuth/Responses) and from the OpenAI API key:

| Concern | Where it lives |
| --- | --- |
| AI Web Chat provider choice | settings key `ai_web_chat.provider` |
| ChatGPT plan (SIWC) | `giscode/providers/chatgpt_*`, `providers.chatgpt` |
| OpenAI API billing | `giscode/providers/openai.py`, API-key settings |

Selecting a provider never reads or writes either of the other two.

## Provider support matrix

| Id | Official destination | Chat access | Session | Direct API |
| --- | --- | --- | --- | --- |
| `chatgpt` | `https://chatgpt.com/` | embedded (default) | isolated GisCode profile | OpenAI Platform API, separate billing |
| `claude` | `https://claude.ai/` | your browser | your browser, none kept | Claude API (Console), separate billing |
| `gemini` | `https://gemini.google.com/app` | your browser | your browser, none kept | Gemini API, separate billing |
| `grok` | `https://grok.com/` | your browser | your browser, none kept | xAI API, separate key |
| `deepseek` | `https://chat.deepseek.com/` | your browser | your browser, none kept | DeepSeek API, separate balance |
| `meta_ai` | `https://www.meta.ai/` | your browser | your browser, none kept | none for the assistant; Llama API is a different product |

Why the split: Google blocks Google Account sign-in from embedded webviews
(`disallowed_useragent`), and no other provider's embedded sign-in has been
qualified in this build. GisCode therefore embeds only ChatGPT and opens the
rest in the user's own browser, where the provider's normal sign-in and
protections apply unchanged. It never automates a page, reads a DOM, or drives a
login. OpenAI's and Anthropic's consumer terms prohibit automated or
programmatic access and data extraction outside their APIs
(<https://openai.com/policies/terms-of-use/>,
<https://www.anthropic.com/legal/consumer-terms>), which is why Pro offers no
browser automation of these sites.

## Architecture

| Responsibility | Module |
| --- | --- |
| Provider registry and definitions | `giscode/webchat/providers.py`, `registry.py` |
| Browser/session manager | `giscode/webchat/session.py` |
| Provider adapters | `giscode/webchat/adapters.py` |
| Shared context preparation and redaction | `giscode/webchat/context.py` |
| Prompt preview and user-mediated handoff | `giscode/gui/ai_web_chat.py` |
| Unverified import and source attribution | `giscode/webchat/guidance.py` |
| Owned WebView2 host and child process | `giscode/webchat/host.py`, `worker.py` |
| GUI surface | `giscode/gui/ai_web_chat.py` |

`giscode/browser/*` and `giscode/gui/chatgpt_browser.py` are compatibility
wrappers over the shared implementation: they pin the ChatGPT provider and keep
the older import paths working.

## Security boundary

- ChatGPT has its own isolated profile at `%GISCODE_HOME%\chatgpt_browser`.
  External providers have no GisCode profile at all: their site opens in the
  user's browser, and GisCode can neither read nor clear that session.
- Core does not read, store, or transmit passwords, MFA codes, cookies, tokens,
  history, or downloaded files, and never automates a login.
- The browser is restricted to the selected provider's official HTTPS hosts at
  startup; foreign hosts, non-HTTPS URLs, credentials in the URL and non-default
  ports are refused.
- Context comes only from files the user explicitly adds, resolved against the
  active project root. Absolute project paths never enter the prompt.
- Credential-like names and suffixes are refused. Common secret formats
  (private keys, token shapes, `key = value` assignments) are redacted, including
  inside the user's question and the imported answer.
- With no project selected, only the user's question is prepared; no project
  context is invented and no directory is scanned.
- Nothing is copied or opened until the user presses a button and confirms the
  export dialog. Core never types into the page, reads its DOM, or sends a
  prompt automatically.
- Imported answers are labelled `[UNVERIFIED <PROVIDER> GUIDANCE]`, carry the
  provider id, project, context SHA-256 and import time, and are inserted into
  Chat only on an explicit action. They are advisory, never executed.

## Workflow

1. Optionally open a project. AI Web Chat also works with no project.
2. Choose a provider (ChatGPT by default). The choice is saved and restored on
   the next start.
3. Add project files if you want file context, then type a question.
4. **Prepare Context** and inspect the bounded, redacted prompt preview.
5. **Copy Prompt & Open ChatGPT** (or the selected provider) after confirming.
6. Sign in and paste the prompt in the provider's own window.
7. Copy the answer, return to Core, **Import Response from Clipboard**, review,
   then **Insert into Chat as Unverified Guidance**.

Switching providers clears the prepared prompt and imported guidance (they name
one provider), but never touches the active project, the selected file list, the
Chat tab, or other conversations.

**Forget Browser Session** removes only the selected provider's isolated profile,
and only while that browser is closed.

## Qualification limits

Unit and GUI tests use mocked browser processes for deterministic lifecycle
coverage. A real WebView2 smoke test verifies startup and shutdown without a
provider account. An authenticated login and a real answer import require the
user to sign in; no credential or automation bypass is used.

No external provider's *embedded* login has been qualified, and Google's policy
rules its embedded login out, so every external provider is presented as
"Ready in your browser" rather than as a GisCode-hosted session. That is a
supported, manual workflow, not a claim that GisCode signs in for the user.
