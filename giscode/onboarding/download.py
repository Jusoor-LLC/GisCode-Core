"""Driving ``ollama pull`` - with consent, progress and honest endings.

Ollama owns its blob store and its resumable downloader. GisCode therefore does
not implement a transfer of its own: it runs Ollama's own pull and reports what
Ollama reports. That keeps a resumed download genuinely resumed, and it keeps
the transfer where the user's models already live.

What this module adds on top is the part Ollama's CLI does not give a GUI:

* **A pull only starts after an explicit confirmation** recorded by the caller.
  ``start(..., confirm=False)`` raises rather than downloading.
* **Progress is parsed from Ollama's own output**, so the percentage shown is
  Ollama's percentage.
* **Duplicate protection**: a second start for the same model returns the
  running job instead of spawning a second process, and a model that is already
  installed completes immediately without any transfer.
* **Honest endings**: success requires a zero exit *or* Ollama's success line; a
  cancel, a failure and an interruption are distinct states, and a retry is a
  new attempt that keeps the previous one in the attempt counter.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from giscode.providers.base import DEFAULT_ENDPOINTS, OLLAMA, validate_endpoint

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
_RATIO_RE = re.compile(
    r"([\d.]+)\s*([KMG]i?B)\s*/\s*([\d.]+)\s*([KMG]i?B)", re.IGNORECASE
)
_SUCCESS_RE = re.compile(r"\bsuccess\b", re.IGNORECASE)
_UNIT = {"B": 1, "KB": 1000, "KIB": 1024, "MB": 1000 ** 2, "MIB": 1024 ** 2,
         "GB": 1000 ** 3, "GIB": 1024 ** 3}


class PullError(RuntimeError):
    """A pull could not be started or did not end usably."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class PullJob:
    model: str
    status: str = PENDING
    percent: int = 0
    detail: str = ""
    started_at: str = ""
    finished_at: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    exit_code: Optional[int] = None
    attempts: int = 1
    history: list = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status in (DONE, FAILED, CANCELLED, INTERRUPTED)

    def as_dict(self) -> dict:
        """The part that is safe to persist and to show - no process handle."""
        return {
            "model": self.model,
            "status": self.status,
            "percent": int(self.percent),
            "detail": self.detail[:300],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "bytes_done": int(self.bytes_done),
            "bytes_total": int(self.bytes_total),
            "exit_code": self.exit_code,
            "attempts": int(self.attempts),
            "history": list(self.history[-10:]),
        }

    def _note(self, message: str) -> None:
        self.history.append({"at": _now(), "detail": message[:200]})
        del self.history[:-10]


def parse_progress(line: str) -> dict:
    """Extract what Ollama's progress line says, without inventing the rest."""
    text = (line or "").strip()
    result: dict = {"detail": text[:200]}
    percent = None
    percent_match = _PERCENT_RE.search(text)
    if percent_match:
        percent = max(0, min(100, int(percent_match.group(1))))
    ratio = _RATIO_RE.search(text)
    if ratio:
        try:
            done = float(ratio.group(1)) * _UNIT.get(ratio.group(2).upper(), 1)
            total = float(ratio.group(3)) * _UNIT.get(ratio.group(4).upper(), 1)
            result["bytes_done"] = int(done)
            result["bytes_total"] = int(total)
            if percent is None and total > 0:
                percent = max(0, min(100, int(done * 100 / total)))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if percent is not None:
        result["percent"] = percent
    return result


class OllamaPuller:
    """One pull at a time per model, driven by a cancellable thread."""

    def __init__(
        self,
        *,
        executable: str = "",
        runner: Optional[Callable[[str, Callable[[str], None]], int]] = None,
        on_update: Optional[Callable[[PullJob], None]] = None,
        transport: str = "cli",
        endpoint: str = "",
    ) -> None:
        self._executable = executable or shutil.which("ollama") or "ollama"
        self._runner = runner
        self._on_update = on_update
        self._transport = (transport or "cli").strip().lower()
        if self._transport not in ("cli", "api"):
            raise ValueError("Ollama pull transport must be 'cli' or 'api'.")
        self._endpoint = validate_endpoint(endpoint or DEFAULT_ENDPOINTS[OLLAMA])
        self._lock = threading.RLock()
        self._jobs: dict[str, PullJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._responses: dict[str, object] = {}
        self._cancelled: set[str] = set()

    # -- inspection ---------------------------------------------------------
    def job(self, model: str) -> Optional[PullJob]:
        with self._lock:
            return self._jobs.get(model)

    def jobs(self) -> list[PullJob]:
        with self._lock:
            return list(self._jobs.values())

    def as_dict(self, model: str) -> dict:
        job = self.job(model)
        return job.as_dict() if job is not None else {}

    # -- lifecycle ----------------------------------------------------------
    def start(self, model: str, *, confirm: bool, installed=()) -> PullJob:
        """Begin a pull. Without ``confirm=True`` this refuses to transfer."""
        name = (model or "").strip()
        if not name:
            raise PullError("A model tag is required, for example qwen2.5-coder:7b.")
        if not confirm:
            raise PermissionError(
                f"Downloading {name} needs explicit confirmation: it transfers "
                "several gigabytes and uses your connection."
            )

        with self._lock:
            installed_set = {str(item) for item in installed or ()}
            if name in installed_set:
                job = PullJob(
                    model=name,
                    status=DONE,
                    percent=100,
                    detail="Already installed; nothing was downloaded.",
                    started_at=_now(),
                    finished_at=_now(),
                )
                job._note("already installed")
                self._jobs[name] = job
                return job

            existing = self._jobs.get(name)
            if existing is not None and not existing.terminal:
                # Deduplicate: a second click must not spawn a second pull.
                return existing
            if existing is not None and existing.status == RUNNING:
                return existing

            attempts = (existing.attempts + 1) if existing is not None else 1
            job = PullJob(
                model=name,
                status=PENDING,
                started_at=_now(),
                attempts=attempts,
                detail="Starting the Ollama model download...",
            )
            self._jobs[name] = job
            self._cancelled.discard(name)
            thread = threading.Thread(
                target=self._run,
                args=(job,),
                name=f"giscode-ollama-pull-{name}",
                daemon=True,
            )
            self._threads[name] = thread
            thread.start()
            return job

    def wait(self, model: str, timeout: float = 0.0) -> Optional[PullJob]:
        thread = self._threads.get(model)
        if thread is not None:
            thread.join(timeout if timeout > 0 else None)
        return self.job(model)

    def cancel(self, model: str) -> Optional[PullJob]:
        with self._lock:
            self._cancelled.add(model)
            job = self._jobs.get(model)
            process = self._processes.get(model)
            response = self._responses.get(model)
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if job is not None and not job.terminal:
            with self._lock:
                job.status = CANCELLED
                job.detail = "Cancelled by the user. Nothing else is downloaded."
                job.finished_at = _now()
                job._note("cancelled")
            self._notify(job)
        return job

    def retry(self, model: str, *, confirm: bool) -> PullJob:
        return self.start(model, confirm=confirm)

    # -- worker -------------------------------------------------------------
    def _notify(self, job: PullJob) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(job)
        except Exception:
            # A listener must never break a transfer.
            pass

    def _emit(self, job: PullJob, line: str) -> None:
        parsed = parse_progress(line)
        with self._lock:
            if job.status in (CANCELLED, INTERRUPTED):
                return
            job.status = RUNNING
            if "percent" in parsed and parsed["percent"] >= job.percent:
                job.percent = int(parsed["percent"])
            if "bytes_done" in parsed:
                job.bytes_done = max(job.bytes_done, int(parsed["bytes_done"]))
            if "bytes_total" in parsed:
                job.bytes_total = max(job.bytes_total, int(parsed["bytes_total"]))
            if parsed.get("detail"):
                job.detail = str(parsed["detail"])[:300]
        self._notify(job)

    def _run(self, job: PullJob) -> None:
        try:
            with self._lock:
                if job.status == CANCELLED:
                    return
                job.status = RUNNING
            self._notify(job)
            code = int(self._runner(job.model, lambda line: self._emit(job, line)) if self._runner
                       else self._run_default(job))
        except Exception as exc:
            with self._lock:
                if job.status == CANCELLED or job.model in self._cancelled:
                    job.status = CANCELLED
                    job.detail = "Cancelled by the user."
                else:
                    job.status = FAILED
                    job.detail = f"{type(exc).__name__}: {exc}"
                    job._note(job.detail)
                job.finished_at = _now()
            self._notify(job)
            return

        with self._lock:
            job.exit_code = code
            job.finished_at = _now()
            if job.status == CANCELLED or job.model in self._cancelled:
                job.status = CANCELLED
                job.detail = "Cancelled by the user."
            elif code == 0 or _SUCCESS_RE.search(job.detail or ""):
                job.status = DONE
                job.percent = 100
                job.detail = job.detail or "ollama pull reported success."
                job._note("done")
            else:
                job.status = FAILED
                job.detail = (
                    f"'ollama pull' exited with code {code}. "
                    + (job.detail or "Check the Ollama log for the reason.")
                )
                job._note(job.detail)
        self._notify(job)

    def _run_default(self, job: PullJob) -> int:
        """Use Ollama's API when asked, otherwise its CLI with the same rules."""
        if self._transport == "api":
            return self._run_api(job)
        return self._run_cli(job)

    def _run_cli(self, job: PullJob) -> int:
        """Stream ``ollama pull <model>`` and keep the process cancellable."""
        command = [self._executable, "pull", job.model]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._lock:
            self._processes[job.model] = process
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if job.model in self._cancelled:
                    break
                self._emit(job, line)
            return process.wait(timeout=30)
        finally:
            with self._lock:
                self._processes.pop(job.model, None)

    def _run_api(self, job: PullJob) -> int:
        """Stream Ollama's documented ``POST /api/pull`` NDJSON response.

        The request is the pull mechanism Ollama itself exposes, so resumable
        blob handling stays inside Ollama. Each line is one status object; the
        connection is held in ``_responses`` so Cancel can close it and stop the
        transfer instead of merely ignoring the rest of the stream.
        """
        url = self._endpoint.rstrip("/") + "/api/pull"
        payload = json.dumps({"model": job.model, "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=30.0)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            raise PullError(
                f"Ollama refused the download (HTTP {exc.code})"
                + (f": {detail}" if detail.strip() else "")
            ) from exc
        except urllib.error.URLError as exc:
            raise PullError(
                "Ollama is not answering, so the download could not start: "
                f"{getattr(exc, 'reason', exc)}"
            ) from exc

        with self._lock:
            self._responses[job.model] = response
        try:
            for raw in response:
                if job.model in self._cancelled:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise PullError(
                        "Ollama sent download progress that was not valid JSON."
                    ) from exc
                if not isinstance(event, dict):
                    raise PullError("Ollama sent an unexpected download status.")
                self._emit_event(job, event)
            return 0
        finally:
            with self._lock:
                self._responses.pop(job.model, None)
            try:
                response.close()
            except Exception:
                pass

    def _emit_event(self, job: PullJob, event: dict) -> None:
        """Translate one Ollama pull object into the same job fields as CLI."""
        error = event.get("error")
        if error:
            raise PullError(f"Ollama reported a download error: {error}")
        status = str(event.get("status") or "downloading")
        completed = event.get("completed")
        total = event.get("total")
        with self._lock:
            if total is not None:
                try:
                    job.bytes_total = max(job.bytes_total, int(total))
                except (TypeError, ValueError):
                    pass
            if completed is not None:
                try:
                    job.bytes_done = max(job.bytes_done, int(completed))
                except (TypeError, ValueError):
                    pass
            if job.bytes_total > 0:
                job.percent = max(0, min(100, int(job.bytes_done * 100 / job.bytes_total)))
        if status.lower() == "success":
            if job.bytes_total > 0:
                with self._lock:
                    job.bytes_done = job.bytes_total
                    job.percent = 100
            self._emit(job, "success")
        else:
            detail = status
            if job.bytes_total > 0:
                detail = (
                    f"{status}  {job.bytes_done / (1024 ** 3):.2f} GB/"
                    f"{job.bytes_total / (1024 ** 3):.2f} GB"
                )
            self._emit(job, detail)


def available_executable() -> str:
    return shutil.which("ollama") or ""


def model_installed(model: str, models) -> bool:
    """Ollama reports ``name:tag``; tolerate a bare name for the default tag."""
    target = (model or "").strip().lower()
    if not target:
        return False
    names = {str(item).strip().lower() for item in models or ()}
    return target in names or (":" not in target and f"{target}:latest" in names)
