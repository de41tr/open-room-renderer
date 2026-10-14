"""Experimental decoded-PCM boundary; not connected to renderer playback.

Only the finite ``audio pcm-check`` consumer is supported today. Importing this
package does not load libasound or open any output.
"""
from .playout import (PcmFormat, PcmPlayout, PcmSink, PlayoutError,
                      PlayoutObservation, SampleAnchor, StaleGeneration)

__all__ = ["PcmFormat", "PcmPlayout", "PcmSink", "PlayoutError",
           "PlayoutObservation", "SampleAnchor", "StaleGeneration"]
