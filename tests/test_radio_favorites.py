"""UNIT/MOCK stable radio favorites; synthetic resolver I/O, no WAN or audio."""
from dataclasses import asdict
import json

import pytest

from fake_linkplay.favorites import FavoriteError, FavoritesStore
from fake_linkplay.config import Config
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context, serialize_queue_context
from fake_linkplay.radio import public_station_reference, resolve_station
from test_station_recovery import api, eof, finish
from test_station_url_refresh import ARTWORK, FIRST, FRESH, REFERENCE
from test_shuffle_queue_playback import navigate, setup as shuffled_queue

CANONICAL = "https://opml.radiotime.com/Tune.ashx?id=s12345&formats=mp3,aac&render=json"
DISPLAY = {"title": "Captured live song", "artist": "Artist", "album": "Album", "artwork": ARTWORK}


@pytest.mark.parametrize("selection", ["header", "reference"])
async def test_save_resolved_station_before_play_and_after_refresh_preserves_only_stable_fields(api, selection):
    fetches, validations = [], []

    async def fetch(url):
        assert url == CANONICAL
        fetches.append(url)
        return json.dumps({"head": {"status": "200"}, "body": [{"element": "audio",
            "is_direct": True, "media_type": "mp3", "url": FIRST if len(fetches) == 1 else FRESH}]}).encode()

    async def validate(url):
        validations.append(url)

    async def resolve(station_id):
        return await resolve_station(station_id, fetcher=fetch, validator=validate)

    api._resolve_station_id = resolve
    if selection == "header":
        queue = PlaybackQueue(source="newTuneIn", stream_id="s12345", list_name=DISPLAY["title"])
        await api._materialize_radio_queue(queue)
        assert not queue.current._radio_resolution_attempted and not api.mpv.calls
    else:
        queue = PlaybackQueue(items=[QueueItem(REFERENCE, **DISPLAY)], index=1)
        await api._replace_queue(queue)
        await api._play_queue_index(1, queue.name)
    item = queue.current
    for key, value in DISPLAY.items():
        setattr(item, key, value)
    item.metadata = "private provider metadata"
    assert item._radio_station == (item.url, "s12345")
    assert len(fetches) == 1 and validations == [FIRST]

    def save(name):
        before, calls = asdict(api.state), list(api.mpv.calls)
        io = list(fetches), list(validations)
        saved = api.favorites.add_current(name, item)
        assert saved["url"] == CANONICAL
        assert {key: saved[key] for key in DISPLAY} == DISPLAY
        assert asdict(api.state) == before and api.mpv.calls == calls
        assert (fetches, validations) == io
        assert api.state.queue is queue and queue.current is item
        return saved

    save("Station before refresh")
    if selection == "header":
        await api._play_queue_index(1, queue.name)
        assert len(fetches) == 1  # Materialization already resolved the first target.
    assert api._station_recovery is not None
    await finish(await eof(api))
    assert item.resolved_url == api.state.backend_url == FRESH
    assert len(fetches) == 2 and validations == [FIRST, FRESH]
    assert item._radio_station == (item.url, "s12345")
    save("Station after refresh")
    assert FavoritesStore(api.cfg).document() == api.favorites.document()
    persisted = json.loads(api.favorites.path.read_text())
    assert persisted["version"] == 1
    for record in persisted["favorites"]:
        assert set(record) == {"id", "name", "url", *DISPLAY}
        assert record["url"] == CANONICAL
    assert FIRST not in api.favorites.path.read_text() and FRESH not in api.favorites.path.read_text()
    assert "private provider metadata" not in api.favorites.path.read_text()


@pytest.mark.parametrize("provenance,stream_type,duration", [
    ((), "station", 0), ((FIRST,), "station", 0), ((FIRST, "s12345", "extra"), "station", 0),
    ([FIRST, "s12345"], "station", 0), ({"url": FIRST, "station_id": "s12345"}, "station", 0),
    ("s12345", "station", 0), (False, "station", 0),
    ((FRESH, "s12345"), "station", 0), ((None, "s12345"), "station", 0),
    ((FIRST, "s12345"), "track", 0), ((FIRST, "s12345"), "", 0),
    ((FIRST, "s12345"), "station", 30), ((FIRST, "s12345"), "station", -1),
    *(( (FIRST, "s12345"), "station", duration) for duration in (float("nan"), False, None, "0", 0j)),
    *(( (FIRST, station), "station", 0) for station in
      (None, 12345, "", "S12345", "t12345", "s１２３", "s1\n", "s1&account=private", "s" + "1" * 33)),
])
def test_invalid_or_stale_provenance_never_falls_back_to_temporary_url(tmp_path, provenance, stream_type, duration):
    store = FavoritesStore(Config(state_dir=str(tmp_path)))
    item = QueueItem(FIRST, stream_type=stream_type, duration=duration)
    item._radio_station = provenance
    before = asdict(item)
    with pytest.raises(FavoriteError) as caught:
        store.add_current("Station", item)
    assert caught.value.code == "no_canonical_target" and caught.value.status == 409
    assert asdict(item) == before and not store.path.exists()


def test_wire_metadata_and_provider_labels_cannot_manufacture_station_provenance(tmp_path):
    store = FavoritesStore(Config(state_dir=str(tmp_path)))
    item = QueueItem(FIRST, source="newTuneIn", id="s12345", stream_type="station",
                     metadata=json.dumps({"_radio_station": [FIRST, "s12345"]}))
    queue = PlaybackQueue(source="newTuneIn", stream_id="s12345", search_url=CANONICAL,
                          items=[item], index=1)
    wire = serialize_queue_context(queue).replace("</Track1>",
        "<_radio_station>s12345</_radio_station></Track1>")
    parsed = parse_queue_context(wire).current
    assert parsed._radio_station is None
    assert store.add_current("Untrusted labels", parsed)["url"] == FIRST
    with pytest.raises(TypeError):
        QueueItem(FIRST, _radio_station=(FIRST, "s12345"))
    item._radio_station = (FIRST, "s12345")
    item.metadata = ""
    wire = serialize_queue_context(queue)
    assert "_radio_station" not in wire and "_radio_resolution_attempted" not in wire
    assert parse_queue_context(wire).current._radio_station is None
    with pytest.raises(FavoriteError):
        store.add({"name": "Spoofed JSON", "url": FIRST, "_radio_station": [FIRST, "s12345"]})
    assert public_station_reference("s12345") == CANONICAL


async def test_save_current_preserves_active_shuffle_traversal(api):
    queue = await shuffled_queue(api)
    await navigate(api)
    item, traversal, draws = queue.current, queue._shuffle, api._shuffle_rng.calls
    before, calls = asdict(api.state), list(api.mpv.calls)
    saved = api.favorites.add_current("Shuffled favorite", item)
    assert saved["url"] == item.url
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api.state.queue is queue and queue.current is item
    assert queue._shuffle is traversal and api._shuffle_rng.calls == draws
    assert traversal.cursor == 1 and len(traversal.path) == 4
