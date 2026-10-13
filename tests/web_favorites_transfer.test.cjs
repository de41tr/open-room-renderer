/* Managed-favorites backup/restore tests use synthetic DOM/fetch/stream doubles.
 * These do not establish native browser downloads, layout, focus, CSP or audio. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {FAVORITES_EXPORT_FILENAME, FAVORITES_TRANSFER_CHANGED, favoritesRestoreBody, validateFavoritesExportSummary, validateFavoritesDocument, validateFavoritesExportDocument, favoritesTransferFailure, FavoritesRestoreDraft, Controller, readExportResponse, requestBytes, DSP_KEYS, guardedAction} = require('../fake_linkplay/web_assets/controller.js');
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
    version: 1, session_id: 'S'.repeat(32), revision: 1, generation: 7, csrf_token: 'test-csrf-token', device: {name: 'Living room', revision: revisionID('1'), name_visible: true},
    transport: {phase: 'STOPPED', intent: 'stop', status: 'CONFIRMED', message: ''},
    media: {title: 'Track <script>bad()</script>', artist: 'Artist', album: '', artwork: null, is_live: false, position_seconds: 0, duration_seconds: 120},
    gain: {status: 'CONFIRMED', volume: 20, mute: false, last_confirmed: {volume: 20, mute: false}},
    queue: {playlist_edit_reason: 'supported', name: 'My queue', count: 2, index: 1, mode: 'normal', items: [{index: 1, title: 'One', artist: '', album: '', duration_seconds: 120}, {index: 2, title: 'Two', artist: '', album: '', duration_seconds: null}]},
    dsp: {status: 'CONFIRMED', balance: 0, eq_enabled: false, name: 'Default', editor: editor()},
    actions: {playlist_replace: true, playlist_append: true, playlist_remove: true, playlist_move: true, play: true, pause: false, stop: true, next: true, previous: false, seek: true, queue_select: true, queue_mode: true, save_current: true}, ...overrides
  };
}
const favorites = (items = [], session_id = 'S'.repeat(32)) => ({version: 1, session_id, favorites: items});
const favorite = () => ({id: 'a'.repeat(32), editable: true, name: 'Radio', title: '', artist: '', album: '', artwork: null, target_label: 'example.com'});
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; }
const tick = () => new Promise(resolve => setImmediate(resolve));
/* Deliberately small DOM test double. It checks event/state wiring only, never
 * claims real browser semantics, CSS layout, image loading, or accessibility QA. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
async function appHarness(items = [favorite()]) {
  const ids = new Map();
  let current = state(), library = favorites(items), revision = 0, postHook = null, exportHook = null;
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
  const window = {RendererControl: require('../fake_linkplay/web_assets/controller.js'), setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;}, clearTimeout: id => timers.delete(id), addEventListener: (name, callback) => events.set(name, callback)};
  const fetch = async (url, options) => {
    requests.push({url, options}); let payload;
    if (url.endsWith('/favorites/export')) return exportHook ? exportHook(JSON.parse(options.body), options) : backupResponse(JSON.parse(options.body).action === 'prepare' ? backupSummary(current, {count: items.filter(item => item.editable).length, size_bytes: encoded(backupJSON(items.filter(item => item.editable).map(() => record()))).length}) : backupJSON(items.filter(item => item.editable).map(() => record())), {attachment: JSON.parse(options.body).action === 'download'});
    if (options.method === 'POST') payload = postHook ? await postHook(JSON.parse(options.body)) : {request_acknowledged: true};
    else if (url.endsWith('/favorites')) payload = typeof library === 'function' ? await library() : clone(library);
    else payload = {...clone(current), revision: ++revision};
    const status = payload.__status || 200; delete payload.__status;
    return new Response(JSON.stringify(payload), {status, headers: {'Content-Type': 'application/json'}});
  };
  class TestURL extends URL {
    static createObjectURL(blob) {const url = 'blob:owned-' + (created.length + 1); blobs.set(url, blob); created.push(url); return url;}
    static revokeObjectURL(url) {assert.ok(blobs.has(url), 'Only revoke a live, owned URL'); blobs.delete(url); revoked.push(url);}
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/app.js'), 'utf8'), {window, document, fetch, AbortController, TextEncoder, TextDecoder, Uint8Array, URL: TestURL, Blob, Date, console});
  async function settle() {for (let i = 0; i < 10; i += 1) await tick();}
  await settle();
  return {ids, timers, requests, blobs, created, revoked, syntheticClicks, focusCalls, focused: () => document.activeElement, setExport: fn => {exportHook = fn;}, exports: () => requests.filter(item => item.url.endsWith('/favorites/export')), pagehide: (persisted = true) => events.get('pagehide')?.({type: 'pagehide', persisted}), pageshow: () => events.get('pageshow')?.({type: 'pageshow', persisted: true}), offline: () => events.get('offline')?.({type: 'offline'}), setState: value => {current = value;}, setFavorites: value => {library = value;}, setPost: fn => {postHook = fn;}, settle, refresh: async () => {await ids.get('refresh').fire('click'); await settle();}, posts: () => requests.filter(item => item.url === '/orr/v1/web/command').map(item => JSON.parse(item.options.body))};
}
const encoded = value => new TextEncoder().encode(value);
const record = (changes = {}) => ({id: 'a'.repeat(32), name: 'PRIVATE_NAME 🎵 Café\tstation\nnext', url: 'https://example.test/audio?token=PRIVATE_TOKEN&sig=%2f%2F+A', title: 'PRIVATE_TITLE\nline\tend', artist: 'Artist', album: '', artwork: 'https://example.test/art?token=PRIVATE_ART', ...changes});
const configured = () => ({...favorite(), id: 'configured:1', editable: false});
const backupJSON = (records = [record()]) => JSON.stringify({version: 1, favorites: records}, null, 2) + '\n';
function backupSummary(current = state(), changes = {}) {
  return {version: 1, session_id: current.session_id, revision: revisionID('e'), count: 1, size_bytes: encoded(backupJSON()).length, filename: FAVORITES_EXPORT_FILENAME, scope: 'managed_only', ...changes};
}
function backupResponse(value, {status = 200, headers = {}, attachment = false} = {}) {
  const bytes = value instanceof Uint8Array ? value : encoded(typeof value === 'string' ? value : JSON.stringify(value));
  return new Response(bytes, {status, headers: {'Content-Type': 'application/json', 'Content-Length': String(bytes.length), ...(attachment ? {'Content-Disposition': `attachment; filename="${FAVORITES_EXPORT_FILENAME}"`} : {}), ...headers}});
}
function file(source = backupJSON(), name = 'PRIVATE_LOCAL_FILENAME.json') {
  const bytes = typeof source === 'string' ? encoded(source) : source;
  return {name, size: bytes.length, arrayBuffer: async () => bytes.slice().buffer};
}
async function chooseFile(h, value = file()) {h.ids.get('favorites-restore-file').files = [value]; h.ids.get('favorites-restore-file').value = value.name; await h.ids.get('favorites-restore-file').fire('change');}
async function consent(h, kind) {h.ids.get(`favorites-${kind}-confirm`).checked = true; await h.ids.get(`favorites-${kind}-confirm`).fire('change');}
async function readyRestore(h, source = file()) {await h.ids.get('favorites-restore').fire('click'); await chooseFile(h, source); await consent(h, 'restore');}
async function readyExport(h) {await h.ids.get('favorites-backup').fire('click'); await consent(h, 'export'); await h.ids.get('favorites-export-form').fire('submit');}
const privatePreview = h => ['favorites-restore-summary', 'favorites-restore-status', 'favorites-export-summary', 'favorites-export-status', 'notice-text'].map(id => h.ids.get(id).textContent).join('\n');
function assertPrivateSafe(h) {assert.doesNotMatch(privatePreview(h), /PRIVATE_|example\.test/);}
function assertCleared(h, kind) {
  assert.equal(h.ids.get(`favorites-${kind}-dialog`).open, false);
  assert.equal(h.ids.get(`favorites-${kind}-confirm`).checked, false);
  assert.equal(h.ids.get(`favorites-${kind}-summary`).textContent, '');
  assert.equal(h.ids.get(`favorites-${kind}-status`).textContent, '');
  if (kind === 'export') {assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('favorites-export-download').getAttribute('href'), undefined); assert.equal(h.ids.get('favorites-export-download').hidden, true);}
  else {assert.equal(h.ids.get('favorites-restore-file').value, ''); assert.equal(h.ids.get('favorites-restore-file').files.length, 0);}
}

test('backup summaries accept exact managed-only metadata including an empty library', () => {
  const summary = backupSummary(); assert.equal(validateFavoritesExportSummary(summary, state().session_id, 1), summary);
  assert.equal(validateFavoritesExportSummary(backupSummary(state(), {count: 0}), state().session_id, 0).count, 0);
  for (const changes of [{version: true}, {version: 2}, {session_id: 'other'}, {revision: 'not-a-revision'}, {revision: 'E'.repeat(64)}, {count: -1}, {count: true}, {count: 1.5}, {count: 101}, {count: 0}, {size_bytes: 0}, {size_bytes: true}, {size_bytes: 32769}, {size_bytes: 1.5}, {filename: '../private.json'}, {scope: 'all'}, {name: 'PRIVATE_NAME'}, {url: 'PRIVATE_TOKEN'}]) assert.throws(() => validateFavoritesExportSummary({...summary, ...changes}, state().session_id, 1));
  const missing = {...summary}; delete missing.scope; assert.throws(() => validateFavoritesExportSummary(missing, state().session_id, 1));
});
test('nested favorites validator preserves exact Unicode, metadata whitespace and URL bytes', () => {
  const source = backupJSON([record(), record({id: 'b'.repeat(32)})]);
  const bytes = encoded(source), summary = backupSummary(state(), {size_bytes: bytes.length, count: 2});
  assert.deepEqual(validateFavoritesDocument(bytes), {text: source, count: 2});
  assert.equal(validateFavoritesExportDocument(bytes, summary), bytes); assert.equal(new TextDecoder().decode(bytes), source);
  // Names and URLs may repeat; managed IDs alone must be unique. The browser
  // structurally accepts uncommon URL forms for authoritative server validation.
  for (const url of ['http://[fe80::1%25zone]/radio?sig=%2f%2F', 'https://éxample.test/audio?x=é', 'renderer must reject this target']) assert.equal(validateFavoritesDocument(encoded(backupJSON([record({url})]))).count, 1);
  const empty = encoded(backupJSON([])); assert.equal(validateFavoritesDocument(empty).count, 0);
  const optional = encoded(backupJSON([{id: 'a'.repeat(32), name: 'Name', url: 'https://example.test/audio'}]));
  assert.equal(validateFavoritesDocument(optional).count, 1); assert.throws(() => validateFavoritesDocument(optional, true));
});
test('nested validator rejects duplicate or escaped keys independently per object and malformed versions', () => {
  const source = backupJSON();
  const invalid = [source.replace('"version": 1', '"version": 1.0'), source.replace('"version": 1', '"version": 1e0'), source.replace('"version": 1,', '"version": 1, "\\u0076ersion": 1,'), source.replace('"album": ""', '"album": "", "\\u0061lbum": ""'), source.replace('"album": ""', '"album": NaN'), source.replace('"album": ""', '"album": 1e999'), source.replace('"album": ""', '"album": Infinity'), '\ufeff' + source, '{} garbage', '[[[[[[[[{}]]]]]]]]', '{"version":1,"favorites":[],}', 'null', '[]'];
  invalid.forEach(value => assert.throws(() => validateFavoritesDocument(encoded(value))));
  assert.throws(() => validateFavoritesDocument(new Uint8Array([0xff]))); assert.throws(() => validateFavoritesDocument(new Uint8Array(32769)));
});
test('strict records enforce required fields, string types, XML-safe text, byte limits and unique managed IDs', () => {
  const invalid = [null, [], {}, {version: true, favorites: []}, {version: 1, favorites: null}, {version: 2, favorites: []}, {version: 1, favorites: [], extra: true}];
  for (const changes of [{id: 'configured:1'}, {id: 'A'.repeat(32)}, {id: 'a'.repeat(31)}, {id: true}, {name: ''}, {name: '\t\n '}, {name: 'é'.repeat(129)}, {name: '\ud800'}, {url: ''}, {url: 'a'.repeat(8193)}, {url: null}, {url: 1}, {title: 'a'.repeat(1025)}, {artist: true}, {album: null}, {title: 'bad\x01text'}, {artwork: 'a'.repeat(8193)}, {extra: 'private'}]) invalid.push({version: 1, favorites: [record(changes)]});
  for (const key of ['id', 'name', 'url']) {const item = record(); delete item[key]; invalid.push({version: 1, favorites: [item]});}
  invalid.push({version: 1, favorites: [record(), record()]}, {version: 1, favorites: Array.from({length: 101}, (_, i) => record({id: i.toString(16).padStart(32, '0')}))});
  invalid.forEach(value => assert.throws(() => validateFavoritesDocument(encoded(JSON.stringify(value)))));
  const hundred = Array.from({length: 100}, (_, i) => ({id: i.toString(16).padStart(32, '0'), name: 'x', url: 'http://example.test'}));
  assert.equal(validateFavoritesDocument(encoded(backupJSON(hundred))).count, 100);
});
test('export document requires all seven fields and matching summary size/count; escaped envelope boundary is exact', async () => {
  let source = backupJSON(); source += ' '.repeat(32768 - requestBytes(favoritesRestoreBody(state().session_id, source)));
  assert.equal(requestBytes(favoritesRestoreBody(state().session_id, source)), 32768); assert.ok(encoded(source).length < 32768);
  const draft = new FavoritesRestoreDraft(state()); await draft.readFile(file(source)); assert.equal(draft.text, source);
  const summary = backupSummary(state(), {size_bytes: encoded(source).length}); assert.equal(validateFavoritesExportDocument(encoded(source), summary).length, summary.size_bytes);
  await draft.readFile(file(source + ' ')); assert.equal(draft.text, ''); assert.match(draft.error, /32,768/);
  assert.throws(() => validateFavoritesExportDocument(encoded(source + ' '), {...summary, size_bytes: summary.size_bytes + 1}));
  for (const change of [{count: 2}, {size_bytes: summary.size_bytes + 1}]) assert.throws(() => validateFavoritesExportDocument(encoded(source), {...summary, ...change}));
  const item = record(); delete item.artist; const partial = encoded(backupJSON([item])); assert.throws(() => validateFavoritesExportDocument(partial, {...summary, size_bytes: partial.length}));
  let reads = 0; await draft.readFile({size: 32769, arrayBuffer() {reads++; throw new Error();}}); assert.equal(reads, 0);
});
test('favorites reader uses the exact fixed filename and bounded attachment headers', async () => {
  const bytes = encoded(backupJSON()), signal = new AbortController().signal;
  assert.deepEqual((await readExportResponse(backupResponse(bytes, {attachment: true}), signal, bytes.length, FAVORITES_EXPORT_FILENAME)).bytes, bytes);
  await assert.rejects(readExportResponse(backupResponse(bytes, {attachment: true}), signal, bytes.length));
  for (const headers of [{'Content-Type': 'text/plain'}, {'Content-Length': String(bytes.length + 1)}, {'Content-Length': '32769'}, {'Content-Disposition': 'attachment; filename="other.json"'}]) await assert.rejects(readExportResponse(backupResponse(bytes, {attachment: true, headers}), signal, bytes.length, FAVORITES_EXPORT_FILENAME));
});
test('local reads replace prior files, erase byte buffers and never retain filenames', async () => {
  const draft = new FavoritesRestoreDraft(state()), pending = deferred(), oldBytes = encoded(backupJSON());
  const reading = draft.readFile({name: 'PRIVATE_LOCAL_FILENAME', size: oldBytes.length, arrayBuffer: () => pending.promise});
  const latest = backupJSON([record({name: 'New file'})]); await draft.readFile(file(latest)); pending.resolve(oldBytes.buffer); await reading;
  assert.equal(draft.text, latest); assert.ok(oldBytes.every(value => value === 0));
  const bytes = encoded(latest); await draft.readFile({size: bytes.length, arrayBuffer: async () => bytes.buffer}); assert.ok(bytes.every(value => value === 0));
  draft.cancel(); assert.equal(draft.text, ''); assert.equal(draft.count, 0); assert.equal(draft.reading, false);
});
test('controller blocks restore unless fresh, healthy and managed-empty, while configured favorites may remain', async () => {
  let library = favorites([favorite()]); const calls = [];
  const control = new Controller(async (path, options) => {calls.push({path, options}); return {status: 200, body: options.method === 'GET' ? path.endsWith('/state') ? state() : library : {request_acknowledged: true}};});
  await control.refresh(); assert.equal(control.canMutate('favorite_restore'), false);
  assert.equal((await control.mutate(favoritesRestoreBody(state().session_id, backupJSON()), 'Restore')).kind, 'blocked');
  library = favorites([configured()]); await control.refresh(); assert.equal(control.canMutate('favorite_restore'), true);
  control.favoritesFresh = false; assert.equal(control.canMutate('favorite_restore'), false); control.favoritesFresh = true; control.fresh = false; assert.equal(control.canMutate('favorite_restore'), false);
  assert.equal(calls.filter(item => item.options.method === 'POST').length, 0);
  assert.equal(guardedAction(state(), 'favorite_restore', {expected_session: 'old', text: 'private'}).expected_session, state().session_id);
});
test('Favorites actions distinguish backup from empty-only restore with configured rows allowed', async () => {
  for (const items of [[favorite()], [], [configured()], [configured(), favorite()]]) {
    const h = await appHarness(items); assert.equal(h.ids.get('favorites-backup').disabled, false);
    assert.equal(h.ids.get('favorites-restore').disabled, items.some(item => item.editable));
    if (items.some(item => item.editable)) {await h.ids.get('favorites-restore').fire('click'); assert.equal(h.ids.get('favorites-restore-dialog').open, false);}
    h.setFavorites({}); await h.refresh(); assert.equal(h.ids.get('favorites-backup').disabled, true); assert.equal(h.ids.get('favorites-restore').disabled, true);
  }
});
test('backup starts with redacted prepare, needs fresh consent and a real download click, preserves exact bytes', async () => {
  const h = await appHarness(); const notice = h.ids.get('notice-text').textContent;
  await h.ids.get('favorites-backup').fire('click'); assert.equal(h.exports().length, 1); assert.deepEqual(JSON.parse(h.exports()[0].options.body), {action: 'prepare', expected_session: state().session_id});
  assert.equal(h.ids.get('favorites-export-confirm').checked, false); await h.ids.get('favorites-export-form').fire('submit'); assert.equal(h.exports().length, 1);
  await consent(h, 'export'); await h.ids.get('favorites-export-form').fire('submit');
  assert.equal(h.exports().length, 2); assert.deepEqual(JSON.parse(h.exports()[1].options.body), {action: 'download', expected_session: state().session_id, expected_export: revisionID('e'), private_export_confirmed: true});
  for (const {options} of h.exports()) {assert.equal(options.credentials, 'same-origin'); assert.equal(options.mode, 'same-origin'); assert.equal(options.redirect, 'error'); assert.equal(options.referrerPolicy, 'no-referrer'); assert.equal(options.headers['X-ORR-CSRF'], state().csrf_token);}
  assert.equal(await h.blobs.get(h.created[0]).text(), backupJSON()); assert.deepEqual(h.syntheticClicks, []); assert.equal(h.ids.get('notice-text').textContent, notice);
  assert.match(h.ids.get('favorites-export-summary').textContent, /Managed favorites only · 1 entry/); assertPrivateSafe(h);
  assert.equal((await h.ids.get('favorites-export-download').fire('click')).defaultPrevented, false); assert.match(h.ids.get('favorites-export-status').textContent, /Download requested/);
  await h.ids.get('favorites-export-form').fire('submit'); assert.equal(h.exports().length, 2); assert.deepEqual(h.posts(), []);
});
test('zero-managed backup is valid even when configured favorites remain', async () => {
  for (const items of [[], [configured()]]) {
    const h = await appHarness(items); await readyExport(h); assert.match(h.ids.get('favorites-export-summary').textContent, /0 entries/);
    assert.equal(await h.blobs.get(h.created[0]).text(), backupJSON([])); assertPrivateSafe(h);
  }
});
test('restore sends the exact original file once after explicit consent, then clears source', async () => {
  const h = await appHarness([configured()]); const source = backupJSON([record(), record({id: 'b'.repeat(32)})]);
  await h.ids.get('favorites-restore').fire('click'); await chooseFile(h, file(source)); await h.ids.get('favorites-restore-form').fire('submit'); assert.deepEqual(h.posts(), []);
  assert.match(h.ids.get('favorites-restore-summary').textContent, /Managed favorites only · 2 entries/); assertPrivateSafe(h);
  await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit');
  assert.deepEqual(h.posts(), [favoritesRestoreBody(state().session_id, source)]); assert.equal(h.ids.get('favorites-restore-file').value, ''); assert.equal(h.ids.get('favorites-restore-summary').textContent, ''); assert.equal(h.ids.get('favorites-restore-confirm').checked, false);
  assert.match(h.ids.get('favorites-restore-status').textContent, /^Restore acknowledged/); assert.equal(h.ids.get('notice-kind').textContent, 'ACKNOWLEDGED');
  assert.match(h.ids.get('notice-text').textContent, /not verified/); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 1); assertPrivateSafe(h);
});
test('restore duplicate submits and local-file changes while POST pending cannot retry or replace submitted source', async () => {
  const h = await appHarness([]), pending = deferred(); h.setPost(() => pending.promise); await readyRestore(h);
  const waiting = h.ids.get('favorites-restore-form').fire('submit'); await h.settle();
  await h.ids.get('favorites-restore-form').fire('submit'); await chooseFile(h, file(backupJSON([]))); await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit');
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].text, backupJSON()); assert.equal(h.ids.get('favorites-restore-file').disabled, true);
  pending.resolve({request_acknowledged: true}); await waiting; assert.equal(h.ids.get('favorites-restore-submit').disabled, true);
});
test('restore malformed, oversize, truncated and escaped-oversize files never enable submission', async () => {
  let large = backupJSON(); large += ' '.repeat(32768 - encoded(large).length);
  for (const input of [file('not JSON PRIVATE_ERROR'), file(new Uint8Array([0xff])), file(large), {size: 32769, arrayBuffer() {throw new Error('must not read');}}, {size: 10, arrayBuffer: async () => encoded(backupJSON()).buffer}]) {
    const h = await appHarness([]); await readyRestore(h, input); await h.ids.get('favorites-restore-form').fire('submit');
    assert.equal(h.ids.get('favorites-restore-submit').disabled, true); assert.equal(h.posts().length, 0); assert.match(h.ids.get('favorites-restore-status').textContent, /32,768/); assertPrivateSafe(h);
  }
});
test('restore server rejections and lost ACK preserve one-shot outcome without private errors', async () => {
  for (const outcome of ['REJECTED', 'UNKNOWN', 'lost']) {
    const h = await appHarness([]); h.setPost(() => {h.setFavorites(favorites([favorite()])); if (outcome === 'lost') throw new Error('PRIVATE_ERROR'); return {__status: outcome === 'REJECTED' ? 409 : 502, outcome, error: 'favorites_not_empty', message: 'PRIVATE_ERROR'};});
    await readyRestore(h); await h.ids.get('favorites-restore-form').fire('submit'); await h.refresh();
    assert.match(h.ids.get('favorites-restore-status').textContent, outcome === 'REJECTED' ? /^ERROR.*empty managed/ : /^UNKNOWN.*No retry was sent/);
    await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 1); assertPrivateSafe(h);
    if (outcome !== 'REJECTED') {assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); h.setPost(() => ({request_acknowledged: true})); await h.ids.get('stop').fire('click'); assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown/);}
  }
});
test('known committed durability failure remains UNKNOWN despite matching readback', async () => {
  const h = await appHarness([]); h.setPost(() => {h.setFavorites(favorites([favorite()])); return {__status: 502, outcome: 'UNKNOWN', error: 'command_failed', committed: true, message: 'PRIVATE_ERROR'};});
  await readyRestore(h); await h.ids.get('favorites-restore-form').fire('submit'); await h.refresh();
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.match(h.ids.get('favorites-restore-status').textContent, /^UNKNOWN.*library was replaced.*Matching readback cannot confirm durability.*No retry/);
  h.setPost(() => ({request_acknowledged: true})); await h.ids.get('stop').fire('click'); assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown: Restore managed favorites \(library replaced; durability unconfirmed\)/);
  await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 2); assertPrivateSafe(h);
});
test('inconsistent or malformed committed effects cannot claim a known library replacement', async () => {
  const valid = {__status: 502, outcome: 'UNKNOWN', error: 'command_failed', committed: true, message: 'PRIVATE_ERROR'};
  for (const changes of [{__status: 200}, {__status: 400}, {error: 'other'}, {error: undefined}, {outcome: 'ERROR'}, {outcome: undefined}, {committed: 'true'}]) {
    const h = await appHarness([]); h.setPost(() => ({...valid, ...changes})); await readyRestore(h); await h.ids.get('favorites-restore-form').fire('submit');
    assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.match(h.ids.get('favorites-restore-status').textContent, /^UNKNOWN.*library may have changed/); assert.doesNotMatch(privatePreview(h), /library was replaced/); assertPrivateSafe(h);
  }
});
test('fixed failure maps never disclose private labels or permit prototype property lookups', async () => {
  for (const code of ['__proto__', 'constructor', 'toString', 'unknown']) assert.equal(favoritesTransferFailure(code), 'The favorites transfer request was rejected. Review the file and current managed favorites before starting again.');
  for (const code of ['favorites_not_empty', 'favorites_unavailable', 'stale_favorites_transfer', 'invalid_favorite', '__proto__']) {
    const h = await appHarness([]); h.setPost(() => ({__status: 409, outcome: 'REJECTED', error: code, message: 'PRIVATE_ERROR PRIVATE_NAME PRIVATE_TOKEN'}));
    await readyRestore(h); await h.ids.get('favorites-restore-form').fire('submit'); assert.match(h.ids.get('favorites-restore-status').textContent, /^ERROR/); assertPrivateSafe(h);
  }
});

test('Close, Cancel, Escape and navigation clear both dialogs, consent, files and owned object URLs', async () => {
  for (const kind of ['export', 'restore']) for (const dismissal of ['close', 'cancel', 'escape', 'pagehide', 'navigation']) {
    const h = await appHarness(kind === 'restore' ? [] : [favorite()]); await (kind === 'export' ? readyExport(h) : readyRestore(h));
    if (dismissal === 'escape') await h.ids.get(`favorites-${kind}-dialog`).fire('cancel');
    else if (dismissal === 'pagehide' || dismissal === 'navigation') h.pagehide(dismissal === 'pagehide');
    else await h.ids.get(`favorites-${kind}-${dismissal}`).fire('click');
    assertCleared(h, kind); if (kind === 'export') assert.equal(h.revoked.length, 1);
    h.pageshow(); await h.refresh(); assertCleared(h, kind);
  }
});
test('consent withdrawal revokes prepared bytes and requires an entirely fresh file review', async () => {
  const h = await appHarness(); await readyExport(h); h.ids.get('favorites-export-confirm').checked = false; await h.ids.get('favorites-export-confirm').fire('change');
  assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('favorites-export-download').getAttribute('href'), undefined);
  await consent(h, 'export'); await h.ids.get('favorites-export-form').fire('submit'); assert.equal(h.exports().length, 2);
  assert.equal((await h.ids.get('favorites-export-download').fire('click')).defaultPrevented, true);
  await h.ids.get('favorites-export-close').fire('click'); h.setFavorites(favorites([])); await h.refresh(); await readyRestore(h);
  h.ids.get('favorites-restore-confirm').checked = false; await h.ids.get('favorites-restore-confirm').fire('change');
  assert.equal(h.ids.get('favorites-restore-summary').textContent, ''); await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 0);
  await chooseFile(h); assert.equal(h.ids.get('favorites-restore-confirm').checked, false); await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 1);
});
test('changed library, restart, unavailable reads and offline permanently invalidate prepared transfers', async () => {
  for (const kind of ['export', 'restore']) for (const scenario of ['library', 'restart', 'unavailable', 'mixed-session', 'offline', 'command']) {
    const items = kind === 'restore' ? [] : [favorite()], h = await appHarness(items); await (kind === 'export' ? readyExport(h) : readyRestore(h));
    if (scenario === 'offline') h.offline();
    else if (scenario === 'command') await h.ids.get('stop').fire('click');
    else {
      if (scenario === 'restart') {h.setState(state({session_id: 'new-session'})); h.setFavorites(favorites(items, 'new-session'));}
      else h.setFavorites(scenario === 'library' ? favorites([configured(), ...items]) : scenario === 'mixed-session' ? favorites(items, 'other') : {});
      await h.refresh();
    }
    assert.equal(h.ids.get(`favorites-${kind}-confirm`).checked, false); assert.equal(h.ids.get(`favorites-${kind}-summary`).textContent, ''); assert.equal(h.blobs.size, 0);
    assert.equal(h.ids.get(`favorites-${kind}-status`).textContent, FAVORITES_TRANSFER_CHANGED);
    h.setState(state()); h.setFavorites(favorites(items)); await h.refresh(); await consent(h, kind); await h.ids.get(`favorites-${kind}-form`).fire('submit');
    assert.equal(h.posts().length, scenario === 'command' ? 1 : 0); assert.equal(h.exports().length, kind === 'export' ? 2 : 0);
  }
});
test('managed arrivals invalidate an empty restore and cannot be undone by a later empty read', async () => {
  const h = await appHarness([]); await readyRestore(h); h.setFavorites(favorites([favorite()])); await h.refresh();
  assert.equal(h.ids.get('favorites-restore').disabled, true); assert.equal(h.ids.get('favorites-restore-submit').disabled, true); assert.equal(h.ids.get('favorites-restore-summary').textContent, '');
  h.setFavorites(favorites([])); await h.refresh(); await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 0);
  await h.ids.get('favorites-restore-close').fire('click'); await readyRestore(h); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 1);
});
test('unrelated queue and DSP readback changes do not invalidate healthy favorites contexts', async () => {
  for (const kind of ['export', 'restore']) {
    const h = await appHarness(kind === 'restore' ? [] : [favorite()]); await (kind === 'export' ? readyExport(h) : readyRestore(h));
    h.setState(state({generation: 99, dsp: {...state().dsp, editor: editor({revision: revisionID('f')})}})); await h.refresh();
    assert.equal(h.ids.get(`favorites-${kind}-confirm`).checked, true); assert.notEqual(h.ids.get(`favorites-${kind}-summary`).textContent, '');
    if (kind === 'export') assert.equal(h.blobs.size, 1);
    else {await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 1);}
  }
});
test('late local files cannot revive dismissed, stale, replaced or consent-cleared restore drafts', async () => {
  for (const change of ['close', 'stale', 'replace', 'consent', 'pagehide']) {
    const h = await appHarness([]), pending = deferred(), bytes = encoded(backupJSON()); await h.ids.get('favorites-restore').fire('click');
    const reading = chooseFile(h, {name: 'PRIVATE_LOCAL_FILENAME', size: bytes.length, arrayBuffer: () => pending.promise}); await h.settle();
    if (change === 'close') {await h.ids.get('favorites-restore-close').fire('click'); await h.ids.get('favorites-restore').fire('click');}
    else if (change === 'pagehide') h.pagehide();
    else if (change === 'stale') {h.setFavorites(favorites([favorite()])); await h.refresh();}
    else if (change === 'replace') await chooseFile(h, file(backupJSON([record({name: 'New selection'})])));
    else {h.ids.get('favorites-restore-confirm').checked = false; await h.ids.get('favorites-restore-confirm').fire('change');}
    pending.resolve(bytes.buffer); await reading;
    assert.equal(h.ids.get('favorites-restore-confirm').checked, false); assert.equal(h.ids.get('favorites-restore-submit').disabled, true); assert.equal(h.posts().length, 0); assertPrivateSafe(h); assert.ok(bytes.every(value => value === 0));
    if (change !== 'replace') assert.equal(h.ids.get('favorites-restore-summary').textContent, '');
    else {await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(JSON.parse(h.posts()[0].text).favorites[0].name, 'New selection');}
  }
});
test('every new file selection clears consent even when selecting identical contents', async () => {
  const h = await appHarness([]); await readyRestore(h); await chooseFile(h); assert.equal(h.ids.get('favorites-restore-confirm').checked, false); assert.equal(h.ids.get('favorites-restore-submit').disabled, true);
  await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 0);
  await consent(h, 'restore'); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 1);
});
test('export double clicks are single-shot; late prepare/download responses cannot resurrect private state', async () => {
  for (const action of ['prepare', 'download']) for (const change of ['close', 'stale', 'consent', 'pagehide', 'offline']) {
    const h = await appHarness(), pending = deferred(); let cancelled = 0;
    h.setExport(body => body.action === action ? pending.promise : backupResponse(backupSummary()));
    let waiting = h.ids.get('favorites-backup').fire('click');
    if (action === 'download') {await waiting; await consent(h, 'export'); waiting = h.ids.get('favorites-export-form').fire('submit');}
    await h.settle(); await h.ids.get('favorites-backup').fire('click'); await h.ids.get('favorites-export-form').fire('submit');
    assert.equal(h.exports().length, action === 'prepare' ? 1 : 2);
    if (change === 'stale') {h.setFavorites(favorites([])); await h.refresh();}
    else if (change === 'consent' && action === 'download') {h.ids.get('favorites-export-confirm').checked = false; await h.ids.get('favorites-export-confirm').fire('change');}
    else if (change === 'pagehide') h.pagehide();
    else if (change === 'offline') h.offline();
    else await h.ids.get('favorites-export-close').fire('click');
    await waiting;
    const response = backupResponse(action === 'prepare' ? backupSummary() : backupJSON(), {attachment: action === 'download'});
    const cancel = response.body.cancel.bind(response.body); response.body.cancel = () => {cancelled++; return cancel();}; pending.resolve(response); await h.settle();
    assert.equal(cancelled, 1); assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('favorites-export-download').getAttribute('href'), undefined); assert.deepEqual(h.syntheticClicks, []); assertPrivateSafe(h);
  }
});
test('export timeout aborts uncooperative fetch and never retains late bytes or automatically retries', async () => {
  for (const action of ['prepare', 'download']) {
    const h = await appHarness(), pending = deferred(); h.setExport(body => body.action === action ? pending.promise : backupResponse(backupSummary()));
    let waiting = h.ids.get('favorites-backup').fire('click'); if (action === 'download') {await waiting; await consent(h, 'export'); waiting = h.ids.get('favorites-export-form').fire('submit');}
    await h.settle(); [...h.timers.values()].find(timer => timer.delay === 20000).callback(); await waiting;
    assert.match(h.ids.get('favorites-export-status').textContent, /Could not prepare/); assert.equal(h.exports().length, action === 'prepare' ? 1 : 2);
    pending.resolve(backupResponse(action === 'prepare' ? backupSummary() : backupJSON(), {attachment: action === 'download'})); await h.settle();
    assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('favorites-export-prepare').disabled, true); assertPrivateSafe(h);
  }
});
test('backup refuses malformed summaries, wrong sizes/attachments, omitted metadata and private error echoes', async () => {
  for (const scenario of ['summary', 'count', 'size', 'attachment', 'document', 'partial', 'error', 'unavailable']) {
    const h = await appHarness();
    h.setExport(body => body.action === 'prepare' ? backupResponse(backupSummary(state(), scenario === 'summary' ? {name: 'PRIVATE_NAME'} : scenario === 'count' ? {count: 2} : {}))
      : scenario === 'error' || scenario === 'unavailable' ? backupResponse({error: scenario === 'error' ? 'stale_favorites_transfer' : 'favorites_unavailable', outcome: 'REJECTED', message: 'PRIVATE_ERROR'}, {status: scenario === 'error' ? 409 : 503})
      : scenario === 'size' ? backupResponse(backupJSON() + ' ', {attachment: true})
      : scenario === 'attachment' ? backupResponse(backupJSON())
      : scenario === 'partial' ? backupResponse(backupJSON([{id: 'a'.repeat(32), name: 'Name', url: 'https://example.test'}]), {attachment: true})
      : backupResponse(backupJSON().replace('"version": 1', '"version": 2'), {attachment: true}));
    await h.ids.get('favorites-backup').fire('click'); await consent(h, 'export'); await h.ids.get('favorites-export-form').fire('submit');
    assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('favorites-export-download').hidden, true); assertPrivateSafe(h); assert.deepEqual(h.syntheticClicks, []); assert.deepEqual(h.posts(), []);
  }
});
test('queued native close events cannot scrub newly reopened and newly consented dialogs', async () => {
  for (const kind of ['export', 'restore']) {
    const h = await appHarness(kind === 'restore' ? [] : [favorite()]), dialog = h.ids.get(`favorites-${kind}-dialog`); dialog.close = () => {dialog.open = false;};
    await (kind === 'export' ? readyExport(h) : readyRestore(h)); await h.ids.get(`favorites-${kind}-close`).fire('click');
    await (kind === 'export' ? readyExport(h) : readyRestore(h)); await dialog.fire('close');
    assert.equal(dialog.open, true); assert.equal(h.ids.get(`favorites-${kind}-confirm`).checked, true); assert.notEqual(h.ids.get(`favorites-${kind}-summary`).textContent, '');
  }
});
test('old restore outcomes after dismissal or pagehide preserve uncertainty without reopening a dialog', async () => {
  for (const dismissal of ['close', 'pagehide']) {
    const h = await appHarness([]), pending = deferred(); h.setPost(() => pending.promise); await readyRestore(h);
    const submitting = h.ids.get('favorites-restore-form').fire('submit'); await h.settle();
    if (dismissal === 'close') await h.ids.get('favorites-restore-close').fire('click'); else h.pagehide();
    assertCleared(h, 'restore'); pending.reject(new Error('PRIVATE_ERROR')); await submitting; h.pageshow(); await h.refresh();
    assertCleared(h, 'restore'); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.equal(h.posts().length, 1); assertPrivateSafe(h);
    await h.ids.get('favorites-restore').fire('click'); assert.equal(h.ids.get('favorites-restore-status').textContent, ''); assert.equal(h.ids.get('favorites-restore-confirm').checked, false);
  }
});
test('ACK followed by renderer restart, unavailable favorites or offline never claims a verified restore', async () => {
  for (const scenario of ['restart', 'library', 'offline']) {
    const h = await appHarness([]); h.setPost(() => {if (scenario === 'restart') h.setState(state({session_id: 'restarted'})); else if (scenario === 'library') h.setFavorites({}); else h.setState({}); return {request_acknowledged: true};});
    await readyRestore(h); await h.ids.get('favorites-restore-form').fire('submit');
    assert.match(h.ids.get('favorites-restore-status').textContent, /Restore acknowledged/); assert.match(h.ids.get('favorites-restore-status').textContent, scenario === 'restart' ? /restarted/ : scenario === 'library' ? /could not be refreshed/ : /unavailable/);
    assert.match(h.ids.get('favorites-restore-status').textContent, /not verified/); assert.equal(h.posts().length, 1);
  }
});
test('closing or timing out private download streams cancels reader and cannot construct a late Blob', async () => {
  for (const change of ['close', 'timeout', 'consent', 'pagehide']) {
    const h = await appHarness(), pending = deferred(); let reads = 0, cancelled = 0;
    h.setExport(body => body.action === 'prepare' ? backupResponse(backupSummary()) : {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': String(encoded(backupJSON()).length), 'Content-Disposition': `attachment; filename="${FAVORITES_EXPORT_FILENAME}"`}), body: {getReader: () => ({read: () => {reads++; return pending.promise;}, cancel: async () => {cancelled++;}})}});
    await h.ids.get('favorites-backup').fire('click'); await consent(h, 'export'); const preparing = h.ids.get('favorites-export-form').fire('submit'); await h.settle(); assert.equal(reads, 1);
    if (change === 'close') await h.ids.get('favorites-export-close').fire('click');
    else if (change === 'timeout') [...h.timers.values()].find(timer => timer.delay === 20000).callback();
    else if (change === 'pagehide') h.pagehide();
    else {h.ids.get('favorites-export-confirm').checked = false; await h.ids.get('favorites-export-confirm').fire('change');}
    await preparing; assert.equal(cancelled, 1); assert.equal(h.blobs.size, 0);
    pending.resolve({done: false, value: encoded(backupJSON())}); await h.settle(); assert.equal(reads, 1); assert.equal(h.created.length, 0);
  }
});
test('old export fetches cannot replace newly opened summaries, new consent or new owned object URLs', async () => {
  const h = await appHarness(), old = deferred(); h.setExport(() => old.promise);
  const preparing = h.ids.get('favorites-backup').fire('click'); await h.settle(); await h.ids.get('favorites-export-close').fire('click'); await preparing;
  h.setExport(null); await readyExport(h); const url = h.ids.get('favorites-export-download').getAttribute('href'), summary = h.ids.get('favorites-export-summary').textContent;
  old.resolve(backupResponse(backupSummary(state(), {revision: revisionID('f')}))); await h.settle();
  assert.equal(h.ids.get('favorites-export-dialog').open, true); assert.equal(h.ids.get('favorites-export-confirm').checked, true); assert.equal(h.ids.get('favorites-export-download').getAttribute('href'), url); assert.equal(h.ids.get('favorites-export-summary').textContent, summary); assert.equal(h.created.length, 1); assert.equal(h.blobs.size, 1);
});
test('old download streams cannot revoke or replace a reopened backup', async () => {
  const h = await appHarness(), late = deferred(); h.setExport(body => body.action === 'prepare' ? backupResponse(backupSummary()) : {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': String(encoded(backupJSON()).length), 'Content-Disposition': `attachment; filename="${FAVORITES_EXPORT_FILENAME}"`}), body: {getReader: () => ({read: () => late.promise, cancel: async () => {}})}});
  await h.ids.get('favorites-backup').fire('click'); await consent(h, 'export'); const preparing = h.ids.get('favorites-export-form').fire('submit'); await h.settle();
  await h.ids.get('favorites-export-close').fire('click'); await preparing; h.setExport(null); await readyExport(h); const owned = h.created[0];
  late.resolve({done: false, value: encoded(backupJSON())}); await h.settle(); assert.equal(h.ids.get('favorites-export-download').getAttribute('href'), owned); assert.equal(h.blobs.size, 1); assert.equal(h.revoked.length, 0);
});
test('dialog dismissal restores its current opener or enabled refresh fallback without late focus changes', async () => {
  for (const kind of ['export', 'restore']) {
    const h = await appHarness(kind === 'restore' ? [] : [favorite()]); await (kind === 'export' ? readyExport(h) : readyRestore(h));
    await h.ids.get(`favorites-${kind}-close`).fire('click'); assert.equal(h.focused(), h.ids.get(kind === 'export' ? 'favorites-backup' : 'favorites-restore'));
    await (kind === 'export' ? readyExport(h) : readyRestore(h)); h.offline(); await h.ids.get(`favorites-${kind}-dialog`).fire('cancel'); assert.equal(h.focused(), h.ids.get('refresh'));
    const count = h.focusCalls.length; await h.ids.get(`favorites-${kind}-dialog`).fire('close'); assert.equal(h.focusCalls.length, count);
  }
});
test('renewed renderer session uses its fresh token only after explicit review in a newly opened transfer', async () => {
  const h = await appHarness(), pending = deferred(); h.setExport(() => pending.promise);
  const waiting = h.ids.get('favorites-backup').fire('click'); await h.settle();
  h.setState(state({session_id: 'R'.repeat(32), csrf_token: 'renewed-csrf'})); h.setFavorites(favorites([favorite()], 'R'.repeat(32))); await h.refresh(); await waiting;
  assert.equal(h.exports()[0].options.signal.aborted, true); await h.ids.get('favorites-export-close').fire('click'); h.setExport(null); await h.ids.get('favorites-backup').fire('click');
  assert.equal(h.exports().at(-1).options.headers['X-ORR-CSRF'], 'renewed-csrf'); assert.equal(JSON.parse(h.exports().at(-1).options.body).expected_session, 'R'.repeat(32));
  pending.resolve(backupResponse(backupSummary())); await h.settle(); assert.equal(h.ids.get('favorites-export-confirm').checked, false); assert.equal(h.exports().length, 2);
});

test('persisted Unicode blank-name rules match Python without rejecting FEFF content', async () => {
  for (const name of ['\ufeff', '\ufeff\n', 'A\ufeffB', ' \ufeff ']) {
    const source = backupJSON([record({name})]), bytes = encoded(source);
    assert.equal(validateFavoritesDocument(bytes).text, source);
    assert.equal(validateFavoritesExportDocument(bytes, backupSummary(state(), {size_bytes: bytes.length})), bytes);
    const draft = new FavoritesRestoreDraft(state()); await draft.readFile(file(source)); assert.equal(draft.text, source);
  }
  for (const name of ['\x85', '\u1680\u2000\u200a\u2028\u2029\u202f\u205f\u3000', '\t\r\n \xa0']) assert.throws(() => validateFavoritesDocument(encoded(backupJSON([record({name})]))));
  assert.throws(() => validateFavoritesDocument(encoded('\ufeff' + backupJSON())));
});
test('quote-heavy targets that fit raw file limit still fail complete escaped restore envelope', async () => {
  const source = JSON.stringify({version: 1, favorites: [0, 1].map(i => ({id: i.toString(16).padStart(32, '0'), name: 'Quoted', url: 'https://media.example.test/' + '"'.repeat(8000), title: '', artist: '', album: '', artwork: ''}))}) + '\n';
  const bytes = encoded(source); assert.equal(bytes.length, 32310); assert.ok(requestBytes(favoritesRestoreBody(state().session_id, source)) > 64000);
  assert.equal(validateFavoritesDocument(bytes).count, 2); assert.throws(() => validateFavoritesExportDocument(bytes, backupSummary(state(), {count: 2, size_bytes: bytes.length})));
  const draft = new FavoritesRestoreDraft(state()); await draft.readFile(file(source)); assert.equal(draft.text, ''); assert.match(draft.error, /32,768/);
  const h = await appHarness([]); await readyRestore(h, file(source)); await h.ids.get('favorites-restore-form').fire('submit'); assert.equal(h.posts().length, 0); assertPrivateSafe(h);
});
