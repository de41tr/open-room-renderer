"""Podcast and direct URL helpers."""

from __future__ import annotations

from urllib.parse import unquote


def decode_url(value: str) -> str:
    """URL-decode a command argument that may contain escaped separators."""

    return unquote(value)
