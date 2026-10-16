# TIDAL account interoperability milestone

This is an explicit, non-advertised integration boundary, not working TIDAL
playback or TIDAL Connect support. Existing renderer startup does not create a
TIDAL adapter, contact TIDAL, or expose an account.

Implemented and tested with synthetic data:

- Optional `TidalAccountBridge` injection into `HttpApi`: SOAP
  `GetUserInfo(AccountSource=Tidal, Version=2)` projects a complete, unexpired
  independently authorized session using the advertised status UUID.
- No manufacturer client fallback. The caller must supply its own authorized
  client identity and registered redirect. Refresh tokens remain private.
- Separate session storage with explicit private-directory selection, bounded
  strict JSON, atomic writes and ownership/no-follow checks. The caller must
  keep it outside release, renderer configuration and favorites directories.
- Standard AES-based account-field compatibility encoding, implemented with
  optional `cryptography` (`pip install '.[tidal]'`). This format uses a public
  identifier and provides **no secrecy or authentication**. It must not be used
  for at-rest protection. LAN delivery needs an explicitly authorized trust
  boundary; account request/response payloads are omitted from detail logs.
- A pure bounded TIDAL queue-selection parser: catalogue references/track IDs
  remain unresolved selections, never fake playable URLs or backend input.

Not implemented or established by this milestone:

- An authorized independent client, provider API or full-playback entitlement.
- Login/token exchange, refresh, stream resolution, protected-media playback,
  active service advertisement, deployment or real WiiM Home acceptance.
- TIDAL `UserLogin` remains unsupported. The observed app flow does not itself
  establish public-client PKCE compatibility. An independently authorized
  external login and account adoption is a separate possible integration step.
- Native ARMv7 availability of the optional cryptography package. Default
  installations do not gain that dependency. The Python 3.11 CI lane installs
  the extra to test actual synthetic codec vectors.

A live release requires the legitimate account/client path, capability and
playback acceptance, token/URL renewal, controller behavior and target-device
checks. A saved token, passing mock tests or matching account-field vectors
is not evidence of those outcomes.
