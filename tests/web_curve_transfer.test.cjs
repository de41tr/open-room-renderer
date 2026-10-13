/* Saved-curve transfer tests use synthetic DOM/fetch/stream doubles.
 * These do not establish native browser downloads, layout, focus, CSP or audio. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {CURVE_EXPORT_FILENAME, CURVE_TRANSFER_CHANGED, curveImportBody, validateCurveExportSummary, validateCurveDocument, validateCurveExportDocument, curveTransferFailure, CurveImportDraft, Controller, readExportResponse, requestBytes, DSP_KEYS, guardedAction} = require('../fake_linkplay/web_assets/controller.js');
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
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; }
const tick = () => new Promise(resolve => setImmediate(resolve));
/* Deliberately small DOM test double. It checks event/state wiring only, never
 * claims real browser semantics, CSS layout, image loading, or accessibility QA. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
async function appHarness() {
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
  const window = {RendererControl: require('../fake_linkplay/web_assets/controller.js'), setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;}, clearTimeout: id => timers.delete(id), addEventListener: (name, callback) => events.set(name, callback)};
  const fetch = async (url, options) => {
    requests.push({url, options}); let payload;
    if (url.endsWith('/dsp/export')) return exportHook ? exportHook(JSON.parse(options.body), options) : curveResponse(JSON.parse(options.body).action === 'prepare' ? curveSummary(current, {preset_id: JSON.parse(options.body).preset_id, shared: JSON.parse(options.body).preset_id === revisionID('d')}) : curveJSON(), {attachment: JSON.parse(options.body).action === 'download'});
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
  return {ids, timers, requests, blobs, created, revoked, syntheticClicks, focusCalls, focused: () => document.activeElement, setExport: fn => {exportHook = fn;}, exports: () => requests.filter(item => item.url.endsWith('/dsp/export')), pagehide: (persisted = true) => events.get('pagehide')?.({type: 'pagehide', persisted}), pageshow: () => events.get('pageshow')?.({type: 'pageshow', persisted: true}), offline: () => events.get('offline')?.({type: 'offline'}), setState: value => {current = value;}, setFavorites: value => {library = value;}, setPost: fn => {postHook = fn;}, settle, refresh: async () => {await ids.get('refresh').fire('click'); await settle();}, posts: () => requests.filter(item => item.url === '/orr/v1/web/command').map(item => JSON.parse(item.options.body))};
}
const encoded = value => new TextEncoder().encode(value);
function curveJSON({name = 'Transferred curve', plugin = 'http://example.test/private-plugin?token=PRIVATE_PLUGIN', bands = editor().bands} = {}) {
  return JSON.stringify({version: 1, presets: {[name]: {[plugin]: bands}}}, null, 2) + '\n';
}
function curveSummary(current = state(), changes = {}) {
  return {version: 1, dsp_revision: current.dsp.editor.revision, preset_id: revisionID('c'), revision: revisionID('e'), size_bytes: encoded(curveJSON()).length, filename: CURVE_EXPORT_FILENAME, scope: 'current_plugin', shared: false, band_count: 40, ...changes};
}
function curveResponse(value, {status = 200, headers = {}, attachment = false} = {}) {
  const bytes = value instanceof Uint8Array ? value : encoded(typeof value === 'string' ? value : JSON.stringify(value));
  return new Response(bytes, {status, headers: {'Content-Type': 'application/json', 'Content-Length': String(bytes.length), ...(attachment ? {'Content-Disposition': `attachment; filename="${CURVE_EXPORT_FILENAME}"`} : {}), ...headers}});
}
function file(source = curveJSON(), name = 'PRIVATE_LOCAL_FILENAME.json') {
  const bytes = typeof source === 'string' ? encoded(source) : source;
  return {name, size: bytes.length, arrayBuffer: async () => bytes.slice().buffer};
}
async function selectPreset(h, id = revisionID('c')) {h.ids.get('dsp-preset').value = id; await h.ids.get('dsp-preset').fire('change');}
async function editBand(h, key = 'a_gain', value = '4') {h.ids.get('dsp-' + key).value = value; await h.ids.get('dsp-' + key).fire('input');}
async function chooseFile(h, value = file()) {h.ids.get('curve-import-file').files = [value]; h.ids.get('curve-import-file').value = value.name; await h.ids.get('curve-import-file').fire('change');}
async function consent(h, kind) {h.ids.get(`curve-${kind}-confirm`).checked = true; await h.ids.get(`curve-${kind}-confirm`).fire('change');}
async function readyImport(h, source = file()) {await h.ids.get('dsp-import').fire('click'); await chooseFile(h, source); await consent(h, 'import');}
async function readyExport(h, id = revisionID('c')) {await selectPreset(h, id); await h.ids.get('dsp-export').fire('click'); await consent(h, 'export'); await h.ids.get('curve-export-form').fire('submit');}
function privatePreview(h) {return ['curve-import-summary','curve-import-status','curve-export-summary','curve-export-status','notice-text'].map(id => h.ids.get(id).textContent).join('\n');}
function assertPrivateSafe(h) {assert.doesNotMatch(privatePreview(h), /PRIVATE_PLUGIN|PRIVATE_LOCAL_FILENAME|PRIVATE_NAME|PRIVATE_ERROR|example\.test/);}
function assertCleared(h, kind) {
  assert.equal(h.ids.get(`curve-${kind}-dialog`).open, false);
  assert.equal(h.ids.get(`curve-${kind}-confirm`).checked, false);
  assert.equal(h.ids.get(`curve-${kind}-summary`).textContent, '');
  assert.equal(h.ids.get(`curve-${kind}-status`).textContent, '');
  if (kind === 'export') {assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('curve-export-download').getAttribute('href'), undefined); assert.equal(h.ids.get('curve-export-download').hidden, true);}
  else {assert.equal(h.ids.get('curve-import-file').value, ''); assert.equal(h.ids.get('curve-import-file').files.length, 0);}
}

test('curve summaries contain only exact safe metadata for the selected saved context', () => {
  const preset = editor().presets[1], summary = curveSummary();
  assert.equal(validateCurveExportSummary(summary, revisionID('a'), preset), summary);
  for (const changes of [{version: true}, {version: 2}, {dsp_revision: revisionID('b')}, {preset_id: revisionID('d')}, {revision: 'not-a-revision'}, {revision: 'E'.repeat(64)}, {size_bytes: 0}, {size_bytes: 1.5}, {size_bytes: 32769}, {size_bytes: true}, {scope: 'all'}, {shared: true}, {shared: 0}, {band_count: 39}, {band_count: true}, {filename: '../private.json'}, {name: 'PRIVATE_NAME'}, {plugin: 'PRIVATE_PLUGIN'}]) assert.throws(() => validateCurveExportSummary({...summary, ...changes}, revisionID('a'), preset));
  const missing = {...summary}; delete missing.shared; assert.throws(() => validateCurveExportSummary(missing, revisionID('a'), preset));
});
test('curve validator preserves authored bytes, record order, floats, negative gain and escaped JSON', () => {
  const source = curveJSON({name: '🎵 Café "curve"', bands: [...editor().bands].reverse().map(band => band.param_name === 'a_gain' ? {...band, value: -1.75} : band)});
  const bytes = encoded(source), summary = curveSummary(state(), {size_bytes: bytes.length});
  assert.equal(validateCurveDocument(bytes), source); assert.equal(validateCurveExportDocument(bytes, summary), bytes);
  assert.equal(new TextDecoder().decode(bytes), source);
});
test('curve validator rejects incomplete, ambiguous, malformed, multi-context and nonnumeric files', () => {
  const base = JSON.parse(curveJSON()), name = Object.keys(base.presets)[0], plugin = Object.keys(base.presets[name])[0];
  const invalid = [null, [], {}, {...base, extra: 1}, {...base, version: true}, {...base, version: 2}, {...base, presets: {}}, {...base, presets: {...base.presets, Other: base.presets[name]}}, {...base, presets: {[name]: {...base.presets[name], Other: editor().bands}}}];
  for (const bands of [editor().bands.slice(1), [...editor().bands, editor().bands[0]], editor().bands.map((band, index) => index ? band : {...band, value: '1000'}), editor().bands.map((band, index) => index ? band : {...band, value: true}), editor().bands.map((band, index) => index ? band : {...band, value: 999999}), editor().bands.map((band, index) => index ? band : {...band, param_name: 'x_freq'}), editor().bands.map((band, index) => index ? band : {...band, other: 1}), editor().bands.map((band, index) => index === 1 ? editor().bands[0] : band)]) invalid.push({...base, presets: {[name]: {[plugin]: bands}}});
  invalid.forEach(value => assert.throws(() => validateCurveDocument(encoded(JSON.stringify(value)))));
  for (const source of [curveJSON().replace('"version": 1', '"version": 1.0'), curveJSON().replace('"version": 1', '"version": 1e0'), curveJSON().replace('"version": 1,', '"version": 1, "\\u0076ersion": 1,'), curveJSON().replace('"value": 1000', '"value": 1000, "value": 1000'), curveJSON().replace('"value": 1000', '"value": NaN'), '\ufeff' + curveJSON(), '{} garbage', '[[[[[[[[{}]]]]]]]]']) assert.throws(() => validateCurveDocument(encoded(source)));
  assert.throws(() => validateCurveDocument(new Uint8Array([0xff])));
  assert.throws(() => validateCurveDocument(new Uint8Array(32769)));
  assert.throws(() => validateCurveDocument(encoded(curveJSON({name: 'bad\nname'}))));
  assert.throws(() => validateCurveDocument(encoded(curveJSON({plugin: 'x'.repeat(8193)}))));
});
test('curve export reader retains playlist defaults and enforces fixed curve attachment bytes', async () => {
  const bytes = encoded(curveJSON()), signal = new AbortController().signal;
  const result = await readExportResponse(curveResponse(bytes, {attachment: true}), signal, bytes.length, CURVE_EXPORT_FILENAME);
  assert.deepEqual(result.bytes, bytes);
  await assert.rejects(readExportResponse(curveResponse(bytes, {attachment: true}), signal, bytes.length));
  for (const headers of [{'Content-Type': 'text/plain'}, {'Content-Length': String(bytes.length + 1)}, {'Content-Length': '32769'}, {'Content-Disposition': 'attachment; filename="other.json"'}]) await assert.rejects(readExportResponse(curveResponse(bytes, {attachment: true, headers}), signal, bytes.length, CURVE_EXPORT_FILENAME));
});
test('file and complete escaped envelope limits are independent and exact', async () => {
  let source = curveJSON();
  source += ' '.repeat(32768 - requestBytes(curveImportBody(revisionID('a'), source)));
  assert.equal(requestBytes(curveImportBody(revisionID('a'), source)), 32768);
  assert.ok(encoded(source).length < 32768);
  const draft = new CurveImportDraft(state()); await draft.readFile(file(source)); assert.equal(draft.text, source);
  const summary = curveSummary(state(), {size_bytes: encoded(source).length}); assert.equal(validateCurveExportDocument(encoded(source), summary).length, encoded(source).length);
  await draft.readFile(file(source + ' ')); assert.equal(draft.text, ''); assert.match(draft.error, /32,768/);
  assert.throws(() => validateCurveExportDocument(encoded(source + ' '), {...summary, size_bytes: encoded(source + ' ').length}));
  let reads = 0; await draft.readFile({size: 32769, arrayBuffer() {reads++; throw new Error();}}); assert.equal(reads, 0);
});
test('local import reads are replaced, cancelled and scrubbed without retaining filenames', async () => {
  const draft = new CurveImportDraft(state()), pending = deferred();
  const first = draft.readFile({name: 'PRIVATE_LOCAL_FILENAME.json', size: encoded(curveJSON()).length, arrayBuffer: () => pending.promise});
  const latest = curveJSON({name: 'Latest'}); await draft.readFile(file(latest));
  pending.resolve(encoded(curveJSON()).buffer); await first; assert.equal(draft.text, latest);
  const bytes = encoded(latest); await draft.readFile({size: bytes.length, arrayBuffer: async () => bytes.buffer}); assert.ok(bytes.every(value => value === 0));
  draft.cancel(); assert.equal(draft.text, ''); assert.equal(draft.reading, false);
});
test('import is fenced by library availability in controller and guarded actions', async () => {
  const current = soundState(editor({library_unavailable: true}));
  assert.throws(() => guardedAction(current, 'dsp_preset_import', {text: curveJSON()}), /Saved EQ presets could not be loaded/);
  const calls = []; const control = new Controller(async (path, options) => {calls.push(path); return {status: 200, body: options.method === 'GET' ? path.endsWith('/state') ? current : favorites() : {request_acknowledged: true}};});
  await control.refresh(); assert.equal(control.canMutate('dsp_preset_import'), false);
  assert.equal((await control.mutate(curveImportBody(revisionID('a'), curveJSON()), 'Import')).kind, 'blocked');
  assert.ok(calls.every(path => path !== '/orr/v1/web/command'));
});
test('saved-only export requires deliberate selection, fresh unchecked consent and actual link click', async () => {
  const h = await appHarness(); assert.equal(h.ids.get('dsp-export').disabled, true);
  await selectPreset(h, revisionID('b')); await h.ids.get('dsp-export').fire('click'); assert.equal(h.exports().length, 0);
  await selectPreset(h); const notice = h.ids.get('notice-text').textContent;
  await h.ids.get('dsp-export').fire('click');
  assert.deepEqual(JSON.parse(h.exports()[0].options.body), {action: 'prepare', expected_dsp: revisionID('a'), preset_id: revisionID('c')});
  assert.equal(h.ids.get('curve-export-confirm').checked, false); assert.equal(h.ids.get('curve-export-prepare').disabled, true);
  await h.ids.get('curve-export-form').fire('submit'); assert.equal(h.exports().length, 1);
  assert.match(h.ids.get('curve-export-summary').textContent, /1 saved curve.*Current plugin only.*40 band parameters/);
  await consent(h, 'export'); await h.ids.get('curve-export-form').fire('submit');
  assert.deepEqual(JSON.parse(h.exports()[1].options.body), {action: 'download', expected_dsp: revisionID('a'), preset_id: revisionID('c'), expected_export: revisionID('e'), private_export_confirmed: true});
  assert.equal(h.ids.get('curve-export-download').getAttribute('download'), CURVE_EXPORT_FILENAME);
  assert.equal(h.ids.get('curve-export-download').hidden, false); assert.equal(h.created.length, 1); assert.deepEqual(h.syntheticClicks, []);
  assert.equal(await h.blobs.get(h.created[0]).text(), curveJSON());
  await h.ids.get('curve-export-form').fire('submit'); assert.equal(h.exports().length, 2);
  const clicked = await h.ids.get('curve-export-download').fire('click'); assert.equal(clicked.defaultPrevented, false); assert.match(h.ids.get('curve-export-status').textContent, /Download requested/);
  assert.deepEqual(h.posts(), []); assert.equal(h.ids.get('notice-text').textContent, notice); assertPrivateSafe(h);
});
test('shared saved export explicitly discloses omission of other contexts without naming them', async () => {
  const h = await appHarness(); await readyExport(h, revisionID('d'));
  assert.match(h.ids.get('curve-export-summary').textContent, /Shared preset: other stored contexts are deliberately omitted/);
  assertPrivateSafe(h);
});
test('curve controls refuse unconfirmed, offline, unavailable library and dirty/review drafts', async () => {
  for (const changed of [soundState(editor({library_unavailable: true})), soundState(null, {status: 'PENDING'}), soundState(null, {status: 'UNKNOWN'})]) {
    const h = await appHarness(); await selectPreset(h); h.setState(changed); await h.refresh();
    for (const id of ['dsp-import', 'dsp-export']) {assert.equal(h.ids.get(id).disabled, true); await h.ids.get(id).fire('click');}
    assert.equal(h.exports().length, 0); assert.equal(h.ids.get('curve-import-dialog').open, false); assert.equal(h.ids.get('stop').disabled, false);
  }
  const h = await appHarness(); await selectPreset(h); await editBand(h);
  for (const id of ['dsp-import', 'dsp-export']) {assert.equal(h.ids.get(id).disabled, true); await h.ids.get(id).fire('click');}
  assert.equal(h.exports().length, 0); assert.equal(h.ids.get('curve-import-dialog').open, false);
  h.setPost(() => {throw new Error('lost response');}); await h.ids.get('dsp-band-form').fire('submit');
  assert.equal(h.ids.get('dsp-import').disabled, true); assert.equal(h.ids.get('dsp-export').disabled, true);
});
test('import sends exact original UTF-8 once, preserves its name and never loads or applies', async () => {
  const h = await appHarness(), source = curveJSON({name: 'Café "literal" %26 name', bands: [...editor().bands].reverse()});
  await h.ids.get('dsp-import').fire('click'); await chooseFile(h, file(source));
  assert.equal(h.ids.get('curve-import-file').value, ''); assert.equal(h.ids.get('curve-import-file').files.length, 0);
  assert.match(h.ids.get('curve-import-summary').textContent, /1 named curve · 1 plugin · 40 band parameters/);
  assert.equal(h.ids.get('curve-import-confirm').checked, false); assert.equal(h.ids.get('curve-import-submit').disabled, true);
  await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 0);
  await consent(h, 'import'); await h.ids.get('curve-import-form').fire('submit');
  assert.deepEqual(h.posts(), [curveImportBody(revisionID('a'), source)]);
  assert.equal(h.ids.get('curve-import-summary').textContent, ''); assert.equal(h.ids.get('curve-import-file').value, ''); assert.equal(h.ids.get('curve-import-confirm').checked, false);
  assert.match(h.ids.get('curve-import-status').textContent, /Import acknowledged.*Select and Load separately/);
  assert.equal(h.ids.get('curve-import-submit').disabled, true); await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 1);
  assert.equal(h.ids.get('dsp-a_gain').value, '0'); assert.equal(h.ids.get('eq-current').textContent, 'Bypassed · Default'); assert.equal(h.ids.get('balance-current').textContent, 'Center'); assertPrivateSafe(h);
});
test('import pending double clicks, changed observations and independent Stop never resend or hide PENDING', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(body => body.action === 'stop' ? {request_acknowledged: true} : pending.promise);
  await readyImport(h); const submitting = h.ids.get('curve-import-form').fire('submit'); await h.settle();
  await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 1); assert.match(h.ids.get('curve-import-status').textContent, /^PENDING/);
  h.setState(soundState(editor({revision: revisionID('f')}))); await h.refresh(); assert.match(h.ids.get('curve-import-status').textContent, /^PENDING/);
  await h.ids.get('stop').fire('click'); assert.deepEqual(h.posts().map(body => body.action), ['dsp_preset_import', 'stop']);
  pending.resolve({request_acknowledged: true}); await submitting; assert.match(h.ids.get('curve-import-status').textContent, /Import acknowledged/);
});
test('import rejected and UNKNOWN outcomes stay one-shot, private-safe and distinct from matching readback', async () => {
  for (const outcome of ['REJECTED', 'UNKNOWN', 'ERROR', 'lost']) {
    const h = await appHarness(); h.setPost(() => {h.setState(soundState(editor({revision: revisionID('f'), presets: [...editor().presets, {id: revisionID('f'), name: 'Transferred curve', kind: 'saved', shared: false}]}))); if (outcome === 'lost') throw new Error('PRIVATE_ERROR'); return {__status: 409, outcome, error: 'preset_conflict', message: 'PRIVATE_ERROR PRIVATE_PLUGIN PRIVATE_NAME PRIVATE_LOCAL_FILENAME.json'};});
    await readyImport(h); await h.ids.get('curve-import-form').fire('submit'); await h.refresh();
    assert.match(h.ids.get('curve-import-status').textContent, outcome === 'REJECTED' ? /^ERROR.*already in use/ : /^UNKNOWN.*No retry was sent/);
    assert.equal(h.ids.get('curve-import-submit').disabled, true); await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 1); assertPrivateSafe(h);
    if (outcome !== 'REJECTED') {assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); h.setPost(() => ({request_acknowledged: true})); await h.ids.get('stop').fire('click'); assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown/);}
  }
});
test('server import codes are rendered as fixed generic guidance and never echo file details', async () => {
  const fallback = 'The saved curve request was rejected. Review the file and current saved presets before starting again.';
  for (const code of ['__proto__','constructor','toString','unknown']) assert.equal(curveTransferFailure(code), fallback);
  for (const code of ['curve_context_mismatch','preset_conflict','preset_library_unavailable','curve_library_full','stale_dsp','invalid_curve','__proto__']) {
    const h = await appHarness(); h.setPost(() => ({__status: 409, outcome: 'REJECTED', error: code, message: 'PRIVATE_ERROR PRIVATE_PLUGIN PRIVATE_NAME'}));
    await readyImport(h); await h.ids.get('curve-import-form').fire('submit'); assertPrivateSafe(h);
    assert.match(h.ids.get('curve-import-status').textContent, /^ERROR/); assert.equal(h.posts().length, 1);
  }
});
test('import malformed, oversize and escaped-oversize files never enable submission', async () => {
  let envelopeLarge = curveJSON(); envelopeLarge += ' '.repeat(32768 - encoded(envelopeLarge).length);
  for (const source of [file('not JSON PRIVATE_ERROR'), file(new Uint8Array([0xff])), file(envelopeLarge), {size: 32769, name: 'PRIVATE_LOCAL_FILENAME', arrayBuffer: () => {throw new Error('must not read');}}]) {
    const h = await appHarness(); await h.ids.get('dsp-import').fire('click'); await chooseFile(h, source);
    assert.equal(h.ids.get('curve-import-submit').disabled, true); await consent(h, 'import'); await h.ids.get('curve-import-form').fire('submit');
    assert.equal(h.posts().length, 0); assert.match(h.ids.get('curve-import-status').textContent, /32,768/); assertPrivateSafe(h);
  }
});
test('Close, Cancel, Escape and pagehide clear both transfer dialogs and their private resources', async () => {
  for (const kind of ['export','import']) for (const dismissal of ['close','cancel','escape','pagehide']) {
    const h = await appHarness(); await (kind === 'export' ? readyExport(h) : readyImport(h));
    if (dismissal === 'escape') await h.ids.get(`curve-${kind}-dialog`).fire('cancel');
    else if (dismissal === 'pagehide') h.pagehide();
    else await h.ids.get(`curve-${kind}-${dismissal}`).fire('click');
    assertCleared(h, kind); if (kind === 'export') assert.equal(h.revoked.length, 1);
    h.pageshow(); await h.refresh(); assertCleared(h, kind);
  }
});
test('consent withdrawal clears source and Blob, invalidates in-flight exports and requires fresh review', async () => {
  const h = await appHarness(); await readyExport(h); h.ids.get('curve-export-confirm').checked = false; await h.ids.get('curve-export-confirm').fire('change');
  assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('curve-export-download').getAttribute('href'), undefined);
  await consent(h, 'export'); await h.ids.get('curve-export-form').fire('submit'); assert.equal(h.exports().length, 2);
  await h.ids.get('curve-export-close').fire('click'); await readyImport(h);
  h.ids.get('curve-import-confirm').checked = false; await h.ids.get('curve-import-confirm').fire('change');
  assert.equal(h.ids.get('curve-import-summary').textContent, ''); await consent(h, 'import'); await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 0);
  await chooseFile(h); assert.equal(h.ids.get('curve-import-confirm').checked, false); await consent(h, 'import'); await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 1);
});
test('stale revision, restart, DSP uncertainty, library loss, dirty draft and offline discard transfers permanently', async () => {
  for (const kind of ['export','import']) for (const scenario of ['revision','restart','pending','library','dirty','offline']) {
    const h = await appHarness(); await (kind === 'export' ? readyExport(h) : readyImport(h));
    if (scenario === 'offline') h.offline();
    else if (scenario === 'dirty') await editBand(h);
    else {h.setState(scenario === 'revision' ? soundState(editor({revision: revisionID('f')})) : scenario === 'restart' ? state({session_id: 'restarted'}) : scenario === 'pending' ? soundState(null, {status: 'PENDING'}) : soundState(editor({library_unavailable: true}))); await h.refresh();}
    assert.equal(h.ids.get(`curve-${kind}-confirm`).checked, false); assert.equal(h.ids.get(`curve-${kind}-summary`).textContent, ''); assert.equal(h.blobs.size, 0);
    assert.equal(h.ids.get(`curve-${kind}-status`).textContent, CURVE_TRANSFER_CHANGED);
    h.setState(state()); await h.refresh(); if (scenario === 'dirty') await h.ids.get('dsp-discard').fire('click');
    await consent(h, kind); await h.ids.get(`curve-${kind}-form`).fire('submit');
    assert.equal(h.posts().length, 0); assert.equal(h.exports().length, kind === 'export' ? 2 : 0);
  }
});
test('late local files cannot revive dismissed, stale, replaced or consent-cleared import drafts', async () => {
  for (const change of ['close','stale','replace','consent','pagehide']) {
    const h = await appHarness(), pending = deferred(); await h.ids.get('dsp-import').fire('click');
    const reading = chooseFile(h, {name: 'PRIVATE_LOCAL_FILENAME', size: encoded(curveJSON()).length, arrayBuffer: () => pending.promise}); await h.settle();
    if (change === 'close') {await h.ids.get('curve-import-close').fire('click'); await h.ids.get('dsp-import').fire('click');}
    else if (change === 'pagehide') h.pagehide();
    else if (change === 'stale') {h.setState(soundState(editor({revision: revisionID('f')}))); await h.refresh();}
    else if (change === 'replace') await chooseFile(h, file(curveJSON({name: 'New chosen curve'})));
    else {h.ids.get('curve-import-confirm').checked = false; await h.ids.get('curve-import-confirm').fire('change');}
    pending.resolve(encoded(curveJSON()).buffer); await reading;
    assert.equal(h.ids.get('curve-import-confirm').checked, false); assert.equal(h.ids.get('curve-import-submit').disabled, true); assert.equal(h.posts().length, 0); assertPrivateSafe(h);
    if (change !== 'replace') assert.equal(h.ids.get('curve-import-summary').textContent, '');
    else {await consent(h, 'import'); await h.ids.get('curve-import-form').fire('submit'); assert.equal(JSON.parse(h.posts()[0].text).presets['New chosen curve'] !== undefined, true);}
  }
});
test('export double clicks are single-shot and late responses never revive dismissed or reopened contexts', async () => {
  for (const action of ['prepare','download']) for (const change of ['close','stale','consent','pagehide']) {
    const h = await appHarness(), pending = deferred(); let cancelled = 0;
    h.setExport(body => body.action === action ? pending.promise : curveResponse(curveSummary()));
    await selectPreset(h); let waiting = h.ids.get('dsp-export').fire('click');
    if (action === 'download') {await waiting; await consent(h, 'export'); waiting = h.ids.get('curve-export-form').fire('submit');}
    await h.settle(); await h.ids.get('dsp-export').fire('click'); await h.ids.get('curve-export-form').fire('submit');
    assert.equal(h.exports().length, action === 'prepare' ? 1 : 2);
    if (change === 'stale') {h.setState(soundState(editor({revision: revisionID('f')}))); await h.refresh();}
    else if (change === 'consent' && action === 'download') {h.ids.get('curve-export-confirm').checked = false; await h.ids.get('curve-export-confirm').fire('change');}
    else if (change === 'pagehide') h.pagehide();
    else await h.ids.get('curve-export-close').fire('click');
    await waiting;
    const response = curveResponse(action === 'prepare' ? curveSummary() : curveJSON(), {attachment: action === 'download'});
    const cancel = response.body.cancel.bind(response.body); response.body.cancel = () => {cancelled++; return cancel();}; pending.resolve(response); await h.settle();
    assert.equal(cancelled, 1); assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('curve-export-download').getAttribute('href'), undefined); assert.deepEqual(h.syntheticClicks, []);
  }
});
test('export timeout aborts an uncooperative fetch, discards late bytes and never automatically retries', async () => {
  const h = await appHarness(), pending = deferred(); h.setExport(() => pending.promise); await selectPreset(h);
  const waiting = h.ids.get('dsp-export').fire('click'); await h.settle();
  [...h.timers.values()].find(timer => timer.delay === 20000).callback(); await waiting;
  assert.match(h.ids.get('curve-export-status').textContent, /Could not prepare/); assert.equal(h.exports().length, 1);
  pending.resolve(curveResponse(curveSummary())); await h.settle(); assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('curve-export-summary').textContent, '');
});
test('export rejects unsafe summaries, truncated files, wrong attachments and private error echoes', async () => {
  for (const scenario of ['summary','size','attachment','document','error']) {
    const h = await appHarness(); await selectPreset(h);
    h.setExport(body => body.action === 'prepare' ? curveResponse(curveSummary(state(), scenario === 'summary' ? {plugin: 'PRIVATE_PLUGIN'} : {})) : scenario === 'error' ? curveResponse({error: 'curve_export_unavailable', outcome: 'REJECTED', message: 'PRIVATE_ERROR'}, {status: 409}) : scenario === 'size' ? curveResponse(curveJSON() + ' ', {attachment: true}) : scenario === 'attachment' ? curveResponse(curveJSON()) : curveResponse(curveJSON().replace('"version": 1','"version": 2'), {attachment: true}));
    await h.ids.get('dsp-export').fire('click'); await consent(h, 'export'); await h.ids.get('curve-export-form').fire('submit');
    assert.equal(h.blobs.size, 0); assert.equal(h.ids.get('curve-export-download').hidden, true); assertPrivateSafe(h); assert.deepEqual(h.syntheticClicks, []); assert.deepEqual(h.posts(), []);
  }
});
test('queued close events and old import outcomes cannot scrub or repopulate a newly opened dialog', async () => {
  for (const kind of ['export','import']) {
    const h = await appHarness(), dialog = h.ids.get(`curve-${kind}-dialog`); dialog.close = () => {dialog.open = false;};
    await (kind === 'export' ? readyExport(h) : readyImport(h)); await h.ids.get(`curve-${kind}-close`).fire('click');
    await (kind === 'export' ? readyExport(h) : readyImport(h)); await dialog.fire('close');
    assert.equal(dialog.open, true); assert.equal(h.ids.get(`curve-${kind}-confirm`).checked, true); assert.notEqual(h.ids.get(`curve-${kind}-summary`).textContent, '');
  }
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise); await readyImport(h);
  const submitting = h.ids.get('curve-import-form').fire('submit'); await h.settle(); await h.ids.get('curve-import-close').fire('click');
  pending.reject(new Error('PRIVATE_ERROR')); await submitting; await h.ids.get('dsp-import').fire('click');
  assert.equal(h.ids.get('curve-import-status').textContent, ''); assert.equal(h.ids.get('curve-import-summary').textContent, ''); assert.equal(h.ids.get('curve-import-confirm').checked, false);
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assertPrivateSafe(h);
});
test('late import outcomes after pagehide stay in the main notice but never reopen the dialog', async () => {
  const h = await appHarness(), pending = deferred(); h.setPost(() => pending.promise); await readyImport(h);
  const submitting = h.ids.get('curve-import-form').fire('submit'); await h.settle(); h.pagehide(); assertCleared(h, 'import');
  pending.reject(new Error('PRIVATE_ERROR')); await submitting; h.pageshow(); await h.refresh();
  assertCleared(h, 'import'); assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN'); assert.equal(h.posts().length, 1); assertPrivateSafe(h);
});
test('ACK with a renderer restart or unavailable library does not claim a verified durable save', async () => {
  for (const scenario of ['restart','library','offline']) {
    const h = await appHarness(); h.setPost(() => {h.setState(scenario === 'restart' ? state({session_id: 'restarted'}) : scenario === 'library' ? soundState(editor({library_unavailable: true})) : {}); return {request_acknowledged: true};});
    await readyImport(h); await h.ids.get('curve-import-form').fire('submit');
    assert.match(h.ids.get('curve-import-status').textContent, /Import acknowledged/);
    assert.match(h.ids.get('curve-import-status').textContent, scenario === 'restart' ? /restarted/ : scenario === 'library' ? /Saved EQ presets could not be loaded/ : /unavailable/);
    assert.doesNotMatch(h.ids.get('curve-import-status').textContent, /verified|successfully/); assert.equal(h.posts().length, 1);
  }
});

test('closing during a private export stream cancels the reader and never constructs a late Blob', async () => {
  const h = await appHarness(), pending = deferred(); let reads = 0, cancelled = 0;
  h.setExport(body => body.action === 'prepare' ? curveResponse(curveSummary()) : {status: 200, headers: new Headers({'Content-Type': 'application/json', 'Content-Length': String(encoded(curveJSON()).length), 'Content-Disposition': `attachment; filename="${CURVE_EXPORT_FILENAME}"`}), body: {getReader: () => ({read: () => {reads++; return pending.promise;}, cancel: async () => {cancelled++;}})}});
  await selectPreset(h); await h.ids.get('dsp-export').fire('click'); await consent(h, 'export');
  const preparing = h.ids.get('curve-export-form').fire('submit'); await h.settle(); assert.equal(reads, 1);
  await h.ids.get('curve-export-close').fire('click'); await preparing; assert.equal(cancelled, 1); assertCleared(h, 'export');
  pending.resolve({done: false, value: encoded(curveJSON())}); await h.settle(); assert.equal(reads, 1); assert.equal(h.created.length, 0);
});
test('an old export fetch cannot replace a reopened dialog or its newly consented file', async () => {
  const h = await appHarness(), old = deferred(); h.setExport(() => old.promise); await selectPreset(h);
  const preparing = h.ids.get('dsp-export').fire('click'); await h.settle(); await h.ids.get('curve-export-close').fire('click'); await preparing;
  h.setExport(body => curveResponse(body.action === 'prepare' ? curveSummary() : curveJSON(), {attachment: body.action === 'download'}));
  await readyExport(h); const url = h.ids.get('curve-export-download').getAttribute('href'), summary = h.ids.get('curve-export-summary').textContent;
  old.resolve(curveResponse(curveSummary(state(), {revision: revisionID('f')}))); await h.settle();
  assert.equal(h.ids.get('curve-export-dialog').open, true); assert.equal(h.ids.get('curve-export-confirm').checked, true);
  assert.equal(h.ids.get('curve-export-download').getAttribute('href'), url); assert.equal(h.ids.get('curve-export-summary').textContent, summary);
  assert.equal(h.created.length, 1); assert.equal(h.blobs.size, 1);
});
test('a fresh local file selection always clears prior consent and stale local buffers are erased', async () => {
  const h = await appHarness(); await readyImport(h); await chooseFile(h, file(curveJSON({name: 'Replacement'})));
  assert.equal(h.ids.get('curve-import-confirm').checked, false); assert.equal(h.ids.get('curve-import-submit').disabled, true);
  const draft = new CurveImportDraft(state()), pending = deferred(), bytes = encoded(curveJSON());
  const reading = draft.readFile({size: bytes.length, arrayBuffer: () => pending.promise}); draft.cancel(); pending.resolve(bytes.buffer); await reading;
  assert.equal(draft.text, ''); assert.ok(bytes.every(value => value === 0));
});
test('curve dialog dismissal restores its current opener or enabled refresh fallback without late focus changes', async () => {
  for (const kind of ['export','import']) {
    const h = await appHarness(); await (kind === 'export' ? readyExport(h) : readyImport(h));
    await h.ids.get(`curve-${kind}-close`).fire('click'); assert.equal(h.focused(), h.ids.get(`dsp-${kind}`));
    await (kind === 'export' ? readyExport(h) : readyImport(h)); h.offline();
    await h.ids.get(`curve-${kind}-dialog`).fire('cancel'); assert.equal(h.focused(), h.ids.get('refresh'));
    const count = h.focusCalls.length; await h.ids.get(`curve-${kind}-dialog`).fire('close'); assert.equal(h.focusCalls.length, count);
  }
});

test('known post-replacement durability uncertainty remains UNKNOWN despite matching library readback', async () => {
  const h = await appHarness();
  h.setPost(() => {
    h.setState(soundState(editor({revision: revisionID('f'), presets: [...editor().presets, {id: revisionID('f'), name: 'Transferred curve', kind: 'saved', shared: false}]})));
    return {__status: 502, outcome: 'UNKNOWN', error: 'command_failed', effect: 'LIBRARY_COMMITTED_DURABILITY_UNKNOWN', message: 'PRIVATE_ERROR PRIVATE_PLUGIN'};
  });
  await readyImport(h); await h.ids.get('curve-import-form').fire('submit'); await h.refresh();
  assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
  assert.equal(h.ids.get('curve-import-status').textContent, 'UNKNOWN · The saved preset library was replaced, but disk durability is unconfirmed. Matching readback cannot confirm durability. No retry was sent.');
  assert.match(h.ids.get('notice-text').textContent, /library was replaced.*disk durability is unconfirmed/); assertPrivateSafe(h);
  await h.ids.get('curve-import-form').fire('submit'); assert.equal(h.posts().length, 1);
  h.setPost(() => ({request_acknowledged: true})); await h.ids.get('stop').fire('click');
  assert.match(h.ids.get('notice-text').textContent, /Earlier request outcome unknown: Import saved EQ curve \(library replaced; durability unconfirmed\)/);
  assert.match(h.ids.get('curve-import-status').textContent, /^UNKNOWN.*Matching readback cannot confirm durability/);
});
test('unexpected import effect metadata never leaks or invents a known library replacement', async () => {
  const h = await appHarness(); h.setPost(() => ({__status: 502, outcome: 'UNKNOWN', effect: 'PRIVATE_ERROR', message: 'PRIVATE_PLUGIN'}));
  await readyImport(h); await h.ids.get('curve-import-form').fire('submit');
  assert.match(h.ids.get('curve-import-status').textContent, /^UNKNOWN.*library may have changed/);
  assert.doesNotMatch(h.ids.get('curve-import-status').textContent, /library was replaced/); assertPrivateSafe(h);
});

test('inconsistent or malformed durability effects cannot claim a known library replacement', async () => {
  const valid = {__status: 502, outcome: 'UNKNOWN', error: 'command_failed', effect: 'LIBRARY_COMMITTED_DURABILITY_UNKNOWN', message: 'PRIVATE_ERROR'};
  for (const changes of [{__status: 400}, {__status: 200}, {error: 'other'}, {error: undefined}, {outcome: 'ERROR'}, {outcome: undefined}, {effect: 'PRIVATE_ERROR'}]) {
    const h = await appHarness(); h.setPost(() => ({...valid, ...changes}));
    await readyImport(h); await h.ids.get('curve-import-form').fire('submit'); await h.refresh();
    assert.equal(h.ids.get('notice-kind').textContent, 'UNKNOWN');
    assert.match(h.ids.get('curve-import-status').textContent, /^UNKNOWN.*library may have changed/);
    assert.doesNotMatch(privatePreview(h), /library was replaced|library replaced; durability unconfirmed/); assertPrivateSafe(h);
    assert.equal(h.ids.get('curve-import-submit').disabled, true);
  }
});
