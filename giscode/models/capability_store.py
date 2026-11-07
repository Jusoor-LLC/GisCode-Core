"""Persisted capability records, keyed by model content identity.

Only what a person or a test established is stored here - ``user_confirmed``
and ``verified`` (see :mod:`giscode.models.capability`). Everything weaker is
re-derived from the model file on every run, so the file stays small and can
never contradict the file it describes.

The store lives under the GisCode profile (``GISCODE_HOME``), never in a model
folder and never in a user's AI installation: connecting an existing install
must not write anything into it.

## What a record is keyed by, and what it is trusted on

A record is *found* by the header digest - size plus the first few megabytes -
because that is cheap enough to compute for every model at discovery. It is
*trusted* only after the whole file has been hashed again and matches the
``content`` digest in the record. Finding and trusting are deliberately
separate:

* the header digest alone is not proof of content (a re-quantization can keep
  the size and the first few megabytes), so a record found by it proves nothing;
* a full-file SHA-256 changes whenever any byte of the weights changes, so
  replaced, merged or re-quantized weights cannot inherit a verification;
* both digests are content-derived and path-free, so renaming or moving an
  unchanged model keeps its record.

A model with no record costs one header read (a few megabytes). A model *with*
a record costs one full-file hash per process, cached against the file's stamp
(size, mtime, change time, device, inode) and its header digest. Nothing is
written back to avoid that cost, because a persisted hash cannot prove that a
file still has the same bytes.

## Concurrency

Writers take an advisory lock on ``<store>.lock`` beside the store - ``fcntl``
on POSIX, ``msvcrt`` on Windows - with a bounded wait, so simultaneous GisCode
processes cannot lose each other's records. The operating system releases the
lock when a process exits, so a crashed writer leaves nothing to clean up.
Updates are written to a unique temporary file in the same directory, flushed,
and moved into place with ``os.replace``, so a reader always sees a whole store
and a failed write leaves the previous one untouched.

A reader takes the lock only long enough to see a consistent snapshot
(``READ_LOCK_TIMEOUT_SECONDS``) and then reads without it, because an atomic
update cannot be observed half-applied. Waiting the writer's timeout there
would let one busy writer stall discovery once per model.

A read that fails (a sharing violation, a permission error) means "no
information", never "no records": the empty view is neither cached nor used as
the basis of a write, so a transient failure cannot hide a record for the life
of the process or replace the store with only the record being added.

## Format

Version 3::

    {"version": 3,
     "records": {"<header digest>": {"content": "sha256:<full file>",
                                     "complete": true,
                                     "size": 12345,
                                     "capabilities": {"coding": {...}} }}}

Version 2 keyed records by the header digest and anchored them to a *sampled*
body fingerprint, and version 1 stored capabilities with no content anchor at
all. Neither can be turned into a full-file identity after the fact, so their
records are not trusted. The first write copies the old file to
``capabilities.json.v<version>.bak`` (once, never overwriting an earlier
backup) and then writes version 3; until then the old file is left exactly as
it is, which also makes an interrupted migration safe to retry.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from giscode.models.capability import (
    PERSISTED_LEVELS,
    ModelIdentity,
    content_identity_for,
    identity_for,
    level_strength,
)

logger = logging.getLogger(__name__)

STORE_FILENAME = "capabilities.json"
STORE_VERSION = 3

#: Suffix of the lock file, which sits beside the store in the profile.
LOCK_SUFFIX = ".lock"

#: How long a writer waits for another process before giving up. Bounded, so a
#: stuck holder is reported instead of hanging the GUI.
LOCK_TIMEOUT_SECONDS = 10.0

#: How long a *reader* waits before reading without the lock. It only needs the
#: lock to see a consistent snapshot and updates are atomic, so it must not wait
#: the writer's timeout: discovery consults the store once per model, and a busy
#: writer would otherwise stall it for ten seconds a model.
READ_LOCK_TIMEOUT_SECONDS = 0.5

_LOCK_POLL_SECONDS = 0.02
_COPY_CHUNK = 1024 * 1024


def _digest_of(identity: "ModelIdentity | str") -> str:
    return identity.digest if isinstance(identity, ModelIdentity) else str(identity or "")


def _retry(operation, *, attempts: int = 6, delay: float = 0.02):
    """Retry an operation that a concurrent rename can transiently break.

    On Windows, replacing a file that another process has open fails with a
    sharing violation, and opening a file at the instant it is replaced fails
    the same way - both surface as ``PermissionError``. Neither is permanent.
    A write that gave up there would silently lose a record, and a read that
    gave up would silently ignore every record, so a few short retries are the
    honest behaviour. Any other error propagates at once.
    """
    last: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            return operation()
        except PermissionError as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(delay)
    raise last if last is not None else PermissionError("retry failed")


class StoreLockTimeout(RuntimeError):
    """The store lock could not be taken before the timeout."""


class _ProcessLock:
    """A cross-process advisory lock on one file.

    POSIX uses ``fcntl.flock``, Windows ``msvcrt.locking``. Both are released by
    the operating system when the holding process exits, so a crashed writer
    leaves no stale lock behind - the next writer simply gets it. The lock file
    stays behind (removing it would race with a writer about to open it) and it
    stays in the GisCode profile, never beside a model.

    The wait is bounded: :meth:`acquire` returns ``False`` on timeout rather
    than blocking forever or writing without the lock.
    """

    def __init__(
        self,
        path: "str | os.PathLike[str]",
        *,
        shared: bool = False,
        timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.shared = bool(shared)
        self.timeout = max(0.0, float(timeout))
        self._handle = None
        self._held = False

    def _try_acquire(self) -> bool:
        handle = self._handle
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                mode = msvcrt.LK_NBRLCK if self.shared else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                fcntl.flock(handle.fileno(), flags | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
        except OSError as exc:
            logger.warning(
                "capability store lock could not be opened (%s)", type(exc).__name__
            )
            return False
        if os.name == "nt":
            # msvcrt locks a byte range, so the file needs a byte to lock.
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
            except OSError:
                handle.close()
                return False
        self._handle = handle
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_acquire():
                self._held = True
                return True
            if time.monotonic() >= deadline:
                handle.close()
                self._handle = None
                return False
            time.sleep(_LOCK_POLL_SECONDS)

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if self._held:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._held = False
            try:
                handle.close()
            except OSError:
                pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exception) -> bool:
        self.release()
        return False


class CapabilityStore:
    """A small JSON store of capability decisions for this profile."""

    def __init__(
        self,
        path: Optional["str | os.PathLike[str]"] = None,
        *,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
        read_lock_timeout: float = READ_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self.lock_timeout = float(lock_timeout)
        self.read_lock_timeout = float(read_lock_timeout)
        #: ``(file stamp, data, version on disk)``, so discovery does not re-read
        #: and re-parse the store once per model.
        self._cache: Optional[tuple] = None

    # ------------------------------------------------------------------ path
    @property
    def path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        from giscode.config.paths import giscode_home

        return giscode_home() / STORE_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + LOCK_SUFFIX)

    @contextmanager
    def _guarded(self, *, shared: bool):
        """Hold the cross-process lock and yield whether it was obtained.

        A read that cannot take the lock still proceeds: writes replace the file
        atomically, so a reader sees either the old store or the new one, never
        a half-written one. A write that cannot take the lock must not proceed,
        and the caller returns ``False``.
        """
        timeout = self.read_lock_timeout if shared else self.lock_timeout
        lock = _ProcessLock(self.lock_path, shared=shared, timeout=timeout)
        acquired = lock.acquire()
        try:
            if not acquired and shared:
                logger.warning(
                    "capability store lock was busy; reading without it (updates are atomic)"
                )
            yield acquired
        finally:
            lock.release()

    # ------------------------------------------------------------------ read
    @staticmethod
    def _file_stamp(path: Path) -> Optional[tuple]:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ino))

    def _load(self, *, use_cache: bool = True) -> tuple[dict, int, bool]:
        """``(normalised data, version on disk, read succeeded)``; never raises.

        Unreadable or malformed JSON is moved aside as a quarantine copy so the
        bytes are preserved, and the store is treated as empty. Records that are
        individually malformed or duplicated are dropped from the result while
        the valid ones are kept.

        The third element is the point: a file that could not be read at all
        (``False``) is *not* the same as a file holding no records. Callers must
        not cache that empty view and must not write from it, or a transient
        failure would ignore every record for the life of the process - or, on
        the write path, replace the whole store with one record.

        ``use_cache=False`` is used by writers: a read-modify-write must see what
        is on disk now, not what a previous lookup happened to cache.
        """
        path = self.path
        stamp = self._file_stamp(path)
        if use_cache and stamp is not None and self._cache is not None:
            if self._cache[0] == stamp:
                return dict(self._cache[1]), self._cache[2], True
        empty = {"version": STORE_VERSION, "records": {}}
        try:
            raw = _retry(lambda: path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty, STORE_VERSION, True
        except UnicodeDecodeError:
            logger.warning(
                "capability store is not valid UTF-8; it is preserved as a quarantine "
                "copy and ignored"
            )
            self._quarantine("corrupt")
            return empty, STORE_VERSION, True
        except OSError as exc:
            logger.warning(
                "capability store could not be read (%s); treating it as empty for this "
                "lookup without caching or rewriting it",
                type(exc).__name__,
            )
            return empty, STORE_VERSION, False
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning(
                "capability store is not valid JSON; it is preserved as a quarantine "
                "copy and ignored"
            )
            self._quarantine("corrupt")
            return empty, STORE_VERSION, True
        if not isinstance(data, dict):
            logger.warning("capability store is not a JSON object; quarantined and ignored")
            self._quarantine("corrupt")
            return empty, STORE_VERSION, True
        version = data.get("version")
        records = data.get("records")
        if not isinstance(version, int):
            logger.warning("capability store has no schema version; quarantined and ignored")
            self._quarantine("corrupt")
            return empty, STORE_VERSION, True
        if not isinstance(records, dict):
            records = {}
        if version != STORE_VERSION:
            # Older or newer formats are never read as if they were this one.
            normalised = {"version": STORE_VERSION, "records": {}}
            return normalised, version, True
        normalised = {"version": STORE_VERSION, "records": self._normalise(records)}
        return normalised, version, True

    def _normalise(self, records: dict) -> dict:
        """Keep the valid records, drop the rest, and refuse duplicate identities."""
        result: dict[str, dict] = {}
        seen_content: dict[str, str] = {}
        dropped = 0
        for digest, entry in records.items():
            cleaned = self._clean_entry(entry)
            if cleaned is None:
                dropped += 1
                continue
            content = cleaned["content"]
            if content in seen_content:
                # Two header keys claiming one content identity cannot both be
                # right; keeping the first avoids choosing by iteration order.
                dropped += 1
                continue
            seen_content[content] = str(digest)
            result[str(digest)] = cleaned
        if dropped:
            logger.warning(
                "%d capability record(s) were unreadable or duplicated and are ignored",
                dropped,
            )
        return result

    @staticmethod
    def _clean_entry(entry) -> Optional[dict]:
        """One record, or ``None`` when it cannot be trusted."""
        if not isinstance(entry, dict):
            return None
        content = str(entry.get("content") or "")
        if not content.startswith("sha256:"):
            return None
        if entry.get("complete") is not True:
            return None
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, dict):
            return None
        cleaned: dict[str, dict] = {}
        for name, record in capabilities.items():
            if not isinstance(record, dict):
                continue
            level = str(record.get("level") or "").strip().lower()
            if level not in PERSISTED_LEVELS:
                # A guess or a declaration is never read back as a decision.
                continue
            key = str(name or "").strip().lower()
            if not key:
                continue
            cleaned[key] = {
                "level": level,
                "source": str(record.get("source") or ""),
                "detail": str(record.get("detail") or ""),
            }
        if not cleaned:
            return None
        size = entry.get("size")
        return {
            "content": content,
            "complete": True,
            "size": int(size) if isinstance(size, int) else 0,
            "capabilities": cleaned,
        }

    def _records(self) -> dict:
        """A snapshot of the stored records (cached until the file changes)."""
        with self._lock:
            with self._guarded(shared=True):
                data, version, readable = self._load()
                if not readable:
                    # Do not remember a view produced by a failed read: doing so
                    # would hide every record until the file changed again.
                    return {}
                stamp = self._file_stamp(self.path)
                if stamp is not None:
                    self._cache = (stamp, data, version)
                return data.get("records", {})

    def _quarantine(self, kind: str) -> None:
        """Move an unusable store aside instead of overwriting it."""
        path = self.path
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.name}.{kind}-{stamp}")
        index = 1
        while target.exists():
            target = path.with_name(f"{path.name}.{kind}-{stamp}-{index}")
            index += 1
        try:
            path.replace(target)
            logger.warning("the unreadable capability store was kept as %s", target.name)
        except OSError as exc:
            logger.warning(
                "the unreadable capability store could not be preserved (%s)",
                type(exc).__name__,
            )

    def _backup_legacy(self, version: int) -> bool:
        """Copy a pre-v3 store once, before it is replaced. Never overwrites."""
        path = self.path
        target = path.with_name(f"{path.name}.v{version}.bak")
        if target.exists():
            return True
        try:
            with open(path, "rb") as source, open(target, "wb") as destination:
                for chunk in iter(lambda: source.read(_COPY_CHUNK), b""):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as exc:
            logger.warning(
                "the older capability store could not be backed up (%s); it is left "
                "untouched and nothing is written",
                type(exc).__name__,
            )
            return False
        logger.warning(
            "capability store version %s was anchored to a sampled body fingerprint "
            "that cannot be turned into a full-file identity; its bytes are kept in "
            "%s and its records must be established again",
            version,
            target.name,
        )
        return True

    # ----------------------------------------------------------------- write
    def _atomic_write(self, data: dict) -> bool:
        """Replace the store with *data* atomically, or leave it alone."""
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
        except OSError as exc:
            logger.warning(
                "capability store could not be written (%s); the previous file is unchanged",
                type(exc).__name__,
            )
            return False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _retry(lambda: os.replace(temporary, path))
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            logger.warning(
                "capability store could not be written (%s); the previous file is unchanged",
                type(exc).__name__,
            )
            return False
        self._sync_directory(path.parent)
        self._cache = None
        return True

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        """Best effort: make the rename itself durable on POSIX."""
        if os.name == "nt":
            return
        try:
            descriptor = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _capabilities(entry) -> dict[str, dict]:
        """The capability mapping inside one stored record (``{}`` if unusable)."""
        if not isinstance(entry, dict):
            return {}
        capabilities = entry.get("capabilities")
        return dict(capabilities) if isinstance(capabilities, dict) else {}

    def _entry(self, records: dict, identity: ModelIdentity):
        entry = records.get(identity.digest)
        return entry if isinstance(entry, dict) else None

    # ------------------------------------------------------------------- api
    def for_identity(self, identity: "ModelIdentity | str") -> dict[str, dict]:
        """Stored capabilities for one identity, *without* re-reading the file.

        Use this to inspect what was recorded. Use :meth:`for_path` before
        acting on a record, because only that establishes that the file still
        holds the bytes that were verified.
        """
        digest = _digest_of(identity)
        if not digest:
            return {}
        return self._capabilities(self._records().get(digest))

    def status_for_path(self, path: "str | os.PathLike[str]") -> dict:
        """What the store can honestly say about the file at *path*.

        ``state`` is one of ``unknown`` (file unreadable), ``unrecorded``,
        ``unverifiable`` (the file changed while it was being hashed),
        ``stale`` (different bytes from the recorded identity) or ``verified``
        (the whole file matches).
        """
        identity = identity_for(path)
        if not identity.known:
            return {
                "state": "unknown",
                "identity": "",
                "detail": "the model file cannot be read",
            }
        entry = self._entry(self._records(), identity)
        if entry is None:
            return {
                "state": "unrecorded",
                "identity": identity.digest,
                "detail": "no capability record for this model",
            }
        recorded = str(entry.get("content") or "")
        current = content_identity_for(path)
        if not current.trusted:
            return {
                "state": "unverifiable",
                "identity": identity.digest,
                "detail": "the file changed while it was being hashed, so it has no "
                "trusted content identity",
            }
        if current.content != recorded:
            return {
                "state": "stale",
                "identity": identity.digest,
                "detail": "the file's content no longer matches the recorded identity",
            }
        return {
            "state": "verified",
            "identity": identity.digest,
            "detail": "the whole file matches the recorded content identity",
        }

    def for_path(self, path: "str | os.PathLike[str]") -> dict[str, dict]:
        """Records for the file at *path*, validated against its current content.

        Returns ``{}`` when the file is unreadable, has no record, the record
        was written in an older format, or its whole-file digest no longer
        matches - in which case the file is not the one that was verified and
        its capability must be re-derived (and, if it still holds, re-recorded).
        """
        identity = identity_for(path)
        if not identity.known:
            return {}
        entry = self._entry(self._records(), identity)
        capabilities = self._capabilities(entry)
        if not capabilities:
            return {}
        recorded = str(entry.get("content") or "")
        if not recorded:
            logger.warning(
                "capability record for %s has no full-content identity; it cannot be "
                "re-validated and is ignored",
                Path(str(path)).name,
            )
            return {}
        current = content_identity_for(path)
        if not current.trusted or current.content != recorded:
            logger.warning(
                "the file at %s no longer matches its capability record; the recorded "
                "capabilities are dropped and must be established again",
                Path(str(path)).name,
            )
            return {}
        return capabilities

    def record(
        self,
        identity: "ModelIdentity | str",
        capability: str,
        level: str,
        *,
        source: str = "",
        detail: str = "",
    ) -> bool:
        """Store a capability decision for *identity*.

        *identity* must carry a **complete** content digest
        (:func:`giscode.models.capability.content_identity_for`). A header-only
        identity is refused: a record that is not anchored to the whole file
        could be inherited by different weights, which is exactly what this
        store exists to prevent.

        Returns ``False`` (and changes nothing) when the identity is not a
        trusted content identity, the level is not one a store may hold, the
        store is locked by another process, or the write fails.
        """
        digest = _digest_of(identity)
        content = identity.content if isinstance(identity, ModelIdentity) else ""
        complete = bool(getattr(identity, "complete", False))
        size = int(getattr(identity, "size_bytes", 0) or 0)
        name = str(capability or "").strip().lower()
        value = str(level or "").strip().lower()
        if not digest or not name or value not in PERSISTED_LEVELS:
            return False
        if not content or not complete:
            return False
        with self._lock:
            with self._guarded(shared=False) as acquired:
                if not acquired:
                    logger.warning(
                        "capability store is locked by another process; the record for "
                        "'%s' was not saved",
                        name,
                    )
                    return False
                data, version, readable = self._load(use_cache=False)
                if not readable:
                    # Reading nothing is not the same as there being nothing:
                    # writing from this view would drop every stored record.
                    logger.warning(
                        "capability store could not be read; it is left untouched and the "
                        "record for '%s' was not saved",
                        name,
                    )
                    return False
                if version > STORE_VERSION:
                    logger.warning(
                        "capability store was written by a newer GisCode (schema %s); "
                        "this build will not overwrite it, so the record was not saved",
                        version,
                    )
                    return False
                if version < STORE_VERSION and not self._backup_legacy(version):
                    return False
                records = data.setdefault("records", {})
                # One content identity may only be claimed once, whatever header
                # key it arrived under.
                for other, existing in list(records.items()):
                    if other != digest and isinstance(existing, dict):
                        if str(existing.get("content") or "") == content:
                            records.pop(other, None)
                entry = records.get(digest)
                if not isinstance(entry, dict):
                    entry = {}
                    records[digest] = entry
                capabilities = entry.get("capabilities")
                if not isinstance(capabilities, dict):
                    capabilities = {}
                    entry["capabilities"] = capabilities
                entry["content"] = content
                entry["complete"] = True
                entry["size"] = size
                existing = capabilities.get(name)
                if isinstance(existing, dict):
                    if level_strength(str(existing.get("level", ""))) > level_strength(value):
                        return True  # a stronger record is already there
                capabilities[name] = {
                    "level": value,
                    "source": str(source or "user"),
                    "detail": str(detail or ""),
                }
                return self._atomic_write(data)

    def record_for_path(
        self,
        path: "str | os.PathLike[str]",
        capability: str,
        level: str,
        *,
        source: str = "",
        detail: str = "",
    ) -> bool:
        """Store a decision for the file at *path* (the full content is hashed here)."""
        identity = content_identity_for(path)
        if not identity.trusted:
            logger.warning(
                "no trusted content identity for %s; the capability was not recorded",
                Path(str(path)).name,
            )
            return False
        return self.record(identity, capability, level, source=source, detail=detail)

    def forget(self, identity: "ModelIdentity | str") -> bool:
        digest = _digest_of(identity)
        if not digest:
            return False
        with self._lock:
            with self._guarded(shared=False) as acquired:
                if not acquired:
                    logger.warning("capability store is locked; the record was not removed")
                    return False
                data, version, readable = self._load(use_cache=False)
                if not readable:
                    logger.warning(
                        "capability store could not be read; nothing was removed"
                    )
                    return False
                if version > STORE_VERSION:
                    return False
                records = data.setdefault("records", {})
                if digest not in records:
                    return True
                records.pop(digest, None)
                return self._atomic_write(data)

    def entries(self) -> dict[str, dict]:
        """Every stored record, for inspection by a GUI or a test."""
        return dict(self._records())
