"""Bounded changing-ICY evidence for the existing uninterrupted station lane.

One outstanding unique title at a time. Each must be received autonomously by
both GENA services before a client status query is allowed to synchronize it.
No per-title list or unbounded history is retained, even for four-hour runs.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any
from xml.etree import ElementTree as ET

from fake_linkplay.mpv_backend import LIVE_STATION_BACK_BYTES


class ICYFramer:
    interval = 1024

    def __init__(self) -> None:
        self.title = 'Bootstrap artist - Synthetic header tone'
        self.remaining = self.interval
        self.blocks = self.changes = 0
        self.last_sent_title = ''

    def encode(self, audio: bytes) -> bytes:
        result = bytearray()
        while audio:
            count = min(len(audio), self.remaining)
            result.extend(audio[:count])
            audio, self.remaining = audio[count:], self.remaining - count
            if self.remaining == 0:
                metadata = f"StreamTitle='{self.title}';".encode('utf-8')
                units = (len(metadata) + 15) // 16
                assert 0 < units <= 255
                result.extend(bytes([units]) + metadata.ljust(units * 16, b'\0'))
                self.blocks += 1
                if self.title != self.last_sent_title:
                    self.changes += 1
                    self.last_sent_title = self.title
                self.remaining = self.interval
        return bytes(result)


def didl_title(metadata: str) -> str:
    root = ET.fromstring(metadata)
    return root.findtext('.//{http://purl.org/dc/elements/1.1/}title', default='')


def metadata_matches(service: str, props: dict[str, str], title: str, url: str) -> bool:
    if props.get('TrackURI') != url:
        return False
    if service == 'PlayQueue':
        return (props.get('TrackTitle') == title and props.get('CurrentIndex') == props.get('Count') == '1'
                and ET.fromstring(props['QueueContext']).findtext('Tracks/Track1/Title') == title)
    return (didl_title(props.get('TrackMetaData', '<empty/>')) == title
            and didl_title(props.get('last:CurrentTrackMetaData', '<empty/>')) == title
            and props.get('last:CurrentTrackURI') == url)


class ICYAudit:
    """Aggregate all changes; a bounded receipt deque cannot conceal a miss."""
    services = ('AVTransport', 'PlayQueue')
    deadline_seconds = 6.0

    def __init__(self) -> None:
        self.requested = self.observed = self.surface_agreements = 0
        self.pending = False
        self.title = ''
        self.requested_at = self.max_latency = 0.0
        self.receipt_floor: dict[str, int] = {}
        self.digest = hashlib.sha256()
        self.cache_samples = self.cache_epochs = self.plateau_samples = 0
        self.cache_min = math.inf
        self.cache_max = self.forward_max = 0
        self.late_min = math.inf
        self.late_max = self.late_back_max = 0
        self.late_back_min = math.inf
        self.plateau_verified = False

    def begin(self, now: float, events: Any) -> str:
        assert not self.pending, 'Cannot overwrite an unacknowledged title'
        self.requested += 1
        self.title = f'Synthetic artist {self.requested:06d} & duo - Tone <{self.requested:06d}>'
        self.requested_at = now
        self.receipt_floor = {service: events.accepted[service] for service in self.services}
        self.pending = True
        return self.title

    def observe(self, now: float, native: str, renderer: str, events: Any,
                subscriptions: dict[str, str], url: str) -> bool:
        assert self.pending
        latency = now - self.requested_at
        assert 0 <= latency <= self.deadline_seconds, ('ICY title/callback deadline missed', self.requested,
                                                       native, renderer, latency)
        if native != self.title or renderer != self.title:
            return False
        for service in self.services:
            if events.accepted[service] <= self.receipt_floor[service] or not events.receipts[service]:
                return False
            sid, props = events.receipts[service][-1]
            if sid != subscriptions[service] or not metadata_matches(service, props, self.title, url):
                return False
        self.observed += 1
        assert self.observed == self.requested, 'An ICY epoch was skipped'
        self.digest.update(self.title.encode() + b'\0')
        self.max_latency = max(self.max_latency, latency)
        self.pending = False
        return True

    async def assert_surfaces(self, command, soap, events: Any, url: str) -> None:
        assert not self.pending and self.observed == self.surface_agreements + 1
        meta = await command('getMetaInfo')
        assert (meta['title'], meta['artist'], meta['album']) == (self.title, '', '')
        assert meta['uri'] == meta['stream_url'] == url
        for name in ('getPlayerStatus', 'getPlayerStatusEx'):
            body = await command(name)
            assert (body['Title'], body['Artist'], body['Album']) == (self.title, '', '')
            assert body['uri'] == body['stream_url'] == url
        metadata = None
        for action, uri_key, metadata_key in (
                ('GetInfoEx', 'TrackURI', 'TrackMetaData'),
                ('GetPositionInfo', 'TrackURI', 'TrackMetaData'),
                ('GetMediaInfo', 'CurrentURI', 'CurrentURIMetaData')):
            body = await soap(action)
            assert body[uri_key] == url and didl_title(body[metadata_key]) == self.title
            metadata = body[metadata_key] if metadata is None else metadata
            assert body[metadata_key] == metadata
        for action in ('BrowseQueue', 'BrowseQueueEx'):
            body = await soap(action, service='PlayQueue')
            track = ET.fromstring(body['QueueContext']).find('Tracks/Track1')
            assert track is not None and track.findtext('Title') == self.title and track.findtext('URL') == url
        for service in self.services:
            assert metadata_matches(service, events.receipts[service][-1][1], self.title, url)
        self.surface_agreements += 1

    def sample_cache(self, state: dict[str, Any], elapsed: float) -> None:
        total, forward = (state[name] for name in ('total-bytes', 'fw-bytes'))
        # libmpv total-bytes includes packet bookkeeping; forward decoding has
        # at most the station's two-second lead plus a small demux packet lead.
        assert type(total) is int and type(forward) is int and 0 <= forward <= total
        assert total <= LIVE_STATION_BACK_BYTES + 256 * 1024, ('Unbounded native cache', state)
        assert forward <= 256 * 1024, ('Unbounded forward cache', state)
        self.cache_samples += 1
        self.cache_epochs = int(elapsed // 60)
        self.cache_min = min(self.cache_min, total)
        self.cache_max = max(self.cache_max, total)
        self.forward_max = max(self.forward_max, forward)
        # Last-minute samples, after the 64 kb/s generated stream filled the
        # one-MiB history, establish turnover rather than only an absolute cap.
        if elapsed >= 180:
            self.late_min = min(self.late_min, total)
            self.late_max = max(self.late_max, total)
            self.late_back_min = min(self.late_back_min, total - forward)
            self.late_back_max = max(self.late_back_max, total - forward)
            self.plateau_samples += 1

    def finish(self, elapsed: float, framer: ICYFramer) -> None:
        assert not self.pending and self.requested == self.observed == self.surface_agreements
        assert self.observed >= 2 and framer.changes == self.requested + 1
        if elapsed >= 240:
            assert self.cache_epochs >= 4 and self.plateau_samples >= 50
            assert self.late_back_min >= LIVE_STATION_BACK_BYTES * .8, 'Cache did not reach history turnover'
            assert self.late_back_max - self.late_back_min <= 128 * 1024, 'Native history did not plateau'
            self.plateau_verified = True

    def evidence(self) -> dict[str, Any]:
        return {'requested_titles': self.requested, 'autonomous_titles': self.observed,
                'metadata_surface_agreements': self.surface_agreements,
                'all_titles_sha256': self.digest.hexdigest(), 'last_title': self.title,
                'max_callback_latency_seconds': round(self.max_latency, 6),
                'receipt_storage_limit_per_service': 32, 'one_outstanding_title': self.pending,
                'cache_samples': self.cache_samples, 'completed_60s_epochs': self.cache_epochs,
                'cache_total_max_bytes': self.cache_max, 'cache_forward_max_bytes': self.forward_max,
                'plateau_samples': self.plateau_samples,
                'late_cache_min_bytes': None if self.late_min == math.inf else self.late_min,
                'late_cache_max_bytes': self.late_max,
                'late_history_min_bytes': None if self.late_back_min == math.inf else self.late_back_min,
                'late_history_max_bytes': self.late_back_max, 'cache_plateau_verified': self.plateau_verified}
