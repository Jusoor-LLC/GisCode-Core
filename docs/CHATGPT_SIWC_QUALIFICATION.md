# Sign in with ChatGPT — consolidated qualification package

Date: 2026-11-08
Target: the repository root (Core edition)
Flow: Sign in with ChatGPT for open-source applications, ChatGPT plan usage
Official reference: <https://developers.openai.com/siwc/token-sharing-open-source>

---

## 1. Verdict

**PASS WITH OBSERVATIONS — both blocking SIWC defects are closed and
mock-qualified. No live account round trip was performed.**

The selected account is now the account actually used, including after A→B→A
switching, a fresh router, and cached backends. An in-flight request keeps the
account it started with. Refresh rotation now survives a signing-key change and
does not lose the rotated refresh token; when the replacement cannot be
verified or stored, the dead credentials are cleared and reauthorization is
required instead of reusing them.

Every OAuth, token, refresh, revocation and streaming test uses a deterministic
loopback server and a test-only RSA key. **No real ChatGPT account was used, no
real token was issued, and no request was sent to OpenAI.** Section 9 states
exactly what is not proven and what a live qualification requires.

The observations are: no live round trip, Windows-only protected storage, and
one pre-existing fixture failure in the complete suite. None is caused by this
change.

The OpenAI API-key integration and local providers keep their own credentials,
billing and behaviour. There is no automatic fallback between them.

---

## 2. Official requirements → implementation

| Requirement (official docs) | Implementation | Evidence |
| --- | --- | --- |
| First registration uses `client_id=dynamic_agent_client` | `begin_authorization` selects it only when no issued client ID exists | `test_dynamic_registration_url_has_every_documented_parameter` |
| `agent_name_hint` on registration only, consistent name | `AGENT_NAME_HINT = "GisCode"`; omitted on reauthorization | URL test; `test_reauth_url_reuses_client_and_adds_account_hints` |
| Stable `ext_agent_host_id` per host, opaque | `host.json` with `urn:uuid:` created once, reused | `test_host_id_is_stable_and_opaque` |
| System browser, no embedded login or scraping | `webbrowser.open` passed to a loopback listener; no website automation anywhere | `test_sign_in_glue_opens_the_browser_and_completes_via_loopback` |
| Loopback `127.0.0.1`, path `/auth/callback`, no `localhost` | `LoopbackCallbackServer` binds `127.0.0.1` and matches the exact path | `test_loopback_listener_accepts_only_the_callback_path`; URL test |
| Fresh `state`, nonce, PKCE S256 per attempt | Generated per attempt; verifier retained until callback; state/nonce compared with `hmac.compare_digest` | `test_pkce_challenge_is_s256_of_the_verifier`; state/nonce tests |
| Exact scope set incl. plan scope | `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct` | URL test |
| `resource=https://api.openai.com/v1` | Constant sent in authorize, exchange and refresh | URL test; token request assertions |
| Callback returns `code`, `state`, issued `client_id` | Issued ID required for new registration; a different ID on reauthorization is rejected | `test_missing_issued_client_id_is_incomplete_registration` |
| Exchange code with issued client ID, PKCE verifier, same redirect URI | Form-encoded `authorization_code` grant; no client secret | `test_successful_mock_sign_in_stores_identity_and_protected_tokens` |
| Validate ID token: signature, issuer, audience, expiry, nonce | RS256 against JWKS; `iss`, `aud`, `exp`, `nbf`, nonce | signature, issuer, audience, expiry, nonce tests |
| Check `chatgpt.tokens.use.direct` before inference | `plan_usage_enabled` from granted scopes; provider refuses otherwise | `test_missing_plan_scope_keeps_sign_in_but_disables_inference` |
| Store identity, tokens, scopes, expiry per issued client ID | One DPAPI-protected token blob plus non-secret metadata per account directory | success test inspects files |
| Retain ID token for `id_token_hint`; email for `login_hint` | Reauthorization adds both; ID token is never logged | reauth URL test |
| Refresh near expiry, rotate, serialize | 120 s skew, in-process + file lock, replacement persisted atomically | rotation and concurrency tests |
| Reject a reauthorization for a different identity | The verified subject must map to the selected account; otherwise nothing is saved | `test_reauthorization_identity_change_is_explicit` |
| Survive signing-key rotation during refresh | A failed signature triggers exactly one forced JWKS refresh; the rotated token set is kept only if the renewed ID token verifies | `test_unknown_kid_refreshes_jwks_and_preserves_the_rotation` |
| Never lose or reuse an already-rotated refresh token | Tokens are one atomic blob; a bad renewed ID token or failed save clears them and requires reauthorization | `test_unverifiable_refreshed_id_token_clears_the_rotated_credentials`; `test_refresh_persistence_failure_requires_sign_in` |
| Revoke on sign-out via discovery `revocation_endpoint` | Form POST `token`, `token_type_hint=refresh_token`, issued `client_id`; bounded retry | `test_disconnect_revokes_remotely_then_clears_local_tokens` |
| Responses API: `POST /v1/responses`, `store:false`, `stream:true` | Payload built with exactly those values | `test_streaming_uses_store_false_and_stream_true` |
| Omit unsupported fields; system text via `instructions` | No temperature, max_output_tokens, metadata, previous_response_id; system → `instructions` | same test |
| Success only on `response.completed` | Completed flag required; early end is an error | `test_stream_without_completed_is_not_a_success` |
| Handle usage/eligibility/unsupported errors by code | `plan_error()` maps documented codes to messages and actions | usage-limit, ineligible, capability tests |
| Handle pre-stream 429/503 without a misleading retry | 429 points to ChatGPT usage; 503 says temporarily unavailable; neither suggests an API-key fallback | `test_prestream_http_failures_get_actionable_guidance[429/503]` |
| Never silently switch billing | No API-key path in the plan provider; refusal message says so | missing-scope and 401 tests |
| Multiple accounts/workspaces isolated | Account id = hash(subject, client ID); separate dirs and tokens | `test_accounts_are_isolated_by_client_and_identity` |
| Selected account is the account used; caches cannot serve the previous one | Provider config stores the account id; router backends are keyed by account identity; GUI clears its router cache | `test_account_switch_binds_new_credentials_and_partitions_the_cache`; `test_gui_picker_binds_selected_account_and_disambiguates_labels` |

---

## 3. Blocking defects closed

### D1 — account switching used stale credentials

**Root cause:** selecting an account changed only the active-account pointer.
The provider configuration and router/backend caches could still hold the
previously selected account, so a request could authenticate as A while the
picker showed B.

**Fix:**

- `select_chatgpt_account(settings, account_id, store=...)` updates the active
  pointer and `providers.chatgpt.account_id` together. If saving the provider
  configuration fails, the previous active pointer is restored.
- `ChatGPTPlanProvider.cache_identity()` returns `(kind, account_id)`.
  `ModelRouter` includes that identity in its backend cache key, so A and B
  never share a backend.
- `ModelRouter.invalidate()` clears cached backends on a profile switch; the
  GUI also clears `_model_router_cache` when an account is selected or settings
  are saved.
- An object already running a request keeps its own provider/account binding;
  it cannot silently switch mid-request.
- The picker disambiguates duplicate labels with a short account-id suffix, and
  the panel reports the account actually bound to coding requests, not merely
  the selected row.

**Evidence:** `test_account_switch_binds_new_credentials_and_partitions_the_cache`
covers A→B→A, the old in-flight backend, a restarted router, and an unrelated
local profile. `test_gui_picker_binds_selected_account_and_disambiguates_labels`
covers duplicate labels, persisted `account_id`, cache clearing, and bound-text
identity. Both pass.

### D2 — refresh rotation could lose the rotated token

**Root cause:** access, refresh and ID tokens were persisted separately, and a
renewed ID token was not always validated before the new pair was stored. A
failure between writes, an unknown signing key, or a persistence error could
leave the client holding an old refresh token after the server had rotated it.

**Fix:**

- Tokens are one DPAPI-protected JSON blob, `tokens.bin` (store key `tokens`).
  Rotation is one atomic write. Legacy `access_token.bin`/`refresh_token.bin`/
  `id_token.bin` files are read once for upgrade compatibility and deleted on
  the next save.
- A future signing key (`kid`) that cannot verify the renewed ID token causes
  exactly one forced JWKS refresh. The token is accepted only if the refreshed
  key set verifies it.
- The renewed ID token must pass signature, issuer and audience validation
  before the rotated token set is saved.
- If the renewed ID token still cannot be verified, or secure storage fails
  after OpenAI has rotated the session, the client clears all tokens and marks
  the account `needs_reauth`. It never retries with the dead refresh token.
- `invalid_client` is handled explicitly: clear tokens, keep the registration
  record, and ask the user to disconnect and register again.
- A lock `OSError` becomes a `ProviderError` with a wait-and-retry action rather
  than leaking an OS error or hanging.
- Sign-in stores tokens before account metadata, so a token-persistence failure
  leaves no half-created account.

**Evidence:**

| Test | What it proves |
| --- | --- |
| `test_unknown_kid_refreshes_jwks_and_preserves_the_rotation` | New `kid` triggers one JWKS refresh; refresh happens once; tokens remain usable |
| `test_unverifiable_refreshed_id_token_clears_the_rotated_credentials` | Bad renewed ID token clears the rotated pair and requires sign-in; no reuse |
| `test_refresh_persistence_failure_requires_sign_in` | A save failure leaves no usable tokens and marks `needs_reauth` |
| `test_sign_in_token_persistence_failure_saves_no_account` | Partial sign-in metadata is not left behind |
| `test_invalid_client_refresh_requires_a_new_registration` | `invalid_client` gives registration guidance, not a network message |
| `test_process_lock_timeout_is_a_provider_error` | Lock contention becomes an actionable provider error |
| `test_reauthorization_identity_change_is_explicit` | A different verified subject cannot replace the selected account |
| `test_two_process_refresh_rotates_once_and_agrees` | Two real Windows processes, gated together, perform exactly one refresh and read the same stored token |

The two-process test uses the real Windows DPAPI store, a shared token file and
a gate file so both processes enter the refresh path together. The observed
refresh counter is `1`; both workers return the same access token, and the
stored token matches.

---

## 4. OAuth qualification results

Command:

```bat
python -m pytest -q -p no:cacheprovider tests/test_chatgpt_siwc.py
```

Result: **47 collected, 47 passed** (26.17 s).

| Group | Tests | What they prove |
| --- | --- | --- |
| Authorization request | 6 | Every documented parameter, PKCE S256, opaque host ID, exact loopback path and method, browser glue, reauthorization hints |
| Sign-in outcomes | 10 | Success; state mismatch; declined consent; missing issued client ID; nonce mismatch; bad signature; wrong issuer; wrong audience; expired token; missing plan scope |
| Refresh & revocation | 4 | Rotation persisted; one rotation under concurrency; unusable refresh clears tokens and requires sign-in; revoke sends the documented form and clears locally |
| Accounts | 1 | Two registrations stay separate; switching changes the token used |
| Responses API | 12 | Payload requirements; streaming aggregation; non-stream aggregation; cancellation; usage limit; ineligibility; unsupported capability; incomplete; missing completed; 401 without API-key fallback; model visibility filter; no-account refusal; redaction |
| Registry & profile | 2 | Defaults keep OpenAI/local unchanged and ChatGPT off; profile persistence contains no tokens |
| Account binding & cache (D1) | 2 | A→B→A and restart bind the selected account; old backends stay pinned; duplicate labels and bound identity are correct |
| Rotation & recovery (D2) | 10 | Unknown `kid`, bad refreshed ID token, persistence failures, `invalid_client`, lock timeout, 429/503 guidance, reauth identity, and a genuine two-process rotation |
| **Total** | **47** | |

Notable assertions:

- The authorization URL contains **no** `client_secret`, and its
  `redirect_uri` is `http://127.0.0.1:<port>/auth/callback`.
- Token storage is DPAPI ciphertext: neither the metadata JSON nor
  `tokens.bin` contains `at-`, `rt-` or a JWT prefix in plaintext.
- A 401 from the plan route produces "rejected the access token" with "will not
  fall back", and the request carries the OAuth bearer token — never an API key.
- Two concurrent expired-token requests produce **one** refresh call and the
  same access token for both callers.
- Two gated operating-system processes produce **one** refresh call and the
  same stored token.

---

## 5. Security results

| Control | Result |
| --- | --- |
| Credential storage | Per-account DPAPI files under `~/.giscode/chatgpt/accounts/<id>/`; one `tokens.bin` blob holds the token set; metadata holds no token. Non-Windows hosts report the store unavailable instead of writing plaintext. |
| Atomic rotation | The whole token set is written as one protected value with temp-file + replace, so a partial write cannot pair a new access token with an old refresh token. |
| Legacy migration | Per-field token files are read once and deleted after the next successful save; no plaintext fallback and no forced sign-in solely for the migration. |
| Token leakage in logs/errors | Provider errors are redacted with live access, refresh and ID token values (`test_token_values_are_redacted_from_error_text`). |
| Tokens in URLs | Never in a request URL. The authorize URL may carry `id_token_hint` as documented; GisCode does not log or display authorization URLs. |
| Browser credential extraction | None. No cookie, password, MFA code or session store is read; no website automation exists in this path. |
| Account isolation | Credentials and client IDs are keyed by verified subject + issued client ID; switching accounts cannot overwrite another registration. |
| Account binding | The provider's saved account id and the active pointer are written together; router caches are partitioned by account id; an in-flight backend stays pinned. |
| Identity binding on reauthorization | A callback with a different client ID or verified subject is rejected rather than replacing the selected registration. |
| Refresh race | In-process `RLock` plus a cross-process file lock; the replacement refresh token is persisted before use; one-rotation tests cover threads and two real processes. |
| Signing-key rotation | A failed signature triggers one forced JWKS refresh; no cryptographic check is relaxed. |
| Failed rotation recovery | If the server rotated but the replacement cannot be verified or stored, all tokens are cleared and `needs_reauth` is set; the old refresh token is not reused. |
| Revocation | Uses the discovered endpoint; clears local tokens even if the network fails, and reports "not confirmed" rather than claiming success. |
| Billing separation | The plan provider has no API-key code path. A plan refusal never becomes an API charge. |
| Lock contention | A failed lock becomes a bounded `ProviderError` with an action; no OS error escapes and no indefinite wait is added. |
| Secret redaction of request text | The shared remote-provider boundary already redacts credential-shaped text; the plan provider also exposes its live tokens to that boundary. |

---

## 6. Incomplete-answer and failure behaviour

| Situation | Behaviour |
| --- | --- |
| `response.failed` (usage limit) | Exact code preserved, action points to ChatGPT Usage, one request only |
| `response.failed` (not eligible) | Explains user/workspace/policy restriction, says not to repeat |
| Unsupported capability | Includes `error.param` (for example `temperature`) |
| `response.incomplete` | Error; partial text is not a finished reply |
| Stream ends without `response.completed` | Error |
| Cancellation | Reading stops, the socket closes, partial text is labelled incomplete |
| 401 / 403 before a stream | Mapped to sign-in-again or policy refusal; no fallback |
| 429 before a stream | Rate-limit guidance; keeps credentials; no automatic retry or paid fallback |
| 503 before a stream | Temporarily unavailable; keeps credentials; bounded retry is the user's decision |
| Invalid refresh / expired refresh | Tokens cleared, account marked `needs_reauth`, reauthorization requested |
| `invalid_client` | Tokens cleared; registration retained; user is told to disconnect and register again |
| Renewed ID token fails verification | Rotated tokens cleared; reauthorization required; dead refresh token never reused |
| Token persistence fails after rotation | Rotated tokens cleared; `needs_reauth`; reauthorization required |
| Sign-in token persistence fails | No account metadata is left behind; nothing is half-connected |
| Rotation lock held by another process | Actionable `ProviderError`; wait and retry |
| Reauthorization yields another subject | Explicit identity-mismatch error; the selected account is unchanged |

---

## 7. UI transparency

The Settings section shows: the connected account label, workspace when OpenAI
reports one (otherwise "not reported by OpenAI"), authentication status, whether
`chatgpt.tokens.use.direct` was granted, which billing applies to coding tasks
(ChatGPT plan / OpenAI API / local), a **Manage usage** link to ChatGPT Settings,
**Reauthorize**, **Disconnect**, and **Refresh status**. A plan-funded profile is
created only when the user selects it; connecting an account does not silently
change the active coding profile.

The picker appends a short account-id suffix when two registrations share a
label, and the panel separately states **Coding requests use:** the account
actually bound to the active coding provider. A picker change saves that binding
and clears the router cache before the status is refreshed.

---

## 8. Regression results

| Suite | Result |
| --- | --- |
| `tests/test_chatgpt_siwc.py` | **47 passed** (26.17 s) |
| Targeted router/provider/GUI/edition batch | **255 passed, 1 skipped** (65.30 s) |
| Browser + SIWC subset | **64 passed** (27.20 s) |
| Test range around the 20% full-suite boundary | **149 passed, 5 skipped** (37.58 s) |
| Complete Core suite | **1851 passed, 1 failed, 38 skipped** (1890 tests; 233.20 s across four chunks) |
| `py_compile` on changed modules | **exit 0** |

The complete suite was executed in four non-overlapping chunks generated from
the same sorted test-file list (127 files, each file once) because a single
invocation exceeds this environment's 295 s command cap. The same Python 3.13
interpreter, `pyproject.toml`, `-q -p no:cacheprovider` flags and test selection
were used; no test was deselected or skipped for this reason. Chunk totals:

| Chunk | Result |
| --- | --- |
| `aa` | 538 passed, 1 failed, 10 skipped (78.34 s) |
| `ab` | 297 passed, 13 skipped (54.83 s) |
| `ac` | 644 passed, 1 skipped (62.97 s) |
| `ad` | 372 passed, 14 skipped (37.06 s) |

The baseline before this change was 1839 passed, 1 failed, 38 skipped. The
complete suite now has 12 more passing tests, exactly the new SIWC cases, with
the same single pre-existing failure.

The single failure is pre-existing and unrelated:

```text
tests/fixtures/audit/fixture1/fixture1_project/tests/test_stub.py::test_stub_handler_identifiable
ModuleNotFoundError: No module named 'giscode.gisai'
```

The fixture imports the absent Pro/GisAI package. It was not skipped, weakened
or edited. No new failure appeared.

Interpreter note: the first `python` on `PATH` here is 3.11 without `tkinter`,
so Tk tests cannot collect under it. All results used Python 3.13
(`%LOCALAPPDATA%\Programs\Python\Python313\python.exe`).

---

## 9. Live round-trip evidence — none, and why

**No live sign-in or live inference was performed.**

- No ChatGPT account was authorized for this mission.
- The official flow requires the account holder to complete authentication and
  consent **in the browser**. GisCode must not and does not bypass that.
- No real access token, refresh token or ID token exists in this environment.
- No request was sent to `auth.openai.com` or `api.openai.com`.
- No API key was used as a substitute, and no ChatGPT website was automated.

To produce live evidence, a user with an eligible account must:

1. Open **Settings → ChatGPT plan → Continue with ChatGPT**.
2. Complete the browser sign-in and allow ChatGPT plan usage.
3. Confirm the account, workspace (if reported) and granted permission shown in
   Settings.
4. Select a listed model, save the coding profile, and run one small task.
5. Record the provider's own `response.completed` and usage, plus the account
   label — without copying any token into a report.

Until that happens, this package proves the protocol and the refusal paths, not
OpenAI's live behaviour.

---

## 10. Change inventory

New files (line counts at this revision):

| File | Lines |
| --- | --- |
| `giscode/providers/chatgpt_auth.py` | 1436 |
| `giscode/providers/chatgpt_plan.py` | 670 |
| `tests/test_chatgpt_siwc.py` | 1416 |
| `docs/CHATGPT_PLAN.md` | this revision |
| `docs/CHATGPT_SIWC_QUALIFICATION.md` | this report |

Modified by this mission (cumulative diff in this checkout; `main_window.py`
also contains earlier OpenAI settings work):

```text
 giscode/inference/router.py  |   67 +-
 giscode/gui/main_window.py   | 1282 +++++++++++++++++++++++++++++++++++++++-
 giscode/providers/base.py    |   44 +-
 giscode/providers/registry.py|   63 +-
```

Functional changes:

- `base.py`: new `CHATGPT` provider kind and default endpoint.
- `registry.py`: ChatGPT plan defaults (off, no API key), builder, account id.
- `chatgpt_auth.py`: OAuth flow, loopback listener, ID-token validation, one
  atomic DPAPI token blob, JWKS rotation retry, refresh/rotation locks,
  recovery from failed persistence, revocation, plan error mapping.
- `chatgpt_plan.py`: Responses API provider, account cache identity, account
  selection persistence, model discovery, streaming, cancellation, usage.
- `router.py`: account-partitioned backend cache and explicit invalidation.
- `main_window.py`: visible ChatGPT plan section, unique account labels,
  bound-account display, account selection and cache clearing.

---

## 11. Integrity

`docs/CHATGPT_SIWC_MANIFEST.sha256` contains SHA-256 digests for every file
added or changed by this mission, including `giscode/inference/router.py`.
Verify from the checkout root:

```bat
sha256sum -c docs/CHATGPT_SIWC_MANIFEST.sha256
```

No commit, push, release or publication was performed. Earlier mission changes
and evidence remain in place.

---

## 12. Remaining limitations

1. **No live qualification** — the primary limitation; see section 9.
2. **Windows DPAPI only** for protected storage; other platforms report the
   store unavailable rather than writing plaintext.
3. **No push notification for app disconnection** — discovered on the next
   failed request or refresh.
4. **Preview restrictions** — the plan route rejects tools/fields beyond text
   chat; GisCode does not attempt workarounds.
5. **Workspace may be unreported** — the UI says so instead of guessing.
6. **No dollar estimate** — plan usage is not API billing; only token counts
   are shown.
7. **Cancellation is client-side** — the server may finish generating a
   cancelled request, as with any HTTP stream.
8. **No Codex app-server integration** — this build calls the Responses API
   directly, as the documentation permits.
9. **A model must be chosen from the account's list**; no default is invented.
10. **Complete-suite execution was chunked** by the local 295 s command cap;
    every test file ran exactly once and the totals are reported above.
