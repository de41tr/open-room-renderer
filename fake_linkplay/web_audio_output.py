"""Private-safe, on-demand output inspection using a separate no-spawn client.

Only startup scalar values are captured. Nothing here holds the renderer, its
active backend, a Config object, a playback lock, or a mutable config reader.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from .mpv_backend import MpvBackend

# Never expose an arbitrary printable mpv value: identifiers may be paths,
# URLs, credentials or hostnames. These are fixed presentation classifications.
DRIVER_FAMILIES = frozenset({"alsa", "pulse", "pipewire", "jack", "coreaudio", "wasapi",
                           "sdl", "sndio", "oss", "openal", "null", "pcm"})
PROPERTY_STATUSES = frozenset({"value", "unavailable", "unsupported", "not_read", "timed_out",
                              "transport_error", "malformed_response", "request_failed",
                              "response_limit"})
CONNECTIONS = frozenset({"not_attempted", "connected", "socket_missing", "socket_invalid",
                         "timed_out", "transport_error"})
SOCKET_CHECKS = frozenset({"not_checked", "unchanged_at_checks", "changed"})


def _family(value: object, *, device: bool = False) -> str:
    if not isinstance(value, str) or device and value == "auto":
        return "not_observed"
    family = value.split("/", 1)[0] if device else value
    return family if family in DRIVER_FAMILIES else "unrecognized"


def _status(value: object) -> str:
    return value if isinstance(value, str) and value in PROPERTY_STATUSES else "malformed_response"


def output_summary(live: dict[str, object], configured_output: str | None,
                   auto_spawn: bool) -> dict[str, object]:
    """Discard every raw value, including unexpected fields and diagnostics."""
    connection = live.get("connection")
    connection = connection if isinstance(connection, str) and connection in CONNECTIONS else "transport_error"
    socket_check = live.get("socket_path_check")
    socket_check = socket_check if isinstance(socket_check, str) and socket_check in SOCKET_CHECKS else "not_checked"
    intent = live.get("audio_device")
    intent = intent if isinstance(intent, dict) else {}
    driver = live.get("current_ao")
    driver = driver if isinstance(driver, dict) else {}
    intent_status, driver_status = _status(intent.get("status")), _status(driver.get("status"))
    intent_value = intent.get("value") if intent_status == "value" else None
    driver_value = driver.get("value") if driver_status == "value" else None
    if intent_status == "value" and not isinstance(intent_value, str):
        intent_status = "malformed_response"
    if driver_status == "value" and not isinstance(driver_value, str):
        driver_status = "malformed_response"
    stable = connection == "connected" and socket_check == "unchanged_at_checks"
    comparison = "not_compared"
    if stable and configured_output is not None and intent_status == "value":
        comparison = "matches" if configured_output == intent_value else "differs"
    complete = stable and all(status in {"value", "unavailable", "unsupported"}
                              for status in (intent_status, driver_status))
    return {
        "version": 1,
        "status": "complete" if complete else "incomplete",
        "configuration": {
            "source": "running_renderer",
            "selection": "mpv_default" if configured_output is None else "explicit_output",
            "driver_family": _family(configured_output, device=True),
            "spawn_policy": "auto_spawn_enabled" if auto_spawn else "external_only",
            "identifier": "hidden",
        },
        "connection": connection, "socket_path_check": socket_check,
        "live_intent": {
            "status": intent_status,
            "selection": ("not_observed" if intent_status != "value" else
                          "mpv_default" if intent_value == "auto" else "explicit_output"),
            "driver_family": _family(intent_value, device=True), "identifier": "hidden",
        },
        "driver": {"status": driver_status, "family": _family(driver_value)},
        "comparison": comparison,
        "null_output": "deliberately_silent" if stable and driver_value == "null" else "not_observed",
        "observations": "sequential_not_atomic", "renderer_ownership": "unverified",
        "physical_device": "not_verified", "audible_output": "not_verified",
    }


def make_output_inspector(*, socket_path: str, configured_output: str | None,
                          auto_spawn: bool) -> Callable[[], Awaitable[dict[str, object]]]:
    """Capture the running service's original configuration, never reread disk."""
    async def inspect() -> dict[str, object]:
        # Each invocation is fresh. Deliberately do not pass audio_device: it
        # applies only to an owned child at startup, and this client owns none.
        backend = MpvBackend(socket_path, auto_spawn=False)
        live = await backend.diagnose_audio_output()
        # diagnose_audio_output completes its bounded transport cleanup before
        # returning; the adapter holds its busy claim across this entire await.
        return output_summary(live, configured_output, auto_spawn)
    return inspect
