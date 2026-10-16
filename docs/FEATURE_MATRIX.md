# WiiM Ultra vs. Open Room Renderer

ORR integration snapshot: **2026-10-15 20:03 UTC**, main
[`22f01c8d5cee5bd5f0dfea5257ca7d0901d8fdfd`](https://github.com/de41tr/open-room-renderer/tree/22f01c8d5cee5bd5f0dfea5257ca7d0901d8fdfd).
[PR #13](https://github.com/de41tr/open-room-renderer/pull/13) incorporated #6–#12
by squash merge. Those source PRs were closed as incorporated, not individually
merged. Their branches were retained. The shared code tree passed all four CI
jobs on its first attempt [I]. The operating-quality fix [O] and value-free
CreateQueue diagnostics [D] are now also integrated. No open PR was found at
the 19:54 UTC repository preflight; later showcase changes need their own review.

**Source integration, installation and hardware acceptance are different stages.**
The combined code has real Chrome, native decoder/IPC, install/uninstall and
Snapcast software evidence [I]. The private target deployment report recorded
this main installed and running at 19:09 UTC, with service checks passed.
That does not establish audible output. The user confirmed original WiiM Android
discovery, but the attempted Fritz/TuneIn playback still failed. No successful
original-app playback, Spotify-account/KA17 listening or physical multiroom
acceptance is established. Private addresses, configuration and captures are
not part of this public matrix.

WiiM entries remain **manufacturer claims**, not project-tested devices. The
manufacturer comparison retains its earlier source snapshot: [U] and [F] were
checked **2026-10-14**, when [F] listed Ultra firmware **5.2.829661
(2026-09-17)**. This refresh does not establish the latest manufacturer release.
Firmware, WiiM Home app versions and account/subscription/region requirements
are separate; none identifies the firmware installed on a particular device.

`JA` = the narrowly described feature exists; `TEILWEISE` = only the stated
subset exists; `NEIN` = absent from the supported product;
`NICHT_VERIFIZIERT` = claimed interoperability has not been established.
Code presence and CI success are not production-ready or audible-playback
claims. `REAL_CLIENT` includes real software with digital/null outputs;
`REAL_DEVICE` establishes only an explicitly tested physical device, and
`ORIGINAL_APP` requires the named unmodified application. `SYNTHETIC` and
`CODE_ONLY` establish neither. `NOT_TESTED` marks an unaccepted scope.

| Feature | WiiM Ultra (manufacturer / version) | ORR code present? | Actual ORR acceptance | User boundary | Next useful step |
| --- | --- | --- | --- | --- | --- |
| Spotify Connect | JA [U, F] | TEILWEISE: integrated optional go-librespot v0.10.3 receiver | SYNTHETIC: real service/process arbitration with generated output; account/DAC NOT_TESTED [S, I] | Separate volume; bypasses ORR EQ; renderer UI stops in Spotify mode; ARM64 CI is not ARMv7 acceptance | Authorized target installation, own-account pairing, listening and safe return [S] |
| TuneIn | JA: app service [U] | TEILWEISE: anonymous station-ID resolver and direct streams | SYNTHETIC: resolver/queue contracts; user-confirmed original Android discovery, attempted Fritz playback failed | No complete official catalog/account integration; diagnostics [D] are not a playback fix | Diagnose an attributable original-app start; require audible acceptance |
| WiiM Radio | JA: app >=3.5.8 [R], Ultra firmware >=5.2.818432 [F] | NICHT_VERIFIZIERT: no dedicated service integration | ORIGINAL_APP: NOT_TESTED | TuneIn IDs and URL playback do not prove this service | Observe an authorized original-app station selection |
| Independent internet radio / station catalog | JA: open streams and radio services [U, F] | TEILWEISE: integrated name search, country browse, Play, stable favorites, favorite repair and separate on-air title [C, L] | REAL_CLIENT: Chrome search/browse/save/play/repair; native MP3/AAC/chained Vorbis and title updates; SYNTHETIC directory [I] | Direct streams only; live directory/provider, original app and physical audio NOT_TESTED | Authorized live-directory workflow and listening acceptance |
| Native WiiM multiroom grouping | JA [U] | NEIN: discovery is not group participation | NOT_TESTED | Legitimate participation/identity, transport and physical sync gates remain open | Establish a permitted participation path before any join claim |
| Open multiroom / Snapcast | NICHT_VERIFIZIERT: no Ultra Snapcast claim in cited sources | TEILWEISE: integrated opt-in foreground source [M] | REAL_CLIENT: source to two file clients; gain/pause/resume/stop/cleanup on x86_64 and ARM64 [I] | No two-host/DAC sync, target ARMv7 or native-WiiM proof; experimental operating scope | Authorized two-host/output acceptance |
| Spotify through existing EQ | Not assessed here | Opt-in existing-service/foreground PCM candidate [S-EQ] | REAL_SOFTWARE: owned synthetic FIFO + existing EQ + digital PCM; account/DAC NOT_TESTED | Separate activation; explicit Web return after Stop; local-app account/output pending; separate Spotify and ORR gain | Authorized target/app/listening and safe-return acceptance |
| EQ / parametric EQ / presets | JA: 10-band GEQ/PEQ, 24 presets [U] | JA: manual ten-band PEQ, balance, saved presets | REAL_CLIENT: digital PCM/control checks [E] | Physical response unmeasured; legacy exclusive Spotify bypasses it; opt-in PCM candidate [S-EQ] | Listen/measure the selected output; retain manual-EQ scope |
| RoomFit automatic room correction | JA [U, A] | NEIN | NOT_TESTED | No microphone measurement or automatic filter derivation | Separate future measurement project; manual EQ is not RoomFit |
| TIDAL in-app browsing | JA [U] | NEIN | NOT_TESTED | No supported provider account/catalog integration | Provider-authorized integration only; outside this sprint |
| TIDAL Connect | JA [U] | NEIN | NOT_TESTED | No compatible Connect receiver | Provider-authorized receiver only; outside this sprint |
| Qobuz in-app browsing | JA [U] | NEIN | NOT_TESTED | No supported provider account/catalog integration | Provider-authorized integration only; outside this sprint |
| Qobuz Connect | JA: official 2025-05-21 announcement [Q] | NEIN | NOT_TESTED | No compatible Connect receiver | Provider-authorized receiver only; outside this sprint |
| Plex | JA: app integration; WAN support 5.2.738432 [F] | TEILWEISE: experimental code, disabled and hidden | CODE_ONLY: no accepted library browser | Not a supported ORR feature | Reassess login/server/browse/play together before surfacing |
| DLNA / UPnP renderer | JA [U] | JA: tested renderer/control subset | REAL_CLIENT: typed DMR and PyWiiM/HTTP/IPC [E, I] | Not every controller or original-app flow accepted | Confirm intended app on the physical LAN |
| Local music / FLAC | JA: USB/library and FLAC playback [U] | TEILWEISE: playable media URLs; finite local FLAC PCM consumer | REAL_CLIENT: real decoder plus byte-exact digital captures [P, I] | No USB/library browser; experimental PCM opens null/file only | Use served local audio; separately accept target DAC output |
| NAS / SMB library | JA: SMBv2/v3 [N] | NEIN: no supported browse/mount workflow | NOT_TESTED | Playing a served URL is not a NAS browser | Dedicated authorized library integration, if prioritized later |
| Google Cast | JA [U, F] | NEIN | NOT_TESTED | No Cast receiver | Separate supported integration; do not advertise it |
| AirPlay 2 | NEIN: Ultra does not support AirPlay 2 [U] | NEIN | NOT_TESTED | Other WiiM models do not establish Ultra support | No claim or implementation in this sprint |
| Roon Ready | JA [U] | NEIN | NOT_TESTED | No certified/accepted Roon endpoint | Separate integration/certification decision |
| Bluetooth / physical inputs | JA: two-way Bluetooth; line, phono, optical, HDMI ARC [U] | NEIN: no supported input/control integration | NOT_TESTED | Host audio outputs do not imply Ultra-like input hardware | Treat host-specific input routing as a separate project |

## Integrated software evidence

- **[I]** [Combined PR #13](https://github.com/de41tr/open-room-renderer/pull/13), candidate `4b4ba74c166080a541911905902508682d1f38ad`, was squash-merged into main `3a5f524fbc7c9bc5ff60a7f7f9caedb2295dd6bd` on 2026-10-15. [Run 38695180456](https://github.com/de41tr/open-room-renderer/actions/runs/38695180456) passed all four jobs and every step on attempt 1. It tested merge-ref `09297a9856b02d4e4181ec7a35f2a9a33ad91f40`; its tree `c6103e02792de8a83fb30ef2521434e3b9304311` equals the candidate and merged-main trees. The workflow is PR-only: there is **no independent run on the squash-main SHA**. Evidence includes genuine Chrome, native x86_64/ARM64, production Unix IPC, install/uninstall and both real Snapcast software stages. Python 3.11's longest group took 160.70 seconds under the unchanged 165-second limit; timing reserve remains narrow.
- **[C]** [Catalog source PR #7](https://github.com/de41tr/open-room-renderer/pull/7), incorporated head `db94b48a1999a61aa1df42d1df557b474e4b27d5`; [four exact-head jobs passed on attempt 1](https://github.com/de41tr/open-room-renderer/actions/runs/38679274979). Chrome exercised name search and country-only browse, Save/Play, and explicit repair of an existing favorite while preserving its name, stable ID, order and current playback. Native checks used generated MP3/AAC/chained Vorbis and synthetic directory/DNS data. Failed favorite lookup retires playing/paused prior audio; uncertain or superseded Stop cannot publish a replacement. Ogg admission does not prove every embedded codec. Earlier failures/retries remain in the PR history. [Integrated catalog guide](RADIO_CATALOG.md).
- **[L]** [Live-title source PR #9](https://github.com/de41tr/open-room-renderer/pull/9), incorporated head `204b14ff3128eb6bc35e11e25ac63e38c639fdc4`; [four exact-head jobs passed](https://github.com/de41tr/open-room-renderer/actions/runs/38663161717). Native ICY/chained-Vorbis titles reach the Web snapshot without replacing station/favorite/legacy names. Combined CI [I] covers the integrated code. Changing-title visual Chrome acceptance is not claimed.
- **[M]** [Snapcast source PR #6](https://github.com/de41tr/open-room-renderer/pull/6), incorporated head `c577de738bd91b0dd28e6eda7532f039f78d3550`; [four exact-head jobs passed](https://github.com/de41tr/open-room-renderer/actions/runs/38659056867), followed by combined CI [I]. Prior timeout attempts remain visible. The existing workflow grouping was balanced once for the integration, with coverage and deadlines preserved. Source-to-two-file-client execution is real software evidence, not physical synchronization or native WiiM participation. [Experiment and limits](TESTING.md#optional-snapcast-digitalcontrol-experiment).
- **[S]** [Spotify receiver and acceptance gates](SPOTIFY_RECEIVER.md). The receiver was already integrated before #13. The incorporated [#11 receipt-validation fix](https://github.com/de41tr/open-room-renderer/pull/11) safely rejects a valid-JSON receipt with the wrong root type; it does not establish Spotify client/account/KA17 playback or hardware safe-switch acceptance.
- **[E]** [Scoped interoperability evidence](COMPATIBILITY_MATRIX.md), [browser evidence](WEB_BROWSER_ACCEPTANCE.md) and [testing details](TESTING.md). Historical checkpoints retain their own dates and limits; [I] provides the combined-code CI result.
- **[P]** [Experimental PCM and finite standard-FLAC consumer](EXPERIMENTAL_PCM.md).

The integration also includes [#10 playback-retirement correction](https://github.com/de41tr/open-room-renderer/pull/10)
and [#12 native-installer correction](https://github.com/de41tr/open-room-renderer/pull/12).
Their inclusion and passing combined CI do not prove the cause or resolution of
an original WiiM app issue. No single radio-to-running-Snapcast handoff sequence
or audible hardware workflow is established by these separate software checks.

## Integrated operating quality and diagnostics

**[O]** [PR #14](https://github.com/de41tr/open-room-renderer/pull/14) was
squash-merged on 2026-10-15 at 16:30 UTC. Its final head
`2bc47b972aac869161e684282f94e7a131befa26` passed all four jobs and 53 steps on
attempt 1 in [run 38755744911](https://github.com/de41tr/open-room-renderer/actions/runs/38755744911).

The integrated fix uses paused retention for demonstrated 400 ms finite direct
playback and ordinary queue admission, with immediate/capped early readiness
sampling. For native loads with an acknowledged entry ID, readiness verifies
its current-and-playing identity (or proven first-level child), rejecting stale
same-URL audio metadata.
It also preserves the active station retry cause and provides an actionable,
privacy-preserving occupied-HTTP-port diagnostic with exit code 2.

Earlier failed runs remain visible:
[`16be9289`](https://github.com/de41tr/open-room-renderer/actions/runs/38743641461)
and [`e3fc2ea`](https://github.com/de41tr/open-room-renderer/actions/runs/38748532769).
The original ARM timeout cause is not established; the same-URL readiness defect
was independently reproduced and fixed. Acceptance of 150 ms media and
unobserved short native-staged successors is not established. Integration does
not establish gapless playback or hardware acceptance.

**[D]** [PR #15](https://github.com/de41tr/open-room-renderer/pull/15) was
squash-merged at 19:06 UTC. Final head
`70c839a203b4b94d70a83a091a545b1be38c28f2` passed all four jobs and 53 steps on
attempt 1 in [run 38789264534](https://github.com/de41tr/open-room-renderer/actions/runs/38789264534).
Its tree `dfa9e5cf5387b8bb85123c2fedad4be8fdbb4156` equals current main's tree.
The PR-only workflow has no separate squash-main run. The closed diagnostic
categories preserve rejection behavior and SOAP fault bytes; they are not a
TuneIn compatibility fix or an audio-output test.

The 20:03 UTC showcase preflight additionally completed a bounded production
Web-HTTP → existing test ctypes adapter → actual libmpv/null journey on this
main: original local stereo WAV decoding and advancing native time, favorite
save/play/recall, pause/resume, volume readback, a −2 dB preset and EQ bypass.
This is **REAL_SOFTWARE_TESTED**, not browser, production Unix IPC, live public
radio, physical DAC or user-heard acceptance. Actual listening remains the
showcase gate; see the [phone-first demo and fallback](demo.md).

## Manufacturer sources and dates

These are the retained manufacturer references from the earlier comparison,
not a new vendor survey. Publication or modification dates are not lookup dates.

- **[U]** [WiiM Ultra product specification](https://www.wiimhome.com/wiimultra/overview), checked 2026-10-14.
- **[F]** [Ultra firmware history](https://faq.wiimhome.com/en/support/solutions/articles/72000631650-wiim-ultra-firmware-release-notes), checked 2026-10-14; firmware versions above refer to that source snapshot.
- **[R]** [WiiM Radio in the mobile app](https://faq.wiimhome.com/en/support/solutions/articles/72000658778-introducing-wiim-radio/), recorded page modification date 2026-05-21.
- **[Q]** [Qobuz Connect official announcement](https://blog.wiimhome.com/post/qobuz-connect-available-on-wiim), published 2025-05-21.
- **[N]** [SMB sharing in WiiM Home](https://faq.wiimhome.com/en/support/solutions/articles/72000631971-adding-and-accessing-smb-shared-files-in-the-wiim-home-app), retained reference; no new lookup date asserted.
- **[A]** [RoomFit settings](https://faq.wiimhome.com/en/support/solutions/articles/72000647399), retained reference; no new lookup date asserted.

- **[S-EQ]** [Spotify PCM through existing ORR EQ](SPOTIFY_EQ.md), a separately activated existing-service or foreground candidate. No account, XU4/DAC or provider acceptance claimed.
