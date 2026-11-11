"""Incremental provider responses: one transport, two wire formats.

Providers deliver chat answers in pieces, and they do it in two different ways:

* **Ollama** streams newline-delimited JSON objects on ``POST /api/chat``;
* **LM Studio** and every OpenAI-compatible server stream Server-Sent Events
  (``data: {...}`` lines) on ``POST /v1/chat/completions``.

This module owns only the parts that are identical for both: opening the
request, reading it without blocking the caller, assembling partial chunks into
whole protocol messages, and closing the socket when the answer is abandoned.
What a chunk *means* stays in each provider module, because the two protocols
do not agree on field names and pretending they do is how text gets lost.

Three behaviours are deliberate:

* **Reads happen on a reader thread with a blocking socket.** Pacing a
  ``urllib`` response with a socket timeout is unsafe on CPython: the first
  timeout poisons the socket permanently ("cannot read from timed out
  object"), so an idle gap while the model thinks would abort a healthy
  generation. The queue turns idle time into a heartbeat for the caller.
* **Cancellation closes the response.** Setting a flag that only stops
  *reading* would leave the connection open and the server generating. Closing
  the response tears the socket down, so the local side genuinely stops.
* **A stream that ends early raises.** A closed connection, a malformed body
  and a provider error are never reported as an empty successful answer.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from giscode.providers.base import ProviderError

#: The terminator OpenAI-shaped servers send after the last chunk.
SSE_DONE = "[DONE]"


class _DoneEvent:
    """Marker yielded by :func:`iter_sse_objects` when ``[DONE]`` arrives.

    A distinct object (rather than a string) keeps it impossible to confuse
    with a chunk that happens to contain that text.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<SSE_DONE>"


#: The single sentinel instance; compare with ``is``.
SSE_DONE_EVENT = _DoneEvent()

#: How often a watchdog re-checks the cancel signal.
CANCEL_POLL = 0.05

#: Read size. Big enough to amortise syscalls, small enough to stay responsive.
_READ_SIZE = 65536


class StreamError(ProviderError):
    """A stream broke in a way the caller must not mistake for success."""


class StreamTimeout(StreamError):
    """A request or idle deadline expired before the answer was complete."""

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        limit: Optional[float] = None,
        kind: str = "idle",
    ) -> None:
        super().__init__(message, action=action)
        self.limit = limit
        self.kind = kind


class StreamCancelled(Exception):
    """The caller's cancel signal fired and the transport was closed."""


@dataclass(frozen=True)
class StreamDelta:
    """One decoded piece of a streaming answer.

    ``text`` carries a content delta; the final event sets ``done`` with the
    provider's ``finish_reason`` and token count. ``usage`` keeps whatever
    metadata the provider actually reported - never an invented number.
    """

    text: str = ""
    finish_reason: str = ""
    tokens: int = 0
    usage: dict = field(default_factory=dict)
    done: bool = False


def _set_blocking(response):
    """Put the response's socket into blocking mode (see module docstring)."""
    fp = getattr(response, "fp", None)
    sock = None
    for candidate in (getattr(fp, "raw", None), fp):
        found = getattr(candidate, "_sock", None)
        if found is not None:
            sock = found
            break
    if sock is not None:
        try:
            sock.settimeout(None)
        except OSError:
            pass
    return sock


def _is_timeout(exc: BaseException) -> bool:
    """True for socket/read timeouts in the shapes CPython and Windows use."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return True
    text = str(reason).lower()
    return "timed out" in text or "timeout" in text


class _ByteStream:
    """Iterate the response body as it arrives, honouring cancel and idle time."""

    def __init__(
        self,
        events: "queue.Queue",
        sentinel: object,
        *,
        idle_timeout: Optional[float],
        state: dict,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._events = events
        self._sentinel = sentinel
        self._idle_timeout = idle_timeout
        self._state = state
        self._stop_requested = stop_requested or (lambda: False)

    def __iter__(self) -> Iterator[bytes]:
        last = time.monotonic()
        while True:
            if self._state["cancelled"] or self._stop_requested():
                self._state["cancelled"] = True
                raise StreamCancelled()
            try:
                item = self._events.get(timeout=0.1)
            except queue.Empty:
                if (
                    self._idle_timeout
                    and time.monotonic() - last > float(self._idle_timeout)
                ):
                    raise StreamTimeout(
                        f"the provider sent nothing for {float(self._idle_timeout):g}s "
                        "while streaming.",
                        action="Try again; if it repeats, check the provider's log.",
                        limit=float(self._idle_timeout),
                        kind="idle",
                    )
                continue
            last = time.monotonic()
            if self._state["cancelled"] or self._stop_requested():
                self._state["cancelled"] = True
                raise StreamCancelled()
            if item is self._sentinel:
                return
            if isinstance(item, BaseException):
                if self._state["cancelled"]:
                    raise StreamCancelled()
                if _is_timeout(item):
                    if (
                        self._idle_timeout
                        and time.monotonic() - last > float(self._idle_timeout)
                    ):
                        raise StreamTimeout(
                            "the provider stopped sending while streaming.",
                            action="Check that the provider is still running.",
                            limit=float(self._idle_timeout),
                            kind="idle",
                        )
                    continue
                raise StreamError(
                    f"the stream broke: {type(item).__name__}: {item}",
                    action="Check that the provider is still running, then try again.",
                )
            yield item


@contextmanager
def stream_response(
    url: str,
    *,
    payload: dict,
    headers: Optional[dict],
    timeout: float,
    idle_timeout: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
    should_stop: Optional[Callable[[], bool]] = None,
):
    """POST ``payload`` and yield an iterator of raw body chunks.

    The caller consumes whole protocol messages from the yielded iterator.
    Leaving the ``with`` block - by completion, exception or cancellation -
    closes the response, so no socket or reader thread outlives the request.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    state = {"cancelled": False, "closed": False}

    def _stop_requested() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        if should_stop is not None:
            try:
                return bool(should_stop())
            except Exception:  # noqa: BLE001 - a broken predicate is not a cancel
                return False
        return False

    def _open_error(exc: BaseException) -> BaseException:
        if isinstance(exc, urllib.error.HTTPError):
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300].strip()
            except Exception:  # noqa: BLE001 - the body is optional
                detail = ""
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            suffix = f": {detail}" if detail else ""
            return StreamError(
                f"the provider refused the streaming request (HTTP {exc.code}){suffix}",
                action=(
                    "The server may not support 'stream'; check its log, then test "
                    "the connection again."
                ),
            )
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return StreamTimeout(
                "the provider did not answer within the request timeout.",
                action="It may still be loading a model; try again in a moment.",
                limit=float(timeout),
                kind="request",
            )
        if isinstance(exc, urllib.error.URLError):
            return StreamError(
                f"the streaming endpoint could not be reached: "
                f"{getattr(exc, 'reason', exc)}",
                action="Start the provider, or correct the endpoint, then try again.",
            )
        if isinstance(exc, ProviderError):
            return exc
        return StreamError(
            f"the streaming request failed: {type(exc).__name__}: {exc}",
            action="Check that the provider is running, then try again.",
        )

    def _open():
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller
            return _open_error(exc)

    if cancel_event is None and should_stop is None:
        opened = _open()
    else:
        # ``urlopen`` cannot be interrupted while it waits for headers. Run
        # the connect on a daemon thread and stop waiting when the caller
        # cancels; a late response is closed by that thread.
        open_events: "queue.Queue" = queue.Queue(maxsize=1)

        def _connect() -> None:
            result = _open()
            if isinstance(result, BaseException):
                try:
                    open_events.put_nowait(result)
                except queue.Full:
                    pass
                return
            if state["cancelled"] or state["closed"]:
                try:
                    result.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                open_events.put_nowait(result)
            except queue.Full:
                try:
                    result.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(
            target=_connect, name="giscode-provider-connect", daemon=True
        ).start()
        opened = None
        while opened is None:
            if _stop_requested():
                state["cancelled"] = True
                # The connect thread may have put the response just before
                # this check observed the cancel; close it instead of leaking
                # a connection nobody will consume.
                try:
                    late = open_events.get_nowait()
                except queue.Empty:
                    late = None
                if late is not None and not isinstance(late, BaseException):
                    try:
                        late.close()
                    except Exception:  # noqa: BLE001
                        pass
                raise StreamCancelled()
            try:
                opened = open_events.get(timeout=CANCEL_POLL)
            except queue.Empty:
                continue

    if isinstance(opened, BaseException):
        raise opened
    response = opened

    sock = _set_blocking(response)
    events: "queue.Queue" = queue.Queue(maxsize=32)
    sentinel = object()

    def _close_response() -> None:
        # A read blocked in another thread is not reliably released by
        # ``HTTPResponse.close`` alone on Windows; shutting the socket down
        # wakes it first, then the response releases its own buffers.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass

    def _put(item) -> bool:
        """Bounded delivery: apply backpressure instead of growing forever."""
        while not state["closed"]:
            try:
                events.put(item, timeout=0.1)
                return True
            except queue.Full:
                if state["cancelled"] or state["closed"]:
                    return False
        return False

    def _reader() -> None:
        try:
            while not state["cancelled"] and not state["closed"]:
                raw = response.read1(_READ_SIZE)
                if not raw:
                    break
                if not _put(raw):
                    break
        except Exception as exc:  # noqa: BLE001 - relayed to the caller
            if not state["cancelled"] and not state["closed"]:
                _put(exc)
        finally:
            _put(sentinel)

    def _watchdog() -> None:
        # Closing the response is what physically stops the transfer; the
        # reader thread then sees EOF/error and the generator reports cancel.
        while not state["closed"]:
            if _stop_requested():
                state["cancelled"] = True
                _close_response()
                return
            time.sleep(CANCEL_POLL)

    reader = threading.Thread(target=_reader, name="giscode-provider-stream", daemon=True)
    reader.start()
    watchdog = None
    if cancel_event is not None or should_stop is not None:
        watchdog = threading.Thread(
            target=_watchdog, name="giscode-provider-stream-cancel", daemon=True
        )
        watchdog.start()
    try:
        yield _ByteStream(
            events,
            sentinel,
            idle_timeout=idle_timeout,
            state=state,
            stop_requested=_stop_requested,
        )
    finally:
        state["closed"] = True
        _close_response()
        # ``close`` normally unblocks ``read1``; the timeout keeps a
        # pathological transport from holding the context manager forever.
        reader.join(timeout=0.5)
        if watchdog is not None:
            watchdog.join(timeout=0.5)


class _LineBuffer:
    """Split arbitrary byte chunks into complete lines, keeping the remainder."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        return lines

    def flush(self) -> list[bytes]:
        rest, self._buffer = self._buffer, b""
        return [rest] if rest.strip() else []


def _decode(raw: bytes) -> str:
    return raw.rstrip(b"\r").decode("utf-8", "replace")


def iter_sse_objects(chunks) -> Iterator:
    """Yield JSON objects from an SSE body, plus :data:`SSE_DONE_EVENT`.

    Handles the shapes that actually occur: several events inside one network
    chunk, one event split across chunks, CRLF endings, comment lines,
    ``data:`` with or without a space, and servers that omit the blank line
    between events.
    """
    buffer = _LineBuffer()
    data_lines: list[str] = []
    parsed_count = 0
    malformed_count = 0

    def _parse(joined: str):
        nonlocal parsed_count, malformed_count
        text = joined.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            malformed_count += 1
            return None
        parsed_count += 1
        return obj

    def _flush():
        if not data_lines:
            return None
        joined = "\n".join(data_lines)
        data_lines.clear()
        return _parse(joined)

    for chunk in chunks:
        for raw in buffer.feed(chunk):
            line = _decode(raw)
            if not line.strip():
                obj = _flush()
                if obj is not None:
                    yield obj
                continue
            if line.lstrip().startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            if field_name.strip().lower() != "data":
                continue
            value = value[1:] if value.startswith(" ") else value
            if value.strip() == SSE_DONE:
                # An unterminated event before [DONE] still carries content.
                obj = _flush()
                if obj is not None:
                    yield obj
                if parsed_count == 0 and malformed_count:
                    raise StreamError(
                        "the provider streamed data that was not valid JSON.",
                        action=(
                            "Check that the endpoint is the provider's API, not a "
                            "web page."
                        ),
                    )
                yield SSE_DONE_EVENT
                return
            if data_lines:
                # Some servers send one data line per event without a blank
                # separator. Flush the pending event only when both sides are
                # complete JSON, so a genuinely multi-line event is preserved.
                pending = "\n".join(data_lines)
                try:
                    previous = json.loads(pending)
                except ValueError:
                    previous = None
                try:
                    current = json.loads(value)
                except ValueError:
                    current = None
                if previous is not None and current is not None:
                    data_lines.clear()
                    parsed_count += 1
                    yield previous
            data_lines.append(value)

    for raw in buffer.flush():
        line = _decode(raw)
        if line.lstrip().startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    obj = _flush()
    if obj is not None:
        yield obj

    if parsed_count == 0 and malformed_count:
        raise StreamError(
            "the provider streamed data that was not valid JSON.",
            action="Check that the endpoint is the provider's API, not a web page.",
        )


def iter_ndjson_objects(chunks) -> Iterator[dict]:
    """Yield one JSON object per line (Ollama's streaming format)."""
    buffer = _LineBuffer()
    parsed_count = 0
    malformed_count = 0

    def _parse(line: str):
        nonlocal parsed_count, malformed_count
        text = line.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            malformed_count += 1
            return None
        parsed_count += 1
        return obj if isinstance(obj, dict) else None

    for chunk in chunks:
        for raw in buffer.feed(chunk):
            obj = _parse(_decode(raw))
            if obj is not None:
                yield obj
    for raw in buffer.flush():
        obj = _parse(_decode(raw))
        if obj is not None:
            yield obj

    if parsed_count == 0 and malformed_count:
        raise StreamError(
            "the provider streamed data that was not valid JSON.",
            action="Check that the endpoint is the provider's API, not a web page.",
        )
