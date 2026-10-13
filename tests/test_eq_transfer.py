"""UNIT: strict saved-curve transfers, without a renderer, files or sockets."""
from __future__ import annotations

from copy import deepcopy
import json
import math

import pytest

from fake_linkplay.eq_presets import DEFAULT_EQ_PLUGIN, default_eq_bands
from fake_linkplay.eq_transfer import (
    EqTransferCurve,
    MAX_EQ_TRANSFER_BYTES,
    MAX_EQ_TRANSFER_PLUGIN_BYTES,
    decode_eq_transfer,
    encode_eq_transfer,
    valid_transfer_name,
)


ERROR = "Invalid saved EQ curve transfer"
PRIVATE_PLUGIN = "https://user:synthetic-password@example.test/eq?token=synthetic-secret"


def document(*, name="Saved", plugin=DEFAULT_EQ_PLUGIN, bands=None):
    return {"version": 1, "presets": {
        name: {plugin: default_eq_bands() if bands is None else bands},
    }}


def encode_document(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def assert_invalid(encoded):
    with pytest.raises(ValueError) as raised:
        decode_eq_transfer(encoded)
    assert str(raised.value) == ERROR
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_roundtrip_preserves_private_plugin_unicode_record_order_and_numeric_types():
    bands = default_eq_bands()
    bands[0]["value"] = 30
    bands[1]["value"] = -0.0
    bands[2]["value"] = 0.12345678901234568
    bands[3]["value"] = 5.0
    bands = [{"value": item["value"], "param_name": item["param_name"]}
             for item in reversed(bands)]
    before = deepcopy(bands)
    name = "Écoute 50% 🎵 e\u0301"
    plugin = PRIVATE_PLUGIN + "#%2F-🎵"
    encoded = encode_eq_transfer(name, plugin, bands)
    restored = decode_eq_transfer(encoded)

    assert isinstance(restored, EqTransferCurve)
    assert restored.name == name
    assert restored.plugin == plugin
    assert restored.bands == before
    assert bands == before
    assert json.loads(encoded) == document(name=name, plugin=plugin, bands=before)
    assert [type(item["value"]) for item in restored.bands] == [
        type(item["value"]) for item in bands]
    assert [list(item) for item in restored.bands] == [list(item) for item in bands]
    negative_zero = next(item["value"] for item in restored.bands
                         if item["param_name"] == "a_gain")
    assert math.copysign(1, negative_zero) == -1
    assert encode_eq_transfer(restored.name, restored.plugin, restored.bands) == encoded
    assert plugin not in repr(restored)
    assert "synthetic-secret" not in repr(restored)


def test_encoded_and_decoded_data_never_alias_the_callers_curves():
    bands = default_eq_bands()
    encoded = encode_eq_transfer("Saved", DEFAULT_EQ_PLUGIN, bands)
    first = decode_eq_transfer(encoded)
    second = decode_eq_transfer(encoded)
    assert first.bands is not bands
    assert all(a is not b for a, b in zip(first.bands, bands))
    assert first.bands is not second.bands
    assert all(a is not b for a, b in zip(first.bands, second.bands))
    bands[1]["value"] = 6
    first.bands[1]["value"] = -6
    assert second.bands[1]["value"] == 0
    assert decode_eq_transfer(encoded).bands == second.bands


@pytest.mark.parametrize("name", [
    "Saved", "50% listening", "Literal %2F and %25", "flat", "Custom",
    "{Other=Saved}", "e\u0301", "Écoute 🎵", "a" * 256, "é" * 128, "🎵" * 64,
])
def test_canonical_visible_literal_names_are_accepted_without_normalization(name):
    assert valid_transfer_name(name)
    assert decode_eq_transfer(encode_document(document(name=name))).name == name
    assert decode_eq_transfer(encode_eq_transfer(name, DEFAULT_EQ_PLUGIN, default_eq_bands())).name == name


@pytest.mark.parametrize("name", [
    None, True, 1, {}, [], "", " ", " Saved", "Saved ", "\u00a0Saved", "Saved\u2003",
    "{Name=Saved}", "{Name=Saved,Id=1}", "{Name= Saved }",
    "Flat", "Bass Boost", "Treble Boost",
    "a" * 257, "é" * 129, "🎵" * 65,
    "Saved\x00", "Saved\n", "Saved\x1f", "Saved\x7f", "Saved\x85",
    "Saved\ud800", "Saved\udfff", "Saved\ufffe", "Saved\uffff",
    "https://example.test/curve", "HTTP://example.test/curve", "file:///private/curve",
    "ftp://example.test/curve", "token=synthetic-secret", "PASSWORD=synthetic-secret",
    "auth_token=synthetic-secret", "api_key=synthetic-secret", "signature=synthetic-secret",
    "Curve ?opaque=synthetic-secret", "https:\\/\\/example.test/curve",
    "https%3A%2F%2Fexample.test/curve", "file%253A%252F%252Fprivate/curve",
    "%74oken%3Dsynthetic-secret", "%2574oken%253Dsynthetic-secret",
    "%FF", "%ED%A0%80", "Saved%00", "Saved%257F",
])
def test_unsafe_noncanonical_private_and_builtin_names_are_rejected(name):
    assert valid_transfer_name(name) is False
    with pytest.raises(ValueError, match=f"^{ERROR}$"):
        encode_eq_transfer(name, DEFAULT_EQ_PLUGIN, default_eq_bands())
    if isinstance(name, str):
        assert_invalid(encode_document(document(name=name)))


@pytest.mark.parametrize("plugin", [
    DEFAULT_EQ_PLUGIN, PRIVATE_PLUGIN, "private:literal-%2F", "plugin 🎵",
    " leading and trailing spaces ", "x" * MAX_EQ_TRANSFER_PLUGIN_BYTES,
    "é" * (MAX_EQ_TRANSFER_PLUGIN_BYTES // 2),
])
def test_private_plugin_is_bounded_but_never_redacted_or_normalized(plugin):
    decoded = decode_eq_transfer(encode_document(document(plugin=plugin)))
    assert decoded.plugin == plugin
    assert decode_eq_transfer(encode_eq_transfer(decoded.name, decoded.plugin, decoded.bands)) == decoded


@pytest.mark.parametrize("plugin", [
    None, True, 1, {}, [], "", " ", "\u00a0", "plugin\x00", "plugin\n", "plugin\x7f",
    "plugin\x85", "plugin\ud800", "plugin\udfff", "plugin\ufffe", "plugin\uffff",
    "x" * (MAX_EQ_TRANSFER_PLUGIN_BYTES + 1),
    "é" * (MAX_EQ_TRANSFER_PLUGIN_BYTES // 2 + 1),
])
def test_plugin_rejects_empty_invalid_unicode_controls_and_overflow(plugin):
    with pytest.raises(ValueError, match=f"^{ERROR}$"):
        encode_eq_transfer("Saved", plugin, default_eq_bands())
    if isinstance(plugin, str):
        assert_invalid(encode_document(document(plugin=plugin)))


@pytest.mark.parametrize("level", ["version", "presets", "name", "plugin", "param_name", "value"])
@pytest.mark.parametrize("escaped_duplicate", [False, True])
def test_duplicate_json_fields_are_rejected_at_every_depth(level, escaped_duplicate):
    raw = encode_document(document()).decode("utf-8")
    key = {"version": "version", "presets": "presets", "name": "Saved",
           "plugin": DEFAULT_EQ_PLUGIN, "param_name": "param_name", "value": "value"}[level]
    quoted_key = json.dumps(key)
    duplicated_key = json.dumps(key)
    if escaped_duplicate:
        duplicated_key = '"\\u%04x%s"' % (ord(key[0]), key[1:])
    raw = raw.replace(quoted_key + ":", duplicated_key + ":null," + quoted_key + ":", 1)
    assert_invalid(raw.encode("utf-8"))


@pytest.mark.parametrize("version", [None, True, False, "1", 1.0, 0, 2, [], {}])
def test_version_must_be_exact_integer_one(version):
    payload = document()
    payload["version"] = version
    assert_invalid(encode_document(payload))


@pytest.mark.parametrize("payload", [None, True, 1, "Saved", [], {}, {"version": 1}, {"presets": {}}])
def test_document_requires_exact_object_and_both_top_level_fields(payload):
    assert_invalid(encode_document(payload))


@pytest.mark.parametrize("presets", [None, True, 1, "Saved", [], {}, {"Saved": None}, {"Saved": []},
                                      {"Saved": {}}, {"Saved": "plugin"}])
def test_exactly_one_name_and_one_plugin_mapping_are_required(presets):
    assert_invalid(encode_document({"version": 1, "presets": presets}))


def test_multiple_names_and_plugin_contexts_are_rejected_without_selection():
    payload = document()
    payload["presets"]["Other"] = {DEFAULT_EQ_PLUGIN: default_eq_bands()}
    assert_invalid(encode_document(payload))
    payload = document()
    payload["presets"]["Saved"]["other:plugin"] = default_eq_bands()
    assert_invalid(encode_document(payload))


@pytest.mark.parametrize("key,value", [
    ("source", "wifi"), ("source_name", "wifi"), ("channelMode", "Stereo"),
    ("EQBandL", []), ("EQBandR", []), ("balance", 0), ("enabled", True),
    ("name", "Saved"), ("plugin", DEFAULT_EQ_PLUGIN), ("bands", []), ("unknown", None),
])
def test_unknown_fields_and_even_supported_live_context_fields_are_rejected(key, value):
    for level in ("document", "presets", "plugins", "record"):
        payload = document()
        target = {"document": payload, "presets": payload["presets"],
                  "plugins": payload["presets"]["Saved"],
                  "record": payload["presets"]["Saved"][DEFAULT_EQ_PLUGIN][0]}[level]
        target[key] = value
        assert_invalid(encode_document(payload))


@pytest.mark.parametrize("bands", [None, True, 1, "bands", {}, [], [{}] * 40,
                                    default_eq_bands()[:-1], default_eq_bands() + [{}]])
def test_band_array_must_have_exactly_forty_complete_records(bands):
    payload = document()
    payload["presets"]["Saved"][DEFAULT_EQ_PLUGIN] = bands
    assert_invalid(encode_document(payload))
    with pytest.raises(ValueError, match=f"^{ERROR}$"):
        encode_eq_transfer("Saved", DEFAULT_EQ_PLUGIN, bands)


@pytest.mark.parametrize("record", [
    None, True, 1, [], "record", {}, {"param_name": "a_freq"}, {"value": 30},
    {"param_name": "a_freq", "value": 30, "extra": None},
    {"param_name": None, "value": 30}, {"param_name": 1, "value": 30},
    {"param_name": "k_freq", "value": 30}, {"param_name": "A_freq", "value": 30},
    {"param_name": "a_freq ", "value": 30}, {"param_name": "a_frequency", "value": 30},
    {"param_name": "a_gain", "value": 0},
])
def test_malformed_extra_and_duplicate_parameter_records_are_rejected(record):
    bands = default_eq_bands()
    bands[0] = record
    assert_invalid(encode_document(document(bands=bands)))
    with pytest.raises(ValueError, match=f"^{ERROR}$"):
        encode_eq_transfer("Saved", DEFAULT_EQ_PLUGIN, bands)


@pytest.mark.parametrize("value", [True, False, None, "30", "30.0", "NaN", [], {},
                                   float("nan"), float("inf"), float("-inf"), 10 ** 400])
def test_values_reject_coercion_nonfinite_and_float_overflow(value):
    bands = default_eq_bands()
    bands[0]["value"] = value
    assert_invalid(encode_document(document(bands=bands)))
    with pytest.raises(ValueError, match=f"^{ERROR}$"):
        encode_eq_transfer("Saved", PRIVATE_PLUGIN, bands)


@pytest.mark.parametrize("suffix,value", [
    ("freq", 9.99), ("freq", 24000.1), ("gain", -24.1), ("gain", 24.1),
    ("q", 0.099), ("q", 30.001), ("mode", -2), ("mode", 4), ("mode", 6), ("mode", 1.5),
])
def test_out_of_range_values_are_rejected_without_clamping(suffix, value):
    bands = default_eq_bands()
    next(item for item in bands if item["param_name"] == "a_" + suffix)["value"] = value
    assert_invalid(encode_document(document(bands=bands)))
    with pytest.raises(ValueError, match=f"^{ERROR}$"):
        encode_eq_transfer("Saved", DEFAULT_EQ_PLUGIN, bands)


@pytest.mark.parametrize("suffix,value", [
    ("freq", 10), ("freq", 24000), ("gain", -24), ("gain", 24),
    ("q", 0.1), ("q", 30), ("mode", -1), ("mode", 0), ("mode", 1),
    ("mode", 2), ("mode", 3), ("mode", 5),
])
def test_boundary_values_and_all_supported_modes_are_preserved(suffix, value):
    bands = default_eq_bands()
    next(item for item in bands if item["param_name"] == "a_" + suffix)["value"] = value
    assert decode_eq_transfer(encode_eq_transfer("Saved", DEFAULT_EQ_PLUGIN, bands)).bands == bands


@pytest.mark.parametrize("numeric_literal", ["1e9999", "-1e9999", "NaN", "Infinity", "-Infinity", "9" * 5000])
def test_invalid_json_numeric_literals_never_escape_generic_failure(numeric_literal):
    encoded = encode_document(document()).replace(b'"value":30.0', b'"value":' + numeric_literal.encode(), 1)
    assert_invalid(encoded)


@pytest.mark.parametrize("param,numeric_literal", [
    ("a_gain", "24.00000000000000001"), ("a_gain", "-24.00000000000000001"),
    ("a_q", "0.0999999999999999999"), ("a_q", "30.00000000000000001"),
    ("a_freq", "9.9999999999999999999"), ("a_freq", "24000.00000000000001"),
    ("a_mode", "1.00000000000000001"), ("a_mode", "0.9999999999999999999"),
    ("a_gain", "1e-999"), ("a_gain", "-1e-999"),
    ("a_gain", "0.1234567890123456789"), ("a_gain", "4e-324"),
    ("a_gain", "1e-99999999999999999999999999999999999999999999"),
    ("a_gain", "0e99999999999999999999999999999999999999999999"),
])
def test_decimal_tokens_reject_boundary_bypasses_rounding_and_underflow(param, numeric_literal):
    bands = default_eq_bands()
    record = next(item for item in bands if item["param_name"] == param)
    original = encode_document(record)
    replacement = ('{"param_name":' + json.dumps(param) + ',"value":' + numeric_literal + '}').encode()
    encoded = encode_document(document(bands=bands)).replace(original, replacement, 1)
    assert original not in encoded
    assert_invalid(encoded)


@pytest.mark.parametrize("param,numeric_literal,value", [
    ("a_gain", "0.1", 0.1), ("a_gain", "1e-1", 0.1), ("a_gain", "0.1000000000000000", 0.1),
    ("a_gain", "2.4000000000000000e1", 24.0), ("a_gain", "-24.0000000000000000", -24.0),
    ("a_q", "1e-1", 0.1), ("a_q", "3.000e1", 30.0),
    ("a_freq", "1.000000000000000e1", 10.0), ("a_freq", "2.4e4", 24000.0),
    ("a_mode", "1.0000000000000000000000000", 1.0), ("a_mode", "5e0", 5.0),
    ("a_gain", "5e-324", 5e-324), ("a_gain", "2.2250738585072014e-308", 2.2250738585072014e-308),
    ("a_gain", "-0.0", -0.0), ("a_gain", "-0e-999", -0.0), ("a_gain", "0e-999", 0.0),
])
def test_equivalent_decimal_and_exponent_tokens_are_accepted_without_coercion(param, numeric_literal, value):
    bands = default_eq_bands()
    record = next(item for item in bands if item["param_name"] == param)
    original = encode_document(record)
    replacement = ('{"param_name":' + json.dumps(param) + ',"value":' + numeric_literal + '}').encode()
    encoded = encode_document(document(bands=bands)).replace(original, replacement, 1)
    actual = next(item["value"] for item in decode_eq_transfer(encoded).bands if item["param_name"] == param)
    assert type(actual) is float
    assert actual == value
    assert math.copysign(1, actual) == math.copysign(1, value)


@pytest.mark.parametrize("value", [
    -0.0, 0.0, 5e-324, -5e-324, 2.2250738585072014e-308,
    0.1, 0.12345678901234568, math.nextafter(24.0, 0.0), 24.0,
])
def test_canonical_finite_float_encodings_roundtrip_including_subnormal_values(value):
    bands = default_eq_bands()
    bands[1]["value"] = value
    actual = decode_eq_transfer(encode_eq_transfer("Saved", DEFAULT_EQ_PLUGIN, bands)).bands[1]["value"]
    assert type(actual) is float
    assert actual == value
    assert math.copysign(1, actual) == math.copysign(1, value)


@pytest.mark.parametrize("encoded", [
    None, "{}", bytearray(b"{}"), memoryview(b"{}"), b"", b"{", b"\xff", b"\xef\xbb\xbf{}",
    b'{} {}', b'{"version":1,"presets":{}} trailing', b"[" * 2000 + b"]" * 2000,
])
def test_malformed_non_utf8_non_bytes_and_excessive_nesting_fail_safely(encoded):
    assert_invalid(encoded)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
def test_non_utf8_encodings_are_not_implicitly_detected(encoding):
    assert_invalid(encode_document(document()).decode("utf-8").encode(encoding))


def test_file_limit_counts_all_bytes_including_padding_and_multibyte_text():
    compact = encode_document(document(name="Écoute 🎵"))
    boundary = compact + b" " * (MAX_EQ_TRANSFER_BYTES - len(compact))
    assert len(boundary) == MAX_EQ_TRANSFER_BYTES
    assert decode_eq_transfer(boundary).name == "Écoute 🎵"
    assert_invalid(boundary + b" ")
    assert_invalid(b" " * (MAX_EQ_TRANSFER_BYTES + 1))


def test_errors_never_echo_private_names_plugins_or_invalid_records():
    payload = document(name="token=synthetic-name-secret", plugin=PRIVATE_PLUGIN)
    assert_invalid(encode_document(payload))
    payload = document(plugin=PRIVATE_PLUGIN)
    payload["presets"]["Saved"][PRIVATE_PLUGIN][0]["value"] = "synthetic-value-secret"
    assert_invalid(encode_document(payload))
    with pytest.raises(ValueError) as raised:
        encode_eq_transfer("Saved", PRIVATE_PLUGIN, [{"param_name": "synthetic-value-secret"}])
    assert str(raised.value) == ERROR
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
