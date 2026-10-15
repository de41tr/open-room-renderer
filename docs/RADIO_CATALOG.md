# Independent internet-radio catalog

Open the renderer's existing `/ui/` page on your trusted LAN. Under **Internet
radio**, enter a station name (2–80 characters), a two-letter country code (for
example `DE`), or both. Leave the name blank to browse stations in that country;
adding a name narrows results. At least one field is required. Select **Play** or
**Save favorite**, or explicitly **Replace favorite…**.

- Search returns at most 20 popularity-ordered directory records, excluding
  broken stations, HLS and unsupported codecs. Some searches therefore show fewer
  than 20 results. Try a narrower station name if the desired station is absent.
- Play replaces the current queue using the renderer's existing playback owner.
  The player reports actual renderer state; search success does not prove audio.
- Save favorite does not start audio. Existing favorites, renaming, backup and
  recall are reused. The saved target is a stable Radio-Browser station UUID
  reference, resolved again when played, so a changed stream URL is not frozen.
- Replace favorite lets you choose one existing managed favorite and review its
  name alongside the selected station. Explicitly keep or clear saved title,
  artist, album and artwork; clearing old metadata is useful when changing
  stations. Its custom name, stable ID and order are preserved. No favorite is
  appended, no stream is fetched, and current playback/queue or an already
  admitted selection is unchanged. Configured favorites are read-only.
  A changed favorite or renderer session is rejected; reopen and review it.
  A lost/uncertain write response remains UNKNOWN, with no automatic retry.
- Results expire after 15 minutes and after a renderer restart. Search again if
  a selection expires. Clear discards results, including a late search response.
- Directory outages during search do not change playback. Playing a selection
  or recalling a favorite first retires the previous source. If the new station
  lookup fails, playback reports an error and the saved favorites remain intact;
  the previous source is not resumed. If stopping the previous source cannot be
  confirmed, replacement is rejected and silence remains unconfirmed. Stations
  may disappear, be geoblocked, or stop working; no availability or rights
  guarantee is implied.
- This is an independent ORR catalog. It does not integrate WiiM Radio, provide a
  TuneIn catalog, or announce an additional service to WiiM Home.

## Privacy and bounds

Search sends only the entered name and/or country code to Radio-Browser.
Playback looks up the selected station UUID. An initial selection also makes a
best-effort popularity click request, as the API asks clients to do; searching,
saving and automatic recovery do not count clicks. The directory sees the renderer's
public network address. The station receives the audio request. No account,
location service, private renderer identifiers, or external artwork is used.

Directory mirrors are discovered via the documented public DNS pool, randomized,
and retried at most three times within a 12-second lookup budget. The optional
click report has a separate one-second cap and cannot fail playback. Responses
are capped at 256 KiB; mirror requests use HTTPS, pinned public DNS addresses, no
redirects, no ambient proxies or cookies. One web search runs at a time; the
selection cache retains at most 100 records for 15 minutes.

Catalog audio goes through the existing loopback-only selected-source proxy even
when ordinary on-demand proxying is disabled. For specific-address or IPv6 API
binds, the CLI starts a proxy-only IPv4 loopback listener on an ephemeral port;
no extra control routes or LAN listener are exposed. It shares normal shutdown
cleanup. If that optional listener fails, ordinary renderer startup continues
and catalog playback is unavailable until the startup error is corrected.
Every upstream redirect is pinned
and validated against public-only addresses; the configured LAN exception does
not apply. The player is restricted to explicit MP3, AAC or Ogg demuxers for these
references; unknown content types fail closed. The exact provider label `OGG`
admits the Ogg container family, with chained Vorbis verified. This does not
claim every embedded codec works or separate Opus/FLAC support. HLS, nested
playlists and other provider format labels are intentionally unsupported. Ordinary
manual URLs and the existing TuneIn resolver retain their previous behavior.

## Evidence and limits

- Targeted unit/real HTTP checks: bounded search, source filtering, browser
  admission, favorites and canonical queue ownership, expiry, source rejection,
  and Stop superseding a pending station lookup. Favorite replacement also
  rejects failed Stop confirmation and respects newer Stop/load ownership.
- `tests/test_libmpv_radio_catalog.py`: opt-in real HTTP/proxy and libmpv/null
  decoding of generated MP3, favorite recall after changed AAC and Ogg URLs/codecs,
  and advancing chained Vorbis across a title change through one selected-source
  request. Failed favorite lookup retires a playing or paused ordinary source
  before directory resolution and preserves saved favorites. Misleading M3U
  bodies fail under MP3 and OGG admission without fetching
  a nested target. Directory data/DNS are synthetic; the decoder is real. This is
  not production Unix IPC.
- The existing hosted Chrome lane additionally checks the genuine desktop/mobile
  UI's name search and country-only browse → save → play flow, pending-search
  Clear, and directory failure,
  using generated MP3, a synthetic directory and production Unix IPC/null output.
- Neither synthetic directory verification nor null output proves a live public
  station, audible DAC playback, original WiiM Home, or user hardware acceptance.
  Check the exact PR-head CI result before treating the hosted lane as passed.

## Public sources

- [Radio-Browser API usage and mirror discovery](https://api.radio-browser.info/)
- [Advanced search: name and countrycode are optional filters](https://docs.radio-browser.info/#advanced-station-search)
- [Station UUIDs and click-counter API](https://docs.radio-browser.info/)
- [Directory data is public-domain; streams remain station property](https://www.radio-browser.info/add)
- [mpv demuxer options](https://mpv.io/manual/stable/#demuxer)

The directory's data license does not grant rights to rebroadcast station audio.
