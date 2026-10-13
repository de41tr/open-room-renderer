/* Dependency-free control-flow tests; these are not browser/layout tests. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Controller, EXPORT_FILENAME, exportIdentity, exportObject, validateExportSummary, validateExportDocument, exportFailure, readExportResponse, PlaylistDraft, MAX_REQUEST_BYTES, requestBytes, playlistAvailable, pastedURLs, playlistPreview, DspDraft, DSP_KEYS, DSP_MODES, validateEditor, MODES, validateState, validateFavorites, safeAudioURL, safeArtwork, formatTime, guardedAction, commandError} = require('../fake_linkplay/web_assets/controller.js');
const clone = value => JSON.parse(JSON.stringify(value));
const revisionID = letter => letter.repeat(64);
const eqLibraryWarning = 'Saved EQ presets could not be loaded. Stop the renderer service, repair or restore its saved EQ preset library, then restart it.';
function editor(overrides = {}) {
  return {revision: revisionID('a'), source: 'global', channel_mode: 'Stereo', bands: DSP_KEYS.map(param_name => ({param_name, value: param_name.endsWith('_freq') ? 1000 : param_name.endsWith('_q') ? 1 : param_name.endsWith('_mode') ? 1 : 0})), presets: [
    {id: revisionID('b'), name: 'Flat', kind: 'builtin', shared: false},
    {id: revisionID('c'), name: 'My curve', kind: 'saved', shared: false},
    {id: revisionID('d'), name: 'Shared curve', kind: 'saved', shared: true}
  ], ...overrides};
}
const soundState = (value = editor(), changes = {}) => state({dsp: {...state().dsp, editor: value, ...changes}});
function state(overrides = {}) {
  return {
    version: 1, session_id: 'session-one', revision: 1, generation: 7, csrf_token: 'test-csrf-token', device: {name: 'Living room', revision: revisionID('1'), name_visible: true},
    transport: {phase: 'STOPPED', intent: 'stop', status: 'CONFIRMED', message: ''},
    media: {title: 'Track <script>bad()</script>', artist: 'Artist', album: '', artwork: null, is_live: false, position_seconds: 0, duration_seconds: 120},
    gain: {status: 'CONFIRMED', volume: 20, mute: false, last_confirmed: {volume: 20, mute: false}},
    queue: {playlist_edit_reason: 'supported', name: 'My queue', count: 2, index: 1, mode: 'normal', items: [{index: 1, title: 'One', artist: '', album: '', duration_seconds: 120}, {index: 2, title: 'Two', artist: '', album: '', duration_seconds: null}]},
    dsp: {status: 'CONFIRMED', balance: 0, eq_enabled: false, name: 'Default', editor: editor()},
    actions: {playlist_replace: true, playlist_append: true, playlist_remove: true, playlist_move: true, play: true, pause: false, stop: true, next: true, previous: false, seek: true, queue_select: true, queue_mode: true, save_current: true}, ...overrides
  };
}
const favorites = (items = [], session_id = 'session-one') => ({version: 1, session_id, favorites: items});
const favorite = () => ({id: 'favorite-id', editable: true, name: 'Radio', title: '', artist: '', album: '', artwork: null, target_label: 'example.com'});
const favoritesUnavailable = () => ({status: 503, body: {error: 'favorites_unavailable', message: 'Managed favorites could not be loaded. Stop the renderer service, repair or restore its saved favorites library, then restart it.', outcome: 'ERROR'}});
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; }
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  let current = state(), library = favorites(), revision = 0;
  const calls = [];
  const hooks = {};
  const request = async (path, options) => {
    calls.push({path, options: clone(options)});
    if (options.method === 'POST') return hooks.post ? hooks.post(path, options) : {status: 200, body: {request_acknowledged: true}};
    if (path.endsWith('/favorites')) return hooks.favorites ? hooks.favorites() : {status: 200, body: clone(library)};
    return hooks.state ? hooks.state() : {status: 200, body: {...clone(current), revision: ++revision}};
  };
  const controller = new Controller(request);
  return {controller, hooks, calls, setState: value => {current = value;}, setFavorites: value => {library = value;}, posts: () => calls.filter(call => call.options.method === 'POST')};
}
test('strict canonical snapshot and favorites validation', () => {
  assert.equal(validateState(state()).generation, 7);
  for (const change of [{version: 2}, {generation: -1}, {revision: NaN}, {session_id: ''}, {csrf_token: ''}, {actions: {}}, {transport: {...state().transport, phase: 'OK'}}, {queue: {...state().queue, count: 3}}, {gain: {...state().gain, volume: 101}}, {dsp: {...state().dsp, balance: Infinity}}]) assert.throws(() => validateState(state(change)));
  assert.equal(validateFavorites(favorites([favorite()]))[0].name, 'Radio');
  assert.throws(() => validateFavorites(favorites([favorite(), favorite()])));
  assert.throws(() => validateFavorites(favorites([{...favorite(), editable: 'yes'}])));
});
test('artwork is HTTP(S), credential-free, without URL interpretation tricks', () => {
  for (const value of [null, '', 'javascript:alert(1)', 'data:image/svg+xml,x', 'file:///etc/passwd', '//example.com/a', 'https://user:secret@example.com/a', 'https://a\\@b/x', 'https://example.com/a\nb', 'https://example.com/<script>', {}, 'http://']) assert.equal(safeArtwork(value), null);
  assert.equal(safeArtwork('https://example.com/cover.jpg'), 'https://example.com/cover.jpg');
  assert.equal(formatTime(null), '—'); assert.equal(formatTime(-1), '—'); assert.equal(formatTime(65.9), '1:05'); assert.equal(formatTime(3672), '1:01:12');
});
test('command guards capture canonical generation and queue identity', () => {
  const old = state();
  const body = guardedAction(old, 'queue_select', {index: 2});
  old.generation = 9; old.queue.name = 'replacement';
  assert.deepEqual(body, {action: 'queue_select', index: 2, expected_generation: 7, queue_name: 'My queue'});
  assert.deepEqual(guardedAction(old, 'stop'), {action: 'stop'});
  assert.equal(Object.keys(MODES).length, 5);
});
test('one initial canonical read enables mutations; no state is fabricated', async () => {
  const h = harness(); assert.equal(h.controller.canMutate('play'), false); await h.controller.refresh();
  assert.equal(h.controller.state.transport.phase, 'STOPPED'); assert.equal(h.controller.canMutate('play'), true); assert.equal(h.controller.favoritesFresh, true);
});
test('ACK is followed by canonical reads, never optimistic playback', async () => {
  const h = harness(); await h.controller.refresh();
  const result = await h.controller.mutate(guardedAction(h.controller.state, 'play'), 'Play');
  assert.equal(result.kind, 'acknowledged'); assert.equal(h.controller.state.transport.phase, 'STOPPED');
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].options.headers['X-ORR-CSRF'], 'test-csrf-token');
  assert.match(h.controller.notice.text, /request acknowledged/); assert.equal(h.calls.at(-2).path, '/orr/v1/web/state');
});
test('duplicate mutation is suppressed while explicit Stop can supersede waiting', async () => {
  const h = harness(); await h.controller.refresh(); const wait = deferred();
  h.hooks.post = async (_, options) => options.body.action === 'play' ? wait.promise : {status: 200, body: {request_acknowledged: true}};
  const playing = h.controller.mutate(guardedAction(h.controller.state, 'play'), 'Play');
  assert.equal(h.controller.busy, true); assert.equal(h.controller.fresh, false);
  const duplicate = await h.controller.mutate(guardedAction(h.controller.state, 'play'), 'Play'); assert.equal(duplicate.kind, 'blocked');
  assert.equal(h.controller.canMutate('stop'), true);
  const stopped = await h.controller.mutate({action: 'stop'}, 'Stop'); assert.equal(stopped.kind, 'acknowledged');
  wait.resolve({status: 200, body: {request_acknowledged: true}}); await playing;
  assert.equal(h.posts().length, 2); assert.match(h.controller.notice.text, /^Stop:/); assert.equal(h.controller.busy, false);
});
test('duplicate Stop is suppressed until its request resolves', async () => {
  const h = harness(); await h.controller.refresh(); const wait = deferred(); h.hooks.post = () => wait.promise;
  const first = h.controller.mutate({action: 'stop'}, 'Stop'); assert.equal(h.controller.canMutate('stop'), false);
  assert.equal((await h.controller.mutate({action: 'stop'}, 'Stop')).kind, 'blocked');
  wait.resolve({status: 200, body: {request_acknowledged: true}}); await first; assert.equal(h.posts().length, 1);
});
test('lost mutation response stays UNKNOWN after canonical reread and never retries', async () => {
  const h = harness(); await h.controller.refresh(); h.hooks.post = async () => { throw new Error('network'); };
  const result = await h.controller.mutate({action: 'volume', value: 35}, 'Volume');
  assert.equal(result.kind, 'unknown'); assert.equal(h.controller.notice.kind, 'unknown'); assert.equal(h.controller.fresh, true); assert.equal(h.controller.state.gain.volume, 20);
  await h.controller.refresh(); await h.controller.refresh(); assert.equal(h.posts().length, 1); assert.equal(h.controller.unknownOutcomes.length, 1);
  h.hooks.post = null; await h.controller.mutate({action: 'stop'}, 'Stop'); assert.equal(h.controller.unknownOutcomes[0].label, 'Volume');
});
test('old lost response does not replace newer Stop notice but unknown history survives', async () => {
  const h = harness(); await h.controller.refresh(); const wait = deferred();
  h.hooks.post = (_, options) => options.body.action === 'next' ? wait.promise : {status: 200, body: {request_acknowledged: true}};
  const next = h.controller.mutate(guardedAction(h.controller.state, 'next'), 'Next'); await h.controller.mutate({action: 'stop'}, 'Stop');
  wait.reject(new Error('lost')); await next;
  assert.match(h.controller.notice.text, /^Stop:/); assert.deepEqual(h.controller.unknownOutcomes.map(item => item.label), ['Next']);
});
test('pre-mutation reads cannot overwrite post-command canonical state', async () => {
  const h = harness(); await h.controller.refresh(); const stale = deferred(); let reads = 0;
  h.hooks.state = () => ++reads === 1 ? stale.promise : {status: 200, body: state({revision: 11, generation: 8, media: {...state().media, title: 'Fresh title'}})};
  const reading = h.controller.refresh(); await tick();
  const mutation = h.controller.mutate({action: 'stop'}, 'Stop'); await tick();
  stale.resolve({status: 200, body: state({revision: 999, media: {...state().media, title: 'Stale title'}})});
  await Promise.all([reading, mutation]);
  assert.equal(h.controller.state.media.title, 'Fresh title'); assert.equal(h.controller.state.revision, 11);
});
test('older snapshot revision is rejected; a new process resets revision and token', async () => {
  const h = harness(); await h.controller.refresh();
  h.hooks.state = () => ({status: 200, body: state({revision: 20})}); await h.controller.refresh();
  h.hooks.state = () => ({status: 200, body: state({revision: 19, media: {...state().media, title: 'Wrong'}})}); await h.controller.refresh();
  assert.equal(h.controller.state.revision, 20); assert.equal(h.controller.fresh, false);
  h.hooks.state = () => ({status: 200, body: state({revision: 1, session_id: 'restarted', csrf_token: 'new-token'})}); await h.controller.refresh();
  assert.equal(h.controller.fresh, true); await h.controller.mutate({action: 'stop'}, 'Stop'); assert.equal(h.posts()[0].options.headers['X-ORR-CSRF'], 'new-token');
});
test('failed reads retain explicit stale values while allowing Stop recovery', async () => {
  const h = harness(); await h.controller.refresh(); h.hooks.state = async () => {throw new Error();}; h.hooks.favorites = async () => {throw new Error();}; await h.controller.refresh();
  assert.equal(h.controller.fresh, false); assert.equal(h.controller.favoritesFresh, false); assert.equal(h.controller.state.gain.volume, 20);
  assert.equal(h.controller.canMutate('play'), false); assert.equal(h.controller.canMutate('stop'), true);
});
test('favorite durability uncertainty refreshes library and never repeats creation', async () => {
  const h = harness(); await h.controller.refresh();
  h.hooks.post = async () => { h.setFavorites(favorites([favorite()])); return {status: 503, body: {error: 'durability_unconfirmed', message: 'Saved, but crash durability is unconfirmed.', outcome: 'UNKNOWN', committed: true}}; };
  const result = await h.controller.mutate({action: 'favorite_add', name: 'Radio', url: 'https://example.com/a'}, 'Add favorite');
  assert.equal(result.kind, 'unknown'); assert.equal(result.committed, true); assert.equal(h.controller.favorites.length, 1); assert.equal(h.controller.unknownOutcomes[0].committed, true); assert.equal(h.posts().length, 1);
});
test('explicit pre-application rejection differs from unknown or error-after-apply', () => {
  assert.equal(commandError({status: 409, body: {outcome: 'REJECTED', message: 'Stale selection'}}).kind, 'error');
  assert.equal(commandError({status: 503, body: {outcome: 'ERROR', message: 'Failed'}}).kind, 'unknown');
  assert.equal(commandError({status: 200, body: {ok: true}}).kind, 'unknown');
});
test('pending gain and DSP never become fabricated confirmed values', async () => {
  const h = harness(); const next = state(); next.gain = {...next.gain, status: 'UNKNOWN', volume: null, mute: null}; next.dsp = {...next.dsp, status: 'PENDING', editor: null, balance: null, eq_enabled: null}; h.setState(next); await h.controller.refresh();
  assert.equal(h.controller.state.gain.volume, null); assert.equal(h.controller.state.dsp.balance, null); assert.equal(h.controller.state.dsp.status, 'PENDING');
});
test('configured favorites do not consume the managed-only 100 item limit', () => {
  const entries = Array.from({length: 102}, (_, index) => ({...favorite(), id: `favorite-${index}`, editable: index > 1}));
  assert.equal(validateFavorites(favorites(entries)).length, 102);
});

/* Deliberately small DOM test double. It checks event/state wiring only, never
 * claims real browser semantics, CSS layout, image loading, or accessibility QA. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
async function appHarness({controllerClass = Controller} = {}) {
  const ids = new Map();
  let current = state(), library = favorites([favorite()]), revision = 0, postHook = null, exportHook = null;
  const requests = [], events = new Map(), timers = new Map(), blobs = new Map(), created = [], revoked = [], syntheticClicks = [], focusCalls = []; let timerId = 0;
  class Element {
    constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = new Map(); this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this.checked = false; this.classList = {toggle() {}}; this.open = false; }
    get isConnected() {return this.attached === true || Boolean(this.parentNode?.isConnected);}
    get value() {return this._value || '';}
    set value(value) {this._value = value; if (this.type === 'file' && value === '') this.files = [];}
    setAttribute(key, value) {this.attributes[key] = value; if (key === 'src') this.src = value;}
    getAttribute(key) {return this.attributes[key];}
    removeAttribute(key) {delete this.attributes[key];}
    addEventListener(name, callback) {if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(callback);}
    async fire(name) {const event = {defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}}; for (const callback of this.listeners.get(name) || []) await callback(event); return event;}
    click() {syntheticClicks.push(this.id); return this.fire('click');}
    append(...children) {for (const child of children.flatMap(child => child.tagName === 'FRAGMENT' ? child.children : [child])) {child.parentNode = this; this.children.push(child);}}
    replaceChildren(...children) {for (const child of this.children) child.parentNode = null; this.children = []; this.append(...children);}
    querySelectorAll(tag) {const found = []; for (const child of this.children) {if (child.tagName.toLowerCase() === tag) found.push(child); found.push(...child.querySelectorAll(tag));} return found;}
    closest(selector) {
      assert.equal(selector, '[hidden], [inert], details:not([open])');
      for (let node = this; node; node = node.parentNode) if (node.hidden || node.inert || node.tagName === 'DETAILS' && !node.open) return node;
      return null;
    }
    focus() {focusCalls.push(this); if (this.isConnected && !this.disabled && !this.closest('[hidden], [inert], details:not([open])')) document.activeElement = this;}
    blur() {if (document.activeElement === this) document.activeElement = null;}
    showModal() {this.open = true;}
    close() {if (!this.open) return; this.open = false; this.fire('close');}
    reset() {ids.get('favorite-name').value = ''; ids.get('favorite-url').value = '';}
  }
  const html = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/index.html'), 'utf8');
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]); element.attached = true; element.id = match[3]; element.hidden = /\bhidden\b/.test(match[2]); element.disabled = /\bdisabled\b/.test(match[2]);
    const type = match[2].match(/\btype="([^"]*)"/); if (type) element.type = type[1];
    const value = match[2].match(/\bvalue="([^"]*)"/); if (value) element.value = value[1];
    const download = match[2].match(/\bdownload="([^"]*)"/); if (download) element.setAttribute('download', download[1]); ids.set(element.id, element);
  }
  const document = {activeElement: null, hidden: false, getElementById: id => {assert.ok(ids.has(id), `Unknown element ${id}`); return ids.get(id);}, createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), addEventListener: (name, callback) => events.set(name, callback)};
  const window = {RendererControl: {...require('../fake_linkplay/web_assets/controller.js'), Controller: controllerClass}, setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;}, clearTimeout: id => timers.delete(id), addEventListener: (name, callback) => events.set(name, callback)};
  const fetch = async (url, options) => {
    requests.push({url, options}); let payload;
    if (url.endsWith('/playlist/export')) return exportHook ? exportHook(JSON.parse(options.body), options) : exportResponse(JSON.parse(options.body).action === 'prepare' ? exportSummary(current) : privateJSON(), {attachment: JSON.parse(options.body).action === 'download'});
    if (options.method === 'POST') payload = postHook ? await postHook(JSON.parse(options.body)) : {request_acknowledged: true};
    else if (url.endsWith('/favorites')) payload = typeof library === 'function' ? await library() : clone(library);
    else payload = typeof current === 'function' ? await current() : {...clone(current), revision: ++revision};
    const status = payload.__status || 200; delete payload.__status;
    return new Response(payload.__raw ?? JSON.stringify(payload), {status, headers: {'Content-Type': 'application/json'}});
  };
  class TestURL extends URL {
    static createObjectURL(blob) {const url = 'blob:owned-' + (created.length + 1); blobs.set(url, blob); created.push(url); return url;}
    static revokeObjectURL(url) {assert.ok(blobs.has(url), 'Only revoke a live, owned URL'); blobs.delete(url); revoked.push(url);}
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/app.js'), 'utf8'), {window, document, fetch, AbortController, TextEncoder, TextDecoder, Uint8Array, URL: TestURL, Blob, Date, console});
  async function settle() {for (let i = 0; i < 10; i += 1) await tick();}
  await settle();
  return {ids, timers, requests, blobs, created, revoked, syntheticClicks, focusCalls, focused: () => document.activeElement, setExport: fn => {exportHook = fn;}, exports: () => requests.filter(item => item.url.endsWith('/playlist/export')), pagehide: (persisted = true) => events.get('pagehide')?.({type: 'pagehide', persisted}), pageshow: () => events.get('pageshow')?.({type: 'pageshow', persisted: true}), offline: () => events.get('offline')?.({type: 'offline'}), setState: value => {current = value;}, setFavorites: value => {library = value;}, setPost: fn => {postHook = fn;}, settle, refresh: async () => {await ids.get('refresh').fire('click'); await settle();}, posts: () => requests.filter(item => item.options.method === 'POST').map(item => JSON.parse(item.options.body))};
}
test('app renders metadata as text, and polling preserves favorite form edits', async () => {
  const h = await appHarness();
  assert.equal(h.ids.get('track-title').textContent, 'Track <script>bad()</script>'); assert.equal(h.ids.get('track-title').children.length, 0);
  await h.ids.get('add-favorite').fire('click'); h.ids.get('favorite-name').value = 'My unsaved name'; h.ids.get('favorite-url').value = 'https://example.com/private?token=abc';
  h.setState(state({media: {...state().media, title: 'New title'}})); await h.refresh();
  assert.equal(h.ids.get('favorite-name').value, 'My unsaved name'); assert.equal(h.ids.get('favorite-url').value, 'https://example.com/private?token=abc');
  assert.equal(h.ids.get('favorite-dialog').open, true);
});
const currentDraftWarning = 'The current selection changed or the renderer restarted. Close this dialog and review the current track or stream before reopening Save current.';
test('Save current latches fresh selection or session changes, preserves input, and requires reopening', async () => {
  for (const changed of [state({generation: 8}), state({session_id: 'restarted'})]) {
    const h = await appHarness(); await h.ids.get('save-current').fire('click'); h.ids.get('favorite-name').value = 'My unsent name';
    h.setState(changed); h.setFavorites(favorites([favorite()], changed.session_id)); await h.refresh();
    assert.equal(h.ids.get('favorite-dialog').open, true); assert.equal(h.ids.get('favorite-name').value, 'My unsent name');
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('favorite-dialog-status').textContent, currentDraftWarning);
    // A stale draft stays blocked even if the observation returns to its original identity.
    h.setState(state()); h.setFavorites(favorites([favorite()])); await h.refresh();
    h.ids.get('favorite-submit').disabled = false; await h.ids.get('favorite-form').fire('submit');
    assert.equal(h.posts().length, 0); assert.equal(h.ids.get('favorite-submit').disabled, true);
    assert.equal(h.ids.get('favorite-dialog-status').textContent, currentDraftWarning);
    h.setState(changed); h.setFavorites(favorites([favorite()], changed.session_id)); await h.refresh();
    const dialog = h.ids.get('favorite-dialog'); dialog.close = () => {dialog.open = false;};
    await h.ids.get('favorite-close').fire('click'); await h.ids.get('save-current').fire('click'); await dialog.fire('close');
    assert.equal(dialog.open, true); assert.equal(h.ids.get('favorite-name').value, changed.media.title);
    assert.equal(h.ids.get('favorite-dialog-status').textContent, ''); assert.equal(h.ids.get('favorite-submit').disabled, false);
    h.ids.get('favorite-name').value = 'New deliberate save'; await h.ids.get('favorite-form').fire('submit');
    assert.deepEqual(h.posts(), [{action: 'favorite_current', name: 'New deliberate save', expected_generation: changed.generation}]);
  }
});
test('Save current distinguishes read loss and unchanged metadata from a fresh selection change', async () => {
  const h = await appHarness(); await h.ids.get('save-current').fire('click'); h.ids.get('favorite-name').value = 'Keep this name';
  for (const unavailable of ['state', 'favorites', 'offline']) {
    if (unavailable === 'state') h.setState({generation: 8, session_id: 'unaccepted'});
    else if (unavailable === 'favorites') h.setFavorites(favorites([], 'mixed-session'));
    if (unavailable === 'offline') h.offline(); else await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0);
    h.setState(state({media: {...state().media, title: 'Updated metadata'}})); h.setFavorites(favorites([])); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-name').value, 'Keep this name');
    assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  }
  // A valid state change is sufficient even when the favorites read fails.
  h.setState(state({generation: 8})); h.setFavorites({}); await h.refresh();
  assert.equal(h.ids.get('favorite-dialog-status').textContent, currentDraftWarning);
  h.setState(state()); h.setFavorites(favorites([favorite()])); await h.refresh();
  await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0); assert.equal(h.ids.get('favorite-submit').disabled, true);
});
test('Save current preserves pending and unverified outcomes after an observed change then restoration', async () => {
  for (const outcome of ['acknowledged', 'unknown', 'rejected']) {
    const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
    await h.ids.get('save-current').fire('click'); h.ids.get('favorite-name').value = 'Saved selection';
    const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
    h.setState(state({generation: 8})); await h.refresh(); h.setState(state()); await h.refresh();
    assert.match(h.ids.get('favorite-dialog-status').textContent, /^PENDING/);
    assert.doesNotMatch(h.ids.get('favorite-dialog-status').textContent, /Close this dialog/);
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1);
    if (outcome === 'unknown') pending.reject(new Error('lost response'));
    else if (outcome === 'rejected') pending.resolve({__status: 409, error: 'stale_generation', outcome: 'REJECTED', message: 'Refresh before retrying.'});
    else pending.resolve({request_acknowledged: true, favorite_id: 'saved-id'});
    await saving; await h.refresh();
    const status = h.ids.get('favorite-dialog-status').textContent;
    assert.match(status, outcome === 'acknowledged' ? /^Request acknowledged.*not yet been verified/ : outcome === 'unknown' ? /^UNKNOWN.*No retry was sent/ : /^ERROR.*Refresh before retrying/);
    assert.ok(status.endsWith(currentDraftWarning)); assert.equal(h.ids.get('favorite-dialog').open, true);
    assert.equal(h.ids.get('favorite-name').value, 'Saved selection'); assert.equal(h.ids.get('favorite-submit').disabled, true);
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1);
    assert.deepEqual(h.posts()[0], {action: 'favorite_current', name: 'Saved selection', expected_generation: 7});
  }
});
test('Save current still closes on verified pending ACK and retains a prior UNKNOWN after later change', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
  await h.ids.get('save-current').fire('click'); h.ids.get('favorite-name').value = 'Saved selection';
  const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
  h.setState(state({generation: 8})); h.setFavorites(favorites([{...favorite(), id: 'saved-id', name: 'Saved selection'}])); await h.refresh();
  assert.match(h.ids.get('favorite-dialog-status').textContent, /^PENDING/);
  pending.resolve({request_acknowledged: true, favorite_id: 'saved-id'}); await saving;
  assert.equal(h.ids.get('favorite-dialog').open, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  await h.ids.get('save-current').fire('click'); h.setPost(() => {throw new Error('lost response');}); await h.ids.get('favorite-form').fire('submit');
  const outcome = h.ids.get('favorite-dialog-status').textContent; assert.match(outcome, /^UNKNOWN.*No retry was sent/);
  h.setState(state({generation: 9})); await h.refresh();
  assert.equal(h.ids.get('favorite-dialog-status').textContent, outcome + ' ' + currentDraftWarning);
  await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 2); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
});
test('Save current navigation clears stale drafts without resending or hiding a late outcome', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
  await h.ids.get('save-current').fire('click'); h.ids.get('favorite-name').value = 'Old name';
  const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
  h.setState(state({generation: 8})); await h.refresh(); h.pagehide(); h.pageshow();
  assert.equal(h.ids.get('favorite-dialog').open, false); assert.equal(h.ids.get('favorite-name').value, '');
  pending.reject(new Error('lost response')); await saving; await h.refresh();
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  await h.ids.get('save-current').fire('click'); assert.equal(h.ids.get('favorite-submit').disabled, false);
  h.setState(state({generation: 9})); await h.refresh(); assert.equal(h.ids.get('favorite-dialog-status').textContent, currentDraftWarning);
  h.pagehide(); h.pageshow(); await h.refresh();
  assert.equal(h.ids.get('favorite-dialog').open, false); assert.equal(h.ids.get('favorite-name').value, '');
  assert.equal(h.ids.get('favorite-dialog-status').textContent, ''); assert.equal(h.posts().length, 1);
});
test('favorite Rename and Delete preserve drafts but reject an observed external change or removal', async () => {
  for (const mode of ['rename', 'delete']) for (const removed of [false, true]) for (const restart of [false, true]) {
    const h = await appHarness(); await favoriteAction(h, mode).fire('click');
    const session = restart ? 'restarted' : 'session-one';
    if (restart) h.setState(state({session_id: session}));
    h.ids.get('favorite-name').value = 'My unsent name';
    h.setFavorites(favorites(removed ? [] : [{...favorite(), name: 'Changed remotely'}], session)); await h.refresh();
    assert.equal(h.ids.get('favorite-dialog').open, true);
    assert.equal(h.ids.get('favorite-name').value, 'My unsent name');
    assert.equal(h.ids.get('favorite-delete-name').textContent, 'Radio');
    assert.equal(h.ids.get('favorite-submit').disabled, true);
    const message = h.ids.get('favorite-dialog-status').textContent;
    assert.equal(message, 'This favorite changed or is no longer available. Close this dialog and review the favorites list before reopening Rename or Delete.');
    // Calling the handler directly must be safe even without the disabled button.
    h.ids.get('favorite-submit').disabled = false; await h.ids.get('favorite-form').fire('submit');
    assert.equal(h.posts().length, 0); assert.equal(h.ids.get('favorite-submit').disabled, true);
    assert.equal(h.ids.get('favorite-dialog-status').textContent, message);
    h.setFavorites(favorites([favorite()], session)); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true); // No silent rebase after restoration.
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0);
    const dialog = h.ids.get('favorite-dialog'); dialog.close = () => {dialog.open = false;};
    await h.ids.get('favorite-cancel').fire('click'); await favoriteAction(h, mode).fire('click');
    await dialog.fire('close'); // A queued old dismissal cannot restore the stale draft.
    assert.equal(dialog.open, true); assert.equal(h.ids.get('favorite-name').value, 'Radio');
    assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  }
});
test('favorite drafts observe the full redacted record rather than only its name', async () => {
  for (const change of [{editable: false}, {title: 'New title'}, {artist: 'New artist'}, {album: 'New album'}, {artwork: 'https://example.test/new.jpg'}, {target_label: 'example.test'}]) {
    const h = await appHarness(); await favoriteAction(h, 'rename').fire('click');
    h.ids.get('favorite-name').value = 'My unsent name'; h.setFavorites(favorites([{...favorite(), ...change}])); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true, JSON.stringify(change));
    assert.equal(h.ids.get('favorite-name').value, 'My unsent name');
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0);
  }
});
test('unchanged favorite observations and unrelated rows preserve an editable draft across restart', async () => {
  for (const mode of ['rename', 'delete']) {
    const h = await appHarness(); await favoriteAction(h, mode).fire('click'); h.ids.get('favorite-name').value = 'My unsent name';
    // Property order and other rows are not a change to the selected record.
    const reordered = Object.fromEntries(Object.entries(favorite()).reverse());
    h.setFavorites(favorites([{...favorite(), id: 'other-id', name: 'Other station'}, reordered])); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
    h.setState(state({session_id: 'restarted'})); h.setFavorites(favorites([reordered], 'restarted')); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-name').value, 'My unsent name');
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].id, 'favorite-id');
  }
});
test('favorite drafts ignore unaccepted and mixed-session observations until fresh matching readback', async () => {
  for (const mode of ['rename', 'delete']) {
    const h = await appHarness(); await favoriteAction(h, mode).fire('click');
    h.setFavorites(favorites([{...favorite(), name: 'Unaccepted change'}], 'wrong-session')); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0);
    h.setState({}); h.setFavorites(favorites([])); await h.refresh();
    assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
    h.setState(state()); h.setFavorites(favorites([favorite()])); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
    h.setFavorites(favorites([{...favorite(), name: 'Accepted change'}])); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.match(h.ids.get('favorite-dialog-status').textContent, /review the favorites list/);
  }
});
test('Add stays self-contained across a renderer restart and changed favorites', async () => {
  const h = await appHarness(); await h.ids.get('add-favorite').fire('click');
  h.ids.get('favorite-name').value = 'My new station'; h.ids.get('favorite-url').value = 'https://example.test/audio?signature=private';
  h.setState(state({session_id: 'restarted'})); h.setFavorites(favorites([], 'restarted')); await h.refresh();
  assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  await h.ids.get('favorite-form').fire('submit');
  assert.deepEqual(h.posts(), [{action: 'favorite_add', name: 'My new station', url: 'https://example.test/audio?signature=private'}]);
  assert.doesNotMatch(h.ids.get('favorite-dialog-status').textContent, /signature|private/);
});
test('own in-flight favorite rename or deletion preserves pending status and verified closure', async () => {
  for (const mode of ['rename', 'delete']) {
    const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
    await favoriteAction(h, mode).fire('click'); h.ids.get('favorite-name').value = 'My saved name';
    const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
    h.setFavorites(favorites(mode === 'rename' ? [{...favorite(), name: 'My saved name'}] : [])); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.match(h.ids.get('favorite-dialog-status').textContent, /^PENDING/);
    assert.doesNotMatch(h.ids.get('favorite-dialog-status').textContent, /review the favorites list/);
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1);
    pending.resolve({request_acknowledged: true}); await saving;
    assert.equal(h.ids.get('favorite-dialog').open, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
    assert.equal(h.posts().length, 1);
  }
});
test('a changed-then-restored observation during an unsuccessful or unverified favorite request stays stale', async () => {
  for (const mode of ['rename', 'delete']) for (const result of ['rejected', 'unknown', 'acknowledged']) {
    const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
    await favoriteAction(h, mode).fire('click'); h.ids.get('favorite-name').value = 'My saved name';
    const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
    h.setFavorites(favorites([{...favorite(), name: 'Changed while waiting'}])); await h.refresh();
    h.setFavorites(favorites([favorite()])); await h.refresh();
    assert.match(h.ids.get('favorite-dialog-status').textContent, /^PENDING/);
    if (result === 'rejected') pending.resolve({__status: 409, outcome: 'REJECTED', message: 'Request rejected.'});
    else if (result === 'acknowledged') pending.resolve({request_acknowledged: true});
    else pending.reject(new Error('lost response'));
    await saving; await h.refresh();
    assert.match(h.ids.get('favorite-dialog-status').textContent, result === 'rejected' ? /^ERROR.*Request rejected/ : result === 'acknowledged' ? /Request acknowledged.*not yet been verified/ : /^UNKNOWN.*No retry was sent/);
    assert.match(h.ids.get('favorite-dialog-status').textContent, /review the favorites list/);
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('favorite-name').value, 'My saved name');
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1);
  }
});
test('changed favorite readback retains unknown or unverified submission outcomes alongside review guidance', async () => {
  for (const acknowledged of [false, true]) {
    const h = await appHarness(); await favoriteAction(h, 'rename').fire('click'); h.ids.get('favorite-name').value = 'My saved name';
    h.setPost(() => {h.setFavorites(favorites([{...favorite(), name: 'Another name'}])); if (!acknowledged) throw new Error('lost response'); return {request_acknowledged: true};});
    await h.ids.get('favorite-form').fire('submit'); await h.refresh();
    assert.equal(h.ids.get('favorite-dialog').open, true); assert.equal(h.ids.get('favorite-name').value, 'My saved name');
    assert.match(h.ids.get('favorite-dialog-status').textContent, acknowledged ? /Request acknowledged.*not yet been verified/ : /^UNKNOWN.*No retry was sent/);
    assert.match(h.ids.get('favorite-dialog-status').textContent, /Close this dialog and review the favorites list/);
    assert.equal(h.ids.get('favorite-submit').disabled, true);
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1);
  }
});

test('app preserves slider draft across polls and submits explicit value once', async () => {
  const h = await appHarness(); const slider = h.ids.get('volume'); slider.value = '42'; await slider.fire('input');
  await h.refresh(); assert.equal(slider.value, '42'); assert.equal(h.ids.get('volume-draft').hidden, false);
  await h.ids.get('volume-form').fire('submit'); assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].value, 42); assert.equal(slider.value, '20'); // canonical state, no optimistic 42
});
test('app rejects seek, mode and save-current drafts after same-generation restart', async () => {
  for (const mode of ['seek', 'mode', 'current']) {
    const h = await appHarness();
    if (mode === 'current') {await h.ids.get('save-current').fire('click'); h.ids.get('favorite-name').value = 'Saved old track';}
    else {h.ids.get(mode).value = mode === 'seek' ? '60' : 'repeat-all'; await h.ids.get(mode).fire('input');}
    h.setState(state({session_id: 'restarted'})); h.setFavorites(favorites([favorite()], 'restarted')); await h.refresh();
    await h.ids.get(mode === 'current' ? 'favorite-form' : mode + '-form').fire('submit');
    assert.equal(h.posts().length, 0, mode);
    assert.match(h.ids.get(mode === 'current' ? 'favorite-dialog-status' : 'notice-text').textContent, /restarted/);
  }
});
test('app hides unconfirmed gain and DSP values instead of presenting old settings', async () => {
  const h = await appHarness(); const next = state(); next.gain = {...next.gain, status: 'UNKNOWN', volume: null, mute: null}; next.dsp = {...next.dsp, status: 'PENDING', editor: null, balance: null, eq_enabled: null, name: 'Old preset'}; h.setState(next); await h.refresh();
  assert.equal(h.ids.get('volume-current').textContent, 'UNKNOWN'); assert.equal(h.ids.get('volume').hidden, true);
  assert.equal(h.ids.get('balance-current').textContent, 'PENDING'); assert.equal(h.ids.get('balance').hidden, true); assert.equal(h.ids.get('eq-current').textContent, 'PENDING');
  assert.equal(h.ids.get('eq').disabled, true); assert.equal(h.ids.get('stop').disabled, false);
});
test('app keeps only one poll timer after repeated refresh clicks', async () => {
  const h = await appHarness(); await Promise.all(Array.from({length: 8}, () => h.ids.get('refresh').fire('click'))); await h.settle();
  assert.equal([...h.timers.values()].filter(timer => timer.delay === 2000).length, 1);
});
test('app confirms favorite save from stable returned ID, and never wipes pending input', async () => {
  const h = await appHarness(); const pending = deferred(); h.setPost(() => pending.promise);
  await h.ids.get('add-favorite').fire('click'); h.ids.get('favorite-name').value = 'New radio'; h.ids.get('favorite-url').value = 'https://example.com/live?signature=secret';
  const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
  assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('stop').disabled, false); assert.equal(h.ids.get('favorite-name').value, 'New radio');
  h.setFavorites(favorites([{...favorite(), id: 'added-id', name: 'New radio'}])); pending.resolve({request_acknowledged: true, favorite_id: 'added-id'}); await saving;
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].url, 'https://example.com/live?signature=secret'); assert.equal(h.ids.get('favorite-dialog').open, false);
});
test('app never labels uncertain native output PLAYING based on phase alone', async () => {
  const h = await appHarness(); h.setState(state({transport: {phase: 'PLAYING', intent: 'play', status: 'ERROR', message: 'Output state is unknown. Stop is available for recovery.'}})); await h.refresh();
  assert.equal(h.ids.get('playback-state').textContent, 'ERROR'); assert.equal(h.ids.get('notice-kind').textContent, 'ERROR'); assert.match(h.ids.get('notice-text').textContent, /unknown/); assert.equal(h.ids.get('stop').disabled, false);
});

test('artwork refuses query and fragment secrets while audio URLs retain signed queries', () => {
  for (const url of ['https://example.com/art?token=secret', 'https://example.com/art#token', 'https://example.com/art?', 'https://example.com/art#']) assert.equal(safeArtwork(url), null);
  assert.equal(safeAudioURL('https://example.com/audio?signature=secret'), 'https://example.com/audio?signature=secret');
});

test('cross-restart favorites/state pair remains stale until both sessions agree', async () => {
  const h = harness(); h.setFavorites(favorites([{...favorite(), id: 'configured:1', editable: false, name: 'Old station'}])); await h.controller.refresh();
  h.setState(state({session_id: 'restarted'}));
  await h.controller.refresh();
  assert.equal(h.controller.state.session_id, 'restarted'); assert.equal(h.controller.fresh, true); assert.equal(h.controller.favoritesFresh, false);
  assert.equal(h.controller.favorites[0].name, 'Old station'); assert.equal(h.controller.canMutate('favorite_play'), false); assert.equal(h.posts().length, 0);
  h.setFavorites(favorites([{...favorite(), id: 'configured:1', editable: false, name: 'New station'}], 'restarted')); await h.controller.refresh();
  assert.equal(h.controller.favoritesFresh, true); assert.equal(h.controller.favorites[0].name, 'New station');
  assert.throws(() => validateFavorites({version: 1, favorites: []}, 'restarted'));
});
test('app disables configured favorite clicks for a mixed-session read', async () => {
  const h = await appHarness(); h.setFavorites(favorites([{...favorite(), id: 'configured:1', editable: false}])); await h.refresh();
  h.setState(state({session_id: 'restarted'})); await h.refresh();
  assert.equal(h.ids.get('favorites-list').querySelectorAll('button')[0].disabled, true); assert.equal(h.ids.get('add-favorite').disabled, true); assert.equal(h.ids.get('save-current').disabled, true);
  assert.match(h.ids.get('favorites-note').textContent, /stale/); assert.equal(h.posts().length, 0);
});

test('closing favorite dialog clears URL input without cancelling or hiding unknown outcome', async () => {
  const h = await appHarness(); const pending = deferred(); h.setPost(() => pending.promise);
  await h.ids.get('add-favorite').fire('click'); h.ids.get('favorite-name').value = 'Private radio'; h.ids.get('favorite-url').value = 'https://example.com/live?secret=token';
  const saving = h.ids.get('favorite-form').fire('submit'); await h.settle();
  await h.ids.get('favorite-close').fire('click');
  assert.equal(h.ids.get('favorite-url').value, ''); assert.equal(h.ids.get('favorite-name').value, ''); assert.equal(h.ids.get('favorite-dialog').open, false);
  pending.reject(new Error('response lost')); await saving;
  assert.equal(h.posts().length, 1); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.match(h.ids.get('notice-text').textContent, /No retry was sent/);
});
test('native Escape close event clears favorite fields', async () => {
  const h = await appHarness(); await h.ids.get('add-favorite').fire('click'); h.ids.get('favorite-url').value = 'https://example.com/live?secret=token';
  h.ids.get('favorite-dialog').open = false; // Native dismissal removes open before queuing close.
  await h.ids.get('favorite-dialog').fire('close'); assert.equal(h.ids.get('favorite-url').value, '');
});

test('sound editor validates exact canonical bands, bounded numbers, modes and opaque preset IDs', () => {
  assert.equal(validateEditor(editor()).bands.length, 40);
  assert.equal(Object.keys(DSP_MODES).length, 6);
  for (const changes of [{revision: 'raw-secret-name'}, {source: 'file:///tmp/plugin'}, {channel_mode: 'Mono'}, {bands: []}, {presets: [{...editor().presets[0], id: 'Flat'}]}, {presets: [editor().presets[0], editor().presets[0]]}]) assert.throws(() => validateEditor(editor(changes)));
  for (const [key, values] of [['a_freq', [9, 24001, '100', true, NaN, Infinity]], ['a_gain', [-25, 25]], ['a_q', [0, 0.09, 31]], ['a_mode', [-2, 4, 1.5, 6]]]) {
    for (const value of values) {const next = editor(); next.bands.find(band => band.param_name === key).value = value; assert.throws(() => validateEditor(next), `${key}: ${value}`);}
  }
  const swapped = editor(); [swapped.bands[0], swapped.bands[1]] = [swapped.bands[1], swapped.bands[0]]; assert.throws(() => validateEditor(swapped));
  assert.throws(() => validateState(soundState(editor(), {status: 'PENDING'})));
  assert.equal(validateState(soundState(null, {status: 'UNKNOWN', balance: null, eq_enabled: null})).dsp.editor, null);
});
test('saved EQ library availability is an optional strict boolean without changing healthy snapshots', () => {
  const healthy = editor(); assert.equal(validateEditor(healthy), healthy); assert.equal(Object.hasOwn(healthy, 'library_unavailable'), false);
  for (const value of [true, false]) assert.equal(validateState(soundState(editor({library_unavailable: value}))).dsp.editor.library_unavailable, value);
  for (const value of [undefined, null, 0, 1, 'true', 'false', [], {}]) assert.throws(() => validateEditor(editor({library_unavailable: value})), /Invalid sound preset library availability/);
});
test('sound drafts preserve input, never mutate canonical state, and require explicit reload after any stale revision', () => {
  const draft = new DspDraft(), original = state(); draft.observe(original, true); draft.edit('a_gain', '3.0');
  draft.observe(state({generation: 22}), true); assert.equal(draft.values.a_gain, '3.0'); assert.equal(draft.dirty, true); assert.equal(draft.canApply(original, true), true); assert.equal(original.dsp.editor.bands[1].value, 0);
  draft.observe(soundState(editor({revision: revisionID('e')})), true); assert.equal(draft.stale, true); assert.equal(draft.canApply(original, true), false);
  draft.observe(original, true); assert.equal(draft.stale, true); // No silent revalidation after a revision changed and reverted.
  draft.load(original); assert.equal(draft.stale, false); assert.equal(draft.dirty, false); assert.equal(draft.values.a_gain, '0');
  draft.edit('a_gain', ''); assert.equal(draft.bands, null); assert.equal(draft.canApply(original, true), false);
  draft.edit('a_gain', '0.0'); assert.equal(draft.dirty, false); draft.edit('a_gain', '2');
  draft.observe(state({session_id: 'restarted'}), true); assert.equal(draft.stale, true);
});
test('sound guard and controller block unconfirmed editing without blocking Stop', async () => {
  const current = state(); assert.deepEqual(guardedAction(current, 'dsp_preset_load', {preset_id: revisionID('b')}), {action: 'dsp_preset_load', preset_id: revisionID('b'), expected_dsp: revisionID('a')});
  const h = harness(); h.setState(soundState(null, {status: 'PENDING', balance: null, eq_enabled: null})); await h.controller.refresh();
  assert.equal(h.controller.canMutate('dsp_bands'), false); assert.equal(h.controller.canMutate('stop'), true);
  assert.equal((await h.controller.mutate({action: 'dsp_bands', bands: editor().bands, expected_dsp: revisionID('a')}, 'Apply')).kind, 'blocked'); assert.equal(h.posts().length, 0);
});
test('unavailable saved EQ library blocks guarded and direct controller writes without blocking live sound', async () => {
  const h = harness(); h.setState(soundState(editor({library_unavailable: true}))); await h.controller.refresh();
  for (const action of ['dsp_preset_save', 'dsp_preset_rename', 'dsp_preset_delete']) {
    assert.equal(h.controller.canMutate(action), false, action);
    assert.throws(() => guardedAction(h.controller.state, action, {name: 'New', preset_id: revisionID('c')}), {message: eqLibraryWarning});
    assert.equal((await h.controller.mutate({action, expected_dsp: revisionID('a'), name: 'New', preset_id: revisionID('c')}, 'Write preset')).kind, 'blocked');
  }
  assert.equal(h.posts().length, 0);
  for (const action of ['dsp_bands', 'dsp_preset_load', 'balance', 'eq', 'stop']) {
    assert.equal(h.controller.canMutate(action), true, action);
    assert.doesNotThrow(() => guardedAction(h.controller.state, action));
  }
  for (const availability of [{}, {library_unavailable: false}]) {
    h.setState(soundState(editor(availability))); await h.controller.refresh();
    for (const action of ['dsp_preset_save', 'dsp_preset_rename', 'dsp_preset_delete']) assert.equal(h.controller.canMutate(action), true, action);
  }
});
async function editBand(h, key, value) {h.ids.get('dsp-' + key).value = value; await h.ids.get('dsp-' + key).fire('input');}
async function selectPreset(h, id) {h.ids.get('dsp-preset').value = id; await h.ids.get('dsp-preset').fire('change');}
test('unavailable saved EQ library shows fixed repair guidance and refuses direct write handlers', async () => {
  const h = await appHarness();
  // Even a retained saved selection cannot bypass the independent write guard.
  h.setState(soundState(editor({revision: revisionID('e'), library_unavailable: true}))); await h.refresh(); await selectPreset(h, revisionID('c'));
  assert.equal(h.ids.get('dsp-save-hint').textContent, eqLibraryWarning);
  assert.equal(h.ids.get('dsp-current').textContent, 'Global · Stereo'); assert.equal(h.ids.get('connection-label').textContent, 'Connected');
  for (const mode of ['save', 'rename', 'delete']) {
    assert.equal(h.ids.get('dsp-' + mode).disabled, true);
    await h.ids.get('dsp-' + mode).fire('click'); assert.equal(h.ids.get('dsp-dialog').open, false);
    await h.ids.get('dsp-preset-form').fire('submit');
  }
  assert.equal(h.posts().length, 0);
  h.setState(soundState(editor({revision: revisionID('f'), presets: []}))); await h.refresh();
  assert.equal(h.ids.get('dsp-save').disabled, false); assert.doesNotMatch(h.ids.get('dsp-save-hint').textContent, /could not be loaded/);
});
test('unavailable saved EQ library leaves builtin Load, bands, balance and bypass usable', async () => {
  const h = await appHarness();
  let active = editor({revision: revisionID('e'), library_unavailable: true, presets: editor().presets.filter(preset => preset.kind === 'builtin')});
  h.setState(soundState(active, {eq_enabled: true})); await h.refresh();
  for (const id of ['dsp-preset', 'dsp-a_gain', 'balance', 'balance-submit', 'eq', 'stop']) assert.equal(h.ids.get(id).disabled, false, id);
  await selectPreset(h, revisionID('b')); assert.equal(h.ids.get('dsp-load').disabled, false); await h.ids.get('dsp-load').fire('click');
  h.ids.get('balance').value = '-0.35'; await h.ids.get('balance').fire('input'); await h.ids.get('balance-form').fire('submit');
  await h.ids.get('eq').fire('click');
  h.setPost(body => {active = {...active, revision: revisionID('f'), bands: body.bands}; h.setState(soundState(active, {eq_enabled: true})); return {request_acknowledged: true};});
  await editBand(h, 'a_gain', '2.5'); assert.equal(h.ids.get('dsp-apply').disabled, false); assert.equal(h.ids.get('dsp-save-hint').textContent, eqLibraryWarning);
  await h.ids.get('dsp-band-form').fire('submit');
  assert.deepEqual(h.posts().slice(0, 3), [{action: 'dsp_preset_load', expected_dsp: revisionID('e'), preset_id: revisionID('b')}, {action: 'balance', value: -0.35}, {action: 'eq', enabled: false}]);
  assert.equal(h.posts()[3].action, 'dsp_bands'); assert.equal(h.posts()[3].expected_dsp, revisionID('e')); assert.equal(h.posts()[3].bands[1].value, 2.5);
  assert.equal(h.ids.get('dsp-draft-label').textContent, 'Confirmed values'); assert.equal(h.ids.get('dsp-save').disabled, true); assert.equal(h.ids.get('dsp-save-hint').textContent, eqLibraryWarning);
});
test('saved EQ library degradation stales an open band draft until explicit discard and reload', async () => {
  const h = await appHarness(); await editBand(h, 'a_gain', '4');
  h.setState(soundState(editor({revision: revisionID('e'), library_unavailable: true, presets: [editor().presets[0]]}))); await h.refresh();
  assert.equal(h.ids.get('dsp-a_gain').value, '4'); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.match(h.ids.get('dsp-editor-message').textContent, /Discard and reload/); assert.equal(h.ids.get('dsp-save-hint').textContent, eqLibraryWarning);
  await h.ids.get('dsp-band-form').fire('submit'); assert.equal(h.posts().length, 0);
  await h.ids.get('dsp-discard').fire('click'); await editBand(h, 'a_gain', '2');
  assert.equal(h.ids.get('dsp-apply').disabled, false); assert.equal(h.ids.get('dsp-save').disabled, true);
});
test('saved EQ library degradation blocks open Save, Rename and Delete dialogs across recovery', async () => {
  for (const mode of ['save', 'rename', 'delete']) {
    const h = await appHarness(); await selectPreset(h, revisionID('d')); await h.ids.get('dsp-' + mode).fire('click');
    h.ids.get('dsp-name').value = 'Keep local name'; h.ids.get('dsp-shared-confirm').checked = true;
    h.setState(soundState(editor({revision: revisionID('e'), library_unavailable: true, presets: [editor().presets[0]]}))); await h.refresh();
    assert.equal(h.ids.get('dsp-dialog').open, true); assert.equal(h.ids.get('dsp-name').value, 'Keep local name'); assert.equal(h.ids.get('dsp-shared-confirm').checked, true);
    assert.equal(h.ids.get('dsp-preset-submit').disabled, true); assert.equal(h.ids.get('dsp-name').disabled, true); assert.equal(h.ids.get('dsp-shared-confirm').disabled, true); assert.equal(h.ids.get('dsp-dialog-status').textContent, eqLibraryWarning);
    await h.ids.get('dsp-preset-form').fire('submit'); await h.refresh(); assert.equal(h.posts().length, 0);
    h.setState(soundState(editor({revision: revisionID('f')}))); await h.refresh();
    assert.equal(h.ids.get('dsp-preset-submit').disabled, true); assert.match(h.ids.get('dsp-dialog-status').textContent, /Close this dialog/);
    await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 0);
    await h.ids.get('dsp-cancel').fire('click'); await selectPreset(h, revisionID('d')); await h.ids.get('dsp-' + mode).fire('click');
    assert.equal(h.ids.get('dsp-preset-submit').disabled, false); assert.equal(h.ids.get('dsp-dialog-status').textContent, '');
  }
});
test('Delete ACK followed by unavailable EQ library readback never verifies an absent saved record', async () => {
  const h = await appHarness(); await selectPreset(h, revisionID('c')); await h.ids.get('dsp-delete').fire('click');
  h.setPost(() => {h.setState(soundState(editor({revision: revisionID('e'), library_unavailable: true, presets: [editor().presets[0]]}))); return {request_acknowledged: true};});
  await h.ids.get('dsp-preset-form').fire('submit');
  assert.equal(h.ids.get('dsp-dialog').open, true); assert.equal(h.ids.get('dsp-preset-submit').disabled, true); assert.match(h.ids.get('dsp-dialog-status').textContent, /saved change has not been verified/); assert.equal(h.ids.get('dsp-save-hint').textContent, eqLibraryWarning);
  assert.ok(h.ids.get('dsp-dialog-status').textContent.endsWith(eqLibraryWarning));
  await h.ids.get('dsp-preset-form').fire('submit'); await h.refresh(); assert.equal(h.posts().length, 1);
});
test('saved EQ library guidance preserves a lost preset response and never retries it', async () => {
  const h = await appHarness(); await h.ids.get('dsp-save').fire('click'); h.ids.get('dsp-name').value = 'Uncertain curve';
  h.setPost(() => {h.setState(soundState(editor({revision: revisionID('e'), library_unavailable: true, presets: [editor().presets[0]]}))); throw new Error('lost response');});
  await h.ids.get('dsp-preset-form').fire('submit'); await h.refresh();
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.match(h.ids.get('dsp-dialog-status').textContent, /^UNKNOWN/); assert.ok(h.ids.get('dsp-dialog-status').textContent.endsWith(eqLibraryWarning));
  assert.equal(h.ids.get('dsp-preset-submit').disabled, true); await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 1);
});
test('app shows 40 confirmed fields, preserves local draft during polls and discards without sending', async () => {
  const h = await appHarness(); assert.equal(DSP_KEYS.filter(key => h.ids.has('dsp-' + key)).length, 40); assert.equal(h.ids.get('dsp-a_freq').value, '1000');
  await editBand(h, 'a_gain', '3.25'); await h.refresh(); assert.equal(h.ids.get('dsp-a_gain').value, '3.25'); assert.equal(h.ids.get('dsp-apply').disabled, false); assert.equal(h.ids.get('dsp-save').disabled, true); assert.match(h.ids.get('dsp-save-hint').textContent, /never saves unsent/);
  await h.ids.get('dsp-save').fire('click'); assert.equal(h.ids.get('dsp-dialog').open, false);
  await h.ids.get('dsp-discard').fire('click'); assert.equal(h.ids.get('dsp-a_gain').value, '0'); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.equal(h.ids.get('dsp-save').disabled, false); assert.equal(h.posts().length, 0);
});
test('app refuses incomplete and out-of-range bands even if submit is invoked directly', async () => {
  const h = await appHarness();
  for (const [key, value] of [['a_gain', ''], ['a_freq', '9'], ['a_q', '31'], ['a_mode', '4']]) {
    await editBand(h, key, value); assert.equal(h.ids.get('dsp-apply').disabled, true); await h.ids.get('dsp-band-form').fire('submit'); assert.equal(h.posts().length, 0); await h.ids.get('dsp-discard').fire('click');
  }
});
test('app submits all forty numeric bands once, preserves bypass, and uses readback before clearing draft', async () => {
  const h = await appHarness(), pending = deferred(); let sent;
  h.setPost(body => {sent = body; return pending.promise;}); await editBand(h, 'a_gain', '3.25'); await editBand(h, 'j_mode', '-1');
  const saving = h.ids.get('dsp-band-form').fire('submit'); await h.settle();
  assert.equal(h.ids.get('dsp-a_gain').disabled, true); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.equal(h.ids.get('stop').disabled, false); assert.match(h.ids.get('dsp-draft-label').textContent, /Submitted draft/);
  await h.ids.get('dsp-band-form').fire('submit'); assert.equal(h.posts().length, 1);
  assert.equal(sent.expected_dsp, revisionID('a')); assert.equal(sent.bands.length, 40); assert.equal(sent.bands[1].value, 3.25); assert.equal(sent.bands[39].value, -1); assert.ok(sent.bands.every(band => typeof band.value === 'number')); assert.deepEqual(Object.keys(sent).sort(), ['action', 'bands', 'expected_dsp']);
  h.setState(soundState(editor({revision: revisionID('e'), bands: sent.bands}))); pending.resolve({request_acknowledged: true}); await saving;
  assert.equal(h.ids.get('dsp-a_gain').value, '3.25'); assert.equal(h.ids.get('dsp-draft-label').textContent, 'Confirmed values'); assert.equal(h.ids.get('dsp-save').disabled, false); assert.match(h.ids.get('eq-current').textContent, /Bypassed/);
});
test('Stop remains recoverable during an in-flight DSP edit without enabling duplicate edits', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(body => body.action === 'dsp_bands' ? pending.promise : {request_acknowledged: true});
  await editBand(h, 'a_gain', '2'); const saving = h.ids.get('dsp-band-form').fire('submit'); await h.settle(); await h.ids.get('stop').fire('click');
  assert.deepEqual(h.posts().map(body => body.action), ['dsp_bands', 'stop']); assert.equal(h.ids.get('dsp-apply').disabled, true);
  pending.resolve({request_acknowledged: true}); await saving; assert.match(h.ids.get('notice-text').textContent, /^Stop:/); assert.equal(h.ids.get('dsp-apply').disabled, true);
});
test('ACK with pending readback retains submitted draft without claiming current bands, then resolves on confirmed readback', async () => {
  const h = await appHarness(); let submitted;
  h.setPost(body => {submitted = body.bands; h.setState(soundState(null, {status: 'PENDING', balance: null, eq_enabled: null})); return {request_acknowledged: true};});
  await editBand(h, 'b_gain', '-2'); await h.ids.get('dsp-band-form').fire('submit');
  assert.equal(h.ids.get('dsp-current').textContent, 'PENDING'); assert.equal(h.ids.get('dsp-draft-label').textContent, 'Local draft'); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.equal(h.ids.get('dsp-save').disabled, true); assert.equal(h.ids.get('stop').disabled, false);
  h.setState(soundState(editor({revision: revisionID('e'), bands: submitted}))); await h.refresh(); assert.equal(h.ids.get('dsp-draft-label').textContent, 'Confirmed values'); assert.equal(h.ids.get('dsp-save').disabled, false); assert.equal(h.posts().length, 1);
});
test('unconfirmed sound hides clean band fields and never presents remembered values as current', async () => {
  const h = await appHarness(); h.setState(soundState(null, {status: 'UNKNOWN', balance: null, eq_enabled: null})); await h.refresh();
  assert.equal(h.ids.get('dsp-band-form').hidden, true); assert.equal(h.ids.get('dsp-current').textContent, 'UNKNOWN'); assert.equal(h.ids.get('dsp-save').disabled, true);
});
test('external DSP changes and restart preserve draft but require discard/reload before another apply', async () => {
  for (const restart of [false, true]) {
    const h = await appHarness(); await editBand(h, 'a_gain', '4');
    const next = soundState(editor({revision: restart ? revisionID('a') : revisionID('e')})); if (restart) {next.session_id = 'restarted'; h.setFavorites(favorites([], 'restarted'));}
    h.setState(next); await h.refresh(); assert.equal(h.ids.get('dsp-a_gain').value, '4'); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.match(h.ids.get('dsp-editor-message').textContent, /restarted/);
    await h.ids.get('dsp-band-form').fire('submit'); assert.equal(h.posts().length, 0); await h.ids.get('dsp-discard').fire('click'); assert.equal(h.ids.get('dsp-a_gain').value, '0');
    await editBand(h, 'a_gain', '1'); assert.equal(h.ids.get('dsp-apply').disabled, false);
  }
});
test('stale DSP rejection sends no retry and keeps the draft blocked until explicit reload', async () => {
  const h = await appHarness(); h.setPost(() => ({__status: 409, outcome: 'REJECTED', error: 'stale_dsp', message: 'Sound state changed. Review it first.'}));
  await editBand(h, 'a_gain', '2'); await h.ids.get('dsp-band-form').fire('submit'); await h.refresh();
  assert.equal(h.posts().length, 1); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.equal(h.ids.get('dsp-a_gain').value, '2'); assert.equal(h.ids.get('dsp-discard').textContent, 'Discard & reload');
});
test('lost DSP response preserves unknown outcome and requires review even when matching bands appear', async () => {
  const h = await appHarness(); h.setPost(body => {h.setState(soundState(editor({revision: revisionID('e'), bands: body.bands}))); throw new Error('lost response');});
  await editBand(h, 'a_gain', '2'); await h.ids.get('dsp-band-form').fire('submit'); await h.refresh();
  assert.equal(h.posts().length, 1); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.equal(h.ids.get('dsp-apply').disabled, true); assert.equal(h.ids.get('dsp-save').disabled, true); assert.match(h.ids.get('dsp-editor-message').textContent, /No retry/);
  await h.ids.get('dsp-discard').fire('click'); assert.equal(h.ids.get('dsp-a_gain').value, '2'); assert.equal(h.ids.get('dsp-draft-label').textContent, 'Confirmed values');
});
test('built-in presets load with opaque identity and offer no rename or delete', async () => {
  const h = await appHarness(); await selectPreset(h, revisionID('b')); assert.equal(h.ids.get('dsp-load').disabled, false); assert.equal(h.ids.get('dsp-rename').disabled, true); assert.equal(h.ids.get('dsp-delete').disabled, true);
  await h.ids.get('dsp-rename').fire('click'); await h.ids.get('dsp-delete').fire('click'); assert.equal(h.ids.get('dsp-dialog').open, false);
  await h.ids.get('dsp-load').fire('click'); assert.deepEqual(h.posts(), [{action: 'dsp_preset_load', expected_dsp: revisionID('a'), preset_id: revisionID('b')}]);
});
test('preset save uses only confirmed active curve, trims name and verifies fresh saved record', async () => {
  const h = await appHarness(); h.setPost(body => {h.setState(soundState(editor({revision: revisionID('e'), presets: [...editor().presets, {id: revisionID('f'), name: body.name, kind: 'saved', shared: false}]}))); return {request_acknowledged: true};});
  await h.ids.get('dsp-save').fire('click'); h.ids.get('dsp-name').value = '  New curve  '; await h.refresh(); assert.equal(h.ids.get('dsp-name').value, '  New curve  ');
  await h.ids.get('dsp-preset-form').fire('submit'); assert.deepEqual(h.posts(), [{action: 'dsp_preset_save', expected_dsp: revisionID('a'), name: 'New curve'}]); assert.equal(h.ids.get('dsp-dialog').open, false); assert.equal(h.ids.get('dsp-name').value, '');
});
test('preset names enforce UTF-8 byte limit locally', async () => {
  const h = await appHarness(); await h.ids.get('dsp-save').fire('click'); h.ids.get('dsp-name').value = '🎵'.repeat(65); await h.ids.get('dsp-preset-form').fire('submit');
  assert.equal(h.posts().length, 0); assert.match(h.ids.get('dsp-dialog-status').textContent, /UTF-8 bytes/);
});
test('preset dialog freezes revision and rejects changes or restarts instead of silently retargeting', async () => {
  for (const restart of [false, true]) {
    const h = await appHarness(); await selectPreset(h, revisionID('c')); await h.ids.get('dsp-rename').fire('click'); h.ids.get('dsp-name').value = 'Changed';
    const next = soundState(editor({revision: restart ? revisionID('a') : revisionID('e')})); if (restart) {next.session_id = 'restarted'; h.setFavorites(favorites([], 'restarted'));} h.setState(next); await h.refresh();
    assert.equal(h.ids.get('dsp-name').value, 'Changed'); assert.equal(h.ids.get('dsp-preset-submit').disabled, true); await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 0);
  }
});
test('shared preset rename and delete require explicit all-context confirmation and preserve it across polls', async () => {
  for (const mode of ['rename', 'delete']) {
    const h = await appHarness(); await selectPreset(h, revisionID('d')); await h.ids.get('dsp-' + mode).fire('click'); h.ids.get('dsp-name').value = 'Renamed';
    assert.equal(h.ids.get('dsp-shared-field').hidden, false); assert.match(h.ids.get('dsp-shared-warning').textContent, /ALL stored contexts/);
    await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 0); h.ids.get('dsp-shared-confirm').checked = true; await h.refresh(); assert.equal(h.ids.get('dsp-shared-confirm').checked, true);
    h.setPost(() => {const presets = editor().presets.filter(item => item.id !== revisionID('d')); if (mode === 'rename') presets.push({id: revisionID('f'), name: 'Renamed', kind: 'saved', shared: true}); h.setState(soundState(editor({revision: revisionID('e'), presets}))); return {request_acknowledged: true};});
    await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].preset_id, revisionID('d')); assert.equal(h.ids.get('dsp-dialog').open, false); assert.equal(h.ids.get('dsp-shared-confirm').checked, false);
  }
});
test('deleting a saved builtin-name shadow verifies saved absence even when builtin with same ID remains', async () => {
  const h = await appHarness(); const presets = [{id: revisionID('b'), name: 'Flat', kind: 'saved', shared: false}]; h.setState(soundState(editor({presets}))); await h.refresh(); await selectPreset(h, revisionID('b')); await h.ids.get('dsp-delete').fire('click');
  h.setPost(() => {h.setState(soundState(editor({revision: revisionID('e'), presets: [{...presets[0], kind: 'builtin'}]}))); return {request_acknowledged: true};}); await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.ids.get('dsp-dialog').open, false);
});
test('preset request is single-shot and closing or Escape clears form without hiding a lost response', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise); await h.ids.get('dsp-save').fire('click'); h.ids.get('dsp-name').value = 'New';
  const saving = h.ids.get('dsp-preset-form').fire('submit'); await h.settle(); await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 1); assert.equal(h.ids.get('stop').disabled, false); assert.equal(h.ids.get('dsp-name').disabled, true);
  await h.ids.get('dsp-close').fire('click'); assert.equal(h.ids.get('dsp-name').value, ''); pending.reject(new Error('lost')); await saving; assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.equal(h.ids.get('dsp-dialog').open, false);
  await h.ids.get('dsp-save').fire('click'); h.ids.get('dsp-name').value = 'Clear me'; h.ids.get('dsp-dialog').open = false; await h.ids.get('dsp-dialog').fire('close'); assert.equal(h.ids.get('dsp-name').value, '');
});
test('preset conflict permits explicit name correction while unknown save blocks duplicate submission', async () => {
  for (const unknown of [false, true]) {
    const h = await appHarness(); h.setPost(() => unknown ? {__status: 502, outcome: 'UNKNOWN', error: 'command_failed', effect: 'LIBRARY_COMMITTED_DURABILITY_UNKNOWN', message: 'Save durability is unknown.'} : {__status: 409, outcome: 'REJECTED', error: 'preset_conflict', message: 'Name already exists.'});
    await h.ids.get('dsp-save').fire('click'); h.ids.get('dsp-name').value = 'Already there'; await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.ids.get('dsp-dialog').open, true); assert.equal(h.ids.get('dsp-preset-submit').disabled, unknown);
    if (unknown) {await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 1);} else {h.ids.get('dsp-name').value = 'Different'; await h.ids.get('dsp-preset-form').fire('submit'); assert.equal(h.posts().length, 2);}
  }
});

/* Playlist tests use synthetic sources and a DOM double, not browser acceptance. */
const playlistJSON = (changes = {}) => JSON.stringify({version: 1, name: 'Authored queue', index: 1, mode: 'repeat-all', urls: ['https://example.test/a?token=private'], ...changes});
function localFile(text, name = 'private-source.json') {
  const bytes = new TextEncoder().encode(text);
  return {name, size: bytes.length, arrayBuffer: async () => bytes.buffer};
}
async function sourceFormat(h, format) { h.ids.get('playlist-format').value = format; await h.ids.get('playlist-format').fire('change'); }
async function sourceURLs(h, text) { h.ids.get('playlist-urls').value = text; await h.ids.get('playlist-urls').fire('input'); }
async function chooseFile(h, file) { h.ids.get('playlist-file').files = [file]; h.ids.get('playlist-file').value = 'C:\\fakepath\\' + file.name; await h.ids.get('playlist-file').fire('change'); }
async function confirmPlaylist(h) { h.ids.get('playlist-confirm').checked = true; await h.ids.get('playlist-confirm').fire('input'); }
function playlistRows(h, action) { return h.ids.get('queue-list').querySelectorAll('button').filter(button => button.dataset.queueAction === action); }
function safeMessages(h) { return ['notice-text', 'playlist-dialog-status', 'playlist-preview', 'playlist-size'].map(id => h.ids.get(id).textContent).join(' '); }
test('playlist actions fail closed without explicit supported capabilities or confirmed stopped state', async () => {
  assert.equal(playlistAvailable(state(), 'playlist_replace'), true);
  for (const transport of [{phase: 'PLAYING', status: 'CONFIRMED'}, {phase: 'PAUSED', status: 'CONFIRMED'}, {phase: 'STOPPED', status: 'PENDING'}, {phase: 'STOPPED', status: 'ERROR'}]) {
    const current = state({transport: {...state().transport, ...transport}}), h = harness(); h.setState(current); await h.controller.refresh();
    for (const action of ['playlist_replace', 'playlist_append', 'playlist_remove', 'playlist_move']) assert.equal(h.controller.canMutate(action), false);
    assert.equal(h.controller.canMutate('stop'), true);
  }
  const noFlags = state(); for (const key of ['playlist_replace', 'playlist_append', 'playlist_remove', 'playlist_move']) delete noFlags.actions[key];
  assert.doesNotThrow(() => validateState(noFlags)); assert.equal(playlistAvailable(noFlags, 'playlist_replace'), false);
  for (const reason of ['empty_queue', 'unsupported_queue', undefined]) {
    const current = state({queue: {...state().queue, playlist_edit_reason: reason}});
    assert.equal(playlistAvailable(current, 'playlist_append'), false); assert.equal(playlistAvailable(current, 'playlist_remove'), false);
  }
  assert.throws(() => validateState(state({queue: {...state().queue, playlist_edit_reason: 'maybe'}})));
  assert.throws(() => validateState(state({actions: {...state().actions, playlist_append: 'yes'}})));
});
test('pasted URL lists preserve original URL bytes and reject ambiguous lines', () => {
  const url = 'HTTP://Example.test:80/a%2fb?sig=One%2BTwo#fragment';
  assert.deepEqual(pastedURLs(url + '\r\n\nhttps://example.test/b\n'), [url, 'https://example.test/b']);
  for (const text of ['', ' ' + url, url + ' ', url + '\r', '#EXTM3U\n' + url, 'https://user:secret@example.test/a', 'file:///secret', '\ufeff' + url, ('https://example.test/a\n').repeat(1001)]) assert.equal(pastedURLs(text), null);
  assert.deepEqual(playlistPreview('json', playlistJSON({index: 2, urls: [url, url]}), {}), {count: 2, index: 2, mode: 'repeat-all'});
  assert.equal(playlistPreview('json', '<script>secret</script>', {}), null);
});
test('local file decode is fatal UTF-8, preserves BOM and never exposes a filename or source in errors', async () => {
  const draft = new PlaylistDraft(state(), 'playlist_replace'); draft.setSource('json');
  const text = '\ufeff' + playlistJSON(); await draft.readFile(localFile(text)); assert.equal(draft.text, text); assert.equal(draft.error, '');
  await draft.readFile({name: 'secret-source.json', size: 2, arrayBuffer: async () => Uint8Array.from([0xc3, 0x28]).buffer});
  assert.equal(draft.text, ''); assert.match(draft.error, /UTF-8/); assert.doesNotMatch(draft.error, /secret-source/);
  let reads = 0; await draft.readFile({name: 'secret-source.json', size: MAX_REQUEST_BYTES + 1, arrayBuffer: async () => {reads += 1;}}); assert.equal(reads, 0);
});
test('cancel, newer source and late file reads cannot restore an obsolete private draft', async () => {
  for (const event of ['cancel', 'newer', 'format']) {
    const draft = new PlaylistDraft(state(), 'playlist_replace'), old = deferred(); draft.setSource('json');
    const reading = draft.readFile({name: 'obsolete.json', size: 3, arrayBuffer: () => old.promise});
    if (event === 'cancel') draft.cancel();
    else if (event === 'newer') await draft.readFile(localFile('new'));
    else draft.setSource('urls', 'https://example.test/new');
    old.resolve(new TextEncoder().encode('old').buffer); await reading;
    assert.equal(draft.text, event === 'cancel' ? '' : event === 'newer' ? 'new' : 'https://example.test/new'); assert.equal(draft.reading, false);
  }
});
test('playlist draft identity is immutable and staleness is sticky across polling reversions', () => {
  for (const next of [state({generation: 8}), state({session_id: 'restarted'}), state({transport: {...state().transport, phase: 'PLAYING'}}), state({actions: {...state().actions, playlist_append: false}})]) {
    const draft = new PlaylistDraft(state(), 'playlist_append'); draft.setSource('urls', 'https://example.test/private'); draft.observe(next, true); draft.observe(state(), true);
    assert.equal(draft.stale, true); assert.equal(draft.canSubmit(state(), true), false); assert.equal(draft.generation, 7); assert.equal(draft.session, 'session-one'); assert.equal(draft.text, 'https://example.test/private');
  }
});
test('full UTF-8 JSON envelope is bounded, including escaping, and checked before requests', async () => {
  const body = {action: 'playlist_replace', expected_generation: 7, source: {format: 'json', text: ''}};
  const base = requestBytes(body); body.source.text = 'a'.repeat(MAX_REQUEST_BYTES - base);
  assert.equal(requestBytes(body), MAX_REQUEST_BYTES);
  const h = harness(); await h.controller.refresh(); await h.controller.mutate(body, 'Replace queue'); assert.equal(h.posts().length, 1);
  body.source.text += 'é'; assert.equal(requestBytes(body), MAX_REQUEST_BYTES + 2); assert.equal((await h.controller.mutate(body, 'Replace queue')).kind, 'blocked'); assert.equal(h.posts().length, 1);
  body.source.text = '\\"\n'.repeat(7000); assert.ok(body.source.text.length < MAX_REQUEST_BYTES); assert.ok(requestBytes(body) > MAX_REQUEST_BYTES);
  assert.equal((await h.controller.mutate(body, 'Replace queue')).kind, 'blocked'); assert.equal(h.posts().length, 1);
});
test('playlist replace needs explicit confirmation and preserves exact pasted URL bytes in strict source envelope', async () => {
  const h = await appHarness(), url = 'HTTP://Example.test:80/a%2fb?sig=One%2BTwo';
  await h.ids.get('playlist-load').fire('click'); await sourceFormat(h, 'urls'); await sourceURLs(h, url + '\r\nhttps://example.test/b\n');
  h.ids.get('playlist-name').value = 'My explicit queue'; h.ids.get('playlist-index').value = '2'; h.ids.get('playlist-mode').value = 'shuffle-once';
  await h.refresh(); assert.equal(h.ids.get('playlist-urls').value, url + '\r\nhttps://example.test/b\n');
  assert.equal(h.ids.get('playlist-submit').disabled, true); await h.ids.get('playlist-form').fire('submit'); assert.equal(h.posts().length, 0);
  await confirmPlaylist(h); assert.equal(h.ids.get('playlist-submit').disabled, false);
  h.setPost(() => {h.setState(state({generation: 8})); return {request_acknowledged: true};}); await h.ids.get('playlist-form').fire('submit');
  assert.deepEqual(h.posts(), [{action: 'playlist_replace', expected_generation: 7, source: {format: 'urls', urls: [url, 'https://example.test/b'], name: 'My explicit queue', index: 2, mode: 'shuffle-once'}}]);
  assert.equal(h.ids.get('playlist-dialog').open, false); assert.equal(h.ids.get('playlist-urls').value, ''); assert.equal(h.ids.get('playlist-name').value, ''); assert.doesNotMatch(safeMessages(h), /One%2BTwo/);
});
test('JSON and M3U send original local text including BOM; only safe summaries enter messages', async () => {
  for (const format of ['json', 'm3u']) {
    const h = await appHarness(), text = format === 'json' ? '\ufeff' + playlistJSON() : '\ufeff#EXTM3U\r\n#EXTINF:123,private-title\r\nhttps://example.test/a?token=private\r\n';
    await h.ids.get('playlist-load').fire('click'); await sourceFormat(h, format); await chooseFile(h, localFile(text, 'private-filename.' + format)); await confirmPlaylist(h);
    assert.equal(h.ids.get('playlist-file').value, ''); assert.equal(h.ids.get('playlist-file').files.length, 0); assert.equal(h.ids.get('playlist-submit').disabled, false); assert.doesNotMatch(safeMessages(h), /private/);
    await h.ids.get('playlist-form').fire('submit'); const body = h.posts()[0]; assert.equal(body.source.text, text); assert.equal(body.source.format, format);
    assert.deepEqual(Object.keys(body.source).sort(), format === 'json' ? ['format', 'text'] : ['format', 'index', 'mode', 'name', 'text']);
  }
});
test('file failures and escaped envelope overflow are local generic errors with no request', async () => {
  for (const file of [{name: 'private-invalid.json', size: 2, arrayBuffer: async () => Uint8Array.from([0xc3, 0x28]).buffer}, localFile('"'.repeat(20000), 'private-large.json')]) {
    const h = await appHarness(); await h.ids.get('playlist-load').fire('click'); await chooseFile(h, file); await confirmPlaylist(h); await h.ids.get('playlist-form').fire('submit');
    assert.equal(h.posts().length, 0); assert.equal(h.ids.get('playlist-submit').disabled, true); assert.match(h.ids.get('playlist-dialog-status').textContent, /UTF-8|32,768/); assert.doesNotMatch(safeMessages(h), /private/);
  }
});
test('native Escape and Cancel scrub source fields; late file reads cannot reopen or populate a newer dialog', async () => {
  for (const close of ['cancel', 'playlist-cancel']) {
    const h = await appHarness(), late = deferred(); await h.ids.get('playlist-load').fire('click');
    const reading = chooseFile(h, {name: 'obsolete-private.json', size: 3, arrayBuffer: () => late.promise}); await h.settle();
    if (close === 'cancel') await h.ids.get('playlist-dialog').fire('cancel'); else await h.ids.get(close).fire('click');
    assert.equal(h.ids.get('playlist-dialog').open, false); assert.equal(h.ids.get('playlist-file').value, ''); assert.equal(h.ids.get('playlist-urls').value, ''); assert.equal(h.ids.get('playlist-name').value, '');
    await h.ids.get('playlist-load').fire('click'); await sourceFormat(h, 'urls'); await sourceURLs(h, 'https://example.test/new');
    late.resolve(new TextEncoder().encode('old').buffer); await reading; assert.equal(h.ids.get('playlist-urls').value, 'https://example.test/new'); assert.match(h.ids.get('playlist-preview').textContent, /^URLS/); assert.equal(h.posts().length, 0);
  }
});
test('playlist draft is preserved but cannot be silently rebased after a changed queue or same-generation restart', async () => {
  for (const change of [{generation: 8}, {session_id: 'restarted'}, {actions: {...state().actions, playlist_replace: false}}]) {
    const h = await appHarness(); await h.ids.get('playlist-load').fire('click'); await sourceFormat(h, 'urls'); await sourceURLs(h, 'https://example.test/private'); await confirmPlaylist(h);
    h.setState(state(change)); await h.refresh(); h.setState(state()); await h.refresh();
    assert.equal(h.ids.get('playlist-urls').value, 'https://example.test/private'); assert.equal(h.ids.get('playlist-submit').disabled, true); await h.ids.get('playlist-form').fire('submit'); assert.equal(h.posts().length, 0); assert.match(h.ids.get('playlist-dialog-status').textContent, /Cancel and review/);
  }
});
test('duplicate playlist submit is single-shot; lost response stays UNKNOWN even after matching stopped readback', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
  await h.ids.get('playlist-add').fire('click'); await sourceURLs(h, 'https://example.test/private?secret=value');
  const saving = h.ids.get('playlist-form').fire('submit'); await h.settle(); await h.ids.get('playlist-form').fire('submit'); assert.equal(h.posts().length, 1); assert.equal(h.ids.get('stop').disabled, false);
  h.setState(state({generation: 8})); pending.reject(new Error('response lost private?secret=value')); await saving; await h.refresh();
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.equal(h.ids.get('playlist-submit').disabled, true); assert.match(safeMessages(h), /No retry was sent/); assert.doesNotMatch(safeMessages(h), /secret=value/);
  await h.ids.get('playlist-form').fire('submit'); assert.equal(h.posts().length, 1);
});
test('Stop remains separate and recoverable during playlist requests; closing does not hide an unknown outcome', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(body => body.action === 'playlist_append' ? pending.promise : {request_acknowledged: true});
  await h.ids.get('playlist-add').fire('click'); await sourceURLs(h, 'https://example.test/a'); const saving = h.ids.get('playlist-form').fire('submit'); await h.settle();
  await h.ids.get('stop').fire('click'); await h.ids.get('playlist-close').fire('click'); pending.reject(new Error('lost')); await saving;
  assert.deepEqual(h.posts().map(body => body.action), ['playlist_append', 'stop']); assert.equal(h.ids.get('playlist-dialog').open, false); assert.match(h.ids.get('notice-text').textContent, /^Stop:/); assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown/);
});
test('server rejection content cannot disclose playlist input and does not permit duplicate submission', async () => {
  const h = await appHarness(); h.setPost(() => ({__status: 400, outcome: 'REJECTED', error: 'invalid_playlist', message: 'Bad secret-file.json https://example.test/?private=value'}));
  await h.ids.get('playlist-add').fire('click'); await sourceURLs(h, 'https://example.test/a'); await h.ids.get('playlist-form').fire('submit'); await h.ids.get('playlist-form').fire('submit');
  assert.equal(h.posts().length, 1); assert.doesNotMatch(safeMessages(h), /secret-file|private=value/); assert.match(safeMessages(h), /Playlist request rejected/);
});
test('unsupported queues and paused playback disable editing; Stop never chains a pending import', async () => {
  const h = await appHarness(); h.setState(state({queue: {...state().queue, playlist_edit_reason: 'unsupported_queue'}})); await h.refresh();
  assert.equal(h.ids.get('playlist-add').disabled, true); assert.equal(h.ids.get('playlist-load').disabled, false); assert.ok(playlistRows(h, 'remove').every(button => button.disabled)); assert.match(h.ids.get('playlist-availability').textContent, /cannot be edited/);
  h.setState(state({transport: {...state().transport, phase: 'PAUSED'}})); await h.refresh(); assert.equal(h.ids.get('playlist-load').disabled, true); await h.ids.get('playlist-load').fire('click'); assert.equal(h.ids.get('playlist-dialog').open, false);
  h.setPost(() => {h.setState(state({generation: 8})); return {request_acknowledged: true};}); await h.ids.get('stop').fire('click'); assert.deepEqual(h.posts(), [{action: 'stop'}]); assert.equal(h.ids.get('playlist-load').disabled, false); assert.equal(h.ids.get('playlist-dialog').open, false);
});
test('queue row move/remove capture displayed indices, enforce edges, and suppress repeated submission', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
  assert.equal(playlistRows(h, 'up')[0].disabled, true); assert.equal(playlistRows(h, 'down')[1].disabled, true); assert.equal(playlistRows(h, 'remove')[0].disabled, false);
  await playlistRows(h, 'up')[0].fire('click'); assert.equal(h.posts().length, 0);
  const moving = playlistRows(h, 'up')[1].fire('click'); await h.settle(); await playlistRows(h, 'up')[1].fire('click'); assert.deepEqual(h.posts(), [{action: 'playlist_move', expected_generation: 7, from_index: 2, to_index: 1}]);
  pending.resolve({request_acknowledged: true}); await moving; assert.equal(playlistRows(h, 'up')[1].disabled, true);
  const h2 = await appHarness(); await playlistRows(h2, 'remove')[1].fire('click'); assert.deepEqual(h2.posts(), [{action: 'playlist_remove', expected_generation: 7, index: 2}]);
});
test('old row actions refuse same-generation restarts, changed queues and unsupported-to-supported revalidation', async () => {
  for (const change of [{generation: 8}, {session_id: 'restarted'}]) {
    const h = await appHarness(), old = playlistRows(h, 'remove')[0]; h.setState(state(change)); await h.refresh(); await old.fire('click'); assert.equal(h.posts().length, 0);
  }
  const h = await appHarness(); h.setState(state({queue: {...state().queue, playlist_edit_reason: 'unsupported_queue'}})); await h.refresh();
  h.setState(state()); await h.refresh(); assert.ok(playlistRows(h, 'remove').every(button => button.disabled)); await playlistRows(h, 'remove')[0].fire('click'); assert.equal(h.posts().length, 0);
  h.setState(state({generation: 8})); await h.refresh(); assert.equal(playlistRows(h, 'remove')[0].disabled, false);
});
test('append, remove and move obey queue count bounds including direct event dispatch', async () => {
  for (const count of [0, 1, 1000]) {
    const h = await appHarness(), queue = {...state().queue, count, index: count ? 1 : 0, items: Array.from({length: count}, (_, offset) => ({...state().queue.items[0], index: offset + 1}))};
    h.setState(state({generation: 8, queue})); await h.refresh();
    if (count < 2) assert.ok(playlistRows(h, 'remove').every(button => button.disabled));
    if (count === 0 || count === 1000) { assert.equal(h.ids.get('playlist-add').disabled, true); await h.ids.get('playlist-add').fire('click'); assert.equal(h.ids.get('playlist-dialog').open, false); }
    assert.equal(h.posts().length, 0);
  }
});

test('inactive source settings are disabled and cannot block JSON or append through native form validation', async () => {
  const h = await appHarness(); await h.ids.get('playlist-load').fire('click');
  assert.equal(h.ids.get('playlist-index').disabled, true); assert.equal(h.ids.get('playlist-urls').disabled, true); assert.equal(h.ids.get('playlist-file').disabled, false);
  await sourceFormat(h, 'urls'); assert.equal(h.ids.get('playlist-index').disabled, false); assert.equal(h.ids.get('playlist-file').disabled, true); assert.equal(h.ids.get('playlist-urls').disabled, false);
  h.ids.get('playlist-index').value = '-1'; await sourceFormat(h, 'json'); assert.equal(h.ids.get('playlist-index').disabled, true);
  await h.ids.get('playlist-close').fire('click'); await h.ids.get('playlist-add').fire('click'); assert.equal(h.ids.get('playlist-format').disabled, true); assert.equal(h.ids.get('playlist-index').disabled, true); assert.equal(h.ids.get('playlist-confirm').disabled, true);
});
test('new file selection invalidates previous confirmation and only latest read may set source', async () => {
  const h = await appHarness(), old = deferred(); await h.ids.get('playlist-load').fire('click');
  const reading = chooseFile(h, {name: 'old.json', size: 3, arrayBuffer: () => old.promise}); await h.settle();
  await chooseFile(h, localFile(playlistJSON())); await confirmPlaylist(h); old.resolve(new TextEncoder().encode('old').buffer); await reading;
  await h.ids.get('playlist-form').fire('submit'); assert.equal(h.posts()[0].source.text, playlistJSON());
});

test('metadata-only row rebuilding cannot upgrade an unsupported queue identity into enabled edits', async () => {
  const h = await appHarness(); h.setState(state({generation: 8, queue: {...state().queue, playlist_edit_reason: 'unsupported_queue'}})); await h.refresh();
  const items = state().queue.items.map(item => ({...item, title: 'Updated title'}));
  h.setState(state({generation: 8, queue: {...state().queue, items}})); await h.refresh();
  assert.ok(playlistRows(h, 'remove').every(button => button.disabled)); assert.equal(h.ids.get('playlist-add').disabled, true); await h.ids.get('playlist-add').fire('click'); assert.equal(h.ids.get('playlist-dialog').open, false); await playlistRows(h, 'remove')[0].fire('click'); assert.equal(h.posts().length, 0);
});

test('detached play-row handlers cannot retarget after a generation change or renderer restart', async () => {
  for (const change of [{generation: 8}, {session_id: 'restarted'}]) {
    const h = await appHarness(), old = h.ids.get('queue-list').querySelectorAll('button')[0]; h.setState(state(change)); await h.refresh(); await old.fire('click'); assert.equal(h.posts().length, 0);
  }
});

test('pagehide closes and scrubs the private playlist draft before navigation caching', async () => {
  const h = await appHarness(); await h.ids.get('playlist-load').fire('click'); await sourceFormat(h, 'urls'); await sourceURLs(h, 'https://example.test/?private=value'); await confirmPlaylist(h);
  h.pagehide(); assert.equal(h.ids.get('playlist-dialog').open, false); assert.equal(h.ids.get('playlist-urls').value, ''); assert.equal(h.ids.get('playlist-name').value, ''); assert.equal(h.ids.get('playlist-confirm').checked, false); assert.equal(h.ids.get('playlist-file').files.length, 0); assert.doesNotMatch(safeMessages(h), /private=value/); assert.equal(h.posts().length, 0);
});

test('typed replacement names enforce the server UTF-8 byte bound without changing authored JSON', async () => {
  const h = await appHarness(); await h.ids.get('playlist-load').fire('click'); await sourceFormat(h, 'urls'); await sourceURLs(h, 'https://example.test/a');
  h.ids.get('playlist-name').value = '🎵'.repeat(65); await h.ids.get('playlist-name').fire('input'); await confirmPlaylist(h); await h.ids.get('playlist-form').fire('submit');
  assert.equal(h.posts().length, 0); assert.match(h.ids.get('playlist-dialog-status').textContent, /1–256 UTF-8 bytes/);
  h.ids.get('playlist-name').value = '🎵'.repeat(64); await h.ids.get('playlist-name').fire('input'); await confirmPlaylist(h); assert.equal(h.ids.get('playlist-submit').disabled, false);
});

test('a delayed close event from the old playlist dialog cannot clear its reopened draft', async () => {
  const h = await appHarness(), dialog = h.ids.get('playlist-dialog');
  // HTMLDialogElement.close queues its close event. Delay it explicitly,
  // rather than relying on the test double's synchronous close shortcut.
  dialog.close = () => {dialog.open = false;};
  await h.ids.get('playlist-add').fire('click');
  await sourceURLs(h, 'https://example.test/old?token=old');
  await h.ids.get('playlist-close').fire('click');
  assert.equal(h.ids.get('playlist-urls').value, '');
  await h.ids.get('playlist-add').fire('click');
  await sourceURLs(h, 'https://example.test/new?token=new');
  await dialog.fire('close');
  assert.equal(dialog.open, true);
  assert.equal(h.ids.get('playlist-urls').value, 'https://example.test/new?token=new');
  await h.ids.get('playlist-form').fire('submit');
  assert.equal(h.posts().length, 1);
  assert.deepEqual(h.posts()[0].urls, ['https://example.test/new?token=new']);
});

test('a delayed favorite close event cannot clear a reopened favorite draft', async () => {
  const h = await appHarness(), dialog = h.ids.get('favorite-dialog');
  dialog.close = () => {dialog.open = false;};
  await h.ids.get('add-favorite').fire('click');
  h.ids.get('favorite-name').value = 'Old';
  h.ids.get('favorite-url').value = 'https://example.test/old';
  await h.ids.get('favorite-close').fire('click');
  assert.equal(h.ids.get('favorite-url').value, '');
  await h.ids.get('add-favorite').fire('click');
  h.ids.get('favorite-name').value = 'New';
  h.ids.get('favorite-url').value = 'https://example.test/new';
  await dialog.fire('close');
  assert.equal(dialog.open, true);
  assert.equal(h.ids.get('favorite-name').value, 'New');
  assert.equal(h.ids.get('favorite-url').value, 'https://example.test/new');
  await h.ids.get('favorite-form').fire('submit');
  assert.equal(h.posts().length, 1);
  assert.equal(h.posts()[0].name, 'New');
});

test('a delayed preset close event cannot clear a reopened preset draft', async () => {
  const h = await appHarness(), dialog = h.ids.get('dsp-dialog');
  dialog.close = () => {dialog.open = false;};
  await h.ids.get('dsp-save').fire('click');
  h.ids.get('dsp-name').value = 'Old';
  await h.ids.get('dsp-close').fire('click');
  assert.equal(h.ids.get('dsp-name').value, '');
  await h.ids.get('dsp-save').fire('click');
  h.ids.get('dsp-name').value = 'New';
  await dialog.fire('close');
  assert.equal(dialog.open, true);
  assert.equal(h.ids.get('dsp-name').value, 'New');
  await h.ids.get('dsp-preset-form').fire('submit');
  assert.equal(h.posts().length, 1);
  assert.equal(h.posts()[0].name, 'New');
});


/* Synthetic page lifecycle events retain this document/JS instance. They check
 * cleanup and late continuations, not actual browser BFCache eligibility. */
const privateDialogs = [
  {dialog: 'favorite-dialog', open: 'add-favorite', form: 'favorite-form', name: 'favorite-name', status: 'favorite-dialog-status', action: 'favorite_add'},
  {dialog: 'dsp-dialog', open: 'dsp-save', form: 'dsp-preset-form', name: 'dsp-name', status: 'dsp-dialog-status', action: 'dsp_preset_save'},
  {dialog: 'playlist-dialog', open: 'playlist-add', form: 'playlist-form', name: 'playlist-name', status: 'playlist-dialog-status', action: 'playlist_append'}
];
async function privateDraft(h, kind, name) {
  await h.ids.get(kind.open).fire('click'); h.ids.get(kind.name).value = name;
  if (kind.action === 'favorite_add') h.ids.get('favorite-url').value = 'https://example.test/audio?signature=' + name;
  if (kind.action === 'playlist_append') await sourceURLs(h, 'https://example.test/audio?signature=' + name);
}
function assertPrivateDialogsCleared(h) {
  for (const kind of privateDialogs) {
    assert.equal(h.ids.get(kind.dialog).open, false, kind.dialog);
    assert.equal(h.ids.get(kind.name).value, '', kind.name);
    assert.equal(h.ids.get(kind.status).textContent, '', kind.status);
  }
  for (const id of ['favorite-url', 'playlist-urls', 'playlist-file']) assert.equal(h.ids.get(id).value, '', id);
  for (const id of ['favorite-delete-name', 'dsp-preset-name', 'playlist-preview', 'playlist-size']) assert.equal(h.ids.get(id).textContent, '', id);
  for (const id of ['dsp-shared-confirm', 'playlist-confirm']) assert.equal(h.ids.get(id).checked, false, id);
  assert.equal(h.ids.get('playlist-file').files.length, 0);
}
test('pagehide immediately scrubs every private dialog even when closed or awaiting queued close, and restoration cannot submit old intent', async () => {
  for (const persisted of [false, true]) for (const closed of [false, true]) {
    const h = await appHarness();
    for (const kind of privateDialogs) {
      // Native close is asynchronous and is a no-op on an already closed dialog.
      h.ids.get(kind.dialog).close = function () {this.open = false;};
      await privateDraft(h, kind, 'private-draft');
      h.ids.get(kind.dialog).open = !closed;
    }
    h.ids.get('favorite-delete-name').textContent = 'Private favorite';
    h.ids.get('dsp-preset-name').textContent = 'Private preset';
    h.ids.get('dsp-shared-confirm').checked = true; h.ids.get('playlist-confirm').checked = true;
    h.ids.get('playlist-file').files = [localFile('private file')]; h.ids.get('playlist-file').value = 'C:\\fakepath\\private.json';
    const before = h.requests.length;
    h.pagehide(persisted); assertPrivateDialogsCleared(h); assert.equal(h.requests.length, before);
    h.pageshow(); await h.refresh(); assertPrivateDialogsCleared(h);
    assert.equal(h.ids.get('track-title').textContent, state().media.title);
    assert.equal(h.ids.get('playback-state').textContent, 'STOPPED'); assert.equal(h.ids.get('volume-current').textContent, '20%');
    for (const kind of privateDialogs) {
      // Even if autofill/new typing restores values, dismissed context is gone.
      h.ids.get(kind.name).value = 'replacement-text';
      h.ids.get('favorite-url').value = 'https://example.test/new';
      await h.ids.get(kind.form).fire('submit');
    }
    assert.equal(h.posts().length, 0);
  }
});
test('navigation preserves sent commands and late UNKNOWN outcomes without repopulating cleared dialogs or retrying', async () => {
  for (const kind of privateDialogs) {
    const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
    await privateDraft(h, kind, 'private-draft');
    const saving = h.ids.get(kind.form).fire('submit'); await h.settle();
    const sent = h.requests.find(request => request.options.method === 'POST');
    assert.equal(h.posts().length, 1); assert.equal(h.ids.get('notice-kind').textContent, 'PENDING');
    h.pagehide(); h.pageshow(); assertPrivateDialogsCleared(h);
    assert.equal(sent.options.signal.aborted, false); assert.equal(h.ids.get('notice-kind').textContent, 'PENDING');
    assert.equal(h.ids.get('stop').disabled, false);
    await h.ids.get(kind.form).fire('submit'); assert.equal(h.posts().length, 1);
    pending.reject(new Error('lost private-draft')); await saving; await h.refresh();
    assertPrivateDialogsCleared(h); assert.equal(h.posts().length, 1);
    assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.match(h.ids.get('notice-text').textContent, /No retry was sent/);
    assert.doesNotMatch(h.ids.get('notice-text').textContent, /private-draft/);
    h.pagehide(); h.pageshow(); await h.refresh(); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
  }
});
test('late responses after navigation preserve a newer Stop outcome and queued close cannot wipe a later reopened draft', async () => {
  for (const kind of privateDialogs) for (const lost of [false, true]) {
    const h = await appHarness(), pending = deferred(), dialog = h.ids.get(kind.dialog);
    dialog.close = () => {dialog.open = false;};
    h.setPost(body => body.action === kind.action ? pending.promise : {request_acknowledged: true});
    await privateDraft(h, kind, 'old'); const saving = h.ids.get(kind.form).fire('submit'); await h.settle();
    h.pagehide(); h.pageshow(); assertPrivateDialogsCleared(h);
    await h.ids.get('stop').fire('click');
    assert.equal(h.ids.get(kind.open).disabled, true); // The earlier request is still in flight.
    if (lost) pending.reject(new Error('lost')); else pending.resolve({request_acknowledged: true});
    await saving; await h.refresh(); assertPrivateDialogsCleared(h);
    await privateDraft(h, kind, 'new'); const status = h.ids.get(kind.status).textContent;
    await dialog.fire('close'); await h.refresh();
    assert.equal(dialog.open, true); assert.equal(h.ids.get(kind.name).value, 'new'); assert.equal(h.ids.get(kind.status).textContent, status);
    if (kind.action === 'favorite_add') assert.equal(h.ids.get('favorite-url').value, 'https://example.test/audio?signature=new');
    if (kind.action === 'playlist_append') assert.equal(h.ids.get('playlist-urls').value, 'https://example.test/audio?signature=new');
    assert.deepEqual(h.posts().map(body => body.action), [kind.action, 'stop']);
    assert.match(h.ids.get('notice-text').textContent, /^Stop:/);
    if (lost) assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown/);
  }
});
test('late file read after navigation cannot restore cached input or replace a reopened playlist draft', async () => {
  for (const reopen of [false, true]) {
    const h = await appHarness(), late = deferred(); await h.ids.get('playlist-load').fire('click');
    const reading = chooseFile(h, {name: 'private.json', size: 3, arrayBuffer: () => late.promise}); await h.settle();
    h.pagehide(); h.pageshow(); assertPrivateDialogsCleared(h);
    if (reopen) {await h.ids.get('playlist-add').fire('click'); await sourceURLs(h, 'https://example.test/new');}
    late.resolve(new TextEncoder().encode('old').buffer); await reading; await h.refresh();
    if (reopen) {assert.equal(h.ids.get('playlist-dialog').open, true); assert.equal(h.ids.get('playlist-urls').value, 'https://example.test/new');}
    else assertPrivateDialogsCleared(h);
    assert.equal(h.posts().length, 0);
  }
});

const unknownGain = (changes = {}) => state({gain: {status: 'UNKNOWN', volume: null, mute: null, last_confirmed: null}, ...changes});
const zeroGain = (changes = {}) => state({gain: {status: 'CONFIRMED', volume: 0, mute: false, last_confirmed: {volume: 0, mute: false}}, ...changes});
test('failed gain followed by Stop offers explicit zero recovery without changing Play', async () => {
  const h = await appHarness();
  h.ids.get('volume').value = '35'; await h.ids.get('volume').fire('input');
  h.setPost(body => {
    if (body.action === 'volume') {
      h.setState(unknownGain({transport: {phase: 'ERROR', intent: 'stop', status: 'ERROR', message: 'Gain confirmation failed; actual output unknown'}}));
      return {__status: 502, outcome: 'UNKNOWN', message: 'Gain confirmation failed'};
    }
    h.setState(unknownGain()); return {request_acknowledged: true};
  });
  await h.ids.get('volume-form').fire('submit');
  assert.equal(h.ids.get('volume-current').textContent, 'UNKNOWN');
  assert.equal(h.ids.get('volume').hidden, true); assert.equal(h.ids.get('mute').disabled, true);
  await h.ids.get('stop').fire('click');
  assert.equal(h.ids.get('playback-state').textContent, 'STOPPED');
  assert.equal(h.ids.get('play').disabled, false); assert.equal(h.ids.get('volume-zero').disabled, false);
  assert.equal(h.ids.get('volume-recovery').hidden, false);
  h.setPost(body => {assert.deepEqual(body, {action: 'volume', value: 0}); h.setState(zeroGain()); return {request_acknowledged: true};});
  await h.ids.get('volume-zero').fire('click');
  assert.deepEqual(h.posts(), [{action: 'volume', value: 35}, {action: 'stop'}, {action: 'volume', value: 0}]);
  assert.equal(h.ids.get('playback-state').textContent, 'STOPPED'); assert.equal(h.ids.get('play').disabled, false);
  assert.equal(h.ids.get('volume-current').textContent, '0%'); assert.equal(h.ids.get('volume').hidden, false);
  assert.equal(h.ids.get('volume-recovery').hidden, true); assert.equal(h.ids.get('mute').disabled, false);
  assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown/);
});
test('zero recovery has no optimistic gain, suppresses pending clicks, and keeps Stop available', async () => {
  const h = await appHarness(), pending = deferred(); h.setState(unknownGain()); await h.refresh();
  h.setPost(body => body.action === 'volume' ? pending.promise : {request_acknowledged: true});
  const applying = h.ids.get('volume-zero').fire('click'); await h.settle();
  assert.equal(h.ids.get('volume-current').textContent, 'UNKNOWN'); assert.equal(h.ids.get('volume').hidden, true);
  assert.equal(h.ids.get('volume-zero').disabled, true); assert.equal(h.ids.get('stop').disabled, false);
  assert.equal(h.ids.get('notice-kind').textContent, 'PENDING');
  await h.ids.get('volume-zero').fire('click'); assert.deepEqual(h.posts(), [{action: 'volume', value: 0}]);
  await h.ids.get('stop').fire('click');
  pending.resolve({request_acknowledged: true}); await applying;
  assert.deepEqual(h.posts(), [{action: 'volume', value: 0}, {action: 'stop'}]);
  assert.match(h.ids.get('notice-text').textContent, /^Stop:/);
  assert.equal(h.ids.get('volume-current').textContent, 'UNKNOWN'); assert.equal(h.ids.get('volume-zero').disabled, false);
  await h.refresh(); assert.equal(h.posts().length, 2);
});
test('zero recovery UNKNOWN or rejected outcomes stay honest and never retry during polls', async () => {
  for (const outcome of ['lost', 'UNKNOWN', 'REJECTED']) {
    const h = await appHarness(); h.setState(unknownGain()); await h.refresh();
    h.setPost(() => {if (outcome === 'lost') throw new Error('lost response'); return {__status: outcome === 'UNKNOWN' ? 502 : 409, outcome, message: 'Request could not be confirmed'};});
    await h.ids.get('volume-zero').fire('click'); await h.refresh(); await h.refresh();
    assert.deepEqual(h.posts(), [{action: 'volume', value: 0}]);
    assert.equal(h.ids.get('volume-current').textContent, 'UNKNOWN'); assert.equal(h.ids.get('mute').disabled, true);
    assert.equal(h.ids.get('notice-kind').textContent, outcome === 'REJECTED' ? 'ERROR' : 'UNKNOWN');
    assert.equal(h.ids.get('volume-zero').disabled, false);
    // A fresh explicit idempotent recovery remains possible; polling never sends it.
    await h.ids.get('volume-zero').fire('click'); assert.equal(h.posts().length, 2);
  }
});
test('zero acknowledgement cannot fabricate readback or enable recovery for PENDING gain', async () => {
  const h = await appHarness(); h.setState(unknownGain()); await h.refresh();
  h.setPost(() => {h.setState(state({gain: {status: 'PENDING', volume: null, mute: null, last_confirmed: null}})); return {request_acknowledged: true};});
  await h.ids.get('volume-zero').fire('click');
  assert.equal(h.ids.get('volume-current').textContent, 'PENDING'); assert.equal(h.ids.get('volume').hidden, true);
  assert.equal(h.ids.get('volume-recovery').hidden, true); assert.equal(h.ids.get('volume-zero').disabled, true);
  await h.ids.get('volume-zero').fire('click'); assert.equal(h.posts().length, 1);
  h.setState(unknownGain()); await h.refresh();
  assert.equal(h.ids.get('volume-recovery').hidden, false); assert.equal(h.ids.get('volume-zero').disabled, false);
  assert.equal(h.posts().length, 1);
  h.setState(zeroGain()); await h.refresh();
  assert.equal(h.ids.get('volume-current').textContent, '0%'); assert.equal(h.ids.get('volume-recovery').hidden, true);
  await h.ids.get('volume-zero').fire('click'); assert.equal(h.posts().length, 1);
});
test('zero recovery refuses stale state and uses the reconnected session token', async () => {
  const h = await appHarness(); h.setState(unknownGain()); await h.refresh();
  h.setState({}); await h.refresh();
  assert.equal(h.ids.get('volume-zero').disabled, true); assert.equal(h.ids.get('stop').disabled, false);
  await h.ids.get('volume-zero').fire('click'); assert.equal(h.posts().length, 0);
  h.setState(unknownGain({session_id: 'session-two', csrf_token: 'new-token'})); await h.refresh();
  assert.equal(h.ids.get('volume-zero').disabled, false); await h.ids.get('volume-zero').fire('click');
  assert.deepEqual(h.posts(), [{action: 'volume', value: 0}]);
  assert.equal(h.requests.find(item => item.options.method === 'POST').options.headers['X-ORR-CSRF'], 'new-token');
});
test('late zero responses cannot overwrite new-session gain or erase an unknown outcome', async () => {
  for (const outcome of ['acknowledged', 'lost']) {
    const h = await appHarness(), pending = deferred(); h.setState(unknownGain()); await h.refresh();
    h.setPost(() => pending.promise); const applying = h.ids.get('volume-zero').fire('click'); await h.settle();
    h.setState(state({session_id: 'session-two', csrf_token: 'new-token'})); await h.refresh();
    if (outcome === 'lost') pending.reject(new Error('lost old-session response'));
    else pending.resolve({request_acknowledged: true});
    await applying; await h.refresh();
    assert.equal(h.ids.get('volume-current').textContent, '20%'); assert.equal(h.ids.get('volume').value, '20');
    assert.equal(h.ids.get('volume-recovery').hidden, true); assert.equal(h.ids.get('volume-zero').disabled, true);
    assert.deepEqual(h.posts(), [{action: 'volume', value: 0}]);
    if (outcome === 'lost') assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
  }
});
test('zero recovery preserves ordinary volume drafts without implicitly sending them', async () => {
  const h = await appHarness(), slider = h.ids.get('volume'); slider.value = '42'; await slider.fire('input');
  h.setState(unknownGain()); await h.refresh();
  assert.equal(slider.value, '42'); assert.equal(h.ids.get('volume-draft').hidden, false);
  h.setPost(() => {h.setState(zeroGain()); return {request_acknowledged: true};});
  await h.ids.get('volume-zero').fire('click'); await h.refresh();
  assert.deepEqual(h.posts(), [{action: 'volume', value: 0}]);
  assert.equal(slider.value, '42'); assert.equal(h.ids.get('volume-target').textContent, '42');
  assert.equal(h.ids.get('volume-draft').hidden, false); assert.equal(h.ids.get('volume-current').textContent, '0%');
  assert.equal(h.ids.get('playback-state').textContent, 'STOPPED');
});

test('lost zero response stays UNKNOWN even when fresh canonical gain later confirms zero', async () => {
  const h = await appHarness(); h.setState(unknownGain()); await h.refresh();
  h.setPost(() => {h.setState(zeroGain()); throw new Error('response lost after application');});
  await h.ids.get('volume-zero').fire('click'); await h.refresh();
  assert.deepEqual(h.posts(), [{action: 'volume', value: 0}]);
  assert.equal(h.ids.get('volume-current').textContent, '0%'); assert.equal(h.ids.get('volume-recovery').hidden, true);
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.match(h.ids.get('notice-text').textContent, /outcome unknown/);
  assert.equal(h.ids.get('playback-state').textContent, 'STOPPED');
});


/* Export tests use synthetic fetch, stream, Blob-URL and DOM doubles. They do
 * not establish browser downloads, focus, native modality, BFCache or CSP QA. */
function privateJSON(changes = {}) {
  return JSON.stringify({version: 1, name: 'Private original queue ?token=queue-secret', index: 1, mode: 'normal', urls: ['https://example.test/a?token=private-one&nested=%22x%22', 'https://example.test/a?token=private-one&nested=%22x%22'], ...changes}, null, 2) + '\n';
}
const encoded = value => new TextEncoder().encode(value);
function exportSummary(current = state(), changes = {}) {
  return {version: 1, session_id: current.session_id, generation: current.generation, revision: revisionID('e'), name: current.queue.name, count: current.queue.count, index: current.queue.index, mode: current.queue.mode, size_bytes: encoded(privateJSON()).length, filename: EXPORT_FILENAME, ...changes};
}
function exportResponse(value, {status = 200, headers = {}, attachment = false} = {}) {
  const bytes = value instanceof Uint8Array ? value : encoded(typeof value === 'string' ? value : JSON.stringify(value));
  return new Response(bytes, {status, headers: {'Content-Type': 'application/json', 'Content-Length': String(bytes.length), ...(attachment ? {'Content-Disposition': `attachment; filename="${EXPORT_FILENAME}"`} : {}), ...headers}});
}
async function consentExport(h) {h.ids.get('export-confirm').checked = true; await h.ids.get('export-confirm').fire('change');}
async function readyExport(h) {await h.ids.get('playlist-export').fire('click'); await consentExport(h); await h.ids.get('export-form').fire('submit');}
function assertExportCleared(h) {
  assert.equal(h.ids.get('export-dialog').open, false);
  assert.equal(h.ids.get('export-confirm').checked, false);
  assert.equal(h.ids.get('export-download').hidden, true);
  assert.equal(h.ids.get('export-download').getAttribute('href'), undefined);
  assert.equal(h.ids.get('export-summary').textContent, ''); assert.equal(h.ids.get('export-status').textContent, '');
  assert.equal(h.blobs.size, 0);
}
test('export validators reject unexpected summary fields, mismatched context, unsafe numbers and duplicate JSON fields', () => {
  assert.equal(validateExportSummary(exportSummary(), state()).filename, EXPORT_FILENAME);
  for (const changes of [{version: true}, {version: 2}, {session_id: 'other'}, {generation: true}, {generation: 8}, {generation: 2 ** 53}, {revision: 'E'.repeat(64)}, {revision: 'e'.repeat(63)}, {name: 'https://example.test/?token=secret'}, {count: true}, {count: 0}, {count: 1001}, {index: true}, {index: 0}, {index: 2}, {mode: 'bad'}, {size_bytes: true}, {size_bytes: 0}, {size_bytes: 32769}, {size_bytes: 1.5}, {filename: '../private.json'}, {urls: ['https://example.test/private']}]) assert.throws(() => validateExportSummary(exportSummary(state(), changes), state()));
  const missing = exportSummary(); delete missing.name; assert.throws(() => validateExportSummary(missing, state()));
  for (const source of ['{"x":1,"x":2}', '{"x":1,"\\u0078":2}', '{"version":1,}', 'null', '[]', '{"x":NaN}', '{"version":1.0}', '{"index":1e0}', '\ufeff{"version":1}']) assert.throws(() => exportObject(encoded(source)));
  assert.throws(() => exportObject(new Uint8Array([0xff])));
  const tricky = {name: 'a \"version\": 4, \\ \"name\": 5', version: 1};
  assert.deepEqual(exportObject(encoded(JSON.stringify(tricky))), tricky);
  assert.throws(() => exportObject(new Uint8Array(32769)));
});
test('export document validation preserves exact UTF-8, signed URLs and duplicates without previewing source', () => {
  const source = privateJSON({name: '🎵 Café "queue" \u00e9'}), bytes = encoded(source), summary = exportSummary(state(), {size_bytes: bytes.length});
  assert.equal(validateExportDocument(bytes, summary), bytes); assert.equal(new TextDecoder().decode(bytes), source);
  for (const changes of [{version: true}, {extra: 'private'}, {name: ''}, {name: 'a'.repeat(257)}, {name: 'bad\nname'}, {index: 2}, {mode: 'repeat-one'}, {urls: ['https://example.test/a']}, {urls: [null, 'https://example.test/a']}, {urls: ['https://example.test/\uffff', 'https://example.test/a']}]) {
    const bad = encoded(privateJSON(changes)); assert.throws(() => validateExportDocument(bad, {...summary, size_bytes: bad.length}));
  }
  const duplicate = encoded(privateJSON().replace('"version": 1,', '"version": 1, "version": 1,'));
  assert.throws(() => validateExportDocument(duplicate, {...summary, size_bytes: duplicate.length}));
  assert.throws(() => validateExportDocument(bytes, {...summary, size_bytes: bytes.length + 1}));
});
test('bounded export reader enforces strict type, exact length and fixed attachment headers', async () => {
  const source = privateJSON(), size = encoded(source).length;
  const read = (response, expected = size) => readExportResponse(response, new AbortController().signal, expected);
  const good = await read(exportResponse(source, {attachment: true, headers: {'Content-Type': 'application/json; charset=utf-8'}}));
  assert.equal(new TextDecoder().decode(good.bytes), source);
  for (const headers of [ {'Content-Type': 'text/html'}, {'Content-Type': 'application/json; charset=latin1'}, {'Content-Type': 'application/json; unknown=x'}, {'Content-Length': '0'}, {'Content-Length': '32769'}, {'Content-Length': '-1'}, {'Content-Length': '01'}, {'Content-Length': '1e2'}, {'Content-Length': '2, 2'}, {'Content-Length': String(size + 1)}, {'Content-Length': String(size - 1)}, {'Content-Disposition': 'attachment; filename="other.json"'}, {'Content-Disposition': ''}]) await assert.rejects(read(exportResponse(source, {attachment: true, headers})));
  const missing = exportResponse(source, {attachment: true}); missing.headers.delete('Content-Length'); await assert.rejects(read(missing));
  const empty = exportResponse(''); await assert.rejects(read(empty, undefined));
  const invalidUTF8 = await readExportResponse(exportResponse(new Uint8Array([0xff])), new AbortController().signal);
  assert.throws(() => exportObject(invalidUTF8.bytes));
});
test('export reader cancels oversize and uncooperative late streams without retaining their bytes', async () => {
  let cancelled = 0, reads = 0;
  const pending = deferred(), abort = new AbortController();
  const response = {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': '3'}), body: {getReader: () => ({read: () => {reads++; return pending.promise;}, cancel: () => {cancelled++; return Promise.resolve();}})}};
  const reading = readExportResponse(response, abort.signal); await tick(); abort.abort();
  await assert.rejects(reading); assert.equal(cancelled, 1); assert.equal(reads, 1);
  pending.resolve({done: false, value: encoded('late private bytes')}); await tick(); assert.equal(reads, 1);
  const oversize = {...response, body: {getReader: () => ({read: async () => ({done: false, value: new Uint8Array(32769)}), cancel: async () => {cancelled++;}})}};
  await assert.rejects(readExportResponse(oversize, new AbortController().signal)); assert.equal(cancelled, 2);
});
test('export prepares only a redacted summary, requires unchecked consent and never automatically downloads', async () => {
  const h = await appHarness(); assert.equal(h.ids.get('playlist-export').disabled, false);
  await h.ids.get('playlist-export').fire('click');
  assert.equal(h.ids.get('export-dialog').open, true); assert.equal(h.ids.get('export-confirm').checked, false); assert.equal(h.ids.get('export-prepare').disabled, true);
  assert.equal(h.exports().length, 1); assert.deepEqual(JSON.parse(h.exports()[0].options.body), {action: 'prepare', expected_session: 'session-one', expected_generation: 7});
  assert.match(h.ids.get('export-summary').textContent, /My queue · 2 entries · Start at 1 · In order/);
  assert.doesNotMatch(h.ids.get('export-summary').textContent, /private-one|queue-secret|https:/);
  await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 1);
  await consentExport(h); assert.equal(h.ids.get('export-prepare').disabled, false);
  await h.ids.get('export-form').fire('submit');
  assert.equal(h.exports().length, 2); assert.deepEqual(JSON.parse(h.exports()[1].options.body), {action: 'download', expected_session: 'session-one', expected_generation: 7, expected_export: revisionID('e'), private_export_confirmed: true});
  for (const request of h.exports()) {assert.equal(request.options.credentials, 'same-origin'); assert.equal(request.options.mode, 'same-origin'); assert.equal(request.options.redirect, 'error'); assert.equal(request.options.cache, 'no-store'); assert.equal(request.options.referrerPolicy, 'no-referrer'); assert.equal(request.options.headers['X-ORR-CSRF'], 'test-csrf-token');}
  assert.equal(h.created.length, 1); assert.equal(h.blobs.size, 1);
  const link = h.ids.get('export-download'); assert.equal(link.tagName, 'A'); assert.equal(link.getAttribute('download'), EXPORT_FILENAME); assert.equal(link.hidden, false);
  assert.equal(await h.blobs.get(link.getAttribute('href')).text(), privateJSON());
  assert.match(h.ids.get('export-status').textContent, /Private file ready/); assert.equal(h.syntheticClicks.length, 0);
  await h.refresh(); await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 2); assert.equal(h.syntheticClicks.length, 0);
  assert.equal((await link.fire('click')).defaultPrevented, false);
  assert.match(h.ids.get('export-status').textContent, /Download requested; check your browser/);
  assert.doesNotMatch(h.ids.get('export-status').textContent, /saved|durabl|backed up/i);
});
test('export availability is read-only, works while playing and defers unsupported queues to prepare', async () => {
  const h = await appHarness();
  h.setState(state({transport: {...state().transport, phase: 'PLAYING'}, queue: {...state().queue, playlist_edit_reason: 'unsupported_queue'}, actions: {...state().actions, playlist_replace: false, playlist_append: false}})); await h.refresh();
  assert.equal(h.ids.get('playlist-load').disabled, true); assert.equal(h.ids.get('playlist-export').disabled, false);
  h.setExport(() => exportResponse({error: 'export_unavailable', message: 'must not echo https://example.test/?token=private', outcome: 'REJECTED'}, {status: 409}));
  await h.ids.get('playlist-export').fire('click');
  assert.match(h.ids.get('export-status').textContent, /playlist save-current FILE/); assert.match(h.ids.get('export-status').textContent, /32,768/); assert.doesNotMatch(h.ids.get('export-status').textContent, /example.test|token=private/);
  assert.equal(h.ids.get('export-prepare').disabled, true); assert.equal(h.ids.get('export-confirm').disabled, true); assert.equal(h.created.length, 0);
  await h.ids.get('export-close').fire('click');
  h.setState(state({queue: {...state().queue, count: 0, index: 0, items: []}})); await h.refresh(); await h.ids.get('playlist-export').fire('click'); assert.equal(h.exports().length, 1);
  h.setState({}); await h.refresh(); assert.equal(h.ids.get('playlist-export').disabled, true);
});
test('export duplicate prepare and file requests are suppressed while dismissal stays usable', async () => {
  for (const action of ['prepare', 'download']) {
    const h = await appHarness(), pending = deferred();
    h.setExport(body => body.action === action ? pending.promise : exportResponse(exportSummary()));
    let request;
    if (action === 'prepare') request = h.ids.get('playlist-export').fire('click');
    else {await h.ids.get('playlist-export').fire('click'); await consentExport(h); request = h.ids.get('export-form').fire('submit');}
    await h.settle();
    await h.ids.get('playlist-export').fire('click'); await h.ids.get('export-form').fire('submit');
    assert.equal(h.exports().length, action === 'prepare' ? 1 : 2); assert.equal(h.ids.get('export-close').disabled, false); assert.equal(h.ids.get('export-cancel').disabled, false); assert.equal(h.ids.get('stop').disabled, false);
    pending.resolve(exportResponse(action === 'prepare' ? exportSummary() : privateJSON(), {attachment: action === 'download'})); await request;
    assert.equal(h.syntheticClicks.length, 0);
  }
});
test('export errors and malformed responses show only fixed text and never create a Blob', async () => {
  const scenarios = [
    () => exportResponse({...exportSummary(), urls: ['https://example.test/?token=private']}),
    () => exportResponse({...exportSummary(), name: 'private-secret'}),
    () => exportResponse({...exportSummary(), size_bytes: 32769}),
    () => exportResponse('{"version":1,"version":1}'),
    () => exportResponse('https://example.test/?token=private', {headers: {'Content-Type': 'text/html'}}),
    () => exportResponse(new Uint8Array(32769)),
    () => exportResponse({outcome: 'REJECTED', error: 'forbidden', message: 'private-secret'}, {status: 403}),
    () => exportResponse({outcome: 'UNKNOWN', error: 'stale_export', message: 'private-secret'}, {status: 409}),
    () => {throw new Error('private-secret');}
  ];
  for (const scenario of scenarios) {
    const h = await appHarness(); h.setExport(scenario); const notice = h.ids.get('notice-text').textContent;
    await h.ids.get('playlist-export').fire('click');
    assert.match(h.ids.get('export-status').textContent, /Could not prepare/); assert.doesNotMatch(h.ids.get('export-status').textContent, /private-secret|token=private/);
    assert.equal(h.ids.get('export-summary').textContent, ''); assert.equal(h.ids.get('export-prepare').disabled, true); assert.equal(h.created.length, 0); assert.equal(h.ids.get('notice-text').textContent, notice);
  }
  const h = await appHarness(); h.setExport(body => body.action === 'prepare' ? exportResponse(exportSummary()) : exportResponse({error: 'stale_export', outcome: 'REJECTED', message: 'private-secret'}, {status: 409})); await readyExport(h);
  assert.match(h.ids.get('export-status').textContent, /reopen Export JSON/); assert.doesNotMatch(h.ids.get('export-status').textContent, /private-secret/); assert.equal(h.created.length, 0);
});
test('invalid private-file responses cannot become download links or expose source in status', async () => {
  for (const response of [() => exportResponse(privateJSON()), () => exportResponse(privateJSON(), {attachment: true, headers: {'Content-Length': '32769'}}), () => exportResponse('private-secret', {attachment: true}), () => exportResponse(new Uint8Array(encoded(privateJSON()).length).fill(0xff), {attachment: true}), () => exportResponse(privateJSON({index: 2}), {attachment: true})]) {
    const h = await appHarness(); h.setExport(body => body.action === 'prepare' ? exportResponse(exportSummary()) : response()); await readyExport(h);
    assert.match(h.ids.get('export-status').textContent, /Could not prepare/); assert.doesNotMatch(h.ids.get('export-status').textContent, /private-secret|private-one/); assert.equal(h.created.length, 0); assert.equal(h.ids.get('export-download').hidden, true);
  }
});
test('Close, Cancel, Escape, native close and pagehide revoke only owned export URLs and clear consent', async () => {
  for (const dismiss of ['export-close', 'export-cancel', 'escape', 'native-close', 'pagehide']) {
    const h = await appHarness(); await readyExport(h); const owned = h.created[0];
    if (dismiss === 'escape') await h.ids.get('export-dialog').fire('cancel');
    else if (dismiss === 'native-close') h.ids.get('export-dialog').close();
    else if (dismiss === 'pagehide') h.pagehide(); else await h.ids.get(dismiss).fire('click');
    assertExportCleared(h); assert.deepEqual(h.revoked, [owned]);
    h.pagehide(); h.pageshow(); await h.refresh(); assertExportCleared(h); assert.deepEqual(h.revoked, [owned]);
    await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 2); assert.equal(h.syntheticClicks.length, 0);
  }
});
test('clearing export consent revokes the prepared private file and requires a new review', async () => {
  const h = await appHarness(); await readyExport(h); h.ids.get('export-confirm').checked = false; await h.ids.get('export-confirm').fire('change');
  assert.equal(h.blobs.size, 0); assert.equal(h.revoked.length, 1); assert.equal(h.ids.get('export-download').getAttribute('href'), undefined); assert.match(h.ids.get('export-status').textContent, /Consent cleared/);
  await consentExport(h); await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 2);
});
test('late export fetches after dismissal are aborted and cannot populate a reopened modal', async () => {
  for (const action of ['prepare', 'download']) for (const dismiss of ['export-close', 'export-cancel', 'escape', 'pagehide']) {
    const h = await appHarness(), pending = deferred(); let lateCancelled = 0;
    h.setExport(body => body.action === action ? pending.promise : exportResponse(exportSummary()));
    let request;
    if (action === 'prepare') request = h.ids.get('playlist-export').fire('click');
    else {await h.ids.get('playlist-export').fire('click'); await consentExport(h); request = h.ids.get('export-form').fire('submit');}
    await h.settle(); const sent = h.exports().at(-1);
    if (dismiss === 'escape') await h.ids.get('export-dialog').fire('cancel'); else if (dismiss === 'pagehide') h.pagehide(); else await h.ids.get(dismiss).fire('click');
    assert.equal(sent.options.signal.aborted, true); await request; assertExportCleared(h);
    h.setExport(() => exportResponse(exportSummary())); await h.ids.get('playlist-export').fire('click'); const status = h.ids.get('export-status').textContent;
    pending.resolve({body: {cancel: async () => {lateCancelled++;}}}); await h.settle();
    assert.equal(lateCancelled, 1); assert.equal(h.ids.get('export-dialog').open, true); assert.equal(h.ids.get('export-status').textContent, status); assert.equal(h.ids.get('export-confirm').checked, false); assert.equal(h.created.length, 0);
  }
});
test('late export body reads after dismissal never create a Blob or repopulate private state', async () => {
  for (const reopen of [false, true]) {
    const h = await appHarness(), late = deferred(); let cancelled = 0;
    h.setExport(body => body.action === 'prepare' ? exportResponse(exportSummary()) : {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': String(encoded(privateJSON()).length), 'Content-Disposition': `attachment; filename="${EXPORT_FILENAME}"`}), body: {getReader: () => ({read: () => late.promise, cancel: async () => {cancelled++;}})}});
    await h.ids.get('playlist-export').fire('click'); await consentExport(h); const reading = h.ids.get('export-form').fire('submit'); await h.settle();
    h.pagehide(); await reading; assertExportCleared(h); assert.equal(cancelled, 1);
    if (reopen) {h.setExport(() => exportResponse(exportSummary())); await h.ids.get('playlist-export').fire('click');}
    late.resolve({done: false, value: encoded(privateJSON())}); await h.settle(); assert.equal(h.created.length, 0);
    if (reopen) {assert.equal(h.ids.get('export-dialog').open, true); assert.equal(h.ids.get('export-confirm').checked, false);} else assertExportCleared(h);
  }
});
test('export timeout covers both ignored-abort fetch and body reads without retry or command notices', async () => {
  for (const stream of [false, true]) {
    const h = await appHarness(), pending = deferred(); let cancelled = 0;
    h.setExport(() => stream ? {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': '3'}), body: {getReader: () => ({read: () => pending.promise, cancel: async () => {cancelled++;}})}} : pending.promise);
    const notice = h.ids.get('notice-text').textContent, preparing = h.ids.get('playlist-export').fire('click'); await h.settle();
    const timer = [...h.timers.values()].find(item => item.delay === 20000); assert.ok(timer); timer.callback(); await preparing;
    assert.equal(h.exports()[0].options.signal.aborted, true); assert.match(h.ids.get('export-status').textContent, /Could not prepare/); assert.equal(h.created.length, 0); assert.equal(h.ids.get('notice-text').textContent, notice); assert.equal(h.ids.get('stop').disabled, false);
    await h.refresh(); await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 1); assert.equal([...h.timers.values()].filter(item => item.delay === 20000).length, 0);
    if (stream) {assert.equal(cancelled, 1); pending.resolve({done: false, value: encoded('old')});} else pending.resolve(exportResponse(exportSummary()));
    await h.settle(); assert.equal(h.created.length, 0);
  }
});
test('known session, generation and same-generation queue edits latch export staleness and revoke ready files', async () => {
  for (const changed of [state({session_id: 'session-two'}), state({generation: 8}), state({queue: {...state().queue, mode: 'repeat-all'}}), state({queue: {...state().queue, name: 'Changed'}}), state({queue: {...state().queue, index: 2}}), state({queue: {...state().queue, count: 1, items: [state().queue.items[0]]}})]) {
    const h = await appHarness(); await readyExport(h); h.setState(changed); await h.refresh();
    assert.equal(h.blobs.size, 0); assert.equal(h.revoked.length, 1); assert.equal(h.ids.get('export-confirm').checked, false); assert.equal(h.ids.get('export-download').hidden, true); assert.match(h.ids.get('export-status').textContent, /queue or renderer session changed/);
    h.setState(state()); await h.refresh(); await consentExport(h); await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 2); assert.equal(h.ids.get('export-prepare').disabled, true); assert.equal((await h.ids.get('export-download').fire('click')).defaultPrevented, true);
  }
});
test('known context changes abort pending export reads and refuse late old-session results', async () => {
  for (const action of ['prepare', 'download']) {
    const h = await appHarness(), pending = deferred(); h.setExport(body => body.action === action ? pending.promise : exportResponse(exportSummary()));
    let request;
    if (action === 'prepare') request = h.ids.get('playlist-export').fire('click'); else {await h.ids.get('playlist-export').fire('click'); await consentExport(h); request = h.ids.get('export-form').fire('submit');}
    await h.settle(); h.setState(state({session_id: 'restarted', csrf_token: 'new-token'})); await h.refresh(); await request;
    assert.equal(h.exports().at(-1).options.signal.aborted, true); assert.match(h.ids.get('export-status').textContent, /session changed/);
    pending.resolve(exportResponse(action === 'prepare' ? exportSummary() : privateJSON(), {attachment: action === 'download'})); await h.settle(); assert.equal(h.created.length, 0);
    await h.ids.get('export-close').fire('click'); h.setExport(() => exportResponse(exportSummary(state({session_id: 'restarted'})))); await h.ids.get('playlist-export').fire('click'); assert.equal(h.exports().at(-1).options.headers['X-ORR-CSRF'], 'new-token');
  }
});
test('metadata-only polls preserve a ready export but stale readback disables its anchor', async () => {
  const h = await appHarness(); await readyExport(h); const owned = h.created[0];
  h.setState(state({media: {...state().media, title: 'New metadata'}, queue: {...state().queue, items: state().queue.items.map(item => ({...item, title: 'New title'}))}})); await h.refresh();
  assert.equal(h.ids.get('export-download').getAttribute('href'), owned); assert.equal(h.revoked.length, 0);
  h.setState({}); await h.refresh(); assert.equal(h.ids.get('export-download').getAttribute('href'), undefined); assert.equal(h.ids.get('export-download').hidden, true); assert.equal((await h.ids.get('export-download').fire('click')).defaultPrevented, true);
  h.setState(state()); await h.refresh(); assert.equal(h.ids.get('export-download').getAttribute('href'), owned); assert.equal(h.exports().length, 2);
});
test('delayed export close events and old read completions cannot erase a new dialog or its Blob', async () => {
  const h = await appHarness(), dialog = h.ids.get('export-dialog'); dialog.close = () => {dialog.open = false;};
  await readyExport(h); const old = h.created[0]; await h.ids.get('export-close').fire('click'); await readyExport(h); const current = h.created[1];
  await dialog.fire('close'); assert.equal(dialog.open, true); assert.equal(h.ids.get('export-confirm').checked, true); assert.equal(h.ids.get('export-download').getAttribute('href'), current); assert.deepEqual(h.revoked, [old]); assert.equal(h.blobs.size, 1);
  await h.ids.get('export-close').fire('click'); assertExportCleared(h); assert.deepEqual(h.revoked, [old, current]);
});
test('export leaves pending, Stop and UNKNOWN command outcomes untouched and never dispatches playback', async () => {
  for (const kind of ['pending', 'stop', 'unknown']) {
    const h = await appHarness(), pending = deferred(); let command;
    if (kind === 'pending') {h.setPost(() => pending.promise); command = h.ids.get('play').fire('click'); await h.settle(); await h.refresh();}
    else if (kind === 'stop') await h.ids.get('stop').fire('click');
    else {h.setPost(() => {throw new Error('lost');}); await h.ids.get('stop').fire('click');}
    const notice = h.ids.get('notice-text').textContent, badge = h.ids.get('notice-kind').textContent;
    await readyExport(h); assert.equal(h.ids.get('notice-text').textContent, notice); assert.equal(h.ids.get('notice-kind').textContent, badge);
    assert.equal(h.requests.filter(item => item.url.endsWith('/command')).length, 1); assert.equal(h.ids.get('export-close').disabled, false);
    await h.ids.get('export-close').fire('click');
    if (kind === 'pending') {h.setPost(() => ({request_acknowledged: true})); await h.ids.get('stop').fire('click'); pending.reject(new Error('lost')); await command; assert.match(h.ids.get('notice-text').textContent, /^Stop:/); assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown/);}
    else assert.equal(h.ids.get('notice-text').textContent, notice);
  }
});
test('export markup provides the complete private warning, fixed download name and modal Stop guidance', () => {
  const html = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/index.html'), 'utf8');
  const modal = html.slice(html.indexOf('<dialog id="export-dialog"'), html.indexOf('<dialog id="dsp-dialog"'));
  for (const phrase of ['original source URLs', 'original queue name', 'private tokens', 'Keep it private', 'Signed links can expire', 'track metadata', 'playback position and state', 'provider and account state', 'shuffle history', 'No media is fetched', 'playability is not checked', 'Close this dialog first', 'including Stop', 'Prepare private file', 'Download JSON', '32,768', 'nested JSON escaping']) assert.ok(modal.includes(phrase), phrase);
  assert.match(modal, /<input id="export-confirm" type="checkbox" disabled>/); assert.doesNotMatch(modal, /<input[^>]*\bchecked\b/);
  assert.match(modal, /<a id="export-download"[^>]*download="open-room-renderer-playlist.json" hidden>/);
  const source = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/app.js'), 'utf8');
  assert.doesNotMatch(source, /\.click\(|localStorage|sessionStorage|clipboard|console\./);
});

test('private export retains server-validated scoped IPv6 references without WHATWG navigation parsing', async () => {
  const source = privateJSON({urls: ['http://[fe80::1%25eth0]/audio.mp3', 'https://example.test/a?case=%2f&case=%2F']}), bytes = encoded(source), summary = exportSummary(state(), {size_bytes: bytes.length});
  assert.equal(validateExportDocument(bytes, summary), bytes);
  const h = await appHarness(); h.setExport(body => body.action === 'prepare' ? exportResponse(summary) : exportResponse(source, {attachment: true})); await readyExport(h);
  assert.equal(h.blobs.size, 1); assert.equal(await h.blobs.values().next().value.text(), source); assert.equal(h.syntheticClicks.length, 0);
});

test('private export preserves server-valid U+FEFF queue names while rejecting a document BOM', async () => {
  for (const name of ['\ufeffQueue', '\ufeff']) {
    const source = privateJSON({name}), bytes = encoded(source), summary = exportSummary(state(), {size_bytes: bytes.length});
    assert.equal(validateExportDocument(bytes, summary), bytes);
    assert.throws(() => exportObject(encoded('\ufeff' + source)));
    const h = await appHarness(); h.setExport(body => body.action === 'prepare' ? exportResponse(summary) : exportResponse(source, {attachment: true})); await readyExport(h);
    assert.equal(await h.blobs.values().next().value.text(), source); assert.equal(h.syntheticClicks.length, 0);
  }
});

test('export reader accepts exact 32,768-byte bounded streaming but rejects a one-byte overflow', async () => {
  for (const size of [32768, 32769]) {
    let part = 0, cancelled = 0;
    const response = {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': '32768'}), body: {getReader: () => ({read: async () => ++part === 1 ? {done: false, value: new Uint8Array(16384)} : part === 2 ? {done: false, value: new Uint8Array(size - 16384)} : {done: true}, cancel: async () => {cancelled++;}})}};
    const reading = readExportResponse(response, new AbortController().signal);
    if (size === 32768) {assert.equal((await reading).bytes.length, 32768); assert.equal(cancelled, 0);}
    else {await assert.rejects(reading); assert.equal(cancelled, 1);}
  }
});
test('a private response arriving during unavailable readback is discarded rather than restored later', async () => {
  const h = await appHarness(), pending = deferred(); h.setExport(body => body.action === 'prepare' ? exportResponse(exportSummary()) : pending.promise);
  await h.ids.get('playlist-export').fire('click'); await consentExport(h); const preparing = h.ids.get('export-form').fire('submit'); await h.settle();
  h.setState({}); await h.refresh(); pending.resolve(exportResponse(privateJSON(), {attachment: true})); await preparing;
  assert.equal(h.created.length, 0); h.setState(state()); await h.refresh();
  assert.match(h.ids.get('export-status').textContent, /Could not prepare/); assert.equal(h.ids.get('export-download').hidden, true);
  await h.ids.get('export-form').fire('submit'); assert.equal(h.exports().length, 2);
});
test('an old private read cannot revoke or replace a newer dialog-owned Blob', async () => {
  const h = await appHarness(), late = deferred();
  h.setExport(body => body.action === 'prepare' ? exportResponse(exportSummary()) : {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': String(encoded(privateJSON()).length), 'Content-Disposition': `attachment; filename="${EXPORT_FILENAME}"`}), body: {getReader: () => ({read: () => late.promise, cancel: async () => {}})}});
  await h.ids.get('playlist-export').fire('click'); await consentExport(h); const old = h.ids.get('export-form').fire('submit'); await h.settle();
  await h.ids.get('export-close').fire('click'); await old; h.setExport(null); await readyExport(h); const owned = h.created[0];
  late.resolve({done: false, value: encoded(privateJSON({name: 'Old private document'}))}); await h.settle();
  assert.equal(h.ids.get('export-download').getAttribute('href'), owned); assert.equal(h.created.length, 1); assert.equal(h.revoked.length, 0); assert.equal(await h.blobs.get(owned).text(), privateJSON());
});

/* Logical focus-target checks only. The double models connected/detached nodes
 * and records application focus calls; native focus/modality remains browser QA. */
const focusDialogs = [
  {dialog: 'favorite-dialog', open: 'add-favorite', close: 'favorite-close', cancel: 'favorite-cancel'},
  {dialog: 'dsp-dialog', open: 'dsp-save', close: 'dsp-close', cancel: 'dsp-cancel'},
  {dialog: 'playlist-dialog', open: 'playlist-load', close: 'playlist-close', cancel: 'playlist-cancel'},
  {dialog: 'export-dialog', open: 'playlist-export', close: 'export-close', cancel: 'export-cancel'}
];
const favoriteAction = (h, action, id = 'favorite-id') => h.ids.get('favorites-list').querySelectorAll('button').find(button => button.dataset.favoriteKey === `${action}:${id}`);
async function dismissDialog(h, kind, dismiss) {
  if (dismiss === 'escape') {const event = await h.ids.get(kind.dialog).fire('cancel'); assert.equal(event.defaultPrevented, true);}
  else await h.ids.get(kind[dismiss]).fire('click');
}
test('logical dialog focus returns to each explicit opening control for Close, Cancel and Escape', async () => {
  for (const kind of focusDialogs) for (const dismiss of ['close', 'cancel', 'escape']) {
    const h = await appHarness(), opener = h.ids.get(kind.open);
    h.ids.get('refresh').focus(); // Pointer activation need not focus its button.
    await opener.fire('click'); const before = h.focusCalls.length;
    await dismissDialog(h, kind, dismiss);
    assert.equal(h.ids.get(kind.dialog).open, false); assert.equal(h.focused(), opener, `${kind.dialog}: ${dismiss}`);
    assert.deepEqual(h.focusCalls.slice(before), [opener]); assert.equal(h.posts().length, kind.dialog === 'export-dialog' ? 1 : 0);
  }
});
test('logical favorite focus resolves successful rename replacement and deleted favorite Add fallback', async () => {
  for (const mode of ['rename', 'delete']) {
    const h = await appHarness(), opener = favoriteAction(h, mode);
    h.setPost(() => {h.setFavorites(favorites(mode === 'rename' ? [{...favorite(), name: 'Renamed'}] : [])); return {request_acknowledged: true};});
    await opener.fire('click'); h.ids.get('favorite-name').value = 'Renamed';
    await h.ids.get('favorite-form').fire('submit');
    assert.equal(opener.isConnected, false); assert.equal(h.ids.get('favorite-dialog').open, false);
    const expected = mode === 'rename' ? favoriteAction(h, mode) : h.ids.get('add-favorite');
    assert.notEqual(expected, opener); assert.equal(expected.disabled, false); assert.equal(h.focused(), expected);
    assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].action, `favorite_${mode}`);
  }
});
test('logical favorite focus survives remote redraw before Close, Cancel or Escape without sending', async () => {
  for (const mode of ['rename', 'delete']) for (const dismiss of ['close', 'cancel', 'escape']) {
    const h = await appHarness(), opener = favoriteAction(h, mode);
    await opener.fire('click'); h.setFavorites(favorites([{...favorite(), name: 'Changed remotely'}])); await h.refresh();
    const expected = favoriteAction(h, mode); assert.equal(opener.isConnected, false); assert.notEqual(expected, opener);
    await dismissDialog(h, focusDialogs[0], dismiss);
    assert.equal(h.focused(), expected); assert.equal(h.posts().length, 0);
  }
});
test('logical favorite focus never resolves an identical row identity from a different session', async () => {
  const h = await appHarness(), opener = favoriteAction(h, 'rename'); await opener.fire('click');
  h.setState(state({session_id: 'restarted'})); h.setFavorites(favorites([favorite()], 'restarted')); await h.refresh();
  assert.equal(opener.isConnected, false); assert.equal(favoriteAction(h, 'rename').disabled, false);
  await h.ids.get('favorite-cancel').fire('click'); assert.equal(h.focused(), h.ids.get('add-favorite')); assert.equal(h.posts().length, 0);
});
test('logical preset focus falls back to Presets after verified rename or deletion disables its opener', async () => {
  for (const mode of ['rename', 'delete']) {
    const h = await appHarness(); await selectPreset(h, revisionID('c')); const opener = h.ids.get('dsp-' + mode);
    await opener.fire('click'); h.ids.get('dsp-name').value = 'Renamed';
    h.setPost(() => {const presets = editor().presets.filter(preset => preset.id !== revisionID('c')); if (mode === 'rename') presets.push({id: revisionID('f'), name: 'Renamed', kind: 'saved', shared: false}); h.setState(soundState(editor({revision: revisionID('e'), presets}))); return {request_acknowledged: true};});
    await h.ids.get('dsp-preset-form').fire('submit');
    assert.equal(opener.disabled, true); assert.equal(h.ids.get('dsp-dialog').open, false); assert.equal(h.ids.get('dsp-preset').disabled, false);
    assert.equal(h.focused(), h.ids.get('dsp-preset')); assert.equal(h.posts().length, 1);
  }
});
test('logical preset focus handles remote deletion before Cancel and Escape', async () => {
  for (const dismiss of ['cancel', 'escape']) {
    const h = await appHarness(); await selectPreset(h, revisionID('c')); await h.ids.get('dsp-rename').fire('click');
    h.setState(soundState(editor({revision: revisionID('e'), presets: editor().presets.filter(preset => preset.id !== revisionID('c'))}))); await h.refresh();
    await dismissDialog(h, focusDialogs[1], dismiss); assert.equal(h.focused(), h.ids.get('dsp-preset')); assert.equal(h.posts().length, 0);
  }
});
test('logical dialog focus falls back to enabled Refresh when failed readback disables opening controls', async () => {
  for (const kind of focusDialogs) for (const dismiss of ['close', 'cancel', 'escape']) {
    const h = await appHarness(); await h.ids.get(kind.open).fire('click'); h.setState({}); await h.refresh();
    assert.equal(h.ids.get(kind.open).disabled, true); assert.equal(h.ids.get('refresh').disabled, false);
    await dismissDialog(h, kind, dismiss); assert.equal(h.focused(), h.ids.get('refresh'), `${kind.dialog}: ${dismiss}`);
  }
});
test('logical dialog focus never chooses a disabled final fallback', async () => {
  const h = await appHarness(); await h.ids.get('playlist-load').fire('click'); h.setState({}); await h.refresh();
  h.ids.get('refresh').disabled = true; const before = h.focusCalls.length;
  await h.ids.get('playlist-close').fire('click'); assert.equal(h.focusCalls.length, before);
});
test('logical dialog focus ignores already-closed and queued old close events before or after reopen', async () => {
  for (const kind of focusDialogs) {
    const h = await appHarness(), dialog = h.ids.get(kind.dialog); dialog.close = () => {dialog.open = false;};
    await h.ids.get(kind.open).fire('click'); await h.ids.get(kind.close).fire('click'); h.ids.get('stop').focus(); const before = h.focusCalls.length;
    await h.ids.get(kind.close).fire('click'); await dialog.fire('close'); assert.equal(h.focusCalls.length, before); assert.equal(h.focused(), h.ids.get('stop'));
    await h.ids.get(kind.open).fire('click'); const current = h.focused(), reopened = h.focusCalls.length;
    await dialog.fire('close'); assert.equal(dialog.open, true); assert.equal(h.focusCalls.length, reopened); assert.equal(h.focused(), current);
    await h.ids.get(kind.cancel).fire('click'); assert.equal(h.focused(), h.ids.get(kind.open));
    h.ids.get('stop').focus(); const ended = h.focusCalls.length; await dialog.fire('close'); assert.equal(h.focusCalls.length, ended);
  }
});
test('logical dialog focus uses the new opening control after reopening a shared dialog', async () => {
  for (const [first, second, dialog, close] of [['add-favorite', 'save-current', 'favorite-dialog', 'favorite-close'], ['playlist-load', 'playlist-add', 'playlist-dialog', 'playlist-close'], ['dsp-save', 'dsp-rename', 'dsp-dialog', 'dsp-close']]) {
    const h = await appHarness(); await selectPreset(h, revisionID('c')); h.ids.get(dialog).close = () => {h.ids.get(dialog).open = false;};
    await h.ids.get(first).fire('click'); await h.ids.get(close).fire('click'); await h.ids.get(second).fire('click');
    const before = h.focusCalls.length; await h.ids.get(dialog).fire('close'); assert.equal(h.focusCalls.length, before);
    await h.ids.get(close).fire('click'); assert.equal(h.focused(), h.ids.get(second));
  }
});
test('logical dialog focus is suppressed on pagehide, including late close events and BFCache-style reopen', async () => {
  for (const kind of focusDialogs) for (const closed of [false, true]) {
    const h = await appHarness(), dialog = h.ids.get(kind.dialog); dialog.close = () => {dialog.open = false;};
    await h.ids.get(kind.open).fire('click'); if (closed) dialog.open = false;
    const before = h.focusCalls.length; h.pagehide(); h.pageshow(); await dialog.fire('close'); await h.refresh();
    assert.equal(h.focusCalls.length, before); assert.equal(dialog.open, false); assertPrivateDialogsCleared(h); assertExportCleared(h);
    await h.ids.get(kind.open).fire('click'); await h.ids.get(kind.close).fire('click'); assert.equal(h.focused(), h.ids.get(kind.open));
  }
});
test('logical dialog focus does not leave another open modal during dismissal', async () => {
  const h = await appHarness(); await h.ids.get('add-favorite').fire('click'); await h.ids.get('playlist-load').fire('click');
  const before = h.focusCalls.length, current = h.focused(); await h.ids.get('favorite-close').fire('click');
  assert.equal(h.focusCalls.length, before); assert.equal(h.focused(), current); assert.equal(h.ids.get('playlist-dialog').open, true);
});
test('logical dialog focus is never restored by a late acknowledged mutation after dismissal', async () => {
  for (const kind of privateDialogs) for (const navigation of [false, true]) {
    const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise);
    await privateDraft(h, kind, 'Submitted'); const submitting = h.ids.get(kind.form).fire('submit'); await h.settle();
    if (navigation) {h.pagehide(); h.pageshow();} else await h.ids.get(kind.dialog).fire('cancel');
    h.ids.get('stop').focus(); const before = h.focusCalls.length;
    h.setState(state({generation: 8})); h.setFavorites(favorites([{...favorite(), id: 'new-id', name: 'Submitted'}]));
    pending.resolve({request_acknowledged: true, favorite_id: 'new-id'}); await submitting; await h.refresh();
    assert.equal(h.focusCalls.length, before); assert.equal(h.focused(), h.ids.get('stop')); assert.equal(h.ids.get(kind.dialog).open, false); assert.equal(h.ids.get(kind.name).value, ''); assert.equal(h.ids.get(kind.status).textContent, ''); assert.equal(h.posts().length, 1);
  }
});
test('logical dialog focus is never restored by an old export response while a newer modal is open', async () => {
  const h = await appHarness(), pending = deferred(); h.setExport(() => pending.promise);
  const reading = h.ids.get('playlist-export').fire('click'); await h.settle(); await h.ids.get('export-close').fire('click'); await reading;
  h.setExport(null); await h.ids.get('playlist-export').fire('click'); const before = h.focusCalls.length, current = h.focused();
  pending.resolve(exportResponse(exportSummary())); await h.settle();
  assert.equal(h.focusCalls.length, before); assert.equal(h.focused(), current); assert.equal(h.ids.get('export-dialog').open, true);
});

test('logical dialog focus skips controls under hidden, inert or collapsed ancestors', async () => {
  for (const kind of focusDialogs) for (const blocked of ['hidden', 'inert', 'collapsed']) {
    const h = await appHarness(), opener = h.ids.get(kind.open);
    await opener.fire('click');
    // Explicit ancestry models these blockers without pretending to parse layout.
    const ancestor = new opener.constructor(blocked === 'collapsed' ? 'details' : 'section');
    h.ids.get('sound-panel').append(ancestor); ancestor.append(opener);
    if (kind.dialog === 'dsp-dialog') ancestor.append(h.ids.get('dsp-preset'));
    if (blocked !== 'collapsed') ancestor[blocked] = true;
    const before = h.focusCalls.length; await h.ids.get(kind.close).fire('click');
    assert.deepEqual(h.focusCalls.slice(before), [h.ids.get('refresh')], `${kind.dialog}: ${blocked}`);
  }
});
test('logical favorite focus skips a rebuilt matching action hidden with its list', async () => {
  const h = await appHarness(); await favoriteAction(h, 'rename').fire('click');
  h.setFavorites(favorites([{...favorite(), name: 'Renamed remotely'}])); await h.refresh(); h.ids.get('favorites-list').hidden = true;
  const before = h.focusCalls.length; await h.ids.get('favorite-cancel').fire('click');
  assert.deepEqual(h.focusCalls.slice(before), [h.ids.get('add-favorite')]); assert.equal(h.posts().length, 0);
});

/* Change URL is intentionally weaker than an exact saved-target readback. */
const targetACK = 'Target-change request acknowledged. The exact saved URL is not verified by this redacted view.';
const targetUNKNOWN = 'Target-change outcome unknown. The change may have taken effect. No retry was sent. The exact saved URL cannot be verified from this redacted view.';
const replacementURL = 'HTTPS://example.com:443/%2fMiXeD?token=PRIVATE%2b+&x=%41#Keep';
const targetBody = metadata => ({action: 'favorite_target', id: 'favorite-id', url: replacementURL, metadata});
async function targetDraft(h, metadata = 'keep', url = replacementURL) {
  await favoriteAction(h, 'target').fire('click');
  h.ids.get('favorite-url').value = url; h.ids.get('favorite-metadata').value = metadata;
}
test('target ACK remains acknowledged with redacted or unavailable readbacks; other commands are unchanged', async () => {
  for (const failed of ['none', 'favorites', 'state', 'both']) {
    const h = harness(); h.setFavorites(favorites([favorite()])); await h.controller.refresh();
    if (['favorites', 'both'].includes(failed)) h.hooks.favorites = () => {throw new Error('offline');};
    if (['state', 'both'].includes(failed)) h.hooks.state = () => {throw new Error('offline');};
    const result = await h.controller.mutate(targetBody('keep'), 'Change URL');
    assert.equal(result.kind, 'acknowledged'); assert.equal(h.controller.notice.kind, 'acknowledged'); assert.ok(result.text.startsWith(targetACK));
    if (failed !== 'none') assert.match(result.text, /favorites list could not be refreshed/);
    if (['state', 'both'].includes(failed)) assert.match(result.text, /Current renderer state is unavailable/);
    await h.controller.refresh(); assert.equal(h.controller.notice.kind, 'acknowledged'); assert.equal(h.posts().length, 1);
  }
  const h = harness(); await h.controller.refresh(); await h.controller.mutate({action: 'favorite_rename', id: 'favorite-id', name: 'Renamed'}, 'Rename favorite');
  assert.equal(h.controller.notice.kind, 'confirmed');
});
test('target lost, malformed, error-after-apply and committed responses remain sticky private-safe UNKNOWN', async () => {
  for (const reply of [null, {status: 200, body: {ok: true}}, {status: 502, body: {outcome: 'ERROR', message: replacementURL}}, {status: 503, body: {outcome: 'UNKNOWN', committed: true, message: replacementURL}}]) {
    const h = harness(); await h.controller.refresh(); h.hooks.post = () => {if (!reply) throw new Error(replacementURL); return reply;};
    const result = await h.controller.mutate(targetBody('clear'), 'Change URL');
    assert.equal(result.kind, 'unknown'); assert.equal(result.text, targetUNKNOWN); assert.equal(h.controller.notice.text, targetUNKNOWN);
    await h.controller.refresh(); await h.controller.refresh(); assert.equal(h.controller.notice.kind, 'unknown'); assert.equal(h.controller.unknownOutcomes.length, 1);
    assert.equal(h.controller.unknownOutcomes[0].committed, reply?.body.committed === true); assert.equal(h.posts().length, 1);
    h.hooks.post = null; await h.controller.mutate({action: 'stop'}, 'Stop'); assert.equal(h.controller.unknownOutcomes.length, 1);
  }
});
test('Change URL is managed-only, safely named, initially blank and requires an explicit metadata policy', async () => {
  const h = await appHarness(); const safeName = '<img src=x onerror=bad()> station';
  h.setFavorites(favorites([{...favorite(), name: safeName}, {...favorite(), id: 'configured', editable: false}])); await h.refresh();
  assert.equal(favoriteAction(h, 'target', 'configured'), undefined);
  assert.equal(favoriteAction(h, 'target').textContent, 'Change URL'); await favoriteAction(h, 'target').fire('click');
  assert.equal(h.ids.get('favorite-delete-name').textContent, safeName); assert.equal(h.ids.get('favorite-delete-name').children.length, 0);
  assert.equal(h.ids.get('favorite-name-field').hidden, true); assert.equal(h.ids.get('favorite-name').required, false);
  assert.equal(h.ids.get('favorite-url').value, ''); assert.equal(h.ids.get('favorite-url').required, true);
  assert.equal(h.ids.get('favorite-metadata').value, ''); assert.equal(h.ids.get('favorite-metadata').required, true);
  assert.equal(h.ids.get('favorite-metadata').disabled, false); assert.equal(h.focused(), h.ids.get('favorite-url'));
  assert.match(h.ids.get('favorite-dialog-hint').textContent, /future playback.*name and ID.*Does not fetch audio or change current playback/);
  await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0);
  h.ids.get('favorite-url').value = replacementURL;
  for (const policy of ['', 'auto']) {
    h.ids.get('favorite-metadata').value = policy; await h.ids.get('favorite-form').fire('submit');
    assert.equal(h.posts().length, 0); assert.match(h.ids.get('favorite-dialog-status').textContent, /Choose whether/); assert.equal(h.ids.get('favorite-url').value, replacementURL);
  }
});
test('Change URL retains invalid private drafts without sending and enforces UTF-8 byte and request bounds', async () => {
  for (const url of ['', ' https://example.com/a', 'https://user:pass@example.com/a', 'https://example.com/\nprivate', 'https://example.com/' + 'x'.repeat(8192), 'https://example.com/' + 'é'.repeat(4100)]) {
    const h = await appHarness(); await targetDraft(h, 'keep', url); await h.ids.get('favorite-form').fire('submit');
    assert.equal(h.posts().length, 0); assert.equal(h.ids.get('favorite-url').value, url); assert.equal(h.ids.get('favorite-url').disabled, false);
  }
  const h = await appHarness(), id = 'x'.repeat(MAX_REQUEST_BYTES); h.setFavorites(favorites([{...favorite(), id}])); await h.refresh();
  await favoriteAction(h, 'target', id).fire('click'); h.ids.get('favorite-url').value = replacementURL; h.ids.get('favorite-metadata').value = 'clear'; await h.ids.get('favorite-form').fire('submit');
  assert.equal(h.posts().length, 0); assert.equal(h.ids.get('favorite-url').value, replacementURL); assert.match(h.ids.get('favorite-dialog-status').textContent, /32,768/);
});
test('Change URL sends exact URL bytes and explicit keep or clear once, immediately clearing its input', async () => {
  for (const policy of ['keep', 'clear']) for (const readback of [favorite(), {...favorite(), title: 'New title', target_label: 'elsewhere.example.test'}, null]) {
    const h = await appHarness(), wait = deferred(); h.setPost(() => wait.promise); await targetDraft(h, policy);
    const sending = h.ids.get('favorite-form').fire('submit');
    assert.equal(h.ids.get('favorite-url').value, ''); assert.equal(h.ids.get('favorite-url').disabled, true); assert.equal(h.ids.get('favorite-metadata').disabled, true);
    assert.equal(h.ids.get('favorite-metadata').value, policy); assert.equal(h.ids.get('favorite-cancel').textContent, 'Done'); assert.equal(h.focused(), h.ids.get('favorite-cancel'));
    await h.ids.get('favorite-form').fire('submit'); assert.deepEqual(h.posts(), [targetBody(policy)]);
    h.setFavorites(favorites(readback ? [readback] : [])); await h.refresh(); assert.match(h.ids.get('favorite-dialog-status').textContent, /^PENDING/);
    wait.resolve({request_acknowledged: true}); await sending;
    assert.equal(h.ids.get('favorite-dialog').open, true); assert.equal(h.ids.get('favorite-dialog-status').textContent, 'ACKNOWLEDGED · ' + targetACK);
    assert.equal(h.ids.get('notice-kind').textContent, 'ACKNOWLEDGED'); assert.equal(h.ids.get('favorite-submit').disabled, true);
    await h.refresh(); await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1);
    assert.ok(h.requests.every(item => ['/orr/v1/web/state', '/orr/v1/web/favorites', '/orr/v1/web/command'].includes(item.url)));
    assert.equal(h.ids.get('favorite-close').disabled, false); assert.equal(h.ids.get('favorite-cancel').disabled, false);
  }
});
test('Change URL distinguishes ACK from failed favorites or state reread and never becomes CONFIRMED', async () => {
  for (const failed of ['favorites', 'state']) {
    const h = await appHarness(); await targetDraft(h);
    h.setPost(() => {if (failed === 'favorites') h.setFavorites({}); else h.setState({}); return {request_acknowledged: true};});
    await h.ids.get('favorite-form').fire('submit');
    assert.equal(h.ids.get('notice-kind').textContent, 'ACKNOWLEDGED'); assert.match(h.ids.get('favorite-dialog-status').textContent, /^ACKNOWLEDGED.*favorites list could not be refreshed/);
    if (failed === 'state') assert.match(h.ids.get('favorite-dialog-status').textContent, /Current renderer state is unavailable/);
    h.setState(state()); h.setFavorites(favorites([favorite()])); await h.refresh(); await h.ids.get('favorite-form').fire('submit');
    assert.equal(h.ids.get('notice-kind').textContent, 'ACKNOWLEDGED'); assert.equal(h.posts().length, 1);
  }
});
test('Change URL rejection and UNKNOWN keep private-safe results, disable retries, and require a deliberate reopen', async () => {
  for (const reply of [null, {}, {__status: 503, outcome: 'UNKNOWN', committed: true, message: replacementURL}, {__status: 409, outcome: 'REJECTED', message: replacementURL}]) {
    const h = await appHarness(); await targetDraft(h); h.setPost(() => {h.setFavorites(favorites([{...favorite(), target_label: 'changed.example.test'}])); if (!reply) throw new Error(replacementURL); return {...reply};});
    await h.ids.get('favorite-form').fire('submit'); const message = h.ids.get('favorite-dialog-status').textContent;
    assert.match(message, reply?.outcome === 'REJECTED' ? /^ERROR.*request rejected/ : /^UNKNOWN.*may have taken effect.*No retry.*cannot be verified/);
    assert.doesNotMatch(message, /PRIVATE|This favorite changed/); assert.equal(h.ids.get('favorite-url').value, '');
    await h.refresh(); await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 1); assert.equal(h.ids.get('favorite-dialog-status').textContent, message);
    await h.ids.get('favorite-cancel').fire('click'); await favoriteAction(h, 'target').fire('click');
    assert.equal(h.ids.get('favorite-url').value, ''); assert.equal(h.ids.get('favorite-url').disabled, false); assert.equal(h.ids.get('favorite-metadata').value, '');
    assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  }
});
test('unsent Change URL drafts latch observed changes or removal across restart and reverted observations', async () => {
  for (const change of [null, {name: 'Remote'}, {editable: false}, {title: 'Remote'}, {artist: 'Remote'}, {album: 'Remote'}, {artwork: 'https://example.test/a.jpg'}, {target_label: 'new.example.test'}]) for (const restart of [false, true]) {
    const h = await appHarness(); await targetDraft(h, 'clear'); const session = restart ? 'restarted' : 'session-one';
    if (restart) h.setState(state({session_id: session}));
    h.setFavorites(favorites(change ? [{...favorite(), ...change}] : [], session)); await h.refresh();
    assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('favorite-url').value, replacementURL); assert.equal(h.ids.get('favorite-metadata').value, 'clear');
    assert.match(h.ids.get('favorite-dialog-status').textContent, /review the favorites list before reopening Change URL/);
    h.setFavorites(favorites([favorite()], session)); await h.refresh(); await h.ids.get('favorite-form').fire('submit'); assert.equal(h.posts().length, 0);
  }
});
test('unchanged Change URL record and unrelated rows remain allowed after restart; mixed sessions cannot invalidate a draft', async () => {
  const h = await appHarness(); await targetDraft(h);
  h.setFavorites(favorites([], 'mixed-session')); await h.refresh(); assert.equal(h.ids.get('favorite-submit').disabled, true); assert.equal(h.ids.get('favorite-dialog-status').textContent, '');
  h.setState(state({session_id: 'restarted'})); h.setFavorites(favorites([{...favorite(), id: 'other-id'}, Object.fromEntries(Object.entries(favorite()).reverse())], 'restarted')); await h.refresh();
  assert.equal(h.ids.get('favorite-submit').disabled, false); assert.equal(h.ids.get('favorite-url').value, replacementURL);
  await h.ids.get('favorite-form').fire('submit'); assert.deepEqual(h.posts(), [targetBody('keep')]);
});
test('Change URL Close, Cancel and Escape clear private drafts and restore logical row focus', async () => {
  for (const dismiss of ['close', 'cancel', 'escape']) {
    const h = await appHarness(); await targetDraft(h); h.setFavorites(favorites([{...favorite(), name: 'Remote rename'}])); await h.refresh();
    const expected = favoriteAction(h, 'target'); await dismissDialog(h, focusDialogs[0], dismiss);
    assert.equal(h.ids.get('favorite-dialog').open, false); assert.equal(h.ids.get('favorite-url').value, ''); assert.equal(h.ids.get('favorite-metadata').value, '');
    assert.equal(h.focused(), expected); assert.equal(h.posts().length, 0);
  }
});
test('Change URL pagehide clears drafts without restoring focus; late replies never clear a newer opening', async () => {
  for (const hide of [false, true]) for (const lost of [false, true]) {
    const h = await appHarness(), first = deferred(), stop = deferred(); await targetDraft(h); h.setPost(body => body.action === 'stop' ? stop.promise : first.promise);
    const sending = h.ids.get('favorite-form').fire('submit'); await h.settle();
    if (hide) {const count = h.focusCalls.length; h.pagehide(); assert.equal(h.focusCalls.length, count); h.pageshow();}
    else await h.ids.get('favorite-close').fire('click');
    assert.equal(h.ids.get('favorite-url').value, ''); assert.equal(h.ids.get('favorite-metadata').value, '');
    // Stop supersedes the outcome but leaves the first operation in flight.
    const stopping = h.ids.get('stop').fire('click'); stop.resolve({request_acknowledged: true}); await stopping;
    // Controller busy correctly blocks reopening until the original command ends.
    await favoriteAction(h, 'target').fire('click'); assert.equal(h.ids.get('favorite-dialog').open, false);
    h.ids.get('stop').focus(); const count = h.focusCalls.length;
    if (lost) first.reject(new Error('lost')); else first.resolve({request_acknowledged: true}); await sending;
    assert.equal(h.focusCalls.length, count); assert.equal(h.ids.get('favorite-dialog-status').textContent, ''); assert.equal(h.ids.get('favorite-dialog').open, false);
    await targetDraft(h, 'clear', 'https://example.com/new?private=new'); await h.ids.get('favorite-dialog').fire('close');
    assert.equal(h.ids.get('favorite-dialog').open, true); assert.equal(h.ids.get('favorite-url').value, 'https://example.com/new?private=new'); assert.equal(h.ids.get('favorite-metadata').value, 'clear');
    assert.deepEqual(h.posts().map(body => body.action), ['favorite_target', 'stop']); assert.match(h.ids.get('notice-text').textContent, /^Stop:/);
  }
});
test('Change URL Done restores the rebuilt row or a safe fallback without changing ACK outcome', async () => {
  for (const readback of ['changed', 'deleted', 'restarted', 'failed']) {
    const h = await appHarness(); await targetDraft(h);
    h.setPost(() => {
      if (readback === 'changed') h.setFavorites(favorites([{...favorite(), target_label: 'new.example.test'}]));
      if (readback === 'deleted') h.setFavorites(favorites([]));
      if (readback === 'restarted') {h.setState(state({session_id: 'restarted'})); h.setFavorites(favorites([favorite()], 'restarted'));}
      if (readback === 'failed') h.setState({});
      return {request_acknowledged: true};
    });
    await h.ids.get('favorite-form').fire('submit'); assert.equal(h.ids.get('favorite-dialog').open, true); assert.match(h.ids.get('favorite-dialog-status').textContent, /^ACKNOWLEDGED/);
    await h.ids.get('favorite-cancel').fire('click');
    const expected = readback === 'changed' ? favoriteAction(h, 'target') : h.ids.get(readback === 'failed' ? 'refresh' : 'add-favorite');
    assert.equal(h.focused(), expected); assert.equal(h.ids.get('favorite-dialog-status').textContent, ''); assert.equal(h.posts().length, 1);
  }
});
test('Change URL pending outcome cannot replace or resend a reopened draft after an old queued close', async () => {
  const h = await appHarness(), wait = deferred(); await targetDraft(h); h.setPost(() => wait.promise);
  const sending = h.ids.get('favorite-form').fire('submit'); await h.settle();
  const dialog = h.ids.get('favorite-dialog'); dialog.close = () => {dialog.open = false;};
  await h.ids.get('favorite-cancel').fire('click'); wait.resolve({request_acknowledged: true}); await sending;
  await targetDraft(h, 'clear', 'https://example.com/new?new=private'); const before = h.focusCalls.length;
  await dialog.fire('close'); await h.refresh();
  assert.equal(h.focusCalls.length, before); assert.equal(dialog.open, true); assert.equal(h.ids.get('favorite-url').value, 'https://example.com/new?new=private');
  assert.equal(h.ids.get('favorite-metadata').value, 'clear'); assert.equal(h.ids.get('favorite-dialog-status').textContent, ''); assert.equal(h.posts().length, 1);
});

test('saved-favorites diagnostic retains stale rows without changing transport, freshness or read timing', async () => {
  const h = harness(); h.setFavorites(favorites([favorite()])); await h.controller.refresh();
  const rows = h.controller.favorites, calls = h.calls.length;
  h.hooks.favorites = favoritesUnavailable; await h.controller.refresh();
  assert.equal(h.controller.favoritesReadFailure, 'favorites_unavailable');
  assert.equal(h.controller.favorites, rows); assert.equal(h.controller.favoritesFresh, false);
  assert.equal(h.controller.fresh, true); assert.equal(h.controller.canMutate('play'), true); assert.equal(h.controller.canMutate('stop'), true);
  for (const action of ['favorite_add', 'favorite_current', 'favorite_play', 'favorite_rename', 'favorite_target', 'favorite_delete']) assert.equal(h.controller.canMutate(action), false);
  assert.deepEqual(h.calls.slice(calls).map(call => [call.path, call.options.method]), [['/orr/v1/web/state', 'GET'], ['/orr/v1/web/favorites', 'GET']]);
  assert.equal(h.posts().length, 0);
  h.hooks.favorites = undefined; await h.controller.refresh();
  assert.equal(h.controller.favoritesReadFailure, null); assert.equal(h.controller.favoritesFresh, true);
});
test('saved-favorites diagnostic requires the exact status, envelope and fixed message, and clears on other failures', async () => {
  const good = favoritesUnavailable();
  const invalid = [
    ...[200, 400, 500, 502, '503'].map(status => ({...good, status})),
    ...['REJECTED', 'UNKNOWN', 'error', null].map(outcome => ({...good, body: {...good.body, outcome}})),
    ...['library_unavailable', 'read_failed', '', null].map(error => ({...good, body: {...good.body, error}})),
    ...['https://example.test/private?token=secret /private/favorites.json', '', null].map(message => ({...good, body: {...good.body, message}})),
    ...[null, [], {}, {error: 'favorites_unavailable', outcome: 'ERROR'}].map(body => ({...good, body})),
    ...[{favorites: [favorite()]}, {session_id: 'mixed-session'}, {version: 1}, {committed: true}, {effect: 'LIBRARY_COMMITTED_DURABILITY_UNKNOWN'}].map(extra => ({...good, body: {...good.body, ...extra}})),
    {status: 200, body: favorites([favorite()], 'mixed-session')}
  ];
  for (const response of invalid) {
    const h = harness(); h.setFavorites(favorites([favorite()])); await h.controller.refresh();
    h.hooks.favorites = favoritesUnavailable; await h.controller.refresh(); assert.equal(h.controller.favoritesReadFailure, 'favorites_unavailable');
    h.hooks.favorites = () => clone(response); await h.controller.refresh();
    assert.equal(h.controller.favoritesReadFailure, null, JSON.stringify(response)); assert.equal(h.controller.favoritesFresh, false);
    assert.deepEqual(h.controller.favorites, [favorite()]); assert.equal(h.controller.fresh, true);
  }
  const h = harness(); h.hooks.favorites = favoritesUnavailable; await h.controller.refresh();
  h.hooks.favorites = () => {throw new Error('private network failure');}; await h.controller.refresh();
  assert.equal(h.controller.favoritesReadFailure, null); assert.equal(h.controller.favoritesFresh, false);
});
test('saved-favorites diagnostic cannot survive invalid or older state reads', async () => {
  for (const failure of [() => ({status: 200, body: {}}), () => ({status: 503, body: favoritesUnavailable().body}), () => ({status: 200, body: state({revision: 0})}), () => {throw new Error('offline');}]) {
    const h = harness(); h.hooks.favorites = favoritesUnavailable; await h.controller.refresh();
    assert.equal(h.controller.favoritesReadFailure, 'favorites_unavailable');
    h.hooks.state = failure; await h.controller.refresh();
    assert.equal(h.controller.favoritesReadFailure, null); assert.equal(h.controller.favoritesFresh, false); assert.equal(h.controller.fresh, false);
  }
});
test('superseded saved-favorites diagnostic never resurfaces after a command or successful recovery', async () => {
  const h = harness(); h.setFavorites(favorites([favorite()])); await h.controller.refresh();
  const late = deferred(); let reads = 0; const observed = [];
  h.controller.onChange = control => observed.push(control.favoritesReadFailure);
  h.hooks.favorites = () => ++reads === 1 ? late.promise : {status: 200, body: favorites([favorite()])};
  const reading = h.controller.refresh(); await tick();
  const stopping = h.controller.mutate({action: 'stop'}, 'Stop'); await tick();
  late.resolve(favoritesUnavailable()); await Promise.all([reading, stopping]);
  assert.equal(h.controller.favoritesReadFailure, null); assert.equal(h.controller.favoritesFresh, true);
  assert.ok(observed.every(value => value === null)); assert.equal(h.posts().length, 1); assert.equal(reads, 2);
});
test('saved-favorites diagnostic stays read-only and does not turn mutation uncertainty into rejection', async () => {
  const h = harness(); await h.controller.refresh();
  h.hooks.post = () => ({status: 502, body: {error: 'command_failed', outcome: 'UNKNOWN', message: 'Command outcome is unknown. Refresh before any explicit retry.'}});
  h.hooks.favorites = favoritesUnavailable;
  const result = await h.controller.mutate({action: 'favorite_add', name: 'Radio', url: 'https://example.test/live'}, 'Add favorite');
  assert.equal(result.kind, 'unknown'); assert.equal(h.controller.notice.kind, 'unknown'); assert.equal(h.controller.unknownOutcomes.length, 1);
  assert.equal(h.controller.favoritesReadFailure, 'favorites_unavailable'); assert.equal(h.posts().length, 1);
});
test('app gives fixed saved-favorites read guidance while stale rows and ordinary playback controls stay intact', async () => {
  const h = await appHarness(), rows = h.ids.get('favorites-list').children.slice(), before = h.requests.length;
  h.setFavorites({__status: 503, ...favoritesUnavailable().body}); await h.refresh();
  assert.equal(h.ids.get('favorites-note').textContent, 'A favorites read reported that the managed library could not be loaded. This list may be stale. Stop the renderer service, repair or restore its saved favorites library, then restart it.');
  assert.deepEqual(h.ids.get('favorites-list').children, rows); assert.equal(h.ids.get('favorites-count').textContent, '1'); assert.equal(h.ids.get('favorites-empty').hidden, true);
  for (const button of h.ids.get('favorites-list').querySelectorAll('button')) assert.equal(button.disabled, true);
  for (const id of ['add-favorite', 'save-current']) assert.equal(h.ids.get(id).disabled, true);
  for (const id of ['play', 'stop', 'volume', 'mute']) assert.equal(h.ids.get(id).disabled, false);
  assert.equal(h.ids.get('connection-label').textContent, 'Connected'); assert.equal(h.posts().length, 0);
  assert.deepEqual(h.requests.slice(before).map(item => [item.url, item.options.method]), [['/orr/v1/web/state', 'GET'], ['/orr/v1/web/favorites', 'GET']]);
  assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [2000]);
  h.setState(state({session_id: 'restarted'})); await h.refresh();
  // The error envelope has no session identity: wording reports the failed read,
  // while fresh library controls still require a matching successful snapshot.
  assert.match(h.ids.get('favorites-note').textContent, /^A favorites read reported/); assert.equal(h.ids.get('add-favorite').disabled, true);
  h.setFavorites(favorites([{...favorite(), name: 'Recovered'}], 'restarted')); await h.refresh();
  assert.equal(h.ids.get('favorites-note').textContent, 'Saved on this renderer. Configuration favorites are read-only.');
  assert.equal(h.ids.get('add-favorite').disabled, false); assert.equal(h.ids.get('save-current').disabled, false);
  for (const button of h.ids.get('favorites-list').querySelectorAll('button')) assert.equal(button.disabled, false);
});
test('app clears saved-favorites advice on network, malformed, private-message, mixed-session and offline failures', async () => {
  const privateText = 'private /local/favorites.json https://example.test/?token=secret';
  const variants = [
    () => {throw new Error(privateText);}, {}, [],
    {__status: 502, ...favoritesUnavailable().body},
    {__status: 503, ...favoritesUnavailable().body, outcome: 'UNKNOWN'},
    {__status: 503, ...favoritesUnavailable().body, message: privateText},
    {__status: 503, ...favoritesUnavailable().body, favorites: [favorite()]},
    {__status: 503, ...favoritesUnavailable().body, session_id: 'mixed-session'},
    favorites([favorite()], 'mixed-session')
  ];
  for (const response of variants) {
    const h = await appHarness(); h.setFavorites({__status: 503, ...favoritesUnavailable().body}); await h.refresh();
    assert.match(h.ids.get('favorites-note').textContent, /repair or restore/);
    h.setFavorites(response); await h.refresh();
    assert.equal(h.ids.get('favorites-note').textContent, 'Favorites could not be refreshed. This list may be stale.');
    assert.equal(h.ids.get('add-favorite').disabled, true); assert.equal(h.ids.get('play').disabled, false); assert.equal(h.posts().length, 0);
    for (const element of h.ids.values()) assert.doesNotMatch(element.textContent, /private|token=secret|local\/favorites/);
  }
  const h = await appHarness(); h.setFavorites({__status: 503, ...favoritesUnavailable().body}); await h.refresh(); h.offline();
  assert.equal(h.ids.get('favorites-note').textContent, 'Favorites could not be refreshed. This list may be stale.');
  assert.equal(h.ids.get('play').disabled, true); assert.equal(h.ids.get('stop').disabled, false);
});

/* Room rename: typed-name and dialog control-flow, not browser acceptance. */
const renamedDevice = (name = 'Küche 🔊 %2F%2525', visible = true) => ({name, revision: revisionID('2'), name_visible: visible});
test('room rename sends one literal typed request and waits for ACK plus canonical name identity', async () => {
  const h = await appHarness(), wait = deferred(); h.setPost(() => wait.promise);
  await h.ids.get('rename-room').fire('click');
  assert.equal(h.focused(), h.ids.get('room-name')); assert.equal(h.ids.get('room-name').value, 'Living room');
  h.ids.get('room-name').value = 'Küche 🔊 %2F%2525';
  const submitting = h.ids.get('room-form').fire('submit'); await h.settle();
  assert.equal(h.ids.get('device-name').textContent, 'Living room'); assert.equal(h.ids.get('room-submit').disabled, true);
  await h.ids.get('room-form').fire('submit'); assert.deepEqual(h.posts(), [{action: 'device_rename', name: 'Küche 🔊 %2F%2525', expected_device: revisionID('1')}]);
  h.setState(state({device: renamedDevice()})); await h.refresh();
  assert.match(h.ids.get('room-dialog-status').textContent, /^PENDING/); assert.equal(h.ids.get('room-dialog').open, true);
  wait.resolve({request_acknowledged: true, device_revision: revisionID('2')}); await submitting;
  assert.equal(h.ids.get('room-dialog').open, false); assert.equal(h.ids.get('room-name').value, '');
  assert.equal(h.focused(), h.ids.get('rename-room')); assert.equal(h.ids.get('notice-kind').textContent, 'CONFIRMED');
});
test('room rename stale session or name is latched while unchanged polling and read loss preserve drafts', async () => {
  for (const changed of [state({session_id: 'restarted'}), state({device: renamedDevice('Remote rename')})]) {
    const h = await appHarness(); await h.ids.get('rename-room').fire('click'); h.ids.get('room-name').value = 'Draft';
    h.offline(); assert.equal(h.ids.get('room-submit').disabled, true); await h.ids.get('room-form').fire('submit'); assert.equal(h.posts().length, 0);
    await h.refresh(); assert.equal(h.ids.get('room-submit').disabled, false); assert.equal(h.ids.get('room-name').value, 'Draft');
    h.setState(changed); await h.refresh(); assert.equal(h.ids.get('room-submit').disabled, true); assert.match(h.ids.get('room-dialog-status').textContent, /name changed or the renderer restarted/);
    h.setState(state()); await h.refresh(); h.ids.get('room-submit').disabled = false;
    await h.ids.get('room-form').fire('submit'); assert.equal(h.posts().length, 0); assert.equal(h.ids.get('room-submit').disabled, true);
  }
});
test('room rename requires matching same-session canonical identity even after a successful ACK', async () => {
  for (const outcome of ['wrong-name', 'missing-token', 'new-session', 'failed-read', 'hidden-name']) {
    const h = await appHarness(); await h.ids.get('rename-room').fire('click'); h.ids.get('room-name').value = 'Requested name';
    h.setPost(() => {
      h.setState(outcome === 'failed-read' ? {} : state({session_id: outcome === 'new-session' ? 'new-session' : 'session-one', device: outcome === 'wrong-name' ? state().device : renamedDevice(outcome === 'hidden-name' ? 'Open Room Renderer' : 'Requested name', outcome !== 'hidden-name')}));
      return {request_acknowledged: true, ...(outcome === 'missing-token' ? {} : {device_revision: revisionID('2')})};
    });
    await h.ids.get('room-form').fire('submit');
    if (outcome === 'hidden-name') {
      assert.equal(h.ids.get('room-dialog').open, false); await h.ids.get('rename-room').fire('click');
      assert.equal(h.ids.get('room-name').value, ''); assert.match(h.ids.get('room-dialog-status').textContent, /current name is hidden/);
    } else {
      assert.equal(h.ids.get('room-dialog').open, true); assert.equal(h.ids.get('room-submit').disabled, true);
      assert.match(h.ids.get('room-dialog-status').textContent, /current name has not been verified/); assert.notEqual(h.ids.get('notice-kind').textContent, 'CONFIRMED');
      await h.ids.get('room-form').fire('submit'); assert.equal(h.posts().length, 1);
    }
  }
});
test('room rename committed error and lost response remain UNKNOWN despite changed name and later requests', async () => {
  for (const fault of ['committed', 'lost', 'rejected']) {
    const h = await appHarness(); await h.ids.get('rename-room').fire('click'); h.ids.get('room-name').value = 'Changed';
    h.setPost(() => {
      h.setState(state({device: renamedDevice('Changed')}));
      if (fault === 'lost') throw new Error('https://private.invalid/?token=DO_NOT_ECHO');
      return {__status: fault === 'rejected' ? 409 : 502, outcome: fault === 'rejected' ? 'REJECTED' : 'UNKNOWN', committed: fault === 'committed', message: 'https://private.invalid/?token=DO_NOT_ECHO'};
    });
    await h.ids.get('room-form').fire('submit'); assert.equal(h.ids.get('room-dialog').open, true); assert.equal(h.ids.get('room-submit').disabled, true);
    assert.match(h.ids.get('room-dialog-status').textContent, fault === 'rejected' ? /^ERROR/ : /^UNKNOWN/);
    assert.doesNotMatch(h.ids.get('room-dialog-status').textContent + h.ids.get('notice-text').textContent, /DO_NOT_ECHO/);
    await h.refresh(); assert.match(h.ids.get('room-dialog-status').textContent, fault === 'rejected' ? /^ERROR/ : /^UNKNOWN/);
    await h.ids.get('room-form').fire('submit'); assert.equal(h.posts().length, 1);
    await h.ids.get('room-cancel').fire('click'); h.setPost(() => ({request_acknowledged: true})); await h.ids.get('stop').fire('click');
    if (fault !== 'rejected') assert.match(h.ids.get('notice-text').textContent, /Rename room \(persistence\/discovery unconfirmed\)/);
  }
});
test('room rename cancellation, Escape and navigation scrub drafts and late responses cannot dismiss a newer dialog', async () => {
  for (const dismiss of ['room-close', 'room-cancel', 'escape', 'pagehide']) {
    const h = await appHarness(); await h.ids.get('rename-room').fire('click'); h.ids.get('room-name').value = 'https://private.invalid/?token=LOCAL_ONLY';
    if (dismiss === 'escape') await h.ids.get('room-dialog').fire('cancel'); else if (dismiss === 'pagehide') h.pagehide(); else await h.ids.get(dismiss).fire('click');
    assert.equal(h.ids.get('room-dialog').open, false); assert.equal(h.ids.get('room-name').value, ''); assert.equal(h.posts().length, 0);
    if (dismiss !== 'pagehide') assert.equal(h.focused(), h.ids.get('rename-room'));
  }
  const h = await appHarness(), wait = deferred(); h.setPost(() => wait.promise);
  await h.ids.get('rename-room').fire('click'); h.ids.get('room-name').value = 'Submitted'; const submitting = h.ids.get('room-form').fire('submit'); await h.settle();
  await h.ids.get('room-cancel').fire('click'); assert.equal(h.ids.get('room-name').value, ''); assert.equal(h.focused(), h.ids.get('refresh'));
  h.setState(state({device: renamedDevice('Submitted')})); wait.resolve({request_acknowledged: true, device_revision: revisionID('2')}); await submitting;
  await h.ids.get('rename-room').fire('click'); h.ids.get('room-name').value = 'New draft'; await h.ids.get('room-dialog').fire('close');
  assert.equal(h.ids.get('room-dialog').open, true); assert.equal(h.ids.get('room-name').value, 'New draft'); assert.equal(h.posts().length, 1);
});

test('room rename leaves legacy whitespace normalization to the server, including U+FEFF', async () => {
  const h = await appHarness(); await h.ids.get('rename-room').fire('click');
  h.ids.get('room-name').value = ' \ufeff ';
  h.setPost(() => {h.setState(state({device: renamedDevice('\ufeff')})); return {request_acknowledged: true, device_revision: revisionID('2')};});
  await h.ids.get('room-form').fire('submit');
  assert.equal(h.posts()[0].name, ' \ufeff '); assert.equal(h.ids.get('room-dialog').open, false);
});

test('recovery view validates pending counters and rejects inconsistent state', () => {
  const recovery = {kind: 'station_retry', attempts_used: 1, attempt_limit: 3, message: 'Station recovery pending: 1 of 3 automatic retry attempts used.'};
  const transport = {phase: 'BUFFERING', intent: 'play', status: 'PENDING', message: 'Waiting for renderer confirmation.', recovery};
  assert.equal(validateState(state({transport})).transport.recovery.attempts_used, 1);
  for (const bad of [{...recovery, attempts_used: -1}, {...recovery, attempts_used: 4}, {...recovery, attempt_limit: 0}, {...recovery, attempts_used: true}, {...recovery, kind: 'unknown'}, {...recovery, message: {}}]) assert.throws(() => validateState(state({transport: {...transport, recovery: bad}})));
  for (const phase of ['PLAYING', 'STOPPED', 'PAUSED', 'ERROR']) assert.throws(() => validateState(state({transport: {...transport, phase}})));
  assert.throws(() => validateState(state({transport: {...transport, status: 'ERROR'}})));
  assert.throws(() => validateState(state({transport: {...transport, phase: 'ERROR', status: 'ERROR', recovery: {...recovery, kind: 'station_exhausted'}}})));
  for (const kind of ['station_exhausted', 'backend_control']) assert.equal(validateState(state({transport: {phase: 'ERROR', intent: 'play', status: 'ERROR', message: '', recovery: {kind, message: 'Fixed explanation'}}})).transport.recovery.kind, kind);
});
test('app recovery explanation is read-only and clears after success, selection or stale state', async () => {
  const h = await appHarness();
  const recovery = {kind: 'station_retry', attempts_used: 2, attempt_limit: 3, message: 'Station recovery pending: 2 of 3 automatic retry attempts used. Wait for confirmation, or choose Stop.'};
  const pending = state({transport: {phase: 'BUFFERING', intent: 'play', status: 'PENDING', message: 'Waiting for renderer confirmation.', recovery}});
  h.setState(pending); await h.refresh();
  const hint = h.ids.get('playback-recovery');
  assert.equal(hint.hidden, false); assert.equal(hint.textContent, recovery.message); assert.equal(hint.children.length, 0);
  assert.equal(h.ids.get('stop').disabled, false); assert.equal(h.posts().length, 0);
  h.offline(); assert.equal(hint.hidden, true); assert.equal(hint.textContent, '');
  await h.refresh(); assert.equal(hint.hidden, false);
  h.setState(state({generation: 8, transport: {phase: 'PLAYING', intent: 'play', status: 'CONFIRMED', message: '', recovery: null}})); await h.refresh();
  assert.equal(hint.hidden, true); assert.equal(hint.textContent, '');
  assert.equal(h.posts().length, 0);
});
test('recovery guidance never erases sticky unknown outcomes or sends a retry', async () => {
  const h = await appHarness(); h.setPost(() => {throw new Error('lost response');});
  await h.ids.get('play').fire('click'); await h.settle();
  assert.equal(h.posts().length, 1); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
  for (const kind of ['station_exhausted', 'backend_control']) {
    h.setState(state({transport: {phase: 'ERROR', intent: 'play', status: 'ERROR', message: 'Playback state is unconfirmed. Stop is available for recovery.', recovery: {kind, message: kind === 'station_exhausted' ? 'Automatic station recovery is exhausted. Choose Play to try again.' : 'Backend control failed. Playback and silence are unconfirmed.'}}, actions: {...state().actions, pause: false, next: false, previous: false, seek: false}}));
    await h.refresh();
    assert.equal(h.ids.get('playback-recovery').hidden, false);
    assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
    assert.match(h.ids.get('notice-text').textContent, /outcome unknown/i);
    assert.equal(h.ids.get('stop').disabled, false); assert.equal(h.ids.get('pause').disabled, true); assert.equal(h.ids.get('next').disabled, true);
    assert.equal(h.posts().length, 1);
  }
});

// Output inspection is a separate, explicit read workflow, never a playback mutation.
const {OutputInspection, validateOutputInspection, outputInspectionLines} = require('../fake_linkplay/web_assets/controller.js');
function outputReport(changes = {}) {
  return {version: 1, session_id: 'session-one', status: 'complete',
    configuration: {source: 'running_renderer', selection: 'explicit_output', driver_family: 'alsa', spawn_policy: 'auto_spawn_enabled', identifier: 'hidden'},
    connection: 'connected', socket_path_check: 'unchanged_at_checks',
    live_intent: {status: 'value', selection: 'explicit_output', driver_family: 'alsa', identifier: 'hidden'},
    driver: {status: 'value', family: 'alsa'}, comparison: 'matches', null_output: 'not_observed',
    observations: 'sequential_not_atomic', renderer_ownership: 'unverified', physical_device: 'not_verified', audible_output: 'not_verified', ...changes};
}
test('output report accepts only exact schemas and allowlisted classifications', () => {
  assert.deepEqual(validateOutputInspection(outputReport(), 'session-one'), outputReport());
  const invalid = [
    {version: 2}, {session_id: 'other-session'}, {secret: 'synthetic-secret'}, {comparison: 'synthetic-secret'},
    {configuration: {...outputReport().configuration, identifier: '/tmp/synthetic-secret'}},
    {configuration: {...outputReport().configuration, source: 'on_disk'}},
    {configuration: {...outputReport().configuration, raw: 'https://example.test/?token=synthetic-secret'}},
    {live_intent: {...outputReport().live_intent, driver_family: 'alsa/synthetic-secret'}},
    {driver: {status: 'value', family: 'synthetic-secret'}}, {connection: 'synthetic-secret'},
    {socket_path_check: 'owned'}, {renderer_ownership: 'verified'}, {audible_output: 'verified'},
    {physical_device: 'verified'}, {observations: 'atomic'}, {null_output: 'probably_silent'}
  ];
  invalid.forEach(change => assert.throws(() => validateOutputInspection(outputReport(change), 'session-one')));
  for (const name of Object.keys(outputReport())) {const value = outputReport(); delete value[name]; assert.throws(() => validateOutputInspection(value, 'session-one'));}
});
test('output text explains incomplete, idle, changed, default, null and comparison limits', () => {
  const text = outputInspectionLines(outputReport({status: 'incomplete', connection: 'timed_out', socket_path_check: 'changed', comparison: 'not_compared', null_output: 'deliberately_silent', driver: {status: 'unavailable', family: 'not_observed'}})).join(' ');
  assert.match(text, /two-second inspection budget/); assert.match(text, /changed during inspection/); assert.match(text, /can be idle/);
  assert.match(text, /not compared/); assert.match(text, /deliberately silent/); assert.match(text, /does not send sound/); assert.match(text, /identifier hidden/);
  assert.match(outputInspectionLines(outputReport({comparison: 'differs'})).join(' '), /differs from/);
  assert.match(outputInspectionLines(outputReport({connection: 'socket_missing'})).join(' '), /socket missing/);
  assert.match(outputInspectionLines(outputReport({configuration: {...outputReport().configuration, selection: 'mpv_default', spawn_policy: 'external_only'}})).join(' '), /mpv default.*external player only/);
});
test('output inspection is explicit and deduplicated without changing playback state or notices', async () => {
  const h = harness(); await h.controller.refresh(); const pending = deferred(); const calls = [];
  const inspection = new OutputInspection(async (...args) => {calls.push(args); return pending.promise;});
  const before = clone({notice: h.controller.notice, state: h.controller.state, epoch: h.controller.epoch, operation: h.controller.operation});
  inspection.observe(h.controller.state); assert.equal(calls.length, 0);
  const first = inspection.inspect(h.controller.state);
  assert.equal(inspection.pending, true); assert.equal(h.controller.canMutate('stop'), true);
  await inspection.inspect(h.controller.state); assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], ['/orr/v1/web/output/inspect', {method: 'POST', timeoutMs: 4000, headers: {'X-ORR-CSRF': 'test-csrf-token'}, body: {action: 'inspect', expected_session: 'session-one'}}]);
  pending.resolve({status: 200, body: outputReport()}); await first;
  assert.equal(inspection.pending, false); assert.equal(inspection.result.comparison, 'matches');
  assert.deepEqual({notice: h.controller.notice, state: h.controller.state, epoch: h.controller.epoch, operation: h.controller.operation}, before);
  await h.controller.refresh(); assert.equal(calls.length, 1);
});
test('output errors are private, local and not retried or put in unknown command outcomes', async () => {
  for (const response of [new Error('synthetic-private-error'), {status: 503, body: {error: 'synthetic-private-error', message: 'synthetic-private-error', outcome: 'UNKNOWN'}}, {status: 200, body: outputReport({driver: {status: 'value', family: 'synthetic-private-error'}})}]) {
    let calls = 0;
    const inspection = new OutputInspection(async () => {calls += 1; if (response instanceof Error) throw response; return response;});
    await inspection.inspect(state());
    assert.equal(inspection.pending, false); assert.equal(inspection.result, null); assert.match(inspection.error, /No automatic retry/);
    assert.doesNotMatch(inspection.error, /synthetic-private-error/);
    inspection.observe(state({revision: 8})); await tick(); assert.equal(calls, 1);
  }
});
test('output dismissal and session changes discard late success or failure and allow explicit new inspection', async () => {
  for (const change of ['dismiss', 'restart']) for (const fail of [false, true]) {
    const pending = deferred(); let calls = 0;
    const inspection = new OutputInspection(async () => ++calls === 1 ? pending.promise : {status: 200, body: outputReport({session_id: change === 'restart' ? 'new-session' : 'session-one'})});
    const first = inspection.inspect(state());
    const latest = change === 'restart' ? state({session_id: 'new-session', csrf_token: 'new-token'}) : state();
    if (change === 'dismiss') inspection.dismiss(); else inspection.observe(latest);
    assert.equal(inspection.open, false); assert.equal(inspection.pending, false);
    await inspection.inspect(latest); assert.equal(inspection.result.session_id, latest.session_id);
    if (fail) pending.reject(new Error('old error')); else pending.resolve({status: 200, body: outputReport()});
    await first;
    assert.equal(inspection.result.session_id, latest.session_id); assert.equal(inspection.error, ''); assert.equal(calls, 2);
  }
});
test('output overlap and stale-session errors provide explicit retry or refresh guidance', async () => {
  for (const code of ['output_inspection_busy', 'stale_output_inspection', 'forbidden']) {
    let calls = 0;
    const inspection = new OutputInspection(async () => {calls += 1; return {status: 409, body: {error: code}};});
    await inspection.inspect(state());
    assert.match(inspection.error, code === 'output_inspection_busy' ? /No new inspection was started/ : /Refresh before inspecting/);
    assert.equal(calls, 1); assert.equal(inspection.result, null);
  }
});
test('inline output UI keeps Stop reachable and has an independent pending/result/error notice', async () => {
  const h = await appHarness(); const wait = deferred();
  h.setPost(body => body.action === 'inspect' ? wait.promise : {request_acknowledged: true});
  const inspecting = h.ids.get('inspect-output').fire('click'); await h.settle();
  assert.equal(h.posts().filter(body => body.action === 'inspect').length, 1);
  assert.equal(h.ids.get('inspect-output').disabled, true);
  assert.equal(h.ids.get('output-inspection-detail').hidden, false);
  assert.match(h.ids.get('output-inspection-status').textContent, /Inspecting output once/);
  for (const id of ['favorite-dialog', 'dsp-dialog', 'playlist-dialog', 'export-dialog', 'room-dialog']) assert.equal(h.ids.get(id).open, false);
  assert.equal(h.ids.get('stop').disabled, false); await h.ids.get('stop').fire('click');
  const notice = h.ids.get('notice-text').textContent; assert.match(notice, /^Stop:/);
  wait.resolve(outputReport({null_output: 'deliberately_silent', driver: {status: 'value', family: 'null'}})); await inspecting;
  assert.equal(h.ids.get('notice-text').textContent, notice);
  assert.equal(h.ids.get('inspect-output').disabled, false);
  assert.match(h.ids.get('output-inspection-results').children.map(item => item.textContent).join(' '), /deliberately silent/);
  const requests = h.requests.filter(item => item.url.endsWith('/output/inspect'));
  assert.equal(requests.length, 1); assert.equal(requests[0].options.credentials, 'same-origin'); assert.equal(requests[0].options.redirect, 'error'); assert.equal(requests[0].options.cache, 'no-store');
  assert.equal(requests[0].options.referrerPolicy, 'no-referrer'); assert.equal(requests[0].options.headers['X-ORR-CSRF'], 'test-csrf-token');
  await h.refresh(); assert.equal(h.posts().filter(body => body.action === 'inspect').length, 1);
  h.setPost(() => {throw new Error('synthetic-private-error');}); await h.ids.get('inspect-output').fire('click');
  assert.equal(h.ids.get('notice-text').textContent, notice); assert.match(h.ids.get('output-inspection-status').textContent, /No automatic retry/);
});
test('inline output dismissal, pagehide and session change scrub results and ignore late response', async () => {
  for (const dismiss of ['button', 'pagehide', 'session']) {
    const h = await appHarness(); const wait = deferred(); h.setPost(() => wait.promise);
    const inspecting = h.ids.get('inspect-output').fire('click'); await h.settle();
    if (dismiss === 'button') {await h.ids.get('dismiss-output-inspection').fire('click'); assert.equal(h.focused().id, 'inspect-output');}
    if (dismiss === 'pagehide') h.pagehide();
    if (dismiss === 'session') {h.setState(state({session_id: 'new-session', csrf_token: 'new-token'})); h.setFavorites(favorites([], 'new-session')); await h.refresh();}
    assert.equal(h.ids.get('output-inspection-detail').hidden, true);
    wait.resolve(outputReport()); await inspecting;
    assert.equal(h.ids.get('output-inspection-detail').hidden, true); assert.equal(h.ids.get('output-inspection-results').children.length, 0);
    assert.equal(h.ids.get('output-inspection-status').textContent, '');
    assert.equal(h.posts().filter(body => body.action === 'inspect').length, 1);
  }
});
test('output section is non-modal and rendered entirely as safe text', () => {
  const html = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/index.html'), 'utf8');
  assert.match(html, /<section class="card output-inspection-card"/); assert.doesNotMatch(html, /<dialog[^>]*output-inspection/);
  assert.match(html, /two observations are sequential, not atomic/); assert.match(html, /physical device and audible sound are unverified/);
  const app = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/app.js'), 'utf8');
  assert.match(app, /item\.textContent = line/);
});

// Explicit Refresh recovery is deliberately narrower than generic rejection.
const queueAdmissionRejection = () => ({__status: 409, error: 'unavailable', outcome: 'REJECTED', message: 'Requested operation is currently unavailable.'});
const queueRejectedGuidance = 'Queue edit rejected; nothing was changed. Review the queue, then choose Refresh to enable a new attempt. Refresh does not send an edit.';
const queueRecoveryActions = ['remove', 'up', 'down'];
const queueRecoveryRow = (h, direction) => playlistRows(h, direction)[direction === 'up' ? 1 : 0];
async function ordinaryQueuePoll(h) {
  const timer = [...h.timers.values()].find(item => item.delay === 2000);
  assert.ok(timer); await timer.callback(); await h.settle();
}
test('queue rejection recovery admits only the exact pre-effect response for Move and Remove', async () => {
  for (const action of ['playlist_remove', 'playlist_move', 'playlist_append', 'playlist_replace', 'play', 'stop']) {
    const h = harness(); await h.controller.refresh();
    h.hooks.post = () => {const body = queueAdmissionRejection(); delete body.__status; return {status: 409, body, rawBody: JSON.stringify(body)};};
    const result = await h.controller.mutate({action, expected_generation: 7}, 'Synthetic operation');
    assert.equal(result.queueEditRejected === true, ['playlist_remove', 'playlist_move'].includes(action));
    if (result.queueEditRejected) assert.equal(result.text, queueRejectedGuidance);
    assert.equal(h.posts().length, 1);
  }
  const h = harness();
  const blocked = await h.controller.mutate({action: 'playlist_remove', expected_generation: 7, index: 1}, 'Remove');
  assert.equal(blocked.kind, 'blocked'); assert.equal(blocked.queueEditRejected, undefined); assert.equal(h.posts().length, 0);
});
test('queue rejection recovery requires explicit Refresh then a separate new row choice for all three actions', async () => {
  for (const direction of queueRecoveryActions) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction);
    h.setPost(queueAdmissionRejection); await row.fire('click');
    assert.equal(row.disabled, true); assert.equal(h.ids.get('notice-text').textContent, queueRejectedGuidance);
    assert.equal(h.ids.get('connection-label').textContent, 'Connected');
    assert.equal(h.ids.get('playlist-add').disabled, false); assert.equal(h.posts().length, 1);
    await ordinaryQueuePoll(h); await ordinaryQueuePoll(h); await row.fire('click');
    assert.equal(row.disabled, true); assert.equal(h.posts().length, 1);
    const readsBefore = h.requests.filter(item => item.url.endsWith('/state')).length;
    await h.refresh();
    assert.ok(h.requests.filter(item => item.url.endsWith('/state')).length > readsBefore);
    assert.equal(queueRecoveryRow(h, direction), row); assert.equal(row.disabled, false);
    assert.equal(h.posts().length, 1); assert.match(h.ids.get('notice-text').textContent, /available again; no edit was sent/);
    assert.equal(playlistRows(h, 'up')[0].disabled, true); assert.equal(playlistRows(h, 'down')[1].disabled, true);
    h.setPost(() => ({request_acknowledged: true})); await row.fire('click');
    assert.equal(h.posts().length, 2); assert.deepEqual(h.posts()[1], h.posts()[0]);
    assert.equal(row.disabled, true); await h.refresh(); await row.fire('click'); assert.equal(h.posts().length, 2);
  }
});
test('queue rejection recovery requires a new review for each subsequent no-effect rejection', async () => {
  for (const direction of queueRecoveryActions) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction); h.setPost(queueAdmissionRejection);
    for (let count = 1; count <= 2; ++count) {
      await row.fire('click'); assert.equal(h.posts().length, count); assert.equal(row.disabled, true);
      await ordinaryQueuePoll(h); await row.fire('click'); assert.equal(h.posts().length, count);
      await h.refresh(); assert.equal(row.disabled, false); assert.equal(h.posts().length, count);
    }
  }
});
test('queue rejection recovery never admits UNKNOWN, ERROR, lost, malformed, ACK or contradictory envelopes', async () => {
  const cases = [
    ['UNKNOWN', () => ({...queueAdmissionRejection(), outcome: 'UNKNOWN'})],
    ['ERROR', () => ({...queueAdmissionRejection(), outcome: 'ERROR'})],
    ['lost', () => {throw new Error('synthetic-private-response');}],
    ['malformed', () => ({ok: true, message: 'synthetic-private-response'})],
    ['ACK', () => ({request_acknowledged: true})],
    ['committed', () => ({...queueAdmissionRejection(), committed: true})],
    ['not committed', () => ({...queueAdmissionRejection(), committed: false})],
    ['effect', () => ({...queueAdmissionRejection(), effect: 'LIBRARY_COMMITTED_DURABILITY_UNKNOWN'})],
    ['extra ACK', () => ({...queueAdmissionRejection(), request_acknowledged: true})],
    ['wrong status 200', () => ({...queueAdmissionRejection(), __status: 200})],
    ['wrong status 400', () => ({...queueAdmissionRejection(), __status: 400})],
    ['wrong error', () => ({...queueAdmissionRejection(), error: 'stale_generation'})],
    ['wrong message', () => ({...queueAdmissionRejection(), message: 'synthetic-private-response'})],
    ['missing message', () => ({__status: 409, error: 'unavailable', outcome: 'REJECTED'})],
    ['missing error', () => ({__status: 409, message: 'Requested operation is currently unavailable.', outcome: 'REJECTED'})],
    ['missing outcome', () => ({__status: 409, error: 'unavailable', message: 'Requested operation is currently unavailable.'})]
  ];
  for (const direction of queueRecoveryActions) for (const [name, reply] of cases) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction); h.setPost(reply);
    await row.fire('click'); await ordinaryQueuePoll(h); await h.refresh(); await row.fire('click');
    assert.equal(row.disabled, true, `${direction}: ${name}`); assert.equal(h.posts().length, 1, name);
    assert.doesNotMatch(h.ids.get('notice-text').textContent, /synthetic-private-response|nothing was changed|available again/);
    if (['UNKNOWN', 'ERROR', 'lost', 'malformed', 'missing message', 'missing outcome'].includes(name)) assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
  }
});
test('queue rejection recovery never re-arms a locally blocked row request', async () => {
  class LocallyBlocked extends Controller {
    async mutate(body, label) {return body.action.startsWith('playlist_') ? {kind: 'blocked', text: 'Read current state before trying again.'} : super.mutate(body, label);}
  }
  for (const direction of queueRecoveryActions) {
    const h = await appHarness({controllerClass: LocallyBlocked}), row = queueRecoveryRow(h, direction);
    await row.fire('click'); await ordinaryQueuePoll(h); await h.refresh();
    assert.equal(row.disabled, true); await row.fire('click'); assert.equal(h.posts().length, 0);
  }
});
test('queue rejection recovery demands fresh eligible readback after the explicit Refresh', async () => {
  const unavailable = [
    {},
    state({transport: {...state().transport, phase: 'PAUSED'}}),
    state({transport: {...state().transport, status: 'UNKNOWN'}}),
    state({actions: {...state().actions, playlist_remove: false, playlist_move: false}}),
    state({queue: {...state().queue, count: 1, items: state().queue.items.slice(0, 1)}})
  ];
  for (const direction of queueRecoveryActions) for (const next of unavailable) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction); h.setPost(queueAdmissionRejection);
    await row.fire('click'); h.setState(next); await h.refresh();
    assert.equal(row.disabled, true); await row.fire('click'); assert.equal(h.posts().length, 1);
    assert.doesNotMatch(h.ids.get('notice-text').textContent, /available again/);
  }
  const h = await appHarness(), row = queueRecoveryRow(h, 'remove'); h.setPost(queueAdmissionRejection);
  await row.fire('click'); h.setState({}); await h.refresh(); h.setState(state());
  await ordinaryQueuePoll(h); assert.equal(row.disabled, true);
  await h.refresh(); assert.equal(row.disabled, false); assert.equal(h.posts().length, 1);
});
test('queue rejection recovery never revives detached, restarted, replaced or unsupported old intents', async () => {
  const changes = [
    {generation: 8}, {session_id: 'restarted'},
    {queue: {...state().queue, name: 'Replacement queue'}},
    {queue: {...state().queue, items: state().queue.items.map(item => ({...item, title: 'Different title'}))}},
    {queue: {...state().queue, playlist_edit_reason: 'unsupported_queue'}}
  ];
  for (const direction of queueRecoveryActions) for (const change of changes) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction); h.setPost(queueAdmissionRejection);
    await row.fire('click'); h.setState(state(change)); await h.refresh();
    assert.equal(row.disabled, true); await row.fire('click'); assert.equal(h.posts().length, 1);
    h.setState(state()); await h.refresh(); await row.fire('click'); assert.equal(h.posts().length, 1);
  }
});
test('queue rejection recovery cannot be pre-authorized by Refresh while the request is pending', async () => {
  for (const direction of queueRecoveryActions) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction), pending = deferred(); h.setPost(() => pending.promise);
    const editing = row.fire('click'); await h.settle(); await h.refresh();
    assert.equal(row.disabled, true); assert.equal(h.ids.get('stop').disabled, false);
    pending.resolve(queueAdmissionRejection()); await editing; await ordinaryQueuePoll(h);
    assert.equal(row.disabled, true); await row.fire('click'); assert.equal(h.posts().length, 1);
    await h.refresh(); assert.equal(row.disabled, false); assert.equal(h.posts().length, 1);
  }
});
test('queue rejection recovery waits for post-click read even when an ordinary read was already pending', async () => {
  const h = await appHarness(), row = queueRecoveryRow(h, 'remove'); h.setPost(queueAdmissionRejection); await row.fire('click');
  const old = deferred(), fresh = deferred(); let reads = 0;
  h.setState(() => ++reads === 1 ? old.promise : fresh.promise);
  const polling = ordinaryQueuePoll(h); await h.settle();
  const refreshing = h.refresh(); await h.settle();
  assert.equal(reads, 1); assert.equal(row.disabled, true);
  old.resolve(state({revision: 50})); await h.settle();
  assert.equal(reads, 2); assert.equal(row.disabled, true); assert.equal(h.posts().length, 1);
  fresh.resolve(state({revision: 51})); await Promise.all([polling, refreshing]);
  assert.equal(row.disabled, false); assert.equal(h.posts().length, 1);
});
test('queue rejection recovery cannot supersede a newer Stop or UNKNOWN outcome', async () => {
  for (const direction of queueRecoveryActions) for (const late of [false, true]) for (const unknownStop of [false, true]) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction), pending = deferred();
    h.setPost(body => {
      if (body.action !== 'stop') return late ? pending.promise : queueAdmissionRejection();
      if (unknownStop) throw new Error('lost Stop');
      return {request_acknowledged: true};
    });
    const editing = row.fire('click'); await h.settle(); if (!late) await editing;
    assert.equal(h.ids.get('stop').disabled, false); await h.ids.get('stop').fire('click');
    const notice = h.ids.get('notice-text').textContent;
    if (late) {pending.resolve(queueAdmissionRejection()); await editing;}
    await h.refresh(); await row.fire('click');
    assert.equal(row.disabled, true); assert.deepEqual(h.posts().map(body => body.action), [direction === 'remove' ? 'playlist_remove' : 'playlist_move', 'stop']);
    assert.equal(h.ids.get('notice-text').textContent, notice);
    assert.equal(h.ids.get('notice-kind').textContent, unknownStop ? 'UNKNOWN' : 'CONFIRMED');
  }
});
test('queue rejection recovery is invalidated by a superseding command or pagehide during Refresh', async () => {
  for (const supersede of ['stop', 'pagehide']) {
    const h = await appHarness(), row = queueRecoveryRow(h, 'remove'); h.setPost(queueAdmissionRejection); await row.fire('click');
    const pending = deferred(); let reads = 0;
    h.setState(() => ++reads === 1 ? pending.promise : state({revision: 51}));
    const refreshing = h.refresh(); await h.settle(); assert.equal(row.disabled, true);
    let stopping;
    if (supersede === 'stop') {h.setPost(() => ({request_acknowledged: true})); stopping = h.ids.get('stop').fire('click'); await h.settle();}
    else {h.pagehide(); h.pageshow();}
    pending.resolve(state({revision: 50})); await refreshing; if (stopping) await stopping;
    assert.equal(row.disabled, true); await row.fire('click');
    assert.equal(h.posts().length, supersede === 'stop' ? 2 : 1);
    assert.doesNotMatch(h.ids.get('notice-text').textContent, /available again/);
    if (supersede === 'pagehide') {await h.refresh(); assert.equal(row.disabled, false);}
  }
});
test('queue rejection recovery preserves earlier UNKNOWN history and independent Stop availability', async () => {
  const h = await appHarness(), row = queueRecoveryRow(h, 'remove');
  h.setPost(() => {throw new Error('lost Play');}); await h.ids.get('play').fire('click');
  h.setPost(queueAdmissionRejection); await row.fire('click'); await h.refresh();
  assert.equal(row.disabled, false); assert.equal(h.ids.get('stop').disabled, false);
  assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown: Play/);
  assert.equal(h.posts().length, 2);
});
test('queue rejection recovery refuses raw duplicate members before JSON.parse can erase contradictions', async () => {
  const invalid = [
    '{"error":"unavailable","outcome":"UNKNOWN","message":"Requested operation is currently unavailable.","outcome":"REJECTED"}',
    '{"error":"unavailable","outcome":"ERROR","message":"Requested operation is currently unavailable.","outcome":"REJECTED"}',
    '{"error":"unavailable","message":"Requested operation is currently unavailable.","outcome":"REJECTED","outcome":"REJECTED"}',
    '{"error":"unavailable","message":"Requested operation is currently unavailable.","outcome":"UNKNOWN","out\\u0063ome":"REJECTED"}',
    '{"error":"unavailable","message":{"committed":true},"message":"Requested operation is currently unavailable.","outcome":"REJECTED"}',
    '{"error":"unavailable","message":"synthetic-private-response: \\\"UNKNOWN\\\"","message":"Requested operation is currently unavailable.","outcome":"REJECTED"}',
    '{"error":"unavailable","message":"Requested operation is currently unavailable.","outcome":"REJECTED",}',
  ];
  for (const direction of queueRecoveryActions) for (const raw of invalid) {
    const h = await appHarness(), row = queueRecoveryRow(h, direction);
    h.setPost(() => ({__status: 409, __raw: raw})); await row.fire('click'); await h.refresh();
    assert.equal(row.disabled, true); await row.fire('click'); assert.equal(h.posts().length, 1);
    assert.doesNotMatch(h.ids.get('notice-text').textContent, /nothing was changed|available again|synthetic-private-response/);
  }
  // Key order, whitespace and legal JSON escaping are not extra members.
  for (const raw of [
    '{"error": "unavailable", "message": "Requested operation is currently unavailable.", "outcome": "REJECTED"}',
    ' { "out\\u0063ome" : "REJECTED", "message" : "Requested operation is currently unavailable.", "error" : "unavailable" }\n'
  ]) {
    const h = await appHarness(), row = queueRecoveryRow(h, 'remove');
    h.setPost(() => ({__status: 409, __raw: raw})); await row.fire('click');
    assert.equal(h.ids.get('notice-text').textContent, queueRejectedGuidance);
    await h.refresh(); assert.equal(row.disabled, false); assert.equal(h.posts().length, 1);
  }
  const h = harness(); await h.controller.refresh();
  const body = queueAdmissionRejection(); delete body.__status; h.hooks.post = () => ({status: 409, body});
  const result = await h.controller.mutate({action: 'playlist_remove', expected_generation: 7, index: 1}, 'Remove');
  assert.equal(result.kind, 'error'); assert.equal(result.queueEditRejected, undefined, 'Missing raw admission evidence must fail closed');
});
