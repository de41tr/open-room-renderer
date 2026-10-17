# Experimental standard-library device login

This separate experiment uses the regular `tidalapi==0.8.11` package from PyPI
(the upstream project is EbbLabs/python-tidal). It does not extract device
firmware credentials or copy the library's client credentials into ORR source.
Provider approval for this integration and API availability remain uncertain.
No account or full-playback acceptance is established by offline tests.

## User action on the renderer

After a tested installation, open the renderer's existing web interface and
choose **Experimental TIDAL device login** (`/ui/tidal`). Read the limitations,
check the explicit confirmation box, and choose **Start device login**.
Only then does the renderer contact TIDAL. Follow the displayed official
verification link and complete the account login/consent yourself in TIDAL's
page. The renderer never asks for a password or a token in its UI.

The existing interface trusts the local network: Host, Origin and CSRF checks
protect browser admission, but do **not** authenticate other LAN users. No new
listener, public relay or remote execution operation is introduced. Use this
only on the trusted network already accepted for renderer control.

Only the official verification URI and the human-facing one-time `user_code`
are shown. The internal polling `device_code`, access token, refresh token and
account identity are never returned by this web API. Do not share login links,
codes or screenshots. The browser status does not attest full playback.

A successful complete session is stored in the current renderer user's
`~/.local/share/open-room-renderer/components/tidal/session.json`, independently
of release directories and renderer configuration. The existing components
parent must exist. The final directory is private (0700), and the token file
is private (0600), atomically replaced under exclusive directory ownership.
Tokens are plaintext on disk, protected by ownership/modes. No token enters a
GitHub job, diagnostic artifact or chat. An existing saved session is rejected,
never silently replaced by another login.

Cancel prevents local adoption. The upstream library's already-running polling
thread may continue until it completes or expires; the operation remains active
while draining, and its lease is retained. Cancel is not provider revocation.
Close and restart behavior must not be represented as provider logout.

## Explicit limits

Login itself does not start audio, advertise a native TIDAL capability,
automatically share the saved account, resolve a track, refresh a token, or
establish full-playback rights. Those require separate actual acceptance checks.
The independent own-client PKCE CLI remains available as a separate route.

The package dependency is pinned because its observed API is version-specific.
Its transitive dependencies are installed by the existing native installer;
actual target installation and login must be verified before calling this a
working XU4 login. Do not bypass installation failures or CI gates.

Sources:
- https://pypi.org/project/tidalapi/0.8.11/
- https://github.com/EbbLabs/python-tidal/tree/v0.8.11

Do not deploy or restart while login is `pending` or `cancelling`; wait until
`active` is false. The installed user service has a 45-second stop timeout. If
upstream polling outlives it, a forced stop is not a clean shutdown and the
existing deployment guard can refuse installation. Do not weaken that guard.

## Explicit LAN account sharing

After a successful device login, **Share account with trusted LAN apps** is a
separate same-origin, CSRF-protected action. It enables the existing SOAP
`GetUserInfo(AccountSource=Tidal, Version=2)` response for the exact saved
session. LAN controllers can obtain its access token through that protocol;
the refresh token is never included. This is trusted-LAN sharing, not LAN-user
authentication. Status endpoints still return no credentials or account identity.

The saved record carries the actual device-flow client ID and the pinned
`tidalapi-device-v0.8.11` provenance. Older records without this provenance
cannot be shared as library-device sessions. Token expiry suspends projection;
a verified same-account refresh can renew that prior decision. An arbitrary
changed record invalidates sharing, and process restart clears it. **Stop
account sharing** revokes even a suspended sharing decision without deleting
or revoking the provider grant.

The adoption-only response includes the encoded actual client ID but omits
`redirectURI`: no registered callback for this device client is established.
Static APK review shows the authenticated account path validates with Bearer
`/v1/sessions`; its local redirect fallback is not proof of a network callback.
Native app OAuth re-login is unsupported. Version-2 GetUserInfo may renew a
recognizably expired session only while the exact stored record still has a
previous explicit LAN-sharing decision. It uses the same refresh owner and
durable retry guard as playback, with a 10-second total refresh deadline. This
is an ORR policy, not an observed native-app timeout. Fresh sessions, missing
sharing, other versions and ordinary status polling do not trigger refresh.
Unshare remains immediate during the request, and consent, current storage and
fresh expiry are checked again before any account response. No TIDAL capability
is advertised. Successful
SOAP tests are synthetic interoperability checks, not Android, catalogue,
full-playback or XU4 acceptance.

Schema-2 device records require the new reader. Returning to an older release
preserves the private file but that release refuses to read its newer format.

## Explicit single-track playback test

With a valid private device session, enter one numeric TIDAL track
ID and choose **Test this track on the renderer**. This action replaces the
current queue and starts the existing player; it is not an automatic login
side effect. This local test does not require LAN account sharing. Explicit TIDAL
`CreateQueue` track lists use the same queue owner after explicit LAN account
sharing. With the currently advertised root capability version 1.0,
`CreateQueue` prepares canonical IDs without track metadata/STREAM requests or
native load/resume. The app's following `PlayQueueWithIndex` resolves and loads
only the selected ID. Invalid, unshared or locally unavailable preparations leave
the old queue unchanged. A successful preparation replaces it in STOPPED state;
provider track errors are therefore reported by the later play command.
For root versions at least 1.2 (currently 1.4 only with the existing Plex
experiment), the selected one-based start index resolves before replacement
and playback begins immediately. Explicit private Webplay and bounded tests
remain immediate; `ReplaceQueue` retains its existing behavior. Later
Next/Previous/index commands resolve only their selected ID.
Unresolved entries retain canonical per-ID catalogue URLs and safe placeholder
metadata. Header-only selections support exactly one requested
`/v1/albums/{id}/tracks` or `/v1/playlists/{id}/items` page, with limit at most
50 (default 50) and a 64-KiB response bound. The original SearchUrl/query is
retained; an explicit countryCode must match the saved session. StartPlayIndex
is local to the returned page. The reported queue size is the loaded item count,
never the provider's collection total; normal playback ends at this page's end.
No following or preceding page is fetched or invented.

Every raw page entry must be unambiguously an audio track with allowStreaming
exactly true. Missing/null/false eligibility and mixed/video pages are unsupported
as a whole; entries are never filtered in a way that could shift the requested
index across app SourceVersion variants. Response offset/limit/total/items and
track IDs are bounded and checked before playback. Page and selected track must
bind to the identical private session. Legacy preparation reads only the page
and retains that private binding until the first selected track is resolved;
a changed or expired page session cannot authorize a load. Immediate playback
checks both bindings before queue replacement. The bounded one-track test lease
is unchanged and cannot admit a page.
This is synthetic local queue coverage, not native-app/provider acceptance.

Privately owned TIDAL tracks report `PlayMedium=SONGLIST-NETWORK`, matching the
app's same-source search gate. Radio and ordinary direct-URL media labels are
unchanged. The observed native-app search path can insert and immediately start one TIDAL
track in an existing privately owned TIDAL queue using `AppendTracksInQueueEx`:
`Action=playnext`, `Direction=1`, `Play=1`, and `StartIndex` equal to the current
one-based queue index. The preceding `GetQueueIndex(QueueName=CurrentQueue)`
read recognizes that alias only for a privately owned TIDAL queue; other name
checks remain exact. Its explicit one-track XML uses `ListName=CurrentQueue`
and `StartPlayIndex=0`. The new track resolves before current audio is stopped;
session, LAN sharing and queue ownership are checked again after resolution and
after Stop confirmation. Existing track objects and their private provenance are
retained, and unselected neighbors remain unresolved. Signed URLs and supplied
metadata do not enter public queue/status data. Header-only or multi-track append,
other append modes, stale indices and non-TIDAL/unowned queues are rejected before
provider work or playback changes. This is a bounded search operation, not general
queue-extension support; root capabilities and radio behavior are unchanged.

### Native-app static contract observation

Clean-room inspection of APK 3.6.11 finds that absence of a Tidal service entry
does not itself block account adoption or CreateQueue. The per-source version
falls back to empty, and normal/default quality becomes LOSSLESS; this resolver
supports LOW/HIGH/LOSSLESS, not Master. A globally saved Master/HI_RES choice
is also reduced to LOSSLESS when the selected renderer's qualities omit HI_RES;
the app applies this check when constructing the playback header. Separately, the root servicesCapability
version controls the `c22.m0`/`y` CreateQueue success path: below 1.2 it sends
PlayQueueWithIndex, at least 1.2 it does not. ReplaceQueue uses a different
callback without that branch. Another CreateQueue callback (`l0`/`b0`) always
sends the play-index follow-up, so the version-dependent observation is specific
to the inspected play-media route. Capabilities remain unchanged; no filtering,
autoplay or ReplayGain support is advertised. This is static protocol evidence,
not verification with the unchanged real app.

The small provider adapter uses the saved device access token for bounded,
fixed-origin TIDAL v1 metadata and `playbackinfopostpaywall` requests. It does
not invoke the library's recursive refresh wrapper. A recognized expiry can
trigger one bounded refresh under the same exclusive owner before the requested
track or page GET. An unknown expiry or HTTP 401 fails without refresh/retry.
No timer, startup or status read contacts the provider. LOW/HIGH/LOSSLESS
are supported requests, AUTO requests HIGH, and HI_RES is rejected.

Only one unencrypted direct BTS audio candidate is accepted. Explicit preview,
DRM/key fields, MPD/HLS and unsafe destinations are rejected. Missing
`assetPresentation` remains unknown, rather than inheriting the library's
FULL default. Even an explicit FULL response is not proof of complete audio.
The real provider grant, full decoded duration and audible output must still
be validated on the target.

The signed media URL stays in private runtime fields. Public queue/UPnP/status
retain the canonical catalogue identity. Actual media uses the existing
loopback-only proxy with public-address DNS pinning, no LAN exceptions, no
TIDAL media redirects, and no API authorization header forwarded to the CDN.
No new listener or native TIDAL capability is enabled by this step.

### Saved favorites and URL playlists

**Save current as favorite** and playlist export retain the stable track URL,
never the private session or signed media URL. Recalling a favorite or playing
an imported playlist recognizes only the renderer's exact
`https://api.tidal.com/v1/tracks/<numeric-id>` form, without query parameters,
fragments, user information or alternate origins. It resolves that track again
through the existing private session and protected media proxy; LAN account
sharing is not required. Saving and importing alone do not contact TIDAL.

These URL-only formats do not preserve the original quality choice. Restored
tracks request **LOSSLESS**. Missing account/runtime/proxy or failed resolution
fails playback instead of loading the catalogue endpoint as audio. Existing
provider queues retain their own quality and sharing requirements.

## Explicit same-account session refresh

**Refresh the saved session** performs one bounded renewal using the regular
pinned library's device-client configuration. It does not start a login or
playback. The private session directory remains exclusively leased across the
exchange and the returned access token's `/v1/sessions` identity verification.
A valid rotated refresh token is retained; if the provider omits rotation, the
existing refresh token is retained. Only a complete same-account result is
saved atomically. No credentials or account identity appear in the response.

Previously enabled LAN sharing follows this verified same-account/client
rotation, without enabling sharing for a previously unshared session. An
arbitrary file replacement, changed identity, explicit unshare or process
restart still requires a fresh sharing decision. Cached playback candidates
bound to the old record cannot authorize a new load after rotation.

A failed exchange is uncertain unless OAuth explicitly rejects the grant:
there is no automatic retry, implicit re-login or background recovery. A
filesystem failure after replacement may also mean the provider or local
session changed despite the error. Recovery needs an explicit local action;
an existing marked grant is never retried. Ordinary status polling never
contacts the provider. On-demand playback refresh uses this same owner, deadline,
identity verification and synchronous consent callback. Explicit Unshare during
refresh always wins; expiry does not turn a suspended opt-in into shared access.

Before a refresh POST, a private atomic 0600 NOFOLLOW sidecar records only a
fingerprint of the full session and the uncertain outcome. Cancellation, lost
responses and process restart cannot automatically repeat that grant. Confirmed
OAuth invalid_grant is retained as rejected and requires a new login; other
uncertain attempts remain blocked. Both automatic and manual refresh refuse the
same marked record. A durable successful rotation clears the marker.

### Explicit local session recovery

Use **Forget saved TIDAL session** on `/ui/tidal` after a rejected or uncertain
refresh, or when deliberately changing accounts. Check its separate removal
confirmation, then press the button. The CSRF/same-origin-protected
`POST /orr/v1/tidal/disconnect` accepts only
`{"confirm_forget_local_session": true}`. Cancel is different: it prevents adoption
of a pending login and never removes an already-saved session.

Forgetting removes the local session and its process-local sharing decision,
including suspended sharing. It does not delete the TIDAL account, revoke
provider grants, start another login, or send a playback command. Pending login,
refresh, resolution, bounded tests and conflicting renderer mutations refuse
removal without waiting. Cancel a pending login and let its worker finish;
stop active or paused TIDAL playback first. Foreign-source playback is preserved.
Existing private-file ownership, permissions, symlink and exclusive-lease checks
remain in force. Unsafe storage needs operator repair; the web action never
repairs permissions or follows an unsafe file.

The old fingerprint-bound refresh marker is retained, so reintroducing the same
uncertain/rejected record cannot silently retry its grant. A genuinely new login
record is not blocked by that old marker. Forgetting clears private cached TIDAL
stream candidates but preserves public saved track references. After success,
confirm and start a new device login explicitly; sharing needs new confirmation.
No automatic retry follows a lost response: inspect local status first.

Native SOAP `UserLogout` remains unsupported. The observed Android path also
re-reads `GetUserInfo` and expects a parseable logged-out account response; the
web recovery action does not claim that native contract. Offline tests do not
establish live-provider refresh acceptance or full playback.
