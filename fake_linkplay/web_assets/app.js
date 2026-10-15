/* Same-origin UI. Canonical snapshots are the only source of displayed state. */
(function () {
  "use strict";
  const {streamTitleText, FAVORITES_EXPORT_FILENAME, FAVORITES_TRANSFER_CHANGED, favoritesRestoreBody, validateFavoritesExportSummary, validateFavoritesExportDocument, favoritesTransferFailure, FavoritesRestoreDraft, CURVE_EXPORT_FILENAME, CURVE_TRANSFER_CHANGED, curveImportBody, validateCurveExportSummary, validateCurveExportDocument, curveTransferFailure, CurveImportDraft, OutputInspection, outputInspectionLines, Controller, EXPORT_FILENAME, exportIdentity, exportObject, validateExportSummary, validateExportDocument, exportFailure, readExportResponse, PlaylistDraft, MAX_REQUEST_BYTES, requestBytes, playlistAvailable, pastedURLs, playlistPreview, DspDraft, DSP_KEYS, DSP_LIBRARY_UNAVAILABLE_MESSAGE, MODES, safeAudioURL, safeArtwork, formatTime, guardedAction} = window.RendererControl;
  const $ = id => document.getElementById(id);
  const drafts = {seek: false, volume: false, mode: false, balance: false};
  let snapshot = null, queueSignature = "", favoritesSignature = "", artworkURL = null;
  let dialogContext = null, dialogSerial = 0, pollTimer = null, pollRun = 0;
  const soundDraft = new DspDraft();
  let playlistDraft = null, queueEditing = null, exportContext = null, roomContext = null, curveExportContext = null, curveImportDraft = null, favoritesExportContext = null, favoritesRestoreDraft = null;
  const queueIntents = new WeakMap();
  let queueReview = 0;
  let presetSignature = "", presetContext = null, presetSerial = 0, soundSubmission = null;
  const text = (id, value) => { const element = $(id); if (element.textContent !== value) element.textContent = value; };
  const balanceLabel = value => value === 0 ? "Center" : `${Math.round(Math.abs(value) * 100)}% ${value < 0 ? "left" : "right"}`;
  const working = key => drafts[key] || document.activeElement === $(key);
  // Return focus during this dismissal, never from a queued close event or a
  // later response. Rebuilt favorite controls are resolved only in their session.
  function dialogLifecycle(id, clear, fallback = null) {
    const dialog = $(id);
    let returnFocus = null;
    function close(restore = true) {
      const target = returnFocus, wasOpen = dialog.open;
      returnFocus = null;
      clear();
      if (!wasOpen) return;
      dialog.close();
      if (restore === false || !target || ["favorite-dialog", "dsp-dialog", "playlist-dialog", "export-dialog", "room-dialog", "curve-export-dialog", "curve-import-dialog", "favorites-export-dialog", "favorites-restore-dialog"].some(other => $(other).open)) return;
      const available = item => item?.isConnected && !item.disabled && !item.closest("[hidden], [inert], details:not([open])");
      const replacement = target.favoriteKey && target.session === snapshot?.session_id
        ? Array.from($("favorites-list").querySelectorAll("button")).find(button => button.dataset.favoriteKey === target.favoriteKey) : null;
      [target.opener, replacement, fallback && $(fallback), $("refresh")].find(available)?.focus({preventScroll: true});
    }
    dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
    // Native close events are queued: an old event must not clear a reopened
    // draft, restore old focus, or retain the old opening control.
    dialog.addEventListener("close", () => { if (!dialog.open) { returnFocus = null; clear(); } });
    return {
      open(opener) {
        if (dialog.open) return;
        returnFocus = {opener, favoriteKey: opener?.dataset.favoriteKey, session: snapshot?.session_id};
        dialog.showModal();
      },
      close
    };
  }
  const favoriteDialog = dialogLifecycle("favorite-dialog", clearFavoriteForm, "add-favorite");
  const presetDialog = dialogLifecycle("dsp-dialog", clearPresetForm, "dsp-preset");
  const playlistDialog = dialogLifecycle("playlist-dialog", clearPlaylist);
  const exportDialog = dialogLifecycle("export-dialog", clearExport);
  const curveExportDialog = dialogLifecycle("curve-export-dialog", clearCurveExport, "dsp-export");
  const curveImportDialog = dialogLifecycle("curve-import-dialog", clearCurveImport, "dsp-import");
  const favoritesExportDialog = dialogLifecycle("favorites-export-dialog", clearFavoritesExport, "favorites-backup");
  const favoritesRestoreDialog = dialogLifecycle("favorites-restore-dialog", clearFavoritesRestore, "favorites-restore");
  const roomDialog = dialogLifecycle("room-dialog", clearRoomForm, "rename-room");
  async function request(path, options) {
    const abort = new AbortController();
    const timer = window.setTimeout(() => abort.abort(), options.timeoutMs || (options.method === "POST" ? 20000 : 8000));
    try {
      const response = await fetch(path, {
        method: options.method, credentials: "same-origin", mode: "same-origin", redirect: "error", cache: "no-store",
        headers: {"Accept": "application/json", ...(options.method === "POST" ? {"Content-Type": "application/json"} : {}), ...options.headers},
        body: options.body ? JSON.stringify(options.body) : undefined, signal: abort.signal, referrerPolicy: "no-referrer"
      });
      if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get("Content-Type") || "")) throw new Error("Unexpected response");
      const size = Number(response.headers.get("Content-Length"));
      if (size > 2 * 1024 * 1024) throw new Error("Response too large");
      const reader = response.body.getReader(), chunks = [];
      let total = 0;
      while (true) { const {done, value} = await reader.read(); if (done) break; total += value.length; if (total > 2 * 1024 * 1024) { await reader.cancel(); throw new Error("Response too large"); } chunks.push(value); }
      const bytes = new Uint8Array(total); let offset = 0;
      chunks.forEach(chunk => { bytes.set(chunk, offset); offset += chunk.length; });
      const source = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
      return {status: response.status, body: JSON.parse(source),
        ...(response.status === 409 && ["playlist_remove", "playlist_move"].includes(options.body?.action) ? {rawBody: source} : {})};
    } finally { window.clearTimeout(timer); }
  }
  const control = new Controller(request, render);
  const outputInspection = new OutputInspection(request, renderOutputInspection);
  function renderOutputInspection() {
    outputInspection.observe(control.state);
    $("inspect-output").disabled = !outputInspection.canInspect(control.state);
    $("output-inspection-detail").hidden = !outputInspection.open;
    text("output-inspection-status", outputInspection.pending ? "Inspecting output once… Playback controls, including Stop, remain available." : outputInspection.error || (outputInspection.result ? `Inspection ${outputInspection.result.status}. This is a point-in-time observation.` : ""));
    const items = outputInspection.result ? outputInspectionLines(outputInspection.result).map(line => {
      const item = document.createElement("li"); item.textContent = line; return item;
    }) : [];
    $("output-inspection-results").replaceChildren(...items);
  }
  $("inspect-output").addEventListener("click", () => outputInspection.inspect(control.state));
  $("dismiss-output-inspection").addEventListener("click", () => { outputInspection.dismiss(); renderOutputInspection(); $("inspect-output").focus({preventScroll: true}); });
  function noticeView() {
    let {kind, text: message} = control.notice;
    if (!control.fresh && !control.busy) { if (kind !== "acknowledged") kind = "unknown"; message = "Connection lost or state unavailable. Displayed values are stale. " + (control.operation ? message : "Refresh to reconnect."); }
    else if (kind === "confirmed" && snapshot?.transport.status === "PENDING") { kind = "pending"; message = snapshot.transport.message || "The renderer is still preparing playback."; }
    else if (kind === "confirmed" && snapshot?.transport.status === "ERROR") { kind = "error"; message = snapshot.transport.message || "The renderer reported a playback error. This does not confirm that audio has stopped."; }
    if (control.unknownOutcomes.length && control.notice.kind !== "unknown") message += ` Earlier request outcome unknown: ${control.unknownOutcomes.slice(-3).map(item => item.label).join(", ")}. Check state before repeating it; no automatic retry was sent.`;
    $("notice").dataset.kind = kind;
    text("notice-kind", kind.toUpperCase()); text("notice-text", message);
    text("connection-label", control.fresh ? "Connected" : control.busy ? "Awaiting state" : snapshot ? "State unavailable" : "Connecting");
    $("connection-dot").dataset.kind = control.fresh ? "confirmed" : "unknown";
  }
  function render() {
    snapshot = control.state;
    noticeView();
    renderOutputInspection();
    $("refresh").disabled = false;
    if (!snapshot) return;
    const {media, transport, gain, queue, dsp} = snapshot;
    const ready = control.canMutate("play");
    text("device-name", snapshot.device.name || "Listening room");
    renderRoomRename();
    text("track-title", media.title || (queue.count ? "Untitled track" : "Ready when you are"));
    const onAir = streamTitleText(snapshot, control.fresh);
    text("stream-title", onAir);
    $("stream-title").hidden = !onAir;
    text("track-artist", media.artist || (queue.count ? "Artist unavailable" : "Choose something from your favorites."));
    text("track-album", media.album);
    text("source-label", media.is_live ? "LIVE STREAM" : "OPEN ROOM");
    const phaseLabel = transport.status === "ERROR" ? "ERROR" : transport.status === "PENDING" && ["PLAYING", "PAUSED", "STOPPED"].includes(transport.phase) ? "PENDING" : transport.phase.replaceAll("_", " ");
    text("playback-state", control.fresh ? phaseLabel : "UNKNOWN · STALE");
    $("playback-state").dataset.kind = control.fresh ? transport.status.toLowerCase() : "unknown";
    // A fresh explanation supplements, never replaces, uncertain request outcomes.
    const recovery = control.fresh ? transport.recovery : null;
    $("playback-recovery").hidden = !recovery;
    text("playback-recovery", recovery?.message || "");
    const image = safeArtwork(media.artwork);
    if (image !== artworkURL) {
      artworkURL = image;
      $("artwork").hidden = true; $("artwork-fallback").hidden = false;
      if (image) $("artwork").src = image;
      else $("artwork").removeAttribute("src");
    }
    text("position", formatTime(media.position_seconds));
    text("duration", media.is_live ? "LIVE" : formatTime(media.duration_seconds));
    const canSeek = ready && snapshot.actions.seek && media.duration_seconds > 0;
    $("seek").disabled = !canSeek;
    text("seek-hint", media.is_live ? "Live stream" : canSeek ? "Drag, then choose Seek" : "Seeking unavailable");
    if (!working("seek")) { $("seek").max = String(Math.max(1, media.duration_seconds || 1)); $("seek").value = String(media.position_seconds || 0); }
    if (drafts.seek && $("seek").dataset.generation !== String(snapshot.generation)) {
      drafts.seek = false; $("seek-draft").hidden = true;
      $("seek").max = String(Math.max(1, media.duration_seconds || 1)); $("seek").value = String(media.position_seconds || 0);
      text("seek-hint", "Track changed. Choose the position again.");
    }
    $("seek-submit").disabled = !canSeek;
    ["play", "pause", "next", "previous"].forEach(action => { $(action).disabled = !ready || !snapshot.actions[action]; });
    // Stop is intentionally independent of readiness, generation and other pending requests.
    $("stop").disabled = !control.canMutate("stop");
    $("volume").disabled = !ready || gain.status !== "CONFIRMED";
    $("volume").hidden = gain.status !== "CONFIRMED";
    $("volume-submit").disabled = !ready;
    $("volume-recovery").hidden = gain.status !== "UNKNOWN";
    $("volume-zero").disabled = gain.status !== "UNKNOWN" || !control.canMutate("volume");
    const gainKnown = gain.status === "CONFIRMED" && gain.volume !== null && gain.mute !== null;
    text("volume-current", gainKnown ? `${gain.volume}%${gain.mute ? " · muted" : ""}${control.fresh ? "" : " · stale"}` : gain.status);
    if (!working("volume") && gainKnown) $("volume").value = String(gain.volume);
    $("mute").disabled = !ready || !gainKnown;
    $("mute").setAttribute("aria-pressed", gainKnown ? String(gain.mute) : "false");
    text("mute", !gainKnown ? "Unavailable" : gain.mute ? "Unmute" : "Mute");
    $("mode").disabled = !ready || !snapshot.actions.queue_mode;
    $("mode-submit").disabled = !ready || !snapshot.actions.queue_mode;
    if (!working("mode")) $("mode").value = queue.mode;
    text("mode-current", `Current: ${MODES[queue.mode]}${control.fresh ? "" : " (stale)"}`);
    $("sound-panel").hidden = false;
    const dspKnown = dsp.status === "CONFIRMED";
    text("balance-current", dspKnown && dsp.balance !== null ? `${balanceLabel(dsp.balance)}${control.fresh ? "" : " · stale"}` : dsp.status);
    if (!working("balance") && dspKnown && dsp.balance !== null) $("balance").value = String(dsp.balance);
    $("balance").disabled = !ready || !dspKnown || dsp.balance === null;
    $("balance").hidden = !dspKnown || dsp.balance === null;
    $("balance-scale").hidden = !dspKnown || dsp.balance === null;
    $("balance-submit").disabled = !ready || !dspKnown || dsp.balance === null;
    text("eq-current", dspKnown && dsp.eq_enabled !== null ? `${dsp.eq_enabled ? "Enabled" : "Bypassed"}${dsp.name ? ` · ${dsp.name}` : ""}${control.fresh ? "" : " · stale"}` : dsp.status);
    text("eq", !dspKnown || dsp.eq_enabled === null ? "EQ unavailable" : dsp.eq_enabled ? "Bypass EQ" : "Enable EQ");
    $("eq").setAttribute("aria-pressed", dspKnown && dsp.eq_enabled === true ? "true" : "false");
    $("eq").disabled = !ready || !dspKnown || dsp.eq_enabled === null;
    renderSoundEditor(); renderQueue(ready); renderPlaylist(); renderExport(); renderFavorites(ready); renderFavoritesTransfer();
    text("freshness", control.lastReadAt ? `${control.fresh ? "State received" : "Last state received"} ${new Date(control.lastReadAt).toLocaleTimeString()}. Playback status comes from the renderer.` : "No state received yet.");
    $("add-favorite").disabled = !ready || !control.favoritesFresh;
    $("save-current").disabled = !ready || !control.favoritesFresh || !snapshot.actions.save_current;
    renderFavoriteDraft(ready);
  }
  function element(tag, className, value) { const item = document.createElement(tag); if (className) item.className = className; if (value !== undefined) item.textContent = value; return item; }
  function currentEditor() { return control.fresh && snapshot?.dsp.status === "CONFIRMED" ? snapshot.dsp.editor : null; }
  function matchingBands(left, right) { return left.length === right.length && left.every((band, index) => band.param_name === right[index].param_name && band.value === right[index].value); }
  function renderSoundEditor() {
    const editor = currentEditor();
    if (soundSubmission?.outcome === "acknowledged" && editor && soundSubmission.session === snapshot.session_id && matchingBands(editor.bands, soundSubmission.bands)) {
      soundDraft.load(snapshot); soundSubmission = null;
    }
    soundDraft.observe(snapshot, control.fresh);
    const local = soundDraft.engaged || soundDraft.pending || soundDraft.review;
    const available = control.canMutate("dsp_bands");
    const editable = available && !soundDraft.stale && !soundDraft.pending && !soundDraft.review;
    text("dsp-current", editor ? `${editor.source === "wifi" ? "Wi-Fi" : "Global"} · ${editor.channel_mode}` : control.fresh ? snapshot.dsp.status === "CONFIRMED" ? "Bands unavailable" : snapshot.dsp.status : "UNKNOWN · STALE");
    let message = editor ? "The values below were confirmed by the renderer. Changes stay local until you apply them." : "Current bands are unavailable until the renderer confirms its sound state.";
    if (soundDraft.pending) message = "PENDING · Applying your local draft. Current bands have not been confirmed yet.";
    else if (soundDraft.review) message = "This request needs review. Read the request status above, then discard and reload confirmed bands before editing or submitting again. No retry was sent.";
    else if (soundDraft.stale) message = "Sound settings or presets changed, or the renderer restarted. Your local draft is preserved. Discard and reload the latest state before applying.";
    else if (local && !editor) message = "Your local draft is preserved. Current bands are unavailable; wait for confirmed readback before applying.";
    else if (soundDraft.dirty) message = soundDraft.bands ? "Unsaved local draft. Apply bands to send it, or discard your changes." : "Unsaved local draft. Fill every field within its allowed range before applying.";
    text("dsp-editor-message", message);
    text("dsp-draft-label", local ? soundDraft.pending ? "Submitted draft" : "Local draft" : "Confirmed values");
    $("dsp-band-form").hidden = !editor && !local;
    DSP_KEYS.forEach(key => {
      const input = $("dsp-" + key);
      if (soundDraft.values && input.value !== soundDraft.values[key]) input.value = soundDraft.values[key];
      input.disabled = !editable;
    });
    $("dsp-apply").disabled = !available || !soundDraft.canApply(snapshot, control.fresh);
    $("dsp-discard").disabled = !editor || control.busy || !local;
    text("dsp-discard", soundDraft.stale || soundDraft.review ? "Discard & reload" : "Discard changes");
    const signature = JSON.stringify([snapshot.session_id, editor?.presets || []]);
    if (signature !== presetSignature) {
      presetSignature = signature;
      const selected = $("dsp-preset").value;
      const fragment = document.createDocumentFragment();
      const prompt = element("option", "", "Choose a preset…"); prompt.value = ""; fragment.append(prompt);
      (editor?.presets || []).forEach(preset => {
        const option = element("option", "", `${preset.name}${preset.kind === "builtin" ? " · built-in" : preset.shared ? " · shared" : ""}`);
        option.value = preset.id; fragment.append(option);
      });
      $("dsp-preset").replaceChildren(fragment);
      $("dsp-preset").value = editor?.presets.some(preset => preset.id === selected) ? selected : "";
    }
    const selected = editor?.presets.find(preset => preset.id === $("dsp-preset").value);
    const clean = !soundDraft.dirty && !soundDraft.stale && !soundDraft.pending && !soundDraft.review;
    const libraryUnavailable = editor?.library_unavailable === true;
    const libraryWritable = control.canMutate("dsp_preset_save");
    renderCurveTransfer();
    $("dsp-preset").disabled = !available || !clean;
    $("dsp-load").disabled = !available || !clean || !selected;
    $("dsp-save").disabled = !libraryWritable || !clean;
    $("dsp-rename").disabled = !libraryWritable || !clean || selected?.kind !== "saved";
    $("dsp-delete").disabled = !libraryWritable || !clean || selected?.kind !== "saved";
    text("dsp-save-hint", libraryUnavailable ? DSP_LIBRARY_UNAVAILABLE_MESSAGE : !clean ? "Apply or discard your draft before saving or changing presets. Save active curve never saves unsent edits." : "Saves only the confirmed active curve. Loading a preset enables EQ.");
    if (presetContext) {
      if (!presetContext.sent && (presetContext.session !== snapshot.session_id || (editor && presetContext.revision !== editor.revision))) presetContext.stale = true;
      const stale = presetContext.stale || !editor || libraryUnavailable;
      $("dsp-preset-submit").disabled = !libraryWritable || !clean || stale || presetContext.sent;
      $("dsp-name").disabled = presetContext.sent || stale;
      $("dsp-shared-confirm").disabled = presetContext.mode === "save" || !presetContext.preset?.shared || presetContext.sent || stale;
      if (libraryUnavailable && !presetContext.sent) text("dsp-dialog-status", DSP_LIBRARY_UNAVAILABLE_MESSAGE);
      else if (stale && !presetContext.sent) text("dsp-dialog-status", "Sound settings or presets changed, or readback is unavailable. Close this dialog and review the latest state before continuing.");
    }
  }
  function renderQueue(ready) {
    const {queue, generation} = snapshot;
    // Eligibility belongs to this queue identity, not a rebuilt DOM row. A
    // metadata-only poll must not upgrade previously unsupported row intent.
    const identity = JSON.stringify([snapshot.session_id, generation]);
    if (!queueEditing || queueEditing.identity !== identity) queueEditing = {identity, allowed: queue.playlist_edit_reason === "supported", stale: false};
    else if (queue.playlist_edit_reason !== "supported") queueEditing.stale = true;
    text("queue-count", String(queue.count)); text("queue-name", queue.name || "Current queue");
    $("queue-empty").hidden = queue.items.length > 0;
    const signature = JSON.stringify([snapshot.session_id, queue.name, generation, queue.items]);
    if (signature !== queueSignature) {
      queueSignature = signature;
      const focusedIndex = document.activeElement?.dataset.queueIndex;
      const focusedAction = document.activeElement?.dataset.queueAction;
      const fragment = document.createDocumentFragment();
      queue.items.forEach(item => {
        const row = element("li", "queue-row"), button = element("button", "queue-button");
        button.type = "button"; button.dataset.queueIndex = String(item.index);
        button.append(element("span", "track-number", String(item.index)));
        const copy = element("span", "item-copy");
        copy.append(element("span", "item-title", item.title || "Untitled track"), element("span", "item-subtitle", item.artist || item.album || "Artist unavailable"));
        button.append(copy, element("span", "item-duration", formatTime(item.duration_seconds)));
        button.setAttribute("aria-label", `Play track ${item.index}: ${item.title || "Untitled track"}`);
        // Capture identity from the displayed row, never a future poll's index.
        const session = snapshot.session_id;
        const command = {action: "queue_select", queue_name: queue.name, index: item.index, expected_generation: generation};
        button.addEventListener("click", () => { if (snapshot.session_id === session && snapshot.generation === generation) return run(command, "Select track"); });
        row.append(button);
        const edits = element("div", "queue-edit-actions"); edits.setAttribute("role", "group"); edits.setAttribute("aria-label", `Edit entry ${item.index}`);
        [["up", "Move up", "playlist_move", {from_index: item.index, to_index: item.index - 1}], ["down", "Move down", "playlist_move", {from_index: item.index, to_index: item.index + 1}], ["remove", "Remove", "playlist_remove", {index: item.index}]].forEach(([direction, label, action, values]) => {
          const edit = element("button", "button subtle small", label); edit.type = "button"; edit.dataset.queueIndex = String(item.index); edit.dataset.queueAction = direction;
          edit.setAttribute("aria-label", `${label} entry ${item.index}`);
          const intent = {action, session: snapshot.session_id, generation, eligibility: queueEditing, allowed: queueEditing.allowed, edge: direction === "up" && item.index === 1 || direction === "down" && item.index === queue.count, sent: false, stale: false};
          queueIntents.set(edit, intent);
          edit.addEventListener("click", async () => {
            if (!intent.allowed || intent.eligibility.stale || intent.edge || intent.sent || intent.stale || intent.session !== snapshot.session_id || intent.generation !== snapshot.generation || !control.canMutate(action)) return;
            intent.sent = true;
            intent.rejectedOperation = null;
            const result = await run({action, expected_generation: intent.generation, ...values}, direction === "remove" ? "Remove queue entry" : "Move queue entry");
            if (result.queueEditRejected === true && result.id === control.operation) intent.rejectedOperation = result.id;
          });
          edits.append(edit);
        });
        row.append(edits); fragment.append(row);
      });
      $("queue-list").replaceChildren(fragment);
      if (focusedIndex) Array.from($("queue-list").querySelectorAll("button")).find(button => button.dataset.queueIndex === focusedIndex && button.dataset.queueAction === focusedAction)?.focus({preventScroll: true});
    }
    $("queue-list").querySelectorAll("button").forEach(button => {
      const intent = queueIntents.get(button);
      if (intent) {
        if (intent.session !== snapshot.session_id || intent.generation !== generation || queue.playlist_edit_reason !== "supported") intent.stale = true;
        button.disabled = !intent.allowed || intent.eligibility.stale || intent.edge || intent.sent || intent.stale || !control.canMutate(intent.action);
      } else { button.disabled = !ready || !snapshot.actions.queue_select; button.setAttribute("aria-current", String(Number(button.dataset.queueIndex) === queue.index)); }
    });
  }
  function playlistInput(draft) {
    const settings = {name: $("playlist-name").value, index: Number($("playlist-index").value), mode: $("playlist-mode").value};
    let body, error = "";
    const urls = draft.format === "urls" ? pastedURLs(draft.text) : null;
    if (draft.action === "playlist_append" && draft.format !== "urls") error = "Appending requires an explicit URL list.";
    else if (!draft.text) error = draft.format === "urls" ? "Enter at least one explicit audio URL." : "Choose a local UTF-8 playlist file.";
    else if (draft.format === "urls" && !urls) error = "Use 1–1000 explicit HTTP or HTTPS URLs, one per line, without credentials or surrounding spaces.";
    else if (draft.action === "playlist_replace" && draft.format !== "json" && (!settings.name.trim() || new TextEncoder().encode(settings.name).length > 256)) error = "Enter a queue name of 1–256 UTF-8 bytes.";
    else if (draft.action === "playlist_replace" && draft.format !== "json" && (!Number.isSafeInteger(settings.index) || settings.index < 1 || settings.index > 1000 || !Object.hasOwn(MODES, settings.mode) || urls && settings.index > urls.length)) error = "Enter a queue name, a valid starting entry and playback mode.";
    else {
      body = {action: draft.action, expected_generation: draft.generation};
      if (draft.action === "playlist_append") {
        if (snapshot.queue.count + urls.length > 1000) error = "The current queue and added URLs must total no more than 1000 entries.";
        body.urls = urls;
      } else if (draft.format === "json") body.source = {format: "json", text: draft.text};
      else body.source = draft.format === "m3u" ? {format: "m3u", text: draft.text, ...settings} : {format: "urls", urls, ...settings};
      if (requestBytes(body) > MAX_REQUEST_BYTES) error = "The complete request exceeds 32,768 UTF-8 bytes. Choose a smaller source, including its JSON escaping and settings.";
    }
    return {body, error, preview: playlistPreview(draft.format, draft.text, settings)};
  }
  function renderPlaylist() {
    $("playlist-load").disabled = !control.canMutate("playlist_replace");
    $("playlist-add").disabled = !control.canMutate("playlist_append");
    const reason = snapshot.queue.playlist_edit_reason === "supported" && !control.playlistEditable ? "unsupported_queue" : snapshot.queue.playlist_edit_reason;
    text("playlist-availability", !control.fresh ? "Queue state is unavailable. Refresh before loading or editing." : reason === "unsupported_queue" ? "This source queue cannot be edited here. You can replace it with a new URL playlist once playback is stopped." : !playlistAvailable(snapshot, "playlist_replace") ? "Stop playback separately and wait for confirmed idle before loading or editing a queue." : reason === "empty_queue" ? "Load an authored playlist or a list of audio URLs to begin." : "Queue changes keep playback stopped. Supported metadata is preserved when editing; shuffle history is reset.");
    const draft = playlistDraft;
    if (!draft) return;
    draft.observe(snapshot, control.fresh);
    const append = draft.action === "playlist_append", file = draft.format !== "urls";
    $("playlist-format-field").hidden = append; $("playlist-file-field").hidden = !file; $("playlist-urls-field").hidden = file;
    $("playlist-settings").hidden = append || draft.format === "json"; $("playlist-replace-warning").hidden = append;
    const locked = draft.sent || draft.stale || draft.cancelled;
    $("playlist-format").disabled = locked || append; $("playlist-file").disabled = locked || !file; $("playlist-urls").disabled = locked || file;
    ["playlist-name", "playlist-index", "playlist-mode"].forEach(id => { $(id).disabled = locked || append || draft.format === "json"; });
    $("playlist-confirm").disabled = locked || append;
    text("playlist-file-hint", draft.format === "m3u" ? "Direct URL lists only; HLS and relative paths are unsupported. Titles and durations in M3U files are not imported. The original text is sent only when you confirm replacement." : "Authored JSON keeps its starting entry and playback mode. The original text is sent only when you confirm replacement.");
    const input = playlistInput(draft), preview = input.preview;
    text("playlist-preview", !draft.text ? draft.reading ? "Reading local file…" : "Choose a source to preview its count and settings." : `${draft.format.toUpperCase()} · ${preview ? `${preview.count} ${preview.count === 1 ? "entry" : "entries"}${append ? " to append" : ` · Start at ${preview.index} · ${MODES[preview.mode]}`}` : "Count and settings unavailable"}. Local preview only; the renderer validates the source.`);
    text("playlist-size", input.body ? `${requestBytes(input.body).toLocaleString()} / 32,768 UTF-8 request bytes, including JSON escaping and settings.` : "The complete request must fit 32,768 UTF-8 bytes, including JSON escaping and settings.");
    let status = draft.status || "";
    if (!draft.sent) {
      if (draft.stale) status = "The queue, playback state or renderer session changed. Your draft is preserved. Cancel and review the current queue before starting again.";
      else if (!control.fresh) status = "Current state is unavailable. Your draft is preserved; wait for a fresh stopped state.";
      else if (draft.reading) status = "Reading the local file. Nothing has been sent.";
      else if (draft.error) status = draft.error;
      else if (draft.text) status = input.error;
    }
    text("playlist-dialog-status", status);
    $("playlist-submit").disabled = !control.canMutate(draft.action) || !draft.canSubmit(snapshot, control.fresh) || Boolean(draft.error || input.error) || !append && !$("playlist-confirm").checked;
  }
  function clearPlaylist() {
    playlistDraft?.cancel(); playlistDraft = null;
    $("playlist-file").value = ""; $("playlist-urls").value = ""; $("playlist-name").value = ""; $("playlist-confirm").checked = false;
    text("playlist-dialog-status", ""); text("playlist-preview", ""); text("playlist-size", "");
  }
  function closePlaylist(restore = true) { playlistDialog.close(restore); }
  function openPlaylist(action) {
    if (!control.canMutate(action)) return;
    clearPlaylist(); playlistDraft = new PlaylistDraft(snapshot, action);
    const append = action === "playlist_append";
    playlistDraft.setSource(append ? "urls" : "json");
    $("playlist-format").value = playlistDraft.format; $("playlist-name").value = "My playlist"; $("playlist-index").value = "1"; $("playlist-mode").value = "normal";
    $("playlist-file").accept = ".json,application/json";
    text("playlist-dialog-heading", append ? "Add URLs to queue" : "Load playlist");
    text("playlist-dialog-hint", append ? "Append explicit URLs to this queue. Supported track metadata is preserved, shuffle history is reset, and playback remains stopped." : "Replace the queue from a local authored JSON file, an M3U URL list, or pasted explicit URLs. Playback remains stopped.");
    text("playlist-submit", append ? "Add URLs, keep stopped" : "Replace queue, keep stopped");
    renderPlaylist(); playlistDialog.open($(append ? "playlist-add" : "playlist-load")); $(append ? "playlist-urls" : "playlist-format").focus();
  }
  $("playlist-load").addEventListener("click", () => openPlaylist("playlist_replace"));
  $("playlist-add").addEventListener("click", () => openPlaylist("playlist_append"));
  ["playlist-close", "playlist-cancel"].forEach(id => $(id).addEventListener("click", closePlaylist));
  $("playlist-format").addEventListener("change", () => {
    if (!playlistDraft || playlistDraft.sent || playlistDraft.stale || playlistDraft.action !== "playlist_replace") return;
    const format = $("playlist-format").value;
    if (!["json", "m3u", "urls"].includes(format)) return;
    playlistDraft.setSource(format); $("playlist-urls").value = ""; $("playlist-file").value = ""; $("playlist-confirm").checked = false;
    $("playlist-file").accept = format === "json" ? ".json,application/json" : ".m3u,.m3u8,audio/x-mpegurl,audio/mpegurl";
    renderPlaylist();
  });
  $("playlist-file").addEventListener("change", async () => {
    const draft = playlistDraft; let file = $("playlist-file").files?.[0];
    $("playlist-file").value = ""; // Release the selection and never display a filename.
    if (!draft || draft.sent || draft.stale || draft.format === "urls") return;
    $("playlist-confirm").checked = false;
    const reading = draft.readFile(file); file = null; renderPlaylist(); await reading;
    if (playlistDraft === draft) renderPlaylist();
  });
  $("playlist-urls").addEventListener("input", () => {
    if (!playlistDraft || playlistDraft.sent || playlistDraft.stale || playlistDraft.format !== "urls") return;
    playlistDraft.setSource("urls", $("playlist-urls").value); $("playlist-confirm").checked = false; renderPlaylist();
  });
  ["playlist-name", "playlist-index", "playlist-mode", "playlist-confirm"].forEach(id => $(id).addEventListener("input", () => { if (id !== "playlist-confirm") $("playlist-confirm").checked = false; renderPlaylist(); }));
  $("playlist-form").addEventListener("submit", async event => {
    event.preventDefault(); const draft = playlistDraft;
    if (!draft) return;
    draft.observe(snapshot, control.fresh);
    if (!control.canMutate(draft.action) || !draft.canSubmit(snapshot, control.fresh)) { renderPlaylist(); return; }
    const input = playlistInput(draft);
    if (draft.error || input.error || draft.action === "playlist_replace" && !$("playlist-confirm").checked) { renderPlaylist(); return; }
    draft.sent = true; draft.status = "PENDING · Waiting for the renderer. No retry will be sent.";
    const result = await run(input.body, draft.action === "playlist_replace" ? "Replace queue" : "Append queue URLs");
    if (playlistDraft !== draft) return;
    if (result.kind === "acknowledged" && control.fresh && snapshot.session_id === draft.session && snapshot.generation !== draft.generation && snapshot.transport.status === "CONFIRMED" && snapshot.transport.phase === "STOPPED") closePlaylist();
    else { draft.status = result.kind === "acknowledged" ? "Request acknowledged, but the stopped queue change has not been verified. Close this dialog and review the current queue before another request." : `${result.kind.toUpperCase()} · ${result.text}`; renderPlaylist(); }
  });
  // A private export is a separate read, never a playback command or notice.
  // The modal owns its request and Blob URL; neither survives dismissal.
  function releaseExportFile(context) {
    if (context?.url) { URL.revokeObjectURL(context.url); context.url = null; }
    $("export-download").removeAttribute("href"); $("export-download").hidden = true;
  }
  function clearExport() {
    const context = exportContext; exportContext = null;
    context?.abort?.abort(); releaseExportFile(context);
    $("export-confirm").checked = false;
    text("export-summary", ""); text("export-status", "");
    $("export-prepare").disabled = true; $("export-confirm").disabled = true;
  }
  function closeExport(restore = true) { exportDialog.close(restore); }
  function exportCurrent(context) {
    return exportContext === context && $("export-dialog").open && !context.stale && control.fresh && snapshot && context.identity === exportIdentity(snapshot);
  }
  function renderExport() {
    $("playlist-export").disabled = !control.fresh || !snapshot?.queue.count;
    const context = exportContext;
    if (!context) return;
    if (control.fresh && context.identity !== exportIdentity(snapshot) && !context.stale) {
      context.stale = true; context.summary = null; context.abort?.abort(); releaseExportFile(context);
      $("export-confirm").checked = false;
      context.status = "The queue or renderer session changed. Close this dialog, reopen Export JSON and review it again.";
    }
    const ready = exportCurrent(context), summary = context.summary;
    text("export-summary", summary ? `${summary.name} · ${summary.count} ${summary.count === 1 ? "entry" : "entries"} · Start at ${summary.index} · ${MODES[summary.mode]} · ${summary.size_bytes.toLocaleString()} UTF-8 file bytes` : "");
    text("export-status", !control.fresh && !context.stale ? "Current state is unavailable. Wait for fresh state or close this dialog." : context.status);
    $("export-confirm").disabled = !ready || !summary || context.pending || context.failed;
    $("export-prepare").disabled = !ready || !summary || context.pending || context.failed || context.sent || !$("export-confirm").checked;
    const downloadable = ready && context.url && $("export-confirm").checked;
    $("export-download").hidden = !downloadable;
    if (downloadable) $("export-download").setAttribute("href", context.url);
    else $("export-download").removeAttribute("href");
  }
  async function fetchExport(context, body, expectedSize, endpoint = "/orr/v1/web/playlist/export", filename = EXPORT_FILENAME) {
    const abort = new AbortController(); context.abort = abort;
    const timer = window.setTimeout(() => abort.abort(), 20000);
    let onAbort;
    const aborted = new Promise((_, reject) => {
      onAbort = () => reject(new Error("Export cancelled"));
      abort.signal.addEventListener("abort", onAbort, {once: true});
    });
    try {
      const fetching = fetch(endpoint, {
        method: "POST", credentials: "same-origin", mode: "same-origin", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer",
        headers: {"Accept": "application/json", "Content-Type": "application/json", "X-ORR-CSRF": context.state.csrf_token},
        body: JSON.stringify(body), signal: abort.signal
      }).then(response => {
        // A late fetch double (or adapter) may ignore abort. Discard its stream.
        if (abort.signal.aborted) { response.body?.cancel().catch(() => {}); throw new Error("Export cancelled"); }
        return response;
      });
      const response = await Promise.race([fetching, aborted]);
      return await readExportResponse(response, abort.signal, expectedSize, filename);
    } finally {
      abort.signal.removeEventListener("abort", onAbort); window.clearTimeout(timer);
      if (context.abort === abort) context.abort = null;
    }
  }
  async function prepareExport(context, download) {
    context.pending = true;
    if (download) context.sent = true;
    context.status = download ? "Preparing the private file…" : "Reading a redacted queue summary…";
    renderExport();
    const body = {action: download ? "download" : "prepare", expected_session: context.state.session_id, expected_generation: context.state.generation};
    if (download) { body.expected_export = context.summary.revision; body.private_export_confirmed = true; }
    let bytes;
    try {
      const response = await fetchExport(context, body, download ? context.summary.size_bytes : undefined);
      bytes = response.bytes;
      if (!exportCurrent(context)) throw new Error("Export context changed");
      if (response.status !== 200) {
        context.failed = true; context.status = exportFailure(response.status, exportObject(bytes));
      } else if (download) {
        validateExportDocument(bytes, context.summary);
        context.url = URL.createObjectURL(new Blob([bytes], {type: "application/json"}));
        context.status = "Private file ready. Choose Download JSON to request a browser download.";
      } else {
        context.summary = validateExportSummary(exportObject(bytes), context.state);
        context.status = "Review the summary and privacy warning before preparing the private file.";
      }
    } catch (_) {
      if (exportContext === context && !context.stale) { context.failed = true; context.status = exportFailure(); }
    } finally {
      if (bytes) bytes.fill(0); // Blob construction copies the unchanged bytes.
      context.pending = false;
      if (exportContext === context) renderExport();
    }
  }
  $("playlist-export").addEventListener("click", () => {
    if (!control.fresh || !snapshot?.queue.count || $("export-dialog").open) return;
    clearExport();
    exportContext = {state: snapshot, identity: exportIdentity(snapshot), summary: null, pending: false, failed: false, sent: false, stale: false, url: null, abort: null, status: ""};
    exportDialog.open($("playlist-export")); $("export-close").focus();
    return prepareExport(exportContext, false);
  });
  ["export-close", "export-cancel"].forEach(id => $(id).addEventListener("click", closeExport));
  $("export-confirm").addEventListener("change", () => {
    if (exportContext && !$("export-confirm").checked) {
      releaseExportFile(exportContext);
      if (exportContext.sent) { exportContext.failed = true; exportContext.abort?.abort(); exportContext.status = "Consent cleared. Close this dialog and review the export again if needed."; }
    }
    renderExport();
  });
  $("export-form").addEventListener("submit", event => {
    event.preventDefault(); renderExport();
    const context = exportContext;
    if (!context || !exportCurrent(context) || context.pending || context.failed || context.sent || !context.summary || !$("export-confirm").checked) return;
    return prepareExport(context, true);
  });
  $("export-download").addEventListener("click", event => {
    const context = exportContext;
    if (!context || !exportCurrent(context) || !context.url || !$("export-confirm").checked) { event.preventDefault(); renderExport(); return; }
    // Leave the real anchor's default action to this user click. A delayed
    // response never clicks it, and a click does not prove a durable save.
    context.status = "Download requested; check your browser’s downloads. Keep the private file safe.";
    renderExport();
  });
  // Curve transfer is saved-library only. Neither dialog applies sound settings.
  function cleanSoundDraft() { return !soundDraft.dirty && !soundDraft.stale && !soundDraft.pending && !soundDraft.review; }
  function curveTransferAvailable() { return control.canMutate("dsp_preset_import") && cleanSoundDraft(); }
  function curveContextCurrent(context) {
    return Boolean(context && curveTransferAvailable() && snapshot.session_id === context.session && currentEditor()?.revision === context.revision);
  }
  function releaseCurveExport(context) {
    if (context?.url) { URL.revokeObjectURL(context.url); context.url = null; }
    $("curve-export-download").removeAttribute("href"); $("curve-export-download").hidden = true;
  }
  function clearCurveExport() {
    const context = curveExportContext; curveExportContext = null;
    context?.abort?.abort(); releaseCurveExport(context);
    $("curve-export-confirm").checked = false; $("curve-export-confirm").disabled = true;
    $("curve-export-prepare").disabled = true;
    text("curve-export-summary", ""); text("curve-export-status", "");
  }
  function clearCurveImport() {
    curveImportDraft?.cancel(); curveImportDraft = null;
    $("curve-import-file").value = ""; $("curve-import-confirm").checked = false;
    $("curve-import-file").disabled = true; $("curve-import-confirm").disabled = true; $("curve-import-submit").disabled = true;
    text("curve-import-summary", ""); text("curve-import-status", "");
  }
  function currentCurveExport(context) {
    return curveExportContext === context && $("curve-export-dialog").open && !context.stale && !context.failed && curveContextCurrent(context);
  }
  function renderCurveTransfer() {
    const available = curveTransferAvailable(), selected = currentEditor()?.presets.find(item => item.id === $("dsp-preset").value);
    $("dsp-export").disabled = !available || selected?.kind !== "saved";
    $("dsp-import").disabled = !available;
    const context = curveExportContext;
    if (context) {
      if (!context.stale && !curveContextCurrent(context)) {
        context.stale = true; context.summary = null; context.abort?.abort(); releaseCurveExport(context);
        $("curve-export-confirm").checked = false; context.status = CURVE_TRANSFER_CHANGED;
      }
      const ready = currentCurveExport(context), summary = context.summary;
      text("curve-export-summary", summary ? `1 saved curve · Current plugin only · ${summary.band_count} band parameters · ${summary.size_bytes.toLocaleString()} UTF-8 file bytes. ${summary.shared ? "Shared preset: other stored contexts are deliberately omitted." : "One stored context included."}` : "");
      text("curve-export-status", context.status);
      $("curve-export-confirm").disabled = !ready || !summary || context.pending;
      $("curve-export-prepare").disabled = !ready || !summary || context.pending || context.sent || !$("curve-export-confirm").checked;
      const downloadable = ready && context.url && $("curve-export-confirm").checked;
      $("curve-export-download").hidden = !downloadable;
      if (downloadable) $("curve-export-download").setAttribute("href", context.url);
      else $("curve-export-download").removeAttribute("href");
    }
    const draft = curveImportDraft;
    if (draft) {
      if (!draft.sent && !draft.stale && !curveContextCurrent(draft)) {
        draft.stale = true; draft.clearSource(); $("curve-import-file").value = "";
        $("curve-import-confirm").checked = false; draft.status = CURVE_TRANSFER_CHANGED;
      }
      const ready = !draft.sent && !draft.stale && !draft.cancelled && curveContextCurrent(draft);
      $("curve-import-file").disabled = !ready;
      $("curve-import-confirm").disabled = !ready || draft.reading || !draft.text;
      $("curve-import-submit").disabled = !ready || draft.reading || !draft.text || Boolean(draft.error) || !$("curve-import-confirm").checked;
      text("curve-import-summary", draft.text ? `1 named curve · 1 plugin · 40 band parameters · ${draft.size.toLocaleString()} UTF-8 file bytes · ${requestBytes(curveImportBody(draft.revision, draft.text)).toLocaleString()} / 32,768 escaped request bytes. Local structural preview only; the renderer validates the name and current plugin.` : "");
      text("curve-import-status", draft.sent || draft.stale ? draft.status : draft.error || (draft.reading ? "Reading the local file. Nothing has been sent." : draft.status));
    }
  }
  async function prepareCurveExport(context, download) {
    context.pending = true; if (download) context.sent = true;
    context.status = download ? "Preparing the private saved curve file…" : "Reading a private-detail-free summary…";
    renderCurveTransfer();
    const body = {action: download ? "download" : "prepare", expected_dsp: context.revision, preset_id: context.preset.id};
    if (download) { body.expected_export = context.summary.revision; body.private_export_confirmed = true; }
    let bytes;
    try {
      const response = await fetchExport(context, body, download ? context.summary.size_bytes : undefined, "/orr/v1/web/dsp/export", CURVE_EXPORT_FILENAME);
      bytes = response.bytes;
      if (!currentCurveExport(context) || download && !$("curve-export-confirm").checked) throw new Error("Curve context changed");
      if (response.status !== 200) {
        const error = exportObject(bytes);
        context.failed = true;
        context.status = response.status === 409 && error.outcome === "REJECTED" ? curveTransferFailure(error.error) : "Could not prepare the saved curve export. Close this dialog and review current state. No download was started.";
      } else if (download) {
        validateCurveExportDocument(bytes, context.summary);
        context.url = URL.createObjectURL(new Blob([bytes], {type: "application/json"}));
        context.status = "Private curve file ready. Choose Download JSON to request a browser download.";
      } else {
        context.summary = validateCurveExportSummary(exportObject(bytes), context.revision, context.preset);
        context.status = "Review the current-context scope and privacy warning before preparing the file.";
      }
    } catch (_) {
      if (curveExportContext === context && !context.stale && !context.failed) {
        context.failed = true; context.status = "Could not prepare the saved curve export. Close this dialog and review current state. No download was started.";
      }
    } finally {
      if (bytes) bytes.fill(0);
      context.pending = false; if (curveExportContext === context) renderCurveTransfer();
    }
  }
  $("dsp-export").addEventListener("click", () => {
    const editor = currentEditor(), preset = editor?.presets.find(item => item.id === $("dsp-preset").value);
    if (!curveTransferAvailable() || preset?.kind !== "saved" || $("curve-export-dialog").open) return;
    clearCurveExport();
    curveExportContext = {state: snapshot, session: snapshot.session_id, revision: editor.revision, preset: {id: preset.id, shared: preset.shared}, summary: null, pending: false, sent: false, stale: false, failed: false, url: null, abort: null, status: ""};
    curveExportDialog.open($("dsp-export")); $("curve-export-close").focus();
    return prepareCurveExport(curveExportContext, false);
  });
  ["curve-export-close", "curve-export-cancel"].forEach(id => $(id).addEventListener("click", () => curveExportDialog.close()));
  $("curve-export-confirm").addEventListener("change", () => {
    const context = curveExportContext;
    if (context && !$("curve-export-confirm").checked) {
      releaseCurveExport(context);
      if (context.sent) { context.failed = true; context.abort?.abort(); context.status = "Consent cleared. Close this dialog and review the export again if needed."; }
    }
    renderCurveTransfer();
  });
  $("curve-export-form").addEventListener("submit", event => {
    event.preventDefault(); renderCurveTransfer();
    const context = curveExportContext;
    if (!context || !currentCurveExport(context) || context.pending || context.sent || !context.summary || !$("curve-export-confirm").checked) return;
    return prepareCurveExport(context, true);
  });
  $("curve-export-download").addEventListener("click", event => {
    const context = curveExportContext;
    if (!context || !currentCurveExport(context) || !context.url || !$("curve-export-confirm").checked) { event.preventDefault(); renderCurveTransfer(); return; }
    context.status = "Download requested; check your browser’s downloads. Keep the private curve file safe.";
    renderCurveTransfer();
  });
  $("dsp-import").addEventListener("click", () => {
    if (!curveTransferAvailable() || $("curve-import-dialog").open) return;
    clearCurveImport(); curveImportDraft = new CurveImportDraft(snapshot);
    curveImportDialog.open($("dsp-import")); renderCurveTransfer(); $("curve-import-file").focus();
  });
  ["curve-import-close", "curve-import-cancel"].forEach(id => $(id).addEventListener("click", () => curveImportDialog.close()));
  $("curve-import-file").addEventListener("change", async () => {
    const draft = curveImportDraft; let file = $("curve-import-file").files?.[0];
    $("curve-import-file").value = ""; $("curve-import-confirm").checked = false;
    if (!draft || draft.sent || draft.stale || !curveContextCurrent(draft)) { renderCurveTransfer(); return; }
    draft.status = "";
    const reading = draft.readFile(file); file = null; renderCurveTransfer(); await reading;
    if (curveImportDraft === draft) renderCurveTransfer();
  });
  $("curve-import-confirm").addEventListener("change", () => {
    const draft = curveImportDraft;
    if (draft && !$("curve-import-confirm").checked) {
      draft.clearSource(); $("curve-import-file").value = "";
      if (!draft.sent) draft.status = "Consent cleared. Choose the file again to review a new import.";
    }
    renderCurveTransfer();
  });
  $("curve-import-form").addEventListener("submit", async event => {
    event.preventDefault(); renderCurveTransfer();
    const draft = curveImportDraft;
    if (!draft || draft.sent || draft.stale || draft.cancelled || draft.reading || !draft.text || draft.error || !curveContextCurrent(draft) || !$("curve-import-confirm").checked) return;
    const body = curveImportBody(draft.revision, draft.text);
    if (requestBytes(body) > MAX_REQUEST_BYTES) return;
    draft.sent = true; draft.clearSource(); $("curve-import-file").value = ""; $("curve-import-confirm").checked = false;
    draft.status = "PENDING · Saving the curve to the preset library. Active sound settings are not applied.";
    renderCurveTransfer();
    const waiting = run(body, "Import saved EQ curve"); body.text = "";
    const result = await waiting;
    if (curveImportDraft !== draft) return;
    draft.status = result.kind === "acknowledged"
      ? "Import acknowledged. Review the saved preset list. Select and Load separately to apply a saved curve."
      : `${result.kind.toUpperCase()} · ${result.text}`;
    if (!control.fresh) draft.status += " Current renderer state is unavailable.";
    else if (snapshot.session_id !== draft.session) draft.status += " The renderer restarted; review its current saved presets.";
    else if (currentEditor()?.library_unavailable === true) draft.status += " " + DSP_LIBRARY_UNAVAILABLE_MESSAGE;
    renderCurveTransfer();
  });
  // Private managed-library transfer. Configured favorites are never restored.
  function favoritesTransferIdentity() { return JSON.stringify([snapshot?.session_id, control.favorites]); }
  function favoritesTransferAvailable() { return control.canMutate("favorite_add"); }
  function favoritesContextCurrent(context, restore = false) {
    return Boolean(context && favoritesTransferAvailable() && snapshot.session_id === context.session && context.identity === favoritesTransferIdentity() && (!restore || control.canMutate("favorite_restore")));
  }
  function releaseFavoritesExport(context) {
    if (context?.url) { URL.revokeObjectURL(context.url); context.url = null; }
    $("favorites-export-download").removeAttribute("href"); $("favorites-export-download").hidden = true;
  }
  function clearFavoritesExport() {
    const context = favoritesExportContext; favoritesExportContext = null;
    context?.abort?.abort(); releaseFavoritesExport(context);
    $("favorites-export-confirm").checked = false; $("favorites-export-confirm").disabled = true;
    $("favorites-export-prepare").disabled = true;
    text("favorites-export-summary", ""); text("favorites-export-status", "");
  }
  function clearFavoritesRestore() {
    favoritesRestoreDraft?.cancel(); favoritesRestoreDraft = null;
    $("favorites-restore-file").value = ""; $("favorites-restore-confirm").checked = false;
    $("favorites-restore-file").disabled = true; $("favorites-restore-confirm").disabled = true; $("favorites-restore-submit").disabled = true;
    text("favorites-restore-summary", ""); text("favorites-restore-status", "");
  }
  function currentFavoritesExport(context) {
    return favoritesExportContext === context && $("favorites-export-dialog").open && !context.stale && !context.failed && favoritesContextCurrent(context);
  }
  function renderFavoritesTransfer() {
    $("favorites-backup").disabled = !favoritesTransferAvailable();
    $("favorites-restore").disabled = !control.canMutate("favorite_restore");
    const context = favoritesExportContext;
    if (context) {
      if (!context.stale && !favoritesContextCurrent(context)) {
        context.stale = true; context.summary = null; context.abort?.abort(); releaseFavoritesExport(context);
        $("favorites-export-confirm").checked = false; context.status = FAVORITES_TRANSFER_CHANGED;
      }
      const ready = currentFavoritesExport(context), summary = context.summary;
      text("favorites-export-summary", summary ? `Managed favorites only · ${summary.count} ${summary.count === 1 ? "entry" : "entries"} · ${summary.size_bytes.toLocaleString()} UTF-8 file bytes. Configured favorites are excluded.` : "");
      text("favorites-export-status", context.status);
      $("favorites-export-confirm").disabled = !ready || !summary || context.pending;
      $("favorites-export-prepare").disabled = !ready || !summary || context.pending || context.sent || !$("favorites-export-confirm").checked;
      const downloadable = ready && context.url && $("favorites-export-confirm").checked;
      $("favorites-export-download").hidden = !downloadable;
      if (downloadable) $("favorites-export-download").setAttribute("href", context.url);
      else $("favorites-export-download").removeAttribute("href");
    }
    const draft = favoritesRestoreDraft;
    if (draft) {
      if (!draft.sent && !draft.stale && !favoritesContextCurrent(draft, true)) {
        draft.stale = true; draft.clearSource(); $("favorites-restore-file").value = "";
        $("favorites-restore-confirm").checked = false; draft.status = FAVORITES_TRANSFER_CHANGED;
      }
      const ready = !draft.sent && !draft.stale && !draft.cancelled && favoritesContextCurrent(draft, true);
      $("favorites-restore-file").disabled = !ready;
      $("favorites-restore-confirm").disabled = !ready || draft.reading || !draft.text;
      $("favorites-restore-submit").disabled = !ready || draft.reading || !draft.text || Boolean(draft.error) || !$("favorites-restore-confirm").checked;
      text("favorites-restore-summary", draft.text ? `Managed favorites only · ${draft.count} ${draft.count === 1 ? "entry" : "entries"} · ${draft.size.toLocaleString()} UTF-8 file bytes · ${requestBytes(favoritesRestoreBody(draft.session, draft.text)).toLocaleString()} / 32,768 escaped request bytes. Local structural preview only; the renderer validates private targets.` : "");
      text("favorites-restore-status", draft.sent || draft.stale ? draft.status : draft.error || (draft.reading ? "Reading the local file. Nothing has been sent." : draft.status));
    }
  }
  async function prepareFavoritesExport(context, download) {
    context.pending = true; if (download) context.sent = true;
    context.status = download ? "Preparing the private favorites backup…" : "Reading a private-detail-free summary…";
    renderFavoritesTransfer();
    const body = {action: download ? "download" : "prepare", expected_session: context.session};
    if (download) { body.expected_export = context.summary.revision; body.private_export_confirmed = true; }
    let bytes;
    try {
      const response = await fetchExport(context, body, download ? context.summary.size_bytes : undefined, "/orr/v1/web/favorites/export", FAVORITES_EXPORT_FILENAME);
      bytes = response.bytes;
      if (!currentFavoritesExport(context) || download && !$("favorites-export-confirm").checked) throw new Error("Favorites context changed");
      if (response.status !== 200) {
        const error = exportObject(bytes);
        context.failed = true;
        context.status = [409, 503].includes(response.status) && error.outcome === "REJECTED" ? favoritesTransferFailure(error.error) : "Could not prepare the favorites backup. Close this dialog and review current state. No download was started.";
      } else if (download) {
        validateFavoritesExportDocument(bytes, context.summary);
        context.url = URL.createObjectURL(new Blob([bytes], {type: "application/json"}));
        context.status = "Private favorites backup ready. Choose Download JSON to request a browser download.";
      } else {
        context.summary = validateFavoritesExportSummary(exportObject(bytes), context.session, context.count);
        context.status = "Review the managed-only scope and privacy warning before preparing the file.";
      }
    } catch (_) {
      if (favoritesExportContext === context && !context.stale && !context.failed) {
        context.failed = true; context.status = "Could not prepare the favorites backup. Close this dialog and review current state. No download was started.";
      }
    } finally {
      if (bytes) bytes.fill(0);
      context.pending = false; if (favoritesExportContext === context) renderFavoritesTransfer();
    }
  }
  $("favorites-backup").addEventListener("click", () => {
    if (!favoritesTransferAvailable() || $("favorites-export-dialog").open) return;
    clearFavoritesExport();
    favoritesExportContext = {state: snapshot, session: snapshot.session_id, identity: favoritesTransferIdentity(), count: control.favorites.filter(item => item.editable).length, summary: null, pending: false, sent: false, stale: false, failed: false, url: null, abort: null, status: ""};
    favoritesExportDialog.open($("favorites-backup")); $("favorites-export-close").focus();
    return prepareFavoritesExport(favoritesExportContext, false);
  });
  ["favorites-export-close", "favorites-export-cancel"].forEach(id => $(id).addEventListener("click", () => favoritesExportDialog.close()));
  $("favorites-export-confirm").addEventListener("change", () => {
    const context = favoritesExportContext;
    if (context && !$("favorites-export-confirm").checked) {
      releaseFavoritesExport(context);
      if (context.sent) { context.failed = true; context.abort?.abort(); context.status = "Consent cleared. Close this dialog and review the export again if needed."; }
    }
    renderFavoritesTransfer();
  });
  $("favorites-export-form").addEventListener("submit", event => {
    event.preventDefault(); renderFavoritesTransfer();
    const context = favoritesExportContext;
    if (!context || !currentFavoritesExport(context) || context.pending || context.sent || !context.summary || !$("favorites-export-confirm").checked) return;
    return prepareFavoritesExport(context, true);
  });
  $("favorites-export-download").addEventListener("click", event => {
    const context = favoritesExportContext;
    if (!context || !currentFavoritesExport(context) || !context.url || !$("favorites-export-confirm").checked) { event.preventDefault(); renderFavoritesTransfer(); return; }
    context.status = "Download requested; check your browser’s downloads. Keep the private favorites backup safe.";
    renderFavoritesTransfer();
  });
  $("favorites-restore").addEventListener("click", () => {
    if (!control.canMutate("favorite_restore") || $("favorites-restore-dialog").open) return;
    clearFavoritesRestore(); favoritesRestoreDraft = new FavoritesRestoreDraft(snapshot); favoritesRestoreDraft.identity = favoritesTransferIdentity();
    favoritesRestoreDialog.open($("favorites-restore")); renderFavoritesTransfer(); $("favorites-restore-file").focus();
  });
  ["favorites-restore-close", "favorites-restore-cancel"].forEach(id => $(id).addEventListener("click", () => favoritesRestoreDialog.close()));
  $("favorites-restore-file").addEventListener("change", async () => {
    const draft = favoritesRestoreDraft; let file = $("favorites-restore-file").files?.[0];
    $("favorites-restore-file").value = ""; $("favorites-restore-confirm").checked = false;
    if (!draft || draft.sent || draft.stale || !favoritesContextCurrent(draft, true)) { renderFavoritesTransfer(); return; }
    draft.status = "";
    const reading = draft.readFile(file); file = null; renderFavoritesTransfer(); await reading;
    if (favoritesRestoreDraft === draft) renderFavoritesTransfer();
  });
  $("favorites-restore-confirm").addEventListener("change", () => {
    const draft = favoritesRestoreDraft;
    if (draft && !$("favorites-restore-confirm").checked) {
      draft.clearSource(); $("favorites-restore-file").value = "";
      if (!draft.sent) draft.status = "Consent cleared. Choose the file again to review a new restore.";
    }
    renderFavoritesTransfer();
  });
  $("favorites-restore-form").addEventListener("submit", async event => {
    event.preventDefault(); renderFavoritesTransfer();
    const draft = favoritesRestoreDraft;
    if (!draft || draft.sent || draft.stale || draft.cancelled || draft.reading || !draft.text || draft.error || !favoritesContextCurrent(draft, true) || !$("favorites-restore-confirm").checked) return;
    const body = favoritesRestoreBody(draft.session, draft.text);
    if (requestBytes(body) > MAX_REQUEST_BYTES) return;
    draft.sent = true; draft.clearSource(); $("favorites-restore-file").value = ""; $("favorites-restore-confirm").checked = false;
    draft.status = "PENDING · Restoring the managed favorites library. Playback and configured favorites stay unchanged.";
    renderFavoritesTransfer();
    const waiting = run(body, "Restore managed favorites"); body.text = "";
    const result = await waiting;
    if (favoritesRestoreDraft !== draft) return;
    draft.status = result.kind === "acknowledged"
      ? "Restore acknowledged. Review managed favorites; exact private contents are not verified by this redacted view."
      : `${result.kind.toUpperCase()} · ${result.text}`;
    if (!control.fresh) draft.status += " Current renderer state is unavailable.";
    else if (snapshot.session_id !== draft.session) draft.status += " The renderer restarted; review its current managed favorites.";
    else if (!control.favoritesFresh) draft.status += " The favorites list could not be refreshed.";
    renderFavoritesTransfer();
  });
  function renderFavorites(ready) {
    text("favorites-count", String(control.favorites.length));
    $("favorites-empty").hidden = control.favorites.length > 0;
    text("favorites-note", control.favoritesFresh ? "Saved on this renderer. Configuration favorites are read-only."
      : control.favoritesReadFailure === "favorites_unavailable"
        ? "A favorites read reported that the managed library could not be loaded. This list may be stale. Stop the renderer service, repair or restore its saved favorites library, then restart it."
        : "Favorites could not be refreshed. This list may be stale.");
    const signature = JSON.stringify([snapshot.session_id, control.favorites]);
    if (signature !== favoritesSignature) {
      favoritesSignature = signature;
      const focused = document.activeElement?.dataset.favoriteKey;
      const fragment = document.createDocumentFragment();
      control.favorites.forEach(favorite => {
        const row = element("li", "favorite-row"), play = element("button", "favorite-play");
        play.type = "button"; play.dataset.favoriteKey = `play:${favorite.id}`;
        const icon = element("span", "favorite-icon", "▶"); icon.setAttribute("aria-hidden", "true");
        const copy = element("span", "item-copy"); copy.append(element("span", "item-title", favorite.name), element("span", "item-subtitle", favorite.artist || favorite.target_label || "Saved audio"));
        play.append(icon, copy); play.setAttribute("aria-label", `Play favorite: ${favorite.name}`);
        play.addEventListener("click", () => run({action: "favorite_play", id: favorite.id}, "Play favorite")); row.append(play);
        const actions = element("div", "favorite-actions");
        if (favorite.editable) {
          [["rename", "Rename"], ["target", "Change URL"], ["delete", "Delete"]].forEach(([action, label]) => {
            const button = element("button", "button subtle small", label); button.type = "button"; button.dataset.favoriteKey = `${action}:${favorite.id}`; button.setAttribute("aria-label", `${label} favorite: ${favorite.name}`); button.addEventListener("click", () => openFavorite(action, favorite, button)); actions.append(button);
          });
        } else actions.append(element("span", "read-only", "From config"));
        row.append(actions); fragment.append(row);
      });
      $("favorites-list").replaceChildren(fragment);
      if (focused) Array.from($("favorites-list").querySelectorAll("button")).find(button => button.dataset.favoriteKey === focused)?.focus({preventScroll: true});
    }
    $("favorites-list").querySelectorAll("button").forEach(button => { button.disabled = !ready || !control.favoritesFresh; });
  }
  function renderRoomRename() {
    const ready = control.canMutate("device_rename"), context = roomContext;
    $("rename-room").disabled = !ready;
    if (!context) return;
    // Only a fresh external observation invalidates an unsent draft. Our own
    // pending write may change readback before its discovery refresh finishes.
    if (!context.sent && control.fresh && (context.session !== snapshot.session_id || context.revision !== snapshot.device.revision)) context.stale = true;
    $("room-name").disabled = context.sent || context.stale || !ready;
    $("room-submit").disabled = context.sent || context.stale || !ready;
    if (context.stale && !context.sent) text("room-dialog-status", "The room name changed or the renderer restarted. Close this dialog and reopen Rename room to review the current name.");
    else if (!context.sent) text("room-dialog-status", !control.fresh ? "Current state is unavailable. Wait for fresh state before saving." : snapshot.device.name_visible ? "" : "The current name is hidden because it may contain private text. Enter a new name to replace it.");
  }
  function clearRoomForm() {
    roomContext = null; $("room-name").value = ""; text("room-dialog-status", "");
  }
  function closeRoom(restore = true) { roomDialog.close(restore); }
  $("rename-room").addEventListener("click", () => {
    if ($("room-dialog").open || !control.canMutate("device_rename")) return;
    roomContext = {session: snapshot.session_id, revision: snapshot.device.revision, sent: false, stale: false};
    $("room-name").value = snapshot.device.name_visible ? snapshot.device.name : "";
    renderRoomRename(); roomDialog.open($("rename-room")); $("room-name").focus();
  });
  ["room-close", "room-cancel"].forEach(id => $(id).addEventListener("click", closeRoom));
  $("room-form").addEventListener("submit", async event => {
    event.preventDefault(); renderRoomRename();
    const context = roomContext;
    if (!context || context.sent || context.stale || !control.canMutate("device_rename")) return;
    const name = $("room-name").value;
    // Python's existing name normalization is authoritative (JS trim differs
    // for U+FEFF and some whitespace); send every nonempty input unchanged.
    if (!name) { text("room-dialog-status", "Enter a room name of 1–128 characters."); $("room-name").focus(); return; }
    context.sent = true; text("room-dialog-status", "PENDING · Saving the name and waiting for discovery refresh…");
    const result = await run({action: "device_rename", name, expected_device: context.revision}, "Rename room");
    // Closing scrubs only this draft, never cancels or rolls back a sent rename.
    if (roomContext !== context) return;
    if (result.kind === "acknowledged" && result.deviceVerified) closeRoom();
    else { text("room-dialog-status", `${result.kind.toUpperCase()} · ${result.text}`); renderRoomRename(); }
  });
  async function run(body, label) { const result = await control.mutate(body, label); return result; }
  function command(action, values = {}) { return guardedAction(snapshot, action, values); }
  ["play", "pause", "stop", "next", "previous"].forEach(action => $(action).addEventListener("click", () => run(command(action), action[0].toUpperCase() + action.slice(1))));
  $("mute").addEventListener("click", () => run(command("mute", {value: !snapshot.gain.mute}), snapshot.gain.mute ? "Unmute" : "Mute"));
  $("volume-zero").addEventListener("click", () => {
    if (snapshot?.gain.status !== "UNKNOWN" || !control.canMutate("volume")) return;
    return run(command("volume", {value: 0}), "Set volume to 0");
  });
  $("eq").addEventListener("click", () => run(command("eq", {enabled: !snapshot.dsp.eq_enabled}), snapshot.dsp.eq_enabled ? "Bypass EQ" : "Enable EQ"));
  const inputLabel = {seek: value => formatTime(Number(value)), volume: value => value, balance: value => balanceLabel(Number(value))};
  ["seek", "volume", "mode", "balance"].forEach(key => {
    $(key).addEventListener("input", () => {
      drafts[key] = true; $(key + "-draft").hidden = false;
      if (["seek", "mode"].includes(key)) { $(key).dataset.generation = String(snapshot.generation); $(key).dataset.session = snapshot.session_id; $(key).dataset.queueName = snapshot.queue.name; }
      if (inputLabel[key]) text(key + "-target", inputLabel[key]($(key).value));
    });
    $(key + "-cancel").addEventListener("click", () => { drafts[key] = false; $(key + "-draft").hidden = true; $(key).blur(); render(); });
    $(key + "-form").addEventListener("submit", async event => {
      event.preventDefault();
      if (!drafts[key]) return;
      const value = $(key).value;
      if (["seek", "mode"].includes(key) && $(key).dataset.session !== snapshot.session_id) { drafts[key] = false; $(key + "-draft").hidden = true; control.notice = {kind: "error", text: "The renderer restarted. Choose the setting again using its current state."}; render(); return; }
      let body;
      if (key === "seek") body = {...command("seek", {seconds: Math.round(Number(value))}), expected_generation: Number($(key).dataset.generation)};
      else if (key === "mode") body = {...command("queue_mode", {mode: value}), expected_generation: Number($(key).dataset.generation), queue_name: $(key).dataset.queueName};
      else body = command(key, {value: Number(value)});
      const result = await run(body, key === "mode" ? "Playback mode" : key[0].toUpperCase() + key.slice(1));
      if (result.kind === "acknowledged") { drafts[key] = false; $(key + "-draft").hidden = true; $(key).blur(); render(); }
    });
  });
  function favoriteRecord(favorite) {
    // Compare only the redacted record, independently of response property order.
    return favorite && JSON.stringify([favorite.id, favorite.editable, favorite.name, favorite.title, favorite.artist, favorite.album, favorite.artwork, favorite.target_label]);
  }
  function renderFavoriteDraft(ready) {
    const context = dialogContext;
    if (!context) return;
    // This detects observed changes, not an atomic server-side precondition.
    // Save current keeps the selection from opening, even if a later observation
    // changes back. A failed favorites read does not hide fresh selection changes.
    if (context.mode === "current" && control.fresh && (context.generation !== snapshot.generation || context.session !== snapshot.session_id)) context.stale = true;
    // Rename/Delete latch even while pending, letting own successful readback
    // reach verification. Target drafts guard only before their single send;
    // the redacted list cannot verify that command’s exact saved URL.
    if (control.fresh && control.favoritesFresh && (["rename", "delete"].includes(context.mode) || context.mode === "target" && !context.sent)) {
      const current = control.favorites.find(item => item.id === context.favorite.id);
      if (favoriteRecord(current) !== context.record) context.stale = true;
    }
    $("favorite-submit").disabled = !ready || !control.favoritesFresh || context.pending || context.stale || context.sent;
    if (context.stale && !context.pending && !context.sent) {
      const guidance = context.mode === "current" ? "The current selection changed or the renderer restarted. Close this dialog and review the current track or stream before reopening Save current."
        : "This favorite changed or is no longer available. Close this dialog and review the favorites list before reopening " + (context.mode === "target" ? "Change URL." : "Rename or Delete.");
      text("favorite-dialog-status", (context.resultText ? context.resultText + " " : "") + guidance);
    }
  }
  function openFavorite(mode, favorite = null, opener = $(mode === "current" ? "save-current" : "add-favorite")) {
    if ($("favorite-dialog").open || !control.canMutate("favorite_add") || !control.favoritesFresh || mode === "target" && !favorite?.editable) return;
    dialogContext = {mode, favorite, record: favoriteRecord(favorite), stale: false, pending: false, sent: false, generation: snapshot.generation, session: snapshot.session_id, serial: ++dialogSerial};
    $("favorite-form").reset(); text("favorite-dialog-status", "");
    const labels = {add: "Add favorite", current: "Save current as favorite", rename: "Rename favorite", target: "Change favorite URL", delete: "Delete favorite"};
    text("favorite-dialog-heading", labels[mode]); text("favorite-submit", mode === "delete" ? "Delete favorite" : mode === "rename" ? "Save name" : mode === "target" ? "Change URL" : "Save favorite");
    text("favorite-dialog-hint", {add: "Save a direct HTTP or HTTPS audio URL.", current: "Save the current track or stream using its original playable address.", rename: "Change the name saved on this renderer.", target: "Changes the saved address for future playback. Keeps the favorite’s name and ID. Does not fetch audio or change current playback.", delete: "Remove this saved favorite? Playback will not be stopped."}[mode]);
    const target = mode === "target", urlVisible = mode === "add" || target;
    $("favorite-name-field").hidden = mode === "delete" || target; $("favorite-name").required = mode !== "delete" && !target;
    $("favorite-url-field").hidden = !urlVisible; $("favorite-url").required = urlVisible; $("favorite-url").disabled = false; $("favorite-url").value = "";
    text("favorite-url-label", target ? "Replacement audio URL" : "Audio URL");
    $("favorite-metadata-field").hidden = !target; $("favorite-metadata").required = target; $("favorite-metadata").disabled = !target; $("favorite-metadata").value = "";
    $("favorite-delete-name").hidden = mode !== "delete" && !target; text("favorite-delete-name", favorite?.name || "");
    text("favorite-cancel", "Cancel");
    $("favorite-name").value = favorite?.name || (mode === "current" ? snapshot.media.title : "");
    $("favorite-submit").classList.toggle("destructive", mode === "delete");
    $("favorite-submit").disabled = false;
    favoriteDialog.open(opener);
    (mode === "delete" ? $("favorite-cancel") : mode === "target" ? $("favorite-url") : $("favorite-name")).focus();
  }
  function clearFavoriteForm() {
    dialogContext = null;
    $("favorite-form").reset();
    $("favorite-url").value = "";
    $("favorite-name").value = "";
    $("favorite-metadata").value = ""; $("favorite-metadata").disabled = true;
    text("favorite-delete-name", ""); text("favorite-dialog-status", "");
  }
  function closeFavorite(restore = true) { favoriteDialog.close(restore); }
  // Closing the dialog clears local input only. An in-flight command continues
  // independently and its acknowledgement/unknown outcome stays in the notice.
  ["favorite-close", "favorite-cancel"].forEach(id => $(id).addEventListener("click", closeFavorite));
  $("add-favorite").addEventListener("click", () => openFavorite("add"));
  $("save-current").addEventListener("click", () => openFavorite("current"));
  $("favorite-form").addEventListener("submit", async event => {
    event.preventDefault();
    const context = dialogContext;
    if (!context) return;
    renderFavoriteDraft(control.canMutate("play"));
    if (context.stale || context.pending || context.sent || !control.canMutate("favorite_add")) return;
    const name = $("favorite-name").value;
    const url = $("favorite-url").value;
    if (!["delete", "target"].includes(context.mode) && !name.trim()) { text("favorite-dialog-status", "Enter a name for this favorite."); $("favorite-name").focus(); return; }
    if ((context.mode === "add" || context.mode === "target") && !safeAudioURL(url)) { text("favorite-dialog-status", "Use a direct HTTP or HTTPS URL without a username or password."); $("favorite-url").focus(); return; }
    const metadata = $("favorite-metadata").value;
    if (context.mode === "target" && !["keep", "clear"].includes(metadata)) { text("favorite-dialog-status", "Choose whether to keep or clear the saved metadata."); $("favorite-metadata").focus(); return; }
    let body;
    if (context.mode === "target") body = {action: "favorite_target", id: context.favorite.id, url, metadata};
    else if (context.mode === "add") body = {action: "favorite_add", name, url};
    else if (context.mode === "current") body = {action: "favorite_current", name, expected_generation: context.generation};
    else if (context.mode === "rename") body = {action: "favorite_rename", id: context.favorite.id, name};
    else body = {action: "favorite_delete", id: context.favorite.id};
    if (context.mode === "target") {
      if (new TextEncoder().encode(url).length > 8192 || requestBytes(body) > MAX_REQUEST_BYTES) { text("favorite-dialog-status", "Use a URL of at most 8,192 UTF-8 bytes and a complete request within 32,768 bytes."); $("favorite-url").focus(); return; }
      // Single-use after synchronous admission, including rejected/unknown
      // outcomes. Clear the private field before POST/readback; this is input
      // cleanup, not secure memory erasure. Reopening is a new deliberate draft.
      context.sent = true;
      $("favorite-url").value = ""; $("favorite-url").disabled = true; $("favorite-metadata").disabled = true;
      text("favorite-cancel", "Done"); $("favorite-cancel").focus();
    }
    context.pending = true;
    text("favorite-dialog-status", "PENDING · Waiting for the renderer…");
    const result = await run(body, {add: "Add favorite", current: "Save current", rename: "Rename favorite", target: "Change URL", delete: "Delete favorite"}[context.mode]);
    if (!dialogContext || dialogContext.serial !== context.serial) return;
    context.pending = false;
    if (context.mode === "target") {
      context.resultText = `${result.kind.toUpperCase()} · ${result.text}`;
      text("favorite-dialog-status", context.resultText);
      renderFavoriteDraft(control.canMutate("play"));
      return; // Redacted readback can never verify the exact saved target.
    }
    const favorites = control.favorites;
    const verified = control.favoritesFresh && result.kind === "acknowledged" && (context.mode === "delete" ? !favorites.some(item => item.id === context.favorite.id) : context.mode === "rename" ? favorites.some(item => item.id === context.favorite.id && item.name === name) : result.favoriteId && favorites.some(item => item.id === result.favoriteId && item.name === name));
    if (verified) closeFavorite();
    else {
      context.resultText = result.kind === "acknowledged" ? "Request acknowledged. The saved change has not yet been verified in the favorites list. Check the list before submitting again." : `${result.kind.toUpperCase()} · ${result.text}`;
      text("favorite-dialog-status", context.resultText);
      renderFavoriteDraft(control.canMutate("play"));
    }
  });
  DSP_KEYS.forEach(key => $("dsp-" + key).addEventListener("input", () => {
    if (!currentEditor() || !control.canMutate("dsp_bands") || soundDraft.stale) return;
    soundDraft.edit(key, $("dsp-" + key).value); renderSoundEditor();
  }));
  $("dsp-discard").addEventListener("click", () => {
    if (!currentEditor() || control.busy) return;
    soundSubmission = null; soundDraft.load(snapshot); renderSoundEditor();
  });
  $("dsp-band-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!control.canMutate("dsp_bands") || !soundDraft.canApply(snapshot, control.fresh)) return;
    const bands = soundDraft.bands, session = soundDraft.session, revision = soundDraft.revision;
    soundDraft.pending = true;
    const submission = {bands, session, outcome: "pending"}; soundSubmission = submission;
    const result = await run({action: "dsp_bands", expected_dsp: revision, bands}, "Apply EQ bands");
    if (soundSubmission !== submission) return;
    soundDraft.pending = false; submission.outcome = result.kind;
    if (result.code === "stale_dsp") soundDraft.stale = true;
    if (result.kind === "acknowledged" || result.kind === "unknown") soundDraft.review = true;
    else soundSubmission = null;
    renderSoundEditor();
  });
  $("dsp-preset").addEventListener("change", renderSoundEditor);
  $("dsp-load").addEventListener("click", async () => {
    const editor = currentEditor(), preset = editor?.presets.find(item => item.id === $("dsp-preset").value);
    if (!preset || !control.canMutate("dsp_preset_load") || soundDraft.dirty || soundDraft.stale || soundDraft.pending || soundDraft.review) return;
    soundDraft.load(snapshot);
    await run({action: "dsp_preset_load", expected_dsp: editor.revision, preset_id: preset.id}, "Load EQ preset");
  });
  function openPreset(mode) {
    const editor = currentEditor(), preset = editor?.presets.find(item => item.id === $("dsp-preset").value);
    if (!editor || !control.canMutate("dsp_preset_" + mode) || soundDraft.dirty || soundDraft.stale || soundDraft.pending || soundDraft.review || (mode !== "save" && preset?.kind !== "saved")) return;
    presetContext = {mode, preset, session: snapshot.session_id, revision: editor.revision, serial: ++presetSerial, sent: false, stale: false};
    $("dsp-name").value = mode === "rename" ? preset.name : ""; $("dsp-shared-confirm").checked = false;
    text("dsp-dialog-status", "");
    text("dsp-dialog-heading", {save: "Save active curve", rename: "Rename saved preset", delete: "Delete saved preset"}[mode]);
    text("dsp-preset-submit", {save: "Save preset", rename: "Save name", delete: "Delete preset"}[mode]);
    text("dsp-dialog-hint", {save: "Save the renderer’s confirmed active EQ curve as a new preset. Unsent band edits are never included.", rename: "Change this saved preset’s name. Existing names cannot be overwritten.", delete: "Remove this saved preset? The renderer’s active curve will remain unchanged."}[mode]);
    $("dsp-name-field").hidden = mode === "delete"; $("dsp-name").required = mode !== "delete";
    $("dsp-preset-name").hidden = mode === "save"; text("dsp-preset-name", preset?.name || "");
    const shared = mode !== "save" && preset.shared;
    $("dsp-shared-field").hidden = !shared; $("dsp-shared-confirm").disabled = !shared; $("dsp-shared-confirm").required = shared;
    text("dsp-shared-warning", `This preset is shared. ${mode === "delete" ? "Deleting" : "Renaming"} it affects ALL stored contexts that use it, including other source or channel settings.`);
    $("dsp-preset-submit").classList.toggle("destructive", mode === "delete");
    renderSoundEditor(); presetDialog.open($("dsp-" + mode)); (mode === "delete" ? $("dsp-cancel") : $("dsp-name")).focus();
  }
  function clearPresetForm() { presetContext = null; $("dsp-name").value = ""; $("dsp-shared-confirm").checked = false; text("dsp-preset-name", ""); text("dsp-dialog-status", ""); }
  function closePreset(restore = true) { presetDialog.close(restore); }
  ["save", "rename", "delete"].forEach(mode => $("dsp-" + mode).addEventListener("click", () => openPreset(mode)));
  ["dsp-close", "dsp-cancel"].forEach(id => $(id).addEventListener("click", closePreset));
  $("dsp-preset-form").addEventListener("submit", async event => {
    event.preventDefault();
    const context = presetContext, editor = currentEditor();
    if (!context || context.sent || !editor || !control.canMutate("dsp_preset_" + context.mode)) return;
    if (context.stale || context.session !== snapshot.session_id || context.revision !== editor.revision) { context.stale = true; text("dsp-dialog-status", "Sound settings or presets changed, or the renderer restarted. Close this dialog and review the latest state before continuing."); renderSoundEditor(); return; }
    if (soundDraft.dirty || soundDraft.stale || soundDraft.pending || soundDraft.review) { text("dsp-dialog-status", "Apply or discard your local band draft first."); return; }
    const name = $("dsp-name").value.trim();
    if (context.mode !== "delete" && (!name || new TextEncoder().encode(name).length > 256)) { text("dsp-dialog-status", "Enter a plain preset name of 1–256 UTF-8 bytes."); $("dsp-name").focus(); return; }
    if (context.mode !== "save" && context.preset.shared && !$("dsp-shared-confirm").checked) { text("dsp-dialog-status", "Confirm that this change affects all stored contexts using this preset."); $("dsp-shared-confirm").focus(); return; }
    const body = {action: "dsp_preset_" + context.mode, expected_dsp: context.revision};
    if (context.mode !== "delete") body.name = name;
    if (context.mode !== "save") body.preset_id = context.preset.id;
    context.sent = true; text("dsp-dialog-status", "PENDING · Waiting for the renderer…");
    const result = await run(body, {save: "Save EQ preset", rename: "Rename EQ preset", delete: "Delete EQ preset"}[context.mode]);
    if (!presetContext || presetContext.serial !== context.serial) return;
    const current = currentEditor();
    const oldRemoved = current && !current.presets.some(item => item.kind === "saved" && item.id === context.preset?.id);
    const named = current?.presets.some(item => item.kind === "saved" && item.name === name);
    const verified = result.kind === "acknowledged" && context.session === snapshot.session_id && current && current.library_unavailable !== true && (context.mode === "delete" ? oldRemoved : context.mode === "rename" ? named && (name === context.preset.name || oldRemoved) : named);
    if (verified) closePreset();
    else {
      context.sent = result.kind !== "error" && result.kind !== "blocked";
      if (result.code === "stale_dsp") context.stale = true;
      renderSoundEditor();
      const message = result.kind === "acknowledged" ? "Request acknowledged, but the saved change has not been verified. Close this dialog and review the preset list before another request." : `${result.kind.toUpperCase()} · ${result.text}`;
      text("dsp-dialog-status", message + (current?.library_unavailable === true ? " " + DSP_LIBRARY_UNAVAILABLE_MESSAGE : ""));
    }
  });
  $("artwork").addEventListener("load", () => { if ($("artwork").getAttribute("src") === artworkURL) { $("artwork").hidden = false; $("artwork-fallback").hidden = true; } });
  $("artwork").addEventListener("error", () => { $("artwork").hidden = true; $("artwork-fallback").hidden = false; });
  async function poll() { const run = ++pollRun; window.clearTimeout(pollTimer); await control.refresh(); if (run === pollRun) pollTimer = window.setTimeout(poll, document.hidden ? 10000 : 2000); }
  $("refresh").addEventListener("click", async () => {
    // Capture only completed, verified rejections already known at this click.
    // A Refresh while a request is pending cannot authorize a later rejection.
    const operation = control.operation, review = ++queueReview;
    const rejected = control.busy ? [] : Array.from($("queue-list").querySelectorAll("button"))
      .filter(button => queueIntents.get(button)?.rejectedOperation === operation);
    await poll();
    if (review !== queueReview || !control.fresh || control.busy || control.operation !== operation) return;
    let rearmed = false;
    rejected.forEach(button => {
      const intent = queueIntents.get(button);
      if (!button.isConnected || intent.rejectedOperation !== operation || !intent.sent || !intent.allowed || intent.eligibility.stale || intent.edge || intent.stale
          || intent.session !== snapshot.session_id || intent.generation !== snapshot.generation || !control.canMutate(intent.action)) return;
      intent.sent = false; intent.rejectedOperation = null; rearmed = true;
    });
    if (rearmed) {
      if (control.notice.id === operation && control.notice.queueEditRejected === true) control.notice = {...control.notice, text: "Queue refreshed. The rejected edit is available again; no edit was sent. Review the queue before choosing it."};
      render();
    }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  // Scrub private dialog input before navigation/BFCache, even if already closed.
  // Only local drafts are dismissed; sent commands and their outcomes stay live.
  window.addEventListener("pagehide", () => { queueReview += 1; outputInspection.dismiss(); renderOutputInspection(); closePlaylist(false); closeFavorite(false); closePreset(false); closeExport(false); closeRoom(false); curveExportDialog.close(false); curveImportDialog.close(false); favoritesExportDialog.close(false); favoritesRestoreDialog.close(false); });
  window.addEventListener("online", () => poll());
  window.addEventListener("offline", () => { control.fresh = false; control.favoritesFresh = false; control.favoritesReadFailure = null; render(); });
  poll();
})();
