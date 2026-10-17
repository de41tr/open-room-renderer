/* Offline DOM/control-flow checks, not browser or provider validation. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const tick = () => new Promise(resolve => setImmediate(resolve));
const snapshot = (session_status, extra = {}) => ({state: 'idle', active: false, session_status, account_shared: false, account_sharing_remembered: false, ...extra});

async function harness(initial) {
  let status = initial, poll, post = async () => ({}), failedStatus = false;
  const elements = {}, calls = [];
  const html = fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/tidal.html'), 'utf8');
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) elements[id] = {
    disabled: true, checked: false, textContent: '', value: '', events: {},
    addEventListener(event, callback) { this.events[event] = callback; },
    removeAttribute(name) { delete this[name]; }
  };
  const fetch = async (url, options) => {
    calls.push({url, options});
    if (options.method === 'POST') return post(url, JSON.parse(options.body));
    if (url.endsWith('/web/state')) return {ok: true, json: async () => ({csrf_token: 'synthetic-csrf'})};
    if (failedStatus) throw new Error('synthetic-private-error');
    return {ok: true, json: async () => ({...status})};
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../fake_linkplay/web_assets/tidal.js'), 'utf8'), {
    document: {getElementById: id => elements[id]}, fetch, URL, AbortSignal,
    window: {setInterval(callback) { poll = callback; }}
  });
  await tick();
  return {
    elements, calls, posts: () => calls.filter(call => call.options.method === 'POST'),
    setStatus(value) { status = value; }, setPost(callback) { post = callback; },
    failStatus() { failedStatus = true; },
    async poll() { poll(); await tick(); },
    async click(id) { if (!elements[id].disabled) elements[id].events.click(); await tick(); },
    async rememberSharing(checked = true) { elements['remember-sharing'].checked = checked; elements['remember-sharing'].events.change(); await tick(); },
    async consent() { elements.consent.checked = true; elements.consent.events.change(); await tick(); },
    async forgetConsent() { elements["forget-consent"].checked = true; elements["forget-consent"].events.change(); await tick(); }
  };
}

test('restart reports a stored local session without logging in, sharing or refreshing', async () => {
  const h = await harness(snapshot('stored'));
  await h.consent();
  assert.match(h.elements.status.textContent, /stored locally.*unverified/);
  assert.equal(h.elements.start.disabled, true);
  assert.equal(h.elements['refresh-session'].disabled, false);
  assert.equal(h.elements.share.disabled, false);
  assert.equal(h.elements.unshare.disabled, false);
  assert.equal(h.elements['remember-sharing'].checked, false);
  await h.poll();
  assert.equal(h.posts().length, 0);
  h.setPost(async (url, body) => {
    assert.ok(url.endsWith('/share'));
    assert.deepEqual(body, {confirm_lan_account_sharing: true});
    h.setStatus(snapshot('stored', {account_shared: true}));
    return {ok: true, json: async () => ({})};
  });
  await h.click('share');
  assert.equal(h.posts().length, 1);
  assert.equal(h.elements.share.disabled, true);
});

test('Remember requires an explicit Share and can upgrade transient sharing with one request', async () => {
  const h = await harness(snapshot('stored', {account_shared: true}));
  assert.equal(h.elements.share.disabled, true);
  assert.equal(h.elements['remember-sharing'].checked, false);
  await h.rememberSharing();
  await h.poll();
  assert.equal(h.posts().length, 0);
  assert.equal(h.elements.share.disabled, false);
  let finish;
  h.setPost((url, body) => {
    assert.ok(url.endsWith('/share'));
    assert.deepEqual(body, {confirm_lan_account_sharing: true, remember_account_sharing: true});
    return new Promise(resolve => { finish = resolve; });
  });
  await h.click('share');
  await h.click('share');
  assert.equal(h.posts().length, 1);
  const remembered = snapshot('stored', {account_shared: true, account_sharing_remembered: true});
  h.setStatus(remembered);
  finish({ok: true, json: async () => remembered});
  await tick();
  await h.poll();
  assert.equal(h.elements['remember-sharing'].checked, true);
  assert.equal(h.elements['remember-sharing'].disabled, true);
  assert.equal(h.elements.share.disabled, true);
  assert.equal(h.elements.unshare.disabled, false);
  assert.match(h.elements.status.textContent, /enabled and remembered across restarts/);
  assert.equal(h.posts().length, 1);
});

test('restart shows remembered active or suspended sharing and Stop durably clears the selected state', async () => {
  for (const active of [true, false]) {
    const kind = active ? 'stored' : 'expired';
    const h = await harness(snapshot(kind, {account_shared: active, account_sharing_remembered: true}));
    await h.poll();
    assert.equal(h.elements['remember-sharing'].checked, true);
    assert.equal(h.elements['remember-sharing'].disabled, true);
    assert.equal(h.elements.share.disabled, true);
    assert.equal(h.elements.unshare.disabled, false);
    assert.match(h.elements.status.textContent, active ? /enabled and remembered across restarts/ : /remembered across restarts but currently suspended.*Stop account sharing/);
    assert.equal(h.posts().length, 0);
    h.setPost(async (url, body) => {
      assert.ok(url.endsWith('/unshare'));
      assert.deepEqual(body, {stop_lan_account_sharing: true});
      const stopped = snapshot(kind);
      h.setStatus(stopped);
      return {ok: true, json: async () => stopped};
    });
    await h.click('unshare');
    await h.poll();
    assert.equal(h.elements['remember-sharing'].checked, false);
    assert.doesNotMatch(h.elements.status.textContent, /sharing.*enabled|sharing.*remembered/);
    assert.equal(h.posts().length, 1);
  }
});

test('unchecking an unsaved Remember choice stays transient and failed sharing never retries', async () => {
  const h = await harness(snapshot('stored'));
  await h.rememberSharing();
  await h.rememberSharing(false);
  assert.equal(h.posts().length, 0);
  h.setPost(async (url, body) => {
    assert.ok(url.endsWith('/share'));
    assert.deepEqual(body, {confirm_lan_account_sharing: true});
    throw new Error('SYNTHETIC_PRIVATE_ERROR');
  });
  await h.click('share');
  await h.poll();
  assert.match(h.elements.status.textContent, /Sharing was not confirmed/);
  assert.doesNotMatch(h.elements.status.textContent, /SYNTHETIC_PRIVATE/);
  assert.equal(h.posts().length, 1);
  assert.equal(h.elements.unshare.disabled, false);
});

test('expired and unsupported local sessions block new login while Unshare remains available', async () => {
  for (const kind of ['expired', 'unsupported', 'unavailable', undefined]) {
    const h = await harness(snapshot(kind));
    await h.consent();
    assert.equal(h.elements.start.disabled, true);
    assert.equal(h.elements.share.disabled, true);
    assert.equal(h.elements.play.disabled, kind !== 'expired');
    assert.equal(h.elements['refresh-session'].disabled, kind !== 'expired');
    assert.equal(h.elements.unshare.disabled, false);
    if (kind === 'expired') assert.match(h.elements.status.textContent, /expired.*explicit track test can attempt one session refresh.*Refresh the saved session/);
    h.setPost(async (url, body) => {
      assert.ok(url.endsWith('/unshare'));
      assert.deepEqual(body, {stop_lan_account_sharing: true});
      return {ok: true, json: async () => ({})};
    });
    await h.click('unshare');
    assert.equal(h.posts().length, 1);
  }
});

test('expired session permits only an explicit Play request, with no UI refresh or sharing side effect', async () => {
  const h = await harness(snapshot('expired'));
  await h.poll();
  assert.equal(h.posts().length, 0);
  assert.equal(h.elements.start.disabled, true);
  assert.equal(h.elements.share.disabled, true);
  h.elements.track.value = '42';
  h.setPost(async (url, body) => {
    assert.ok(url.endsWith('/play'));
    assert.deepEqual(body, {track_id: '42', confirm_experimental_playback: true});
    h.setStatus(snapshot('stored'));
    return {ok: true, json: async () => ({playback_requested: true})};
  });
  await h.click('play');
  await h.poll();
  assert.equal(h.posts().length, 1);
  assert.match(h.elements.status.textContent, /Playback was requested.*unverified/);
  assert.equal(h.elements.share.disabled, false);
  assert.doesNotMatch(h.elements.status.textContent, /sharing.*enabled/);
});

test('refresh marker results retain distinct recovery guidance without new login or override requests', async () => {
  for (const [error, refresh_outcome] of [['refresh_result_uncertain', 'uncertain'], ['refresh_rejected', 'rejected']]) {
    const h = await harness(snapshot('expired'));
    await h.consent();
    h.setPost(async (url, body) => {
      assert.ok(url.endsWith('/refresh'));
      assert.deepEqual(body, {confirm_session_refresh: true});
      return {ok: true, json: async () => ({state: 'error', error, refresh_outcome})};
    });
    await h.click('refresh-session');
    await h.poll();
    assert.match(h.elements.status.textContent, /Further refresh attempts are blocked.*Forget saved TIDAL session.*explicitly start a new login/);
    assert.doesNotMatch(h.elements.status.textContent, /Refresh was not started/);
    if (refresh_outcome === 'rejected') {
      assert.match(h.elements.status.textContent, /TIDAL rejected the saved refresh grant/);
      assert.doesNotMatch(h.elements.status.textContent, /uncertain|may have rotated/);
    } else assert.match(h.elements.status.textContent, /previous session refresh has an uncertain result/);
    assert.equal(h.elements.start.disabled, true);
    assert.equal(h.elements.unshare.disabled, false);
    assert.equal(h.posts().length, 1);
  }
});

test('missing session still requires consent and pending login retains Cancel', async () => {
  const h = await harness(snapshot('missing'));
  assert.equal(h.elements.start.disabled, true);
  await h.consent();
  assert.equal(h.elements.start.disabled, false);
  h.setStatus(snapshot('missing', {state: 'pending', active: true,
    user_code: 'SYNTHETIC', verification_uri: 'https://link.tidal.com/SYNTHETIC'}));
  await h.poll();
  assert.equal(h.elements.start.disabled, true);
  assert.equal(h.elements.cancel.disabled, false);
  assert.equal(h.elements.challenge.hidden, false);
  h.setStatus(snapshot('missing', {state: 'cancelled', retry_after: 5}));
  await h.poll();
  assert.equal(h.elements.start.disabled, true);
  assert.equal(h.elements.challenge.hidden, true);
  assert.equal(h.elements.verify.href, undefined);
});

test('fixed refresh errors stay actionable after polling and never automatically retry', async () => {
  const errors = {
    dependency_unavailable: /Check installation/, storage_unavailable: /check storage/,
    unsafe_debug: /Disable HTTP wire debugging/, invalid_timeout: /time limit is invalid/,
    session_unavailable: /complete device-login session/, client_mismatch: /configuration do not match/,
    invalid_response: /unsupported response/, identity_mismatch: /account did not match/,
    refresh_failed: /failed or timed out/, commit_failed: /could not be confirmed saved/
  };
  for (const [error, message] of Object.entries(errors)) {
    const h = await harness(snapshot('expired'));
    h.setPost(async (url, body) => {
      assert.ok(url.endsWith('/refresh'));
      assert.deepEqual(body, {confirm_session_refresh: true});
      return {ok: true, json: async () => ({state: 'error', error, refresh_outcome: 'uncertain'})};
    });
    await h.click('refresh-session');
    await h.poll();
    assert.match(h.elements.status.textContent, message);
    assert.match(h.elements.status.textContent, /may have rotated credentials.*Do not retry automatically/);
    assert.doesNotMatch(h.elements.status.textContent, /Login status|Login failed/);
    assert.equal(h.posts().length, 1);
  }
});

test('refresh uses one explicit request and updates expired local status without changing sharing', async () => {
  const h = await harness(snapshot('expired'));
  let finish;
  h.setPost(() => new Promise(resolve => { finish = resolve; }));
  await h.click('refresh-session');
  await h.click('refresh-session');
  assert.equal(h.posts().length, 1);
  h.setStatus(snapshot('stored'));
  finish({ok: true, json: async () => ({state: 'refreshed', refresh_outcome: 'saved'})});
  await tick();
  assert.match(h.elements.status.textContent, /stored locally.*Session refresh saved/);
  assert.equal(h.elements.share.disabled, false);
  assert.doesNotMatch(h.elements.status.textContent, /sharing.*enabled/);
});

test('failed playback is described as playback and failed status closes session actions safely', async () => {
  const h = await harness(snapshot('stored'));
  h.elements.track.value = '42';
  h.setPost(async () => ({ok: false}));
  await h.click('play');
  assert.match(h.elements.status.textContent, /Playback request was not confirmed/);
  assert.doesNotMatch(h.elements.status.textContent, /Login.*fail|Login status|synthetic-private/);
  h.failStatus();
  await h.poll();
  for (const id of ['start', 'play', 'share', 'refresh-session']) assert.equal(h.elements[id].disabled, true);
  assert.equal(h.elements.unshare.disabled, false);
  assert.equal(h.posts().length, 1);
});

test('Forget requires separate confirmation, sends once, and requires fresh login consent afterward', async () => {
  const h = await harness(snapshot('expired'));
  await h.consent();
  assert.equal(h.elements.disconnect.disabled, true);
  await h.click('disconnect');
  assert.equal(h.posts().length, 0);
  await h.forgetConsent();
  assert.equal(h.elements.disconnect.disabled, false);
  let finish;
  h.setPost((url, body) => {
    assert.ok(url.endsWith('/disconnect'));
    assert.deepEqual(body, {confirm_forget_local_session: true});
    return new Promise(resolve => { finish = resolve; });
  });
  await h.click('disconnect');
  await h.click('disconnect');
  assert.equal(h.posts().length, 1);
  h.setStatus(snapshot('missing'));
  finish({ok: true, json: async () => ({session_forgotten: true})});
  await tick();
  await h.poll();
  assert.equal(h.elements['forget-consent'].checked, false);
  assert.equal(h.elements.consent.checked, false);
  assert.equal(h.elements.start.disabled, true);
  assert.equal(h.elements.disconnect.disabled, true);
  assert.match(h.elements.status.textContent, /session was forgotten.*sharing was removed/);
  assert.equal(h.posts().length, 1);
  await h.consent();
  assert.equal(h.elements.start.disabled, false);
  assert.equal(h.posts().length, 1);
});

test('Forget handles stop-first, busy and lost responses without retries or reusing confirmation', async () => {
  for (const [error, pattern] of [
    ['disconnect_stop_first', /Stop TIDAL playback first.*No playback was stopped/],
    ['disconnect_busy', /Another renderer or TIDAL operation is active/],
    ['disconnect_storage_unavailable', /Storage may be busy or unsafe/],
    ['disconnect_unavailable', /recovery is unavailable/],
    [null, /removal was not confirmed.*no automatic retry/]
  ]) {
    const h = await harness(snapshot('expired'));
    await h.forgetConsent();
    h.setPost(async () => {
      if (!error) throw new Error('SYNTHETIC_PRIVATE_RESPONSE');
      return {ok: false, json: async () => ({error})};
    });
    await h.click('disconnect');
    await h.poll();
    assert.match(h.elements.status.textContent, pattern);
    assert.doesNotMatch(h.elements.status.textContent, /SYNTHETIC_PRIVATE/);
    assert.equal(h.elements['forget-consent'].checked, false);
    assert.equal(h.elements.disconnect.disabled, true);
    assert.equal(h.posts().length, 1);
  }
});

test('pending login cannot Forget and Cancel does not send a Forget request', async () => {
  const h = await harness(snapshot('missing', {state: 'pending', active: true}));
  await h.forgetConsent();
  assert.equal(h.elements.disconnect.disabled, true);
  h.setPost(async (url, body) => {
    assert.ok(url.endsWith('/cancel'));
    assert.deepEqual(body, {cancel: true});
    h.setStatus(snapshot('missing', {state: 'cancelled'}));
    return {ok: true, json: async () => ({state: 'cancelled'})};
  });
  await h.click('cancel');
  assert.equal(h.posts().length, 1);
});
