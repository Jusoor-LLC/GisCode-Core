"""Provider definitions for the AI Web Chat surface.

An AI Web Chat provider is a *user-mediated browser destination*: GisCode opens
the provider's official web chat, the user signs in there, reviews and copies a
prompt, and imports the answer as unverified guidance.  It is deliberately not
the same product as a plan OAuth login or a provider API key.

Three access methods exist and are never conflated:

* **browser chat** - a web page the user drives.  ``browser_mode`` says whether
  GisCode can host it in its own isolated WebView2 window (``embedded``) or only
  open it in the user's default browser (``external``).
* **direct API** - the provider's developer API, with its own account, key and
  per-token billing.  A consumer subscription never implies API access.
* **local models** - GisCode's own inference backends (Model Manager); no
  provider is involved and nothing here configures them.

Only a provider whose embedded browser lifecycle has actually been qualified is
``embedded``.  Everything else is opened in the user's own browser, where the
provider's normal sign-in and protections apply untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

#: Selection is stored under this key so it survives restarts.
PROVIDER_PREFERENCE_KEY = "ai_web_chat.provider"

#: The product decision: ChatGPT is the default AI Web Chat provider.
DEFAULT_PROVIDER_ID = "chatgpt"

#: GisCode hosts this provider in its own isolated WebView2 window.
EMBEDDED = "embedded"
#: The provider is opened in the user's default browser; GisCode keeps no session.
EXTERNAL = "external"


@dataclass(frozen=True)
class WebChatCapabilities:
    """The operations a provider adapter may expose to the shared UI.

    A capability is a contract, not a claim of availability.  The provider's
    ``available`` flag and the adapter registry decide whether the operation can
    actually be used.
    """

    project_context: bool = False
    prompt_handoff: bool = False
    response_import: bool = False
    no_project_question: bool = True

    def supports(self, name: str) -> bool:
        return bool(getattr(self, str(name or "").strip(), False))


@dataclass(frozen=True)
class ApiAccess:
    """The provider's direct developer API, reported separately from chat.

    Recording the API here never configures, activates or bills it.  It exists
    so the UI can say which product the user would need, link to the official
    documentation, and state that it is not included in a chat subscription.
    """

    product: str
    docs_url: str
    billing_note: str
    available: bool = True


@dataclass(frozen=True)
class WebChatProvider:
    """Stable provider metadata shared by the UI, adapter and session layer."""

    id: str
    display_name: str
    url: str
    allowed_hosts: tuple[str, ...]
    profile_key: str
    window_title: str
    auth_guidance: str
    capabilities: WebChatCapabilities
    official_source: str
    verified: bool = False
    qualification_note: str = ""
    browser_mode: str = EMBEDDED
    embedding_note: str = ""
    api: ApiAccess | None = None
    api_note: str = ""
    automation_allowed: bool = False
    automation_note: str = (
        "Automation: none. GisCode never types into, clicks or reads a provider "
        "page, and no provider offers a supported chat-automation API."
    )

    @property
    def uses_embedded_browser(self) -> bool:
        return self.browser_mode == EMBEDDED

    @property
    def available(self) -> bool:
        """True when this provider can be opened now, by either browser mode."""
        return bool(self.verified) or self.browser_mode == EXTERNAL

    @property
    def embedded_available(self) -> bool:
        """True only when this provider may run in GisCode's own window."""
        return bool(self.verified) and self.uses_embedded_browser

    @property
    def status_label(self) -> str:
        if self.verified:
            return "Ready"
        if self.browser_mode == EXTERNAL:
            return "Ready in your browser"
        return "Not yet qualified"

    @property
    def method_label(self) -> str:
        """How this provider's chat is reached, in one honest line."""
        if self.verified:
            return "Browser chat — GisCode's isolated window"
        if self.browser_mode == EXTERNAL:
            return "Browser chat — your own browser (GisCode keeps no session)"
        return "Browser chat — not yet qualified in this build"

    @property
    def session_note(self) -> str:
        if self.browser_mode == EXTERNAL:
            return (
                "Session: your own browser (the default one). GisCode cannot "
                "see, store or end it, and keeps no profile for this provider."
            )
        return (
            "Session: sign in inside GisCode's isolated window. GisCode never "
            "reads, stores or exports the cookies, password or MFA codes."
        )

    @property
    def recovery_guidance(self) -> str:
        """What the user does when a provider session ends. Never automatic."""
        if self.browser_mode == EXTERNAL:
            return (
                "If you are signed out, sign in again in your own browser and "
                "reopen the page; GisCode has no session to restore."
            )
        return (
            "If the session expires, close or Reload the window and sign in "
            "again. GisCode cannot see, restore or bypass the provider's session."
        )

    @property
    def api_label(self) -> str:
        if self.api is None:
            return self.api_note or "Direct API: none published for this assistant."
        return (
            f"Direct API: {self.api.product} — separate account and billing. "
            f"{self.api.billing_note}"
        )

    @property
    def open_label(self) -> str:
        if self.browser_mode == EXTERNAL:
            return f"Open {self.display_name} in Your Browser"
        return f"Open {self.display_name}"

    def allows_url(self, url: str) -> bool:
        """True only for a plain HTTPS URL on this provider's official hosts."""
        try:
            parsed = urlparse(str(url or "").strip())
        except ValueError:
            return False
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return False
        try:
            port = parsed.port
        except ValueError:
            return False
        if port not in (None, 443):
            return False
        return (parsed.hostname or "").lower() in set(self.allowed_hosts)


_CHATGPT = WebChatProvider(
    id="chatgpt",
    display_name="ChatGPT",
    url="https://chatgpt.com/",
    allowed_hosts=("chatgpt.com", "www.chatgpt.com", "chat.openai.com"),
    profile_key="chatgpt",
    window_title="GisCode — ChatGPT",
    auth_guidance=(
        "Sign in inside the isolated ChatGPT window. GisCode never reads or "
        "stores your ChatGPT cookies, password, or MFA codes."
    ),
    capabilities=WebChatCapabilities(
        project_context=True,
        prompt_handoff=True,
        response_import=True,
        no_project_question=True,
    ),
    official_source="https://chatgpt.com/",
    verified=True,
    api=ApiAccess(
        product="OpenAI Platform API",
        docs_url="https://platform.openai.com/docs/api-reference",
        billing_note=(
            "A ChatGPT Plus/Pro subscription is not API access; API usage is "
            "billed per token in a separate OpenAI Platform account."
        ),
    ),
)

# The remaining providers are opened in the user's own browser. Their embedded
# sign-in has not been qualified in this build, and Google blocks account
# sign-in from embedded webviews outright, so GisCode never pretends to host
# their session. Official URLs are recorded so the user lands on the real site.
_CLAUDE = WebChatProvider(
    id="claude",
    display_name="Claude",
    url="https://claude.ai/",
    allowed_hosts=("claude.ai", "www.claude.ai"),
    profile_key="claude",
    window_title="GisCode — Claude",
    auth_guidance=(
        "Sign in on claude.ai in your own browser. GisCode does not extract "
        "cookies, passwords, or authentication codes."
    ),
    capabilities=WebChatCapabilities(
        project_context=True, prompt_handoff=True, response_import=True
    ),
    official_source=(
        "https://support.claude.com/en/articles/8114491-get-started-with-claude"
    ),
    browser_mode=EXTERNAL,
    embedding_note=(
        "Claude's embedded sign-in is not qualified in this build, so the "
        "official site opens in your default browser."
    ),
    api=ApiAccess(
        product="Claude API (Console)",
        docs_url="https://docs.claude.com/en/api/overview",
        billing_note=(
            "A paid Claude plan covers Claude apps; Claude API usage is billed "
            "separately through Claude Console."
        ),
    ),
)

_GEMINI = WebChatProvider(
    id="gemini",
    display_name="Gemini",
    url="https://gemini.google.com/app",
    allowed_hosts=("gemini.google.com",),
    profile_key="gemini",
    window_title="GisCode — Gemini",
    auth_guidance=(
        "Sign in on gemini.google.com in your own browser. GisCode does not "
        "read or store Google account cookies, passwords, or MFA codes."
    ),
    capabilities=WebChatCapabilities(
        project_context=True, prompt_handoff=True, response_import=True
    ),
    official_source="https://gemini.google.com/",
    browser_mode=EXTERNAL,
    embedding_note=(
        "Google blocks Google Account sign-in from embedded webviews "
        "(disallowed_useragent), so Gemini must open in your default browser."
    ),
    api=ApiAccess(
        product="Gemini API (Google AI Studio)",
        docs_url="https://ai.google.dev/gemini-api/docs",
        billing_note=(
            "Google AI plan benefits apply inside AI Studio; API keys and "
            "external applications are billed and managed separately."
        ),
    ),
)

_GROK = WebChatProvider(
    id="grok",
    display_name="Grok",
    url="https://grok.com/",
    allowed_hosts=("grok.com", "www.grok.com"),
    profile_key="grok",
    window_title="GisCode — Grok",
    auth_guidance=(
        "Sign in on grok.com in your own browser. GisCode does not extract or "
        "store your X or Grok credentials."
    ),
    capabilities=WebChatCapabilities(
        project_context=True, prompt_handoff=True, response_import=True
    ),
    official_source="https://docs.x.ai/docs/overview",
    browser_mode=EXTERNAL,
    embedding_note=(
        "Grok's embedded sign-in is not qualified in this build, so the "
        "official site opens in your default browser."
    ),
    api=ApiAccess(
        product="xAI API",
        docs_url="https://docs.x.ai/",
        billing_note=(
            "The xAI API uses its own API key and account; a Grok or X "
            "subscription does not include API access."
        ),
    ),
)

_DEEPSEEK = WebChatProvider(
    id="deepseek",
    display_name="DeepSeek",
    url="https://chat.deepseek.com/",
    allowed_hosts=("chat.deepseek.com",),
    profile_key="deepseek",
    window_title="GisCode — DeepSeek",
    auth_guidance=(
        "Sign in on chat.deepseek.com in your own browser. GisCode does not "
        "extract or store your DeepSeek credentials."
    ),
    capabilities=WebChatCapabilities(
        project_context=True, prompt_handoff=True, response_import=True
    ),
    official_source="https://www.deepseek.com/about",
    browser_mode=EXTERNAL,
    embedding_note=(
        "DeepSeek's embedded sign-in is not qualified in this build, so the "
        "official site opens in your default browser."
    ),
    api=ApiAccess(
        product="DeepSeek API",
        docs_url="https://api-docs.deepseek.com/",
        billing_note=(
            "The DeepSeek web chat and the DeepSeek API are separate products; "
            "API usage is charged to your API balance."
        ),
    ),
)

_META_AI = WebChatProvider(
    id="meta_ai",
    display_name="Meta AI",
    url="https://www.meta.ai/",
    allowed_hosts=("meta.ai", "www.meta.ai"),
    profile_key="meta_ai",
    window_title="GisCode — Meta AI",
    auth_guidance=(
        "Sign in on meta.ai in your own browser if the provider requires it. "
        "GisCode does not extract or store Meta credentials."
    ),
    capabilities=WebChatCapabilities(
        project_context=True, prompt_handoff=True, response_import=True
    ),
    official_source="https://ai.meta.com/discover",
    browser_mode=EXTERNAL,
    embedding_note=(
        "Meta AI's embedded sign-in is not qualified in this build, so the "
        "official site opens in your default browser."
    ),
    api=None,
    api_note=(
        "Direct API: none for the Meta AI assistant. Meta's Llama API is a "
        "separate developer product for Llama models, not this chat."
    ),
)

#: Display order in the selector.  ChatGPT stays first and is the default.
PROVIDERS: tuple[WebChatProvider, ...] = (
    _CHATGPT,
    _CLAUDE,
    _GEMINI,
    _GROK,
    _DEEPSEEK,
    _META_AI,
)

CHATGPT_PROVIDER = _CHATGPT

PROVIDER_BY_ID: dict[str, WebChatProvider] = {p.id: p for p in PROVIDERS}


def get_provider(provider_id: str | None) -> WebChatProvider | None:
    """Return a provider by stable id, or ``None`` for an unknown id."""
    return PROVIDER_BY_ID.get(str(provider_id or "").strip())


def require_provider(provider_id: str | None) -> WebChatProvider:
    provider = get_provider(provider_id)
    if provider is None:
        raise KeyError(f"unknown AI Web Chat provider: {provider_id!r}")
    return provider
