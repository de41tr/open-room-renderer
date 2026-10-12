# Open Room Renderer: manual browser acceptance

Status: **BROWSER_ACCEPTANCE_PENDING**. Automated checks cover JavaScript control flow and DOM-test-double event wiring; they do not establish real browser behavior, responsive layout, accessibility, audio output, or image loading.

Use the running renderer's existing HTTP port and open `/ui/` in a real browser on its trusted LAN. Do not expose the service to the internet. Use test audio and low volume for playback changes. No additional web server, framework, extension, or browser installation is required.

## Visual and keyboard checks

- At desktop width (about 1280 px), check the player/queue/favorites two-column layout. At 390 px and 320 px, check the stacked layout, readable text, touch controls, and absence of horizontal overflow.
- Tab from the skip link through controls. Confirm visible focus, native range arrow-key adjustment, labels, and status announcements without position updates flooding a screen reader.
- Open Add, Rename, and Delete dialogs. Confirm focus enters the dialog, Escape/Cancel closes it without sending a command, and Delete initially focuses Cancel. Check focus returns to the opening control.
- Load long titles and markup-like text; they must appear as text and wrap without creating markup. Missing/broken artwork must leave the record fallback. Artwork must send no referrer and must not fetch query/fragment-bearing URLs.

## Live controls

- Choose a favorite or queue entry. Verify title, artist, album, artwork, position, and phase against canonical renderer state. Click Play/Pause/Stop/Next/Previous and verify actual renderer state after the response; a request acknowledgement alone is not an audible-output guarantee.
- Drag seek and volume sliders. Polling must not wipe the draft. Choose Seek/Set volume once and verify the reported result. Cancel must send no command. Changing the track while seek is drafted must invalidate that old seek intent.
- Choose each repeat/shuffle mode, then Apply mode. Confirm the reported mode. Toggle mute and, when confirmed available, balance and EQ bypass. Unknown or pending gain/DSP values must not be shown as current values.
- Save current and add a direct HTTP(S) favorite. Verify saved rows, rename, play, and delete. Configured favorites must be playable but read-only. Keep an unsaved name/URL open through multiple polling cycles; text must stay intact.

## Interruptions and concurrency

- Delay a command response or disconnect after submission. The UI must say PENDING, then UNKNOWN if the response is lost. Inspect the browser network log: there must be no automatic repeated POST. The last received values must be marked stale on read failure.
- Double-click a mutation. Only one non-Stop request may be in flight. While it is pending, Stop must remain available; a second pending Stop must be suppressed. A delayed earlier response must not replace the newer Stop result.
- Reconnect and refresh. Current state may recover while the earlier unknown command outcome remains explicitly visible. Retrying a mutation must require an explicit user action.
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
  remains available during a pending apply, and that a lost response is never
  automatically retried. Physical/audible validation remains separate.
