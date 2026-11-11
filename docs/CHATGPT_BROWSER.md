# Core ChatGPT browser (now AI Web Chat)

The ChatGPT-only tab has been replaced by the provider-neutral **AI Web Chat**
tab, in which ChatGPT is the default selected provider. The architecture,
provider support matrix, security boundary and qualification limits are
documented in [`AI_WEB_CHAT.md`](AI_WEB_CHAT.md).

What did not change:

- The ChatGPT browser is still a Core-owned, visible WebView2 window launched in
  an owned child process, because pywebview requires its GUI loop on the process
  main thread.
- Its profile is still the existing isolated `chatgpt_browser/` directory under
  the Core home, never the user's normal browser profile.
- Start, Reload, Close and Forget Session keep their behaviour. Forget removes
  only that isolated profile, and only while the browser is closed.
- Context is still built only from files the user explicitly adds, with path
  containment, credential-file refusal, size limits and secret redaction.
- Imported answers are still labelled `[UNVERIFIED CHATGPT GUIDANCE]` and
  inserted into Chat only after an explicit user action.
- If `pywebview` or the WebView2 runtime is absent, the tab reports that the
  browser is unavailable and Core continues to start and run normally.

`giscode/gui/chatgpt_browser.py` and `giscode/browser/*` remain as compatibility
wrappers that pin the ChatGPT provider; new code uses `giscode/webchat` and
`giscode/gui/ai_web_chat.py`.
