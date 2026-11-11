"""Credential lookup and optional at-rest protection.

API keys must never be written into ``config.json`` or a log line.  The
provider layer therefore accepts a credential store and asks it for the secret
at call time.  Three implementations are provided:

* :class:`EnvironmentCredentialStore` - read-only, uses named environment
  variables (the default for headless installs);
* :class:`MemoryCredentialStore` - process-local, used by tests;
* :class:`WindowsDpapiStore` - writes a value encrypted with Windows DPAPI,
  scoped to the current Windows user, under ``GISCODE_HOME``.

The Windows store is deliberately optional.  On a non-Windows host or a Python
runtime without ``crypt32``, it reports unavailable instead of silently storing
plaintext.  Credential values are never returned in ``repr`` or status text.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import threading
from pathlib import Path
from typing import Optional

from giscode.config.paths import giscode_home


class CredentialStoreError(RuntimeError):
    """A credential could not be read or written securely."""


class CredentialStore:
    """Minimal secret-store interface used by providers."""

    available = True
    read_only = True

    def get(self, name: str, default: str = "") -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:  # pragma: no cover - interface
        raise CredentialStoreError("This credential store is read-only.")

    def delete(self, name: str) -> None:  # pragma: no cover - interface
        raise CredentialStoreError("This credential store is read-only.")

    def has(self, name: str) -> bool:
        return bool(self.get(name))


class EnvironmentCredentialStore(CredentialStore):
    """Read secrets from environment variables only.

    ``name`` is either an environment variable name (``OPENAI_API_KEY``) or a
    lower-case provider name mapped through ``names``.  No value is copied into
    settings or a file.
    """

    available = True
    read_only = True

    def __init__(self, names: Optional[dict[str, str]] = None) -> None:
        self._names = {str(k).lower(): str(v) for k, v in (names or {}).items()}

    def _env_name(self, name: str) -> str:
        return self._names.get(str(name).lower(), str(name))

    def get(self, name: str, default: str = "") -> str:
        value = os.environ.get(self._env_name(name), "")
        return str(value) if value else default


class MemoryCredentialStore(CredentialStore):
    """In-process secret store for tests and injected test doubles."""

    def __init__(self, values: Optional[dict[str, str]] = None) -> None:
        self._values = {str(k): str(v) for k, v in (values or {}).items()}
        self._lock = threading.Lock()

    def get(self, name: str, default: str = "") -> str:
        with self._lock:
            return self._values.get(str(name), default)

    def set(self, name: str, value: str) -> None:
        with self._lock:
            self._values[str(name)] = str(value)

    def delete(self, name: str) -> None:
        with self._lock:
            self._values.pop(str(name), None)


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        ctypes.WinDLL("crypt32")  # type: ignore[attr-defined]
        ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        return True
    except (AttributeError, OSError):
        return False


#: The two system libraries the DPAPI calls need.  Loading them once and
#: keeping the prototyped functions matters for more than speed: every
#: ``WinDLL`` call takes the Windows loader lock, so loading on each credential
#: read serialises token access behind any other thread in the process that is
#: loading a DLL (a model backend, for example) and can stall a refresh for many
#: seconds.  One import-time load removes that coupling.
_WIN32_LOCK = threading.Lock()
_WIN32_API: Optional[tuple[object, object, object, object]] = None


def _win32_api() -> tuple[object, object, object, object]:
    """Return prototyped ``CryptProtectData``/``CryptUnprotectData``/LocalFree."""

    global _WIN32_API
    with _WIN32_LOCK:
        if _WIN32_API is not None:
            return _WIN32_API
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        _WIN32_API = (
            crypt32.CryptProtectData,
            crypt32.CryptUnprotectData,
            kernel32.LocalFree,
            ctypes.get_last_error,
        )
        return _WIN32_API


class WindowsDpapiStore(CredentialStore):
    """Encrypt one secret per file with the current Windows user's DPAPI key.

    DPAPI binds the ciphertext to the Windows account.  A copied file is not
    useful to another account and is not readable as plaintext.  This protects
    the credential at rest; it does not protect it from the current user's own
    process, which is exactly the trust boundary of a desktop application.
    """

    available = _dpapi_available()
    read_only = False

    def __init__(self, root: Optional[Path | str] = None) -> None:
        self._root = Path(root) if root is not None else giscode_home() / "credentials"
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_") or "secret"
        return self._root / f"{safe}.bin"

    @staticmethod
    def _protect(data: bytes) -> bytes:
        protect, _unprotect, local_free, last_error = _win32_api()
        buffer = ctypes.create_string_buffer(data, len(data))
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        target = _DataBlob()
        if not protect(
            ctypes.byref(source), "GisCode credential", None, None, None, 1,
            ctypes.byref(target),
        ):
            raise CredentialStoreError(
                f"Windows DPAPI could not protect the credential (error {last_error()})."
            )
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            if target.pbData:
                local_free(target.pbData)

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        _protect, unprotect, local_free, last_error = _win32_api()
        buffer = ctypes.create_string_buffer(data, len(data))
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        target = _DataBlob()
        if not unprotect(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        ):
            raise CredentialStoreError(
                f"Windows DPAPI could not unprotect the credential (error {last_error()})."
            )
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            if target.pbData:
                local_free(target.pbData)

    def get(self, name: str, default: str = "") -> str:
        path = self._path(name)
        if not path.is_file():
            return default
        with self._lock:
            try:
                raw = path.read_bytes()
            except OSError:
                return default
            try:
                value = self._unprotect(raw).decode("utf-8")
            except (CredentialStoreError, UnicodeDecodeError):
                return default
        return value or default

    def set(self, name: str, value: str) -> None:
        if not self.available:
            raise CredentialStoreError(
                "Windows DPAPI is not available on this system; use an environment "
                "variable or the read-only environment store instead."
            )
        if not str(value):
            raise CredentialStoreError("An empty credential cannot be stored.")
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            path = self._path(name)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(self._protect(str(value).encode("utf-8")))
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def delete(self, name: str) -> None:
        with self._lock:
            path = self._path(name)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise CredentialStoreError(f"The credential file could not be removed: {exc}") from exc


def default_credential_store() -> CredentialStore:
    """Return the best available local store without creating anything.

    The environment is a valid secret source on every platform.  Windows gets
    a protected-file store as a convenience for GUI users; non-Windows hosts
    stay read-only rather than inventing an insecure file fallback.
    """
    if WindowsDpapiStore.available:
        return WindowsDpapiStore()
    return EnvironmentCredentialStore({"openai": "OPENAI_API_KEY"})


def masked_credential_status(store: Optional[CredentialStore] = None, *, name: str = "openai") -> str:
    """Return a short status that never includes the secret."""
    env_value = os.environ.get("OPENAI_API_KEY", "") if name == "openai" else ""
    if env_value:
        return "set in the environment"
    try:
        if store is not None and store.has(name):
            return "set in the protected local store"
    except Exception:
        pass
    return "not set"
