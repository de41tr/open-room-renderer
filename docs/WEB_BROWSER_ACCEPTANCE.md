# Open Room Renderer: browser acceptance

Status: **BROWSER_ACCEPTANCE_PENDING** for the full manual matrix below. A small
real-Chrome smoke now has its own CI job; treat it as passed only when the
**Real Chrome / desktop and mobile smoke** job succeeds for the exact revision.
The older JavaScript/DOM-double tests remain separate and cannot satisfy it.

## Reproducible real-browser smoke

On an unprivileged Linux environment with working loopback/Unix sockets, installed
Google Chrome, a matching ChromeDriver, mpv, and the project's Python dependencies
plus pytest:

```sh
PYTHONPATH=. python tools/browser_smoke.py --artifacts /tmp/orr-browser-evidence-NEW
```

The output directory must not already exist. No browser/framework download,
account, external media, relaxed sandbox, CORS change or public listener is used.
CI uses the preinstalled Chrome/ChromeDriver on `ubuntu-24.04`; a missing browser,
unavailable sandbox or failed native playback is a failure, never a skipped or
mocked pass. The isolated job has an eight-minute cap; the smoke has a 120-second
work deadline and the existing 150-second process-tree guard. Existing native
jobs and their budgets/assertions are unchanged.

The smoke drives genuine W3C element clicks and keyboard actions through existing
`aiohttp`, with no Selenium/Playwright dependency. It checks:

- Desktop 1280-pixel and mobile-emulated 390-pixel rendering, actual column geometry
  and page overflow, plus home-link navigation and browser Back/Forward.
- The export dialog's Tab/Shift+Tab/Escape handling, opener focus restoration,
  fresh unchecked consent, and cleanup on close and navigation/history restore.
- UI Play/Pause through the real HTTP handler and unchanged production Unix IPC
  backend; real mpv reports selected PCM audio, advancing time, `ao=null`, and
  paused readback. The UI must display the renderer-confirmed phase.
- The actual browser-generated download, including filename, complete disk bytes,
  canonical JSON contents and equality to the renderer response hash. Opening or
  preparing the export must leave the download directory empty until Download;
  exporting must not send playback commands or request more media.
- Browser severe console errors (including CSP failures) as failures.

The `browser-smoke-<revision>` artifact retains desktop/mobile PNGs, downloaded
synthetic JSON, source hashes, browser/driver/mpv versions, native readback and
HTTP evidence on success. Failure also attempts a current screenshot, DOM and
console capture and retains diagnostic logs. Only disposable generated fixture
state is used. Profiles, account data and real user playlists are not uploaded.

This is Chromium-based Chrome desktop/mobile emulation, not Firefox, physical
mobile hardware, audible output, a DAC, or a complete accessibility/visual matrix.
The generated 30-second local WAVs are synthetic media; the browser, HTTP server,
production IPC backend and mpv are real. The following manual checks, especially
320-pixel/zoom/screen-reader/interruption coverage, remain outstanding.

Use the running renderer's existing HTTP port and open `/ui/` in a real browser on its trusted LAN. Do not expose the service to the internet. Use test audio and low volume for playback changes. No additional web server, framework, extension, or browser installation is required.

## Visual and keyboard checks

- At desktop width (about 1280 px), check the player/queue/favorites two-column layout. At 390 px and 320 px, check the stacked layout, readable text, touch controls, and absence of horizontal overflow.
- Tab from the skip link through controls. Confirm visible focus, native range arrow-key adjustment, labels, and status announcements without position updates flooding a screen reader.
- Open Add, Rename, and Delete dialogs. Confirm focus enters the dialog, Escape/Cancel closes it without sending a command, and Delete initially focuses Cancel. Check focus returns to the opening control.
- Rename a favorite and confirm focus returns to its rebuilt Rename action;
  delete one and confirm focus returns to Add. Repeat Cancel/Escape after another
  controller redraws the list. Preset rename/delete should return to Presets when
  the original action becomes disabled. If a playlist/export opener becomes
  unavailable, dismissal should return to Refresh. Hidden/inert/collapsed targets
  must not be selected. Immediate reopen, navigation and late responses must not
  steal newer focus. These are real-browser checks, not inferred from DOM doubles.
- Load long titles and markup-like text; they must appear as text and wrap without creating markup. Missing/broken artwork must leave the record fallback. Artwork must send no referrer and must not fetch query/fragment-bearing URLs.

## Live controls

- Choose a favorite or queue entry. Verify title, artist, album, artwork, position, and phase against canonical renderer state. Click Play/Pause/Stop/Next/Previous and verify actual renderer state after the response; a request acknowledgement alone is not an audible-output guarantee.
- Drag seek and volume sliders. Polling must not wipe the draft. Choose Seek/Set volume once and verify the reported result. Cancel must send no command. Changing the track while seek is drafted must invalidate that old seek intent.
- Choose each repeat/shuffle mode, then Apply mode. Confirm the reported mode. Toggle mute and, when confirmed available, balance and EQ bypass. Unknown or pending gain/DSP values must not be shown as current values.
- After gain confirmation fails, use Stop and verify STOPPED with gain still UNKNOWN. Choose Set volume to 0 once: current gain must stay unconfirmed until readback, and Stop must remain available while pending. Check that zero readback restores the slider without starting playback or erasing a slider draft. Lost/rejected responses must remain honest, with no automatic retry; stale state disables this recovery. Repeat across a renderer restart and delayed response. This verifies controls, not audible silence.
- Save current and add a direct HTTP(S) favorite. Verify saved rows, rename, play, and delete. Configured favorites must be playable but read-only. Keep an unsaved name/URL open through multiple polling cycles; text must stay intact.

## Interruptions and concurrency

- Delay a command response or disconnect after submission. The UI must say PENDING, then UNKNOWN if the response is lost. Inspect the browser network log: there must be no automatic repeated POST. The last received values must be marked stale on read failure.
- Double-click a mutation. Only one non-Stop request may be in flight. While it is pending, close any open modal with Close/Cancel/Escape, then use Stop; dismissal must not cancel the sent request. A second pending Stop must be suppressed. A delayed earlier response must not replace the newer Stop result.
- Reconnect and refresh. Current state may recover while the earlier unknown command outcome remains explicitly visible. Retrying a mutation must require an explicit user action.
- With Save current open and an edited name, let the selected track/stream change
  through another controller or automatic advancement. After a fresh poll, the
  name must remain, Save must be disabled, and guidance must say to close, review
  the current selection and reopen Save current. Returning to the old selection
  must not re-enable that draft. A failed read alone must recover without this
  warning. Repeat while a save is pending: PENDING and any later ACK/UNKNOWN
  outcome must remain honest, with no retry; verified successful saves may close.
  Close/reopen and Back/Forward must clear the old draft and warning.
- Restart the renderer while seek/mode/save-current is drafted. The previous session's draft must be rejected. If state and favorites responses straddle the restart, favorite controls must remain stale/disabled until their session IDs agree.
- Confirm a stale queue selection is rejected instead of playing a different entry after replacement. Read failures or another open controller's changes must not silently become optimistic success.

Record browser/version, viewport, server revision, passes/failures, and screenshots only after actually performing these checks. Audio/hardware validation remains a separate gate.

## DSP editor addition

- Repeat layout checks at 320, 375, 768 and 1440 px and 200% zoom. Band controls
  should remain reachable without page-wide overflow; keyboard navigation and
  collapsed Edit bands details must work in the real browser.
- Edit several bands while polling continues, then Apply or Discard. Verify
  readback and actual sound separately. Applying while bypassed must not enable
  EQ; loading a preset must clearly enable it.
- Save the active curve, rename it and delete it. Built-ins must remain read-only.
  Shared-context warnings and explicit confirmation must be visible before
  whole-preset rename/delete. Saving an unsent draft must be unavailable.
- Change DSP from another controller while a draft/dialog is open. Old revision
  submissions must reject without silently rebasing. Restart, Escape and Cancel
  must not resurrect old drafts or let a late response wipe newer inputs.
- Check that PENDING/UNKNOWN hides unconfirmed current editor values, that Stop
  remains enabled during a pending apply (close any modal first), and that a lost response is never
  automatically retried. Physical/audible validation remains separate.

## Stopped playlist workflow

- At narrow/mobile widths and 200% zoom, open Load playlist and Add URLs. Check
  labels, native file selection, focus return, keyboard movement and wrapping.
- Load a small authored JSON, a simple M3U and pasted direct URLs. The preview
  must show only safe counts/settings. Explicitly acknowledge replacement; the
  renderer must stay stopped and make no media request until Play.
- Verify JSON-owned settings, M3U/URL settings, duplicate occurrence selection,
  append, remove and move. Metadata must survive ordinary edits. Removing the
  selected last item chooses its predecessor; removing the only item is disabled.
- Confirm playing/paused/pending or provider/radio queues do not offer unsafe
  edits. Stop is a separate explicit action, not an automatic import prelude.
- Test malformed JSON, duplicate JSON fields, invalid UTF-8, JSON BOM, valid M3U
  BOM, large files and escaped request sizes exceeding 32 KiB. Errors must not
  echo filenames, original file text, private URLs or query tokens.
- Change selection/queue in another controller, restart the renderer, and flip
  capabilities while a dialog/row is visible. Old intent must stay stale rather
  than become actionable after a later poll. Detached old row handlers must fail.
- Close and immediately reopen each favorite, DSP and playlist dialog. A delayed
  previous close event must not erase the new draft. Cancel/Escape/navigation
  must still clear private input, including a late file-read result and browser
  back/forward-cache restoration.
- Lose the mutation response and verify sticky UNKNOWN with no automatic POST
  retry; inspect canonical state before making a new explicit choice.

## Private queue JSON download

- In a real browser, open Export JSON at desktop/mobile widths and 200% zoom.
  Verify focus, wrapping and initially unchecked privacy consent. The summary
  must not reveal source URLs or a URL-derived queue name.
- Prepare a file only after consent. Verify no automatic download occurs and
  the genuine Download JSON link works under the shipped CSP. Check the actual
  downloaded filename and bytes, then load that JSON while stopped; order,
  duplicate selection and mode must match. Keep the test file private.
- Check Cancel/Escape/Close, page navigation/back-forward restoration, restart,
  queue changes and delayed responses. Owned Blob URLs must be revoked and old
  files/consent must not reappear; no playback command outcome may be overwritten.
- Interrupt the private read and try unsupported/oversized queues. Fixed errors
  must not reveal sources; no automatic retry or truncation may occur. Playback
  and Stop remain independent (close the modal before choosing Stop).
- A Blob/link click is not evidence of a filesystem save. Record actual browser
  version, downloaded bytes and interaction results before closing this gate.

## Favorite address repair and observed conflicts

- At narrow widths and 200% zoom, verify the extra Change URL row action, wrapping,
  native URL input, required metadata choice, dialog scrolling and keyboard focus.
  Configured favorites must have no Change URL action.
- Open Change URL: its field must be blank and keep/clear must require an explicit
  choice. Submit a same-host replacement. Input should clear immediately; the
  result must stay ACKNOWLEDGED, never exact-target CONFIRMED. No request may fetch
  the replacement or change active playback. Check the stored result separately.
- Repeat both metadata policies. Lose a response or its readback, then try repeated
  Enter/clicks: one opening must send at most once and UNKNOWN must stay honest.
  Done/Close/Escape/navigation and late responses must not restore private input
  or erase a newer dialog's draft. Check actual focus return on the rebuilt row.
- While Rename/Delete/unsent Change URL is open, rename/remove that record from
  another controller. Fresh observation must preserve the draft but disable its
  stale submission. Changing it back must not silently re-enable it. Reopen to
  review. This does not prove atomic protection against unobserved URL changes.

## Saved-favorites failure guidance

- Use disposable test state, never corrupt a real library. Start with a malformed
  or unreadable managed test library. Verify specific failed-read/restart guidance,
  stale or empty rows and disabled favorite controls; ordinary playback controls
  should retain their normal availability. No private contents/path may appear.
- Disconnect networking and verify the generic stale-read message replaces the
  repair hint. Stop the test renderer service, restore known-good test state and
  restart: valid matching reads should clear the hint and restore controls.
  A browser Refresh alone must not pretend to repair or reload saved state.

## Saved EQ library preservation

- Use disposable test state with malformed saved presets and a valid independent
  active-DSP snapshot. Confirm current DSP can remain CONFIRMED, recovery guidance
  is visible, and saved-library writes are disabled. Band edits, balance, bypass
  and built-in loading must retain their normal controls; use low/test volume.
- Open a preset draft while healthy, then observe a library-unavailable state in
  the test fixture. The draft must not silently regain write capability or claim
  an acknowledged save was verified. Check keyboard/focus and narrow layouts.
- Stop the test service, restore a known-good test library and restart. Healthy
  controls should return after fresh state. Do not damage a real library or infer
  filesystem/crash guarantees from browser state. Legacy write rejection and
  exact byte preservation have separate synthetic/native acceptance evidence.
