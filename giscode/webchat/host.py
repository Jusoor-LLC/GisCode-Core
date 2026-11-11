"""Provider-neutral lifecycle controller for isolated AI Web Chat browsers."""
from __future__ import annotations

import importlib.util
import multiprocessing
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

from giscode.config.paths import giscode_home

from .providers import CHATGPT_PROVIDER, WebChatProvider, get_provider
from .worker import run_webview2_process


class BrowserHostError(RuntimeError):
    """Base class for a browser lifecycle failure."""


class BrowserUnavailableError(BrowserHostError):
    """The optional browser engine, runtime, or qualified adapter is absent."""


class BrowserStartError(BrowserHostError):
    """The owned browser process could not be started."""


@dataclass(frozen=True)
class BrowserRuntimeStatus:
    available: bool
    reason: str = ""
    engine: str = "WebView2"


class _Process(Protocol):
    pid: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


ProcessFactory = Callable[[str, Path], _Process]

#: Set only by the acceptance executable's scenario runner.  If it is present,
#: profile deletion is an acceptance operation and must prove its disposable
#: home; an ordinary application launch never sets it.
_ACCEPTANCE_SCENARIO_ENV = "GISCODE_ACCEPTANCE_SCENARIO"


def _verify_acceptance_profile_removal(target: Path) -> None:
    """Fail closed before an acceptance run can remove a profile.

    The normal application owns ``giscode_home()`` and may forget its own
    browser session there.  An acceptance run is different: it can be pointed
    at a real profile by inherited environment, so it must carry the separate
    possession marker and disposable-home declaration.  This check is repeated
    at the destructive call site, not only when the scenario is scheduled.
    """
    from giscode.acceptance_safety import (
        ENV_DISPOSABLE,
        DisposableHomeError,
        verify_destructive_target,
        verify_disposable_home,
    )

    acceptance_context = bool(
        os.environ.get(ENV_DISPOSABLE)
        or os.environ.get(_ACCEPTANCE_SCENARIO_ENV)
    )
    if not acceptance_context:
        return
    if not os.environ.get(ENV_DISPOSABLE):
        raise BrowserHostError(
            "acceptance profile deletion requires a declared disposable home"
        )
    try:
        home = verify_disposable_home()
        verify_destructive_target(target, home)
    except DisposableHomeError as exc:
        raise BrowserHostError(
            f"refusing to delete browser profile: {exc.reason}"
        ) from exc


def default_profile_root(provider: WebChatProvider | str | None = None) -> Path:
    """Return a provider-scoped profile directory.

    ChatGPT deliberately keeps the existing ``chatgpt_browser`` directory so an
    existing isolated ChatGPT session is preserved.  Every additional provider
    gets its own directory under ``ai_web_chat``; no two providers share cookies
    or storage.
    """
    if provider is None:
        selected = CHATGPT_PROVIDER
    elif isinstance(provider, str):
        selected = get_provider(provider) or CHATGPT_PROVIDER
    else:
        selected = provider
    base = giscode_home()
    if selected.id == CHATGPT_PROVIDER.id:
        return (base / "chatgpt_browser").resolve()
    return (base / "ai_web_chat" / selected.profile_key).resolve()


def probe_webview2_runtime() -> BrowserRuntimeStatus:
    """Detect pywebview and the system WebView2 runtime without importing it."""
    if os.name != "nt":
        return BrowserRuntimeStatus(False, "the integrated WebView2 browser is Windows-only")
    try:
        if importlib.util.find_spec("webview") is None:
            return BrowserRuntimeStatus(
                False,
                "pywebview is not installed; install the optional 'webview' dependency",
            )
    except (ImportError, ValueError, AttributeError) as exc:
        return BrowserRuntimeStatus(
            False, f"browser package probe failed: {type(exc).__name__}"
        )
    candidates = [
        os.environ.get("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", ""),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", ""),
            "Microsoft",
            "EdgeWebView",
            "Application",
        ),
        os.path.join(
            os.environ.get("ProgramFiles", ""),
            "Microsoft",
            "EdgeWebView",
            "Application",
        ),
    ]
    for candidate in candidates:
        if candidate and any(Path(candidate).glob("*/msedgewebview2.exe")):
            return BrowserRuntimeStatus(True, "", "WebView2")
    return BrowserRuntimeStatus(
        False,
        "the Microsoft WebView2 runtime is not installed",
    )


def _validate_url(url: str, provider: WebChatProvider) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise BrowserStartError(
            "the AI Web Chat browser requires a plain HTTPS URL"
        )
    host = (parsed.hostname or "").lower()
    if not provider.allows_url(parsed.geturl()):
        raise BrowserStartError(
            f"the integrated browser is restricted to {provider.display_name}"
        )
    if not host:
        raise BrowserStartError("the AI Web Chat URL has no host")
    return parsed.geturl()


def _process_alive(process: _Process) -> bool:
    """Support both multiprocessing.Process and small test doubles."""
    poll = getattr(process, "poll", None)
    if callable(poll):
        try:
            return poll() is None
        except Exception:
            return False
    is_alive = getattr(process, "is_alive", None)
    if callable(is_alive):
        try:
            return bool(is_alive())
        except Exception:
            return False
    return getattr(process, "exitcode", None) is None


def _process_exitcode(process: _Process) -> int | None:
    """Return a process code, or None while it is still alive."""
    poll = getattr(process, "poll", None)
    if callable(poll):
        try:
            return poll()
        except Exception:
            return None
    is_alive = getattr(process, "is_alive", None)
    if callable(is_alive):
        try:
            if is_alive():
                return None
        except Exception:
            return None
    return getattr(process, "exitcode", None)


class WebChatBrowserHost:
    """Own at most one visible browser process for one provider."""

    def __init__(
        self,
        provider: WebChatProvider,
        profile_root: str | Path | None = None,
        *,
        probe: Callable[[], BrowserRuntimeStatus] = probe_webview2_runtime,
        process_factory: ProcessFactory | None = None,
        url: str | None = None,
    ) -> None:
        self._provider = provider
        self._profile_root = (
            Path(profile_root).expanduser().resolve()
            if profile_root
            else default_profile_root(provider)
        )
        self._probe = probe
        self._process_factory = process_factory
        self._url = _validate_url(url or provider.url, provider)
        self._process: _Process | None = None
        self._status = "Browser closed"
        self._last_error = ""

    @property
    def provider(self) -> WebChatProvider:
        return self._provider

    @property
    def profile_root(self) -> Path:
        return self._profile_root

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        if _process_alive(self._process):
            return True
        self._finish_process()
        return False

    def runtime_status(self) -> BrowserRuntimeStatus:
        if not self._provider.embedded_available:
            return BrowserRuntimeStatus(
                False,
                self._provider.qualification_note
                or f"{self._provider.display_name} adapter is not qualified",
                "WebView2",
            )
        try:
            return self._probe()
        except Exception as exc:  # noqa: BLE001 - a probe must never break Core
            return BrowserRuntimeStatus(
                False, f"browser probe failed: {type(exc).__name__}"
            )

    def _default_process_factory(self, url: str, profile_root: Path) -> _Process:
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(
            target=run_webview2_process,
            args=(url, str(profile_root), self._provider.window_title),
            name=f"giscode-{self._provider.profile_key}-browser",
            daemon=True,
        )
        process.start()
        return process

    def start(self, url: str | None = None) -> None:
        if self._process is not None and _process_alive(self._process):
            raise BrowserStartError("the AI Web Chat browser is already running")
        if self._process is not None:
            self._finish_process()
        if not self._provider.embedded_available:
            reason = (
                self._provider.qualification_note
                or f"{self._provider.display_name} adapter is not qualified"
            )
            self._status = f"Browser unavailable: {reason}"
            self._last_error = reason
            raise BrowserUnavailableError(reason)
        target = _validate_url(url or self._url, self._provider)
        runtime = self.runtime_status()
        if not runtime.available:
            self._status = f"Browser unavailable: {runtime.reason}"
            self._last_error = runtime.reason
            raise BrowserUnavailableError(runtime.reason)
        try:
            self._profile_root.mkdir(parents=True, exist_ok=True)
            factory = self._process_factory or self._default_process_factory
            self._process = factory(target, self._profile_root)
        except BrowserHostError:
            raise
        except Exception as exc:  # noqa: BLE001 - report, never crash Core
            self._process = None
            self._last_error = f"browser start failed: {type(exc).__name__}"
            self._status = self._last_error
            raise BrowserStartError(self._last_error) from exc
        self._last_error = ""
        self._status = f"{self._provider.display_name} browser open"

    def poll(self) -> bool:
        """Return True while the browser process is alive, updating status."""
        if self._process is None:
            return False
        if _process_alive(self._process):
            return True
        self._finish_process()
        return False

    def _finish_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        code = _process_exitcode(process)
        if code == 0 or code is None:
            self._status = f"{self._provider.display_name} browser closed"
        elif code == 2:
            self._status = "Browser unavailable: pywebview failed to load"
        else:
            self._status = (
                f"{self._provider.display_name} browser ended (code {code})"
            )

    def stop(self, timeout: float = 3.0) -> None:
        process = self._process
        if process is None:
            self._status = "Browser closed"
            return
        try:
            if _process_alive(process):
                process.terminate()
                try:
                    process.join(timeout)
                except Exception:
                    pass
            if _process_alive(process):
                process.kill()
                try:
                    process.join(1.0)
                except Exception:
                    pass
        finally:
            self._process = None
            self._status = f"{self._provider.display_name} browser closed"

    def clear_session(self) -> None:
        """Delete only this provider's isolated profile. Never while open."""
        if self.is_running:
            raise BrowserHostError(
                "close the AI Web Chat browser before forgetting its session"
            )
        _verify_acceptance_profile_removal(self._profile_root)
        try:
            shutil.rmtree(self._profile_root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BrowserHostError(
                f"could not remove the isolated browser profile: {type(exc).__name__}"
            ) from exc
        self._status = "Browser session cleared"

    def shutdown(self) -> None:
        self.stop()
