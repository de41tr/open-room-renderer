# WiiM Ultra vs. Open Room Renderer

Product snapshot checked **2026-10-15 03:04 UTC**. ORR source baseline:
[`7a5548a2fbcd55564b6466ca4b106b33e9bc5e8b`](https://github.com/de41tr/open-room-renderer/tree/7a5548a2fbcd55564b6466ca4b106b33e9bc5e8b).
This compares user-visible features, not the number of compatible API fields.
Unmerged candidates are labelled separately and are not installed-main claims.

WiiM entries are **manufacturer claims**, not devices tested by this project.
The latest listed Ultra firmware is **5.2.829661 (2026-09-17)** [F]; it is
independent of the WiiM Home app version. Services may also require accounts,
subscriptions, supported regions and compatible app versions. Do not infer the
firmware installed on a particular device from this table.

`JA` = the narrowly described feature exists; `TEILWEISE` = only the stated
subset/candidate exists; `NEIN` = absent from the supported product;
`NICHT_VERIFIZIERT` = the claimed interoperability has not been established.
The **code** and **acceptance** columns are deliberately separate: code presence
is not a production-ready or audible-playback claim. `REAL_CLIENT` includes
real software running with digital/null outputs; only `REAL_DEVICE` establishes
the explicitly tested physical device, and `ORIGINAL_APP` requires the named
unmodified application. `SYNTHETIC` and `CODE_ONLY` do not establish either.

| Feature | WiiM Ultra (manufacturer / version) | ORR code present? | Actual ORR acceptance | User boundary | Next useful step |
| --- | --- | --- | --- | --- | --- |
| Spotify Connect | JA [U, F] | TEILWEISE: optional pinned go-librespot receiver | SYNTHETIC: real service/process arbitration, generated output; account/DAC NOT_TESTED | Separate volume; bypasses ORR EQ; renderer UI stops in Spotify mode | Authorized ARMv7 install, own-account pairing, listen and safe return [S] |
| TuneIn | JA: app service [U] | TEILWEISE: anonymous station-ID resolver and direct streams | SYNTHETIC: resolver/queue contracts; original app NOT_TESTED | No complete official catalog/account integration | Test a public station from the original app |
| WiiM Radio | JA: app >=3.5.8 [R], Ultra firmware >=5.2.818432 [F] | NICHT_VERIFIZIERT: no dedicated service integration | ORIGINAL_APP: NOT_TESTED | TuneIn IDs and URL playback do not prove this service | Observe an authorized original-app station selection |
| Independent internet radio / station catalog | JA: open streams and radio services [U, F] | TEILWEISE: search, Play, stable favorites and separate on-air title; Drafts #7/#9 [C, L] | REAL_CLIENT: Chrome station flow; native MP3/AAC/chained Vorbis and title updates; SYNTHETIC directory [C, L] | Unmerged; direct streams only; live provider/original app/physical audio NOT_TESTED | Authorized live-directory and listening acceptance |
| Native WiiM multiroom grouping | JA [U] | NEIN: discovery is not group participation | NOT_TESTED | Legitimate participation/identity, transport and physical sync gates remain open | Establish a permitted participation path before any join claim |
| Open multiroom / Snapcast | NICHT_VERIFIZIERT: no Ultra Snapcast claim in cited sources | TEILWEISE: opt-in foreground source candidate, Draft #6 [M] | REAL_CLIENT: shipped source to two file clients; gain/pause/resume/stop/cleanup on x86_64 and ARM64; exact-head CI passed [M] | Unmerged; no two-host/DAC sync, target ARMv7 or native-WiiM proof | Authorized two-host/output acceptance |
| EQ / parametric EQ / presets | JA: 10-band GEQ/PEQ, 24 presets [U] | JA: manual ten-band PEQ, balance, saved presets | REAL_CLIENT: digital PCM/control checks [E] | Physical response unmeasured; Spotify bypasses it | Listen/measure the selected output; keep manual-EQ scope |
| RoomFit automatic room correction | JA [U, A] | NEIN | NOT_TESTED | No microphone measurement or automatic filter derivation | Separate future measurement project; manual EQ is not RoomFit |
| TIDAL in-app browsing | JA [U] | NEIN | NOT_TESTED | No supported provider account/catalog integration | Provider-authorized integration only; outside this sprint |
| TIDAL Connect | JA [U] | NEIN | NOT_TESTED | No compatible Connect receiver | Provider-authorized receiver only; outside this sprint |
| Qobuz in-app browsing | JA [U] | NEIN | NOT_TESTED | No supported provider account/catalog integration | Provider-authorized integration only; outside this sprint |
| Qobuz Connect | JA: official 2025-05-21 announcement [Q] | NEIN | NOT_TESTED | No compatible Connect receiver | Provider-authorized receiver only; outside this sprint |
| Plex | JA: app integration; WAN support 5.2.738432 [F] | TEILWEISE: experimental code, disabled and hidden | CODE_ONLY: no accepted library browser | Not a supported ORR feature | Reassess login/server/browse/play together before surfacing |
| DLNA / UPnP renderer | JA [U] | JA: tested renderer/control subset | REAL_CLIENT: typed DMR and PyWiiM/HTTP/IPC [E] | Not every controller or original-app flow accepted | Confirm intended app on the physical LAN |
| Local music / FLAC | JA: USB/library and FLAC playback [U] | TEILWEISE: playable media URLs; finite local FLAC PCM consumer | REAL_CLIENT: real decoder plus byte-exact digital captures [P] | No USB/library browser; experimental PCM opens null/file only | Use served local audio; separately accept target DAC output |
| NAS / SMB library | JA: SMBv2/v3 [N] | NEIN: no supported browse/mount workflow | NOT_TESTED | Playing a served URL is not a NAS browser | Dedicated authorized library integration, if prioritized later |
| Google Cast | JA [U, F] | NEIN | NOT_TESTED | No Cast receiver | Separate supported integration; do not advertise it |
| AirPlay | NEIN: Ultra does not support AirPlay 2 [U] | NEIN | NOT_TESTED | Other WiiM models do not establish Ultra support | No claim or implementation in this sprint |
| Roon Ready | JA [U] | NEIN | NOT_TESTED | No certified/accepted Roon endpoint | Separate integration/certification decision |
| Bluetooth / physical inputs | JA: two-way Bluetooth; line, phono, optical, HDMI ARC [U] | NEIN: no supported input/control integration | NOT_TESTED | Host audio outputs do not imply Ultra-like input hardware | Treat host-specific input routing as a separate project |

## Evidence and sources

- **[U]** [WiiM Ultra product specification](https://www.wiimhome.com/wiimultra/overview), checked 2026-10-14.
- **[F]** [Ultra firmware history](https://faq.wiimhome.com/en/support/solutions/articles/72000631650-wiim-ultra-firmware-release-notes), latest listed release above; checked 2026-10-14.
- **[R]** [WiiM Radio in the mobile app](https://faq.wiimhome.com/en/support/solutions/articles/72000658778-introducing-wiim-radio/), modified 2026-05-21.
- **[Q]** [Qobuz Connect official announcement](https://blog.wiimhome.com/post/qobuz-connect-available-on-wiim), 2025-05-21.
- **[N]** [SMB sharing in WiiM Home](https://faq.wiimhome.com/en/support/solutions/articles/72000631971-adding-and-accessing-smb-shared-files-in-the-wiim-home-app).
- **[A]** [RoomFit settings](https://faq.wiimhome.com/en/support/solutions/articles/72000647399).
- **[C]** [Independent catalog candidate, Draft PR #7](https://github.com/de41tr/open-room-renderer/pull/7), head `bce57cf27329ad8f0f4ea31699b47f0ec2ba8033`; [all four exact-head jobs passed](https://github.com/de41tr/open-room-renderer/actions/runs/38672684610) after one targeted Python 3.11 rerun. Its first attempt hit the unchanged 165-second watchdog; the retry took 160.15 seconds, so timing margin remains narrow. Chrome verified search/Save/Play with MP3/null. x86_64/ARM64 native checks covered refreshed MP3/AAC/Ogg favorites, chained Vorbis, ICY metadata relay, specific-bind proxy and rejected nested playlists. A failed favorite lookup retires playing/paused prior audio; unconfirmed or superseded Stop cannot publish the replacement, and saved favorites remain intact. Ogg is container admission, not universal embedded-codec proof. Directory/DNS fixtures were synthetic; live provider acceptance remains open. Earlier failed/cancelled attempts and corrected stale fixture/call-sequence assertions remain in the PR. [Candidate guide](https://github.com/de41tr/open-room-renderer/blob/bce57cf27329ad8f0f4ea31699b47f0ec2ba8033/docs/RADIO_CATALOG.md).
- **[L]** [Separate live-title candidate, Draft PR #9](https://github.com/de41tr/open-room-renderer/pull/9), head `204b14ff3128eb6bc35e11e25ac63e38c639fdc4`; [all four exact-head jobs passed](https://github.com/de41tr/open-room-renderer/actions/runs/38663161717). Owned native titles reach the Web snapshot without replacing station/favorite/legacy names. A private combined catalog/title check also passed for ICY and chained Vorbis, separately from per-PR CI; changing-title visual Chrome acceptance is not claimed.
- **[S]** [ORR Spotify candidate and acceptance gates](SPOTIFY_RECEIVER.md).
- **[M]** [Foreground Snapcast candidate, Draft PR #6](https://github.com/de41tr/open-room-renderer/pull/6), head `c577de738bd91b0dd28e6eda7532f039f78d3550`; [all four exact-head CI jobs passed](https://github.com/de41tr/open-room-renderer/actions/runs/38659056867). Two prior heads exceeded the unchanged Python 3.11 watchdog; the PR retains that history. Three measured CLI test files were moved between existing groups with identical coverage and every deadline preserved. Timing variability and narrow floor margin remain risks, not a production-stability claim. [Baseline experiment and limits](TESTING.md#optional-snapcast-digitalcontrol-experiment).
- **[E]** [Scoped interoperability evidence](COMPATIBILITY_MATRIX.md), [browser evidence](WEB_BROWSER_ACCEPTANCE.md) and [testing details](TESTING.md). Historical dated checkpoints retain their own scope.
- **[P]** [Experimental PCM and finite standard-FLAC consumer](EXPERIMENTAL_PCM.md).

The final merged PR heads have passing CI: [Spotify #3, `4cd61a7`](https://github.com/de41tr/open-room-renderer/actions/runs/38615409736),
[discovery #4, `965ec23`](https://github.com/de41tr/open-room-renderer/actions/runs/38525001107),
and [PCM #5, `ba1fc6c`](https://github.com/de41tr/open-room-renderer/actions/runs/38536868709).
The workflow runs on PRs only, so there is no separate run for the squash-main
SHA above. Neither those runs nor this matrix establish original-app, physical
multiroom, Spotify-account or XU4/KA17 listening acceptance.
