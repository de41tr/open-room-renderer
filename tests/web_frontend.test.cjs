/* Dependency-free control-flow tests; these are not browser/layout tests. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Controller, PlaylistDraft, MAX_REQUEST_BYTES, requestBytes, playlistAvailable, pastedURLs, playlistPreview, DspDraft, DSP_KEYS, DSP_MODES, validateEditor, MODES, validateState, validateFavorites, safeAudioURL, safeArtwork, formatTime, guardedAction, commandError} = require('../fake_linkplay/web_assets/controller.js');
const clone = value => JSON.parse(JSON.stringify(value));
const revisionID = letter => letter.repeat(64);
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
    version: 1, session_id: 'session-one', revision: 1, generation: 7, csrf_token: 'test-csrf-token', device: {name: 'Living room'},
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
async function appHarness() {
  const ids = new Map();
  let current = state(), library = favorites([favorite()]), revision = 0, postHook = null;
  const requests = [], events = new Map(), timers = new Map(); let timerId = 0;
  class Element {
    constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = new Map(); this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this.classList = {toggle() {}}; this.open = false; }
    get value() {return this._value || '';}
    set value(value) {this._value = value; if (this.type === 'file' && value === '') this.files = [];}
    setAttribute(key, value) {this.attributes[key] = value; if (key === 'src') this.src = value;}
    getAttribute(key) {return this.attributes[key];}
    removeAttribute(key) {delete this.attributes[key];}
    addEventListener(name, callback) {if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(callback);}
    async fire(name) {for (const callback of this.listeners.get(name) || []) await callback({preventDefault() {}});}
    append(...children) {this.children.push(...children.flatMap(child => child.tagName === 'FRAGMENT' ? child.children : [child]));}
    replaceChildren(...children) {this.children = []; this.append(...children);}
    querySelectorAll(tag) {const found = []; for (const child of this.children) {if (child.tagName.toLowerCase() === tag) found.push(child); found.push(...child.querySelectorAll(tag));} return found;}
    focus() {document.activeElement = this;}
    blur() {if (document.activeElement === this) document.activeElement = null;}
    showModal() {this.open = true;}
    close() {this.open = false; this.fire('close');}
    reset() {ids.get('favorite-name').value = ''; ids.get('favorite-url').value = '';}
  }
  const html = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/index.html'), 'utf8');
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]); element.id = match[3]; element.hidden = /\bhidden\b/.test(match[2]); element.disabled = /\bdisabled\b/.test(match[2]);
    const type = match[2].match(/\btype="([^"]*)"/); if (type) element.type = type[1];
    const value = match[2].match(/\bvalue="([^"]*)"/); if (value) element.value = value[1]; ids.set(element.id, element);
  }
  const document = {activeElement: null, hidden: false, getElementById: id => {assert.ok(ids.has(id), `Unknown element ${id}`); return ids.get(id);}, createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), addEventListener: (name, callback) => events.set(name, callback)};
  const window = {RendererControl: require('../fake_linkplay/web_assets/controller.js'), setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;}, clearTimeout: id => timers.delete(id), addEventListener: (name, callback) => events.set(name, callback)};
  const fetch = async (url, options) => {
    requests.push({url, options}); let payload;
    if (options.method === 'POST') payload = postHook ? await postHook(JSON.parse(options.body)) : {request_acknowledged: true};
    else if (url.endsWith('/favorites')) payload = library;
    else payload = {...clone(current), revision: ++revision};
    const status = payload.__status || 200; delete payload.__status;
    return new Response(JSON.stringify(payload), {status, headers: {'Content-Type': 'application/json'}});
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/app.js'), 'utf8'), {window, document, fetch, AbortController, TextEncoder, TextDecoder, Uint8Array, URL, Date, console});
  async function settle() {for (let i = 0; i < 10; i += 1) await tick();}
  await settle();
  return {ids, timers, requests, pagehide: () => events.get('pagehide')?.(), setState: value => {current = value;}, setFavorites: value => {library = value;}, setPost: fn => {postHook = fn;}, settle, refresh: async () => {await ids.get('refresh').fire('click'); await settle();}, posts: () => requests.filter(item => item.options.method === 'POST').map(item => JSON.parse(item.options.body))};
}
test('app renders metadata as text, and polling preserves favorite form edits', async () => {
  const h = await appHarness();
  assert.equal(h.ids.get('track-title').textContent, 'Track <script>bad()</script>'); assert.equal(h.ids.get('track-title').children.length, 0);
  await h.ids.get('add-favorite').fire('click'); h.ids.get('favorite-name').value = 'My unsaved name'; h.ids.get('favorite-url').value = 'https://example.com/private?token=abc';
  h.setState(state({media: {...state().media, title: 'New title'}})); await h.refresh();
  assert.equal(h.ids.get('favorite-name').value, 'My unsaved name'); assert.equal(h.ids.get('favorite-url').value, 'https://example.com/private?token=abc');
  assert.equal(h.ids.get('favorite-dialog').open, true);
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
async function editBand(h, key, value) {h.ids.get('dsp-' + key).value = value; await h.ids.get('dsp-' + key).fire('input');}
async function selectPreset(h, id) {h.ids.get('dsp-preset').value = id; await h.ids.get('dsp-preset').fire('change');}
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
