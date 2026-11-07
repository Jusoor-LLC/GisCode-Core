"""Streaming reads must survive an idle gap between chunks.

CPython poisons a socket after its first read timeout ("cannot read from
timed out object"), so pacing ``read1`` with a socket timeout aborts a healthy
generation whenever the server is slow to send the next chunk (e.g. during
prompt processing). The runtime must instead read on a background thread with
blocking reads and surface idle gaps as heartbeats.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    SubprocessLlamaCppRuntime,
)


class _StallingSSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    stall = 0.8

    def log_message(self, *args):  # noqa: ANN002 - silence the test server
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        # The role delta arrives before the stall: the body is live, headers
        # are already sent, and the next chunk is delayed past read_timeout.
        send({"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]})
        time.sleep(self.stall)
        send({"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]})
        send({"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]})
        send({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _run_stalling_server(stall: float):
    handler = type("_H", (_StallingSSEHandler,), {"stall": stall})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_idle_gap_yields_heartbeat_and_does_not_abort():
    server, port = _run_stalling_server(stall=0.8)
    try:
        api = SubprocessLlamaCppRuntime(server_path=None)
        api._ensure_model_ready = lambda: None  # noqa: SLF001 - bypass real load
        request = ChatRequest(
            messages=[ChatMessage(role="user", content="hi")], max_tokens=16
        )
        events = []
        for piece, reason, tokens in api.chat_generate_stream(
            request, port=port, read_timeout=0.25
        ):
            events.append((piece, reason, tokens))

        text = "".join(p for p, _r, _t in events if p)
        heartbeats = [e for e in events if e == ("", None, None)]
        finals = [e for e in events if e[1] == "stop"]

        assert text == "Hello"
        assert heartbeats, "the idle gap must surface as a heartbeat, not an error"
        assert finals and finals[-1][2] == 2
    finally:
        server.shutdown()
        server.server_close()
