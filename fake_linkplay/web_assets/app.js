/* Same-origin UI. Canonical snapshots are the only source of displayed state. */
(function () {
  "use strict";
  const {Controller, PlaylistDraft, MAX_REQUEST_BYTES, requestBytes, playlistAvailable, pastedURLs, playlistPreview, DspDraft, DSP_KEYS, MODES, safeAudioURL, safeArtwork, formatTime, guardedAction} = window.RendererControl;
  const $ = id => document.getElementById(id);
  const drafts = {seek: false, volume: false, mode: false, balance: false};
  let snapshot = null, queueSignature = "", favoritesSignature = "", artworkURL = null;
  let dialogContext = null, dialogSerial = 0, pollTimer = null, pollRun = 0;
  const soundDraft = new DspDraft();
  let playlistDraft = null, queueEditing = null;
  const queueIntents = new WeakMap();
  let presetSignature = "", presetContext = null, presetSerial = 0, soundSubmission = null;
  const text = (id, value) => { const element = $(id); if (element.textContent !== value) element.textContent = value; };
  const balanceLabel = value => value === 0 ? "Center" : `${Math.round(Math.abs(value) * 100)}% ${value < 0 ? "left" : "right"}`;
  const working = key => drafts[key] || document.activeElement === $(key);
  async function request(path, options) {
    const abort = new AbortController();
    const timer = window.setTimeout(() => abort.abort(), options.method === "POST" ? 20000 : 8000);
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
      return {status: response.status, body: JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes))};
    } finally { window.clearTimeout(timer); }
  }
  const control = new Controller(request, render);
  function noticeView() {
    let {kind, text: message} = control.notice;
    if (!control.fresh && !control.busy) { kind = "unknown"; message = "Connection lost or state unavailable. Displayed values are stale. " + (control.operation ? message : "Refresh to reconnect."); }
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
    $("refresh").disabled = false;
    if (!snapshot) return;
    const {media, transport, gain, queue, dsp} = snapshot;
    const ready = control.canMutate("play");
    text("device-name", snapshot.device.name || "Listening room");
    text("track-title", media.title || (queue.count ? "Untitled track" : "Ready when you are"));
    text("track-artist", media.artist || (queue.count ? "Artist unavailable" : "Choose something from your favorites."));
    text("track-album", media.album);
    text("source-label", media.is_live ? "LIVE STREAM" : "OPEN ROOM");
    const phaseLabel = transport.status === "ERROR" ? "ERROR" : transport.status === "PENDING" && ["PLAYING", "PAUSED", "STOPPED"].includes(transport.phase) ? "PENDING" : transport.phase.replaceAll("_", " ");
    text("playback-state", control.fresh ? phaseLabel : "UNKNOWN · STALE");
    $("playback-state").dataset.kind = control.fresh ? transport.status.toLowerCase() : "unknown";
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
    renderSoundEditor(); renderQueue(ready); renderPlaylist(); renderFavorites(ready);
    text("freshness", control.lastReadAt ? `${control.fresh ? "State received" : "Last state received"} ${new Date(control.lastReadAt).toLocaleTimeString()}. Playback status comes from the renderer.` : "No state received yet.");
    $("add-favorite").disabled = !ready || !control.favoritesFresh;
    $("save-current").disabled = !ready || !control.favoritesFresh || !snapshot.actions.save_current;
    if (dialogContext) $("favorite-submit").disabled = !ready || !control.favoritesFresh;
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
    $("dsp-preset").disabled = !available || !clean;
    $("dsp-load").disabled = !available || !clean || !selected;
    $("dsp-save").disabled = !available || !clean;
    $("dsp-rename").disabled = !available || !clean || selected?.kind !== "saved";
    $("dsp-delete").disabled = !available || !clean || selected?.kind !== "saved";
    text("dsp-save-hint", !clean ? "Apply or discard your draft before saving or changing presets. Save active curve never saves unsent edits." : "Saves only the confirmed active curve. Loading a preset enables EQ.");
    if (presetContext) {
      if (!presetContext.sent && (presetContext.session !== snapshot.session_id || (editor && presetContext.revision !== editor.revision))) presetContext.stale = true;
      const stale = presetContext.stale || !editor;
      $("dsp-preset-submit").disabled = !available || !clean || stale || presetContext.sent;
      $("dsp-name").disabled = presetContext.sent || stale;
      $("dsp-shared-confirm").disabled = presetContext.mode === "save" || !presetContext.preset?.shared || presetContext.sent || stale;
      if (stale && !presetContext.sent) text("dsp-dialog-status", "Sound settings or presets changed, or readback is unavailable. Close this dialog and review the latest state before continuing.");
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
            await run({action, expected_generation: intent.generation, ...values}, direction === "remove" ? "Remove queue entry" : "Move queue entry");
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
  function closePlaylist() { clearPlaylist(); $("playlist-dialog").close(); }
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
    renderPlaylist(); $("playlist-dialog").showModal(); $(append ? "playlist-urls" : "playlist-format").focus();
  }
  $("playlist-load").addEventListener("click", () => openPlaylist("playlist_replace"));
  $("playlist-add").addEventListener("click", () => openPlaylist("playlist_append"));
  ["playlist-close", "playlist-cancel"].forEach(id => $(id).addEventListener("click", closePlaylist));
  // Native close events are queued: an old event must not erase a reopened draft.
  $("playlist-dialog").addEventListener("close", () => { if (!$("playlist-dialog").open) clearPlaylist(); });
  $("playlist-dialog").addEventListener("cancel", event => { event.preventDefault(); closePlaylist(); });
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
  function renderFavorites(ready) {
    text("favorites-count", String(control.favorites.length));
    $("favorites-empty").hidden = control.favorites.length > 0;
    text("favorites-note", control.favoritesFresh ? "Saved on this renderer. Configuration favorites are read-only." : "Favorites could not be refreshed. This list may be stale.");
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
          [["rename", "Rename"], ["delete", "Delete"]].forEach(([action, label]) => {
            const button = element("button", "button subtle small", label); button.type = "button"; button.dataset.favoriteKey = `${action}:${favorite.id}`; button.setAttribute("aria-label", `${label} favorite: ${favorite.name}`); button.addEventListener("click", () => openFavorite(action, favorite)); actions.append(button);
          });
        } else actions.append(element("span", "read-only", "From config"));
        row.append(actions); fragment.append(row);
      });
      $("favorites-list").replaceChildren(fragment);
      if (focused) Array.from($("favorites-list").querySelectorAll("button")).find(button => button.dataset.favoriteKey === focused)?.focus({preventScroll: true});
    }
    $("favorites-list").querySelectorAll("button").forEach(button => { button.disabled = !ready || !control.favoritesFresh; });
  }
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
  function openFavorite(mode, favorite = null) {
    if (!control.canMutate("favorite_add") || !control.favoritesFresh) return;
    dialogContext = {mode, favorite, generation: snapshot.generation, session: snapshot.session_id, serial: ++dialogSerial};
    $("favorite-form").reset(); text("favorite-dialog-status", "");
    const labels = {add: "Add favorite", current: "Save current as favorite", rename: "Rename favorite", delete: "Delete favorite"};
    text("favorite-dialog-heading", labels[mode]); text("favorite-submit", mode === "delete" ? "Delete favorite" : mode === "rename" ? "Save name" : "Save favorite");
    text("favorite-dialog-hint", {add: "Save a direct HTTP or HTTPS audio URL.", current: "Save the current track or stream using its original playable address.", rename: "Change the name saved on this renderer.", delete: "Remove this saved favorite? Playback will not be stopped."}[mode]);
    $("favorite-name-field").hidden = mode === "delete"; $("favorite-name").required = mode !== "delete";
    $("favorite-url-field").hidden = mode !== "add"; $("favorite-url").required = mode === "add";
    $("favorite-delete-name").hidden = mode !== "delete"; text("favorite-delete-name", favorite?.name || "");
    $("favorite-name").value = favorite?.name || (mode === "current" ? snapshot.media.title : "");
    $("favorite-submit").classList.toggle("destructive", mode === "delete");
    $("favorite-submit").disabled = false;
    $("favorite-dialog").showModal();
    (mode === "delete" ? $("favorite-cancel") : $("favorite-name")).focus();
  }
  function clearFavoriteForm() {
    dialogContext = null;
    $("favorite-form").reset();
    $("favorite-url").value = "";
    $("favorite-name").value = "";
    text("favorite-delete-name", ""); text("favorite-dialog-status", "");
  }
  function closeFavorite() { $("favorite-dialog").close(); clearFavoriteForm(); }
  // Closing the dialog clears local input only. An in-flight command continues
  // independently and its acknowledgement/unknown outcome stays in the notice.
  // A queued close event from an older dialog must not clear its reopened draft.
  $("favorite-dialog").addEventListener("close", () => { if (!$("favorite-dialog").open) clearFavoriteForm(); });
  ["favorite-close", "favorite-cancel"].forEach(id => $(id).addEventListener("click", closeFavorite));
  $("add-favorite").addEventListener("click", () => openFavorite("add"));
  $("save-current").addEventListener("click", () => openFavorite("current"));
  $("favorite-form").addEventListener("submit", async event => {
    event.preventDefault();
    const context = dialogContext;
    if (!context || !control.canMutate("favorite_add")) return;
    if (context.mode === "current" && context.session !== snapshot.session_id) { text("favorite-dialog-status", "The renderer restarted. Close this dialog and choose Save current again."); return; }
    const name = $("favorite-name").value;
    const url = $("favorite-url").value;
    if (context.mode !== "delete" && !name.trim()) { text("favorite-dialog-status", "Enter a name for this favorite."); $("favorite-name").focus(); return; }
    if (context.mode === "add" && !safeAudioURL(url)) { text("favorite-dialog-status", "Use a direct HTTP or HTTPS URL without a username or password."); $("favorite-url").focus(); return; }
    let body;
    if (context.mode === "add") body = {action: "favorite_add", name, url};
    else if (context.mode === "current") body = {action: "favorite_current", name, expected_generation: context.generation};
    else if (context.mode === "rename") body = {action: "favorite_rename", id: context.favorite.id, name};
    else body = {action: "favorite_delete", id: context.favorite.id};
    text("favorite-dialog-status", "PENDING · Waiting for the renderer…");
    const result = await run(body, {add: "Add favorite", current: "Save current", rename: "Rename favorite", delete: "Delete favorite"}[context.mode]);
    if (!dialogContext || dialogContext.serial !== context.serial) return;
    const favorites = control.favorites;
    const verified = control.favoritesFresh && result.kind === "acknowledged" && (context.mode === "delete" ? !favorites.some(item => item.id === context.favorite.id) : context.mode === "rename" ? favorites.some(item => item.id === context.favorite.id && item.name === name) : result.favoriteId && favorites.some(item => item.id === result.favoriteId && item.name === name));
    if (verified) closeFavorite();
    else text("favorite-dialog-status", result.kind === "acknowledged" ? "Request acknowledged. The saved change has not yet been verified in the favorites list. Check the list before submitting again." : `${result.kind.toUpperCase()} · ${result.text}`);
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
    renderSoundEditor(); $("dsp-dialog").showModal(); (mode === "delete" ? $("dsp-cancel") : $("dsp-name")).focus();
  }
  function clearPresetForm() { presetContext = null; $("dsp-name").value = ""; $("dsp-shared-confirm").checked = false; text("dsp-preset-name", ""); text("dsp-dialog-status", ""); }
  function closePreset() { $("dsp-dialog").close(); clearPresetForm(); }
  ["save", "rename", "delete"].forEach(mode => $("dsp-" + mode).addEventListener("click", () => openPreset(mode)));
  ["dsp-close", "dsp-cancel"].forEach(id => $(id).addEventListener("click", closePreset));
  $("dsp-dialog").addEventListener("close", () => { if (!$("dsp-dialog").open) clearPresetForm(); });
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
    const verified = result.kind === "acknowledged" && context.session === snapshot.session_id && current && (context.mode === "delete" ? oldRemoved : context.mode === "rename" ? named && (name === context.preset.name || oldRemoved) : named);
    if (verified) closePreset();
    else {
      context.sent = result.kind !== "error" && result.kind !== "blocked";
      if (result.code === "stale_dsp") context.stale = true;
      renderSoundEditor();
      text("dsp-dialog-status", result.kind === "acknowledged" ? "Request acknowledged, but the saved change has not been verified. Close this dialog and review the preset list before another request." : `${result.kind.toUpperCase()} · ${result.text}`);
    }
  });
  $("artwork").addEventListener("load", () => { if ($("artwork").getAttribute("src") === artworkURL) { $("artwork").hidden = false; $("artwork-fallback").hidden = true; } });
  $("artwork").addEventListener("error", () => { $("artwork").hidden = true; $("artwork-fallback").hidden = false; });
  async function poll() { const run = ++pollRun; window.clearTimeout(pollTimer); await control.refresh(); if (run === pollRun) pollTimer = window.setTimeout(poll, document.hidden ? 10000 : 2000); }
  $("refresh").addEventListener("click", () => { poll(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  // Scrub private dialog input before navigation/BFCache, even if already closed.
  // Only local drafts are dismissed; sent commands and their outcomes stay live.
  window.addEventListener("pagehide", () => { closePlaylist(); closeFavorite(); closePreset(); });
  window.addEventListener("online", () => poll());
  window.addEventListener("offline", () => { control.fresh = false; control.favoritesFresh = false; render(); });
  poll();
})();
