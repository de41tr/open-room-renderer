/* Browser-independent, single-shot control flow. No command is retried. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RendererControl = Object.freeze(api);
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const MODES = Object.freeze({"normal": "In order", "repeat-one": "Repeat one", "repeat-all": "Repeat all", "shuffle-once": "Shuffle", "shuffle-repeat-all": "Shuffle + repeat all"});
  const PHASES = ["COMMAND_ACCEPTED", "LOADING", "BUFFERING", "PLAYING", "PAUSED", "STOPPED", "ERROR"];
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const optionalNumber = value => value === null || (typeof value === "number" && Number.isFinite(value) && value >= 0);
  const string = value => typeof value === "string";
  const optionalBoolean = value => value === null || typeof value === "boolean";
  const DSP_MODES = Object.freeze({"-1": "Bypass band", "0": "Low shelf", "1": "Peak", "2": "High shelf", "3": "Low pass", "5": "High pass"});
  const DSP_FIELDS = Object.freeze({freq: {label: "Frequency (Hz)", min: 10, max: 24000}, gain: {label: "Gain (dB)", min: -24, max: 24}, q: {label: "Q", min: 0.1, max: 30}, mode: {label: "Filter"}});
  const DSP_KEYS = Object.freeze(Array.from("abcdefghij", band => Object.keys(DSP_FIELDS).map(field => `${band}_${field}`)).flat());
  const DSP_LIBRARY_ACTIONS = Object.freeze(["dsp_preset_save", "dsp_preset_rename", "dsp_preset_delete"]);
  const DSP_LIBRARY_UNAVAILABLE_MESSAGE = "Saved EQ presets could not be loaded. Stop the renderer service, repair or restore its saved EQ preset library, then restart it.";
  const opaqueID = value => string(value) && /^[a-f0-9]{64}$/.test(value);
  function validBandValue(key, value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return false;
    const field = key.split("_")[1];
    return field === "mode" ? Object.hasOwn(DSP_MODES, String(value)) : value >= DSP_FIELDS[field].min && value <= DSP_FIELDS[field].max;
  }
  function validateEditor(editor) {
    if (!isObject(editor) || !opaqueID(editor.revision) || !["global", "wifi"].includes(editor.source) || editor.channel_mode !== "Stereo" || !Array.isArray(editor.bands) || editor.bands.length !== DSP_KEYS.length || !Array.isArray(editor.presets)) throw new Error("Invalid sound editor snapshot");
    if (Object.hasOwn(editor, "library_unavailable") && typeof editor.library_unavailable !== "boolean") throw new Error("Invalid sound preset library availability");
    editor.bands.forEach((band, index) => { if (!isObject(band) || band.param_name !== DSP_KEYS[index] || !validBandValue(band.param_name, band.value)) throw new Error("Invalid sound band"); });
    const ids = new Set();
    editor.presets.forEach(preset => {
      if (!isObject(preset) || !opaqueID(preset.id) || ids.has(preset.id) || !string(preset.name) || !preset.name.trim() || !["builtin", "saved"].includes(preset.kind) || typeof preset.shared !== "boolean") throw new Error("Invalid sound preset");
      ids.add(preset.id);
    });
    return editor;
  }
  // Local input is never renderer state. A changed revision stays stale until
  // the user explicitly discards/reloads it, including across process restarts.
  class DspDraft {
    constructor() { this.reset(); }
    reset() { this.session = null; this.revision = null; this.values = null; this.original = null; this.engaged = false; this.stale = false; this.pending = false; this.review = false; }
    observe(state, fresh) {
      const editor = fresh && state?.dsp.status === "CONFIRMED" ? state.dsp.editor : null;
      if (!editor) return;
      if (this.engaged || this.pending || this.review) {
        if (this.session !== state.session_id || this.revision !== editor.revision) this.stale = true;
      } else if (this.session !== state.session_id || this.revision !== editor.revision) this.load(state);
    }
    load(state) {
      this.reset();
      const editor = state?.dsp.status === "CONFIRMED" ? state.dsp.editor : null;
      if (!editor) return;
      this.session = state.session_id; this.revision = editor.revision;
      this.values = Object.fromEntries(editor.bands.map(band => [band.param_name, String(band.value)]));
      this.original = {...this.values};
    }
    edit(key, value) {
      if (!this.values || !DSP_KEYS.includes(key) || this.pending || this.review) return;
      this.engaged = true; this.values[key] = value;
    }
    get dirty() { return Boolean(this.values && DSP_KEYS.some(key => this.values[key].trim() === "" || Number(this.values[key]) !== Number(this.original[key]))); }
    get bands() {
      if (!this.values || DSP_KEYS.some(key => !string(this.values[key]) || !this.values[key].trim() || !validBandValue(key, Number(this.values[key])))) return null;
      return DSP_KEYS.map(param_name => ({param_name, value: Number(this.values[param_name])}));
    }
    canApply(state, fresh) { return Boolean(fresh && state?.dsp.status === "CONFIRMED" && state.dsp.editor && this.session === state.session_id && this.revision === state.dsp.editor.revision && this.dirty && this.bands && !this.stale && !this.pending && !this.review); }
  }
  const PLAYLIST_ACTIONS = Object.freeze(["playlist_replace", "playlist_append", "playlist_remove", "playlist_move"]);
  const MAX_REQUEST_BYTES = 32768;
  function requestBytes(body) { return new TextEncoder().encode(JSON.stringify(body)).length; }
  const EXPORT_FILENAME = "open-room-renderer-playlist.json";
  function exportIdentity(state) {
    return JSON.stringify([state.session_id, state.generation, state.queue.name, state.queue.count, state.queue.index, state.queue.mode]);
  }
  function exportObject(bytes) {
    if (!(bytes instanceof Uint8Array) || !bytes.length || bytes.length > MAX_REQUEST_BYTES) throw new Error("Invalid export response");
    const source = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes), value = JSON.parse(source);
    if (!isObject(value)) throw new Error("Invalid export response");
    // The export schemas have one object with scalar fields (plus a URL array).
    // Tokenize valid JSON strings intact so duplicate keys cannot be hidden by
    // escapes. Nested objects are rejected by the exact schema validators.
    const tokens = source.match(/"(?:\\.|[^"\\])*"|[{}\[\],:]|[^ \t\r\n{}\[\],:]+/g), keys = new Set();
    tokens.forEach((token, index) => {
      if (/^-?[0-9]/.test(token) && !/^(0|[1-9][0-9]*)$/.test(token)) throw new Error("Invalid export response");
      if (tokens[index + 1] !== ":") return;
      const key = JSON.parse(token);
      if (keys.has(key)) throw new Error("Invalid export response");
      keys.add(key);
    });
    return value;
  }
  function exactFields(value, fields) { return isObject(value) && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key)); }
  function validateExportSummary(value, state) {
    if (!exactFields(value, ["version", "session_id", "generation", "revision", "name", "count", "index", "mode", "size_bytes", "filename"]) || value.version !== 1 || value.session_id !== state.session_id || value.generation !== state.generation || !integer(value.generation) || !opaqueID(value.revision) || value.filename !== EXPORT_FILENAME || !integer(value.size_bytes) || value.size_bytes < 1 || value.size_bytes > MAX_REQUEST_BYTES || !string(value.name) || value.name !== state.queue.name || value.count !== state.queue.count || !integer(value.count) || value.count < 1 || value.count > 1000 || value.index !== state.queue.index || !integer(value.index) || value.index < 1 || value.index > value.count || value.mode !== state.queue.mode || !Object.hasOwn(MODES, value.mode)) throw new Error("Invalid export summary");
    return value;
  }
  function validateExportDocument(bytes, summary) {
    const value = exportObject(bytes);
    if (bytes.length !== summary.size_bytes || !exactFields(value, ["version", "name", "index", "mode", "urls"]) || value.version !== 1 || !string(value.name) || !value.name.length || /[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]/u.test(value.name) || new TextEncoder().encode(value.name).length > 256 || value.index !== summary.index || value.mode !== summary.mode || !Array.isArray(value.urls) || value.urls.length !== summary.count || value.urls.some(url => !string(url) || !url.length || /[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]/u.test(url))) throw new Error("Invalid export document");
    // Validate the authored schema without rewriting the codec bytes or using
    // WHATWG URL parsing: the server alone checks URL/import eligibility,
    // including references (such as scoped IPv6) browsers cannot navigate.
    return bytes;
  }
  function exportFailure(status, value) {
    if (status === 409 && exactFields(value, ["error", "message", "outcome"]) && value.outcome === "REJECTED") {
      if (value.error === "export_unavailable") return "This queue cannot be exported here. Use playlist save-current FILE in the CLI for unsupported or larger queues. Web import still requires the complete escaped request to fit 32,768 bytes.";
      if (value.error === "stale_export") return "The queue or renderer session changed. Close this dialog, reopen Export JSON and review it again.";
    }
    return "Could not prepare the private export. Close this dialog and review current state before trying again. No download was started.";
  }
  async function readExportResponse(response, signal, expectedSize) {
    let reader = null, complete = false, onAbort;
    const aborted = new Promise((_, reject) => {
      onAbort = () => reject(new Error("Export cancelled"));
      signal.addEventListener("abort", onAbort, {once: true});
    });
    const chunks = [];
    try {
      if (signal.aborted) throw new Error("Export cancelled");
      const length = response.headers.get("Content-Length"), type = response.headers.get("Content-Type") || "";
      if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(type) || !/^(0|[1-9][0-9]*)$/.test(length || "")) throw new Error("Invalid export response");
      const size = Number(length);
      if (!integer(size) || size < 1 || size > MAX_REQUEST_BYTES) throw new Error("Invalid export size");
      if (response.status === 200 && expectedSize !== undefined && (size !== expectedSize || response.headers.get("Content-Disposition") !== `attachment; filename="${EXPORT_FILENAME}"`)) throw new Error("Invalid export attachment");
      reader = response.body.getReader();
      let total = 0;
      while (true) {
        const {done, value} = await Promise.race([reader.read(), aborted]);
        if (signal.aborted) throw new Error("Export cancelled");
        if (done) break;
        if (!(value instanceof Uint8Array) || value.length > size - total) throw new Error("Invalid export size");
        total += value.length; if (value.length) chunks.push(value);
      }
      if (total !== size) throw new Error("Invalid export size");
      const bytes = new Uint8Array(total); let offset = 0;
      chunks.forEach(chunk => { bytes.set(chunk, offset); offset += chunk.length; });
      complete = true;
      return {status: response.status, bytes};
    } finally {
      signal.removeEventListener("abort", onAbort);
      if (!complete) {
        // Do not await cancellation: an uncooperative read must not block Close
        // or the independent timeout. Late read results are never retained.
        if (reader) reader.cancel().catch(() => {});
        else if (response.body) response.body.cancel().catch(() => {});
      }
      chunks.length = 0;
    }
  }
  function playlistAvailable(state, action) {
    if (!state || !PLAYLIST_ACTIONS.includes(action) || state.transport.status !== "CONFIRMED" || state.transport.phase !== "STOPPED" || state.actions[action] !== true) return false;
    if (action === "playlist_replace") return true;
    return state.queue.playlist_edit_reason === "supported" && (action === "playlist_append" ? state.queue.count > 0 && state.queue.count < 1000 : state.queue.count > 1);
  }
  function pastedURLs(text) {
    // Remove only LF/CRLF separators and truly empty lines, never URL bytes.
    if (!string(text)) return null;
    const urls = text.split(/\r?\n/).filter(line => line !== "");
    return urls.length >= 1 && urls.length <= 1000 && urls.every(url => safeAudioURL(url)) ? urls : null;
  }
  function playlistPreview(format, source, settings) {
    let urls, index, mode;
    try {
      if (format === "json") {
        // This is only a local summary. Send the original text (including BOM)
        // to the authoritative decoder, even when a summary is unavailable.
        const document = JSON.parse(source);
        if (!isObject(document) || document.version !== 1 || !Array.isArray(document.urls)) return null;
        urls = document.urls; index = document.index; mode = document.mode;
      } else {
        urls = format === "urls" ? pastedURLs(source) : source.replace(/^\ufeff/, "").split(/\r?\n/).filter(line => line !== "" && !line.startsWith("#"));
        index = settings.index; mode = settings.mode;
      }
      if (!urls || urls.length < 1 || urls.length > 1000 || !integer(index) || index < 1 || index > urls.length || !Object.hasOwn(MODES, mode)) return null;
      return {count: urls.length, index, mode};
    } catch (_) { return null; }
  }
  class PlaylistDraft {
    constructor(state, action) {
      this.session = state.session_id; this.generation = state.generation; this.action = action;
      this.allowed = playlistAvailable(state, action); this.stale = false; this.sent = false; this.cancelled = false;
      this.format = "urls"; this.text = ""; this.reading = false; this.error = ""; this.serial = 0;
    }
    observe(state, fresh) {
      if (!this.sent && fresh && (this.session !== state.session_id || this.generation !== state.generation || !playlistAvailable(state, this.action))) this.stale = true;
    }
    canSubmit(state, fresh) {
      return Boolean(fresh && this.allowed && !this.stale && !this.sent && !this.reading && !this.cancelled && this.session === state?.session_id && this.generation === state.generation && playlistAvailable(state, this.action));
    }
    setSource(format, text = "") { if (this.sent || this.cancelled) return; this.serial += 1; this.format = format; this.text = text; this.reading = false; this.error = ""; }
    async readFile(file) {
      if (this.sent || this.cancelled) return;
      const serial = ++this.serial; this.text = ""; this.error = ""; this.reading = true;
      try {
        if (!file || !integer(file.size) || file.size === 0 || file.size > MAX_REQUEST_BYTES) throw new Error();
        const size = file.size, reading = file.arrayBuffer(); file = null;
        const bytes = await reading;
        if (this.serial !== serial || this.cancelled) return;
        if (bytes.byteLength !== size) throw new Error();
        const source = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes);
        if (this.serial === serial && !this.cancelled) this.text = source;
      } catch (_) {
        if (this.serial === serial && !this.cancelled) this.error = "Could not read this file as a bounded UTF-8 playlist. Choose a UTF-8 file; the complete request must fit 32,768 bytes.";
      } finally { if (this.serial === serial && !this.cancelled) this.reading = false; }
    }
    cancel() { this.cancelled = true; this.serial += 1; this.text = ""; this.error = ""; this.reading = false; }
  }
  function validateState(state) {
    if (!isObject(state) || state.version !== 1 || !string(state.session_id) || !state.session_id || !integer(state.revision) || !integer(state.generation) || !string(state.csrf_token) || !state.csrf_token || !isObject(state.device) || !string(state.device.name)) throw new Error("Invalid renderer snapshot");
    if (state.device.revision !== undefined && (!string(state.device.revision) || !/^[0-9a-f]{64}$/.test(state.device.revision) || typeof state.device.name_visible !== "boolean")) throw new Error("Invalid room name snapshot");
    const {transport, media, gain, queue, dsp, actions} = state;
    if (!isObject(transport) || !PHASES.includes(transport.phase) || !["CONFIRMED", "PENDING", "ERROR"].includes(transport.status) || !string(transport.message)) throw new Error("Invalid transport snapshot");
    if (transport.recovery !== undefined && transport.recovery !== null) {
      const recovery = transport.recovery;
      if (!isObject(recovery) || !string(recovery.message) || !["station_retry", "station_exhausted", "backend_control"].includes(recovery.kind)) throw new Error("Invalid recovery snapshot");
      if (recovery.kind === "station_retry") {
        if (transport.status !== "PENDING" || !["LOADING", "BUFFERING"].includes(transport.phase) || !integer(recovery.attempts_used) || !integer(recovery.attempt_limit) || recovery.attempt_limit < 1 || recovery.attempts_used > recovery.attempt_limit) throw new Error("Invalid recovery progress");
      } else if (transport.status !== "ERROR" || transport.phase !== "ERROR" || recovery.attempts_used !== undefined || recovery.attempt_limit !== undefined) throw new Error("Invalid recovery error");
    }
    if (!isObject(media) || ![media.title, media.artist, media.album].every(string) || !(media.artwork === null || string(media.artwork)) || typeof media.is_live !== "boolean" || !optionalNumber(media.position_seconds) || !optionalNumber(media.duration_seconds)) throw new Error("Invalid media snapshot");
    if (!isObject(gain) || !["CONFIRMED", "PENDING", "UNKNOWN"].includes(gain.status) || !(gain.volume === null || (integer(gain.volume) && gain.volume <= 100)) || !optionalBoolean(gain.mute)) throw new Error("Invalid gain snapshot");
    if (!isObject(queue) || !string(queue.name) || !integer(queue.count) || queue.count > 1000 || !integer(queue.index) || !Object.hasOwn(MODES, queue.mode) || !Array.isArray(queue.items) || queue.items.length !== queue.count || (queue.count === 0 ? queue.index !== 0 : queue.index < 1 || queue.index > queue.count)) throw new Error("Invalid queue snapshot");
    if (queue.playlist_edit_reason !== undefined && !["supported", "empty_queue", "unsupported_queue"].includes(queue.playlist_edit_reason)) throw new Error("Invalid queue edit capability");
    queue.items.forEach((item, offset) => { if (!isObject(item) || item.index !== offset + 1 || ![item.title, item.artist, item.album].every(string) || !optionalNumber(item.duration_seconds)) throw new Error("Invalid queue item"); });
    if (!isObject(dsp) || !["CONFIRMED", "PENDING", "UNKNOWN"].includes(dsp.status) || !(dsp.balance === null || (typeof dsp.balance === "number" && Number.isFinite(dsp.balance) && Math.abs(dsp.balance) <= 1)) || !optionalBoolean(dsp.eq_enabled) || !string(dsp.name)) throw new Error("Invalid sound snapshot");
    if (dsp.editor !== null) { if (dsp.status !== "CONFIRMED") throw new Error("Unconfirmed sound editor"); validateEditor(dsp.editor); }
    if (!isObject(actions) || !["play", "pause", "stop", "next", "previous", "seek", "queue_select", "queue_mode", "save_current"].every(key => typeof actions[key] === "boolean")) throw new Error("Invalid actions snapshot");
    if (PLAYLIST_ACTIONS.some(action => actions[action] !== undefined && typeof actions[action] !== "boolean")) throw new Error("Invalid playlist actions");
    return state;
  }
  function validateFavorites(payload, session) {
    if (!isObject(payload) || payload.version !== 1 || !string(payload.session_id) || !payload.session_id || (session !== undefined && payload.session_id !== session) || !Array.isArray(payload.favorites)) throw new Error("Invalid favorites snapshot");
    const ids = new Set();
    for (const favorite of payload.favorites) {
      if (!isObject(favorite) || !string(favorite.id) || !favorite.id || ids.has(favorite.id) || typeof favorite.editable !== "boolean" || ![favorite.name, favorite.title, favorite.artist, favorite.album, favorite.target_label].every(string) || !(favorite.artwork === null || string(favorite.artwork))) throw new Error("Invalid favorite");
      ids.add(favorite.id);
    }
    return payload.favorites;
  }
  function favoritesReadFailure(response) {
    const body = response && response.body;
    // This is a read-failure hint, never a canonical library snapshot. Accept
    // only the fixed Web envelope and retain neither server text nor records.
    return response?.status === 503 && isObject(body)
      && Object.keys(body).sort().join(",") === "error,message,outcome"
      && body.error === "favorites_unavailable" && body.outcome === "ERROR"
      && body.message === "Managed favorites could not be loaded. Stop the renderer service, repair or restore its saved favorites library, then restart it."
      ? "favorites_unavailable" : null;
  }
  function safeAudioURL(value) {
    // Validate protocol and credentials without changing the submitted audio URL.
    if (!string(value) || !/^https?:\/\//i.test(value) || /[\s\\<>\x00-\x1f\x7f]/.test(value)) return null;
    try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && url.hostname ? url.href : null; } catch (_) { return null; }
  }
  function safeArtwork(value) {
    // Mirror the server-approved artwork contract. Images never carry query or
    // fragment data, even if a malformed state response slips through.
    return string(value) && !/[?#]/.test(value) ? safeAudioURL(value) : null;
  }
  function formatTime(seconds) {
    if (!optionalNumber(seconds) || seconds === null) return "—";
    const value = Math.floor(seconds), hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60), remainder = String(value % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${remainder}` : `${minutes}:${remainder}`;
  }
  function guardedAction(state, action, values = {}) {
    if (!state) throw new Error("Read renderer state first");
    const body = {action, ...values};
    if (["play", "pause", "next", "previous", "seek", "queue_select", "queue_mode", "favorite_current", ...PLAYLIST_ACTIONS].includes(action)) body.expected_generation = state.generation;
    if (["queue_select", "queue_mode"].includes(action)) body.queue_name = state.queue.name;
    if (action.startsWith("dsp_")) {
      if (state.dsp.status !== "CONFIRMED" || !state.dsp.editor) throw new Error("Read confirmed sound state first");
      if (DSP_LIBRARY_ACTIONS.includes(action) && state.dsp.editor.library_unavailable === true) throw new Error(DSP_LIBRARY_UNAVAILABLE_MESSAGE);
      body.expected_dsp = state.dsp.editor.revision;
    }
    return body;
  }
  function commandError(response) {
    const body = response && response.body;
    if (isObject(body) && string(body.message) && ["REJECTED", "UNKNOWN", "ERROR"].includes(body.outcome)) {
      // Only explicit pre-application rejection means it did not take effect.
      const kind = body.outcome === "REJECTED" ? "error" : "unknown";
      return {kind, text: body.message, committed: body.committed === true, code: string(body.error) ? body.error : null};
    }
    return {kind: "unknown", text: "The request outcome could not be confirmed. Check the current state before trying again.", committed: false};
  }
  class Controller {
    constructor(request, onChange = () => {}) {
      this.request = request;
      this.onChange = onChange;
      this.state = null;
      this.playlistIdentity = null; this.playlistEditable = false;
      this.favorites = [];
      this.fresh = false;
      this.favoritesFresh = false;
      this.favoritesReadFailure = null;
      this.lastReadAt = null;
      this.notice = {kind: "pending", text: "Connecting to your renderer…"};
      this.active = new Map();
      this.unknownOutcomes = [];
      this.operation = 0;
      this.epoch = 0;
      this.readPromise = null;
      this.readAgain = false;
    }
    changed() { this.onChange(this); }
    get busy() { return this.active.size > 0; }
    get stopBusy() { return Array.from(this.active.values()).some(item => item.action === "stop"); }
    canMutate(action) { return action === "stop" ? Boolean(this.state && !this.stopBusy) : Boolean(this.state && this.fresh && !this.busy && (action !== "device_rename" || /^[0-9a-f]{64}$/.test(this.state.device.revision || "")) && (!action.startsWith("favorite_") || this.favoritesFresh) && (!action.startsWith("dsp_") || (this.state.dsp.status === "CONFIRMED" && this.state.dsp.editor && (!DSP_LIBRARY_ACTIONS.includes(action) || this.state.dsp.editor.library_unavailable !== true))) && (!action.startsWith("playlist_") || playlistAvailable(this.state, action) && (action === "playlist_replace" || this.playlistEditable))); }
    refresh() {
      this.readAgain = true;
      if (!this.readPromise) {
        this.readPromise = this.drainReads().finally(() => { this.readPromise = null; });
      }
      return this.readPromise;
    }
    async drainReads() {
      do {
        this.readAgain = false;
        const epoch = this.epoch;
        const [stateResult, favoriteResult] = await Promise.allSettled([
          this.request("/orr/v1/web/state", {method: "GET"}),
          this.request("/orr/v1/web/favorites", {method: "GET"})
        ]);
        if (epoch !== this.epoch) continue; // A command superseded this read.
        let acceptedState = false;
        try {
          if (stateResult.status !== "fulfilled" || stateResult.value.status !== 200) throw new Error();
          const state = validateState(stateResult.value.body);
          if (this.state && this.state.session_id === state.session_id && state.revision < this.state.revision) throw new Error();
          const playlistIdentity = JSON.stringify([state.session_id, state.generation]);
          if (playlistIdentity !== this.playlistIdentity) this.playlistEditable = state.queue.playlist_edit_reason === "supported";
          else if (state.queue.playlist_edit_reason !== "supported") this.playlistEditable = false;
          this.playlistIdentity = playlistIdentity;
          this.state = state;
          this.fresh = true;
          this.lastReadAt = Date.now();
          acceptedState = true;
          if (!this.operation && !this.busy) this.notice = {kind: "confirmed", text: "Connected. Controls show the renderer’s reported state."};
        } catch (_) {
          this.fresh = false;
          if (!this.busy && !this.operation) this.notice = {kind: "unknown", text: "Cannot read the renderer. Last received values may be stale. Refresh to reconnect."};
        }
        this.favoritesReadFailure = null;
        try {
          if (!acceptedState || favoriteResult.status !== "fulfilled" || favoriteResult.value.status !== 200) throw new Error();
          this.favorites = validateFavorites(favoriteResult.value.body, this.state.session_id);
          this.favoritesFresh = true;
        } catch (_) {
          this.favoritesFresh = false;
          if (acceptedState && favoriteResult.status === "fulfilled") this.favoritesReadFailure = favoritesReadFailure(favoriteResult.value);
        }
        this.changed();
      } while (this.readAgain);
      return this.fresh;
    }
    async mutate(body, label) {
      if (!isObject(body) || !string(body.action) || !this.canMutate(body.action)) return {kind: "blocked", text: "Read current state and wait for the pending request before trying again."};
      if (requestBytes(body) > MAX_REQUEST_BYTES) return {kind: "blocked", text: "The complete request exceeds the 32,768-byte limit."};
      const id = ++this.operation, session = this.state.session_id;
      this.epoch += 1;
      this.fresh = false;
      this.favoritesReadFailure = null;
      if (body.action.startsWith("favorite_")) this.favoritesFresh = false;
      this.active.set(id, {action: body.action, label});
      this.notice = {kind: "pending", text: `${label}: waiting for the renderer.`, id};
      this.changed();
      let result;
      try {
        const response = await this.request("/orr/v1/web/command", {method: "POST", headers: {"X-ORR-CSRF": this.state.csrf_token}, body});
        if (response.status === 200 && isObject(response.body) && response.body.request_acknowledged === true) result = {kind: "acknowledged", text: `${label}: request acknowledged. Reading the current state…`, id, favoriteId: response.body.favorite_id || null, deviceRevision: response.body.device_revision};
        else result = {...commandError(response), id};
      } catch (_) {
        result = {kind: "unknown", text: `${label}: outcome unknown because the response was lost. The command may have taken effect. No retry was sent.`, id};
      }
      if (body.action.startsWith("playlist_") && result.kind !== "acknowledged") {
        // Never echo a server error that could contain private source text.
        result.text = result.kind === "error" ? "Playlist request rejected. Review the source, current queue and stopped state before starting again." : "Playlist request outcome unknown. It may have taken effect. Check the current queue before starting again. No retry was sent.";
      }
      if (body.action === "favorite_target") {
        // The redacted library cannot verify the exact URL, even with a fresh
        // matching hostname/name/ID. Never echo potentially private errors.
        result.text = result.kind === "acknowledged"
          ? "Target-change request acknowledged. The exact saved URL is not verified by this redacted view."
          : result.kind === "error"
            ? "Target-change request rejected. Review the replacement URL and metadata choice before starting again."
            : "Target-change outcome unknown. The change may have taken effect. No retry was sent. The exact saved URL cannot be verified from this redacted view.";
      }
      if (body.action === "device_rename") {
        // Names can legally contain private URL text. Never echo server errors
        // or user input, and never let matching readback erase a failed step.
        result.text = result.kind === "acknowledged"
          ? "Room rename acknowledged. Reading the current name…"
          : result.kind === "error"
            ? "Room rename rejected. Use 1–128 characters without control characters. Reopen Rename room and review the current name before trying again."
            : result.committed
              ? "The room name was applied, but persistence or discovery is unconfirmed. A matching name cannot confirm either. No retry was sent."
              : "Room rename outcome unknown. The name may have changed; persistence or discovery is unconfirmed. No retry was sent.";
      }
      this.active.delete(id);
      this.fresh = false;
      if (result.kind === "unknown") this.unknownOutcomes = this.unknownOutcomes.concat({id, label: body.action === "device_rename" ? "Rename room (persistence/discovery unconfirmed)" : label, committed: result.committed === true}).slice(-20);
      if (id === this.operation) this.notice = {...result, kind: result.kind === "acknowledged" ? "pending" : result.kind};
      this.changed();
      await this.refresh();
      if (body.action === "device_rename" && result.kind === "acknowledged") {
        result.deviceVerified = Boolean(this.fresh && this.state.session_id === session
          && string(result.deviceRevision) && /^[0-9a-f]{64}$/.test(result.deviceRevision)
          && this.state.device.revision === result.deviceRevision);
        result.text = result.deviceVerified
          ? "Room name saved; the current name is verified."
          : "Room rename acknowledged, but the current name has not been verified. Close this dialog and refresh before another request.";
      }
      if (body.action === "favorite_target" && result.kind === "acknowledged") {
        if (!this.favoritesFresh) result.text += " The favorites list could not be refreshed.";
        if (!this.fresh) result.text += " Current renderer state is unavailable. Refresh before another command.";
      }
      if (id === this.operation && result.kind === "acknowledged") {
        // An ACK is not a playback observation. State badges remain canonical.
        if (body.action === "device_rename") {
          this.notice = {...result, kind: result.deviceVerified ? "confirmed" : "acknowledged"};
        } else if (body.action === "favorite_target") {
          this.notice = {...result};
        } else {
          this.notice = this.fresh
            ? {kind: "confirmed", text: `${label}: request acknowledged; current renderer state received.`, id}
            : {kind: "unknown", text: `${label}: request acknowledged, but current state is unavailable. Refresh before another command.`, id};
        }
      }
      this.changed();
      return result;
    }
  }
  return {Controller, EXPORT_FILENAME, exportIdentity, exportObject, validateExportSummary, validateExportDocument, exportFailure, readExportResponse, PlaylistDraft, PLAYLIST_ACTIONS, MAX_REQUEST_BYTES, requestBytes, playlistAvailable, pastedURLs, playlistPreview, DspDraft, DSP_MODES, DSP_FIELDS, DSP_KEYS, DSP_LIBRARY_UNAVAILABLE_MESSAGE, MODES, validateEditor, validateState, validateFavorites, safeAudioURL, safeArtwork, formatTime, guardedAction, commandError};
});
