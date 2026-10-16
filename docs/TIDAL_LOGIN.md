# Operator-run TIDAL login (own public client)

This is an implementation-only login boundary. It has been tested with synthetic
responses, not a live client or account. It does **not** enable TIDAL playback,
TIDAL Connect, renderer startup integration, or a ready-to-use XU4 login.

## Before running

- Independently register your own **public client** with TIDAL and confirm that
  the intended use and requested scopes are permitted for that client. This
  project supplies no client ID, manufacturer identity, secret, or scopes.
- Register an exact callback such as `http://127.0.0.1:8765/tidal/callback` for
  your app. Use the same URI in the command. This CLI accepts only numeric IPv4
  loopback, an explicit port from 1024 through 65535, and a simple path without
  query parameters or a fragment. Provider acceptance of your registration is
  a separate prerequisite; do not assume all clients can register this URI.
- Run the CLI on the **same Linux host as the browser** that completes login.
  The receiver binds only to `127.0.0.1`. Opening the URL on a laptop while the
  CLI runs on a cloud machine, remote shell host, or XU4 sends the callback to
  the wrong computer. Run the CLI on the browser host instead. There is no
  cloud callback, hosted relay, or callback-URL/code paste mode.
- Use an absolute, dedicated private session directory outside the repository,
  renderer configuration, favorites, release directories, and synced folders.
  Its parent directories must already exist and be safe. The final directory
  is created as `0700`; an existing directory must already be owned by you and
  exactly `0700`. Unsafe files, symlinks, and unsafe ancestors are rejected.
  Credentials are **plaintext at rest**, protected by ownership and file modes.
- Install the project's normal dependencies in your local Python 3.11+ setup.
  The account-field bridge uses the base `pyaes` dependency; no separate native
  cryptography extra is needed.

## Run locally

From the checkout, with its normal dependencies installed:

```sh
python -m fake_linkplay.tidal_auth \
  --client-id 'YOUR_OWN_PUBLIC_CLIENT_ID' \
  --redirect-uri 'http://127.0.0.1:8765/tidal/callback' \
  --scope 'SCOPES_EXPLICITLY_PERMITTED_FOR_YOUR_CLIENT' \
  --session-dir '/absolute/private/location/tidal-session'
```

Replace every placeholder, including the scope string. There is no default or
suggested scope: use your own developer-app permissions and the relevant API
reference. Do not pass a client secret, password, access token, or refresh token
on the command line. Sign in and review consent only in TIDAL's browser page.

The default total deadline is 180 seconds; `--timeout` accepts 1–600 seconds.
An occupied registered port is an error, not a reason to silently change the
redirect. Free that port or register and select another exact URI. Ctrl-C
cancels the attempt. The listener closes after the authorization response.

By default the CLI opens your local browser. If the browser launcher is not
available, `--no-browser` prints the authorization URL for manual opening on
that **same host**. It does not enable remote callbacks. The authorization URL
contains public-client parameters, a random state, and an S256 challenge. Keep
it private while the attempt is pending. The verifier stays in process memory.
The standard provider redirect necessarily returns a one-time code in the
loopback query; the callback page removes that query from the visible history
when JavaScript is enabled, contains no external content, and does not log or
echo it. Do not share callback URLs, browser screenshots, or HTTP debug traces.

## OAuth success versus account adoption

The documented standard OAuth token response does not promise account identity.
The existing account bridge needs both a numeric user ID and a two-letter
country code. The CLI can adopt these from a complete `user.userId` /
`user.countryCode` token-response extension, when the provider supplies one.
It does not invent identity, infer it from a token, or ask you to type it.

If identity is missing, the default result is **OAuth succeeded, identity
incomplete**, with exit code 2 and **no session write**. A successful OAuth
grant alone does not establish access to TIDAL's v1 APIs or full playback.

Only if your independently registered client is **separately permitted** to
use `https://api.tidal.com/v1/sessions`, add `--probe-v1-session`. That explicit
opt-in always performs one identity request, even when the token response
already has identity. Any identity fields supplied by the token response must match the probe. Public OAuth scopes are not evidence of that permission;
this endpoint is not presented here as a generally available public API.
A denied, invalid, incomplete, or conflicting probe leaves existing storage unchanged and
reports OAuth success separately from the missing identity/access proof. An
accepted identity response proves only that request, never playback rights.
No stream or protected-playback request is made by this CLI.

Only a complete, unexpired session is written. `TidalSessionStore` writes
`session.json` atomically with mode `0600`, preserving the existing session
until a complete replacement is ready. Login, callback, token-validation, or
identity failures do not replace it. A local filesystem error is reported
separately; check storage before retrying. The stored schema contains
`access_token`, optional `refresh_token`, `expires_at`, `user_id`, and `country_code`
(with the store's schema version). A missing refresh token is stored as null; an old refresh token is never reused.
There is no automatic token refresh.

The login owns a nonblocking exclusive lease on the private session directory
until it finishes or is cancelled. A concurrent login, save, or local logout
fails explicitly as busy instead of silently changing the pending account.
Cancel the pending login before requesting local logout. Read-only session
checks remain possible. No lock file or persistent service is installed.

Exit status:

- `0`: a complete OAuth session was saved on this host; playback remains unproven
- `1`: configuration, callback, authorization, or token exchange failed
- `2`: invalid CLI arguments, or OAuth succeeded but identity/adoption did not
- `130`: interrupted; no completed adoption was confirmed

## Browser host versus XU4

The saved session stays on the browser host. It is **not** automatically placed
on the XU4, and the renderer does not automatically load or advertise it.
Client identity and the registered redirect must also match the separately
configured account bridge. Runtime wiring, renewal, legitimate API access,
full-playback acceptance, and real controller/device behavior remain open.

If a separately authorized deployment needs this session on another machine,
the operator must explicitly manage that private transfer over their existing
trusted SSH/SCP connection, then check ownership and `0700` directory / `0600`
file modes on the destination. This CLI implements no transfer. Do not send
session files or credentials through Git, GitHub issues/PRs, chat, shared
artifacts, public links, or a host-bridge secret relay. Account-field encoding
is not encryption for storage or transport.

## Protocol and security boundaries

The implementation follows TIDAL's documented public-client authorization-code
flow: `https://login.tidal.com/authorize` with S256 PKCE and random state, then a
form POST to `https://auth.tidal.com/v1/oauth2/token` using the same redirect and
the in-memory verifier. No client secret is used. The back-channel endpoints
are fixed, HTTPS verification remains enabled, redirects are rejected, and
proxy/netrc environment configuration is ignored. Individual HTTP requests
have a 15-second total timeout and a 64 KiB response limit (compressed responses are not decoded) within the
overall login deadline. Provider response bodies and request secrets are never
included in terminal diagnostics. Tests replace HTTP, listener, and browser
boundaries; they do not contact TIDAL or authenticate a real account.

Official references (reviewed 2026-10-16):

- [TIDAL authorization](https://developer.tidal.com/documentation/api-sdk/api-sdk-authorization)
- [TIDAL API reference and scope requirements](https://developer.tidal.com/reference/web-api)

See [the account interoperability status](TIDAL_PROVIDER_STATUS.md) for the
separate, non-advertised renderer boundary and remaining playback work.
