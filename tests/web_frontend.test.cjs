/* Dependency-free control-flow tests; these are not browser/layout tests. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Controller, MODES, validateState, validateFavorites, safeAudioURL, safeArtwork, formatTime, guardedAction, commandError} = require('../fake_linkplay/web_assets/controller.js');
const clone = value => JSON.parse(JSON.stringify(value));
function state(overrides = {}) {
  return {
    version: 1, session_id: 'session-one', revision: 1, generation: 7, csrf_token: 'test-csrf-token', device: {name: 'Living room'},
    transport: {phase: 'STOPPED', intent: 'stop', status: 'CONFIRMED', message: ''},
    media: {title: 'Track <script>bad()</script>', artist: 'Artist', album: '', artwork: null, is_live: false, position_seconds: 0, duration_seconds: 120},
    gain: {status: 'CONFIRMED', volume: 20, mute: false, last_confirmed: {volume: 20, mute: false}},
    queue: {name: 'My queue', count: 2, index: 1, mode: 'normal', items: [{index: 1, title: 'One', artist: '', album: '', duration_seconds: 120}, {index: 2, title: 'Two', artist: '', album: '', duration_seconds: null}]},
    dsp: {status: 'CONFIRMED', balance: 0, eq_enabled: false, name: 'Default'},
    actions: {play: true, pause: false, stop: true, next: true, previous: false, seek: true, queue_select: true, queue_mode: true, save_current: true}, ...overrides
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
  const h = harness(); const next = state(); next.gain = {...next.gain, status: 'UNKNOWN', volume: null, mute: null}; next.dsp = {...next.dsp, status: 'PENDING', balance: null, eq_enabled: null}; h.setState(next); await h.controller.refresh();
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
    const value = match[2].match(/\bvalue="([^"]*)"/); if (value) element.value = value[1]; ids.set(element.id, element);
  }
  const document = {activeElement: null, hidden: false, getElementById: id => {assert.ok(ids.has(id), `Unknown element ${id}`); return ids.get(id);}, createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), addEventListener: (name, callback) => events.set(name, callback)};
  const window = {RendererControl: require('../fake_linkplay/web_assets/controller.js'), setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;}, clearTimeout: id => timers.delete(id), addEventListener: (name, callback) => events.set(name, callback)};
  const fetch = async (url, options) => {
    requests.push({url, options}); let payload;
    if (options.method === 'POST') payload = postHook ? await postHook(JSON.parse(options.body)) : {request_acknowledged: true};
    else if (url.endsWith('/favorites')) payload = library;
    else payload = {...clone(current), revision: ++revision};
    return new Response(JSON.stringify(payload), {status: 200, headers: {'Content-Type': 'application/json'}});
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/app.js'), 'utf8'), {window, document, fetch, AbortController, TextDecoder, Uint8Array, URL, Date, console});
  async function settle() {for (let i = 0; i < 10; i += 1) await tick();}
  await settle();
  return {ids, timers, requests, setState: value => {current = value;}, setFavorites: value => {library = value;}, setPost: fn => {postHook = fn;}, settle, refresh: async () => {await ids.get('refresh').fire('click'); await settle();}, posts: () => requests.filter(item => item.options.method === 'POST').map(item => JSON.parse(item.options.body))};
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
  const h = await appHarness(); const next = state(); next.gain = {...next.gain, status: 'UNKNOWN', volume: null, mute: null}; next.dsp = {...next.dsp, status: 'PENDING', balance: null, eq_enabled: null, name: 'Old preset'}; h.setState(next); await h.refresh();
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
  await h.ids.get('favorite-dialog').fire('close'); assert.equal(h.ids.get('favorite-url').value, '');
});
