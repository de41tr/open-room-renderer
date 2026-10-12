# Service Overview For Local Renderer Adapters

This document summarizes service/source labels seen during black-box compatibility testing and translates them into clean-room implementation guidance for Open Room Renderer. It is intended for documentation and interoperability work. It does not describe credential extraction, DRM bypass, private media download, app modification, or certificate-pinning workarounds.

## Scope

The names below are compatibility labels and capability identifiers that controller apps may already know how to display or classify. They are documented here as observed behavior, not as copied app internals.

## Service Families

| Service / identifier family | App-visible role | Implementation similarity to TuneIn/Plex | Recommendation for this renderer |
|---|---|---|---|
| Open Network Stream | User supplies a playable URL directly | Simpler than TuneIn and Plex | Implement first. Convert URL into one queue item and expose full status/seek metadata when duration is known. |
| `CustomRadio` | Custom user radio/source entry | Similar to TuneIn live station, but without directory account logic | Implement as direct stream presets or local catalog entries. Source should report `dlnaTrackSource=CustomRadio`. |
| `TuneIn`, `newTuneIn`, `vTuner` | Radio/podcast directory sources | Baseline model for station and episode playback | Implement with your own catalog and DRM-free URLs. `newTuneIn` maps back to TuneIn search UI. |
| `Linkplay Radio`, `RadioParadise`, `RadioParadise2`, `HotMix`, `SoundMachine`, `iHeartRadio`, `iHeart`, `CalmRadio` | Radio-directory-like sources | Mostly similar to TuneIn for playback once a stream URL is known | Treat as station/episode catalogs if you want source labels. Avoid private service calls unless official/open. |
| `Plex` | User-owned media library source | More complex than TuneIn because it has account/server/library discovery | Experimental study only. Disabled/not surfaced in the alpha profile until login, server selection, library browse, and playback are reliable without exposing tokens. |
| `UPnPServer`, `Samba`, local music/source-phone | LAN/local library sources | Similar to Plex after media URLs are resolved, but discovery/browse differs | Useful for local files. Implement as generic containers/tracks; playback still becomes normal queue items. |
| `Deezer2`, `Qobuz`, `Tidal`, `AmazonMusic`/`Prime`, `Pandora`/`Pandora2`, `SoundCloud`, `Rhapsody`/Napster, `KKBOX`, `YouTubeMusic`, `KUGOUHiFi` | Cloud/subscription service sources | Browse/login may look structurally similar, but playback rights and URLs are service-controlled | Do not emulate protected playback. Only support official APIs/SDKs or DRM-free user-provided URLs. Keep docs high-level. |
| `SpotifyConnect`, `TidalConnect`, `QobuzConnect`, `Roon`, `Squeezelite`, `HiPlay`, `Soundtrack` | Connect/protocol integrations | Not like TuneIn/Plex HTTP browse. They are separate protocols or daemons | Do not claim support unless an actual compatible official/open protocol stack is integrated. |
| `linein`, `Bluetooth`, `HDMI`, optical/coax/phono/aux, `preset` | Device input/status modes | Not catalog services | Useful for status display only. They do not solve network playback. |

## Observed Source and Capability Identifiers

The capability class defines these service IDs:

- `CalmRadio`
- `deezer`
- `Deezer2`
- `HiPlay`
- `HotMix`
- `iheartaudio`
- `KKBOX`
- `LinkplaySpotify`
- `newTuneIn`
- `Pandora2`
- `Plex`
- `prime`
- `Qobuz`
- `QobuzConnect`
- `RadioParadise2`
- `rhapsody`
- `Roon`
- `Roon Ready`
- `Soundtrack`
- `SoundCloud`
- `soundmachine`
- `Samba`
- `SpotifyConnect`
- `Squeezelite`
- `tunein`
- `TidalConnect`

The player model recognizes additional source/play-medium labels:

- Track-source labels: `AmazonMusic`, `Prime`, `CustomRadio`, `Deezer2`, `HotMix`, `iHeart`, `iHeartRadio`, `KUGOUHiFi`, `Linkplay Radio`, `Rhapsody`, `AldiLife`, `NEWTUNEIN`, `Pandora`, `Pandora2`, `Plex`, `Qobuz`, `RadioParadise`, `RadioParadise2`, `SoundMachine`, `Soundtrack`, `Tidal`, `TuneIn`, `UPnPServer`, `vTuner`, `YouTubeMusic`.
- Play-medium labels: `BLUETOOTH`, `CAST`, `HiPlay`, `ROON`, `Samba`, `SQUEEZELITE`, `TIDAL_CONNECT`, `QOBUZ_CONNECT`, Spotify's play-medium constant, line-in/optical/HDMI/coax/phono/aux constants.
- Special `newTuneIn` behavior: the app treats `dlnaTrackSource=NEWTUNEIN` as New TuneIn. It also treats metadata containing `<song:originSource>newTuneIn<` with `dlnaTrackSource=SleepRadio` as New TuneIn.

## UI Item Types

The app's source UI has icon mappings for these generic content item types:

| Type | Meaning inferred from UI mapping |
|---|---|
| `0` | collection |
| `1` | playlist |
| `2` | album |
| `3` | artist |
| `4` | station |
| `5` | song |
| `6`, `7`, `8` | episode |
| `9`, `104` | genre |
| `10`, `107` | folder |
| `105` | artist |

This is useful for service adapters. Podcasts should be modeled as episode items, but once playback starts they should behave like long seekable tracks: title, artwork, duration, position, `pause`, `resume`, `stop`, `next`, `prev`, and seek.

## Common App Flow Pattern

Most services can be understood as this sequence:

1. Device discovery finds a renderer through SSDP/UPnP and `description.xml`.
2. The app reads device status/capability data and decides which sources/services to show.
3. For a selected service, the app either opens a built-in flow, asks the renderer for account state, or browses a service catalog.
4. The selected station/track/episode is converted to a renderer queue/playback request.
5. The player screen is driven by renderer status: source, play state, title, album/artist/show, artwork URL, duration, current position, and seekability.

For the emulator, the important part is step 4 and step 5. A service browser can be minimal if every playable item becomes a normal queue item with strong metadata.

## Visibility Requirements

To make a service appear and remain usable, the renderer likely needs to expose a consistent combination of:

- Service capability IDs in whatever capability/status field the app is reading.
- `getStatusEx` fields that identify a plausible model/project/firmware/capability set.
- `dlnaTrackSource` matching the selected service during playback.
- `dlnaPlayMedium` matching the actual mode if the app differentiates service source from playback transport.
- Queue/current-track metadata that includes source, item ID, title, artwork, duration, and URL or opaque track ID.

The exact capability container is firmware-dependent. Do not expose every possible cloud service blindly. For an open-source renderer, prefer a small honest set:

- Open Network Stream / URL playback.
- CustomRadio.
- TuneIn-like local catalog, if implemented.
- Plex, only if the Plex adapter is fully implemented. In the alpha it is an experimental disabled/not-surfaced study.
- UPnPServer/Samba, only if the LAN library adapter is implemented.

## Recommended Implementation Priority

1. Core renderer: SSDP, `description.xml`, `/httpapi.asp`, AVTransport, RenderingControl, PlayQueue, and status.
2. Open Network Stream: direct URL to single queue item.
3. Podcast/episode model: duration, current position, seek, `+30`/`-30`, `next`, `prev`, artwork.
4. CustomRadio/TuneIn-like local catalog: folders, stations, episodes, direct playable URLs.
5. Plex: user-owned PMS browse and direct library playback, with token handling kept out of logs and docs examples. Status for this alpha: started, then disabled/not surfaced.
6. UPnPServer/Samba: optional local library browse.
7. Cloud music service adapters only through official APIs/SDKs and only where playback rights allow it.

## Minimal Service Adapter Contract

Every service adapter should output the same internal queue item shape:

```json
{
  "id": "stable-service-item-id",
  "source": "TuneIn",
  "type": "episode",
  "title": "Episode title",
  "subtitle": "Show or station name",
  "artist": "Publisher or station",
  "album": "Podcast or collection",
  "artwork_url": "http://<renderer-ip>/art/<id>.jpg",
  "media_url": "https://example.invalid/audio.mp3",
  "mime_type": "audio/mpeg",
  "duration_sec": 3600,
  "seekable": true,
  "is_live": false
}
```

For live radio, use `type=station`, `is_live=true`, omit or set `duration_sec=0`, and make seeking unavailable.

For podcasts/episodes, use `type=episode`, `is_live=false`, set a real duration when known, and implement absolute seek. This is what makes progress bars and `+30`/`-30` buttons behave like users expect.

## Service-Specific Notes

### TuneIn-like and radio services

Implement these as catalog folders plus playable station/episode items. The app has separate labels for old and new TuneIn, but both map into the TuneIn search/source area. New TuneIn can require either `dlnaTrackSource=NEWTUNEIN` or metadata with `<song:originSource>newTuneIn</song:originSource>` depending on path.

For a clean implementation, do not depend on private TuneIn APIs. Use your own JSON catalog or open podcast feeds and direct audio URLs.

### Plex

Plex is a real library/account adapter, not just a station list. It was started during local experiments, then disabled and hidden from the alpha app-facing profile because login/server/library behavior is not yet reliable enough to ship as a feature.

To continue the experiment privately, a developer can enable the hidden service in a local config copy:

```yaml
enable_plex_service: true
enable_plex_legacy_gate: false
plex_capability_mode: "exact"
plex_user_info_mode: "not_login"
```

The renderer loads manual Plex account state from `.state/plex_user_info.json` when it contains `msg=success`, `userId`, and `accessToken`. That file is local secret state and must never be committed, pasted into issues, or included in release archives. A sanitized shape is:

```json
{
  "source": "Plex",
  "accountSource": "Plex",
  "msg": "success",
  "userId": "synthetic-user-id",
  "userName": "plex-user",
  "showName": "Plex User",
  "accessToken": "REDACTED_USER_TOKEN",
  "clientIdentifier": "open-room-renderer-dev-client",
  "sourceVersion": "1.4",
  "capabilityVersion": "1.4",
  "subscription": "0"
}
```

This is for legitimate user-owned accounts and libraries only. Do not add credential scraping, token extraction, protected playback bypasses, or fake subscription behavior.

The useful future abstraction is:

- account state;
- server list;
- library sections;
- albums/artists/playlists/tracks;
- media part URL;
- queue item handoff to the renderer.

Keep actual Plex auth/token handling out of public examples, avoid logging tokens, and support manual local PMS configuration for development.

### UPnPServer and Samba

These should look like folder/container browsers. Once a file is selected, playback is the same as any other queue item. If full SMB access is out of scope, expose a local folder catalog and label it honestly.

### Connect services

Spotify Connect, Tidal Connect, Qobuz Connect, Roon, Squeezelite, and HiPlay should not be represented as simple HTTP service adapters. They imply separate protocol support. Only advertise them if the renderer actually starts and integrates the corresponding official/open service stack.

## Free receiver feasibility under the current P2 boundary

Reviewed 2026-10-12. Free/open-source software does not grant a music subscription,
provider API access, device certification, or permission to redistribute music.
This work permits no provider-cloud authentication, registration, DRM bypass,
OEM/private-key material, or proprietary firmware. No provider runtime or account
flow was tested in this review. TIDAL restrictions below are from indexed primary
portal text; its directly opened SPA did not expose readable content in this
check. Separate partner agreements are not inferred or excluded.
Licenses below identify the main projects;
dependencies and any redistributed binaries need their own notice/source review.

| Route | Software and runtime boundary | Account/provisioning boundary | Current decision |
|---|---|---|---|
| librespot / Spotify Connect | MIT-licensed Rust client and headless Connect receiver, with separate audio and discovery backends. This is not Spotify's official Embedded SDK. | Upstream explicitly requires Spotify Premium. OAuth, access-token and discovery login are authentication routes; discovery does not make playback anonymous. Its README also warns about Spotify's permission to use the service. | Read-only research; no login, receiver launch, cached credentials or advertised capability. Free software does not mean Spotify Free support. [Project](https://github.com/librespot-org/librespot), [authentication options](https://github.com/librespot-org/librespot/wiki/Options). |
| Official Spotify Embedded SDK | Partner-supplied SDK, separate from the Web API or librespot. | Spotify's commercial-hardware route requires approved company onboarding, evaluation agreement/NDA, client/product identity and commercial licensing/certification. | Outside the no-registration/provisioning boundary. No implied official support. [Onboarding](https://developer.spotify.com/documentation/commercial-hardware/onboarding), [requirements](https://developer.spotify.com/documentation/commercial-hardware/implementation/requirements/technical). |
| Official TIDAL app SDK | The Web SDK is Apache-2.0 licensed; Web, Android and iOS SDKs implement app functionality. They do not create a TIDAL Connect receiver. | Public API/SDK requests need access tokens and app credentials. Public developer terms require registration and limit full-length playback to the subscriber Embed Player; SDK features alone do not confer broader playback rights. Playback must use the official, unmodified Player module under the published guidelines. | Read-only research. No generic mpv media-URL extraction or full-track renderer claim. [Web SDK](https://github.com/tidal-music/tidal-sdk-web), [authorization](https://developer.tidal.com/documentation/api-sdk/api-sdk-authorization), [terms](https://developer.tidal.com/documentation/guidelines/guidelines-developer-terms), [guidelines](https://developer.tidal.com/documentation/guidelines/guidelines-developer-guidelines). |
| TIDAL Connect / other approved partner playback | A distinct device integration, not the public app SDK. | The official Connect developer page restricts integrations to device partners. A separate partner agreement cannot be inferred from open SDK source or a working third-party player. | Outside scope; no partner enrollment or advertised support. [TIDAL Connect developer boundary](https://developer.tidal.com/documentation/connect). |
| Shairport Sync / AirPlay | Main code is MIT licensed with other component licenses. AirPlay 2 also needs NQPTP timing, exclusive UDP ports 319/320 and suitable audio timing; a pipe/file sink is not proof of synchronized physical output. | Local user-owned audio does not inherently need a music-provider account. However, the current source contains an embedded RSA private-key constant, and its upstream heritage documents ROM-derived key provenance. | Unmodified runtime excluded by the current no-OEM/private-key boundary. Do not assume selecting AirPlay 2 removes that dependency. No installation or compatibility claim. [Project/runtime](https://github.com/mikebrady/shairport-sync), [AirPlay 2 limits](https://github.com/mikebrady/shairport-sync/blob/master/AIRPLAY2.md), [licenses](https://github.com/mikebrady/shairport-sync/blob/master/LICENSES), [source](https://github.com/mikebrady/shairport-sync/blob/master/common.c), [upstream provenance](https://github.com/abrasive/shairport#thanks). |
| Snapcast with synthetic/user-owned PCM | GPL-3.0-or-later server/client; distributes an existing player's PCM and provides separate group/control interfaces. It is not a provider catalog or Connect receiver. | No provider login, device registration or OEM credentials are needed for the isolated local-PCM route. Snapcast's optional librespot/AirPlay source wrappers do not remove those sources' restrictions. | Continue only as the existing opt-in experiment. Same-host digital delivery/control has evidence; two-host clocks, physical output and WiiM grouping remain unverified. [Project](https://github.com/snapcast/snapcast), [license notice](https://github.com/snapcast/snapcast/blob/develop/server/snapserver.cpp), [existing acceptance](TESTING.md#optional-snapcast-digitalcontrol-experiment). |

The useful next permitted experiment is an isolated Snapcast downstream-failure
case added to the existing production-IPC acceptance path: interrupt the owned
server or ingress reader during generated PCM, verify bounded renderer control
responses, observe actual downstream silence/recovery, and prove complete owned
process/FIFO cleanup. Retain the earlier unexplained Snapcast failure separately
from later passes. Do not convert missing downstream audio into a claim that the
source stopped without observing it, and do not introduce host services or LAN
targets for this experiment.

Discovery, successful control replies, decoded PCM, two-host timing and audible
output are separate acceptance levels. A source label, an mDNS announcement or
same-host file capture cannot justify an advertised provider/multiroom capability.
The current decisions are engineering scope/evidence limits, not legal compliance
certification. Provider terms and project requirements must be rechecked before
any future integration.

## Practical Rule for Publication

Public documentation should describe the renderer's own stable interfaces and observed generic compatibility patterns. It should avoid publishing service credentials, private app secrets, protected media URL mechanisms, or instructions that would bypass third-party access controls.
