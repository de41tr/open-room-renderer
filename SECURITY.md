# Security Policy

Open Room Renderer is an experimental local-network audio renderer. Security and privacy reports are welcome, especially when they help prevent accidental exposure of local identifiers or credentials.

## Reporting

Do not open public issues containing secrets, tokens, cookies, private dumps, raw protocol captures, real device identifiers, MAC addresses, serial numbers, private IP addresses, keys, certificates, or credentials.

For now, report security/privacy issues privately to the maintainer through the repository owner's preferred private contact channel. If no private channel is published yet, open a public issue with only a high-level sanitized description and ask for a private contact path.

## In Scope

- Logs exposing tokens, cookies, credentials, private IPs, MAC addresses, UUIDs, or serial numbers.
- Unsafe defaults that publish sensitive local data.
- Accidental inclusion of private state, captures, or credentials in examples.
- Bugs that allow remote control beyond the intended local-network renderer surface.

## Out Of Scope

- Requests to bypass DRM, authentication, pairing, signatures, encryption, certificates, access controls, or technical protection measures.
- Requests for proprietary app internals, firmware fragments, copied assets, or raw dumps.
- Compatibility requests that require credentials or protected-service access without a legitimate user-controlled path.

## Public Logs

Raw logs are not safe by default. The project redacts common sensitive values, but redaction is best effort. Review logs manually before sharing.

## Local callback and media-proxy boundaries

This is an unauthenticated local-network renderer. Do not expose its control
ports to the Internet. A renderer firewall does not control a phone/app's own
network traffic.

GENA callbacks must be one HTTP URL with a literal IP matching the request's
transport peer. Forwarded headers, arbitrary DNS names, other hosts, credentials,
fragments, metadata/link-local addresses and redirects are refused. Legitimate
same-peer LAN callbacks are allowed; loopback callbacks require a loopback peer.
Leases are 30–1800 seconds, capped at 64 total and 8 per peer, with at most 8
concurrent deliveries. Transient failures retry with backoff within the lease;
410/412 reject the lease. A retry keeps its payload/sequence, then coalesced new
state receives the next sequence. Expiry/unsubscribe/shutdown cancel work.

The audio proxy is disabled unless an explicit proxy/seek option is enabled.
It serves only the local loopback backend and currently selected media, never
arbitrary caller URLs. Every redirect's DNS answers are checked and pinned;
loopback, link-local, metadata, multicast, reserved and unapproved private
addresses are refused. `proxy_allowed_lan_hosts` may name exact trusted LAN media
hosts, with no wildcards; it cannot allow forbidden address classes. Client
cookies/authorization and environment proxy settings are never forwarded.

There are at most four proxy streams, a 20-second setup deadline, bounded
redirects and socket timeouts. Proxy mode needs the HTTP listener to accept
loopback connections (the default wildcard bind does). Tests use only synthetic
addresses and local fixtures; no cloud metadata or internal-admin endpoints
are contacted by the test suite.

## Local browser controller boundary

New `/orr/v1/web/*` controls add strict admitted Host/listener-port and Origin
checks, custom-header CSRF, bounded typed JSON and safe state projection. Static
assets are an exact local package map with CSP, no-referrer, no-store and nosniff.
Media/backend URLs, raw protocol metadata and provider account fields are not
part of browser state. Known URL-derived title fallbacks and credential-bearing
artwork query/userinfo/fragment forms are withheld; this is not general DLP.

These protections apply to the new browser routes only. Legacy unauthenticated
HTTP GET mutations, SOAP and favorites routes remain reachable and can still
permit cross-site requests/control on an untrusted network. No whole-service
authentication or Internet-safe deployment claim is made. Use a trusted isolated
LAN and do not expose the listener publicly. See [Web security scope](docs/WEB_UI.md#browser-security-and-privacy-scope).
