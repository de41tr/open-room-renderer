"""Own-client OAuth boundary tests: synthetic credentials, no network or browser."""
import stat
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from fake_linkplay import tidal_auth as auth
from fake_linkplay.tidal_account import TidalAccountUnavailable, TidalClientIdentity
from fake_linkplay.tidal_session import TidalSession, TidalSessionStore

CLIENT = TidalClientIdentity("synthetic-public-client", "http://127.0.0.1:8765/tidal/callback")
SCOPE = "synthetic.scope"
ACCESS = "synthetic-access-secret"
REFRESH = "synthetic-refresh-secret"
CODE = "synthetic-authorization-secret"
TOKEN = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
         "token_type": "Bearer", "user": {"userId": 42, "countryCode": "DE"}}


@pytest.fixture(autouse=True)
def prohibit_real_network_and_browser(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("tests must not open a browser or make network calls")
    monkeypatch.setattr(auth.aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(auth.asyncio, "start_server", forbidden)
    monkeypatch.setattr(auth.webbrowser, "open", forbidden)


@pytest.fixture
def login_harness(tmp_path, monkeypatch):
    store = TidalSessionStore(tmp_path / "private")
    store.save(TidalSession("synthetic-previous-access", "synthetic-previous-refresh", 2000, "7", "US"))
    calls = []
    emitted = []
    receivers = []
    server_states = []

    class Server:
        async def __aenter__(self):
            server_states.append("open")
            return self
        async def __aexit__(self, *args):
            server_states.append("closed")

    async def start(handler, host, port, **kwargs):
        assert host == "127.0.0.1" and port == 8765
        receivers.append(handler.__self__)
        return Server()

    async def browser(url):
        query = parse_qs(urlsplit(url).query)
        assert urlsplit(url)._replace(query="").geturl() == auth.AUTHORIZE_URL
        assert query["state"] == [receivers[0].state]
        assert query["code_challenge_method"] == ["S256"]
        assert all(secret not in url for secret in (ACCESS, REFRESH, CODE))
        assert "code_verifier" not in query and "client_secret" not in query
        receivers[0].result.set_result(CODE)
        return True

    async def request(url, **kwargs):
        calls.append((url, kwargs))
        return dict(TOKEN)

    monkeypatch.setattr(auth.asyncio, "start_server", start)
    monkeypatch.setattr(auth, "_open_browser", browser)
    monkeypatch.setattr(auth, "_request_json", request)
    return store, calls, emitted, receivers, server_states


def test_pkce_matches_rfc7636_s256_vector_and_never_renders_verifier(monkeypatch):
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    values = iter((verifier, "synthetic-state"))
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _: next(values))
    attempt = auth._Attempt.create()
    assert attempt.challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert verifier not in repr(attempt)
    query = parse_qs(urlsplit(auth._authorize_url(CLIENT, SCOPE, attempt)).query)
    assert query["scope"] == [SCOPE]
    assert query["client_id"] == [CLIENT.client_id]
    assert query["redirect_uri"] == [CLIENT.redirect_uri]
    assert query["response_type"] == ["code"]
    assert "code_verifier" not in query and "client_secret" not in query


@pytest.mark.parametrize("redirect", [
    "https://127.0.0.1:8765/callback", "http://localhost:8765/callback",
    "http://127.0.0.1:80/callback", "http://127.0.0.1/callback",
    "http://127.0.0.1:8765/callback?fixed=1", "http://127.0.0.1:8765/../callback",
    "https://remote.example.test/callback", "http://0.0.0.0:8765/callback",
])
def test_only_exact_numeric_loopback_registration_is_accepted(redirect):
    with pytest.raises((auth.TidalAuthError, TidalAccountUnavailable)):
        auth._configuration(TidalClientIdentity(CLIENT.client_id, redirect), SCOPE, 180)


@pytest.mark.parametrize("scope,timeout", [("", 180), ("a  b", 180), ("a\nb", 180),
                                         (SCOPE, 0), (SCOPE, 601), (SCOPE, float("nan"))])
def test_scopes_are_explicit_and_timeout_is_bounded(scope, timeout):
    with pytest.raises(auth.TidalAuthError):
        auth._configuration(CLIENT, scope, timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("target,host", [
    ("/tidal/callback?state=wrong&code=" + CODE, "127.0.0.1:8765"),
    ("/tidal/callback?state=expected&state=expected&code=" + CODE, "127.0.0.1:8765"),
    ("/tidal/callback?state=%C3%A9&code=" + CODE, "127.0.0.1:8765"),
    ("/other?state=expected&code=" + CODE, "127.0.0.1:8765"),
    ("//evil.example/tidal/callback?state=expected&code=" + CODE, "127.0.0.1:8765"),
    ("/tidal/callback?state=expected&code=" + CODE, "evil.example"),
    ("/tidal/callback?state=expected&code=" + CODE + "&error=denied", "127.0.0.1:8765"),
])
async def test_callback_rejects_forged_or_ambiguous_requests_without_consuming_state(target, host):
    receiver = auth._Callback(8765, "/tidal/callback", "expected")
    raw = f"GET {target} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode("ascii")
    with pytest.raises(ValueError):
        receiver._parse(raw)
    assert not receiver.result.done()
    await receiver.close()


@pytest.mark.asyncio
async def test_callback_response_and_logs_do_not_echo_oauth_secrets(capsys):
    receiver = auth._Callback(8765, "/tidal/callback", "synthetic-state-secret")
    reader = auth.asyncio.StreamReader()
    query = urlencode({"state": receiver.state, "code": CODE})
    reader.feed_data(f"GET /tidal/callback?{query} HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n".encode("ascii"))

    class Writer:
        data = b""
        closed = False
        def write(self, data):
            self.data += data
        async def drain(self):
            pass
        def close(self):
            self.closed = True

    writer = Writer()
    await receiver.receive(reader, writer)
    assert await receiver.result == CODE
    assert writer.closed
    assert b"200 OK" in writer.data and b"Cache-Control: no-store" in writer.data
    assert b"Referrer-Policy: no-referrer" in writer.data and b"history.replaceState" in writer.data
    visible = writer.data.decode() + str(capsys.readouterr())
    assert CODE not in visible and receiver.state not in visible


@pytest.mark.asyncio
async def test_complete_grant_is_saved_atomically_without_claiming_v1_or_playback(login_harness):
    store, calls, emitted, _, states = login_harness
    result = await auth.login(CLIENT, SCOPE, store, announce=emitted.append)
    assert result == auth.LoginResult(False)
    assert states == ["open", "closed"]
    assert len(calls) == 1 and calls[0][0] == auth.TOKEN_URL
    form = calls[0][1]["data"]
    assert form["grant_type"] == "authorization_code" and form["client_id"] == CLIENT.client_id
    assert form["redirect_uri"] == CLIENT.redirect_uri and form["code"] == CODE
    assert "client_secret" not in form and 43 <= len(form["code_verifier"]) <= 128
    saved = store.load()
    assert saved.access_token == ACCESS and saved.refresh_token == REFRESH
    assert saved.user_id == "42" and saved.country_code == "DE"
    assert saved.expires_at > auth.time.time()
    assert stat.S_IMODE((store.directory / "session.json").stat().st_mode) == 0o600
    assert all(secret not in " ".join(emitted) for secret in (ACCESS, REFRESH, CODE, form["code_verifier"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [False, True])
async def test_missing_identity_or_failed_v1_does_not_overwrite_prior_session(login_harness, monkeypatch, probe):
    store, calls, emitted, _, _ = login_harness
    before = (store.directory / "session.json").read_bytes()

    async def request(url, **kwargs):
        calls.append(url)
        if url == auth.TOKEN_URL:
            return {key: value for key, value in TOKEN.items() if key != "user"}
        raise auth.TidalAuthError("synthetic provider failure " + ACCESS)

    monkeypatch.setattr(auth, "_request_json", request)
    with pytest.raises(auth.OAuthIncomplete) as error:
        await auth.login(CLIENT, SCOPE, store, probe_v1_session=probe, announce=emitted.append)
    assert "OAuth succeeded" in str(error.value) and ACCESS not in str(error.value)
    assert calls == ([auth.TOKEN_URL, auth.SESSIONS_URL] if probe else [auth.TOKEN_URL])
    assert (store.directory / "session.json").read_bytes() == before


@pytest.mark.asyncio
async def test_explicitly_permitted_v1_probe_can_supply_missing_identity(login_harness, monkeypatch):
    store, calls, emitted, _, _ = login_harness

    async def request(url, **kwargs):
        calls.append((url, kwargs))
        if url == auth.TOKEN_URL:
            return {key: value for key, value in TOKEN.items() if key != "user"}
        assert kwargs == {"token": ACCESS}
        return {"userId": 42, "countryCode": "DE"}

    monkeypatch.setattr(auth, "_request_json", request)
    result = await auth.login(CLIENT, SCOPE, store, probe_v1_session=True, announce=emitted.append)
    assert result.v1_session_verified and store.load().user_id == "42"
    assert [call[0] for call in calls] == [auth.TOKEN_URL, auth.SESSIONS_URL]


@pytest.mark.asyncio
async def test_missing_refresh_token_is_saved_without_reusing_old_refresh(login_harness, monkeypatch):
    store, _, emitted, _, _ = login_harness

    async def request(url, **kwargs):
        return {key: value for key, value in TOKEN.items() if key != "refresh_token"}

    monkeypatch.setattr(auth, "_request_json", request)
    await auth.login(CLIENT, SCOPE, store, announce=emitted.append)
    assert store.load().access_token == ACCESS and store.load().refresh_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("probed", [{"userId": 42, "countryCode": "DE"},
                                    {"userId": 43, "countryCode": "DE"},
                                    {"userId": 42, "countryCode": "US"}, {}])
async def test_selected_v1_probe_runs_even_with_identity_and_rejects_disagreement(login_harness, monkeypatch, probed):
    store, calls, emitted, _, _ = login_harness
    before = store.load()

    async def request(url, **kwargs):
        calls.append(url)
        return dict(TOKEN) if url == auth.TOKEN_URL else probed

    monkeypatch.setattr(auth, "_request_json", request)
    if probed == TOKEN["user"]:
        result = await auth.login(CLIENT, SCOPE, store, probe_v1_session=True, announce=emitted.append)
        assert result.v1_session_verified
    else:
        with pytest.raises(auth.OAuthIncomplete):
            await auth.login(CLIENT, SCOPE, store, probe_v1_session=True, announce=emitted.append)
        assert store.load() == before
    assert calls == [auth.TOKEN_URL, auth.SESSIONS_URL]


@pytest.mark.asyncio
async def test_timeout_closes_callback_and_preserves_prior_session(login_harness, monkeypatch):
    store, calls, emitted, _, states = login_harness
    before = store.load()

    async def stalled_browser(url):
        await auth.asyncio.Future()

    monkeypatch.setattr(auth, "_open_browser", stalled_browser)
    with pytest.raises(auth.TidalAuthError, match="timed out"):
        await auth.login(CLIENT, SCOPE, store, timeout=1, announce=emitted.append)
    assert states == ["open", "closed"] and calls == [] and store.load() == before


@pytest.mark.parametrize("change", [
    {"access_token": ACCESS + "\r\nInjected: value"}, {"refresh_token": ""},
    {"token_type": "other"}, {"expires_in": float("inf")}, {"expires_in": True},
    {"expires_in": "3600"}, {"expires_in": -1},
])
def test_malformed_grants_have_fixed_redacted_errors(change):
    with pytest.raises(auth.TidalAuthError) as error:
        auth._token_session(dict(TOKEN, **change))
    rendered = "".join(traceback.format_exception(error.value))
    assert ACCESS not in rendered and REFRESH not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(302, b"secret redirect"), (403, ACCESS.encode()),
                                        (200, b"x" * (auth.MAX_RESPONSE_BYTES + 1)),
                                        (200, b'{"access_token":1,"access_token":2}'),
                                        (200, ACCESS.encode())])
async def test_http_is_pinned_bounded_no_redirects_no_proxy_and_error_bodies_are_redacted(monkeypatch, status, body):
    seen = []

    class Content:
        async def iter_chunked(self, size):
            for offset in range(0, len(body), size):
                yield body[offset:offset + size]

    class Response:
        content = Content()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    Response.status = status

    class Http:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["timeout"].total == 15
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            assert method == "POST" and url == auth.TOKEN_URL
            assert kwargs["allow_redirects"] is False
            assert kwargs["data"] == {"code": CODE}
            assert "Authorization" not in kwargs["headers"]
            seen.append(url)
            return Response()

    monkeypatch.setattr(auth.aiohttp, "ClientSession", Http)
    with pytest.raises(auth.TidalAuthError) as error:
        await auth._request_json(auth.TOKEN_URL, data={"code": CODE})
    assert len(seen) == 1
    assert all(secret not in str(error.value) for secret in (ACCESS, REFRESH, CODE))
    with pytest.raises(auth.TidalAuthError, match="Unsupported"):
        await auth._request_json("https://other.example/token", data={"code": CODE})
    assert len(seen) == 1


def test_cli_argument_errors_never_echo_accidentally_supplied_secret(capsys):
    with pytest.raises(SystemExit):
        auth.main(["--client-secret", ACCESS])
    assert ACCESS not in str(capsys.readouterr())


def test_cli_success_does_not_claim_xu4_or_playback_acceptance(tmp_path, monkeypatch, capsys):
    async def successful(*args, **kwargs):
        return auth.LoginResult(False)
    monkeypatch.setattr(auth, "login", successful)
    result = auth.main(["--client-id", CLIENT.client_id, "--redirect-uri", CLIENT.redirect_uri,
                        "--scope", SCOPE, "--session-dir", str(tmp_path / "private")])
    assert result == 0
    output = capsys.readouterr().out
    assert "v1 session access was not tested" in output
    assert "Full playback, entitlement, XU4 transfer and renderer adoption remain unverified" in output
