"""Device-UUID-derived account-field compatibility encoding.

This format provides NO secrecy or authentication: the identifier is public,
CBC is unauthenticated, and the IV is fixed. Never use it for stored credentials.
The established ``pyaes`` dependency is loaded only when encoding is needed.
It is also required by the pinned experimental TIDAL library.
"""
from __future__ import annotations

import base64

MAX_FIELD_BYTES = 16 * 1024
MAX_ENCODED_CHARS = 4 * ((MAX_FIELD_BYTES + 2) // 3)


class TidalCodecUnavailable(RuntimeError):
    """The standards-based AES dependency is unavailable."""


def _key(short_uuid: str) -> bytes:
    # Deliberately reject invalid device identities rather than sending plaintext.
    if not isinstance(short_uuid, str) or len(short_uuid) < 16:
        raise ValueError("TIDAL field encoding requires an ASCII device UUID of at least 16 characters")
    if any(not 32 <= ord(char) <= 126 for char in short_uuid[:16]):
        raise ValueError("TIDAL field encoding requires a printable ASCII device UUID prefix")
    try:
        return short_uuid[:16].encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("TIDAL field encoding requires an ASCII device UUID prefix") from None


def _cipher(key: bytes):
    try:
        import pyaes
    except ImportError:
        raise TidalCodecUnavailable("TIDAL field encoding requires the pyaes dependency") from None
    return pyaes.AESModeOfOperationCBC(key, iv=bytes(16))


def encode_account_field(short_uuid: str, value: str) -> str:
    """Encode UTF-8 text using the advertised status/short UUID, not the UDN.

    Empty fields remain empty. Invalid UUIDs fail closed, unlike the app's
    legacy plaintext passthrough. Do not log the input or output of this call.
    """
    key = _key(short_uuid)
    if not isinstance(value, str):
        raise TypeError("TIDAL account field must be text")
    if not value:
        return ""
    if len(value) > MAX_FIELD_BYTES or "\0" in value:
        raise ValueError("Invalid TIDAL account field")
    try:
        data = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid TIDAL account field") from None
    if len(data) > MAX_FIELD_BYTES:
        raise ValueError("Invalid TIDAL account field")
    data += bytes((-len(data)) % 16)
    cipher = _cipher(key)
    encoded = b"".join(cipher.encrypt(data[offset:offset + 16]) for offset in range(0, len(data), 16))
    return base64.b64encode(encoded).decode("ascii")


def decode_account_field(short_uuid: str, value: str) -> str:
    """Decode a compatibility field; this does not verify authenticity.

    Malformed inputs raise ValueError without including field contents. NULs
    are removed throughout the decoded string, matching the consumer format.
    """
    key = _key(short_uuid)
    if not isinstance(value, str):
        raise TypeError("TIDAL account field must be text")
    if not value:
        return ""
    if len(value) > MAX_ENCODED_CHARS:
        raise ValueError("Invalid TIDAL encoded account field")
    try:
        data = base64.b64decode("".join(value.split()), validate=True)
        if not data or len(data) > MAX_FIELD_BYTES or len(data) % 16:
            raise ValueError
        cipher = _cipher(key)
        decoded = b"".join(cipher.decrypt(data[offset:offset + 16]) for offset in range(0, len(data), 16))
        return decoded.decode("utf-8").replace("\0", "")
    except (ValueError, UnicodeError):
        raise ValueError("Invalid TIDAL encoded account field") from None
