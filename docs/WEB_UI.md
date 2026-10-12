# Local Web controller

The renderer now serves a lightweight browser controller on its **existing HTTP
listener**. There is no second web server, new port, framework, CDN or remote
script dependency. Start the renderer normally, then open:

```text
http://<renderer-ip>:8080/ui/
```

Use your configured `http_api_port` if different. `/` also serves the controller.
On the renderer itself, a loopback-bound/default installation can use
`http://localhost:8080/ui/`. Use the renderer's literal IP or its exact enabled
mDNS hostname; arbitrary DNS aliases and reverse-proxy/forwarded-host origins
are intentionally not accepted by the browser boundary. No browser extension,
Node runtime or frontend build step is needed to use it. Packaged installations
include the four static assets.

## Controls

- Now Playing: safe title/artist/album, optional artwork, live-stream label,
  canonical playback phase and reported position/duration.
- Play, Pause, Stop, Next, Previous, bounded Seek, volume and mute.
- Current queue, explicit track selection, and the five supported modes:
  in order, repeat one, repeat all, shuffle once, shuffle with repeat all.
- Favorites: add a direct HTTP(S) URL, save current, play, rename and delete.
  Configuration-owned favorites remain read-only. Selecting a queue entry plays
  it; Next/Previous on a stopped queue preserve stopped selection semantics.
- Confirmed channel balance, EQ enable/bypass, all ten EQ bands and preset
  load/save/rename/delete. Playlist import/edit/export is a separate increment.

Sliders show a local draft and require an explicit Apply/Set/Seek action. Cancel
sends nothing. Polling does not overwrite an unfinished edit. The supported
shuffle modes do not imply an independent shuffle-plus-repeat-one combination.
Live sources without a seekable timeline do not offer Seek. Some native staged
queue states temporarily forbid changing mode; the UI follows those core gates.

### Honest state and uncertain commands

An HTTP acknowledgement says the request returned, not that music is audible.
The UI rereads canonical renderer state after every command and never invents
successful playback, gain or DSP values.

- **PENDING:** a command or backend transition is still awaiting confirmation.
- **UNKNOWN:** a response/read was lost or a setting is not confirmed. A previous
  value may be shown only as stale; it is not silently restored as current truth.
- **ERROR:** the renderer reports a failure. This does not guarantee silence.
- **CONFIRMED:** current renderer observation/readback is available, within the
  same software scope as its existing controls.

Mutations are single-shot. A timeout may occur after the command took effect;
there is no automatic retry. Inspect current state before explicitly trying
again. Earlier unknown outcomes remain visible even after a later read succeeds.
Stop stays available during another pending request and without a stale queue
generation, so it can serve as explicit recovery. It is still a checked backend
operation, not a promise of silence before acknowledgement/readback.

Displayed queue actions carry the generation they were chosen from. Replacement,
selection or restart invalidates stale intent rather than interpreting an old
row as a new track. State and favorites reads also have to agree on the server
session before favorite controls become fresh. Closed dialogs clear entered
URLs; closing a dialog does not cancel a command already sent.

## Ten-band EQ and presets

Open **Sound → Edit bands** to change frequency, gain, Q and filter type in the
active Global/Wi-Fi context. **Apply curve** sends all ten bands together;
**Discard edits** restores the current confirmed view. Start at low volume:
positive boosts can clip. The editor retains the backend's exact ranges and
six supported modes; pass filters still affect sound at zero gain.

Applying bands preserves the current EQ enable/bypass setting. **Load** enables
EQ and applies the selected preset. **Save active curve** saves only confirmed
settings already applied on the renderer, never an unsent draft. Built-in
presets are read-only; save under a new name rather than silently overwrite.
Rename/delete of a preset shared by multiple stored plugin contexts explicitly
warns and requires confirmation that all those contexts are affected.

If another controller changes the active curve, source, name or library while
you edit, the original revision becomes stale. Review the current state and
make a fresh choice; the UI does not automatically rebase/retry the draft.
A restart also invalidates previous editor IDs. Pending/unconfirmed DSP hides
editor values rather than presenting desired settings as current.

The renderer distinguishes backend-applied persistence failure, library
replacement with unconfirmed durability, and a post-application follow-up whose
outcome is unknown. For example, actual DSP can be applied even though saving
restart settings failed. Keep the warning and reread the current state; do not
infer rollback or click repeated automatic retries. The existing lock/shield
keeps each transaction intact if its browser request is interrupted.

See [the DSP editor contract](WEB_DSP_EDITOR.md) for precise numeric bounds,
opaque identifiers, locked revision checks and effect codes.

## Browser security and privacy scope

New `/orr/v1/web/*` mutations require the exact admitted Host/listener port,
same Origin, process-local CSRF token in a custom header and bounded strict
UTF-8 JSON. Cross-site Fetch Metadata, duplicate/unknown fields, nonfinite
numbers, compressed/chunked bodies and arbitrary legacy commands are rejected.
No CORS access is granted. Forwarded headers do not establish browser authority.
Ordinary external links may navigate to the inert top-level HTML page; that
exception does not allow cross-site data reads, scripts, frames or mutations.
The CSP permits only local script/style/connect resources, refuses framing, and
uses no-referrer/no-store/nosniff headers. Static file access uses an exact map.

State JSON is an explicit projection. It excludes current/resolved/backend media
URLs, DIDL, queue XML and provider account data. Known URL-derived titles and
encoded URL wrappers are withheld; safe live/native metadata is retained.
Artwork URLs containing userinfo, query strings or fragments are withheld and
fall back to the locally drawn record image. Safe direct artwork can contact its
image host from the browser without a referrer; image loading and any redirect
are browser-managed. This is conservative URL hygiene, not general-purpose DLP.
A new favorite's original URL is submitted in JSON and stored by the existing
local library; it is not echoed into the safe browser list. Do not share signed
media URLs or private state files publicly.

**These checks are not authentication for the renderer.** Existing HTTP GET
mutations, SOAP, favorites and other legacy APIs remain unauthenticated; their
older browser/cross-site exposure is not repaired by this Web boundary. Anyone
who can reach those controls can still control the renderer. Keep the entire
service on a trusted isolated LAN, with no public port forwarding. Do not put it
on the Internet or claim the Web UI makes an untrusted network safe.

## Verification and remaining acceptance

The implementation has real loopback HTTP/MOCK tests, independent security
review, dependency-free JavaScript/DOM-test-double control-flow checks, and a
short actual libmpv/null Web control flow. The existing digital-PCM DSP gates
remain separate. The native Web flow does not operate a real browser or a DAC.

**BROWSER_ACCEPTANCE_PENDING:** actual responsive layout, keyboard/focus behavior,
image loading and browser interruption flows have not been accepted in a real
browser. A supported browser cannot reach the current development fixture; this
is not a failed product-browser test and is not disguised as a pass. The complete
implemented controller can be reproduced locally using the URL above. Follow
[the manual acceptance checklist](WEB_BROWSER_ACCEPTANCE.md) and record browser,
viewport and exact server revision when performing it.

**HARDWARE_PENDING:** physical ARMv7/Odroid XU4, DAC/acoustic sound, physical LAN
and original WiiM Home app remain untested. See [testing scope](TESTING.md) and
[compatibility limits](COMPATIBILITY_MATRIX.md).

## Native installation

The native installer copies the four packaged Web assets into its owned source
snapshot, includes them in source/ownership hashes and installs them into the
venv. Use a newly built installation for this branch; an older installed prefix
does not acquire UI assets merely because the checkout changed. The normal
owned-uninstall rules preserve modified assets and state. Installation acceptance
checks actual `/ui/` and static-resource responses from the isolated executable.
