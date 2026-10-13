"""Managed-only preservation with disposable files and in-process locks.

No renderer/listener, native socket, user data or external media is involved.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
from threading import Event

import pytest

from fake_linkplay import favorites, persistence
from fake_linkplay.capabilities import configured_presets
from fake_linkplay.config import Config, PodcastPreset
from fake_linkplay.favorites import (FavoriteError, FavoritesStore, MAX_FAVORITES_BYTES,
    decode_favorites_document, encode_favorites_document)
from fake_linkplay.persistence import AtomicWriteCommittedError

EXACT = 'https://media.example.test/音楽/caf%C3%A9%2Fone?signature=a%26b%3Dc&literal=%252F&x=one+two'
RECORD = {'id': 'a' * 32, 'name': ' É té\t日本語 ', 'url': EXACT,
    'title': ' First\nline\rsecond ', 'artist': 'A & B', 'album': '音 🎵',
    'artwork': 'https://art.example.test/a%2Fb?signature=NOT_REAL&x=one+two'}


def document(records=None):
    return json.dumps({'version': 1, 'favorites': [RECORD] if records is None else records},
        ensure_ascii=False, separators=(',', ':')).encode() + b'\n'


def config(path):
    return Config(state_dir=str(path), podcast_presets=[
        PodcastPreset('Configured untouched', 'https://media.example.test/configured')])


def test_managed_only_exact_values_order_duplicates_optional_defaults_and_restart(tmp_path):
    cfg = config(tmp_path / 'source')
    source = FavoritesStore(cfg)
    records = [RECORD, {**RECORD, 'id': 'b' * 32},
        {'id': 'c' * 32, 'name': 'Minimal', 'url': EXACT}]
    configured = configured_presets(cfg)
    source.restore_empty(document(records))
    encoded = source.backup_bytes()
    payload = json.loads(encoded)
    assert payload == {'version': 1, 'favorites': [RECORD, records[1],
        {**records[2], 'title': '', 'artist': '', 'album': '', 'artwork': ''}]}
    assert all(set(item) == {'id', 'name', 'url', 'title', 'artist', 'album', 'artwork'}
        for item in payload['favorites'])
    assert b'configured' not in encoded
    assert source.effective_presets()[:1] == configured
    destination = FavoritesStore(config(tmp_path / 'destination'))
    destination.restore_empty(encoded)
    assert destination.backup_bytes() == encoded
    assert FavoritesStore(destination.cfg).backup_bytes() == encoded
    assert destination.document() == source.document()
    assert source.effective_presets()[:1] == configured_presets(cfg) == configured


@pytest.mark.parametrize('target', ['memory', 'disk'])
@pytest.mark.parametrize('records', [[], [RECORD]])
def test_empty_or_nonempty_input_never_overwrites_nonempty_target(tmp_path, monkeypatch, target, records):
    store = FavoritesStore(config(tmp_path))
    if target == 'memory':
        store.restore_empty(document())
        store.path.write_bytes(document([]))  # Memory remains authoritative too.
    else:
        store.path.write_bytes(document())
    before, memory = store.path.read_bytes(), store.backup_bytes()
    monkeypatch.setattr(favorites, 'atomic_write_text', lambda *_: pytest.fail('Refusal wrote storage'))
    with pytest.raises(FavoriteError) as exc:
        store.restore_empty(document(records))
    assert exc.value.code == 'library_not_empty'
    assert store.path.read_bytes() == before and store.backup_bytes() == memory


@pytest.mark.parametrize('existing', [False, True])
def test_empty_document_is_no_write_only_on_empty_target(tmp_path, monkeypatch, existing):
    store = FavoritesStore(config(tmp_path))
    original = b'{ "version" : 1, "favorites" : [ ] }\n'
    if existing:
        store.path.write_bytes(original)
    monkeypatch.setattr(favorites, 'atomic_write_text', lambda *_: pytest.fail('No-op wrote'))
    store.restore_empty(document([]))
    assert store.backup_bytes() == document([])
    assert store.path.exists() == existing
    if existing:
        assert store.path.read_bytes() == original


@pytest.mark.parametrize('kind', ['corrupt', 'unreadable', 'dangling', 'non-file', 'oversize'])
@pytest.mark.parametrize('startup', [False, True])
def test_unavailable_and_newly_unhealthy_storage_are_preserved(tmp_path, monkeypatch, caplog, kind, startup):
    path = tmp_path / 'favorites.json'
    store = None if startup else FavoritesStore(config(tmp_path))
    if kind == 'dangling':
        path.symlink_to('PRIVATE_MISSING')
    elif kind == 'non-file':
        path.mkdir()
    else:
        path.write_bytes(b'PRIVATE_CORRUPTION' if kind == 'corrupt' else
            b' ' * (MAX_FAVORITES_BYTES + 1) if kind == 'oversize' else document([]))
    original_open = Path.open
    def unreadable(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError('PRIVATE_READ_ERROR')
        return original_open(candidate, *args, **kwargs)
    if kind == 'unreadable':
        monkeypatch.setattr(Path, 'open', unreadable)
    if startup:
        store = FavoritesStore(config(tmp_path))
    monkeypatch.setattr(favorites, 'atomic_write_text', lambda *_: pytest.fail('Unavailable wrote'))
    with pytest.raises(FavoriteError) as exc:
        store.restore_empty(document())
    assert exc.value.code == 'library_unavailable'
    assert 'PRIVATE_' not in str(exc.value) + caplog.text
    assert store._records == ()
    assert not list(tmp_path.glob('.favorites.json.*'))
    if kind == 'dangling':
        assert path.is_symlink() and os.readlink(path) == 'PRIVATE_MISSING'
    elif kind == 'non-file':
        assert path.is_dir()
    else:
        with original_open(path, 'rb') as stream:
            assert stream.read() == (b'PRIVATE_CORRUPTION' if kind == 'corrupt' else
                b' ' * (MAX_FAVORITES_BYTES + 1) if kind == 'oversize' else document([]))


def test_startup_unavailable_latch_requires_restart_after_external_repair(tmp_path):
    cfg = config(tmp_path)
    path = tmp_path / 'favorites.json'; path.write_bytes(b'broken')
    store = FavoritesStore(cfg)
    path.write_bytes(document([]))
    for operation in (store.backup_bytes, lambda: store.restore_empty(document())):
        with pytest.raises(FavoriteError) as exc:
            operation()
        assert exc.value.code == 'library_unavailable'
    assert path.read_bytes() == document([])
    restarted = FavoritesStore(cfg); restarted.restore_empty(document())
    assert restarted.backup_bytes() == document()


def test_healthy_empty_symlink_replacement_leaves_target_unchanged(tmp_path):
    target = tmp_path / 'target.json'; target.write_bytes(document([]))
    path = tmp_path / 'favorites.json'; path.symlink_to(target.name)
    store = FavoritesStore(config(tmp_path)); store.restore_empty(document())
    assert not path.is_symlink() and path.read_bytes() == document()
    assert target.read_bytes() == document([])
    assert FavoritesStore(store.cfg).backup_bytes() == document()


@pytest.mark.parametrize('operation', ['restore', 'add'])
def test_restore_lock_covers_fresh_disk_read_through_commit(tmp_path, monkeypatch, operation):
    store = FavoritesStore(config(tmp_path))
    entered, release, second_started = Event(), Event(), Event()
    load = store._load
    def held_load():
        value = load(); entered.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(store, '_load', held_load)
    def second():
        second_started.set()
        if operation == 'restore':
            with pytest.raises(FavoriteError) as exc:
                store.restore_empty(document([{**RECORD, 'id': 'b' * 32}]))
            assert exc.value.code == 'library_not_empty'
        else:
            return store.add({'name': 'Added', 'url': EXACT})
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(store.restore_empty, document())
        assert entered.wait(5)
        other = executor.submit(second); assert second_started.wait(5)
        assert not other.done()
        release.set(); first.result(timeout=5); added = other.result(timeout=5)
    records = json.loads(store.backup_bytes())['favorites']
    assert records[0] == RECORD
    assert len(records) == (2 if operation == 'add' else 1)
    if added:
        assert records[1]['id'] == added['id']
    assert FavoritesStore(store.cfg).backup_bytes() == store.backup_bytes()


def test_add_winning_lock_refuses_restore_without_overwrite(tmp_path, monkeypatch):
    store = FavoritesStore(config(tmp_path))
    entered, release, restore_started = Event(), Event(), Event()
    commit = store._commit
    def held_commit(records):
        entered.set(); assert release.wait(5); commit(records)
    monkeypatch.setattr(store, '_commit', held_commit)
    def restore():
        restore_started.set()
        with pytest.raises(FavoriteError) as exc:
            store.restore_empty(document())
        assert exc.value.code == 'library_not_empty'
    with ThreadPoolExecutor(max_workers=2) as executor:
        added = executor.submit(store.add, {'name': 'Added', 'url': EXACT})
        assert entered.wait(5)
        restored = executor.submit(restore); assert restore_started.wait(5)
        assert not restored.done(); release.set()
        record = added.result(timeout=5); restored.result(timeout=5)
    assert [item['id'] for item in json.loads(store.backup_bytes())['favorites']] == [record['id']]


@pytest.mark.parametrize('failure', ['file-fsync', 'replace', 'directory-fsync'])
def test_single_atomic_commit_fault_boundaries_preserve_memory_disk_and_cleanup(tmp_path, monkeypatch, failure):
    original = b'{ "version": 1, "favorites": [] }\n'
    path = tmp_path / 'favorites.json'; path.write_bytes(original)
    store = FavoritesStore(config(tmp_path)); before = store.backup_bytes()
    fsync, replace = persistence.os.fsync, persistence.os.replace
    calls = []
    def fail_fsync(fd):
        calls.append('fsync')
        if failure == 'file-fsync' or failure == 'directory-fsync' and calls.count('fsync') == 2:
            raise OSError('PRIVATE_FSYNC_ERROR')
        return fsync(fd)
    def fail_replace(*args):
        calls.append('replace')
        if failure == 'replace':
            raise OSError('PRIVATE_REPLACE_ERROR')
        return replace(*args)
    monkeypatch.setattr(persistence.os, 'fsync', fail_fsync)
    monkeypatch.setattr(persistence.os, 'replace', fail_replace)
    with pytest.raises(AtomicWriteCommittedError if failure == 'directory-fsync' else OSError):
        store.restore_empty(document())
    committed = failure == 'directory-fsync'
    assert path.read_bytes() == (document() if committed else original)
    assert store.backup_bytes() == (document() if committed else before)
    assert FavoritesStore(store.cfg).backup_bytes() == store.backup_bytes()
    assert calls.count('replace') == (0 if failure == 'file-fsync' else 1)
    assert not list(tmp_path.glob('.favorites.json.*'))


BAD_DOCUMENTS = [b'null', b'[]', b'{}', b'{"version":true,"favorites":[]}',
    b'{"version":1.0,"favorites":[]}', b'{"version":1e0,"favorites":[]}',
    b'{"version":2,"favorites":[]}', b'{"version":1,"favorites":null}',
    b'{"version":1,"favorites":[],"extra":0}', b'{"version":1,"version":1,"favorites":[]}',
    b'{"version":1,"favorites":[],"\\u0066avorites":[]}', b'\xef\xbb\xbf{"version":1,"favorites":[]}',
    b'{"version":NaN,"favorites":[]}', b'{"version":Infinity,"favorites":[]}',
    b'{"version":1e999,"favorites":[]}', b'\xff', b'[' * 1100]
BAD_RECORDS = [None, [], {}, {**RECORD, 'id': 'configured:1'}, {**RECORD, 'id': 'A' * 32},
    {**RECORD, 'slot': 1}, {**RECORD, 'editable': True}, {**RECORD, 'resolved_url': EXACT},
    {**RECORD, 'name': ''}, {**RECORD, 'title': None}, {**RECORD, 'artist': 3},
    {**RECORD, 'url': False}, {**RECORD, 'name': '\x00'}, {**RECORD, 'album': '\ud800'},
    {**RECORD, 'artwork': 'file:///private'}, {**RECORD, 'url': 'https://u:p@example.test/a'},
    {**RECORD, 'url': 'https://media.example.test/proxy/%61udio'}]


@pytest.mark.parametrize('encoded', BAD_DOCUMENTS + [document([item]) for item in BAD_RECORDS
    if not isinstance(item, dict) or item.get('album') != '\ud800'] +
    [document([RECORD, RECORD]), document([{**RECORD, 'id': f'{i:032x}'} for i in range(101)]),
     document().replace(b'"name":', b'"id":"b", "name":'),
     b'{"version":1,"favorites":[{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"N",'
     b'"url":"https://example.test/a","album":"\\ud800"}]}'])
def test_complete_invalid_document_rejected_before_any_write(tmp_path, monkeypatch, encoded):
    store = FavoritesStore(config(tmp_path))
    monkeypatch.setattr(favorites, 'atomic_write_text', lambda *_: pytest.fail('Invalid document wrote'))
    with pytest.raises(ValueError):
        store.restore_empty(encoded)
    assert store.backup_bytes() == document([]) and not store.path.exists()


def test_codec_preserves_existing_error_categories_and_exact_maximum_byte_limit():
    encoded = document([])
    assert decode_favorites_document(encoded + b' ' * (MAX_FAVORITES_BYTES - len(encoded))) == ()
    with pytest.raises(ValueError, match='Persisted favorites exceeds its size limit'):
        decode_favorites_document(encoded + b' ' * (MAX_FAVORITES_BYTES - len(encoded) + 1))
    with pytest.raises(FavoriteError) as exc:
        decode_favorites_document(b'PRIVATE_INVALID_JSON')
    assert exc.value.code == 'invalid_json' and str(exc.value) == 'Invalid JSON document'
    records = decode_favorites_document(document())
    assert tuple(asdict(item) for item in records) == (RECORD,)
    assert encode_favorites_document(records) == document()
