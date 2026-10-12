"""Conservative sanitization for media URL diagnostics, never playback input."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(
    r'(?i)https?(?::|%3a|%253a|\\u003a)(?:/|\\/|%2f|%252f|\\u002f){2}[^\s<>"]+'
)


def redact_media_urls(value: object) -> str:
    """Hide URL credentials and all query values, including unfamiliar keys.

    Raw URL structure is parsed without decoding its components. Encoded/JSON
    wrappers are masked as a whole because mixed escaping can be ambiguous.
    The original playback destination is never altered. Raw authority/resource
    path remain useful for local diagnosis; this is not general-purpose DLP.
    """
    def redact(match: re.Match[str]) -> str:
        candidate = match.group(0)
        # Mixed outer encoding and inner escaped delimiters are ambiguous.
        # Do not decode credentials into URL structure just to retain a path.
        if not re.match(r"(?i)^https?://", candidate) or "\\" in candidate:
            return "[redacted-media-url]"
        # Existing address masking deliberately uses this non-URL placeholder.
        # Preserve it across the module sanitizer and final formatter passes.
        private_placeholder = bool(re.match(r"(?i)^https?://\[private-ip\](?=[:/?#]|$)", candidate))
        if private_placeholder:
            candidate = candidate.replace("[private-ip]", "redacted.invalid", 1)
        try:
            parts = urlsplit(candidate)
            authority = parts.netloc.rsplit("@", 1)[-1]
            if not parts.hostname or "%" in authority or parts.port is not None and not 0 < parts.port < 65536:
                return "[invalid-media-url]"
            query = "[redacted]" if parts.query else ""
            if private_placeholder:
                authority = authority.replace("redacted.invalid", "[private-ip]", 1)
            return urlunsplit((parts.scheme, authority, parts.path, query,
                               "***" if parts.fragment else ""))
        except ValueError:
            return "[invalid-media-url]"
    return _URL.sub(redact, str(value))
