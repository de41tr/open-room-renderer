"""Closed, value-free reasons for CreateQueue rejection responses."""
from enum import Enum


class QueueRejectionReason(Enum):
    UNKNOWN = "unknown"
    SOAP_FRAMING = "soap-framing"
    MISSING_CONTEXT = "queue-context-missing"
    XML = "queue-xml"
    STRUCTURE = "queue-structure"
    COUNT = "queue-count"
    INDEX = "queue-index"
    # Supplemental source events only; the existing HTTP rejection stays INDEX.
    INDEX_FORMAT = "queue-index-format"
    INDEX_CONFLICT = "queue-index-conflict"
    INDEX_ZERO = "queue-index-zero"
    INDEX_RANGE = "queue-index-range"
    INDEX_EMPTY = "queue-index-empty"
    TRACK_URL = "queue-track-url"
    LOOP = "queue-loop"
    DURATION = "queue-duration"
    NAME = "queue-name"
    LIMIT = "queue-limit"
    PROVIDER_SOURCE = "provider-source"
    PROVIDER_STATION_ID = "provider-station-id"
    PROVIDER_STREAM_TYPE = "provider-stream-type"
