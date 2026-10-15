# Independent internet-radio catalog

Open the renderer's existing `/ui/` page on your trusted LAN. Under **Internet
radio**, enter at least two characters of a station name. An optional two-letter
country code (for example `DE`) narrows results. Select **Play** or **Save favorite**.

- Search returns at most 20 popularity-ordered directory records, excluding
  broken stations, HLS and unsupported codecs. Some searches therefore show fewer
  than 20 results. Try a narrower station name if the desired station is absent.
- Play replaces the current queue using the renderer's existing playback owner.
  The player reports actual renderer state; search success does not prove audio.
- Save favorite does not start audio. Existing favorites, renaming, backup and
  recall are reused. The saved target is a stable Radio-Browser station UUID
  reference, resolved again when played, so a changed stream URL is not frozen.
- Results expire after 15 minutes and after a renderer restart. Search again if
  a selection expires. Clear discards results, including a late search response.
- Directory outages do not change playback. Stations may disappear, be
  geoblocked, or stop working; no availability or rights guarantee is implied.
- This is an independent ORR catalog. It does not integrate WiiM Radio, provide a
  TuneIn catalog, or announce an additional service to WiiM Home.

## Privacy and bounds

Search sends only the entered name and optional country code to Radio-Browser.
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
not apply. The player is restricted to raw MP3/AAC demuxers for these references.
HLS, nested playlists and other formats are intentionally unsupported. Ordinary
manual URLs and the existing TuneIn resolver retain their previous behavior.

## Evidence and limits

- Targeted unit/real HTTP checks: bounded search, source filtering, browser
  admission, favorites and canonical queue ownership, expiry, source rejection,
  and Stop superseding a pending station lookup.
- `tests/test_libmpv_radio_catalog.py`: opt-in real HTTP/proxy and libmpv/null
  decoding of generated MP3, favorite recall after a changed AAC URL/codec, and
  rejection of a misleading M3U without fetching its nested target. Directory
  data/DNS are synthetic; the decoder is real. This is not production Unix IPC.
- The existing hosted Chrome lane additionally checks the genuine desktop/mobile
  UI's search → save → play flow, pending-search Clear, and directory failure,
  using generated MP3, a synthetic directory and production Unix IPC/null output.
- Neither synthetic directory verification nor null output proves a live public
  station, audible DAC playback, original WiiM Home, or user hardware acceptance.
  Check the exact PR-head CI result before treating the hosted lane as passed.

## Public sources

- [Radio-Browser API usage and mirror discovery](https://api.radio-browser.info/)
- [Search, station UUIDs and click-counter API](https://docs.radio-browser.info/)
- [Directory data is public-domain; streams remain station property](https://www.radio-browser.info/add)
- [mpv demuxer options](https://mpv.io/manual/stable/#demuxer)

The directory's data license does not grant rights to rebroadcast station audio.
