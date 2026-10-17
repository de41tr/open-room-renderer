"""Pinned-library login error privacy with synthetic HTTP responses only."""
import asyncio
import json
import logging
import socket
from urllib.parse import urlsplit

import pytest
import requests
import tidalapi

from fake_linkplay.tidal_device_auth import TidalDeviceLoginManager
from fake_linkplay.tidal_session import TidalSessionStore


@pytest.mark.asyncio
async def test_library_session_error_body_stays_private(tmp_path, monkeypatch, caplog):
    secrets = {key: f"synthetic-{key}-secret" for key in (
        "access_token", "refresh_token", "device_code", "client_secret",
    )}
    config_init = tidalapi.Config.__init__

    def synthetic_config(self, *args, **kwargs):
        config_init(self, *args, **kwargs)
        self.client_id = "synthetic-client"
        self.client_secret = secrets["client_secret"]

    def forbid_network(*args, **kwargs):
        pytest.fail("The library privacy regression must not use the network")

    calls = []

    def send(adapter, request, **kwargs):
        path = urlsplit(request.url).path
        calls.append((request.method, path))
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        response.encoding = "utf-8"
        if path == "/v1/oauth2/device_authorization":
            value = {"userCode": "ABCDE", "verificationUri": "link.tidal.com",
                     "verificationUriComplete": "link.tidal.com/ABCDE",
                     "expiresIn": 300, "interval": 1, "deviceCode": secrets["device_code"]}
        elif path == "/v1/oauth2/token":
            value = {"access_token": secrets["access_token"],
                     "refresh_token": secrets["refresh_token"],
                     "token_type": "Bearer", "expires_in": 3600}
        elif path == "/v1/sessions":
            response.status_code = 400
            value = {"error": "synthetic-provider-error", **secrets}
        else:
            pytest.fail("Unexpected library request")
        response._content = json.dumps(value).encode()
        return response

    monkeypatch.setattr(tidalapi.Config, "__init__", synthetic_config)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    store = TidalSessionStore(tmp_path / "private")
    manager = TidalDeviceLoginManager(store)
    try:
        with caplog.at_level(logging.DEBUG):
            started = await manager.start()
            async with asyncio.timeout(3):
                while (status := await manager.status())["active"]:
                    await asyncio.sleep(0.005)
        assert calls == [("POST", "/v1/oauth2/device_authorization"),
                         ("POST", "/v1/oauth2/token"), ("GET", "/v1/sessions")]
        assert status["state"] == "error" and status["error"] == "login_failed"
        assert status["message"] == "TIDAL device login failed; no new session was saved."
        assert status["session_saved"] is False and store.load() is None
        public = json.dumps([started, status])
        for secret in secrets.values():
            assert secret not in public
            assert secret not in caplog.text
    finally:
        await manager.close()
