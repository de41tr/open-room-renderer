"""Pure DIDL-like metadata edits preserving existing text and regex behavior."""

from __future__ import annotations

import re
from html import escape


def metadata_with_art(metadata: str, art_url: str, is_live: bool | None = None) -> str:
    """Insert or replace artwork and live-state tags in metadata."""

    if not metadata:
        return metadata

    def upsert_tag(text: str, tag: str, value: str) -> str:
        escaped_value = escape(value)
        pattern = rf"<(?:\w+:)?{re.escape(tag)}[^>]*>.*?</(?:\w+:)?{re.escape(tag)}>"
        replacement = f"<{tag}>{escaped_value}</{tag}>"
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return re.sub(pattern, replacement, text, count=1, flags=re.IGNORECASE | re.DOTALL)
        insert_at = re.search(r"</(?:\w+:)?item>", text, re.IGNORECASE)
        if insert_at:
            return text[: insert_at.start()] + replacement + text[insert_at.start() :]
        return text + replacement

    updated = metadata
    if art_url:
        updated = upsert_tag(updated, "upnp:albumArtURI", art_url)
        updated = upsert_tag(updated, "song:image", art_url)
    if is_live is not None:
        updated = upsert_tag(updated, "song:isLive", "1" if is_live else "0")
    return updated


def metadata_with_url(metadata: str, url: str) -> str:
    """Replace the first DIDL ``res`` URL with the resolved media URL."""

    if not metadata or not url:
        return metadata

    def replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}{escape(url)}{match.group(3)}"

    return re.sub(r"(<res\b[^>]*>)(.*?)(</res>)", replace, metadata, count=1, flags=re.IGNORECASE | re.DOTALL)


def metadata_with_duration(metadata: str, duration_sec: int) -> str:
    """Insert or update DIDL duration metadata in seconds."""

    if not metadata:
        return metadata
    value = str(max(0, duration_sec))
    if re.search(r"<res\b[^>]*\bduration=", metadata, re.IGNORECASE):
        return re.sub(r'(<res\b[^>]*\bduration=")[^"]*(")', rf"\g<1>{value}\2", metadata, count=1, flags=re.IGNORECASE)
    return re.sub(r"(<res\b)", rf'\1 duration="{value}"', metadata, count=1, flags=re.IGNORECASE)
