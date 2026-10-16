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
  const DSP_LIBRARY_ACTIONS = Object.freeze(["dsp_preset_save", "dsp_preset_rename", "dsp_preset_delete", "dsp_preset_import"]);
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
  async function readExportResponse(response, signal, expectedSize, filename = EXPORT_FILENAME) {
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
      if (response.status === 200 && expectedSize !== undefined && (size !== expectedSize || response.headers.get("Content-Disposition") !== `attachment; filename="${filename}"`)) throw new Error("Invalid export attachment");
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
  const CURVE_EXPORT_FILENAME = "open-room-renderer-eq-curve.json";
  const CURVE_TRANSFER_CHANGED = "Sound settings, saved presets or renderer availability changed. Close this dialog and review current state before starting again.";
  function curveImportBody(revision, source) {
    return {action: "dsp_preset_import", expected_dsp: revision, text: source, private_import_confirmed: true};
  }
  function validateCurveExportSummary(value, revision, preset) {
    if (!exactFields(value, ["version", "dsp_revision", "preset_id", "revision", "size_bytes", "filename", "scope", "shared", "band_count"]) || value.version !== 1 || value.dsp_revision !== revision || value.preset_id !== preset.id || !opaqueID(value.revision) || !integer(value.size_bytes) || value.size_bytes < 1 || value.size_bytes > MAX_REQUEST_BYTES || value.filename !== CURVE_EXPORT_FILENAME || value.scope !== "current_plugin" || value.shared !== preset.shared || value.band_count !== 40) throw new Error("Invalid saved curve summary");
    return value;
  }
  function validateCurveDocument(bytes) {
    if (!(bytes instanceof Uint8Array) || !bytes.length || bytes.length > MAX_REQUEST_BYTES) throw new Error("Invalid saved curve file");
    const source = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes);
    const value = JSON.parse(source);
    // JSON.parse validates syntax; these per-object key sets also reject
    // duplicate keys, including escaped spellings, before trusting a preview.
    // Bound nesting independently of JSON.parse and never project private text.
    const tokens = source.match(/"(?:\\.|[^"\\])*"|[{}\[\],:]|[^ \t\r\n{}\[\],:]+/g), stack = [];
    tokens.forEach((token, index) => {
      if (token === "{") stack.push(new Set());
      else if (token === "[") stack.push(null);
      else if (token === "}" || token === "]") stack.pop();
      else if (tokens[index + 1] === ":") {
        const key = JSON.parse(token), keys = stack[stack.length - 1];
        if (!keys || keys.has(key)) throw new Error("Invalid saved curve file");
        keys.add(key);
        if (stack.length === 1 && key === "version" && tokens[index + 2] !== "1") throw new Error("Invalid saved curve version");
      }
      if (stack.length > 5) throw new Error("Invalid saved curve nesting");
    });
    if (!exactFields(value, ["version", "presets"]) || value.version !== 1 || !isObject(value.presets) || Object.keys(value.presets).length !== 1) throw new Error("Invalid saved curve file");
    const name = Object.keys(value.presets)[0], plugins = value.presets[name];
    const validText = (item, limit) => string(item) && Boolean(item.trim()) && !/[\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]/u.test(item) && new TextEncoder().encode(item).length <= limit;
    if (!validText(name, 256) || !isObject(plugins) || Object.keys(plugins).length !== 1) throw new Error("Invalid saved curve file");
    const plugin = Object.keys(plugins)[0], bands = plugins[plugin], keys = new Set();
    if (!validText(plugin, 8192) || !Array.isArray(bands) || bands.length !== 40) throw new Error("Invalid saved curve file");
    bands.forEach(band => {
      if (!exactFields(band, ["param_name", "value"]) || !DSP_KEYS.includes(band.param_name) || keys.has(band.param_name) || !validBandValue(band.param_name, band.value)) throw new Error("Invalid saved curve file");
      keys.add(band.param_name);
    });
    // The renderer alone authorizes the literal name and exact current plugin.
    // Preserve original source bytes; do not rename, remap, normalize or apply.
    return source;
  }
  function validateCurveExportDocument(bytes, summary) {
    const source = validateCurveDocument(bytes);
    if (bytes.length !== summary.size_bytes || requestBytes(curveImportBody(summary.dsp_revision, source)) > MAX_REQUEST_BYTES) throw new Error("Invalid saved curve attachment");
    return bytes;
  }
  function curveTransferFailure(code) {
    const messages = {
      curve_export_unavailable: "This saved curve cannot be exported here. An unsupported or private legacy name may need renaming first. Review the saved preset before starting again.",
      curve_context_mismatch: "The file does not match the current sound context. No plugin remapping is performed.",
      preset_conflict: "That preset name is already in use. Import cannot overwrite or rename an existing preset.",
      preset_library_unavailable: DSP_LIBRARY_UNAVAILABLE_MESSAGE,
      curve_library_full: "The saved preset library cannot fit this curve. Review the library before another import.",
      stale_dsp: CURVE_TRANSFER_CHANGED
    };
    return Object.hasOwn(messages, code) ? messages[code] : "The saved curve request was rejected. Review the file and current saved presets before starting again.";
  }
  class CurveImportDraft {
    constructor(state) {
      this.session = state.session_id; this.revision = state.dsp.editor.revision;
      this.serial = 0; this.text = ""; this.size = 0; this.reading = false; this.error = "";
      this.sent = false; this.stale = false; this.cancelled = false; this.status = "";
    }
    clearSource() { this.serial += 1; this.text = ""; this.size = 0; this.reading = false; this.error = ""; }
    cancel() { this.clearSource(); this.cancelled = true; }
    async readFile(file) {
      if (this.sent || this.stale || this.cancelled) return;
      this.clearSource(); const serial = this.serial; this.reading = true;
      let bytes;
      try {
        if (!file || !integer(file.size) || !file.size || file.size > MAX_REQUEST_BYTES) throw new Error();
        const size = file.size, reading = file.arrayBuffer(); file = null;
        const buffer = await reading;
        bytes = new Uint8Array(buffer);
        if (this.serial !== serial || this.cancelled) return;
        if (bytes.length !== size) throw new Error();
        const source = validateCurveDocument(bytes);
        if (requestBytes(curveImportBody(this.revision, source)) > MAX_REQUEST_BYTES) throw new Error();
        this.text = source; this.size = size;
      } catch (_) {
        if (this.serial === serial && !this.cancelled) this.error = "Choose a UTF-8 v1 JSON file containing one named curve, one plugin and all 40 band parameters. The complete escaped request must fit 32,768 bytes.";
      } finally {
        if (bytes) bytes.fill(0);
        if (this.serial === serial && !this.cancelled) this.reading = false;
      }
    }
  }
  const FAVORITES_EXPORT_FILENAME = "open-room-renderer-favorites.json";
  const FAVORITES_TRANSFER_CHANGED = "Favorites or renderer availability changed. Close this dialog and review current state before starting again.";
  function favoritesRestoreBody(session, source) {
    return {action: "favorite_restore", expected_session: session, text: source, private_import_confirmed: true};
  }
  function validateFavoritesExportSummary(value, session, count) {
    if (!exactFields(value, ["version", "session_id", "revision", "count", "size_bytes", "filename", "scope"]) || value.version !== 1 || value.session_id !== session || !opaqueID(value.revision) || !integer(value.count) || value.count > 100 || value.count !== count || !integer(value.size_bytes) || value.size_bytes < 1 || value.size_bytes > MAX_REQUEST_BYTES || value.filename !== FAVORITES_EXPORT_FILENAME || value.scope !== "managed_only") throw new Error("Invalid favorites backup summary");
    return value;
  }
  function validateFavoritesDocument(bytes, complete = false) {
    if (!(bytes instanceof Uint8Array) || !bytes.length || bytes.length > MAX_REQUEST_BYTES) throw new Error("Invalid favorites file");
    const source = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes), value = JSON.parse(source);
    // JSON.parse owns syntax validation. A bounded token walk checks each
    // object's decoded keys independently, including escaped duplicates. The
    // flat exportObject scanner cannot accept repeated fields across records.
    const tokens = source.match(/"(?:\\.|[^"\\])*"|[{}\[\],:]|[^ \t\r\n{}\[\],:]+/g), stack = [];
    tokens.forEach((token, index) => {
      if (token === "{") stack.push(new Set());
      else if (token === "[") stack.push(null);
      else if (token === "}" || token === "]") stack.pop();
      else if (tokens[index + 1] === ":") {
        const key = JSON.parse(token), keys = stack[stack.length - 1];
        if (!keys || keys.has(key)) throw new Error("Invalid favorites file");
        keys.add(key);
        if (stack.length === 1 && key === "version" && tokens[index + 2] !== "1") throw new Error("Invalid favorites version");
      }
      if (stack.length > 3) throw new Error("Invalid favorites nesting");
    });
    if (!exactFields(value, ["version", "favorites"]) || value.version !== 1 || !Array.isArray(value.favorites) || value.favorites.length > 100) throw new Error("Invalid favorites file");
    const fields = ["id", "name", "url", "title", "artist", "album", "artwork"], ids = new Set();
    // Python's persisted-text whitespace set differs from String.trim():
    // U+FEFF is valid nonblank content, while U+0085 is whitespace there.
    const blank = /^[\x09-\x0d\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;
    const validText = (item, limit, required = false) => string(item) && (!required || !blank.test(item)) && !/[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]/u.test(item) && new TextEncoder().encode(item).length <= limit;
    value.favorites.forEach(record => {
      if (!isObject(record) || !["id", "name", "url"].every(key => Object.hasOwn(record, key)) || Object.keys(record).some(key => !fields.includes(key)) || complete && !exactFields(record, fields) || !string(record.id) || !/^[0-9a-f]{32}$/.test(record.id) || ids.has(record.id) || !validText(record.name, 256, true) || !validText(record.url, 8192, true) || ["title", "artist", "album", "artwork"].some(key => Object.hasOwn(record, key) && !validText(record[key], key === "artwork" ? 8192 : 1024))) throw new Error("Invalid favorites record");
      ids.add(record.id);
    });
    // Preserve the exact source, Unicode and signed URL bytes. URL eligibility
    // is authoritative on the renderer; never normalize with browser URL APIs.
    return {text: source, count: value.favorites.length};
  }
  function validateFavoritesExportDocument(bytes, summary) {
    const document = validateFavoritesDocument(bytes, true);
    if (bytes.length !== summary.size_bytes || document.count !== summary.count || requestBytes(favoritesRestoreBody(summary.session_id, document.text)) > MAX_REQUEST_BYTES) throw new Error("Invalid favorites backup attachment");
    return bytes;
  }
  function favoritesTransferFailure(code) {
    const messages = {
      favorites_export_unavailable: "This managed favorites library cannot be backed up here. The complete escaped restore request must fit 32,768 bytes. Review the library before starting again.",
      stale_favorites_transfer: FAVORITES_TRANSFER_CHANGED,
      favorites_not_empty: "Restore requires an empty managed favorites library. Existing managed entries cannot be merged or overwritten.",
      favorites_unavailable: "Managed favorites could not be loaded. Stop the renderer service, repair or restore its saved favorites library, then restart it."
    };
    return Object.hasOwn(messages, code) ? messages[code] : "The favorites transfer request was rejected. Review the file and current managed favorites before starting again.";
  }
  class FavoritesRestoreDraft {
    constructor(state) {
      this.session = state.session_id;
      this.serial = 0; this.text = ""; this.size = 0; this.count = 0; this.reading = false; this.error = "";
      this.sent = false; this.stale = false; this.cancelled = false; this.status = "";
    }
    clearSource() { this.serial += 1; this.text = ""; this.size = 0; this.count = 0; this.reading = false; this.error = ""; }
    cancel() { this.clearSource(); this.cancelled = true; }
    async readFile(file) {
      if (this.sent || this.stale || this.cancelled) return;
      this.clearSource(); const serial = this.serial; this.reading = true;
      let bytes;
      try {
        if (!file || !integer(file.size) || !file.size || file.size > MAX_REQUEST_BYTES) throw new Error();
        const size = file.size, reading = file.arrayBuffer(); file = null;
        const buffer = await reading;
        bytes = new Uint8Array(buffer);
        if (this.serial !== serial || this.cancelled) return;
        if (bytes.length !== size) throw new Error();
        const document = validateFavoritesDocument(bytes);
        if (requestBytes(favoritesRestoreBody(this.session, document.text)) > MAX_REQUEST_BYTES) throw new Error();
        this.text = document.text; this.size = size; this.count = document.count;
      } catch (_) {
        if (this.serial === serial && !this.cancelled) this.error = "Choose a UTF-8 v1 favorites JSON file containing up to 100 managed entries with unique IDs. The complete escaped request must fit 32,768 bytes.";
      } finally {
        if (bytes) bytes.fill(0);
        if (this.serial === serial && !this.cancelled) this.reading = false;
      }
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
    if (media.stream_title !== undefined && media.stream_title !== null
        && (!string(media.stream_title) || new TextEncoder().encode(media.stream_title).length > 1024)) throw new Error("Invalid stream title");
    if (media.audio !== undefined) {
      const audio = media.audio;
      if (!isObject(audio) || !(audio.codec === null || (string(audio.codec) && /^[a-zA-Z0-9_+-]{1,40}$/.test(audio.codec)))
          || !(audio.bitrate_bps === null || (integer(audio.bitrate_bps) && audio.bitrate_bps >= 1 && audio.bitrate_bps <= 1000000000))
          || audio.bitrate_kind !== (audio.bitrate_bps === null ? "unavailable" : "estimate")) throw new Error("Invalid audio format");
    }
    if (!isObject(gain) || !["CONFIRMED", "PENDING", "UNKNOWN"].includes(gain.status) || !(gain.volume === null || (integer(gain.volume) && gain.volume <= 100)) || !optionalBoolean(gain.mute)) throw new Error("Invalid gain snapshot");
    if (!isObject(queue) || !string(queue.name) || !integer(queue.count) || queue.count > 1000 || !integer(queue.index) || !Object.hasOwn(MODES, queue.mode) || !Array.isArray(queue.items) || queue.items.length !== queue.count || (queue.count === 0 ? queue.index !== 0 : queue.index < 1 || queue.index > queue.count)) throw new Error("Invalid queue snapshot");
    if (queue.playlist_edit_reason !== undefined && !["supported", "empty_queue", "unsupported_queue"].includes(queue.playlist_edit_reason)) throw new Error("Invalid queue edit capability");
    queue.items.forEach((item, offset) => { if (!isObject(item) || item.index !== offset + 1 || ![item.title, item.artist, item.album].every(string) || !optionalNumber(item.duration_seconds)) throw new Error("Invalid queue item"); });
    if (!isObject(dsp) || !["CONFIRMED", "PENDING", "UNKNOWN"].includes(dsp.status) || !(dsp.balance === null || (typeof dsp.balance === "number" && Number.isFinite(dsp.balance) && Math.abs(dsp.balance) <= 1)) || !optionalBoolean(dsp.eq_enabled) || !string(dsp.name)) throw new Error("Invalid sound snapshot");
    if (dsp.editor !== null) { if (dsp.status !== "CONFIRMED") throw new Error("Unconfirmed sound editor"); validateEditor(dsp.editor); }
    if (!isObject(actions) || !["play", "pause", "stop", "next", "previous", "seek", "queue_select", "queue_mode", "save_current"].every(key => typeof actions[key] === "boolean")) throw new Error("Invalid actions snapshot");
    if (PLAYLIST_ACTIONS.some(action => actions[action] !== undefined && typeof actions[action] !== "boolean")) throw new Error("Invalid playlist actions");
    if (state.spotify !== undefined && (!exactFields(state.spotify, ["configured", "active", "waiting_for_app"]) || Object.values(state.spotify).some(value => typeof value !== "boolean"))) throw new Error("Invalid Spotify availability");
    if (actions.spotify_start !== undefined && typeof actions.spotify_start !== "boolean") throw new Error("Invalid Spotify action");
    return state;
  }
  function streamTitleText(state, fresh) {
    const {media, transport} = state, title = media.stream_title;
    return fresh && media.is_live && transport.status === "CONFIRMED"
      && ["PLAYING", "PAUSED"].includes(transport.phase) && title && title !== media.title
      ? `On air: ${title}` : "";
  }
  function audioFormatText(state, fresh) {
    const {media, transport} = state;
    const active = fresh && transport.status === "CONFIRMED" && ["PLAYING", "PAUSED"].includes(transport.phase);
    const audio = active ? media.audio : null;
    const raw = audio?.codec;
    const codec = raw ? (/^pcm_/i.test(raw) ? "PCM" : raw.toUpperCase()) : "unknown";
    const bitrate = audio?.bitrate_kind === "estimate" && audio.bitrate_bps > 0
      ? `≈${Math.max(1, Math.round(audio.bitrate_bps / 1000))} kb/s (estimate)` : "unknown";
    return `Codec: ${codec} · Bitrate: ${bitrate}`;
  }
  function validateFavorites(payload, session) {
    if (!isObject(payload) || payload.version !== 1 || !string(payload.session_id) || !payload.session_id || (session !== undefined && payload.session_id !== session) || !Array.isArray(payload.favorites)) throw new Error("Invalid favorites snapshot");
    const ids = new Set();
    for (const favorite of payload.favorites) {
      if (!isObject(favorite) || !string(favorite.id) || !favorite.id || ids.has(favorite.id) || typeof favorite.editable !== "boolean" || ![favorite.name, favorite.title, favorite.artist, favorite.album, favorite.target_label].every(string) || !(favorite.artwork === null || string(favorite.artwork))) throw new Error("Invalid favorite");
      if (favorite.revision !== undefined && (!string(favorite.revision) || (favorite.editable ? !opaqueID(favorite.revision) : favorite.revision !== ""))) throw new Error("Invalid favorite revision");
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
    if (["play", "pause", "next", "previous", "seek", "queue_select", "queue_mode", "favorite_current", "spotify_start", ...PLAYLIST_ACTIONS].includes(action)) body.expected_generation = state.generation;
    if (["queue_select", "queue_mode"].includes(action)) body.queue_name = state.queue.name;
    if (action === "favorite_restore") body.expected_session = state.session_id;
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
  function queueEditRejected(response) {
    if (response.status !== 409 || !exactFields(response.body, ["error", "message", "outcome"])
        || response.body.error !== "unavailable" || response.body.outcome !== "REJECTED"
        || response.body.message !== "Requested operation is currently unavailable." || !string(response.rawBody)) return false;
    // The parsed schema has exactly three fixed string values. Count members
    // in the original valid JSON with strings intact: any duplicate (including
    // escaped keys) or overwritten nested value adds a fourth separator.
    return response.rawBody.match(/"(?:\\.|[^"\\])*"|:/g)?.filter(token => token === ":").length === 3;
  }
  const OUTPUT_FAMILIES = Object.freeze(["alsa", "pulse", "pipewire", "jack", "coreaudio", "wasapi", "sdl", "sndio", "oss", "openal", "null", "pcm", "not_observed", "unrecognized"]);
  const OUTPUT_PROPERTY_STATUSES = Object.freeze(["value", "unavailable", "unsupported", "not_read", "timed_out", "transport_error", "malformed_response", "request_failed", "response_limit"]);
  function validateOutputInspection(value, session) {
    const oneOf = (item, values) => values.includes(item);
    if (!exactFields(value, ["version", "session_id", "status", "configuration", "connection", "socket_path_check", "live_intent", "driver", "comparison", "null_output", "observations", "renderer_ownership", "physical_device", "audible_output"]) || value.version !== 1 || value.session_id !== session || !oneOf(value.status, ["complete", "incomplete"]) ||
        !exactFields(value.configuration, ["source", "selection", "driver_family", "spawn_policy", "identifier"]) || value.configuration.source !== "running_renderer" || !oneOf(value.configuration.selection, ["mpv_default", "explicit_output"]) || !oneOf(value.configuration.driver_family, OUTPUT_FAMILIES) || !oneOf(value.configuration.spawn_policy, ["auto_spawn_enabled", "external_only"]) || value.configuration.identifier !== "hidden" ||
        !oneOf(value.connection, ["not_attempted", "connected", "socket_missing", "socket_invalid", "timed_out", "transport_error"]) || !oneOf(value.socket_path_check, ["not_checked", "unchanged_at_checks", "changed"]) ||
        !exactFields(value.live_intent, ["status", "selection", "driver_family", "identifier"]) || !oneOf(value.live_intent.status, OUTPUT_PROPERTY_STATUSES) || !oneOf(value.live_intent.selection, ["not_observed", "mpv_default", "explicit_output"]) || !oneOf(value.live_intent.driver_family, OUTPUT_FAMILIES) || value.live_intent.identifier !== "hidden" ||
        !exactFields(value.driver, ["status", "family"]) || !oneOf(value.driver.status, OUTPUT_PROPERTY_STATUSES) || !oneOf(value.driver.family, OUTPUT_FAMILIES) || !oneOf(value.comparison, ["matches", "differs", "not_compared"]) || !oneOf(value.null_output, ["deliberately_silent", "not_observed"]) ||
        value.observations !== "sequential_not_atomic" || value.renderer_ownership !== "unverified" || value.physical_device !== "not_verified" || value.audible_output !== "not_verified") throw new Error("Invalid output inspection");
    return value;
  }
  function outputInspectionLines(value) {
    const selection = {mpv_default: "mpv default", explicit_output: "explicit output", not_observed: "not observed"};
    const family = name => ({not_observed: "driver family not observed", unrecognized: "driver family unrecognized"}[name] || `${name} driver family`);
    const property = {value: "observed", unavailable: "unavailable (can be idle)", unsupported: "not supported by this mpv", not_read: "not read", timed_out: "timed out", transport_error: "connection failed", malformed_response: "invalid response", request_failed: "read failed", response_limit: "response limit reached"};
    const connection = {not_attempted: "not attempted", connected: "connected", socket_missing: "socket missing; the player may not be running", socket_invalid: "socket path is invalid", timed_out: "two-second inspection budget expired", transport_error: "socket connection failed"};
    const path = {not_checked: "not checked", unchanged_at_checks: "unchanged at the check instants; ownership remains unverified", changed: "changed during inspection; results are incomplete and were not compared"};
    return [
      `Running configuration: ${selection[value.configuration.selection]}; ${family(value.configuration.driver_family)}; identifier hidden.`,
      `Startup policy: ${value.configuration.spawn_policy === "auto_spawn_enabled" ? "renderer may start its configured player" : "external player only"}. This inspection never starts a player.`,
      `Local mpv connection: ${connection[value.connection]}. Socket path: ${path[value.socket_path_check]}.`,
      `Live output intent: ${property[value.live_intent.status]}; ${selection[value.live_intent.selection]}; ${family(value.live_intent.driver_family)}; identifier hidden.`,
      `Live output driver: ${property[value.driver.status]}; ${family(value.driver.family)}.`,
      {matches: "Configured output matches the observed live intent. This does not prove the physical output.", differs: "Configured output differs from the observed live intent. Review the running configuration and player before making changes.", not_compared: "Configured and live output intent were not compared. A default selection or incomplete observation cannot establish a match."}[value.comparison],
      value.null_output === "deliberately_silent" ? "The null output is deliberately silent. It does not send sound to a physical device." : "A deliberately silent null output was not established by this inspection."
    ];
  }
  class OutputInspection {
    constructor(request, onChange = () => {}) { this.request = request; this.onChange = onChange; this.session = null; this.dismiss(); }
    dismiss() { this.context = null; this.open = false; this.pending = false; this.result = null; this.error = ""; }
    observe(state) {
      if (this.session !== state?.session_id) { this.dismiss(); this.session = state?.session_id || null; }
    }
    canInspect(state) { return Boolean(state?.session_id && state?.csrf_token && !this.pending); }
    async inspect(state) {
      this.observe(state);
      if (!this.canInspect(state)) return;
      const context = {session: state.session_id}; this.context = context;
      this.open = true; this.pending = true; this.result = null; this.error = ""; this.onChange();
      try {
        const response = await this.request("/orr/v1/web/output/inspect", {method: "POST", timeoutMs: 4000,
          headers: {"X-ORR-CSRF": state.csrf_token}, body: {action: "inspect", expected_session: context.session}});
        if (this.context !== context) return;
        if (response.status !== 200) {
          const code = response.body?.error;
          this.error = code === "output_inspection_busy" ? "Another output inspection is still finishing. No new inspection was started. Try again explicitly when ready." : code === "stale_output_inspection" || code === "forbidden" ? "The renderer session or browser admission changed. Refresh before inspecting again." : "Output inspection could not be completed. No automatic retry was sent.";
        } else this.result = validateOutputInspection(response.body, context.session);
      } catch (_) {
        if (this.context === context) this.error = "Output inspection was not received or could not be read. No automatic retry was sent.";
      } finally {
        if (this.context === context) { this.pending = false; this.onChange(); }
      }
    }
  }
  class RadioSearch {
    constructor(request, onChange = () => {}) { this.request = request; this.onChange = onChange; this.serial = 0; this.clear(); }
    clear() { this.serial += 1; this.pending = false; this.session = null; this.stations = []; this.message = "Search by station name, country, or both. Your search is sent to Radio-Browser. No account is needed."; }
    observe(state) {
      if (this.session && state && this.session !== state.session_id) {
        this.clear(); this.message = "The renderer restarted. Search again to choose a station.";
      }
    }
    async search(state, name, country) {
      if (this.pending || !state) return;
      name = name.trim(); country = country.trim().toUpperCase();
      if ((name && (name.length < 2 || name.length > 80)) || (country && !/^[A-Z]{2}$/.test(country)) || (!name && !country)) {
        this.message = "Enter a station name (2–80 characters), a two-letter country code, or both."; this.onChange(); return;
      }
      const serial = ++this.serial;
      this.session = state.session_id; this.pending = true; this.stations = []; this.message = "Searching Radio-Browser…"; this.onChange();
      try {
        const response = await this.request("/orr/v1/web/radio/search", {method: "POST", headers: {"X-ORR-CSRF": state.csrf_token}, body: {action: "search", name, country}, timeoutMs: 15000});
        if (serial !== this.serial) return;
        const body = response.body, ids = new Set();
        if (response.status === 503 && isObject(body) && body.error === "radio_unavailable" && body.outcome === "ERROR") {
          // Only this fixed server classification identifies a catalog failure.
          // Do not expose provider details or imply every favorite is independent
          // of the directory: saved catalog stations still need its resolver.
          this.message = "Radio-Browser search is unavailable. Try again later, or use Favorites → + Add with a direct audio URL from the station’s official website. Playback was not changed.";
          return;
        }
        if (response.status !== 200 || !isObject(body) || body.session_id !== this.session || !Array.isArray(body.stations) || body.stations.length > 20) throw new Error();
        for (const station of body.stations) {
          if (!isObject(station) || !/^[0-9a-f]{32}$/.test(station.id) || ids.has(station.id) || !string(station.name) || !station.name.trim() || station.name.length > 256 || !string(station.country) || !/^(?:[A-Z]{2})?$/.test(station.country) || !["MP3", "AAC", "AAC+", "OGG"].includes(station.codec) || !integer(station.bitrate) || station.bitrate > 10000) throw new Error();
          ids.add(station.id);
        }
        this.stations = body.stations;
        this.message = this.stations.length ? `${this.stations.length} stations. Choose Play, Save favorite, or Replace favorite. Results expire after 15 minutes.` : "No supported stations found. Try another name or country.";
      } catch (_) {
        if (serial !== this.serial) return;
        this.stations = []; this.message = "Search unavailable. Check the connection or try again later. Playback was not changed.";
      } finally {
        if (serial === this.serial) { this.pending = false; this.onChange(); }
      }
    }
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
    canMutate(action) { return action === "stop" ? Boolean(this.state && !this.stopBusy) : Boolean(this.state && this.fresh && !this.busy && (action !== "spotify_start" || this.state.spotify?.configured === true && this.state.actions.spotify_start === true) && (action !== "device_rename" || /^[0-9a-f]{64}$/.test(this.state.device.revision || "")) && (!action.startsWith("favorite_") || this.favoritesFresh && (action !== "favorite_restore" || !this.favorites.some(item => item.editable))) && (!action.startsWith("dsp_") || (this.state.dsp.status === "CONFIRMED" && this.state.dsp.editor && (!DSP_LIBRARY_ACTIONS.includes(action) || this.state.dsp.editor.library_unavailable !== true))) && (!action.startsWith("playlist_") || playlistAvailable(this.state, action) && (action === "playlist_replace" || this.playlistEditable))); }
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
        if (body.action === "favorite_radio_target" && result.kind === "error"
            && (response.status < 400 || response.status >= 500 || !exactFields(response.body, ["error", "message", "outcome"]))) result.kind = "unknown";
        if (body.action === "favorite_restore" && result.kind === "unknown" && response.status === 502 && response.body?.error === "command_failed" && response.body.outcome === "UNKNOWN" && response.body.committed === true) result.favoritesLibraryDurabilityUnknown = true;
        // Only this exact admission rejection proves a row edit had no effect.
        // Extra fields (including committed/effect), other errors and local
        // blocking must never grant the UI permission to re-arm an old edit.
        if (["playlist_remove", "playlist_move"].includes(body.action) && queueEditRejected(response)) result.queueEditRejected = true;
        if (body.action === "dsp_preset_import" && result.kind === "unknown" && response.status === 502 && response.body?.error === "command_failed" && response.body.outcome === "UNKNOWN" && response.body.effect === "LIBRARY_COMMITTED_DURABILITY_UNKNOWN") {
          result.curveLibraryDurabilityUnknown = true; result.committed = true;
        }
      } catch (_) {
        result = {kind: "unknown", text: `${label}: outcome unknown because the response was lost. The command may have taken effect. No retry was sent.`, id};
      }
      if (body.action.startsWith("playlist_") && result.kind !== "acknowledged") {
        // Never echo a server error that could contain private source text.
        result.text = result.queueEditRejected ? "Queue edit rejected; nothing was changed. Review the queue, then choose Refresh to enable a new attempt. Refresh does not send an edit." : result.kind === "error" ? "Playlist request rejected. Review the source, current queue and stopped state before starting again." : "Playlist request outcome unknown. It may have taken effect. Check the current queue before starting again. No retry was sent.";
      }
      if (body.action === "dsp_preset_import") {
        // Import errors must never disclose a local filename, name, plugin or
        // file content. An ACK is distinct from durable saved-library readback.
        result.text = result.kind === "acknowledged"
          ? "Saved curve import acknowledged. Review the saved presets; loading is a separate action."
          : result.kind === "error"
            ? curveTransferFailure(result.code)
            : result.curveLibraryDurabilityUnknown
              ? "The saved preset library was replaced, but disk durability is unconfirmed. Matching readback cannot confirm durability. No retry was sent."
              : "Saved curve import outcome unknown. The library may have changed. Review saved presets before another import. No retry was sent.";
      }
      if (body.action === "favorite_restore") {
        // Private file contents and server error details never reach the UI.
        // A redacted matching readback cannot verify exact restored targets or
        // establish crash durability after a failed persistence step.
        result.text = result.kind === "acknowledged"
          ? "Favorites restore acknowledged. Review managed favorites; exact private contents are not verified by this redacted view."
          : result.kind === "error"
            ? favoritesTransferFailure(result.code)
            : result.favoritesLibraryDurabilityUnknown
              ? "The managed favorites library was replaced, but disk durability is unconfirmed. Matching readback cannot confirm durability. No retry was sent."
              : "Favorites restore outcome unknown. The library may have changed. Review managed favorites before another restore. No retry was sent.";
      }
      if (["favorite_target", "favorite_radio_target"].includes(body.action)) {
        // The redacted library cannot verify the exact URL, even with a fresh
        // matching hostname/name/ID. Never echo potentially private errors.
        result.text = result.kind === "acknowledged"
          ? "Target-change request acknowledged. The exact saved URL is not verified by this redacted view."
          : result.kind === "error"
            ? body.action === "favorite_radio_target"
              ? result.code === "radio_expired"
                ? "Station selection expired. Search again, then choose the favorite and metadata policy again. Nothing was changed."
                : "Replacement rejected. The favorite, station selection or renderer session may have changed. Close this dialog and review them before starting again."
              : "Target-change request rejected. Review the replacement URL and metadata choice before starting again."
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
      if (result.kind === "unknown") this.unknownOutcomes = this.unknownOutcomes.concat({id, label: body.action === "device_rename" ? "Rename room (persistence/discovery unconfirmed)" : result.favoritesLibraryDurabilityUnknown ? "Restore managed favorites (library replaced; durability unconfirmed)" : result.curveLibraryDurabilityUnknown ? "Import saved EQ curve (library replaced; durability unconfirmed)" : label, committed: result.committed === true}).slice(-20);
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
      if (["favorite_target", "favorite_radio_target"].includes(body.action) && result.kind === "acknowledged") {
        if (!this.favoritesFresh) result.text += " The favorites list could not be refreshed.";
        if (!this.fresh) result.text += " Current renderer state is unavailable. Refresh before another command.";
      }
      if (id === this.operation && result.kind === "acknowledged") {
        // An ACK is not a playback observation. State badges remain canonical.
        if (body.action === "device_rename") {
          this.notice = {...result, kind: result.deviceVerified ? "confirmed" : "acknowledged"};
        } else if (["favorite_target", "favorite_radio_target", "favorite_restore"].includes(body.action)) {
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
  return {RadioSearch, FAVORITES_EXPORT_FILENAME, FAVORITES_TRANSFER_CHANGED, favoritesRestoreBody, validateFavoritesExportSummary, validateFavoritesDocument, validateFavoritesExportDocument, favoritesTransferFailure, FavoritesRestoreDraft, CURVE_EXPORT_FILENAME, CURVE_TRANSFER_CHANGED, curveImportBody, validateCurveExportSummary, validateCurveDocument, validateCurveExportDocument, curveTransferFailure, CurveImportDraft, OutputInspection, validateOutputInspection, outputInspectionLines, Controller, EXPORT_FILENAME, exportIdentity, exportObject, validateExportSummary, validateExportDocument, exportFailure, readExportResponse, PlaylistDraft, PLAYLIST_ACTIONS, MAX_REQUEST_BYTES, requestBytes, playlistAvailable, pastedURLs, playlistPreview, DspDraft, DSP_MODES, DSP_FIELDS, DSP_KEYS, DSP_LIBRARY_UNAVAILABLE_MESSAGE, MODES, validateEditor, validateState, streamTitleText, audioFormatText, validateFavorites, safeAudioURL, safeArtwork, formatTime, guardedAction, commandError};
});
