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
    def __init__(self, admission, manager, account=None, play_track=None):
        self.admission = admission
        self.manager = manager
        self.account = account
        self.play_track = play_track

    def routes(self):
        return [web.route("*", path, self.handle) for path in
                ("/ui/tidal", "/ui/tidal.js", PREFIX + "/status", PREFIX + "/start", PREFIX + "/cancel", PREFIX + "/share", PREFIX + "/unshare", PREFIX + "/play")]

    async def handle(self, request):
        try:
            origin = self.admission._origin(request)
            if request.raw_path.split("?", 1)[0] != request.path:
                raise WebAdmissionError("not_found", 404)
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
                # Exact one-field request, no URLs, credentials or arbitrary options.
                try:
                    pairs = json.loads(raw, object_pairs_hook=lambda value: value)
                except (ValueError, UnicodeError):
                    raise WebAdmissionError() from None
                action = request.path.rsplit("/", 1)[-1]
                if action == "play":
                    if (type(pairs) is not list or len(pairs) != 2
                            or {key for key, _ in pairs} != {"track_id", "confirm_experimental_playback"}):
                        raise WebAdmissionError()
                    payload = dict(pairs)
                    if (payload["confirm_experimental_playback"] is not True
                            or type(payload["track_id"]) is not str
                            or not re.fullmatch(r"[1-9][0-9]{0,18}", payload["track_id"])):
                        raise WebAdmissionError()
                else:
                    expected = [({"start": "confirm_experimental_login", "cancel": "cancel",
                                  "share": "confirm_lan_account_sharing", "unshare": "stop_lan_account_sharing"}[action], True)]
                    if pairs != expected or any(type(value) is not bool for _, value in pairs):
                        raise WebAdmissionError()
                if action == "play":
                    result = await self.manager.status()
                    if self.play_track is None or result.get("active"):
                        raise WebAdmissionError("experimental_playback_unavailable", 409)
                    await self.play_track(payload["track_id"])
                    result["playback_requested"] = True
                elif action in {"share", "unshare"}:
                    result = await self.manager.status()
                    if self.account is None or (action == "share" and result.get("active")):
                        raise WebAdmissionError("account_sharing_unavailable", 409)
                    try:
                        await self.account.set_shared(action == "share")
                    except TidalAccountUnavailable:
                        raise WebAdmissionError("account_sharing_unavailable", 409) from None
                else:
                    result = await (self.manager.start() if action == "start" else self.manager.cancel())
            result = dict(result)
            result["account_shared"] = bool(self.account and self.account.shared)
            allowed = {"state", "active", "verification_uri", "user_code", "expires_in", "expires_at", "retry_after", "account_shared", "playback_requested"}
            safe_errors = {"dependency_unavailable", "session_exists", "storage_unavailable", "invalid_challenge", "login_failed", "expired", "unsafe_debug"}
            if result.get("error") in safe_errors:
                allowed.add("error")
            return web.json_response({key: value for key, value in result.items() if key in allowed}, headers=_HEADERS)
        except WebAdmissionError as error:
            return self.admission._error(error.code, error.status)
        except Exception:
            return web.json_response({"state": "error", "active": False,
                                      "message": "Device login unavailable. No completed adoption confirmed."},
                                     status=503, headers=_HEADERS)
