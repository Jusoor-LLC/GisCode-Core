# ChatGPT plan usage (Sign in with ChatGPT)

GisCode Core can use an eligible **ChatGPT plan** for coding inference through
OpenAI's official *Sign in with ChatGPT* flow for open-source applications. It
is a different product from the **OpenAI API key** integration:

| | ChatGPT plan | OpenAI API key |
| --- | --- | --- |
| Credential | OAuth tokens from Sign in with ChatGPT | `sk-...` API key |
| Who pays | Your ChatGPT plan / credits | Your OpenAI API account, per token |
| Endpoint | `POST https://api.openai.com/v1/responses` | `POST https://api.openai.com/v1/chat/completions` |
| Setup | **Settings → ChatGPT plan → Continue with ChatGPT** | **Settings → OpenAI API** |
| Enabled by default | No | No |

Both are optional and off until you act. GisCode never converts one into the
other: a plan failure does **not** fall back to paid API billing, and an API
key is never used as a ChatGPT plan credential.

Official reference followed:
<https://developers.openai.com/siwc/token-sharing-open-source>.

---

## 1. What the sign-in does

1. Creates a stable, opaque host identifier (`urn:uuid:...`) once per
   installation and reuses it. It is not an email, a user id or a credential.
2. Starts a loopback listener on `127.0.0.1` at `/auth/callback`.
3. Opens the **system browser** at
   `https://auth.openai.com/api/accounts/authorize` with:
   `client_id=dynamic_agent_client` (first registration),
   `agent_name_hint=GisCode`, `ext_agent_host_id`, `response_type=code`,
   `redirect_uri`, `scope`, `resource=https://api.openai.com/v1`, fresh
   `state`, fresh `nonce`, and `S256` PKCE.
4. Receives the callback with `code`, `state` and the issued `client_id`,
   validates `state`, and rejects a mismatched or declined result.
5. Exchanges the code at `https://auth.openai.com/api/accounts/oauth/token`
   with the issued client ID, PKCE verifier, the exact redirect URI and the
   resource. No client secret is involved.
6. Validates the ID token: RS256 signature against OpenAI's JWKS, issuer
   (`https://auth.openai.com`), audience (the issued client ID), expiry,
   not-before, and the nonce from this attempt.
7. Checks the **granted scopes** for `chatgpt.tokens.use.direct`. A valid ID
   token alone is not permission to use the plan.
8. Stores the verified identity, issued client ID, tokens, scopes and expiry in
   protected storage (section 4), and makes the account active.

A declined consent, an invalid state, a bad nonce, a bad signature, a wrong
issuer/audience or an expired token all stop the flow **before** anything is
saved. The specific messages are listed in section 7.

### Returning sign-in

Reauthorization reuses the saved issued client ID, omits `agent_name_hint`, and
sends the retained ID token as `id_token_hint` plus the saved email as
`login_hint`. The new ID token is validated again, and its subject must map to
the same account before credentials are replaced.

### Multiple accounts

Each registration keeps its own issued client ID, tokens and metadata, keyed by
the verified subject and client ID. Two accounts with the same email are still
two registrations. Switching accounts changes which token is used; it never
merges or overwrites another registration. Selecting an account updates the
active-account pointer and the provider's saved `account_id` together. Router
and backend caches are partitioned by that account id, and a request already in
flight keeps the backend it started with; the next request authenticates as the
newly selected account. **Disconnect** revokes the renewable session (when
reachable) and removes the local tokens, keeping the registration for a later
sign-in.

---

## 2. Inference

- Model discovery: `GET https://api.openai.com/v1/models` with the OAuth access
  token. Only entries with `visibility: "list"` are offered; `display_name` is
  shown and `slug` is sent as `model`.
- Requests: `POST https://api.openai.com/v1/responses` with
  `"store": false` and `"stream": true` on every call.
- The preview rejects `temperature`, `max_output_tokens`,
  `previous_response_id`, `metadata`, `top_p`, `truncation` and similar fields.
  GisCode omits them all; `max_tokens`/`temperature` exist only in the internal
  interface.
- System text travels in `instructions`; an explicit
  `{"type": "message", "role": "system"}` item is never sent because the plan
  route rejects it.
- Streaming events used: `response.output_text.delta` for text,
  `response.completed` as the **only** success signal, `response.failed` and
  `response.incomplete` as errors. A stream that ends without
  `response.completed` is an error, not an empty answer.
- Cancellation stops reading and closes the connection; partial text is
  labelled incomplete.
- Attribution: responses carry provider `chatgpt`, the selected model and the
  caller's request id; the usage ledger records the same id with plan token
  counts (never a dollar estimate, because plan usage is not API billing).

---

## 3. Usage limits, eligibility and errors

GisCode surfaces OpenAI's exact code and gives one recovery step. It never
retries automatically into another billing path.

| Code | Meaning | GisCode's response |
| --- | --- | --- |
| `subscription_sharing_user_not_eligible` | Plan usage unavailable for this user/workspace/policy | Explains the restriction; tells you to use an eligible account or the separate API key; does not repeat the request. |
| `subscription_sharing_usage_limit_exceeded` | Plan/app usage limit reached | Points to ChatGPT Settings → Usage; no automatic retry; no paid fallback. |
| `subscription_sharing_usage_unavailable` | Availability check failed | Keep credentials, retry later. |
| `subscription_sharing_unsupported_capability` | Unsupported input/tool/model/override | Reports the `param`; the request is not repeated. |
| `subscription_sharing_route_not_supported` | Wrong endpoint/method | Points at `POST /v1/responses`. |
| `subscription_sharing_invalid_user` | Subscriber context invalid | Ask for a new sign-in. |
| `chatpass_v2_scope_not_authorized` / `..._invalid_authorization_context` | Granted context does not authorize the operation | Reauthorize and grant plan usage. |
| `subscription_sharing_user_unavailable` | Account/workspace temporarily unavailable | Retry later. |
| HTTP 401 before a stream | Signed identity/direct permission not accepted | Report clearly; refuse; never fall back to an API key. |
| HTTP 403 before a stream | Policy/permission admission failure (for example region) | Report the restriction. |
| HTTP 429 before a stream | Plan/app requests are rate limited | Keep credentials; points to ChatGPT Settings → Usage; no automatic retry or paid fallback. |
| HTTP 503 before a stream | Direct routing unavailable | Keep credentials; bounded retry is the user's decision. |

For ChatGPT Plus, the five-hour limit is shared across apps that use your
ChatGPT plan; no app gets a separate allowance. Pro accounts do not have that
five-hour limit. OpenAI does **not** notify GisCode when you disconnect the app
in ChatGPT settings; GisCode learns it from a failed request or refresh and then
asks you to sign in again.

---

## 4. Credential storage

```
%USERPROFILE%\.giscode\chatgpt\
  host.json                     ext_agent_host_id (opaque, not a credential)
  active.json                   selected account id
  accounts\<account-id>\
    metadata.json               email, subject, client id, host id, scopes,
                                expiry, plan permission, reauth flag
    tokens.bin                  one DPAPI-protected JSON blob: access,
                                refresh and ID tokens together
    refresh.lock                rotation lock
```

- Metadata contains **no token values** and no password.
- Tokens are encrypted with Windows DPAPI through the same store the API-key
  integration uses. A copied file is not readable by another Windows account.
- The token blob is written atomically (temporary file, then replace) after a
  successful refresh, so a crash cannot leave a new access token beside the
  previous refresh token. Older per-field token files are read once for
  upgrade compatibility and removed on the next save; no reauthorization is
  needed for that migration.
- On a platform with no protected store, the account store reports unavailable
  instead of writing plaintext.
- Tokens never appear in `config.json`, logs, usage records or error text.
  Provider errors are redacted with the live token values.
- The authorization URL can contain `id_token_hint`; GisCode does not log or
  display authorization URLs. As the documentation requires, treat such a URL
  as sensitive if you copy it.

### Refresh and rotation

- Access tokens last 1 hour; refresh tokens last 30 days and rotate on every
  refresh.
- Refresh happens within 120 s of expiry, under an in-process lock **and** a
  file lock, so two threads or two processes cannot race a rotating token.
- The replacement refresh token is persisted before the new access token is
  used. A renewed ID token must pass signature, issuer and audience validation
  before any token is saved; a cached JWKS is refreshed once when a new signing
  key (`kid`) appears. If verification or secure storage still fails, the
  already-rotated tokens are cleared and the account is marked `needs_reauth`;
  the dead refresh token is never reused.
- `invalid_client` means the app registration itself is no longer recognized.
  GisCode clears the tokens, keeps the account record, and asks you to
  disconnect and register again; it is not presented as a network error.
- `invalid_grant`, `invalid_refresh_token`, `token_expired`,
  `refresh_token_expired`, `refresh_token_invalidated` and
  `refresh_token_reused` clear the unusable tokens, mark the account as needing
  a new sign-in, and tell you to authorize again with the saved client ID.

### Disconnect

**Settings → ChatGPT plan → Disconnect**:

1. POSTs the refresh token to the discovered `revocation_endpoint` with
   `token_type_hint=refresh_token` and the issued client ID.
2. Retries a network/5xx failure a bounded number of times.
3. Clears local tokens in every case and keeps the registration.
4. Tells you whether remote revocation was confirmed. If it was not, you can
   also disconnect the app in ChatGPT Settings.

---

## 5. What GisCode does not do

- It does not read cookies, passwords, MFA codes, browser sessions or ChatGPT
  conversation state.
- It does not automate or scrape the ChatGPT website.
- It does not use `backend-api` endpoints; only the documented public API.
- It does not store tokens in browser storage, source control, logs or usage
  records.
- It does not treat a ChatGPT subscription as API access, or an API key as plan
  access.
- It does not silently bill the API when plan usage fails.
- It does not send unsupported fields (temperature, tools, previous response
  ids) to the plan route.
- It does not show a workspace, email or eligibility it did not receive from a
  validated token.

---

## 6. What the UI shows

**Settings → ChatGPT plan (Sign in with ChatGPT)**:

- **Continue with ChatGPT** (the documented action label).
- Account picker with a stable label per registration; duplicate labels get a
  short account-id suffix so every choice is distinct.
- **Coding requests use:** the account actually bound to the active coding
  provider, which may differ from a picker selection until it is applied.
- Workspace, when OpenAI reports one; otherwise "not reported by OpenAI".
- Authentication status: signed in, needs a new sign-in, or not connected.
- Whether `chatgpt.tokens.use.direct` was granted.
- Which billing applies to coding tasks: ChatGPT plan, OpenAI API usage, or a
  local model.
- **Manage usage** → ChatGPT Settings → Usage.
- **Reauthorize**, **Disconnect**, **Refresh status**.
- Model id and **Use ChatGPT plan for coding tasks**, which creates a normal
  `coding` profile pointing at provider `chatgpt`.

Errors from inference appear through the same task/chat error surface as other
providers, with OpenAI's code and the recovery step.

---

## 7. Troubleshooting

| Message | Meaning | Fix |
| --- | --- | --- |
| `ChatGPT sign-in was declined or plan usage was not granted.` | Consent declined or plan permission withheld. Nothing saved. | Try again and allow plan usage, or use the OpenAI API key option. |
| `The ChatGPT sign-in callback did not match this request.` | State mismatch (stale or tampered callback). | Start the sign-in again. |
| `The ChatGPT ID token nonce does not match...` | Token is not from this attempt. | Start again; nothing was accepted. |
| `The ChatGPT ID token signature could not be verified.` | Wrong signing key or tampered token. | Start again. |
| `... has expired` / `issued by an unexpected issuer` / `issued for a different client` | Claim validation failed. | Check the system clock, then sign in again. |
| `ChatGPT plan usage is not enabled for this account.` | Signed in without the plan scope. | Reauthorize and grant plan usage, or use the API key provider. |
| `The ChatGPT session can no longer be renewed (invalid_grant).` | Refresh token was revoked, expired or already rotated. | Reauthorize with the saved client ID. |
| `ChatGPT no longer recognizes this app registration (invalid_client).` | The issued client registration is invalid. | Disconnect, then **Continue with ChatGPT** to register again. |
| `The renewed ChatGPT credential could not be verified.` / `could not be stored securely.` | OpenAI rotated the session but the replacement could not be trusted or saved. | Reauthorize; the previous refresh token is already dead. |
| `Another GisCode process is refreshing this ChatGPT session.` | The cross-process rotation lock is held. | Wait for the other GisCode process, then retry. |
| `This sign-in is for a different ChatGPT account...` | Reauthorization returned a different verified subject. | Choose the matching account, or disconnect the registration before signing in again. |
| `You have reached your ChatGPT plan usage limit` | 429 plan/app limit. | Open ChatGPT Settings → Usage; no automatic retry. |
| `No protected credential store is available...` | Non-Windows host without DPAPI. | Use the API key provider, or sign in on Windows. |
| `Sign in with ChatGPT was not completed in the browser.` | Callback never arrived. | Retry; allow the loopback port through any local firewall. |

---

## 8. Honest limits

- **Preview behaviour.** The plan route is a preview with the documented field
  and tool restrictions. GisCode follows them; it does not guess alternatives.
- **Eligibility is OpenAI's decision.** Free-plan users are not promised
  plan-funded inference; OpenAI's error is shown verbatim.
- **Usage is not money.** GisCode reports plan tokens, not a price, and links to
  ChatGPT Settings for limits.
- **Windows DPAPI.** The protected store is Windows-specific; other platforms
  report it unavailable rather than writing plaintext.
- **No push notification for disconnection.** A revoked app is discovered by a
  failed request or refresh.
- **No live qualification in this build.** All OAuth, token, refresh, revocation
  and streaming tests use a deterministic loopback server. A real account
  round trip requires you to complete the official browser consent.

---

## 9. Related reading

- [`CHATGPT_SIWC_QUALIFICATION.md`](CHATGPT_SIWC_QUALIFICATION.md) — test
  evidence and exact limitations.
- [`OPENAI_DIRECT.md`](OPENAI_DIRECT.md) — the separate API-key route.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — provider gateway.
- [`LOCAL_AI_SETUP.md`](LOCAL_AI_SETUP.md) — local models.
