"""UNIT/MOCK queue control: index changes must select a different backend URL."""
import asyncio
from dataclasses import asdict
from html import escape
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay.upnp_description import avtransport_response_fields
from test_playback_generations import StatefulBackend


FIRST = "https://media.example.test/one.wav"
SECOND = "https://media.example.test/two.wav"


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    return result


def context(name="Two & tracks"):
    return (f"<PlayList><QueueName>{escape(name)}</QueueName><ListName>Local set</ListName>"
            "<ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber><StartPlayIndex>1</StartPlayIndex></ListInfo>"
            f"<Tracks><Track1><Id>synthetic-one</Id><Title>One &amp; α</Title><URL>{FIRST}</URL></Track1>"
            f"<Track2><Id>synthetic-two</Id><Title>Two &lt; β</Title><URL>{SECOND}</URL></Track2></Tracks></PlayList>")


async def create(api, value=None):
    await api._execute_soap_side_effect("CreateQueue", f"<CreateQueue><QueueContext>{escape(value or context())}</QueueContext></CreateQueue>")


async def play(api, index, name="Two & tracks"):
    await api._execute_soap_side_effect("PlayQueueWithIndex", f"<PlayQueueWithIndex><QueueName>{escape(name)}</QueueName><Index>{index}</Index></PlayQueueWithIndex>")


def assert_item(api, index, url, title):
    assert api.state.queue.index == api.state.plicurr == index
    assert api.state.plicount == 2
    assert api.state.current_url == api.mpv.url == url
    assert api.state.title == title
    fields = avtransport_response_fields("GetPositionInfo", api.state)
    assert f"<Track>{index}</Track>" in fields
    assert f"<TrackURI>{url}</TrackURI>" in fields
    browse = ET.fromstring("<Root>" + api._playqueue_response_fields("BrowseQueue") + "</Root>").findtext("QueueContext")
    parsed = parse_queue_context(browse)
    assert parsed.index == index and parsed.count == 2
    assert parsed.current.url == url
    event = ET.fromstring(api._event_body("AVTransport"))
    last_change = next(node.text for node in event.iter() if node.tag.rsplit("}", 1)[-1] == "LastChange")
    assert f'<CurrentTrack val="{index}"/>' in last_change


@pytest.mark.asyncio
async def test_two_track_queue_selects_actual_resources_and_navigation(api):
    await create(api)
    assert not api.mpv.calls
    assert api.state.plicount == 2 and api.state.plicurr == 1
    assert api.state.phase == PlaybackPhase.STOPPED
    await play(api, 1)
    assert_item(api, 1, FIRST, "One & α")
    await api._execute_soap_side_effect("Next", "<Next/>")
    assert_item(api, 2, SECOND, "Two < β")
    await api._execute_soap_side_effect("Previous", "<Previous/>")
    assert_item(api, 1, FIRST, "One & α")
    await play(api, 2)
    assert_item(api, 2, SECOND, "Two < β")
    assert [value for command, value in api.mpv.calls if command == "load"] == [FIRST, SECOND, FIRST, SECOND]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, -1, 3, "one", "1.0"])
async def test_invalid_index_has_no_effect(api, index):
    await create(api)
    await play(api, 1)
    before, calls = asdict(api.state), list(api.mpv.calls)
    with pytest.raises(SoapFault):
        await play(api, index)
    assert asdict(api.state) == before
    assert api.mpv.calls == calls


@pytest.mark.asyncio
async def test_wrong_queue_name_has_no_effect(api):
    await create(api)
    before = asdict(api.state)
    with pytest.raises(SoapFault):
        await play(api, 1, "Another queue")
    assert asdict(api.state) == before
    assert not api.mpv.calls


@pytest.mark.asyncio
async def test_queue_replacement_stops_old_backend_and_replaces_count(api):
    await create(api)
    await play(api, 2)
    await create(api, '<PlayList><QueueName>Empty</QueueName><Count>0</Count><Index>0</Index><Tracks/></PlayList>')
    assert api.mpv.idle
    assert api.state.plicount == api.state.plicurr == 0
    assert api.state.current_url == ""
    assert api.state.queue.name == "Empty"
    assert api.state.phase == PlaybackPhase.STOPPED
    with pytest.raises(SoapFault):
        await play(api, 1, "Empty")


@pytest.mark.asyncio
async def test_failed_track_switch_is_error_not_false_playing(api):
    await create(api)
    await play(api, 1)
    api.mpv.loadfile = AsyncMock(side_effect=BackendError("synthetic failure"))
    with pytest.raises(BackendError):
        await play(api, 2)
    assert api.mpv.idle
    assert api.state.plicurr == 2
    assert api.state.current_url == SECOND
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["play", "playlist", "m3u_play"])
async def test_direct_http_play_replaces_existing_queue(api, command):
    await create(api)
    await play(api, 2)
    await api.execute(ParsedCommand(command, (FIRST,)), "192.0.2.1")
    assert api.state.plicount == api.state.plicurr == 1
    assert api.state.queue.current.url == FIRST
    assert api.mpv.calls == [("load", SECOND), ("resume", None), ("stop", None), ("load", FIRST), ("resume", None)]
    assert api.state.track_source == "CustomRadio" and api.state.title == "one.wav"
    assert api.state.play_medium == "THIRD-DLNA" and api.state.album_art_uri == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["play", "playlist", "m3u_play"])
@pytest.mark.parametrize("paused", [False, True], ids=["playing", "paused"])
async def test_legacy_resolution_failure_retires_previous_audio(api, command, paused):
    await create(api)
    await play(api, 1)
    if paused:
        await api._transport_command("pause")
    reference = "https://opml.radiotime.com/Tune.ashx?id=s12345"
    entered, release = asyncio.Event(), asyncio.Event()

    async def unavailable(station_id):
        assert station_id == "s12345"
        entered.set()
        await release.wait()
        from fake_linkplay.radio import RadioResolutionError
        raise RadioResolutionError("Synthetic unavailable station")

    api._resolve_station_id = unavailable
    pending = asyncio.create_task(api.execute(ParsedCommand(command, (reference,)), "192.0.2.1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert api.mpv.idle and api.mpv.url == ""
        assert api.state.backend_url == api.state.backend_original_url == ""
        assert api.state.current_url == api.state.queue.current.url == reference
        release.set()
        with pytest.raises(BackendError, match="resolution failed"):
            await pending
        assert api.mpv.idle and api.state.phase == PlaybackPhase.ERROR
        assert [value for name, value in api.mpv.calls if name == "load"] == [FIRST]
        assert [name for name, _ in api.mpv.calls].count("stop") == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "unconfirmed"])
async def test_legacy_failed_stop_preserves_previous_selection(api, failure):
    await create(api)
    await play(api, 1)
    queue, item = api.state.queue, api.state.queue.current
    before = (api.state.current_url, api.state.title, api.state.track_metadata, api.queue_context)

    async def failed_stop():
        if failure == "rejected":
            raise BackendError("Synthetic rejected Stop")
        # A successful command reply without idle readback is not retirement.
        return {"error": "success"}

    api.mpv.stop = failed_stop
    api._resolve_playback_url = AsyncMock(return_value=SECOND)
    with pytest.raises(BackendError):
        await api.execute(ParsedCommand("play", (SECOND,)), "192.0.2.1")
    assert api.state.queue is queue and queue.current is item
    assert (api.state.current_url, api.state.title, api.state.track_metadata, api.queue_context) == before
    assert api.mpv.url == FIRST and api.state.phase == PlaybackPhase.ERROR
    api._resolve_playback_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_replacement_superseded_during_stop_cannot_publish(api):
    await create(api)
    await play(api, 1)
    queue = api.state.queue
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop

    async def delayed_stop():
        entered.set()
        await release.wait()
        await original_stop()

    api.mpv.stop = delayed_stop
    api._resolve_playback_url = AsyncMock(side_effect=lambda url: url)
    old = asyncio.create_task(api.execute(ParsedCommand("play", (SECOND,)), "192.0.2.1"))
    newer = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert api.state.queue is queue and api.state.current_url == FIRST
        third = "https://media.example.test/three.wav"
        newer = asyncio.create_task(api.execute(ParsedCommand("play", (third,)), "192.0.2.1"))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(BackendError, match="superseded"):
            await old
        assert await newer == "OK"
        assert api.state.current_url == api.mpv.url == third
        assert api.state.phase == PlaybackPhase.PLAYING
        api._resolve_playback_url.assert_awaited_once_with(third)
    finally:
        release.set()
        await asyncio.gather(*[task for task in (old, newer) if task is not None], return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_bad_xml_does_not_replace_active_queue(api):
    await create(api)
    before = asdict(api.state)
    with pytest.raises(SoapFault):
        await create(api, "<PlayList><broken></PlayList>")
    assert asdict(api.state) == before


@pytest.mark.asyncio
async def test_next_at_end_and_previous_at_start_fail_without_mutation(api):
    await create(api)
    await play(api, 1)
    with pytest.raises(SoapFault):
        await api._execute_soap_side_effect("Previous", "<Previous/>")
    assert api.state.plicurr == 1
    await play(api, 2)
    with pytest.raises(SoapFault):
        await api._execute_soap_side_effect("Next", "<Next/>")
    assert api.state.plicurr == 2


@pytest.mark.asyncio
async def test_finite_queue_advances_on_backend_eof(api):
    import asyncio
    await create(api)
    await play(api, 1)
    api.mpv.idle = True
    await api.sync_mpv_state()
    task = api._queue_advance_task
    assert task is not None
    await task
    assert_item(api, 2, SECOND, "Two < β")
    api.mpv.idle = True
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.STOPPED
    assert api.state.plicurr == 2
    assert api.mpv.idle


@pytest.mark.asyncio
async def test_owner_stop_prevents_scheduled_eof_advance(api):
    await create(api)
    await play(api, 1)
    api.mpv.idle = True
    await api.sync_mpv_state()
    task = api._queue_advance_task
    await api.execute(ParsedCommand("stop"), "192.0.2.1")
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.state.plicurr == 1
    assert api.mpv.idle
    assert [c[0] for c in api.mpv.calls].count("load") == 1


@pytest.mark.asyncio
async def test_normal_loop_and_queue_event_values(api):
    await create(api)
    await play(api, 2)
    await api._execute_soap_side_effect("SetQueueLoopMode", "<SetQueueLoopMode><LoopMode>4</LoopMode></SetQueueLoopMode>")
    for value in ["5", "shuffle", ""]:
        with pytest.raises(SoapFault):
            await api._execute_soap_side_effect("SetQueueLoopMode", f"<SetQueueLoopMode><LoopMode>{value}</LoopMode></SetQueueLoopMode>")
    xml = ET.fromstring(api._event_body("PlayQueue"))
    props = {child.tag: child.text for prop in xml for child in prop}
    assert props["CurrentPlayListName"] == "Two & tracks"
    assert props["CurrentIndex"] == "2"
    assert props["Count"] == "2"
    assert props["LoopMode"] == "4"


@pytest.mark.asyncio
async def test_queue_metadata_survives_two_xml_layers(api):
    didl = '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item id="synthetic"><dc:title>One &amp; α &lt; β</dc:title><res duration="00:00:30">https://media.example.test/one.wav</res></item></DIDL-Lite>'
    queue = f"<PlayList><ListName>Metadata</ListName><Tracks><Track1><URL>{FIRST}</URL><Metadata>{escape(didl)}</Metadata></Track1></Tracks></PlayList>"
    await create(api, queue)
    assert api.state.track_metadata == didl
    await play(api, 1, "Metadata")
    fields = ET.fromstring("<Root>" + api._playqueue_response_fields("BrowseQueue") + "</Root>")
    assert parse_queue_context(fields.findtext("QueueContext")).current.metadata == didl
    assert api.state.title == "One & α < β"


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", ["http", "soap"])
async def test_direct_replacement_does_not_reuse_old_item_resolution(api, interface):
    await create(api)
    await play(api, 1)
    await play(api, 2)
    await play(api, 1)
    third = "https://media.example.test/three.wav"
    if interface == "http":
        await api.execute(ParsedCommand("play", (third,)), "192.0.2.1")
    else:
        await api._execute_soap_side_effect("SetAVTransportURI", f"<SetAVTransportURI><CurrentURI>{third}</CurrentURI></SetAVTransportURI>")
        await api._execute_soap_side_effect("Play", "<Play/>")
    assert api.state.current_url == api.mpv.url == third
    assert api.state.queue.current.url == third
    assert api.state.plicount == api.state.plicurr == 1


@pytest.mark.asyncio
async def test_replacing_queue_cancels_stale_eof_advance_and_new_queue_advances(api):
    import asyncio
    await create(api)
    await play(api, 1)
    entered = asyncio.Event()
    async def blocked_resolve(url):
        if url == SECOND:
            entered.set()
            await asyncio.Future()
        return url
    api._resolve_playback_url = blocked_resolve
    api.mpv.idle = True
    await api.sync_mpv_state()
    old = api._queue_advance_task
    await entered.wait()
    newer = context("New queue").replace("one.wav", "three.wav").replace("two.wav", "four.wav")
    await create(api, newer)
    await play(api, 1, "New queue")
    api.mpv.idle = True
    await api.sync_mpv_state()
    await api._queue_advance_task
    assert api.state.plicurr == 2
    assert api.mpv.url == "https://media.example.test/four.wav"
    assert old.cancelled()
