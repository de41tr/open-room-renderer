# Clean-Room Local Renderer Compatibility Specification

This document describes the expected behavior of a minimal local renderer compatible with WiiM Home-style local network control. It is intended for interoperability research and open-source implementation. It avoids service credential extraction, DRM bypass, protected media download, app modification, and certificate pinning workarounds.

The goal is a renderer that can be discovered by controller apps on the local network, can expose a plausible device description, can receive basic playback commands, can report state, and can play user-supplied DRM-free audio URLs.

## Scope

The renderer should implement:

- local discovery through SSDP/UPnP;
- a UPnP device description and service list;
- a compatibility HTTP API at `/httpapi.asp`;
- enough PlayQueue/AVTransport behavior for controller apps to start playback;
- status, metadata, artwork, duration, position, pause/resume/stop, and seek;
- optional service adapters for sources such as Open Network Stream, TuneIn-like items, and Plex-owned media.

The renderer should not implement:

- DRM circumvention;
- private account scraping;
- third-party service credential capture;
- protected media URL resolution;
- app modification;
- certificate pinning bypass.

See also `docs/app-service-overview.md` for the app-derived service/source overview and implementation priority matrix.

## Architecture

A practical mini renderer can be structured as five layers:

1. Discovery layer
   - Announces the device over SSDP.
   - Responds to `M-SEARCH`.
   - Serves `/description.xml`.

2. Control API layer
   - Handles `/httpapi.asp?command=...`.
   - Handles UPnP SOAP actions for AVTransport, RenderingControl, and the PlayQueue service.

3. Playback state layer
   - Maintains current URI, queue, metadata, play state, elapsed position, duration, and volume.
   - Translates commands into backend player operations.

4. Media backend layer
   - Plays HTTP/HTTPS audio URLs using a local player such as mpv, GStreamer, ffmpeg, or a native audio pipeline.
   - Supports direct MP3/AAC/FLAC/OGG streams and podcast episodes.

5. Service adapter layer
   - Converts app service models into normal playable queue items.
   - Examples: Open Network Stream, TuneIn-like station/episode lists, Plex-owned library tracks.

## Network Ports

Recommended defaults:

- UDP `1900`: SSDP discovery.
- TCP `80`: default device description, UPnP endpoints, and HTTP API.
- Optional historical experiment ports `49152` and `59152`: retained only for targeted compatibility comparisons.

Many apps accept the port advertised in SSDP `LOCATION`. If using a non-privileged development port, ensure all URLs in SSDP and XML point to that port consistently.

## SSDP Discovery

The renderer should respond to multicast discovery requests sent to:

- multicast address: `239.255.255.250`
- UDP port: `1900`
- method: `M-SEARCH`

Typical search targets to support:

- `ssdp:all`
- `upnp:rootdevice`
- `urn:schemas-upnp-org:device:MediaRenderer:1`
- `urn:schemas-upnp-org:service:AVTransport:1`
- `urn:schemas-upnp-org:service:RenderingControl:1`
- `urn:schemas-wiimu-com:service:PlayQueue:1`

Minimal response:

```http
HTTP/1.1 200 OK
CACHE-CONTROL: max-age=1800
DATE: <rfc1123-date>
EXT:
LOCATION: http://<renderer-ip>:<port>/description.xml
SERVER: Linux/6.x UPnP/1.0 OpenRoomRenderer/0.1
ST: urn:schemas-upnp-org:device:MediaRenderer:1
USN: uuid:<device-uuid>::urn:schemas-upnp-org:device:MediaRenderer:1
```

The renderer should also send periodic `NOTIFY` advertisements:

```http
NOTIFY * HTTP/1.1
HOST: 239.255.255.250:1900
CACHE-CONTROL: max-age=1800
LOCATION: http://<renderer-ip>:<port>/description.xml
NT: urn:schemas-upnp-org:device:MediaRenderer:1
NTS: ssdp:alive
SERVER: Linux/6.x UPnP/1.0 OpenRoomRenderer/0.1
USN: uuid:<device-uuid>::urn:schemas-upnp-org:device:MediaRenderer:1
```

Advertise multiple `NT` values if possible:

- `upnp:rootdevice`
- `uuid:<device-uuid>`
- `urn:schemas-upnp-org:device:MediaRenderer:1`
- `urn:schemas-upnp-org:service:AVTransport:1`
- `urn:schemas-upnp-org:service:RenderingControl:1`
- `urn:schemas-wiimu-com:service:PlayQueue:1`

## Device Description

`GET /description.xml` should return a UPnP device description.

Important fields:

```xml
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Open Room Renderer</friendlyName>
    <manufacturer>Independent</manufacturer>
    <manufacturerURL>https://example.invalid/</manufacturerURL>
    <modelDescription>Linux/mpv local audio renderer</modelDescription>
    <modelName>Linux Room Renderer</modelName>
    <modelNumber>1</modelNumber>
    <serialNumber>00000001</serialNumber>
    <UDN>uuid:<device-uuid></UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/upnp/AVTransport.xml</SCPDURL>
        <controlURL>/upnp/control/AVTransport</controlURL>
        <eventSubURL>/upnp/event/AVTransport</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/upnp/RenderingControl.xml</SCPDURL>
        <controlURL>/upnp/control/RenderingControl</controlURL>
        <eventSubURL>/upnp/event/RenderingControl</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-wiimu-com:service:PlayQueue:1</serviceType>
        <serviceId>urn:wiimu-com:serviceId:PlayQueue</serviceId>
        <SCPDURL>/upnp/PlayQueue.xml</SCPDURL>
        <controlURL>/upnp/control/PlayQueue</controlURL>
        <eventSubURL>/upnp/event/PlayQueue</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
```

Useful mimic fields:

- `friendlyName`: user-facing device name.
- `manufacturer`: a neutral project/vendor name for public configs.
- `modelName`: a neutral model name for public configs. Compatibility test configs may use other values locally.
- `UDN`: stable UUID. Do not change between runs unless intentionally resetting pairing/cache.

## HTTP API

The compatibility HTTP API is exposed at:

```text
/httpapi.asp?command=<command>
```

Responses are usually plain text or JSON-like objects depending on command. For a mini renderer, JSON is easiest for status endpoints and plain `OK` for simple commands.

### Required Commands

| Command | Purpose | Minimal behavior |
|---|---|---|
| `getStatusEx` | Extended device/player status | Return state, volume, source, track metadata, firmware/model/capability fields |
| `getStatus` | Legacy status | Return smaller status object |
| `getPlayerStatus` | Playback state | Return play/pause/stop, position, duration, current URI |
| `getMetaInfo` | Current metadata | Return title, artist, album, artwork URL |
| `getPresetInfo` | Preset list | Return empty or simple preset list |
| `setPlayerCmd:play:<url>` | Play URL | Decode URL, load it, start playback |
| `setPlayerCmd:playlist:<url>` | Play playlist URL | Fetch/parse basic M3U or treat as URL |
| `setPlayerCmd:pause` | Pause | Pause backend and report paused state |
| `setPlayerCmd:resume` | Resume | Resume backend and report playing state |
| `setPlayerCmd:stop` | Stop | Stop backend and clear/keep current metadata |
| `setPlayerCmd:seek:<seconds>` | Seek | Seek in current track if seekable |
| `setPlayerCmd:prev` | Previous | Move queue index backward if possible |
| `setPlayerCmd:next` | Next | Move queue index forward if possible |
| `setPlayerCmd:vol:<0-100>` | Volume | Set volume |

### Example `getStatusEx`

```json
{
  "uuid": "<device-uuid>",
  "DeviceName": "Open Room Renderer",
  "GroupName": "Open Room Renderer",
  "ssid": "Open Room Renderer",
  "language": "en_us",
  "firmware": "4.8.000000",
  "project": "OpenRoomRenderer",
  "priv_prj": "OpenRoomRenderer",
  "Release": "20260520",
  "FW_Release_version": "4.8.000000",
  "hardware": "Linux Room Renderer",
  "model": "Linux Room Renderer",
  "manufacturer": "Independent",
  "status": "play",
  "mode": "10",
  "source": "URL",
  "vol": "40",
  "mute": "0",
  "curpos": "12",
  "totlen": "180",
  "Title": "Example Episode",
  "Artist": "Example Podcast",
  "Album": "Example Feed",
  "AlbumArtURI": "http://<renderer-ip>:<port>/art/current.jpg",
  "uri": "https://example.invalid/audio.mp3",
  "capability": "0x00000000",
  "streams_all": "0x7fffffff"
}
```

The exact field set can be expanded over time. The important properties are stable identity, compatible model/manufacturer strings, state, source, volume, position, duration, and current metadata.

### Example `getPlayerStatus`

```json
{
  "status": "play",
  "mode": "10",
  "vol": "40",
  "mute": "0",
  "curpos": "12",
  "totlen": "180",
  "uri": "https://example.invalid/audio.mp3",
  "Title": "Example Episode",
  "Artist": "Example Podcast",
  "Album": "Example Feed",
  "AlbumArtURI": "http://<renderer-ip>:<port>/art/current.jpg"
}
```

Recommended state values:

- `play`
- `pause`
- `stop`
- `load`

## UPnP AVTransport

Implement enough SOAP behavior for common controller flows:

- `SetAVTransportURI`
- `GetTransportInfo`
- `GetPositionInfo`
- `GetMediaInfo`
- `Play`
- `Pause`
- `Stop`
- `Seek`
- `Next`
- `Previous`

`SetAVTransportURI` should accept a direct audio URL plus optional DIDL-Lite metadata. Store:

- current URI;
- title;
- artist;
- album;
- artwork URL;
- duration if available.

`GetPositionInfo` should return:

- `TrackDuration`
- `RelTime`
- `TrackURI`
- `TrackMetaData`

## UPnP RenderingControl

Required actions:

- `GetVolume`
- `SetVolume`
- `GetMute`
- `SetMute`

Use channel `Master`. Keep volume range `0..100`.

## PlayQueue Service

Some controller flows prefer a PlayQueue service over plain AVTransport.

Implement these actions if possible:

- `CreateQueue`
- `BrowseQueue`
- `PlayQueueWithIndex`
- `GetQueueIndex`
- `AppendTracksInQueueEx`
- `RemoveTracksInQueue`
- `DeleteQueue`
- `UserLogin`
- `GetUserInfo`

The minimal useful subset is:

- accept a queue payload;
- store queue items;
- start selected index;
- expose current index and metadata;
- return success for unsupported queue mutation actions when safe.

Queue item fields to preserve:

- source/service name;
- item id;
- title;
- artist;
- album;
- duration;
- artwork URL;
- direct play URL;
- service-specific metadata such as Plex server id or TuneIn guide id.

## Queue Model

Internal queue item:

```json
{
  "id": "item-1",
  "source": "OpenNetworkStream",
  "title": "Example Episode",
  "artist": "Example Podcast",
  "album": "Example Feed",
  "duration": 180,
  "artwork": "http://<renderer-ip>:<port>/art/example.jpg",
  "url": "https://example.invalid/audio.mp3",
  "seekable": true,
  "headers": {}
}
```

Behavior:

- `play`: load queue item at current index and start.
- `pause`: pause without clearing queue.
- `resume`: continue.
- `stop`: stop playback; queue may remain.
- `next`/`previous`: when stopped, select the neighboring queue item without
  starting playback; if already playing, start the selected item. Multi-item navigation
  while paused is rejected without resuming audio. An explicit play request is
  required to start a stopped selection. Loaded finite single-item `previous` retains
  its existing pause-preserving restart behavior.
- `seek`: update backend position for seekable media.

## Open Network Stream

This is the simplest source model. Treat a submitted URL as a single queue item.

Supported input:

- direct audio URL;
- basic M3U playlist URL;
- podcast episode enclosure URL.

Recommended validation:

- reject local file paths unless explicitly enabled;
- reject unsupported schemes other than `http` and `https`;
- avoid fetching large media in the control path;
- probe headers only when needed.

## TuneIn-Like Service Model

For a clean-room implementation, do not rely on private TuneIn APIs. Model it as generic radio/podcast directory data.

Container item:

```json
{
  "type": "container",
  "id": "podcast:example",
  "title": "Example Podcast",
  "childrenUrl": "/service/tunein/podcast/example"
}
```

Playable item:

```json
{
  "type": "episode",
  "id": "episode:example-1",
  "title": "Episode 1",
  "subtitle": "Example Podcast",
  "duration": 3600,
  "artwork": "http://<renderer-ip>:<port>/art/episode.jpg",
  "url": "https://example.invalid/episode.mp3",
  "seekable": true
}
```

Podcast episodes should behave like long tracks:

- show duration;
- report elapsed position;
- support seek;
- support `+30s` and `-30s` if the controller maps those to seek commands;
- support next/previous across episode queue.

If the app shows a progress bar but cannot seek, check:

- `duration` is known and non-zero;
- `curpos` updates;
- backend media is seekable;
- status endpoints return seconds consistently;
- `Seek` and/or `setPlayerCmd:seek` is implemented.

## Plex Service Model

Plex support should be implemented only for the user's own Plex account and server.

Alpha status: Plex was started as an experiment and then disabled/not surfaced in the public app-facing profile. Treat this section as future design notes, not as a supported feature claim.

Recommended safe modes:

1. Manual local configuration for development.
2. Renderer-owned Plex PIN flow, if the user explicitly authorizes it.
3. Controller-app login bridge, if the callback flow works reliably.

Never log Plex tokens. Store account state outside public examples and with restrictive file permissions such as `0600` or equivalent.

### Plex Account State

Minimal logged-in user info shape:

```json
{
  "msg": "success",
  "userId": "<plex-user-id>",
  "userName": "<plex-user-name>",
  "showName": "<display-name>",
  "accessToken": "<redacted>",
  "sourceVersion": "1.4",
  "capabilityVersion": "1.4",
  "subscription": "1",
  "redirectUrl": "<app-callback-url-if-used>"
}
```

`NOT_LOGIN` state:

```json
{
  "msg": "NOT_LOGIN",
  "userId": "",
  "accessToken": "",
  "sourceVersion": "1.4",
  "capabilityVersion": "1.4",
  "subscription": "0",
  "redirectUrl": "<app-callback-url-if-used>"
}
```

### Plex Discovery Flow

After login:

1. `GET https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1`
2. Filter resources:
   - `owned == true`
   - `clientIdentifier` present
   - connections present
3. For each connection, test:
   - `<baseUrl>/identity`
   - `<baseUrl>/library/sections/?type=audio`
4. Accept a music library when the sections response contains a directory with:
   - `type == "artist"`
   - `key` set to the section id
   - `title` set to the library name

Important practical issue:

Some controller code prioritizes local Plex connections over relay connections. If a local connection is advertised but unreachable, relay may not be tried reliably. The most robust workaround is to make at least one advertised PMS connection reachable from the Android device, for example by:

- placing phone and PMS on the same network;
- using VPN/routing;
- enabling Plex Remote Access;
- configuring Plex custom server access URLs to a reachable HTTPS reverse proxy.

### Plex PMS Endpoints

Useful endpoints:

```text
/identity
/library/sections/?type=audio
/hubs/sections/<sectionId>?includeStations=1&count=50&includeLibraryPlaylists=1&type=audio
/library/sections/<sectionId>/all?type=8
/library/sections/<sectionId>/all?type=9
/library/sections/<sectionId>/all?type=10
/playlists?playlistType=audio
/library/sections/<sectionId>/all?type=18
/library/sections/<sectionId>/folder
/hubs/search?query=<query>&contentDirectoryID=<sectionId>
```

Request headers should include normal JSON negotiation and authorized Plex API access. Do not publish real account headers or tokens in docs, logs, or issues.

```http
Accept: application/json
X-Plex-Client-Identifier: <stable-client-id>
```

Track playback URL shape:

```text
<pms-base-url><part-key>?<authorized-query-if-required>
```

Example track object:

```json
{
  "ratingKey": "1001",
  "key": "/library/metadata/1001",
  "type": "track",
  "title": "Test Track",
  "grandparentTitle": "Test Artist",
  "parentTitle": "Test Album",
  "duration": 180000,
  "thumb": "/library/metadata/1001/thumb/1",
  "Media": [
    {
      "Part": [
        {
          "key": "/library/parts/1001/file.mp3",
          "duration": 180000,
          "container": "mp3"
        }
      ]
    }
  ]
}
```

Convert Plex duration from milliseconds to seconds for renderer status fields.

## Status And Progress

The renderer should keep one source of truth for playback state:

- `state`: `stop`, `load`, `play`, `pause`
- `startedAt`
- `pausedAt`
- `elapsedWhenPaused`
- `duration`
- `queueIndex`
- `currentItem`

Every status endpoint should derive from this state. Avoid returning contradictory values such as `status=play` with frozen `curpos`.

Seek handling:

- absolute seek: set position to target seconds;
- relative seek: add/subtract seconds;
- clamp to `0..duration`;
- if stream is not seekable, return an explicit no-op success or clear error.

## Artwork

Use stable local artwork proxy URLs when possible:

```text
/art/current.jpg
/art/<item-id>.jpg
```

Benefits:

- controller can fetch without service tokens;
- tokens do not leak in UI logs;
- artwork URLs remain stable across status calls.

For Plex, proxying `thumb`/`art` avoids exposing account authorization details in app-visible metadata.

## Service Visibility

Controller apps often show services based on a device capability response rather than only UPnP service declarations.

Recommended capabilities to expose in status or service metadata:

- Open Network Stream support;
- TuneIn-like radio/podcast support if implemented;
- Plex support only when token flow and library browse work;
- supported codecs: MP3, AAC, FLAC, WAV, OGG where true;
- seek support;
- queue support;
- playlist support.

Do not claim support for a third-party service unless the renderer can safely implement at least login state, browse or import, queue creation, and playback of allowed media.

## Error Handling

Return stable, boring errors:

- not logged in;
- no server found;
- no music library found;
- media unavailable;
- unsupported format;
- network timeout;
- seek not supported.

Avoid crashes or empty 404 pages for service browse endpoints. Prefer `200` with an empty list and a clear message for optional service UI endpoints.

## Development Checklist

1. Device appears in SSDP discovery.
2. `/description.xml` loads from another machine.
3. `/httpapi.asp?command=getStatusEx` returns stable identity.
4. `setPlayerCmd:play:<url>` plays a known MP3.
5. `getPlayerStatus` updates elapsed position.
6. Pause/resume/stop work.
7. Seek works for seekable files.
8. UPnP `SetAVTransportURI` and `Play` work.
9. PlayQueue accepts a one-item queue.
10. Artwork loads.
11. App can refresh without losing UUID/device identity.
12. Optional service adapters do not log tokens or credentials.

## Clean-Room Contribution Rules

Contributors should:

- document observed network behavior without copying proprietary code;
- use public protocol documentation where available;
- write new implementation code from the behavior specification;
- use their own devices, accounts, and local network;
- avoid protected media and DRM workarounds;
- redact tokens, cookies, and account identifiers from issues and logs.

When reporting compatibility bugs, include:

- renderer version;
- app version;
- device OS;
- SSDP response;
- `/description.xml`;
- relevant `/httpapi.asp` responses;
- sanitized controller request/response logs;
- sanitized queue item metadata.

## Recommended Repository Docs

For a public GitHub repository, include:

- `README.md`: quick start, safety scope, supported features.
- `docs/clean-room-linkplay-renderer-spec.md`: this specification.
- `docs/discovery.md`: SSDP and description XML.
- `docs/httpapi.md`: `/httpapi.asp` commands.
- `docs/upnp-playqueue.md`: SOAP and queue actions.
- `docs/playback-state.md`: status, progress, seek, queue.
- `docs/service-adapters.md`: Open Network Stream, TuneIn-like, Plex.
- `docs/security.md`: tokens, logs, local-network assumptions.
- `docs/testing.md`: curl, tcpdump, Wireshark, controller-app tests.
