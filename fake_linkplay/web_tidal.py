"""Explicit experimental device login on the existing trusted-LAN web boundary.

Origin/CSRF checks are browser admission, not LAN user authentication. Never
return provider tokens, internal device codes, account identity or exceptions.
"""
from importlib.resources import files
import json

from aiohttp import web

from .web_ui import WebAdmissionError, _HEADERS

PREFIX = "/orr/v1/tidal"


class WebTidalLogin:
    def __init__(self, admission, manager):
        self.admission = admission
        self.manager = manager

    def routes(self):
        return [web.route("*", path, self.handle) for path in
                ("/ui/tidal", "/ui/tidal.js", PREFIX + "/status", PREFIX + "/start", PREFIX + "/cancel")]

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
                expected = [("confirm_experimental_login", True)] if request.path.endswith("/start") else [("cancel", True)]
                if pairs != expected or any(type(value) is not bool for _, value in pairs):
                    raise WebAdmissionError()
                result = await (self.manager.start() if request.path.endswith("/start") else self.manager.cancel())
            allowed = {"state", "active", "verification_uri", "user_code", "expires_in", "expires_at", "retry_after"}
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
