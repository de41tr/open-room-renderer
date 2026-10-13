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

- Rename room: a persisted renderer/discovery name from an explicit dialog.
- [Inspect output](WEB_OUTPUT_INSPECTION.md): an inline, explicit read of running
  output configuration and two mpv properties, with identifiers hidden and no
  playback commands. Stop remains reachable while this non-modal read runs.
- Now Playing: safe title/artist/album, optional artwork, live-stream label,
  canonical playback phase and reported position/duration.
- Play, Pause, Stop, Next, Previous, bounded Seek, volume and mute.
- Current queue, explicit track selection, and the five supported modes:
  in order, repeat one, repeat all, shuffle once, shuffle with repeat all.
- Favorites: add a direct HTTP(S) URL, save current, play, rename, change URL
  and delete.
  Configuration-owned favorites remain read-only. Selecting a queue entry plays
  it; Next/Previous on a stopped queue preserve stopped selection semantics.
- Confirmed channel balance, EQ enable/bypass, all ten EQ bands and preset
  load/save/rename/delete.
- Stopped-only local JSON/M3U playlist loading or direct URL entry, plus append,
  remove and move for ordinary explicit queues.
- Private current-queue JSON export with a redacted review, explicit consent and
  a user-clicked download link; no automatic download or durable-save claim.

Sliders show a local draft and require an explicit Apply/Set/Seek action. Cancel
sends nothing. Polling does not overwrite an unfinished edit. The supported
shuffle modes do not imply an independent shuffle-plus-repeat-one combination.
Live sources without a seekable timeline do not offer Seek. Some native staged
queue states temporarily forbid changing mode; the UI follows those core gates.

### Room name

Choose **Rename room** below the room heading, enter a name, then **Save name**.
The existing name transaction saves `state_dir/device_name`, updates live naming
and invokes the existing discovery refresh. UUID, mDNS hostname, playback and
queue ownership do not change. The exact legacy name rule still applies:
nonempty after Python whitespace stripping, at most 128 Unicode characters, and
no control/surrogate/invalid XML characters. Typed percent sequences stay literal;
this JSON action never goes through the legacy URL/hex command decoder.

A draft is tied to its opening renderer session and an opaque name identity.
Fresh external name changes invalidate it; the server also checks that identity
inside the existing rename lock immediately before writing. Close and reopen to
review a changed name. Polling preserves unsent input; Cancel/Escape/navigation
scrub it. Dismissal does not cancel a rename already sent.

Only an acknowledged request followed by matching canonical name identity closes
the dialog. Names hidden by the existing private-text redaction can still be
verified without revealing them; their next edit starts with a blank field.
A failed disk-durability or discovery step can leave the new stored/live name in
place. **UNKNOWN remains visible even if the new name is read back**, and later
commands retain that warning. No automatic retry is sent. A deliberate same-name
retry uses the original transaction, including another discovery refresh.

The typed action is `device_rename` with `name` and `expected_device` (the opaque
`device.revision` from the opening snapshot), under the unchanged same-origin and
CSRF boundary. The ACK includes `device_revision` for canonical verification.
This dialog has focused HTTP/DOM-double coverage; the existing real-Chrome smoke
at the preceding checkpoint does not establish acceptance of this new dialog.

### Station recovery explanations

Now Playing adds a read-only explanation when the current station is actually
retrying: **N of 3 automatic retry attempts used**. The original load is not one
of those retries, and a used attempt is not a successful attempt. The existing
renderer owns this finite budget; brief success does not replenish it. Counts
appear only during a pending recovery owned by the current selection and
vanish on success, superseding selection/owner commands, or stale browser state.

After the budget is exhausted, choose **Play** for an explicit new attempt,
select another queue item, or choose **Stop**. A backend-control failure instead
warns that playback and silence are unconfirmed; request Stop and inspect the
reported state. Neither explanation asserts silence. Other or unrecognized
failures keep the generic PENDING/ERROR explanation. Earlier UNKNOWN request
outcomes and all existing action gates remain in force.

This is presentation only: no Retry action/endpoint, browser retry timer,
automatic skip, new source resolution or changed recovery policy. Only exact
fixed core error classifications and current selection-owned retry counters are
projected; raw errors, URLs and paths are never included in the recovery view.
Focused Python and existing DOM-double tests cover this addition; there is no
new dedicated real-browser/layout or native-audio acceptance claim.

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
Stop remains enabled during another pending request and does not require a
current queue generation. If a room-name, favorite, preset or playlist dialog is open,
first choose **Close** or **Cancel**, or press **Escape**, then choose **Stop**.
A native modal dialog makes background controls inert while it is open.
On dismissal, the controller returns keyboard focus to the usable opening
control or its current logical replacement, with Add/Presets/Refresh fallbacks.
Dismissing it clears the local draft without cancelling an already-sent request
or hiding its later outcome. Stop still requires backend acknowledgement/readback;
choosing it does not itself confirm silence.

When gain is UNKNOWN, **Set volume to 0** sends the existing typed volume
command without starting playback or changing an unsent slider draft. It is
available only with fresh state and no pending command. Current volume/mute
remain hidden until canonical readback confirms them; acknowledgement alone is
not confirmation of zero gain or audible silence. An unknown response remains
visible after later polls. A new explicit zero-volume choice is allowed when
fresh and idle, but there is no automatic retry. Stop remains independent.

Displayed queue actions carry the generation they were chosen from. Replacement,
selection or restart invalidates stale intent rather than interpreting an old
row as a new track. State and favorites reads also have to agree on the server
session before favorite controls become fresh. Closed dialogs clear entered
URLs. Navigation also closes and scrubs private room-name, favorite, preset and playlist
drafts before browser back/forward caching, without cancelling a command already
sent or erasing its later acknowledgement/unknown outcome.

Save current keeps the selection and renderer session from when its dialog
opened. A fresh observation of a different selection or session preserves the
entered name but disables submission, even if the selection later changes back.
Close the dialog, review the current track or stream, and reopen Save current.
A temporary read failure only disables controls until fresh reads recover; it
does not itself invalidate the draft. Pending and uncertain results stay visible,
and an already-sent save can still close after verified successful readback.
This is a local draft safeguard; the server's generation check still applies.

## Repair a saved favorite

Use **Change URL** on an editable managed favorite to replace a moved or expired
address without deleting its stable ID. The name and order remain unchanged;
configured favorites stay read-only. Enter the replacement in the initially blank
field and explicitly choose whether to **keep** or **clear** saved title, artist,
album and artwork. The stored URL is never loaded into this browser view.

The change affects future playback only. It neither fetches the new address nor
replaces the detached item already playing. Choose Play separately if you want
to test the repaired favorite. Submitted URL bytes are not normalized; existing
URL and complete request-size rules remain. No new API route or server behavior
is introduced.

After local validation, one send is admitted and the private input is cleared
and disabled. Each dialog opening sends at most once, including rejected or
UNKNOWN outcomes. Done/Close/Escape/navigation scrubs local references; it cannot
retract a sent request and does not promise secure memory erasure.

A successful response remains **ACKNOWLEDGED**. A refreshed hostname, name, ID or
cleared metadata cannot prove the exact saved URL in this redacted view. A lost
response remains **UNKNOWN** and may have taken effect; no retry is sent. If the
favorites/current-state reread fails, that limitation remains explicit. The
result stays open for review rather than auto-closing as confirmed. For stronger
exact-target response validation, use the existing `favorites set-url` CLI.

Rename, Delete and unsent Change URL drafts latch a review warning if fresh
polling observes the selected record changed or removed. Inputs are preserved,
but submission stays disabled until you close and reopen, even if the displayed
record later changes back. An unchanged persisted record remains usable after a
restart; Add is self-contained and Save current uses the opening selection/session
guard described above.
This is a browser safeguard only: favorites still use last-writer-wins updates,
so changes after the last read or URL-only repairs hidden by the redacted listing
may not be detected. It is not an atomic concurrency precondition.

### If the saved favorites library cannot be read

For the specific managed-library load failure, the favorites note now reports
that a read found the library unavailable, rather than implying the whole
renderer state failed. Previously displayed rows may be stale and all favorite
controls remain disabled; unrelated playback controls retain their normal gates.
No partial configured/managed list is presented as a healthy library.

A saved-favorites link whose target is missing now counts as unavailable
storage, rather than a new empty library. If a failed read cannot establish
that the file entry is genuinely absent, managed writes stay blocked for that
store's lifetime. A genuinely missing file still supports first-run saves.
This deliberate error-policy correction leaves valid-link loading and normal
successful atomic saves unchanged; it does not detect later external writers.

Stop the renderer **service** (not just audio playback), repair or restore its
saved favorites library from a known-good backup, then restart the service.
Do not delete/reset the library to force startup. The renderer preserves the
unavailable file and does not automatically repair or reload it. An ordinary
network failure still uses the generic stale-read message. Successful matching
state/favorites recovery clears the advice. The hint describes a failed read,
not a canonical snapshot of a newer server session.

The admitted `GET /orr/v1/web/favorites` now returns the fixed three-field
503 / ERROR envelope with `error: favorites_unavailable` for this one condition.
No raw exception, filename, path or URL is copied into it. Other reads and all
mutating error/UNKNOWN classifications retain their existing behavior.

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

If saved EQ presets could not be loaded, the editor now shows fixed recovery
guidance and disables Save/Rename/Delete. Confirmed active bands, balance, bypass
and built-in loading still work. Stop the renderer service, repair or restore the
saved library from a known-good backup, then restart; Refresh does not repair it.
The renderer does not overwrite/reset a failed-load library. Normal healthy
writes and existing committed-write uncertainty remain unchanged. See
[the exact degraded-storage contract](WEB_DSP_EDITOR.md#unavailable-saved-preset-storage).

See [the DSP editor contract](WEB_DSP_EDITOR.md) for precise numeric bounds,
opaque identifiers, locked revision checks and effect codes.

## Load and edit a stopped playlist

Choose **Stop** and wait for confirmed stopped state before **Load playlist**.
Select an authored JSON or simple M3U file, or paste direct HTTP(S) URLs. File
text is read locally; only the selected document/URLs are submitted to this
renderer after confirmation, never to an external service. JSON preserves its
stored name, selected index and mode. For M3U/direct URLs, choose those settings
in the dialog. The preview shows counts/settings, not private source URLs.

Replacement explicitly discards the old queue's metadata/history. It remains
stopped and fetches no media until you choose **Play**. Keep your original file:
the queue is in memory and is not a durable playlist library or automatic backup.
M3U support is the existing strict direct-URL subset, not HLS resolution; comment
and EXTINF display text are discarded. The entire encoded Web request is limited
to 32,768 bytes, so a larger offline CLI playlist may not fit in the browser.

For ordinary explicit queues, **Add URLs**, Remove and Move preserve canonical
metadata and the selected occurrence even when URLs repeat. Removing the selected
entry chooses its next neighbor, or the preceding final item. Removing the only
item is disabled. Moving uses final one-based positions. Every accepted edit
resets position/shuffle traversal through existing queue replacement behavior;
there is no seamless live reorder. Provider/radio-derived queues are deliberately
read-only here, although a stopped queue can be replaced with a new explicit one.

Changes from another controller or a server restart make old rows/drafts stale.
Review fresh state and reopen the dialog; the UI does not silently rebase an old
edit, chain it after Stop, or retry an unknown response. Cancel, Escape and page
navigation clear private file/URL drafts. Closing cannot retract a sent command.
See [the bounded playlist contract](WEB_PLAYLISTS.md) for exact formats/admission.

## Export a private queue file

Choose **Export JSON**, review the redacted queue summary, and explicitly accept
that the file contains original source URLs and the queue name. Select **Prepare
private file**, then the **Download JSON** link. Check your browser's downloads;
a ready file/link does not prove a durable or private filesystem save.

The unchanged authored JSON codec preserves order, duplicates, selected index
and mode, but omits metadata, playback/shuffle history and provider/account state.
The exact file must also fit the existing Web import envelope. Some valid CLI
queues cannot fit; use `playlist save-current FILE` rather than expect truncation.
Favorites and M3U export are not included. Export neither stops playback nor
fetches media. Close/Cancel/Escape/navigation discards private browser references;
it cannot retract an already downloaded file. See [the private export contract](WEB_PLAYLIST_EXPORT.md).

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

A separate [real-Chrome CI smoke](WEB_BROWSER_ACCEPTANCE.md#reproducible-real-browser-smoke)
now exercises desktop/mobile-emulated rendering, navigation, export-dialog
keyboard/focus behavior, actual JSON download bytes and UI Play/Pause through
production HTTP/Unix IPC to mpv/null. Its exact-revision CI result and artifacts,
not these test descriptions, establish whether that bounded smoke passed.

**BROWSER_ACCEPTANCE_PENDING:** the broader manual matrix, including image
loading, screen readers, zoom, other browser engines and interruption scenarios,
remains separate. Follow [the acceptance checklist](WEB_BROWSER_ACCEPTANCE.md)
and record browser, viewport and exact server revision when performing it.

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
