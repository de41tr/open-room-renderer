# One-file local audio fallback

`tools/serve_demo_audio.py` is a small optional foreground helper for an audio
file you own or otherwise have permission to play. It needs Python 3.11+ on
Linux and a repository checkout; it has no additional dependencies. It serves
one file to the existing renderer, without an internet radio directory or WAN
connection. A working LAN is still needed when the file is on another computer.

## Prepare and play

1. On the authorized computer holding the audio file, keep that file unchanged
   for the demonstration. Start with loopback when ORR is on the **same** computer:

   ```sh
   python3 tools/serve_demo_audio.py /path/to/your/own-track.mp3
   ```

   For a different renderer computer, explicitly bind to the serving computer's
   existing private LAN address, not the renderer's address:

   ```sh
   python3 tools/serve_demo_audio.py /path/to/your/own-track.mp3 --bind YOUR_LAN_IP --port 8099
   ```

   Replace `YOUR_LAN_IP` with a literal private address already assigned to that
   computer. Wildcard/public addresses and ports below 1025 are rejected. The
   helper does not change network settings. If the port is occupied, select
   another unprivileged port; do not stop an unknown service. Do not open router
   ports or expose this helper to the internet. If LAN access is blocked, obtain
   the appropriate authorization instead of changing firewall rules.

2. Copy the printed **Audio URL** into the existing ORR web UI: **Favorites →
   + Add**, enter a name, then save. Saving does not start playback. Keep physical
   gain low and explicitly choose that favorite's **Play** button. Verify actual
   sound, Pause, and Stop on the intended output before relying on the fallback.
   Loopback URLs work only when the helper and renderer share the same computer;
   the browser/phone does not fetch the audio itself.
3. Keep the helper's terminal open for the demo. To finish, choose **Stop** in
   ORR, check its stopped state, then press **Ctrl-C** in the helper terminal.
   Closing the source is not a substitute for renderer Stop or proof of silence.
   The saved favorite remains; restart the helper on the same address/port to
   reuse it. No startup service or automatic playback is installed.

## Bounds and limitations

- Only `/audio` is served. There is no directory listing, upload, file browsing,
  arbitrary path selection, proxy, external fetch, or renderer control endpoint.
- MP3, WAV, FLAC, Ogg/Opus, AAC and M4A extensions are accepted as audio. Playlists
  and other extensions are rejected. This is not a codec-conversion service;
  actual decoder support still determines playback.
- The helper opens one nonempty regular file read-only, rejects a final-component
  symlink, and keeps that descriptor. Replacing the pathname cannot select a
  different file. Do not edit the selected file while serving it.
- GET, HEAD and single byte ranges support ordinary decoder access and seeking.
  At most eight connections are handled concurrently, with socket timeouts.
  Request paths and client addresses are not written to access logs. The startup
  URL is local operational information; keep it out of public reports.
- There is no authentication or TLS. Anyone able to reach this private listener
  can download the selected audio. Use only a trusted LAN and a non-sensitive,
  authorized file. This is a temporary fallback, not a general media library.
- The helper neither selects an audio output nor changes ORR state, volume, EQ,
  accounts, services or firewall configuration. Starting it on another computer
  or installing it on the target requires authorization for that host.

## Verification

Two focused HTTP/input-boundary tests cover exact-file routing, HEAD, ranges,
invalid ranges, unsupported writes, traversal, pathname replacement, unsafe
files and listener arguments. A separate local check ran the actual helper CLI
with original MP3 audio through the existing web Favorite/Play/Pause/Stop API and
real libmpv 0.40/null: decoder time advanced, Pause and native idle were observed,
and Ctrl-C exited the helper cleanly. This is **REAL_SOFTWARE_TESTED**, not a LAN,
XU4, DAC or user-heard result. Verify the physical fallback separately.
