"""Private managed-favorites transfer through production Web admission in memory.

Only synthetic MOCK rooms and disposable files. No listener/native sockets,
external media, credentials or physical-output claims.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict
import pytest

from fake_linkplay import favorites, persistence, web_ui
from fake_linkplay.capabilities import configured_presets
from fake_linkplay.config import PodcastPreset
from fake_linkplay.favorites import FavoritesStore, decode_favorites_document, encode_favorites_document
from fake_linkplay.persistence import AtomicWriteCommittedError
from fake_linkplay.web_ui import (FAVORITES_EXPORT_FILENAME, MAX_REQUEST_BYTES, PREFIX,
    WebAdmissionError, decode_command, decode_favorites_export, prepare_favorites_export)
from test_favorites_transfer import BAD_DOCUMENTS, EXACT, RECORD, document
from test_web_dsp_transfer import room
from test_web_ui import selected

ORIGIN = 'http://127.0.0.1:8099'


def encoded(body):
    return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()


def export_body(api, action='prepare', **fields):
    return {'action': action, 'expected_session': api.web_api._session_id, **fields}


def restore_body(api, text=None, **fields):
    return {'action': 'favorite_restore', 'expected_session': api.web_api._session_id,
        'text': document().decode() if text is None else text,
        'private_import_confirmed': True, **fields}


def request(api, body, *, export=False, method='POST', headers=None, raw=None, path=None):
    data = encoded(body) if raw is None else raw
    values = CIMultiDict({'Host': '127.0.0.1:8099', 'Origin': ORIGIN,
        'X-ORR-CSRF': api.web_api._csrf_token, 'Content-Type': 'application/json',
        'Content-Length': str(len(data))})
    for key, value in headers or []:
        if value is None:
            values.popall(key, None)
        else:
            values.add(key, value)
    transport = Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: (
        ('127.0.0.1', 8099) if key == 'sockname' else default)
    return make_mocked_request(method, path or PREFIX + ('/favorites/export' if export else '/command'),
        headers=values, transport=transport,
        payload=SimpleNamespace(readexactly=AsyncMock(return_value=data)))


async def send(api, body, **kwargs):
    return await api.web_api.handle(request(api, body, **kwargs))


def protected(api):
    return {'state': asdict(api.state), 'cfg': asdict(api.cfg),
        'active_dsp': api._active_dsp_snapshot(), 'library': deepcopy(api.eq_presets),
        'calls': deepcopy(api.mpv.calls),
        'backend': (api.mpv.balance, deepcopy(api.mpv.bands), api.mpv.actual_volume, api.mpv.actual_mute),
        'files': {p.name: p.read_bytes() for p in Path(api.cfg.state_dir).iterdir()
            if p.is_file() and p.name != 'favorites.json'}}


def failure(response, code, status=409, outcome='REJECTED'):
    assert response.status == status
    body = json.loads(response.body)
    assert body['error'] == code and body['outcome'] == outcome
    assert 'PRIVATE_' not in response.text and EXACT not in response.text
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'Access-Control-Allow-Origin' not in response.headers
    return body


async def test_two_rooms_exact_private_managed_only_roundtrip_and_restart_no_active_effects(tmp_path, monkeypatch, caplog):
    source = await room(tmp_path / 'source'); destination = await room(tmp_path / 'destination')
    records = [RECORD, {**RECORD, 'id': 'b' * 32},
        {'id': 'c' * 32, 'name': 'Minimal', 'url': EXACT}]
    source.favorites.restore_empty(document(records))
    for api in (source, destination):
        api.cfg.podcast_presets = [PodcastPreset('Configured', 'https://media.example.test/configured')]
        selected(api)
    before_source, before_destination = protected(source), protected(destination)
    configured = configured_presets(destination.cfg)
    # The private export must never borrow the mixed configured presentation.
    monkeypatch.setattr(source.favorites, 'document', lambda: pytest.fail('Export used document()'))
    prepared = await send(source, export_body(source), export=True)
    assert prepared.status == 200
    summary = json.loads(prepared.body)
    assert set(summary) == {'version', 'session_id', 'revision', 'count', 'size_bytes', 'filename', 'scope'}
    assert summary['count'] == 3 and summary['scope'] == 'managed_only'
    assert summary['filename'] == FAVORITES_EXPORT_FILENAME
    assert RECORD['name'] not in prepared.text and EXACT not in prepared.text and 'NOT_REAL' not in prepared.text
    exported = await send(source, export_body(source, 'download', expected_export=summary['revision'],
        private_export_confirmed=True), export=True)
    assert exported.status == 200 and exported.body == source.favorites.backup_bytes()
    assert len(exported.body) == summary['size_bytes']
    assert exported.headers['Content-Disposition'] == f'attachment; filename="{FAVORITES_EXPORT_FILENAME}"'
    assert 'configured' not in exported.text
    restored = await send(destination, restore_body(destination, exported.text))
    assert restored.status == 200 and json.loads(restored.body) == {'request_acknowledged': True}
    assert destination.favorites.backup_bytes() == exported.body
    assert FavoritesStore(destination.cfg).backup_bytes() == exported.body
    assert destination.favorites.effective_presets()[:1] == configured
    assert protected(source) == before_source and protected(destination) == before_destination
    assert 'NOT_REAL' not in caplog.text and EXACT not in caplog.text


async def test_empty_snapshot_export_and_restore_no_write_with_configured_rows(tmp_path, monkeypatch):
    api = await room(tmp_path)
    api.cfg.podcast_presets = [PodcastPreset('Configured', EXACT)]
    monkeypatch.setattr(favorites, 'atomic_write_text', lambda *_: pytest.fail('Empty restore wrote'))
    summary = json.loads((await send(api, export_body(api), export=True)).body)
    assert summary['count'] == 0
    response = await send(api, restore_body(api, document([]).decode()))
    assert response.status == 200 and not api.favorites.path.exists()
    assert len(api.web_api.favorites_snapshot()['favorites']) == 1


@pytest.mark.parametrize('kind', ['url', 'metadata', 'order'])
async def test_revision_covers_exact_hidden_values_and_order(tmp_path, kind):
    api = await room(tmp_path)
    api.favorites.restore_empty(document([RECORD, {**RECORD, 'id': 'b' * 32}]))
    original = json.loads((await send(api, export_body(api), export=True)).body)
    if kind == 'url':
        api.favorites.set_url(RECORD['id'], EXACT + '&private=CHANGED', 'keep')
    elif kind == 'metadata':
        api.favorites.set_url(RECORD['id'], EXACT, 'clear')
    else:
        api.favorites._commit(tuple(reversed(api.favorites._records)))
    changed = json.loads((await send(api, export_body(api), export=True)).body)
    assert changed['revision'] != original['revision']
    failure(await send(api, export_body(api, 'download', expected_export=original['revision'],
        private_export_confirmed=True), export=True), 'stale_favorites_transfer')


@pytest.mark.parametrize('export', [False, True])
async def test_stale_session_rejected_before_store_operation(tmp_path, monkeypatch, export):
    api = await room(tmp_path)
    monkeypatch.setattr(api.favorites, 'backup_bytes', lambda: pytest.fail('Stale export read'))
    monkeypatch.setattr(api.favorites, 'restore_empty', lambda *_: pytest.fail('Stale restore wrote'))
    body = export_body(api, expected_session='f' * 32) if export else restore_body(api, expected_session='f' * 32)
    failure(await send(api, body, export=export), 'stale_favorites_transfer')


@pytest.mark.parametrize('export', [False, True])
@pytest.mark.parametrize('kind,status', [('host',403), ('origin',403), ('csrf',403),
    ('duplicate-origin',403), ('duplicate-csrf',403), ('duplicate-host',403), ('cross-site',403),
    ('same-site',403), ('method',405), ('query',403), ('encoding',415), ('type',415),
    ('no-length',411), ('duplicate-length',411), ('chunked',411), ('oversized',413)])
async def test_shared_admission_precedes_private_read_or_write(tmp_path, monkeypatch, export, kind, status):
    api = await room(tmp_path)
    monkeypatch.setattr(api.favorites, 'backup_bytes', lambda: pytest.fail('Unadmitted export read'))
    monkeypatch.setattr(api.favorites, 'restore_empty', lambda *_: pytest.fail('Unadmitted restore wrote'))
    body = export_body(api) if export else restore_body(api)
    headers = []
    def replace(key, value):
        headers.extend([(key, None), (key, value)])
    method, path = 'POST', PREFIX + ('/favorites/export' if export else '/command')
    if kind == 'host': replace('Host', 'attacker.example.test:8099')
    elif kind == 'origin': replace('Origin', 'https://attacker.example.test')
    elif kind == 'csrf': replace('X-ORR-CSRF', 'wrong')
    elif kind == 'duplicate-origin': headers.append(('Origin', ORIGIN))
    elif kind == 'duplicate-csrf': headers.append(('X-ORR-CSRF', api.web_api._csrf_token))
    elif kind == 'duplicate-host': headers.append(('Host', '127.0.0.1:8099'))
    elif kind in {'same-site', 'cross-site'}: headers.append(('Sec-Fetch-Site', kind))
    elif kind == 'method': method = 'GET'
    elif kind == 'query': path += '?PRIVATE_QUERY=1'
    elif kind == 'encoding': headers.append(('Content-Encoding', 'gzip'))
    elif kind == 'type': replace('Content-Type', 'text/plain')
    elif kind == 'no-length': headers.append(('Content-Length', None))
    elif kind == 'duplicate-length': headers.append(('Content-Length', str(len(encoded(body)))))
    elif kind == 'chunked': headers.append(('Transfer-Encoding', 'chunked'))
    elif kind == 'oversized': replace('Content-Length', str(MAX_REQUEST_BYTES + 1))
    response = await send(api, body, export=export, headers=headers, method=method, path=path)
    assert response.status == status and json.loads(response.body)['outcome'] == 'REJECTED'
    assert 'PRIVATE_' not in response.text


@pytest.mark.parametrize('body', [
    {'action': 'prepare'}, {'action': 'prepare', 'expected_session': 1},
    {'action': 'prepare', 'expected_session': 's'}, {'action': 'prepare', 'expected_session': 'é' * 32},
    {'action': 'prepare', 'expected_session': 'f' * 32, 'extra': 1},
    {'action': 'download', 'expected_session': 'f' * 32, 'expected_export': 'd' * 64},
    *[{'action': 'download', 'expected_session': 'f' * 32, 'expected_export': 'd' * 64,
       'private_export_confirmed': value} for value in [False, 0, 1, 'true', None]],
    {'action': 'download', 'expected_session': 'f' * 32, 'expected_export': 'D' * 64,
     'private_export_confirmed': True},
])
def test_strict_export_schema_and_fresh_consent(body):
    with pytest.raises(WebAdmissionError):
        decode_favorites_export(encoded(body))


@pytest.mark.parametrize('consent', [False, 0, 1, 'true', None])
async def test_restore_requires_explicit_consent(tmp_path, consent):
    api = await room(tmp_path)
    failure(await send(api, restore_body(api, private_import_confirmed=consent)), 'invalid_request', 400)
    assert not api.favorites.path.exists()


@pytest.mark.parametrize('data', [item for item in BAD_DOCUMENTS if item != b'\xff'] + [
    document([RECORD, RECORD]), document().replace(b'"name":', b'"name":"PRIVATE_DUP", "name":'),
    document().replace(b'"id":', b'"\\u0069d":"PRIVATE_DUP", "id":'),
    document([{**RECORD, 'slot': 1}]), document([{**RECORD, 'editable': True}]),
    document([{**RECORD, 'id': 'configured:1'}]), document([{**RECORD, 'id': 'A' * 32}]),
    document([{**RECORD, 'name': '\x00'}]), document([{**RECORD, 'title': 3}]),
    document([{**RECORD, 'artwork': None}])])
async def test_complete_invalid_restore_rejected_without_dispatch(tmp_path, monkeypatch, data):
    api = await room(tmp_path)
    monkeypatch.setattr(api.favorites, 'restore_empty', lambda *_: pytest.fail('Invalid restore dispatched'))
    failure(await send(api, restore_body(api, data.decode())), 'invalid_request', 400)


@pytest.mark.parametrize('field', ['url', 'artwork'])
@pytest.mark.parametrize('target', ['file:///private', 'https://user:pass@example.test/a',
    'https://example.test/a b', 'https://example.test/proxy/%61udio', 'https://example.test:0/a',
    'https://example.test/a\\b', 'https://example.test/<x>'])
async def test_every_audio_and_artwork_target_is_validated(tmp_path, field, target):
    api = await room(tmp_path)
    failure(await send(api, restore_body(api, document([{**RECORD, field: target}]).decode())),
        'invalid_request', 400)
    assert not api.favorites.path.exists()


def test_transfer_uses_web_target_validator_for_each_url_without_rewriting(monkeypatch):
    observed = []
    validate = web_ui.validate_url
    def observe(value):
        observed.append(value); return validate(value)
    monkeypatch.setattr(web_ui, 'validate_url', observe)
    body = {'action': 'favorite_restore', 'expected_session': 'f' * 32,
        'text': document().decode(), 'private_import_confirmed': True}
    command = decode_command(encoded(body))
    assert command.favorites_bytes == document()
    assert observed == [RECORD['url'], RECORD['artwork']]
    assert EXACT not in repr(command) and RECORD['name'] not in repr(command)
    assert prepare_favorites_export(document()) == document()


@pytest.mark.parametrize('target', ['memory', 'disk', 'corrupt', 'dangling', 'unreadable', 'startup'])
async def test_restore_refuses_conflicts_or_unhealthy_storage_with_fixed_errors(tmp_path, monkeypatch, target):
    api = await room(tmp_path)
    path = api.favorites.path
    if target == 'memory': api.favorites.restore_empty(document())
    elif target == 'disk': path.write_bytes(document())
    elif target in {'corrupt', 'startup'}: path.write_bytes(b'PRIVATE_CORRUPTION')
    elif target == 'dangling': path.symlink_to('PRIVATE_MISSING')
    else:
        open_file = Path.open
        def unreadable(candidate, *args, **kwargs):
            if candidate == path: raise PermissionError('PRIVATE_READ_ERROR')
            return open_file(candidate, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', unreadable)
    if target == 'startup':
        api.favorites = FavoritesStore(api.cfg); api.web_api._favorites = api.favorites
        path.write_bytes(document([]))
    before = protected(api)
    monkeypatch.setattr(favorites, 'atomic_write_text', lambda *_: pytest.fail('Refusal wrote'))
    code = 'favorites_not_empty' if target in {'memory', 'disk'} else 'favorites_unavailable'
    failure(await send(api, restore_body(api)), code, 409 if code == 'favorites_not_empty' else 503)
    assert protected(api) == before


@pytest.mark.parametrize('failure_kind', ['file-fsync', 'replace', 'directory-fsync', 'lost-ack'])
async def test_restore_single_dispatch_and_uncertainty_survives_readback(tmp_path, monkeypatch, failure_kind):
    api = await room(tmp_path)
    path = api.favorites.path; path.write_bytes(document([]))
    before = protected(api)
    fsync, replace, dispatch = persistence.os.fsync, persistence.os.replace, api.web_api._dispatch
    writes, dispatched = [], []
    def failing_fsync(fd):
        writes.append('fsync')
        if failure_kind == 'file-fsync' or failure_kind == 'directory-fsync' and writes.count('fsync') == 2:
            raise OSError('PRIVATE_FSYNC_ERROR')
        return fsync(fd)
    def failing_replace(*args):
        writes.append('replace')
        if failure_kind == 'replace': raise OSError('PRIVATE_REPLACE_ERROR')
        return replace(*args)
    async def dispatch_once(command):
        dispatched.append(command.action)
        result = await dispatch(command)
        if failure_kind == 'lost-ack': raise ConnectionError('PRIVATE_LOST_ACK')
        return result
    monkeypatch.setattr(persistence.os, 'fsync', failing_fsync)
    monkeypatch.setattr(persistence.os, 'replace', failing_replace)
    monkeypatch.setattr(api.web_api, '_dispatch', dispatch_once)
    response = await send(api, restore_body(api))
    body = failure(response, 'command_failed', 502, 'UNKNOWN')
    assert body.get('committed', False) == (failure_kind == 'directory-fsync')
    assert dispatched == ['favorite_restore']
    committed = failure_kind in {'directory-fsync', 'lost-ack'}
    assert api.favorites.backup_bytes() == (document() if committed else document([]))
    assert path.read_bytes() == api.favorites.backup_bytes()
    assert FavoritesStore(api.cfg).backup_bytes() == api.favorites.backup_bytes()
    assert not list(tmp_path.glob('.favorites.json.*'))
    # Subsequent redacted reads cannot change the earlier response's uncertainty.
    api.web_api.snapshot(); api.web_api.favorites_snapshot()
    assert json.loads(response.body)['outcome'] == 'UNKNOWN' and dispatched == ['favorite_restore']
    assert protected(api) == before


async def test_restore_and_export_do_not_wait_for_playback_or_dsp_locks(tmp_path):
    api = await room(tmp_path)
    before = protected(api)
    async with api._backend_lock, api._dsp_command_lock:
        restored = await asyncio.wait_for(send(api, restore_body(api)), .5)
        exported = await asyncio.wait_for(send(api, export_body(api), export=True), .5)
    assert restored.status == exported.status == 200
    assert protected(api) == before


async def test_private_values_never_appear_in_normal_presentation_summary_errors_or_logs(tmp_path, caplog):
    api = await room(tmp_path)
    record = {**RECORD, 'name': 'https://hidden.example.test/?token=PRIVATE_NAME',
        'title': 'token=PRIVATE_TITLE', 'artist': 'token=PRIVATE_ARTIST', 'album': 'token=PRIVATE_ALBUM',
        'url': 'https://media.example.test/a?token=PRIVATE_URL',
        'artwork': 'https://art.example.test/a?token=PRIVATE_ART'}
    api.favorites.restore_empty(document([record]))
    presentation = json.dumps([api.web_api.snapshot(), api.web_api.favorites_snapshot()])
    assert 'PRIVATE_' not in presentation
    summary = await send(api, export_body(api), export=True)
    assert 'PRIVATE_' not in summary.text
    response = await send(api, restore_body(api))
    assert 'PRIVATE_' not in response.text + caplog.text
    assert api.favorites.backup_bytes() == document([record])


def test_complete_escaped_request_limit_including_exact_boundary():
    base = document([]).decode()
    body = {'action': 'favorite_restore', 'expected_session': 'f' * 32,
        'text': base, 'private_import_confirmed': True}
    body['text'] += ' ' * (MAX_REQUEST_BYTES - len(encoded(body)))
    assert len(encoded(body)) == MAX_REQUEST_BYTES
    assert decode_command(encoded(body)).favorites_bytes == body['text'].encode()
    assert prepare_favorites_export(body['text'].encode()) == body['text'].encode()
    body['text'] += ' '
    with pytest.raises(WebAdmissionError): decode_command(encoded(body))
    with pytest.raises(WebAdmissionError) as exc: prepare_favorites_export(body['text'].encode())
    assert exc.value.code == 'favorites_export_unavailable'


async def test_unsupported_whole_export_never_returns_partial_library(tmp_path):
    api = await room(tmp_path)
    records = [{**RECORD, 'id': f'{i:032x}', 'title': '\\' * 1024, 'artist': '\\' * 1024,
        'album': '\\' * 1024} for i in range(5)]
    data = document(records)
    assert len(data) < MAX_REQUEST_BYTES
    assert len(encoded(restore_body(api, data.decode()))) > MAX_REQUEST_BYTES
    api.favorites.restore_empty(data)
    before = api.favorites.backup_bytes()
    failure(await send(api, export_body(api), export=True), 'favorites_export_unavailable')
    assert api.favorites.backup_bytes() == before


def test_known_quote_heavy_file_fits_alone_but_exact_restore_request_does_not():
    records = [{'id': f'{i:032x}', 'name': 'Quoted',
        'url': 'https://media.example.test/' + '"' * 8000} for i in range(2)]
    data = encode_favorites_document(decode_favorites_document(document(records)))
    envelope = encoded({'action': 'favorite_restore', 'expected_session': 'x' * 32,
        'text': data.decode(), 'private_import_confirmed': True})
    assert len(data) == 32310 and len(envelope) == 64496
    with pytest.raises(WebAdmissionError) as exc:
        prepare_favorites_export(data)
    assert exc.value.code == 'favorites_export_unavailable'
