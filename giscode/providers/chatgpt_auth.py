"""Sign in with ChatGPT, for ChatGPT plan usage (open-source clients).

This module implements OpenAI's documented *Sign in with ChatGPT* flow for
open-source applications: dynamic client registration, system-browser OAuth
with PKCE, a loopback callback, ID-token validation against OpenAI's JWKS, and
protected per-account credential storage.

Boundaries that do not move:

* The browser is the system browser. GisCode never embeds a ChatGPT login page,
  never reads cookies, passwords, MFA codes or subscription state, and never
  automates the ChatGPT website.
* A ChatGPT plan and an OpenAI API key are different products with different
  billing. This module only handles the plan. The API key path lives in
  :mod:`giscode.providers.openai` and is never used as a fallback here.
* Credentials are stored per issued client ID and verified account identity.
  Two registrations never share tokens, even for the same email address.

References followed exactly:
``https://developers.openai.com/siwc/token-sharing-open-source`` (overview,
sign-in, accounts and sessions, token reference, models and inference, errors
and recovery, preview limitations).
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from giscode.config.paths import giscode_home
from giscode.providers.base import ProviderError
from giscode.providers.credentials import (
    CredentialStore,
    CredentialStoreError,
    WindowsDpapiStore,
)

#: Production identity provider. Tests pass their own issuer.
ISSUER = "https://auth.openai.com"
AUTHORIZE_PATH = "/api/accounts/oauth/authorize"
TOKEN_PATH = "/api/accounts/oauth/token"
DISCOVERY_PATH = "/.well-known/openid-configuration"

#: The dynamic-registration entry point. It is *not* a client ID to save.
DYNAMIC_CLIENT_ID = "dynamic_agent_client"

#: Resource audience for the inference API.
RESOURCE = "https://api.openai.com/v1"

#: Scope that actually authorizes ChatGPT plan inference.
PLAN_SCOPE = "chatgpt.tokens.use.direct"

#: The complete requested set from the sign-in documentation.
REQUESTED_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "resource.invoke",
    PLAN_SCOPE,
)

#: Shown in the browser consent screen. Must be the app's real name.
AGENT_NAME_HINT = "GisCode"

CALLBACK_PATH = "/auth/callback"
CALLBACK_HOST = "127.0.0.1"

#: Refresh an access token this many seconds before it expires.
REFRESH_SKEW_SECONDS = 120.0

#: Clock skew tolerated when validating an ID token.
CLAIM_LEEWAY_SECONDS = 60

_TOKEN_REFRESH_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


# --------------------------------------------------------------------------- #
# Small encoding and crypto helpers (no third-party dependency)
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _json_b64url(payload: dict) -> str:
    return _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for ``S256`` PKCE."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_state() -> str:
    return _b64url(secrets.token_bytes(32))


def generate_nonce() -> str:
    return _b64url(secrets.token_bytes(32))


_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_verify_sha256(modulus: int, exponent: int, message: bytes, signature: bytes) -> bool:
    """Verify an RSASSA-PKCS1-v1_5 SHA-256 signature without a dependency.

    Implemented directly so Core keeps its minimal dependency set. Only the
    public operation is needed, so this cannot leak private key material.
    """
    if modulus <= 0 or exponent <= 0 or not signature:
        return False
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    try:
        recovered = pow(int.from_bytes(signature, "big"), exponent, modulus)
    except (ValueError, OverflowError):
        return False
    encoded = recovered.to_bytes(size, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if len(encoded) < len(digest_info) + 11:
        return False
    if encoded[0] != 0x00 or encoded[1] != 0x01:
        return False
    separator = encoded.find(b"\x00", 2)
    if separator < 10:
        return False
    if encoded[2:separator] != b"\xff" * (separator - 2):
        return False
    return encoded[separator + 1 :] == digest_info


def _rsa_key_from_jwk(jwk: dict) -> Optional[tuple[int, int]]:
    if str(jwk.get("kty") or "").upper() != "RSA":
        return None
    try:
        modulus = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
        exponent = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    except (KeyError, ValueError, TypeError):
        return None
    if modulus <= 0 or exponent <= 0:
        return None
    return modulus, exponent


#: Error codes documented for ChatGPT plan inference.
_PLAN_ERRORS = {
    "subscription_sharing_user_not_eligible": (
        "ChatGPT plan usage is not available for this account, workspace or policy.",
        "Use an eligible account/workspace, or configure the separate OpenAI API "
        "key provider. Do not repeat this request.",
    ),
    "subscription_sharing_usage_limit_exceeded": (
        "The ChatGPT plan usage limit has been reached.",
        "Open ChatGPT Settings, Usage, to review or raise the limit. GisCode will "
        "not retry automatically and will not switch to paid API billing.",
    ),
    "subscription_sharing_usage_unavailable": (
        "ChatGPT plan usage availability could not be checked.",
        "Keep the credentials and try again later; this is a temporary service "
        "condition, not a reason to sign in again.",
    ),
    "subscription_sharing_unsupported_capability": (
        "This ChatGPT plan request used a capability the plan route does not support.",
        "Remove the unsupported input, tool, model or override and try again.",
    ),
    "subscription_sharing_route_not_supported": (
        "This endpoint or method is not supported for ChatGPT plan usage.",
        "Use POST https://api.openai.com/v1/responses for plan inference.",
    ),
    "subscription_sharing_invalid_user": (
        "The ChatGPT subscriber context could not be validated.",
        "Sign in with ChatGPT again after the current session is revoked.",
    ),
    "chatpass_v2_scope_not_authorized": (
        "The granted permission context does not authorize this operation.",
        "Reauthorize the ChatGPT connection and grant plan usage.",
    ),
    "chatpass_v2_invalid_authorization_context": (
        "The granted permission context is not valid for this operation.",
        "Reauthorize the ChatGPT connection and grant plan usage.",
    ),
    "subscription_sharing_user_unavailable": (
        "ChatGPT account or workspace information is temporarily unavailable.",
        "Keep the credentials and retry later with backoff.",
    ),
}


def plan_error(code: str, *, param: str = "", detail: str = "") -> ProviderError:
    """Translate a documented plan error code into an actionable error."""
    message, action = _PLAN_ERRORS.get(
        str(code or ""),
        (
            f"ChatGPT plan inference failed ({code or 'unknown_error'}).",
            "Check the selected account and granted scopes, then try again.",
        ),
    )
    if param:
        message = f"{message} (parameter: {param})"
    if detail:
        message = f"{message} {detail}"
    error = ProviderError(message, action=action)
    setattr(error, "code", str(code or ""))
    return error


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _http_json(
    url: str,
    *,
    method: str = "GET",
    fields: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 20.0,
) -> dict:
    """One JSON/form request. Never puts a token in a URL or an error string."""
    data = None
    request_headers = dict(headers or {})
    if fields is not None:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request_headers.setdefault(
            "Content-Type", "application/x-www-form-urlencoded"
        )
    request_headers.setdefault("Accept", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - the body is optional
            pass
        status = exc.code
    except urllib.error.URLError as exc:
        raise ProviderError(
            "The OpenAI authorization service could not be reached.",
            action="Check the network connection, then try signing in again.",
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise ProviderError(
            "The OpenAI authorization service did not answer in time.",
            action="Try signing in again in a moment.",
        ) from exc

    text = raw.decode("utf-8", "replace").strip()
    body: dict = {}
    if text:
        try:
            parsed = json.loads(text)
            body = parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            body = {"detail": text[:400]}
    body.setdefault("_status", status)
    return body


def _status_of(body: dict) -> int:
    try:
        return int(body.get("_status", 200))
    except (TypeError, ValueError):
        return 200


def _error_code_of(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    if isinstance(error, str):
        return error
    return str(body.get("code") or "")


def _error_detail_of(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    if isinstance(body.get("detail"), str):
        return body["detail"]
    return ""


# --------------------------------------------------------------------------- #
# Loopback callback
# --------------------------------------------------------------------------- #
class LoopbackCallbackServer:
    """A one-shot HTTP listener on ``127.0.0.1`` for the OAuth redirect."""

    def __init__(self, port: int = 0) -> None:
        self._params: Optional[dict] = None
        self._event = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # noqa: D401 - silence the default log
                return

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                outer._params = {
                    key: values[-1]
                    for key, values in urllib.parse.parse_qs(
                        parsed.query, keep_blank_values=True
                    ).items()
                }
                body = (
                    b"<html><body style='font-family:Segoe UI,sans-serif;padding:32px'>"
                    b"<h2>GisCode</h2><p>Sign-in finished. You can close this window "
                    b"and return to GisCode.</p></body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                outer._event.set()

        self._httpd = ThreadingHTTPServer((CALLBACK_HOST, int(port)), Handler)
        self._httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://{CALLBACK_HOST}:{self.port}{CALLBACK_PATH}"

    def __enter__(self) -> "LoopbackCallbackServer":
        threading.Thread(
            target=self._httpd.serve_forever, name="giscode-siwc-callback", daemon=True
        ).start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def wait(self, timeout: float = 300.0) -> dict:
        if not self._event.wait(max(1.0, float(timeout))):
            raise ProviderError(
                "Sign in with ChatGPT was not completed in the browser.",
                action="Try again and finish the browser sign-in, or close the "
                "browser tab if you changed your mind.",
            )
        return dict(self._params or {})


# --------------------------------------------------------------------------- #
# Account records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChatGPTAccount:
    """One issued client registration and its verified ChatGPT identity."""

    account_id: str
    label: str
    email: str
    subject: str
    client_id: str
    host_id: str
    issuer: str
    workspace: str = ""
    scopes: tuple[str, ...] = ()
    plan_usage_enabled: bool = False
    expires_at: float = 0.0
    saved_at: str = ""
    needs_reauth: bool = False
    last_error: str = ""

    @property
    def plan_scope(self) -> bool:
        return PLAN_SCOPE in self.scopes

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "label": self.label,
            "email": self.email,
            "subject": self.subject,
            "client_id": self.client_id,
            "host_id": self.host_id,
            "issuer": self.issuer,
            "workspace": self.workspace,
            "scopes": list(self.scopes),
            "plan_usage_enabled": self.plan_usage_enabled,
            "expires_at": self.expires_at,
            "saved_at": self.saved_at,
            "needs_reauth": self.needs_reauth,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["ChatGPTAccount"]:
        try:
            account_id = str(data.get("account_id") or "").strip()
            client_id = str(data.get("client_id") or "").strip()
            subject = str(data.get("subject") or "").strip()
            if not account_id or not client_id or not subject:
                return None
            scopes = data.get("scopes")
            return cls(
                account_id=account_id,
                label=str(data.get("label") or "ChatGPT account"),
                email=str(data.get("email") or ""),
                subject=subject,
                client_id=client_id,
                host_id=str(data.get("host_id") or ""),
                issuer=str(data.get("issuer") or ISSUER),
                workspace=str(data.get("workspace") or ""),
                scopes=tuple(str(s) for s in scopes) if isinstance(scopes, list) else (),
                plan_usage_enabled=bool(data.get("plan_usage_enabled", False)),
                expires_at=float(data.get("expires_at") or 0.0),
                saved_at=str(data.get("saved_at") or ""),
                needs_reauth=bool(data.get("needs_reauth", False)),
                last_error=str(data.get("last_error") or ""),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class PendingAuthorization:
    """Everything one authorization attempt must keep until its callback."""

    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    account_id: str = ""
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "nonce": self.nonce,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "account_id": self.account_id,
            "created_at": self.created_at,
        }


@contextlib.contextmanager
def _process_lock(path: Path):
    """Serialize refresh/rotation across threads and processes.

    Refresh-token rotation is destructive: two refreshes with the same token
    invalidate the session. The in-process lock is not enough when the same
    profile is used by two processes, so a file lock backs it up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise ProviderError(
                    "Another GisCode process is refreshing this ChatGPT session.",
                    action="Wait a moment and try again.",
                ) from exc
            try:
                yield
            finally:
                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise ProviderError(
                    "Another GisCode process is refreshing this ChatGPT session.",
                    action="Wait a moment and try again.",
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _account_key(subject: str, client_id: str) -> str:
    digest = hashlib.sha256(f"{subject}|{client_id}".encode("utf-8")).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class ChatGPTAccountStore:
    """Protected, per-registration storage plus the OAuth flow itself."""

    def __init__(
        self,
        root: Optional[Path | str] = None,
        *,
        issuer: str = ISSUER,
        timeout: float = 20.0,
        secret_store_factory: Optional[Callable[[Path], CredentialStore]] = None,
        http_json: Optional[Callable[..., dict]] = None,
        clock: Callable[[], float] = time.time,
        retry_delay: float = 0.4,
    ) -> None:
        self._root = Path(root) if root is not None else giscode_home() / "chatgpt"
        self._issuer = str(issuer).rstrip("/")
        self._timeout = float(timeout)
        self._clock = clock
        self._retry_delay = float(retry_delay)
        self._http_json = http_json or _http_json
        self._secret_store_factory = secret_store_factory or self._default_secret_store
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._secret_stores: dict[str, CredentialStore] = {}
        self._jwks: Optional[dict] = None
        self._jwks_at = 0.0

    # -- paths -------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    def _accounts_root(self) -> Path:
        return self._root / "accounts"

    def _account_dir(self, account_id: str) -> Path:
        safe = "".join(ch for ch in account_id if ch.isalnum() or ch in "-_")
        return self._accounts_root() / (safe or "account")

    def _metadata_path(self, account_id: str) -> Path:
        return self._account_dir(account_id) / "metadata.json"

    @staticmethod
    def _default_secret_store(directory: Path) -> CredentialStore:
        if not WindowsDpapiStore.available:
            raise CredentialStoreError(
                "No protected credential store is available on this system, so "
                "ChatGPT credentials cannot be stored. GisCode Core stores them "
                "with Windows DPAPI."
            )
        return WindowsDpapiStore(directory)

    def _secret_store(self, account_id: str) -> CredentialStore:
        """The store for one account directory, created once per account.

        Rebuilding it on every token read gave each call its own lock and
        repeated the Windows DPAPI import; the read that decides whether a
        refresh is needed and the write that follows it now share one store.
        """

        directory = self._account_dir(account_id)
        key = str(directory)
        with self._locks_guard:
            store = self._secret_stores.get(key)
            if store is None:
                store = self._secret_store_factory(directory)
                self._secret_stores[key] = store
            return store

    def _lock_for(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    # -- host identity -----------------------------------------------------
    def host_id(self) -> str:
        """Stable, opaque per-host identifier, created once and persisted."""
        path = self._root / "host.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = str(data.get("ext_agent_host_id") or "").strip()
            if value.startswith("urn:uuid:"):
                return value
        except (OSError, ValueError, TypeError):
            pass
        value = f"urn:uuid:{uuid.uuid4()}"
        _atomic_write(
            path,
            json.dumps({"ext_agent_host_id": value}, indent=2).encode("utf-8"),
        )
        return value

    # -- account listing ---------------------------------------------------
    def accounts(self) -> list[ChatGPTAccount]:
        root = self._accounts_root()
        found: list[ChatGPTAccount] = []
        if not root.is_dir():
            return found
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                data = json.loads((child / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            account = ChatGPTAccount.from_dict(data)
            if account is not None:
                found.append(account)
        return found

    def get_account(self, account_id: str) -> Optional[ChatGPTAccount]:
        if not account_id:
            return None
        try:
            data = json.loads(
                self._metadata_path(account_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return ChatGPTAccount.from_dict(data)

    def active_account_id(self) -> str:
        try:
            data = json.loads(
                (self._root / "active.json").read_text(encoding="utf-8")
            )
            value = str(data.get("account_id") or "").strip()
        except (OSError, ValueError, TypeError):
            value = ""
        if value and self.get_account(value) is not None:
            return value
        accounts = self.accounts()
        return accounts[0].account_id if accounts else ""

    def active_account(self) -> Optional[ChatGPTAccount]:
        return self.get_account(self.active_account_id())

    def set_active(self, account_id: str) -> ChatGPTAccount:
        account = self.get_account(account_id)
        if account is None:
            raise ProviderError("That ChatGPT account is not saved on this host.")
        _atomic_write(
            self._root / "active.json",
            json.dumps({"account_id": account_id}, indent=2).encode("utf-8"),
        )
        return account

    # -- tokens ------------------------------------------------------------
    #: One protected value holds the token set. Rotation becomes a single
    #: atomic write instead of three independent ones, so a crash can never
    #: leave a new access token paired with an old refresh token.
    _TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")
    _TOKEN_BLOB = "tokens"

    def _load_tokens(self, account_id: str) -> dict:
        store = self._secret_store(account_id)
        raw = store.get(self._TOKEN_BLOB, "")
        if raw:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                tokens = {
                    name: str(data.get(name) or "") for name in self._TOKEN_FIELDS
                }
                if tokens["access_token"] or tokens["refresh_token"]:
                    return tokens
        # Older installs stored each field separately; read them once so an
        # upgrade does not force a new sign-in. The next save migrates them.
        legacy = {name: store.get(name, "") for name in self._TOKEN_FIELDS}
        return legacy if legacy["access_token"] or legacy["refresh_token"] else {}

    def _save_tokens(self, account_id: str, tokens: dict) -> None:
        store = self._secret_store(account_id)
        payload = {
            name: str(tokens.get(name) or "") for name in self._TOKEN_FIELDS
        }
        store.set(
            self._TOKEN_BLOB, json.dumps(payload, separators=(",", ":"))
        )
        for name in self._TOKEN_FIELDS:
            with contextlib.suppress(Exception):
                store.delete(name)

    def _clear_tokens(self, account_id: str) -> None:
        store = self._secret_store(account_id)
        for name in (self._TOKEN_BLOB,) + self._TOKEN_FIELDS:
            with contextlib.suppress(Exception):
                store.delete(name)

    def has_tokens(self, account_id: str) -> bool:
        try:
            return bool(self._load_tokens(account_id))
        except (CredentialStoreError, OSError):
            return False

    def _save_account(self, account: ChatGPTAccount) -> None:
        _atomic_write(
            self._metadata_path(account.account_id),
            json.dumps(account.as_dict(), indent=2).encode("utf-8"),
        )

    # -- authorization -----------------------------------------------------
    def begin_authorization(
        self, *, account_id: str = "", callback_port: int = 0
    ) -> PendingAuthorization:
        """Prepare one attempt. The listener is started by the caller."""
        account = self.get_account(account_id) if account_id else None
        if account_id and account is None:
            raise ProviderError("That ChatGPT account is not saved on this host.")
        verifier, _challenge = generate_pkce()
        redirect_uri = f"http://{CALLBACK_HOST}:{int(callback_port)}{CALLBACK_PATH}"
        return PendingAuthorization(
            state=generate_state(),
            nonce=generate_nonce(),
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=account.client_id if account else DYNAMIC_CLIENT_ID,
            account_id=account.account_id if account else "",
        )

    def authorization_url(
        self, pending: PendingAuthorization, *, account_id: str = ""
    ) -> str:
        """Build the documented authorize URL (no secret is ever included)."""
        account = self.get_account(account_id or pending.account_id)
        _verifier, challenge = generate_pkce_from(pending.code_verifier)
        params = {
            "client_id": pending.client_id,
            "response_type": "code",
            "redirect_uri": pending.redirect_uri,
            "scope": " ".join(REQUESTED_SCOPES),
            "resource": RESOURCE,
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "ext_agent_host_id": self.host_id(),
        }
        if pending.client_id == DYNAMIC_CLIENT_ID:
            params["agent_name_hint"] = AGENT_NAME_HINT
        elif account is not None:
            # The retained ID token is an account hint for the authorize
            # endpoint only. It is never logged, never placed in a URL by us
            # other than this one, and never sent to the token endpoint.
            try:
                id_token = str(self._load_tokens(account.account_id).get("id_token") or "")
            except (CredentialStoreError, OSError):
                id_token = ""
            if id_token:
                params["id_token_hint"] = id_token
            if account.email:
                params["login_hint"] = account.email
        return f"{self._issuer}{AUTHORIZE_PATH}?{urllib.parse.urlencode(params)}"

    def sign_in(
        self,
        *,
        account_id: str = "",
        open_browser: Optional[Callable[[str], bool]] = None,
        timeout: float = 300.0,
    ) -> ChatGPTAccount:
        """Run the whole flow in the system browser and store the result."""
        opener = open_browser or webbrowser.open
        with LoopbackCallbackServer(0) as server:
            pending = self.begin_authorization(account_id=account_id)
            pending.redirect_uri = server.redirect_uri
            url = self.authorization_url(pending)
            try:
                opened = opener(url)
            except Exception as exc:  # noqa: BLE001 - the browser is external
                raise ProviderError(
                    "The system browser could not be opened for ChatGPT sign-in.",
                    action="Open the authorization URL manually, or check the "
                    "default browser, then try again.",
                ) from exc
            if opened is False:
                raise ProviderError(
                    "The system browser refused to open the ChatGPT sign-in page.",
                    action="Set a default browser, then try again.",
                )
            params = server.wait(timeout)
        return self.complete_authorization(pending, params)

    def complete_authorization(
        self, pending: PendingAuthorization, params: dict
    ) -> ChatGPTAccount:
        """Validate the callback, exchange the code, validate the ID token."""
        params = params or {}
        if not hmac.compare_digest(str(params.get("state") or ""), pending.state):
            raise ProviderError(
                "The ChatGPT sign-in callback did not match this request.",
                action="Nothing was saved. Start the sign-in again.",
            )
        error = str(params.get("error") or "").strip()
        if error:
            description = str(params.get("error_description") or "").strip()
            if error == "access_denied":
                raise ProviderError(
                    "ChatGPT sign-in was declined or plan usage was not granted.",
                    action="GisCode did not receive an authorization code and "
                    "nothing was saved. You can try again, or use the separate "
                    "OpenAI API key option.",
                )
            raise ProviderError(
                f"The ChatGPT authorization failed ({error})."
                + (f" {description}" if description else ""),
                action="Start the sign-in again.",
            )

        code = str(params.get("code") or "").strip()
        if not code:
            raise ProviderError(
                "The ChatGPT callback did not include an authorization code.",
                action="Start the sign-in again.",
            )

        issued_client_id = str(params.get("client_id") or "").strip()
        if pending.client_id == DYNAMIC_CLIENT_ID:
            if not issued_client_id:
                raise ProviderError(
                    "ChatGPT did not issue a client ID, so registration is incomplete.",
                    action="Start the sign-in again; nothing was saved.",
                )
            client_id = issued_client_id
        else:
            client_id = pending.client_id
            if issued_client_id and issued_client_id != client_id:
                raise ProviderError(
                    "The ChatGPT callback returned a different client registration.",
                    action="Nothing was replaced. Start the sign-in for the "
                    "selected account again.",
                )

        body = self._http_json(
            f"{self._issuer}{TOKEN_PATH}",
            method="POST",
            fields={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": pending.code_verifier,
                "redirect_uri": pending.redirect_uri,
                "resource": RESOURCE,
            },
            timeout=self._timeout,
        )
        status = _status_of(body)
        access_token = str(body.get("access_token") or "")
        if status != 200 or not access_token:
            code_value = _error_code_of(body)
            if code_value == "invalid_grant":
                raise ProviderError(
                    "ChatGPT rejected the authorization code.",
                    action="Start the sign-in again; the previous code is no longer valid.",
                )
            raise ProviderError(
                "The ChatGPT token exchange failed"
                + (f" ({code_value})" if code_value else "")
                + ".",
                action="Start the sign-in again. No credential was saved.",
            )

        expires_in = body.get("expires_in")
        try:
            lifetime = float(expires_in) if expires_in is not None else 3600.0
        except (TypeError, ValueError):
            lifetime = 3600.0
        expires_at = self._clock() + max(0.0, lifetime)
        id_token = str(body.get("id_token") or "")
        if not id_token:
            raise ProviderError(
                "The ChatGPT token response did not include an ID token.",
                action="Start the sign-in again. No credential was saved.",
            )
        claims = self.validate_id_token(
            id_token,
            expected_nonce=pending.nonce,
            client_id=client_id,
        )
        subject = str(claims.get("sub") or "")
        if not subject:
            raise ProviderError(
                "The ChatGPT ID token has no subject, so its identity is unusable.",
                action="Start the sign-in again.",
            )

        scopes = tuple(
            str(body.get("scope") or params.get("scope") or "").split()
        )
        account_id = _account_key(subject, client_id)
        previous = self.get_account(account_id)
        if pending.account_id and previous is None:
            raise ProviderError(
                "The sign-in returned a different ChatGPT account than the one "
                "selected.",
                action=(
                    "Nothing was replaced. Start the sign-in again for the selected "
                    "account, or remove the stale registration first."
                ),
            )
        email = str(claims.get("email") or "") or (
            previous.email if previous is not None else ""
        )
        workspace = _workspace_hint(claims) or (
            previous.workspace if previous is not None else ""
        )
        account = ChatGPTAccount(
            account_id=account_id,
            label=_account_label(email, workspace, account_id),
            email=email,
            subject=subject,
            client_id=client_id,
            host_id=self.host_id(),
            issuer=self._issuer,
            workspace=workspace,
            scopes=scopes,
            plan_usage_enabled=PLAN_SCOPE in scopes,
            expires_at=expires_at,
            saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            needs_reauth=False,
            last_error="",
        )
        try:
            self._save_tokens(
                account_id,
                {
                    "access_token": access_token,
                    "refresh_token": str(body.get("refresh_token") or ""),
                    "id_token": id_token,
                },
            )
        except (CredentialStoreError, OSError) as exc:
            with contextlib.suppress(Exception):
                self._clear_tokens(account_id)
            raise ProviderError(
                "The ChatGPT credential could not be stored securely, so the "
                "sign-in was not saved.",
                action=(
                    "Check that the Windows credential store is available, then "
                    "sign in again."
                ),
            ) from exc
        try:
            self._save_account(account)
        except (OSError, TypeError, ValueError) as exc:
            with contextlib.suppress(Exception):
                self._clear_tokens(account_id)
            raise ProviderError(
                "The ChatGPT account record could not be saved, so the sign-in "
                "was not completed.",
                action=(
                    "Check that the GisCode home directory is writable, then sign "
                    "in again."
                ),
            ) from exc
        self.set_active(account_id)
        return account

    # -- authorization URL helper needs the retained id_token --------------
    # (declared here so ChatGPTAccount can expose it without a second read)

    # -- ID token validation ----------------------------------------------
    def _discovery(self) -> dict:
        body = self._http_json(
            f"{self._issuer}{DISCOVERY_PATH}", timeout=self._timeout
        )
        if _status_of(body) != 200 or not body.get("jwks_uri"):
            raise ProviderError(
                "OpenAI's OIDC discovery document could not be read, so the "
                "ChatGPT ID token cannot be verified.",
                action="Check the network connection, then sign in again.",
            )
        return body

    def _jwks_for(self, *, force: bool = False) -> dict:
        now = self._clock()
        if not force and self._jwks is not None and now - self._jwks_at < 300.0:
            return self._jwks
        discovery = self._discovery()
        body = self._http_json(str(discovery["jwks_uri"]), timeout=self._timeout)
        keys = body.get("keys")
        if _status_of(body) != 200 or not isinstance(keys, list):
            raise ProviderError(
                "OpenAI's signing keys could not be read, so the ChatGPT ID "
                "token cannot be verified.",
                action="Check the network connection, then sign in again.",
            )
        self._jwks = body
        self._jwks_at = now
        return body

    def validate_id_token(
        self,
        token: str,
        *,
        expected_nonce: str = "",
        client_id: str,
        leeway: int = CLAIM_LEEWAY_SECONDS,
    ) -> dict:
        """Verify signature, issuer, audience, times and nonce.

        Returns the verified claims. Raises :class:`ProviderError` otherwise;
        no claim is trusted before the signature passes.
        """
        parts = str(token or "").split(".")
        if len(parts) != 3:
            raise ProviderError("The ChatGPT ID token is malformed.")
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, TypeError):
            raise ProviderError("The ChatGPT ID token is malformed.") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise ProviderError("The ChatGPT ID token is malformed.")
        if str(header.get("alg") or "") != "RS256":
            raise ProviderError(
                "The ChatGPT ID token uses an unsupported signature algorithm.",
                action="Sign in again. Nothing was trusted.",
            )
        kid = str(header.get("kid") or "")
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        jwks = self._jwks_for()
        verified = self._verify_with_jwks(jwks, kid, signing_input, signature)
        if not verified:
            # A cached JWKS can predate a signing-key rotation. Refresh once
            # before rejecting an otherwise valid token; if the key still does
            # not verify, the token is not trusted.
            jwks = self._jwks_for(force=True)
            verified = self._verify_with_jwks(jwks, kid, signing_input, signature)
        if not verified:
            raise ProviderError(
                "The ChatGPT ID token signature could not be verified.",
                action="Sign in again. No credential was trusted or saved.",
            )

        if str(claims.get("iss") or "") != self._issuer:
            raise ProviderError(
                "The ChatGPT ID token was issued by an unexpected issuer.",
                action="Sign in again. No credential was trusted or saved.",
            )
        audience = claims.get("aud")
        audiences = (
            [str(a) for a in audience]
            if isinstance(audience, list)
            else [str(audience or "")]
        )
        if client_id not in audiences:
            raise ProviderError(
                "The ChatGPT ID token was issued for a different client.",
                action="Sign in again. No credential was trusted or saved.",
            )
        now = self._clock()
        try:
            expires = float(claims.get("exp"))
        except (TypeError, ValueError):
            raise ProviderError("The ChatGPT ID token has no valid expiry.") from None
        if expires + float(leeway) < now:
            raise ProviderError(
                "The ChatGPT ID token has expired.",
                action="Sign in again.",
            )
        not_before = claims.get("nbf")
        if not_before is not None:
            try:
                if float(not_before) - float(leeway) > now:
                    raise ProviderError(
                        "The ChatGPT ID token is not valid yet.",
                        action="Check the system clock, then sign in again.",
                    )
            except (TypeError, ValueError):
                raise ProviderError("The ChatGPT ID token has an invalid nbf.") from None
        if expected_nonce and not hmac.compare_digest(
            str(claims.get("nonce") or ""), str(expected_nonce)
        ):
            raise ProviderError(
                "The ChatGPT ID token nonce does not match this sign-in attempt.",
                action="Sign in again. The response was not accepted.",
            )
        return claims

    @staticmethod
    def _verify_with_jwks(
        jwks: dict, kid: str, signing_input: bytes, signature: bytes
    ) -> bool:
        """True when any matching RSA key in *jwks* verifies the signature."""
        for key in jwks.get("keys", []):
            if not isinstance(key, dict):
                continue
            if kid and str(key.get("kid") or "") != kid:
                continue
            if str(key.get("kty") or "").upper() != "RSA":
                continue
            pair = _rsa_key_from_jwk(key)
            if pair is None:
                continue
            if rsa_verify_sha256(pair[0], pair[1], signing_input, signature):
                return True
        return False

    # -- refresh -----------------------------------------------------------
    def access_token(self, account_id: str, *, force: bool = False) -> str:
        """Return a usable access token, refreshing under a lock when needed."""
        account = self.get_account(account_id)
        if account is None:
            raise ProviderError("That ChatGPT account is not saved on this host.")
        if account.needs_reauth:
            raise ProviderError(
                "The ChatGPT session needs a new sign-in.",
                action="Use Continue with ChatGPT to reauthorize this account.",
            )
        with self._lock_for(account_id):
            tokens = self._load_tokens(account_id)
            if not tokens.get("access_token") and not tokens.get("refresh_token"):
                raise ProviderError(
                    "No ChatGPT credentials are stored for this account.",
                    action="Use Continue with ChatGPT to sign in again.",
                )
            if not force and tokens.get("access_token") and not self._expiring(account):
                return str(tokens["access_token"])
            return self._refresh_locked(account, force=force)

    def _expiring(self, account: ChatGPTAccount) -> bool:
        return bool(account.expires_at) and account.expires_at - self._clock() <= REFRESH_SKEW_SECONDS

    def refresh(self, account_id: str) -> ChatGPTAccount:
        with self._lock_for(account_id):
            account = self.get_account(account_id)
            if account is None:
                raise ProviderError("That ChatGPT account is not saved on this host.")
            self._refresh_locked(account, force=True)
            updated = self.get_account(account_id)
            if updated is None:  # pragma: no cover - defensive
                raise ProviderError("The ChatGPT account record disappeared.")
            return updated

    def _refresh_locked(self, account: ChatGPTAccount, *, force: bool) -> str:
        """Perform the refresh exactly once per expired token set."""
        account_id = account.account_id
        with _process_lock(self._account_dir(account_id) / "refresh.lock"):
            # Another process may have refreshed while we waited.
            current = self.get_account(account_id) or account
            tokens = self._load_tokens(account_id)
            if not tokens.get("refresh_token"):
                raise ProviderError(
                    "The ChatGPT session cannot be renewed because its refresh "
                    "token is missing.",
                    action="Use Continue with ChatGPT to sign in again.",
                )
            if not force and tokens.get("access_token") and not self._expiring(current):
                return str(tokens["access_token"])
            body = self._http_json(
                f"{self._issuer}{TOKEN_PATH}",
                method="POST",
                fields={
                    "grant_type": "refresh_token",
                    "client_id": current.client_id,
                    "refresh_token": str(tokens["refresh_token"]),
                    "resource": RESOURCE,
                },
                timeout=self._timeout,
            )
            status = _status_of(body)
            code = _error_code_of(body)
            if status != 200 or not body.get("access_token"):
                if code == "invalid_client":
                    self._clear_tokens(account_id)
                    failed = ChatGPTAccount(
                        **{
                            **current.as_dict(),
                            "needs_reauth": True,
                            "last_error": code,
                            "scopes": current.scopes,
                        }
                    )
                    self._save_account(failed)
                    raise ProviderError(
                        "ChatGPT no longer recognizes this app registration "
                        "(invalid_client).",
                        action=(
                            "Disconnect the account and use Continue with ChatGPT to "
                            "register again. No API-key billing is involved."
                        ),
                    )
                if code in _TOKEN_REFRESH_ERRORS:
                    self._clear_tokens(account_id)
                    failed = ChatGPTAccount(
                        **{
                            **current.as_dict(),
                            "needs_reauth": True,
                            "last_error": code,
                            "scopes": current.scopes,
                        }
                    )
                    self._save_account(failed)
                    raise ProviderError(
                        "The ChatGPT session can no longer be renewed "
                        f"({code or 'invalid refresh'}).",
                        action="Use Continue with ChatGPT to sign in again.",
                    )
                raise ProviderError(
                    "The ChatGPT token refresh failed"
                    + (f" ({code})" if code else "")
                    + ".",
                    action="Check the network connection and try again.",
                )
            access = str(body.get("access_token") or "")
            refresh_token = str(body.get("refresh_token") or "") or str(
                tokens["refresh_token"]
            )
            id_token = str(body.get("id_token") or "") or str(tokens.get("id_token") or "")
            if body.get("id_token"):
                # A refreshed ID token is validated without a nonce, but its
                # signature, issuer and audience still must hold. The JWKS
                # lookup refreshes once if the cached key set is stale.
                try:
                    self.validate_id_token(
                        id_token, client_id=current.client_id, expected_nonce=""
                    )
                except ProviderError as exc:
                    # The server has already rotated the refresh token, so the
                    # old one is dead. Do not leave it in place for a retry.
                    self._clear_tokens(account_id)
                    failed = ChatGPTAccount(
                        **{
                            **current.as_dict(),
                            "needs_reauth": True,
                            "last_error": "refresh_credential_invalid",
                            "scopes": current.scopes,
                        }
                    )
                    self._save_account(failed)
                    raise ProviderError(
                        "The renewed ChatGPT credential could not be verified.",
                        action=(
                            "Use Continue with ChatGPT to sign in again. The "
                            "previous refresh token was already replaced."
                        ),
                    ) from exc
            try:
                lifetime = float(body.get("expires_in") or 3600.0)
            except (TypeError, ValueError):
                lifetime = 3600.0
            scopes = tuple(str(body.get("scope") or "").split()) or current.scopes
            updated = ChatGPTAccount(
                **{
                    **current.as_dict(),
                    "scopes": scopes,
                    "plan_usage_enabled": PLAN_SCOPE in scopes,
                    "expires_at": self._clock() + max(0.0, lifetime),
                    "needs_reauth": False,
                    "last_error": "",
                }
            )
            try:
                self._save_tokens(
                    account_id,
                    {
                        "access_token": access,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                    },
                )
            except (CredentialStoreError, OSError) as exc:
                with contextlib.suppress(Exception):
                    self._clear_tokens(account_id)
                failed = ChatGPTAccount(
                    **{
                        **current.as_dict(),
                        "needs_reauth": True,
                        "last_error": "credential_persistence_failed",
                        "scopes": current.scopes,
                    }
                )
                with contextlib.suppress(Exception):
                    self._save_account(failed)
                raise ProviderError(
                    "The renewed ChatGPT credential could not be stored securely.",
                    action="Use Continue with ChatGPT to sign in again.",
                ) from exc
            self._save_account(updated)
            return access

    # -- disconnect --------------------------------------------------------
    def revoke(self, account_id: str) -> dict:
        """Revoke the renewable session, then clear local tokens.

        Returns ``{"remote_confirmed": bool}``. A network failure still clears
        the local tokens - the user asked to disconnect - but is reported as
        unconfirmed so the UI does not claim OpenAI revoked it.
        """
        account = self.get_account(account_id)
        if account is None:
            raise ProviderError("That ChatGPT account is not saved on this host.")
        tokens = self._load_tokens(account_id)
        refresh_token = str(tokens.get("refresh_token") or "")
        confirmed = False
        if refresh_token:
            try:
                discovery = self._discovery()
                endpoint = str(discovery.get("revocation_endpoint") or "")
            except ProviderError:
                endpoint = ""
            if endpoint:
                for attempt in range(3):
                    try:
                        body = self._http_json(
                            endpoint,
                            method="POST",
                            fields={
                                "token": refresh_token,
                                "token_type_hint": "refresh_token",
                                "client_id": account.client_id,
                            },
                            timeout=self._timeout,
                        )
                    except ProviderError:
                        body = {"_status": 0}
                    status = _status_of(body)
                    if status == 200:
                        confirmed = True
                        break
                    if status and status < 500:
                        break
                    if attempt < 2 and self._retry_delay:
                        time.sleep(self._retry_delay * (2**attempt))
        self._clear_tokens(account_id)
        updated = ChatGPTAccount(
            **{
                **account.as_dict(),
                "needs_reauth": True,
                "last_error": "" if confirmed else "revocation_unconfirmed",
                "scopes": account.scopes,
            }
        )
        self._save_account(updated)
        return {"remote_confirmed": confirmed}

    def disconnect(self, account_id: str) -> dict:
        """Sign out: revoke when possible, always stop using local tokens."""
        return self.revoke(account_id)

    def forget_account(self, account_id: str) -> None:
        """Remove a saved registration entirely (its tokens are deleted first)."""
        self._clear_tokens(account_id)
        directory = self._account_dir(account_id)
        try:
            for child in directory.iterdir():
                with contextlib.suppress(OSError):
                    child.unlink()
            directory.rmdir()
        except OSError:
            pass
        if self.active_account_id() == account_id:
            remaining = self.accounts()
            if remaining:
                self.set_active(remaining[0].account_id)
            else:
                with contextlib.suppress(OSError):
                    (self._root / "active.json").unlink()


def generate_pkce_from(verifier: str) -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for an existing verifier."""
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _workspace_hint(claims: dict) -> str:
    """Best-effort workspace display value, never invented.

    OpenAI documents the plan registration as bound to a workspace but does not
    promise a claim name in the ID token. If no recognised claim is present the
    UI shows "not reported" instead of guessing.
    """
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in ("workspace_id", "workspace", "organization_id"):
            value = str(auth.get(key) or "").strip()
            if value:
                return value
    for key in ("workspace_id", "workspace"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return ""


def _account_label(email: str, workspace: str, account_id: str) -> str:
    base = email.strip() or f"ChatGPT account {account_id[:6]}"
    if workspace:
        return f"{base} · {workspace}"
    return base
