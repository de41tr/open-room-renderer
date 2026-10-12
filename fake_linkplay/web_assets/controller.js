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
  function validateState(state) {
    if (!isObject(state) || state.version !== 1 || !string(state.session_id) || !state.session_id || !integer(state.revision) || !integer(state.generation) || !string(state.csrf_token) || !state.csrf_token || !isObject(state.device) || !string(state.device.name)) throw new Error("Invalid renderer snapshot");
    const {transport, media, gain, queue, dsp, actions} = state;
    if (!isObject(transport) || !PHASES.includes(transport.phase) || !["CONFIRMED", "PENDING", "ERROR"].includes(transport.status) || !string(transport.message)) throw new Error("Invalid transport snapshot");
    if (!isObject(media) || ![media.title, media.artist, media.album].every(string) || !(media.artwork === null || string(media.artwork)) || typeof media.is_live !== "boolean" || !optionalNumber(media.position_seconds) || !optionalNumber(media.duration_seconds)) throw new Error("Invalid media snapshot");
    if (!isObject(gain) || !["CONFIRMED", "PENDING", "UNKNOWN"].includes(gain.status) || !(gain.volume === null || (integer(gain.volume) && gain.volume <= 100)) || !optionalBoolean(gain.mute)) throw new Error("Invalid gain snapshot");
    if (!isObject(queue) || !string(queue.name) || !integer(queue.count) || queue.count > 1000 || !integer(queue.index) || !Object.hasOwn(MODES, queue.mode) || !Array.isArray(queue.items) || queue.items.length !== queue.count || (queue.count === 0 ? queue.index !== 0 : queue.index < 1 || queue.index > queue.count)) throw new Error("Invalid queue snapshot");
    queue.items.forEach((item, offset) => { if (!isObject(item) || item.index !== offset + 1 || ![item.title, item.artist, item.album].every(string) || !optionalNumber(item.duration_seconds)) throw new Error("Invalid queue item"); });
    if (!isObject(dsp) || !["CONFIRMED", "PENDING", "UNKNOWN"].includes(dsp.status) || !(dsp.balance === null || (typeof dsp.balance === "number" && Number.isFinite(dsp.balance) && Math.abs(dsp.balance) <= 1)) || !optionalBoolean(dsp.eq_enabled) || !string(dsp.name)) throw new Error("Invalid sound snapshot");
    if (!isObject(actions) || !["play", "pause", "stop", "next", "previous", "seek", "queue_select", "queue_mode", "save_current"].every(key => typeof actions[key] === "boolean")) throw new Error("Invalid actions snapshot");
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
    if (["play", "pause", "next", "previous", "seek", "queue_select", "queue_mode", "favorite_current"].includes(action)) body.expected_generation = state.generation;
    if (["queue_select", "queue_mode"].includes(action)) body.queue_name = state.queue.name;
    return body;
  }
  function commandError(response) {
    const body = response && response.body;
    if (isObject(body) && string(body.message) && ["REJECTED", "UNKNOWN", "ERROR"].includes(body.outcome)) {
      // Only explicit pre-application rejection means it did not take effect.
      const kind = body.outcome === "REJECTED" ? "error" : "unknown";
      return {kind, text: body.message, committed: body.committed === true};
    }
    return {kind: "unknown", text: "The request outcome could not be confirmed. Check the current state before trying again.", committed: false};
  }
  class Controller {
    constructor(request, onChange = () => {}) {
      this.request = request;
      this.onChange = onChange;
      this.state = null;
      this.favorites = [];
      this.fresh = false;
      this.favoritesFresh = false;
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
    canMutate(action) { return action === "stop" ? Boolean(this.state && !this.stopBusy) : Boolean(this.state && this.fresh && !this.busy && (!action.startsWith("favorite_") || this.favoritesFresh)); }
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
          this.state = state;
          this.fresh = true;
          this.lastReadAt = Date.now();
          acceptedState = true;
          if (!this.operation && !this.busy) this.notice = {kind: "confirmed", text: "Connected. Controls show the renderer’s reported state."};
        } catch (_) {
          this.fresh = false;
          if (!this.busy && !this.operation) this.notice = {kind: "unknown", text: "Cannot read the renderer. Last received values may be stale. Refresh to reconnect."};
        }
        try {
          if (!acceptedState || favoriteResult.status !== "fulfilled" || favoriteResult.value.status !== 200) throw new Error();
          this.favorites = validateFavorites(favoriteResult.value.body, this.state.session_id);
          this.favoritesFresh = true;
        } catch (_) { this.favoritesFresh = false; }
        this.changed();
      } while (this.readAgain);
      return this.fresh;
    }
    async mutate(body, label) {
      if (!isObject(body) || !string(body.action) || !this.canMutate(body.action)) return {kind: "blocked", text: "Read current state and wait for the pending request before trying again."};
      const id = ++this.operation;
      this.epoch += 1;
      this.fresh = false;
      if (body.action.startsWith("favorite_")) this.favoritesFresh = false;
      this.active.set(id, {action: body.action, label});
      this.notice = {kind: "pending", text: `${label}: waiting for the renderer.`, id};
      this.changed();
      let result;
      try {
        const response = await this.request("/orr/v1/web/command", {method: "POST", headers: {"X-ORR-CSRF": this.state.csrf_token}, body});
        if (response.status === 200 && isObject(response.body) && response.body.request_acknowledged === true) result = {kind: "acknowledged", text: `${label}: request acknowledged. Reading the current state…`, id, favoriteId: response.body.favorite_id || null};
        else result = {...commandError(response), id};
      } catch (_) {
        result = {kind: "unknown", text: `${label}: outcome unknown because the response was lost. The command may have taken effect. No retry was sent.`, id};
      }
      this.active.delete(id);
      this.fresh = false;
      if (result.kind === "unknown") this.unknownOutcomes = this.unknownOutcomes.concat({id, label, committed: result.committed === true}).slice(-20);
      if (id === this.operation) this.notice = {...result, kind: result.kind === "acknowledged" ? "pending" : result.kind};
      this.changed();
      await this.refresh();
      if (id === this.operation && result.kind === "acknowledged") {
        // An ACK is not a playback observation. State badges remain canonical.
        this.notice = this.fresh
          ? {kind: "confirmed", text: `${label}: request acknowledged; current renderer state received.`, id}
          : {kind: "unknown", text: `${label}: request acknowledged, but current state is unavailable. Refresh before another command.`, id};
      }
      this.changed();
      return result;
    }
  }
  return {Controller, MODES, validateState, validateFavorites, safeAudioURL, safeArtwork, formatTime, guardedAction, commandError};
});
