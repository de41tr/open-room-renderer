# Demo: a phone, a real source, and real sound

Use an **already installed and running** renderer and its existing `/ui/` page
on a trusted LAN. Do not start a second renderer, replace a working installation,
or change discovery just to give a demo. Keep the actual LAN address private.
The default URL is `http://<renderer-address>:8080/ui/`; use the configured port.

An HTTP acknowledgement, a green status or **Inspect output** is not a listening
test. Rehearse on the intended phone and physical output before a visitor arrives.
Keep a working fallback and the installed commit recorded privately.

## Prepare once

1. Check the connected output and lower the physical amplifier/headphone level.
   Set ORR volume to 0, wait for confirmed readback, then raise it slowly to a
   previously checked modest listening level. A percentage alone cannot establish
   safe loudness. Never use a demo to discover the maximum level.
2. In **Internet radio**, search for a station or browse a country. Play one
   result and verify actual sound. This is ORR's independent Radio-Browser
   catalog; it is not WiiM Radio or the complete TuneIn catalog.
3. Prepare a **directory-independent** favorite with **Favorites → + Add**:
   enter a clear name and one broadcaster-published direct audio URL, then
   **Save favorite**. Saving does not play audio. Choose its **Play** separately
   and listen. Two public examples are:

   - [Deutschlandfunk's official player links](https://www.deutschlandfunk.de/livestream-100.html):
     `https://st01.sslstream.dlf.de/dlf/01/128/mp3/stream.mp3?aggregator=web`
   - [Fritz's official reception page](https://www.fritz.de/programm/frequenzen/):
     `http://fritz.de/livemp3`

   The addresses are published by their broadcasters, but availability and
   playback on your device are **not guaranteed**. A favorite saved from a
   Radio-Browser result still needs that directory when recalled; it is not this
   independent fallback. Direct favorites do not promise the catalog's live-title
   or station-retry behavior. These streams still require working internet.
4. For a ready two-source queue, optionally download
   [showcase-direct-radio.json](../examples/showcase-direct-radio.json). Choose
   **Stop → Load playlist → Authored playlist JSON file**, select the file,
   review, then **Replace queue, keep stopped**. This replaces the current queue;
   retain its original playlist first. It does not change saved favorites or
   autoplay. Select a row to play, and save the preferred source with a clear
   favorite name. Do this during preparation, not while repairing a live demo.
5. Prepare one subtle EQ comparison. The optional
   [showcase-gentle-bass-cut.json](../examples/showcase-gentle-bass-cut.json)
   contains a **−2 dB low shelf at 160 Hz, Q 0.707**, with no boosted bands.
   **Sound → Import saved curve**, select the file, review and confirm the import.
   Import saves without activating and refuses an existing name or a different
   active plugin context. Do not overwrite a personal preset or change plugin
   context merely to force this example. Select **Showcase gentle bass cut →
   Load** only after reviewing it; Load enables EQ. Rehearse **Bypass EQ / Enable
   EQ** at the same modest level. The curve is a comparison example, not room
   correction or a claim of better sound. If the difference is not useful on
   your music/output, omit this moment rather than exaggerating it.

For a WAN-outage fallback, use only your own permitted audio on a previously
tested local HTTP server. A laptop file path is not a renderer URL. No such
local source or second device is assumed to exist, and no service installation
is needed for the normal radio demo.

## Five-minute visitor flow

| Time | Visitor action | What to establish |
| --- | --- | --- |
| 0:00–0:45 | Open the existing phone controller; show the room name. | This is the independently running Linux network player. |
| 0:45–2:30 | Search and Play, or choose the rehearsed direct favorite. Adjust volume with **Set volume**, then **Pause / Play**. | Actual sound, an audible control effect, and a reliable return. |
| 2:30–4:00 | Load the prepared gentle preset; compare **Bypass EQ / Enable EQ**. | Manual parametric EQ, with a real observed effect and modest level. |
| 4:00–5:00 | **Save current as favorite**, then **Stop** and play that favorite again; or switch to the other rehearsed source. | A useful saved selection and repeatable playback. |

Favoriting preserves other entries. Playing a favorite replaces the current
queue. Live titles should be described only when actual changing metadata is
present; never show fake audio activity or imply the UI can prove audibility.

Pitch: **“An independent Linux network player: phone control, its own radio
search, favorites and real parametric EQ.”** Include each capability only to the
extent established by your rehearsal.

## Optional extra, only after its own rehearsal

- [Spotify Connect](SPOTIFY_RECEIVER.md): use the existing exclusive receiver,
  reviewed service permission and the user's own manual pairing. Confirm actual
  output, title change and return first. Each Spotify service start begins at
  volume 0; raise it carefully. Spotify has its own volume and **bypasses ORR EQ**.
  ORR's Web UI is unavailable in this mode. Keep an independent terminal open:
  `open-room-renderer spotify receiver --directory <verified-private-directory> switch renderer`.
  After returning, open the UI, recheck volume and explicitly start the favorite;
  playback does not resume automatically. Do not install/pair in front of guests.
- [Snapcast](SNAPCAST.md): only with two authorized real clients and outputs,
  separately rehearsed. It is not native WiiM grouping. Do not claim precise
  synchronization without a measurement.

Missing account, provider permission, hardware or a reliable return means omit
the optional act. It must never endanger the working main demo.

## Two-minute fallback

1. **First 20 seconds:** Close/Cancel an open dialog, then choose **Stop** and
   inspect the state and physical output. If an outcome is UNKNOWN, do not
   hammer the same command or claim silence.
2. **By 75 seconds:** If radio search failed, play the already tested **direct**
   favorite. If Spotify failed, use the independently prepared receiver CLI to
   return to the renderer, then start that favorite. No fresh setup or account
   troubleshooting during the demo.
3. **By two minutes:** Try the other prepared source. For a WAN failure, use the
   previously heard local HTTP source. If no tested source works, stop the audio
   act and explain the limit. No private-state reset, random deployment, new
   firewall rule, port forwarding or speculative repair.

The original WiiM app's discovery and its actual playback are separate checks.
Neither ORR's own UI nor protocol tests establish original-app TuneIn playback,
native WiiM multiroom, RoomFit or other unsupported provider integrations.

Developer setup and targeted integration checks live in
[Getting Started](../GETTING_STARTED.md), [Web UI](WEB_UI.md),
[Radio catalog](RADIO_CATALOG.md) and [the feature matrix](FEATURE_MATRIX.md).
They are preparation tools, not the visitor demonstration.
