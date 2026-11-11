"""Mock qualification for Sign in with ChatGPT (ChatGPT plan usage).

Everything here runs against a deterministic loopback server and a test-only
RSA key. No request goes to OpenAI, and no real ChatGPT account is involved.
The tests prove the protocol, refusal paths, token validation, rotation,
isolation and streaming shape - not a live sign-in.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import giscode.providers.chatgpt_plan as plan_module
from giscode.providers.base import ProviderError
from giscode.inference.router import ModelRouter
from giscode.providers.chatgpt_auth import (
    AGENT_NAME_HINT,
    DYNAMIC_CLIENT_ID,
    PLAN_SCOPE,
    REQUESTED_SCOPES,
    ChatGPTAccountStore,
    CredentialStoreError,
    LoopbackCallbackServer,
    generate_pkce,
)
from giscode.providers.credentials import MemoryCredentialStore, WindowsDpapiStore
from giscode.providers.chatgpt_plan import (
    ChatGPTPlanProvider,
    configure_chatgpt_profile,
    select_chatgpt_account,
)
from giscode.providers.profiles import ProfileStore
from giscode.providers.redaction import REDACTED
from giscode.providers.registry import ProviderRegistry
from giscode.providers.settings_adapter import DictSettings
from giscode.providers.streaming import StreamCancelled

# Test-only RSA key. Public values are served through the mock JWKS; the
# private exponent signs mock ID tokens. This is not a credential.
N = int(
    "29806780485664394742372771569105294220720020072134097770246619169228136439337639448318081447694949688495274176177571116708767553339277643650878714995493665473185274994366566041486281759612673734445913302505868170664755843747198990025433462890959741105853000338528543364077125265647510728298589035170126640623345766512418276553329144406130271778191574016463917738702477868524028757733800242169387014179021560776159219968603436972669936477697379099487322828982220016912185827768916747922425210055271943976270022251830154693887149813163453441252406881660936022606770974481241487805442351389179913433276399481580886601059"
)
E = 65537
D = int(
    "9604417152234888017029418551514122567497063092196649368496925100419231445713300899489709400364650503483512403915801120849251549622994577716365278604716991632665365285160076878421144621490465501734311521211951586035567412764262548970231947641787728043744003153163503280450717805168245537709163586221602290818258578824413740259455370849567931519829996754739929878056671833413104044570598222969783886722570700262930285649655741150227577270928935905629776641195471073778337642265748782173001284124634286916717564306939640997185622457506176387801855648819135666481783263120272378426351666877901982475052507931278733629533"
)
KID = "siwc-test-key"
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _rsa_signing_key() -> tuple[int, int]:
    return N, D


def _sign_jwt(
    claims: dict, *, bad_signature: bool = False, kid: str = KID
) -> str:
    header = _b64u(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    payload = _b64u(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    size = (N.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (size - 3 - len(digest_info)) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), D, N).to_bytes(size, "big")
    if bad_signature:
        signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    return f"{header}.{payload}.{_b64u(signature)}"


class _Server:
    """OIDC, token, revocation, models and Responses endpoints in one place."""

    def __init__(
        self,
        *,
        plan_scope: bool = True,
        refresh_error: str = "",
        response_mode: str = "ok",
        response_status: int = 200,
        stream_delay: float = 0.0,
        refresh_delay: float = 0.0,
        claims_override: dict | None = None,
        bad_signature: bool = False,
        kid: str = KID,
        jwks_kid: str = KID,
        refresh_bad_signature: bool = False,
    ) -> None:
        self.plan_scope = plan_scope
        self.refresh_error = refresh_error
        self.response_mode = response_mode
        self.response_status = response_status
        self.stream_delay = stream_delay
        self.refresh_delay = refresh_delay
        self.claims_override = dict(claims_override or {})
        self.bad_signature = bad_signature
        self.kid = kid
        self.jwks_kid = jwks_kid
        self.refresh_bad_signature = refresh_bad_signature
        self.nonce = ""
        self.email = "user@example.com"
        self.token_posts: list[dict] = []
        self.revocations: list[dict] = []
        self.responses: list[dict] = []
        self.models_calls = 0
        self.jwks_calls = 0
        self.refresh_calls = 0
        self._codes: dict[str, str] = {}
        self._clients = 0
        self._access = 0
        self._refresh = 0
        self._current_refresh = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                return

            def _send_json(self, payload: dict, status: int = 200) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            def _send_empty(self, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if path == "/.well-known/openid-configuration":
                    self._send_json(
                        {
                            "issuer": outer.base,
                            "authorization_endpoint": outer.base
                            + "/api/accounts/oauth/authorize",
                            "token_endpoint": outer.base + "/api/accounts/oauth/token",
                            "revocation_endpoint": outer.base + "/api/accounts/oauth/revoke",
                            "jwks_uri": outer.base + "/jwks",
                        }
                    )
                    return
                if path == "/jwks":
                    outer.jwks_calls += 1
                    self._send_json(
                        {
                            "keys": [
                                {
                                    "kty": "RSA",
                                    "kid": outer.jwks_kid,
                                    "use": "sig",
                                    "alg": "RS256",
                                    "n": _b64u(N.to_bytes((N.bit_length() + 7) // 8, "big")),
                                    "e": _b64u(E.to_bytes(3, "big")),
                                }
                            ]
                        }
                    )
                    return
                if path == "/v1/models":
                    outer.models_calls += 1
                    if not self.headers.get("Authorization", "").startswith("Bearer "):
                        self._send_json({"error": {"message": "missing bearer"}}, 401)
                        return
                    self._send_json(
                        {
                            "models": [
                                {
                                    "slug": "gpt-plan-1",
                                    "display_name": "Plan One",
                                    "visibility": "list",
                                },
                                {
                                    "slug": "hidden-model",
                                    "display_name": "Hidden",
                                    "visibility": "hidden",
                                },
                            ]
                        }
                    )
                    return
                self._send_json({"detail": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if path == "/api/accounts/oauth/token":
                    fields = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
                    outer.token_posts.append(fields)
                    self._token(fields)
                    return
                if path == "/api/accounts/oauth/revoke":
                    outer.revocations.append(dict(urllib.parse.parse_qsl(raw.decode("utf-8"))))
                    self._send_empty(200)
                    return
                if path == "/v1/responses":
                    payload = json.loads(raw or b"{}")
                    payload["_auth"] = self.headers.get("Authorization", "")
                    outer.responses.append(payload)
                    if outer.response_status != 200:
                        self._send_json(
                            {"error": {"message": "unauthorized"}},
                            outer.response_status,
                        )
                        return
                    self._stream(payload)
                    return
                self._send_json({"detail": "not found"}, 404)

            def _token(self, fields: dict) -> None:
                grant = fields.get("grant_type")
                client_id = fields.get("client_id", "")
                if grant == "authorization_code":
                    if fields.get("code") not in outer._codes:
                        self._send_json({"error": {"code": "invalid_grant"}}, 400)
                        return
                    outer._access += 1
                    outer._refresh += 1
                    outer._current_refresh = f"rt-{client_id}-{outer._refresh}"
                    scopes = list(REQUESTED_SCOPES)
                    if not outer.plan_scope:
                        scopes = [s for s in scopes if s != PLAN_SCOPE]
                    self._send_json(
                        {
                            "access_token": f"at-{client_id}-{outer._access}",
                            "refresh_token": outer._current_refresh,
                            "id_token": outer.id_token(client_id),
                            "token_type": "Bearer",
                            "expires_in": 3600,
                            "scope": " ".join(scopes),
                            "earliest_refresh_at": 0,
                        }
                    )
                    return
                if grant == "refresh_token":
                    outer.refresh_calls += 1
                    if outer.refresh_delay:
                        time.sleep(outer.refresh_delay)
                    if outer.refresh_error:
                        self._send_json(
                            {"error": {"code": outer.refresh_error}}, 400
                        )
                        return
                    if fields.get("refresh_token") != outer._current_refresh:
                        self._send_json({"error": {"code": "invalid_grant"}}, 400)
                        return
                    outer._access += 1
                    outer._refresh += 1
                    outer._current_refresh = f"rt-{client_id}-{outer._refresh}"
                    self._send_json(
                        {
                            "access_token": f"at-{client_id}-{outer._access}",
                            "refresh_token": outer._current_refresh,
                            "id_token": outer.id_token(
                                client_id, bad_signature=outer.refresh_bad_signature
                            ),
                            "token_type": "Bearer",
                            "expires_in": 3600,
                            "scope": " ".join(REQUESTED_SCOPES),
                        }
                    )
                    return
                self._send_json({"error": {"code": "unsupported_grant_type"}}, 400)

            def _stream(self, payload: dict) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def event(obj: dict) -> None:
                    self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                    self.wfile.flush()
                    if outer.stream_delay:
                        time.sleep(outer.stream_delay)

                try:
                    event({"type": "response.created", "response": {"id": "resp-1"}})
                    if outer.response_mode == "failed_usage_limit":
                        event(
                            {
                                "type": "response.failed",
                                "response": {
                                    "id": "resp-1",
                                    "error": {
                                        "code": "subscription_sharing_usage_limit_exceeded",
                                        "message": "limit reached",
                                    },
                                },
                            }
                        )
                        self.wfile.write(b"data: [DONE]\n\n")
                        return
                    if outer.response_mode == "failed_ineligible":
                        event(
                            {
                                "type": "response.failed",
                                "response": {
                                    "error": {
                                        "code": "subscription_sharing_user_not_eligible"
                                    }
                                },
                            }
                        )
                        self.wfile.write(b"data: [DONE]\n\n")
                        return
                    if outer.response_mode == "failed_capability":
                        event(
                            {
                                "type": "response.failed",
                                "response": {
                                    "error": {
                                        "code": "subscription_sharing_unsupported_capability",
                                        "param": "temperature",
                                    }
                                },
                            }
                        )
                        self.wfile.write(b"data: [DONE]\n\n")
                        return
                    if outer.response_mode == "incomplete":
                        event({"type": "response.incomplete"})
                        self.wfile.write(b"data: [DONE]\n\n")
                        return
                    for piece in ("Hello", ", ", "world!"):
                        event(
                            {
                                "type": "response.output_text.delta",
                                "delta": piece,
                            }
                        )
                    if outer.response_mode == "no_completed":
                        self.wfile.write(b"data: [DONE]\n\n")
                        return
                    event(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "usage": {
                                    "input_tokens": 3,
                                    "output_tokens": 7,
                                    "total_tokens": 10,
                                },
                            },
                        }
                    )
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def issue_client(self) -> str:
        self._clients += 1
        return f"oaiapp_test{self._clients}"

    def make_code(self) -> str:
        code = f"code-{len(self._codes) + 1}"
        self._codes[code] = "pending"
        return code

    def id_token(
        self,
        client_id: str,
        *,
        nonce: str | None = None,
        bad_signature: bool = False,
    ) -> str:
        claims = {
            "iss": self.base,
            "aud": client_id,
            "sub": "sub-user-1",
            "email": self.email,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "nonce": self.nonce if nonce is None else nonce,
        }
        claims.update(self.claims_override)
        return _sign_jwt(
            claims,
            bad_signature=bool(bad_signature or self.bad_signature),
            kid=self.kid,
        )

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class _TestFileStore:
    """A test-only on-disk secret store, portable across operating systems.

    It keeps the production contract (one ``<name>.bin`` per secret inside the
    account directory) without Windows DPAPI, so persistence across processes
    and rotation semantics are exercised on Linux too. It is deliberately not
    a production store: the encrypted-at-rest property is covered by the
    Windows-only DPAPI test below.
    """

    available = True
    read_only = False

    def __init__(self, directory) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_") or "secret"
        return self._directory / (safe + ".bin")

    def get(self, name, default=""):
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.is_file() else default

    def set(self, name, value):
        self._path(name).write_text(str(value), encoding="utf-8")

    def delete(self, name):
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass

    def has(self, name):
        return bool(self.get(name))


def _test_file_store(directory) -> _TestFileStore:
    return _TestFileStore(directory)


def _store(root, server) -> ChatGPTAccountStore:
    """A store for protocol tests, independent of the host platform.

    Sign-in, rotation, isolation and streaming are not Windows-specific, so the
    secret store is injected here: a real DPAPI store only exists on Windows,
    and these tests must exercise the same protocol on Linux. The production
    factory (WindowsDpapiStore, fail-closed elsewhere) is covered separately by
    test_default_secret_store_matches_the_platform.
    """
    return ChatGPTAccountStore(
        root,
        issuer=server.base,
        retry_delay=0.0,
        secret_store_factory=_test_file_store,
    )


def _sign_in(store: ChatGPTAccountStore, server: _Server, *, email: str = "") -> object:
    pending = store.begin_authorization(callback_port=45123)
    server.nonce = pending.nonce
    if email:
        server.email = email
    client_id = server.issue_client()
    return store.complete_authorization(
        pending,
        {
            "code": server.make_code(),
            "state": pending.state,
            "client_id": client_id,
        },
    )


def _provider(store, server, account_id: str = "", monkeypatch=None) -> ChatGPTPlanProvider:
    if monkeypatch is not None:
        monkeypatch.setattr(plan_module, "API_HOST", "127.0.0.1")
    return ChatGPTPlanProvider(
        server.base,
        account_store=store,
        account_id=account_id,
        timeout=5.0,
    )


# --------------------------------------------------------------------------- #
# Authorization request
# --------------------------------------------------------------------------- #
def test_dynamic_registration_url_has_every_documented_parameter(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45678)
        url = store.authorization_url(pending)
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        assert parsed.scheme == "http"
        assert parsed.path == "/api/accounts/oauth/authorize"
        assert query["client_id"] == DYNAMIC_CLIENT_ID
        assert query["agent_name_hint"] == AGENT_NAME_HINT
        assert query["ext_agent_host_id"].startswith("urn:uuid:")
        assert query["response_type"] == "code"
        assert query["redirect_uri"] == "http://127.0.0.1:45678/auth/callback"
        assert query["resource"] == "https://api.openai.com/v1"
        assert set(query["scope"].split()) == set(REQUESTED_SCOPES)
        assert PLAN_SCOPE in query["scope"]
        assert query["code_challenge_method"] == "S256"
        assert query["state"] == pending.state
        assert query["nonce"] == pending.nonce
        assert "client_secret" not in query


def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = generate_pkce()
    expected = _b64u(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


def test_host_id_is_stable_and_opaque(tmp_path):
    with _Server() as server:
        first = _store(tmp_path, server).host_id()
        second = _store(tmp_path, server).host_id()
    assert first == second
    assert first.startswith("urn:uuid:")
    assert "@" not in first


def test_loopback_listener_accepts_only_the_callback_path():
    with LoopbackCallbackServer(0) as server:
        result: dict = {}

        def browser():
            try:
                urllib.request.urlopen(
                    server.redirect_uri + "?code=abc&state=xyz", timeout=5
                ).read()
                result.update(
                    {"params": server.wait(5)}
                )
            except Exception as exc:  # noqa: BLE001
                result.update({"error": exc})

        thread = threading.Thread(target=browser, daemon=True)
        thread.start()
        thread.join(5)
    assert result.get("params") == {"code": "abc", "state": "xyz"}


# --------------------------------------------------------------------------- #
# Sign-in outcomes
# --------------------------------------------------------------------------- #
def test_successful_mock_sign_in_stores_identity_and_keeps_tokens_out_of_metadata(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        assert account.plan_usage_enabled is True
        assert account.email == "user@example.com"
        assert account.client_id.startswith("oaiapp_")
        assert PLAN_SCOPE in account.scopes
        assert store.active_account().account_id == account.account_id
        assert store.has_tokens(account.account_id)
        metadata = (store.root / "accounts" / account.account_id / "metadata.json").read_text(
            encoding="utf-8"
        )
        assert "at-" not in metadata
        assert "rt-" not in metadata
        assert "eyJ" not in metadata
        # Token material belongs to the secret store, never to the metadata file.
        assert (store.root / "accounts" / account.account_id / "tokens.bin").is_file()
        # A token that is still valid is used without a refresh request.
        token = store.access_token(account.account_id)
        assert token.startswith("at-")
        assert server.refresh_calls == 0


def test_state_mismatch_is_rejected_before_exchange(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45123)
        with pytest.raises(ProviderError, match="did not match"):
            store.complete_authorization(
                pending,
                {"code": server.make_code(), "state": "wrong-state", "client_id": "oaiapp_x"},
            )
        assert server.token_posts == []
        assert store.accounts() == []


def test_declined_consent_exchanges_nothing(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45123)
        with pytest.raises(ProviderError, match="declined"):
            store.complete_authorization(
                pending, {"error": "access_denied", "state": pending.state}
            )
        assert server.token_posts == []
        assert store.accounts() == []


def test_missing_issued_client_id_is_incomplete_registration(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45123)
        with pytest.raises(ProviderError, match="client ID"):
            store.complete_authorization(
                pending, {"code": server.make_code(), "state": pending.state}
            )
        assert server.token_posts == []


def test_nonce_mismatch_is_rejected(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45123)
        server.nonce = "not-the-pending-nonce"
        with pytest.raises(ProviderError, match="nonce"):
            store.complete_authorization(
                pending,
                {
                    "code": server.make_code(),
                    "state": pending.state,
                    "client_id": server.issue_client(),
                },
            )
        assert store.accounts() == []


def test_bad_signature_is_rejected(tmp_path):
    with _Server(bad_signature=True) as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45123)
        server.nonce = pending.nonce
        with pytest.raises(ProviderError, match="signature"):
            store.complete_authorization(
                pending,
                {
                    "code": server.make_code(),
                    "state": pending.state,
                    "client_id": server.issue_client(),
                },
            )
        assert store.accounts() == []


@pytest.mark.parametrize(
    "override, match",
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "oaiapp_other"}, "different client"),
        ({"exp": 1}, "expired"),
    ],
)
def test_issuer_audience_and_expiry_are_enforced(tmp_path, override, match):
    with _Server(claims_override=override) as server:
        store = _store(tmp_path, server)
        pending = store.begin_authorization(callback_port=45123)
        server.nonce = pending.nonce
        with pytest.raises(ProviderError, match=match):
            store.complete_authorization(
                pending,
                {
                    "code": server.make_code(),
                    "state": pending.state,
                    "client_id": server.issue_client(),
                },
            )
        assert store.accounts() == []


def test_missing_plan_scope_keeps_sign_in_but_disables_inference(tmp_path, monkeypatch):
    with _Server(plan_scope=False) as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        assert account.plan_usage_enabled is False
        assert account.email == "user@example.com"
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError, match="not enabled") as excinfo:
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert "API key" in str(excinfo.value.action)
        assert server.responses == []


def test_reauth_url_reuses_client_and_adds_account_hints(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        pending = store.begin_authorization(
            account_id=account.account_id, callback_port=45123
        )
        url = store.authorization_url(pending, account_id=account.account_id)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query["client_id"] == account.client_id
        assert "agent_name_hint" not in query
        assert query["login_hint"] == "user@example.com"
        assert query["id_token_hint"].count(".") == 2


def test_sign_in_glue_opens_the_browser_and_completes_via_loopback(tmp_path):
    """End-to-end glue: system browser -> loopback callback -> tokens."""
    with _Server() as server:
        store = _store(tmp_path, server)
        seen: dict = {}

        def fake_browser(url: str) -> bool:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            seen["url"] = url
            server.nonce = query["nonce"]
            client_id = server.issue_client()
            callback = (
                query["redirect_uri"]
                + "?code="
                + server.make_code()
                + "&state="
                + query["state"]
                + "&client_id="
                + client_id
            )
            threading.Thread(
                target=lambda: urllib.request.urlopen(callback, timeout=5).read(),
                daemon=True,
            ).start()
            return True

        account = store.sign_in(open_browser=fake_browser, timeout=15)
        assert seen["url"].startswith(server.base + "/api/accounts/oauth/authorize?")
        assert account.plan_usage_enabled is True
        assert store.has_tokens(account.account_id)


# --------------------------------------------------------------------------- #
# Refresh, rotation and revocation
# --------------------------------------------------------------------------- #
def _expire(store: ChatGPTAccountStore, account_id: str) -> None:
    path = store.root / "accounts" / account_id / "metadata.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expires_at"] = time.time() - 10
    path.write_text(json.dumps(data), encoding="utf-8")


def test_refresh_rotates_and_persists_the_replacement(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        first_refresh = (store.root / "accounts" / account.account_id / "tokens.bin").read_bytes()
        _expire(store, account.account_id)
        token = store.access_token(account.account_id)
        assert token.endswith("-2")
        assert server.refresh_calls == 1
        refreshed = store.get_account(account.account_id)
        assert refreshed.expires_at > time.time()
        assert refreshed.needs_reauth is False
        assert first_refresh != (
            store.root / "accounts" / account.account_id / "tokens.bin"
        ).read_bytes()


def test_concurrent_refresh_performs_one_rotation(tmp_path):
    with _Server(refresh_delay=0.25) as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        _expire(store, account.account_id)
        results: list[str] = []
        errors: list[BaseException] = []
        # Both workers must be inside access_token together. Starting them one
        # after the other let the first finish before the second began, so the
        # race this test exists for was not guaranteed to happen at all.
        start = threading.Barrier(2)

        def worker():
            try:
                start.wait(10)
                results.append(store.access_token(account.account_id))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        alive = [thread for thread in threads if thread.is_alive()]
        if alive:
            # A worker that never returns is a failure in its own right: left
            # alone it produced an empty result list, kept the interpreter alive
            # after the test and hid where the thread was stuck.
            frames = sys._current_frames()
            stacks = "\n".join(
                f"thread {thread.ident} (name={thread.name})\n"
                + "".join(traceback.format_stack(frames[thread.ident]))
                for thread in alive
                if thread.ident in frames
            )
            raise AssertionError(
                f"{len(alive)} refresh worker(s) did not finish; "
                f"{len(results)} result(s), {len(errors)} error(s)\n{stacks}"
            )
        assert not errors
        assert len(results) == 2
        assert results[0] == results[1]
        assert server.refresh_calls == 1


def test_invalid_refresh_clears_tokens_and_requires_sign_in(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        server.refresh_error = "invalid_grant"
        _expire(store, account.account_id)
        with pytest.raises(ProviderError, match="no longer be renewed") as excinfo:
            store.access_token(account.account_id)
        assert "Continue with ChatGPT" in str(excinfo.value.action)
        updated = store.get_account(account.account_id)
        assert updated.needs_reauth is True
        assert store.has_tokens(account.account_id) is False


def test_disconnect_revokes_remotely_then_clears_local_tokens(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        result = store.disconnect(account.account_id)
        assert result["remote_confirmed"] is True
        assert len(server.revocations) == 1
        form = server.revocations[0]
        assert form["token_type_hint"] == "refresh_token"
        assert form["client_id"] == account.client_id
        assert form["token"].startswith("rt-")
        assert store.has_tokens(account.account_id) is False
        assert store.get_account(account.account_id).needs_reauth is True
        # The registration is retained for a later sign-in.
        assert store.get_account(account.account_id).client_id == account.client_id


# --------------------------------------------------------------------------- #
# Multiple accounts
# --------------------------------------------------------------------------- #
def test_accounts_are_isolated_by_client_and_identity(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        first = _sign_in(store, server, email="first@example.com")
        second = _sign_in(store, server, email="second@example.com")
        assert first.account_id != second.account_id
        assert first.client_id != second.client_id
        assert len(store.accounts()) == 2
        first_token = store.access_token(first.account_id)
        second_token = store.access_token(second.account_id)
        assert first_token != second_token
        assert store.active_account().account_id == second.account_id
        store.set_active(first.account_id)
        assert store.active_account().account_id == first.account_id
        # The provider uses the selected account's token, never the other one.
        provider = _provider(store, server, first.account_id, monkeypatch)
        list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert server.responses[-1]["_auth"] == f"Bearer {first_token}"
        provider.use_account(second.account_id)
        list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert server.responses[-1]["_auth"] == f"Bearer {second_token}"


# --------------------------------------------------------------------------- #
# Responses API
# --------------------------------------------------------------------------- #
def test_streaming_uses_store_false_and_stream_true(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        deltas = list(
            provider.stream_chat(
                "gpt-plan-1",
                [
                    {"role": "system", "content": "You are GisCode."},
                    {"role": "user", "content": "Say hello"},
                ],
                temperature=0.9,
                max_tokens=99,
            )
        )
        text = "".join(delta.text for delta in deltas)
        assert text == "Hello, world!"
        final = deltas[-1]
        assert final.done is True
        assert final.tokens == 7
        body = server.responses[-1]
        assert body["store"] is False
        assert body["stream"] is True
        assert body["instructions"] == "You are GisCode."
        assert all(item["role"] != "system" for item in body["input"])
        assert "temperature" not in body
        assert "max_output_tokens" not in body
        assert "previous_response_id" not in body
        assert "metadata" not in body
        assert provider.usage_snapshot()["completion_tokens"] == 7


def test_non_streaming_chat_aggregates_the_required_stream(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        assert provider.chat("gpt-plan-1", [{"role": "user", "content": "hi"}]) == "Hello, world!"


def test_stream_can_be_cancelled_mid_answer(tmp_path, monkeypatch):
    with _Server(stream_delay=0.05) as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        cancel = threading.Event()
        pieces: list[str] = []
        with pytest.raises(StreamCancelled):
            for delta in provider.stream_chat(
                "gpt-plan-1",
                [{"role": "user", "content": "hi"}],
                cancel_event=cancel,
            ):
                if delta.text:
                    pieces.append(delta.text)
                    cancel.set()
        assert "".join(pieces) in {"Hello", "Hello, "}


def test_queued_stream_data_is_not_yielded_after_cancellation():
    import queue

    from giscode.providers.streaming import StreamCancelled, _ByteStream

    events = queue.Queue()
    events.put(b"first")
    events.put(b"second")
    stopped = threading.Event()
    stream = _ByteStream(
        events,
        object(),
        idle_timeout=None,
        state={"cancelled": False},
        stop_requested=stopped.is_set,
    )
    iterator = iter(stream)
    assert next(iterator) == b"first"
    stopped.set()
    with pytest.raises(StreamCancelled):
        next(iterator)


def test_usage_limit_error_is_actionable_and_not_retried(tmp_path, monkeypatch):
    with _Server(response_mode="failed_usage_limit") as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError) as excinfo:
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        error = excinfo.value
        assert getattr(error, "code", "") == "subscription_sharing_usage_limit_exceeded"
        assert "Usage" in str(error.action)
        assert "API" in str(error.action)
        assert len(server.responses) == 1


def test_ineligible_account_error_explains_the_restriction(tmp_path, monkeypatch):
    with _Server(response_mode="failed_ineligible") as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError, match="not available") as excinfo:
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert getattr(excinfo.value, "code", "") == "subscription_sharing_user_not_eligible"
        assert "Do not repeat" in str(excinfo.value.action)


def test_unsupported_capability_includes_the_parameter(tmp_path, monkeypatch):
    with _Server(response_mode="failed_capability") as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError) as excinfo:
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert "temperature" in str(excinfo.value)


def test_incomplete_answer_is_an_error(tmp_path, monkeypatch):
    with _Server(response_mode="incomplete") as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError, match="incomplete"):
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))


def test_stream_without_completed_is_not_a_success(tmp_path, monkeypatch):
    with _Server(response_mode="no_completed") as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError, match="response.completed"):
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))


def test_revoked_access_token_does_not_fall_back_to_an_api_key(tmp_path, monkeypatch):
    with _Server(response_status=401) as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError, match="rejected the access token") as excinfo:
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert "will not fall back" in str(excinfo.value.action)
        assert server.responses[-1]["_auth"].startswith("Bearer at-")


def test_model_discovery_keeps_only_visible_models(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        models = provider.list_models()
        assert [model.id for model in models] == ["gpt-plan-1"]
        assert models[0].display_name == "Plan One"
        # A second call inside the cache window does not hit the network.
        provider.list_models()
        assert server.models_calls == 1


def test_no_account_refuses_before_any_network(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        provider = _provider(store, server, monkeypatch=monkeypatch)
        with pytest.raises(ProviderError, match="No ChatGPT account"):
            list(provider.stream_chat("gpt-plan-1", [{"role": "user", "content": "hi"}]))
        assert server.responses == []
        assert store.accounts() == []


def test_token_values_are_redacted_from_error_text(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        token = store.access_token(account.account_id)
        message = provider._safe_error(ProviderError(f"failed for {token}", action=token))
        assert token not in str(message)
        assert REDACTED in str(message)


# --------------------------------------------------------------------------- #
# Registry and profiles
# --------------------------------------------------------------------------- #
def test_registry_defaults_add_chatgpt_off_and_keep_the_others():
    registry = ProviderRegistry(DictSettings({}))
    configs = registry.configs()
    assert configs["chatgpt"]["enabled"] is False
    assert configs["chatgpt"]["kind"] == "chatgpt"
    assert configs["openai"]["enabled"] is False
    assert configs["openai"]["paid_calls_allowed"] is False
    assert configs["ollama"]["enabled"] is True
    assert configs["lmstudio"]["enabled"] is True
    # Constructing it must not sign in or open a socket.
    assert registry.get("chatgpt") is not None


def test_profile_persists_selection_without_tokens():
    settings = DictSettings({})
    profile = configure_chatgpt_profile(
        settings, model="gpt-plan-1", account_id="abc123", active=True
    )
    assert profile.provider == "chatgpt"
    stored = json.dumps(settings._data)
    assert "gpt-plan-1" in stored
    assert "access_token" not in stored
    assert "refresh_token" not in stored
    assert ProfileStore(settings).active_for("coding").provider == "chatgpt"


# --------------------------------------------------------------------------- #
# Account switching and cache partitioning
# --------------------------------------------------------------------------- #
class _TestRegistry:
    """Build ChatGPT plan providers from the current settings for router tests."""

    def __init__(self, settings, store, server):
        self.settings = settings
        self.store = store
        self.server = server
        self.providers = []

    def provider_for_model(self, provider_id: str):
        cfg = (self.settings.get("providers", {}) or {}).get("chatgpt", {})
        provider = ChatGPTPlanProvider(
            self.server.base,
            account_store=self.store,
            account_id=str(cfg.get("account_id") or ""),
            timeout=5.0,
        )
        self.providers.append(provider)
        return provider

    def get(self, provider_id: str):
        return self.provider_for_model(provider_id)


def test_account_switch_binds_new_credentials_and_partitions_the_cache(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(plan_module, "API_HOST", "127.0.0.1")
    with _Server() as server:
        store = _store(tmp_path, server)
        first = _sign_in(store, server, email="first@example.com")
        second = _sign_in(store, server, email="second@example.com")
        first_token = store.access_token(first.account_id)
        second_token = store.access_token(second.account_id)
        settings = DictSettings({})
        configure_chatgpt_profile(
            settings, model="gpt-plan-1", account_id=first.account_id, active=True
        )
        select_chatgpt_account(settings, first.account_id, store=store)
        registry = _TestRegistry(settings, store, server)
        router = ModelRouter(
            builtin=None, settings=settings, purpose="coding", registry=registry
        )

        backend_a = router._backend_for(router.active_target("coding"))
        assert backend_a.provider.account().account_id == first.account_id
        list(
            backend_a.provider.stream_chat(
                "gpt-plan-1", [{"role": "user", "content": "A"}]
            )
        )
        assert server.responses[-1]["_auth"] == f"Bearer {first_token}"

        select_chatgpt_account(settings, second.account_id, store=store)
        # A request already using the old backend cannot silently change account.
        assert backend_a.provider.account().account_id == first.account_id
        backend_b = router._backend_for(router.active_target("coding"))
        assert backend_b is not backend_a
        assert backend_b.provider.account().account_id == second.account_id
        list(
            backend_b.provider.stream_chat(
                "gpt-plan-1", [{"role": "user", "content": "B"}]
            )
        )
        assert server.responses[-1]["_auth"] == f"Bearer {second_token}"

        # Switching back to A must authenticate as A, not reuse B's backend.
        select_chatgpt_account(settings, first.account_id, store=store)
        backend_a2 = router._backend_for(router.active_target("coding"))
        assert backend_a2.provider.account().account_id == first.account_id
        list(
            backend_a2.provider.stream_chat(
                "gpt-plan-1", [{"role": "user", "content": "A again"}]
            )
        )
        assert server.responses[-1]["_auth"] == f"Bearer {first_token}"

        # A restarted router reads the persisted config and binds the same account.
        select_chatgpt_account(settings, second.account_id, store=store)
        restarted = ModelRouter(
            builtin=None,
            settings=settings,
            purpose="coding",
            registry=_TestRegistry(settings, store, server),
        )
        restarted_backend = restarted._backend_for(restarted.active_target("coding"))
        assert restarted_backend.provider.account().account_id == second.account_id

        # A different profile is not silently rewritten by the account switch.
        profiles = ProfileStore(settings)
        chatgpt_profile = profiles.active_id("coding")
        local = profiles.create(
            "coding", provider="ollama", model="local-only", make_active=False
        )
        profiles.set_active(local.id)
        assert router.active_target("coding").kind == "ollama"
        profiles.set_active(chatgpt_profile)
        assert (
            router._backend_for(router.active_target("coding"))
            .provider.account()
            .account_id
            == second.account_id
        )
        assert settings.get("profiles")["items"]


def test_gui_picker_binds_selected_account_and_disambiguates_labels(tmp_path):
    from giscode.gui.main_window import MainWindow

    with _Server() as server:
        store = _store(tmp_path, server)
        first = _sign_in(store, server)
        second = _sign_in(store, server)
        assert first.label == second.label
        settings = DictSettings({})
        configure_chatgpt_profile(
            settings, model="gpt-plan-1", account_id=first.account_id, active=True
        )
        select_chatgpt_account(settings, first.account_id, store=store)
        choices = MainWindow._chatgpt_choices_for([first, second])
        labels = [label for label, _account in choices]
        assert len(set(labels)) == 2
        assert all(
            first.account_id[:8] in label or second.account_id[:8] in label
            for label in labels
        )

        class _Var:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        window = type("_Window", (), {})()
        window._chatgpt_choices = choices
        window._chatgpt_account_var = _Var(choices[1][0])
        window._status_var = _Var()
        window._model_router_cache = object()
        window._settings_source = lambda: settings
        window._settings_get = lambda key, default=None: settings.get(key, default)
        window._refresh_chatgpt_status = lambda: None
        window._chatgpt_store = lambda: store

        MainWindow._on_chatgpt_account_selected(window, None)

        assert settings.get("providers.chatgpt")["account_id"] == second.account_id
        assert window._model_router_cache is None
        assert "Coding requests use:" in MainWindow._chatgpt_bound_text(window)
# --------------------------------------------------------------------------- #
# Signing-key rotation and refresh persistence
# --------------------------------------------------------------------------- #
def test_unknown_kid_refreshes_jwks_and_preserves_the_rotation(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        baseline = server.jwks_calls
        server.kid = "rotated-signing-key"
        server.jwks_kid = "rotated-signing-key"
        _expire(store, account.account_id)
        token = store.access_token(account.account_id)
        assert token.endswith("-2")
        assert server.refresh_calls == 1
        assert server.jwks_calls == baseline + 1
        refreshed = store.get_account(account.account_id)
        assert refreshed.needs_reauth is False
        assert store.has_tokens(account.account_id) is True


def test_unverifiable_refreshed_id_token_clears_the_rotated_credentials(tmp_path):
    with _Server(refresh_bad_signature=True) as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        _expire(store, account.account_id)
        with pytest.raises(ProviderError, match="could not be verified"):
            store.access_token(account.account_id)
        updated = store.get_account(account.account_id)
        assert updated.needs_reauth is True
        assert store.has_tokens(account.account_id) is False
        with pytest.raises(ProviderError, match="needs a new sign-in"):
            store.access_token(account.account_id)
        assert server.refresh_calls == 1


def test_refresh_persistence_failure_requires_sign_in(tmp_path, monkeypatch):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)

        def fail_save(account_id, tokens):
            raise CredentialStoreError("disk full")

        monkeypatch.setattr(store, "_save_tokens", fail_save)
        _expire(store, account.account_id)
        with pytest.raises(ProviderError, match="could not be stored securely"):
            store.access_token(account.account_id)
        updated = store.get_account(account.account_id)
        assert updated.needs_reauth is True
        assert store.has_tokens(account.account_id) is False


class _FailingTokenStore:
    def __init__(self):
        self.values = {}

    def get(self, name, default=""):
        return self.values.get(name, default)

    def set(self, name, value):
        raise CredentialStoreError("disk full")

    def delete(self, name):
        self.values.pop(name, None)


def test_sign_in_token_persistence_failure_saves_no_account(tmp_path):
    failing = _FailingTokenStore()
    with _Server() as server:
        store = ChatGPTAccountStore(
            tmp_path,
            issuer=server.base,
            retry_delay=0.0,
            secret_store_factory=lambda _directory: failing,
        )
        with pytest.raises(ProviderError, match="not saved"):
            _sign_in(store, server)
        assert store.accounts() == []
        assert not (tmp_path / "accounts").exists() or not list(
            (tmp_path / "accounts").iterdir()
        )


def test_reauthorization_identity_change_is_explicit(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        pending = store.begin_authorization(
            account_id=account.account_id, callback_port=45123
        )
        server.nonce = pending.nonce
        # Same issued client (so registration matches) but a different subject:
        # the verified identity no longer maps to the selected account.
        server.claims_override = {"sub": "sub-different-user"}
        with pytest.raises(ProviderError, match="different ChatGPT account"):
            store.complete_authorization(
                pending,
                {
                    "code": server.make_code(),
                    "state": pending.state,
                    "client_id": account.client_id,
                },
            )
        assert len(store.accounts()) == 1


@pytest.mark.parametrize(
    "status, expected",
    [(429, "rate limited"), (503, "temporarily unavailable")],
)
def test_prestream_http_failures_get_actionable_guidance(
    tmp_path, monkeypatch, status, expected
):
    with _Server(response_status=status) as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        provider = _provider(store, server, account.account_id, monkeypatch)
        with pytest.raises(ProviderError) as excinfo:
            list(
                provider.stream_chat(
                    "gpt-plan-1", [{"role": "user", "content": "hi"}]
                )
            )
        error = excinfo.value
        assert expected in str(error)
        assert "stream" not in str(error.action).lower()
        assert "API" in str(error.action)


def test_invalid_client_refresh_requires_a_new_registration(tmp_path):
    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        server.refresh_error = "invalid_client"
        _expire(store, account.account_id)
        with pytest.raises(ProviderError, match="invalid_client") as excinfo:
            store.access_token(account.account_id)
        assert "register again" in str(excinfo.value.action)
        assert "network" not in str(excinfo.value.action).lower()
        assert store.get_account(account.account_id).needs_reauth is True
        assert store.has_tokens(account.account_id) is False


def test_process_lock_timeout_is_a_provider_error(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("the Windows lock implementation is exercised on Windows")
    import msvcrt

    def locked(*_args, **_kwargs):
        raise OSError("lock timed out")

    monkeypatch.setattr(msvcrt, "locking", locked)
    from giscode.providers.chatgpt_auth import _process_lock

    with pytest.raises(ProviderError, match="Another GisCode process"):
        with _process_lock(tmp_path / "contended.lock"):
            pass


# --------------------------------------------------------------------------- #
# Two processes, one refresh rotation
# --------------------------------------------------------------------------- #
_WORKER_SCRIPT = r'''
import os
import sys
import time
from pathlib import Path

root, issuer, account_id, gate, counter = sys.argv[1:6]
sys.path.insert(0, os.getcwd())

from giscode.providers.chatgpt_auth import ChatGPTAccountStore


class _FileStore:
    available = True
    read_only = False

    def __init__(self, directory):
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_") or "secret"
        return self._directory / (safe + ".bin")

    def get(self, name, default=""):
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.is_file() else default

    def set(self, name, value):
        self._path(name).write_text(str(value), encoding="utf-8")

    def delete(self, name):
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass

    def has(self, name):
        return bool(self.get(name))


def fake_http(url, *, method="GET", fields=None, headers=None, timeout=20.0):
    counter_path = Path(counter)
    try:
        value = int(counter_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        value = 0
    value += 1
    counter_path.write_text(str(value), encoding="utf-8")
    time.sleep(0.6)
    return {
        "_status": 200,
        "access_token": f"proc-at-{value}",
        "refresh_token": f"proc-rt-{value}",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
    }


print("ready", flush=True)
deadline = time.time() + 20
while not Path(gate).exists():
    if time.time() > deadline:
        raise SystemExit("gate timeout")
    time.sleep(0.02)

store = ChatGPTAccountStore(root, issuer=issuer, timeout=5.0, http_json=fake_http,
                            secret_store_factory=_FileStore)
print("TOKEN:" + store.access_token(account_id), flush=True)
'''


def test_two_process_refresh_rotates_once_and_agrees(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    script = tmp_path / "refresh_worker.py"
    script.write_text(_WORKER_SCRIPT, encoding="utf-8")
    gate = tmp_path / "gate"
    counter = tmp_path / "refresh_count"
    counter.write_text("0", encoding="utf-8")

    with _Server() as server:
        store = _store(tmp_path, server)
        account = _sign_in(store, server)
        _expire(store, account.account_id)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    str(store.root),
                    server.base,
                    account.account_id,
                    str(gate),
                    str(counter),
                ],
                cwd=str(project_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                line = process.stdout.readline()
                assert "ready" in line, process.stderr.read()
            gate.write_text("go", encoding="utf-8")
            finished = [process.communicate(timeout=60) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()

        assert all(process.returncode == 0 for process in processes), [
            item[1] for item in finished
        ]
        tokens = []
        for stdout, _stderr in finished:
            tokens.extend(
                line.split("TOKEN:", 1)[1]
                for line in stdout.splitlines()
                if line.startswith("TOKEN:")
            )
        assert len(tokens) == 2
        assert tokens[0] == tokens[1]
        assert counter.read_text(encoding="utf-8") == "1"
        assert store.access_token(account.account_id) == tokens[0]


# --------------------------------------------------------------------------- #
# The production secret store, on whichever platform this is
# --------------------------------------------------------------------------- #
def test_default_secret_store_matches_the_platform(tmp_path):
    """DPAPI on Windows; an explicit refusal (never plaintext) elsewhere."""
    if WindowsDpapiStore.available:
        store = ChatGPTAccountStore._default_secret_store(tmp_path)
        assert isinstance(store, WindowsDpapiStore)
        assert store.read_only is False
    else:
        with pytest.raises(CredentialStoreError, match="No protected credential store"):
            ChatGPTAccountStore._default_secret_store(tmp_path)


@pytest.mark.skipif(
    not WindowsDpapiStore.available, reason="DPAPI ciphertext is a Windows property"
)
def test_sign_in_ciphertext_never_contains_plaintext_tokens(tmp_path):
    """The real store keeps tokens unreadable without the Windows user key."""
    with _Server() as server:
        store = ChatGPTAccountStore(
            tmp_path,
            issuer=server.base,
            retry_delay=0.0,
            secret_store_factory=lambda directory: WindowsDpapiStore(directory),
        )
        account = _sign_in(store, server)
        blob = (store.root / "accounts" / account.account_id / "tokens.bin").read_bytes()
        assert blob
        assert b"rt-" not in blob
        assert b"eyJ" not in blob
        assert store.access_token(account.account_id).startswith("at-")
