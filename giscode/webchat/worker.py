"""The owned WebView2 child process used by provider-neutral AI Web Chat."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def run_webview2_process(
    url: str,
    profile_path: str,
    window_title: str = "GisCode — AI Web Chat",
) -> None:
    """Run one visible WebView2 window until the user closes it.

    The profile is an isolated provider-owned directory. No cookie, password,
    token or browser history is read or returned by this function.
    """
    try:
        import webview  # type: ignore[import-not-found]
    except Exception:
        os._exit(2)

    try:
        Path(profile_path).mkdir(parents=True, exist_ok=True)
        webview.settings["ALLOW_DOWNLOADS"] = False
        webview.settings["ALLOW_FILE_URLS"] = False
        webview.settings["REMOTE_DEBUGGING_PORT"] = None
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        webview.create_window(
            window_title,
            url=url,
            width=1180,
            height=820,
            min_size=(760, 560),
            text_select=True,
            confirm_close=False,
        )
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(Path(profile_path).resolve()),
        )
    except BaseException:
        # The parent observes a non-zero exit and reports it. Do not print a
        # browser profile, URL, or session data from the child process.
        os._exit(1)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3):
        return 2
    run_webview2_process(*args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
