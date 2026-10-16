# Native host operations v1

This is the product-side release engine for the existing private XU4 GitHub control path. After bootstrap, the existing protected orr-update entry delegates to this SAME engine; legacy deploy and API v1 do not maintain separate deployments. Pre-contract bootstrap still uses the old build path, with target-config checks and no blind data restore on return.
It does not start with the renderer, expose an HTTP endpoint, install a service
or change playback behavior. Importing it performs no device/network operation.

The protected host always chooses the repository, account, installation root,
service, sandbox and entry module. No request can choose a command, path, URL,
environment or import. The only product entry is:

```text
<verified-installed-release>/venv/bin/python -I -m fake_linkplay.host_operations
```

It receives <=1024 bytes of strict JSON on stdin and emits one bounded JSON
report. Never run it directly to test a live installation. The host independently
verifies selected and installed commits on de41tr/open-room-renderer main.

## Versioned contract

api_version=1, layout=xu4-native-v1. Product paths derive from the existing
normal user's home; the private XU4 boundary selects the approved existing
account and ALSA device.
The layout intentionally supports current -> releases/RELEASE, external native
config and shared state/run. Unit validation checks effective service identity
and a named semantic template, rather than a release path or unit-file hash.
Unknown receipt formats, directives, process/IPC owners and layout variants
refuse. A layout-contract change still requires review.

Operations:
- status/capabilities: report the actual installation and supported operations.
- release-prepare: build the explicitly selected main SHA in an isolated native
  release using scripts/install-native.py. Validate current config and component
  compatibility. Do not switch current, alter shared data or touch the service.
- release-install: install a previously prepared SHA by changing current/previous
  and controlling the one existing user service. No automatic push deployment.
- release-rollback: only select the actual previous release with matching SHA.
  Check the immediately current config using the target's real config checker
  **before stopping**. Never copy back old config/state or delete unknown keys.
- component-prepare/component-install: preparation only. Currently spotify-eq:
  existing official pinned client installer + fixed private EQ setup. No receiver
  start, config/unit change, login or audio action.
- component-activate/component-deactivate: separate authorization and service
  transaction. Use only the driver's intended startup key in the current config.
  EQ disable removes the key entirely, retaining later changes and private data.
- diagnose-start/diagnose-collect and test are fixed host routes. The product
  entry deliberately refuses them; they never run in the production-user context.

The metadata fake_linkplay/host_components.json contains only versioned data:
known profile, product-code driver, contract version, existing-user-v1 rights
class and binding contract. Unknown/extra fields and higher rights refuse.
A new component requires a reviewed data entry and fixed product-code driver;
metadata cannot name a module, command, download source or root instruction.
Within this existing rights contract, ordinary product releases and new drivers
need no new sudo, SSH or protected-dispatch code.

Spotify EQ reuses the established driver and private run.lock checks. Idle
run.lock is retained, never created/removed/replaced by validation. Busy/unsafe
locks refuse. Client-pin binding is checked against the target during compatible
updates. The exclusive/direct receiver still requires its separately reviewed
stopped rebind contract; it is never silently forgotten or migrated.
No package manager, added privileges, new credentials or device access is
implied by a descriptor.

## Results and failures

Reports separate accepted, prepared, installed, activated, service_running,
checks_passed and audio_acceptance=not_performed. Include requested/installed/
running commits, API/helper/component versions, request ID, UTC start/end,
phase, dependency booleans, fixed required steps and fixed error codes.
Backend verification uses existing owned-process/SO_PEERCRED/volume readback;
it sends no play/load/set action and proves no audible output.

Preparation keeps the running identity/config/state/unit unchanged. Failed
staging removes only its unique unactivated output. A release switch retains
previous and private recovery metadata. There is no blind auto-restore of old
configuration/state and no automatic retry after a partial/uncertain start.
A failure after stop can leave the service stopped or current changed; report it.

The host adds trigger head_sha/run ID, bounded private logs and the existing
seven-day / 20 MiB artifact. Network diagnostic limits remain 120 seconds,
5,000 events / 10 MiB, existing CPU/RAM limits and retention.
No raw exception, URL/query, media title, token, cookie or packet is a report field.

## Focused verification

scripts/check-host-contract.py has only environment and contract profiles.
They are offline and use no production configuration, service bus, socket or
audio device. The private adapter's targeted regression script covers real
private fixture files/flocks, simulated service lifecycle/release switches and
the existing uploader with mocked HTTP plus an inspected synthetic failure ZIP.
No full/smoke/audio test is required by this change.

PR code stays in ordinary disposable CI until a host isolation boundary has been
separately approved and actually verified. Never execute a PR as malte or inside
the existing credential-bearing runner. A trusted main-only hardware-test
candidate has network/devices/home/service-bus access disabled, but needs its
separate protected-host rollout/namespace acceptance before use.

Bootstrap order: review/merge product and private integration; separately select
the merged public-main deployment using the existing route; separately approve
the exact protected-host rollout. None of these approvals follows from a PR.
The private README/AGENTS/STATUS are the operational authority.

## v1.1 maintenance clarification

The host can install its broker and source/Spotify diagnostic profiles without
deploying or restarting the current renderer. A pre-contract renderer remains
reported as product_contract_available=false. Existing explicit main deployment
is the one-time product bootstrap; it still requires a separately authorized
restart/deployment, which this infrastructure task does not perform.
After that ordinary compatible releases use the same product release engine
without changing sudo, SSH or protected commit hashes.

The fixed host-only result operation adds target_request_id and reads the
durable result without executing it again. Unknown/partial outcomes block new
management and legacy mutations pending explicit review; diagnostics and result
collection remain available. Source diagnostics use the existing leased buffer;
network, renderer and spotify-events are profiles of the same Start/Collect path.

Complete backend reports include renderer_pid, mpv_pid and optional spotify_pid.
The private broker accepts spotify_pid only as a positive bounded integer;
unknown fields and other invalid types still refuse. Component outcomes retain
these backend fields too. Status may verify owned IPC with a volume readback,
but does not play media, authenticate or establish heard audio.
