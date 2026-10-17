"""Offline SOAP index diagnostics; no provider, mpv, device or production state."""
import datetime as dt
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from fake_linkplay.config import Config
from fake_linkplay.queue import MAX_QUEUE_TRACKS
from fake_linkplay.radio import RadioResolution
from fake_linkplay.diagnostics import Diagnostics
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from fake_linkplay.soap import SERVICE_TYPES, SOAP_NS


class RadioIndexDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                     state_dir=self.tmp.name, advertise_ip="127.0.0.1")
        # No backend methods exist: an attempted audio action fails the test.
        self.api = HttpApi(cfg, RendererState.from_config(cfg), object())
        self.api._resolve_station_id = AsyncMock(side_effect=AssertionError("No provider I/O"))
        self.api.diagnostics = Diagnostics(Path(self.tmp.name), lambda: self.api.state,
                                           version="0.1.0", commit="a" * 40)
        now = dt.datetime.now(dt.timezone.utc)
        assert self.api.diagnostics.activate({
            "test_id": "synthetic-radio-index", "commit": "a" * 40,
            "started_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(seconds=120)).isoformat()})
        app = web.Application()
        # Production SOAP handler and actual alias, without background/device tasks.
        app.router.add_post("/upnp/control/{service}", self.api.soap)
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def reject(self, context, expected_header):
        body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
                f'<u:CreateQueue xmlns:u="{SERVICE_TYPES["PlayQueue"]}">'
                f'<QueueContext>{escape(context)}</QueueContext>'
                '</u:CreateQueue></s:Body></s:Envelope>')
        response = await self.client.post("/upnp/control/PlayQueue1", data=body,
            headers={"SOAPACTION": f'"{SERVICE_TYPES["PlayQueue"]}#CreateQueue"'})
        text = await response.text()
        self.assertEqual(response.status, 500)
        self.assertEqual(response.headers.get("X-Open-Room-CreateQueue-Rejection"), expected_header)
        self.assertEqual(ET.fromstring(text).findtext(
            ".//{urn:schemas-upnp-org:control-1-0}errorCode"), "402")
        self.assertEqual(self.api.state.generation, 0)
        self.assertEqual(self.api.state.queue.count, 0)
        self.api._resolve_station_id.assert_not_awaited()
        return text

    async def test_index_guard_is_correlated_and_value_free_without_changing_fault(self):
        cases = [
            ("", "<TrackNumber>1</TrackNumber>", "PRIVATE_MARKER https://private.invalid/?token=SECRET", "format"),
            ("<Index>1</Index>", "<TrackNumber>2</TrackNumber>", "2", "conflict"),
            ("", "<TrackNumber>1</TrackNumber>", "0", "zero"),
            ("", "<TrackNumber>1</TrackNumber>", "2", "range"),
            ("", "", "1", "empty"),
        ]
        for root_fields, count, index, detail in cases:
            with self.subTest(guard=detail):
                start = len(self.api.diagnostics.events)
                source = ("" if detail == "empty" else
                          "<SourceName>newTuneIn</SourceName><StreamId>s12345</StreamId>")
                context = (f"<PlayList>{root_fields}<ListInfo>{source}"
                           f"{count}<StartPlayIndex>{escape(index)}</StartPlayIndex>"
                           "</ListInfo></PlayList>")
                response = await self.reject(context, "queue-index")
                rows = self.api.diagnostics.events[start:]
                specific = [r for r in rows if r["reason"] == "queue-index-" + detail]
                self.assertEqual(len(specific), 1)
                specific = specific[0]
                final = rows[-1]
                self.assertEqual(specific["request_id"], final["request_id"])
                for row in (specific, final):
                    self.assertEqual((row["action"], row["outcome"], row["http_code"], row["soap_code"]),
                                     ("CreateQueue", "rejected", 500, 402))
                self.assertEqual(final["reason"], "queue-index")
                exported = json.dumps(rows) + response
                for secret in ("PRIVATE_MARKER", "private.invalid", "SECRET"):
                    self.assertNotIn(secret, exported)

    async def test_other_rejection_is_not_mislabeled_and_diagnostic_failure_is_nonfatal(self):
        context = ("<PlayList><Count>2</Count><Tracks><Track1>"
                   "<URL>https://media.example.invalid/not-fetched.mp3</URL>"
                   "</Track1></Tracks></PlayList>")
        await self.reject(context, "queue-count")
        self.assertFalse(any(r["reason"].startswith("queue-index-")
                             for r in self.api.diagnostics.events))
        self.api.diagnostics.add = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("PRIVATE_MARKER"))
        index_context = ("<PlayList><ListInfo><SourceName>newTuneIn</SourceName>"
                         "<StreamId>s12345</StreamId><TrackNumber>1</TrackNumber>"
                         "<StartPlayIndex>0</StartPlayIndex></ListInfo></PlayList>")
        await self.reject(index_context, "queue-index")


    async def post_context(self, context):
        body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
                f'<u:CreateQueue xmlns:u="{SERVICE_TYPES["PlayQueue"]}">'
                f'<QueueContext>{escape(context)}</QueueContext>'
                '</u:CreateQueue></s:Body></s:Envelope>')
        return await self.client.post("/upnp/control/PlayQueue1", data=body,
            headers={"SOAPACTION": f'"{SERVICE_TYPES["PlayQueue"]}#CreateQueue"'})

    async def test_unknown_radio_count_keeps_remote_index_a_hint_until_resolution(self):
        media = "https://media.example.invalid/synthetic-station.mp3"
        self.api._resolve_station_id = AsyncMock(return_value=RadioResolution(media, "s12345"))
        self.api._resolve_playback_url = AsyncMock(side_effect=AssertionError("No media I/O"))
        for count in ("", "<TrackNumber>0</TrackNumber>"):
            for index in (1, 20):
                with self.subTest(count_present=bool(count), remote_index=index):
                    self.api._resolve_station_id.reset_mock()
                    context = ("<PlayList><ListInfo><SourceName>newTuneIn</SourceName>"
                               f"<StreamId>s12345</StreamId>{count}"
                               f"<StartPlayIndex>{index}</StartPlayIndex>"
                               "</ListInfo></PlayList>")
                    response = await self.post_context(context)
                    self.assertEqual(response.status, 200)
                    await response.read()
                    self.api._resolve_station_id.assert_awaited_once_with("s12345")
                    queue = self.api.state.queue
                    self.assertEqual((queue.count, queue.index), (1, 1))
                    self.assertEqual((queue.current.id, queue.current.url), ("s12345", media))
                    self.assertEqual(self.api.state.status, "stop")
                    self.api._resolve_playback_url.assert_not_awaited()

    async def test_unknown_count_still_bounds_index_and_checks_provider_before_effects(self):
        oversized = ("<PlayList><ListInfo><SourceName>newTuneIn</SourceName>"
                     "<StreamId>s12345</StreamId><TrackNumber>0</TrackNumber>"
                     f"<StartPlayIndex>{MAX_QUEUE_TRACKS + 1}</StartPlayIndex>"
                     "</ListInfo></PlayList>")
        await self.reject(oversized, "queue-index")
        unsupported = ("<PlayList><ListInfo><SourceName>other</SourceName>"
                       "<StreamId>s12345</StreamId><TrackNumber>0</TrackNumber>"
                       "<StartPlayIndex>1</StartPlayIndex></ListInfo></PlayList>")
        await self.reject(unsupported, "provider-source")


if __name__ == "__main__":
    unittest.main()
