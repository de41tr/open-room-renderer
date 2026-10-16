# TIDAL account interoperability milestone

This is an explicit, non-advertised integration boundary, not working TIDAL
playback or TIDAL Connect support. Existing renderer startup creates only inactive login/account boundaries; it
does not contact TIDAL or expose an account.

Implemented and tested with synthetic data:

- Optional `TidalAccountBridge` injection into `HttpApi`: SOAP
  `GetUserInfo(AccountSource=Tidal, Version=2)` projects a complete, unexpired
  independently authorized session using the advertised status UUID.
- Own-client projection requires an explicit client identity and registered
  redirect. Device-flow projection instead binds the actual library client
  and omits an unproven callback; see [explicit LAN sharing](TIDAL_DEVICE_LOGIN_EXPERIMENT.md#explicit-lan-account-sharing). Refresh tokens remain private.
- Separate session storage with explicit private-directory selection, bounded
  strict JSON, atomic writes and ownership/no-follow checks. The caller must
  keep it outside release, renderer configuration and favorites directories.
- Standard AES-based account-field compatibility encoding, implemented with
  the pinned `pyaes` dependency already used by the regular TIDAL library. This format uses a public
  identifier and provides **no secrecy or authentication**. It must not be used
  for at-rest protection. LAN delivery needs an explicitly authorized trust
  boundary; account request/response payloads are omitted from detail logs.
- A pure bounded TIDAL queue-selection parser: catalogue references/track IDs
  remain unresolved selections, never fake playable URLs or backend input.

Not implemented or established by this milestone:

- An authorized independent client, provider API or full-playback entitlement.
- Automatic login/token refresh, stream resolution, protected-media playback,
  active service advertisement, deployment or real WiiM Home acceptance.
- TIDAL `UserLogin` remains unsupported. The observed app flow does not itself
  establish public-client PKCE compatibility. An independently authorized
  external login remains separate from native app code-login support.
- Target installation of the runtime dependencies. The codec now uses pure-
  Python `pyaes`, rather than requiring a separate native cryptography extra.

A live release requires the legitimate account/client path, capability and
playback acceptance, token/URL renewal, controller behavior and target-device
checks. A saved token, passing mock tests or matching account-field vectors
is not evidence of those outcomes.

An operator-run own-client PKCE CLI is now available for a browser on the same
Linux host: see [the login guide](TIDAL_LOGIN.md). Its offline tests do not
establish live client acceptance. Optional permitted v1 identity probing and
local storage remain separate from full-playback rights and XU4 transfer.
