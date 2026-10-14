"""Experimental decoded-PCM boundary; not connected to renderer playback.

The finite ``audio pcm-check`` and local-FLAC ``audio pcm-decode`` consumers are
supported today. Importing this package does not load libasound or open output.
"""
from .playout import (PcmFormat, PcmPlayout, PcmSink, PlayoutError,
                      PlayoutObservation, SampleAnchor, StaleGeneration)

__all__ = ["PcmFormat", "PcmPlayout", "PcmSink", "PlayoutError",
           "PlayoutObservation", "SampleAnchor", "StaleGeneration"]
