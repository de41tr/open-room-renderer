"""Artificial interoperability vectors; no account or vendor credentials."""
import builtins

import pytest

from fake_linkplay.tidal_codec import (
    MAX_FIELD_BYTES,
    MAX_ENCODED_CHARS,
    TidalCodecUnavailable,
    decode_account_field,
    encode_account_field,
)

UUID = "0123456789abcdef-SYNTHETIC-ONLY"


@pytest.mark.parametrize("plaintext,encoded,decoded", [
    ("synthetic-token", "y8KHr68Rf8wmFOqQxBiPMw==", "synthetic-token"),
    ("0123456789abcdef", "cnJ+iB7c/QEApxhoeQm1ZQ==", "0123456789abcdef"),
    ("synthetic-client-id", "mZq/W8BEEbh1u3DaL1SujI8OHoMg3tkSnWDBe58SmB8=", "synthetic-client-id"),
    ("Grüße", "xWdnwaucq8qoOwme8qqU5g==", "Grüße"),
    ("", "", ""),
])
def test_synthetic_wire_vectors(plaintext, encoded, decoded):
    if plaintext:
        pytest.importorskip("cryptography")
    assert encode_account_field(UUID, plaintext) == encoded
    assert decode_account_field(UUID, encoded) == decoded


def test_decoder_accepts_base64_line_breaks():
    pytest.importorskip("cryptography")
    assert decode_account_field(UUID, "y8KHr68Rf8wm\nFOqQxBiPMw==\n") == "synthetic-token"


@pytest.mark.parametrize("uuid", ["", "short", "é" * 16, "\x00" * 16, "\x7f" * 16, None])
def test_invalid_identity_never_returns_plaintext(uuid):
    with pytest.raises(ValueError):
        encode_account_field(uuid, "synthetic-token")


@pytest.mark.parametrize("encoded", ["not-base64!", "YQ==", "!!!!"])
def test_malformed_fields_do_not_leak_input(encoded):
    with pytest.raises(ValueError) as error:
        decode_account_field(UUID, encoded)
    assert encoded not in str(error.value)


def test_missing_dependency_is_explicit_and_does_not_leak_input(monkeypatch):
    original = builtins.__import__

    def without_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("synthetic import failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    with pytest.raises(TidalCodecUnavailable) as error:
        encode_account_field(UUID, "synthetic-token")
    assert "synthetic-token" not in str(error.value)


def test_decoder_preserves_legacy_nul_removal_but_encoder_rejects_nuls():
    pytest.importorskip("cryptography")
    assert decode_account_field(UUID, "LOYKUT9sPwEt+k7DDzlSoA==") == "AB"
    with pytest.raises(ValueError, match="^Invalid TIDAL account field$"):
        encode_account_field(UUID, "A\0B")


@pytest.mark.parametrize("value", ["x" * (MAX_FIELD_BYTES + 1), "é" * MAX_FIELD_BYTES, "synthetic\ud800"])
def test_encoder_rejects_oversized_or_invalid_text_without_leaking(value):
    with pytest.raises(ValueError, match="^Invalid TIDAL account field$"):
        encode_account_field(UUID, value)


def test_field_size_boundary_and_wire_limit():
    pytest.importorskip("cryptography")
    value = "x" * MAX_FIELD_BYTES
    encoded = encode_account_field(UUID, value)
    assert len(encoded) == MAX_ENCODED_CHARS
    assert decode_account_field(UUID, encoded) == value
    with pytest.raises(ValueError, match="^Invalid TIDAL encoded account field$"):
        decode_account_field(UUID, encoded + " ")
