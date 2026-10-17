"""Explicit experimental device login on the existing trusted-LAN web boundary.

Origin/CSRF checks are browser admission, not LAN user authentication. Never
return provider tokens, internal device codes, account identity or exceptions.
"""
from importlib.resources import files
import json
import re

from aiohttp import web

from .web_ui import WebAdmissionError, _HEADERS
from .tidal_account import TidalAccountUnavailable

PREFIX = "/orr/v1/tidal"


class WebTidalLogin:
    def __init__(self, admission, manager, account=None, play_track=None, session_refresh=None, test_lease=None, *, disconnect=None):
        self.admission = admission
        self.manager = manager
        self.account = account
        self.play_track = play_track
        self.session_refresh = session_refresh
        self.test_lease = test_lease
        self.disconnect = disconnect

    def routes(self):
        return [web.route("*", path, self.handle) for path in
                ("/ui/tidal", "/ui/tidal.js", PREFIX + "/status", PREFIX + "/start", PREFIX + "/cancel", PREFIX + "/share", PREFIX + "/unshare", PREFIX + "/play", PREFIX + "/refresh", PREFIX + "/disconnect", PREFIX + "/test/status", PREFIX + "/test/start", PREFIX + "/test/stop")]

    async def handle(self, request):
        try:
            origin = self.admission._origin(request)
            if request.raw_path.split("?", 1)[0] != request.path:
                raise WebAdmissionError("not_found", 404)
            if request.path.startswith(PREFIX + "/test/"):
                return await self._handle_test(request, origin)
            if request.path in {"/ui/tidal", "/ui/tidal.js"}:
                if request.method != "GET":
                    raise WebAdmissionError("method_not_allowed", 405)
                self.admission._bodyless(request)
                name = "tidal.html" if request.path == "/ui/tidal" else "tidal.js"
                return web.Response(body=files("fake_linkplay").joinpath("web_assets", name).read_bytes(),
                                    content_type="text/html" if name.endswith("html") else "text/javascript",
                                    charset="utf-8", headers=_HEADERS)
            if request.path == PREFIX + "/status":
                if request.method != "GET":
                    raise WebAdmissionError("method_not_allowed", 405)
                self.admission._bodyless(request)
                result = await self.manager.status()
            else:
                if request.method != "POST":
                    raise WebAdmissionError("method_not_allowed", 405)
                raw = await self.admission._body(request, origin)
                # Exact action fields, no URLs, credentials or arbitrary options.
                try:
                    pairs = json.loads(raw, object_pairs_hook=lambda value: value)
                except (ValueError, UnicodeError):
                    raise WebAdmissionError() from None
                action = request.path.rsplit("/", 1)[-1]
                remember = False
                if action == "play":
                    if (type(pairs) is not list or len(pairs) != 2
                            or {key for key, _ in pairs} != {"track_id", "confirm_experimental_playback"}):
                        raise WebAdmissionError()
                    payload = dict(pairs)
                    if (payload["confirm_experimental_playback"] is not True
                            or type(payload["track_id"]) is not str
                            or not re.fullmatch(r"[1-9][0-9]{0,18}", payload["track_id"])):
                        raise WebAdmissionError()
                elif action == "share":
                    if (type(pairs) is not list or len(pairs) not in (1, 2)
                            or any(type(pair) is not tuple or len(pair) != 2 for pair in pairs)):
                        raise WebAdmissionError()
                    payload = dict(pairs)
                    fields = {"confirm_lan_account_sharing"}
                    if len(pairs) == 2:
                        fields.add("remember_account_sharing")
                    if set(payload) != fields or any(value is not True for value in payload.values()):
                        raise WebAdmissionError()
                    remember = payload.get("remember_account_sharing", False)
                else:
                    expected = [({"start": "confirm_experimental_login", "cancel": "cancel",
                                  "share": "confirm_lan_account_sharing", "unshare": "stop_lan_account_sharing",
                                  "refresh": "confirm_session_refresh", "disconnect": "confirm_forget_local_session"}[action], True)]
                    if pairs != expected or any(type(value) is not bool for _, value in pairs):
                        raise WebAdmissionError()
                if action == "disconnect":
                    if self.disconnect is None:
                        raise WebAdmissionError("disconnect_unavailable", 409)
                    result = await self.disconnect()
                    result = {**await self.manager.status(), **result}
                elif action == "play":
                    result = await self.manager.status()
                    if self.play_track is None or result.get("active"):
                        raise WebAdmissionError("experimental_playback_unavailable", 409)
                    await self.play_track(payload["track_id"])
                    result["playback_requested"] = True
                elif action == "refresh":
                    current = await self.manager.status()
                    if self.session_refresh is None or current.get("active"):
                        raise WebAdmissionError("session_refresh_unavailable", 409)
                    result = await self.session_refresh.refresh()
                elif action in {"share", "unshare"}:
                    result = await self.manager.status()
                    if self.account is None or (action == "share" and result.get("active")):
                        raise WebAdmissionError("account_sharing_unavailable", 409)
                    try:
                        await self.account.set_shared(action == "share", remember=remember)
                    except TidalAccountUnavailable:
                        raise WebAdmissionError("account_sharing_unavailable", 409) from None
                else:
                    result = await (self.manager.start() if action == "start" else self.manager.cancel())
            result = dict(result)
            result["account_shared"] = bool(self.account and self.account.shared)
            result["account_sharing_remembered"] = bool(self.account and self.account.sharing_remembered)
            allowed = {"state", "active", "verification_uri", "user_code", "expires_in", "expires_at", "retry_after", "account_shared", "account_sharing_remembered", "playback_requested", "refresh_outcome", "sharing_warning", "session_forgotten"}
            if result.get("session_status") in ("missing", "stored", "expired", "unsupported", "unavailable"):
                allowed.add("session_status")
            safe_errors = {"dependency_unavailable", "session_exists", "storage_unavailable", "invalid_challenge", "login_failed", "expired", "unsafe_debug", "invalid_timeout", "session_unavailable", "client_mismatch", "invalid_response", "identity_mismatch", "refresh_failed", "commit_failed", "refresh_result_uncertain", "refresh_rejected"}
            if result.get("error") in safe_errors:
                allowed.add("error")
            return web.json_response({key: value for key, value in result.items() if key in allowed}, headers=_HEADERS)
        except WebAdmissionError as error:
            return self.admission._error(error.code, error.status)
        except Exception:
            return web.json_response({"state": "error", "active": False,
                                      "message": "TIDAL action unavailable. Its outcome was not confirmed."},
                                     status=503, headers=_HEADERS)

    async def _handle_test(self, request, origin):
        if self.test_lease is None:
            raise WebAdmissionError("test_unavailable", 409)
        action = request.path.rsplit("/", 1)[-1]
        if action == "status":
            if request.method != "GET":
                raise WebAdmissionError("method_not_allowed", 405)
            self.admission._bodyless(request)
            result, status = await self.test_lease.status(), 200
        else:
            if request.method != "POST":
                raise WebAdmissionError("method_not_allowed", 405)
            raw = await self.admission._body(request, origin)
            try:
                pairs = json.loads(raw, object_pairs_hook=lambda value: value)
                fields = ({"track_id", "duration_seconds", "volume_percent", "confirm_experimental_playback"}
                          if action == "start" else {"lease_id", "stop"})
                if (type(pairs) is not list or len(pairs) != len(fields)
                        or any(type(pair) is not tuple or len(pair) != 2 for pair in pairs)
                        or {key for key, _ in pairs} != fields):
                    raise ValueError()
                payload = dict(pairs)
                if action == "start":
                    if (payload["confirm_experimental_playback"] is not True
                            or type(payload["track_id"]) is not str
                            or not re.fullmatch(r"[1-9][0-9]{0,18}", payload["track_id"])
                            or type(payload["duration_seconds"]) is not int
                            or not 1 <= payload["duration_seconds"] <= 120
                            or type(payload["volume_percent"]) is not int or payload["volume_percent"] != 1):
                        raise ValueError()
                elif (payload["stop"] is not True or type(payload["lease_id"]) is not str
                      or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["lease_id"])):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise WebAdmissionError() from None
            if action == "start":
                result, status = await self.test_lease.start(payload["track_id"], payload["duration_seconds"])
            else:
                result, status = await self.test_lease.stop(payload["lease_id"])
        allowed = {"capability_version", "available", "state", "lease_id", "remaining_seconds",
                   "volume_percent", "volume_confirmed", "error", "playback_position_seconds",
                   "observed_progress_seconds", "decoded_audio_observed"}
        return web.json_response({key: value for key, value in result.items() if key in allowed},
                                 status=status, headers=_HEADERS)
