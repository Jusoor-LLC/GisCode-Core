"""Backward-compatible ChatGPT WebView2 child process entry point."""
from __future__ import annotations

import sys

from giscode.webchat.providers import CHATGPT_PROVIDER
from giscode.webchat.worker import run_webview2_process as _run_webview2_process


def run_webview2_process(url: str, profile_path: str) -> None:
    """Run one visible ChatGPT WebView2 window until the user closes it."""
    _run_webview2_process(url, profile_path, CHATGPT_PROVIDER.window_title)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2
    run_webview2_process(args[0], args[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
