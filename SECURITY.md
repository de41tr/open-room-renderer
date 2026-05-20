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
