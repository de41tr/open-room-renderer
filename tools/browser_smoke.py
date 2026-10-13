#!/usr/bin/env python3
"""Small opt-in real Chrome smoke, never part of the default pytest suite.

Uses installed Chrome/ChromeDriver through the W3C HTTP protocol and aiohttp
already required by the project. The native fixture is the existing production
Unix IPC/null player; only its local generated WAV media is synthetic.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
from html import escape
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import tempfile
import traceback
from typing import Any

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, web

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.queue_playlist import encode_current_playlist
from fake_linkplay.state import RendererState
from test_mpv_unix_integration import _eventually, _external_player, _property, _reap, _write_tone
from tools.native_probe_supervisor import run_probe

ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
FILENAME = "open-room-renderer-playlist.json"


class Browser:
    """Only the handful of WebDriver commands this smoke needs; no DOM mocks."""

    def __init__(self, session: ClientSession, base: str):
        self.http, self.base, self.id = session, base, ""

    async def request(self, method: str, path: str, body=None):
        async with self.http.request(method, self.base + path, json=body) as response:
            result = await response.json()
            value = result.get("value")
            assert response.status < 400, (method, path, response.status, value)
            return value

    async def call(self, method: str, path: str, body=None):
        return await self.request(method, f"/session/{self.id}" + path, body)

    async def inspect(self, expression: str):
        # Read-only real DOM/layout inspection. Interactions use WebDriver below.
        return await self.call("POST", "/execute/sync", {"script": "return (" + expression + ");", "args": []})

    async def wait(self, expression: str):
        return await _eventually(expression, lambda: self.inspect(expression), timeout=10)

    async def click(self, selector: str):
        element = await self.call("POST", "/element", {"using": "css selector", "value": selector})
        await self.call("POST", f"/element/{element[ELEMENT]}/click", {})

    async def blocked_click(self, selector: str):
        element = await self.call("POST", "/element", {"using": "css selector", "value": selector})
        path = f"/session/{self.id}/element/{element[ELEMENT]}/click"
        async with self.http.post(self.base + path, json={}) as response:
            result = await response.json()
            assert response.status == 400 and result["value"]["error"] == "element click intercepted", result
        return "element click intercepted"

    async def key(self, value: str, *, shift=False):
        keys = ([{"type": "keyDown", "value": "\ue008"}] if shift else [])
        keys += [{"type": "keyDown", "value": value}, {"type": "keyUp", "value": value}]
        if shift:
            keys.append({"type": "keyUp", "value": "\ue008"})
        await self.call("POST", "/actions", {"actions": [{"type": "key", "id": "keyboard", "actions": keys}]})

    async def screenshot(self, path: Path):
        raw = base64.b64decode(await self.call("GET", "/screenshot"), validate=True)
        assert raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) > 1000
        path.write_bytes(raw)

    async def diagnostics(self, directory: Path, label: str):
        for suffix, get in (
            (".png", lambda: self.screenshot(directory / (label + ".png"))),
            (".html", lambda: self.call("GET", "/source")),
            ("-console.json", lambda: self.call("POST", "/log", {"type": "browser"})),
        ):
            try:
                result = await get()
                if result is not None:
                    (directory / (label + suffix)).write_text(
                        result if isinstance(result, str) else json.dumps(result, indent=2), encoding="utf-8")
            except Exception as exc:
                print(f"Diagnostic {label}{suffix} unavailable: {exc}", flush=True)

    @asynccontextmanager
    async def opened(self, chrome: str, downloads: Path, *, mobile: bool):
        options: dict[str, Any] = {"binary": chrome,
            "args": ["--headless=new", "--window-size=1280,900", "--no-first-run", "--no-default-browser-check"],
            "prefs": {"download.default_directory": str(downloads), "download.prompt_for_download": False}}
        if mobile:
            options["mobileEmulation"] = {"deviceMetrics": {
                "width": 390, "height": 844, "pixelRatio": 1, "touch": True, "mobile": True},
                "clientHints": {"platform": "Android", "mobile": True}}
        result = await self.request("POST", "/session", {"capabilities": {"alwaysMatch": {
            "browserName": "chrome", "acceptInsecureCerts": False,
            "goog:chromeOptions": options, "goog:loggingPrefs": {"browser": "ALL"},
            "timeouts": {"implicit": 0, "pageLoad": 10000, "script": 5000}}}})
        self.id = result["sessionId"]
        failed = False
        try:
            yield result["capabilities"]
        except BaseException:
            failed = True
            raise
        finally:
            try:
                await self.call("DELETE", "")
            except Exception as exc:
                if not failed:
                    raise
                print(f"Browser cleanup also failed: {exc}", flush=True)
            self.id = ""


async def exercise(browser: Browser, api: HttpApi, backend, base: str,
                   artifacts: Path, downloads: Path, *, mobile: bool, evidence: dict):
    label = "mobile" if mobile else "desktop"
    await browser.call("POST", "/url", {"url": base + "/"})
    await browser.wait("document.querySelector('#connection-label')?.textContent === 'Connected'")
    assert await browser.call("GET", "/title") == "Open Room Renderer"
    # Real link navigation, then actual history traversal; no synthetic events.
    await browser.click("a.brand")
    assert await browser.call("GET", "/url") == base + "/ui/"
    await browser.wait("document.querySelector('#connection-label')?.textContent === 'Connected'")
    await browser.call("POST", "/back", {})
    assert await browser.call("GET", "/url") == base + "/"
    await browser.call("POST", "/forward", {})
    assert await browser.call("GET", "/url") == base + "/ui/"
    await browser.wait("document.querySelector('#connection-label')?.textContent === 'Connected'")
    geometry = await browser.inspect("""(() => {
      const p = document.querySelector('.player-column').getBoundingClientRect();
      const l = document.querySelector('.library-column').getBoundingClientRect();
      return {width: innerWidth, client: document.documentElement.clientWidth,
        scroll: document.documentElement.scrollWidth, player: p.toJSON(), library: l.toJSON(),
        columns: getComputedStyle(document.querySelector('.dashboard')).gridTemplateColumns};
    })()""")
    assert geometry["scroll"] <= geometry["client"], geometry
    p, l = geometry["player"], geometry["library"]
    assert p["width"] > 100 and l["width"] > 100, geometry
    if mobile:
        assert geometry["width"] == 390 and abs(p["x"] - l["x"]) < 2 and l["y"] >= p["bottom"], geometry
    else:
        assert geometry["width"] >= 1200 and l["x"] >= p["right"] and abs(l["y"] - p["y"]) < 2, geometry
    evidence[label] = {"layout": geometry, "navigation": "home link, back, forward"}
    await browser.screenshot(artifacts / (label + "-render.png"))

    # Both viewports send a real UI Play/Pause command. mpv confirms actual
    # decoder selection, null output, advancing media time, and paused readback.
    await browser.wait("!document.querySelector('#play').disabled")
    first_command = len(evidence["http_commands"])
    await browser.click("#play")
    await browser.wait("document.querySelector('#playback-state').textContent === 'PLAYING' && document.querySelector('#playback-state').dataset.kind === 'confirmed'")
    position = await _property(backend, "playback-time")
    async def advanced():
        value = await _property(backend, "playback-time")
        return value if value > position + .08 else None
    later = await _eventually("native decoded playback advances", advanced)
    assert await _property(backend, "path") == api.state.queue.items[0].url
    assert await _property(backend, "current-ao") == "null"
    tracks = await _property(backend, "track-list")
    assert any(t.get("type") == "audio" and t.get("selected") and t.get("codec") == "pcm_s16le" for t in tracks), tracks
    await browser.wait("!document.querySelector('#pause').disabled")
    await browser.click("#pause")
    await browser.wait("document.querySelector('#playback-state').textContent === 'PAUSED' && document.querySelector('#playback-state').dataset.kind === 'confirmed'")
    assert await _property(backend, "pause") is True
    commands = evidence["http_commands"][first_command:]
    assert len(commands) == 2, commands
    assert all(item["status"] == 200 and item["request_acknowledged"] is True for item in commands), commands
    evidence[label]["native"] = {"codec": "pcm_s16le", "ao": "null", "pause": True,
        "initial_time": position, "advanced_time": later, "ui_phase": "PAUSED"}

    # Native dialog focus, tab sequence and dismissal, then a fresh opening.
    before_export = (len(evidence["http_commands"]), evidence["media_requests"])
    await browser.click("#playlist-export")
    await browser.wait("document.querySelector('#export-dialog').open && !document.querySelector('#export-confirm').disabled")
    evidence[label]["focus_trace"] = []
    async def focused(expected: str, *, boundary=False, shift=False):
        async def record():
            state = await browser.inspect("""(() => {
              const active = document.activeElement, dialog = document.querySelector('#export-dialog');
              return {id: active.id, tag: active.tagName, document_focused: document.hasFocus(),
                in_dialog: dialog.contains(active), modal: dialog.matches(':modal'),
                body_fallback: active === document.body && !active.hasAttribute('tabindex') && !active.isContentEditable};
            })()""")
            evidence[label]["focus_trace"].append(state)
            return state
        state = await record()
        assert state["modal"], state
        if boundary and state["id"] != expected:
            # Headless Chrome/WebDriver exposes a noninteractive BODY fallback
            # at the native tab boundary. hasFocus() does not establish whether
            # browser chrome took focus, so record it without inferring that.
            # Reject every background element; the next real Tab must re-enter.
            assert state["id"] == "" and state["tag"] == "BODY" and state["body_fallback"] and not state["in_dialog"], state
            await browser.key("\ue004", shift=shift)
            state = await record()
        assert state["modal"] and state["id"] == expected and state["in_dialog"] and state["document_focused"], state
    await focused("export-close")
    await browser.key("\ue004")  # Tab
    await focused("export-confirm")
    await browser.key("\ue004")
    await focused("export-cancel")
    await browser.key("\ue004")
    await focused("export-close", boundary=True)
    await browser.key("\ue004", shift=True)
    await focused("export-cancel", boundary=True, shift=True)
    await browser.key("\ue004", shift=True)
    await focused("export-confirm")
    evidence[label]["background_click"] = await browser.blocked_click("#play")
    await focused("export-confirm")
    await browser.key("\ue00c")  # Escape, handled by the real dialog cancel event.
    await browser.wait("!document.querySelector('#export-dialog').open && document.activeElement.id === 'playlist-export'")
    await browser.click("#playlist-export")
    await browser.wait("!document.querySelector('#export-confirm').disabled")
    assert await browser.inspect("!document.querySelector('#export-confirm').checked && !document.querySelector('#export-download').hasAttribute('href')")
    assert not list(downloads.iterdir()), "Opening the dialog must not download a file"
    await browser.click("#export-confirm")
    await browser.wait("!document.querySelector('#export-prepare').disabled")
    await browser.click("#export-prepare")
    await browser.wait("!document.querySelector('#export-download').hidden && document.querySelector('#export-download').href.startsWith('blob:')")
    assert not list(downloads.iterdir()), "Preparing a Blob is not a completed download"
    # A prepared private Blob must not survive real navigation/history restore.
    await browser.call("POST", "/url", {"url": base + "/"})
    await browser.call("POST", "/back", {})
    await browser.wait("document.querySelector('#connection-label')?.textContent === 'Connected'")
    assert await browser.inspect("!document.querySelector('#export-dialog').open && !document.querySelector('#export-confirm').checked && !document.querySelector('#export-download').hasAttribute('href')")
    assert not list(downloads.iterdir())
    await browser.click("#playlist-export")
    await browser.wait("!document.querySelector('#export-confirm').disabled")
    await browser.click("#export-confirm")
    await browser.wait("!document.querySelector('#export-prepare').disabled")
    await browser.click("#export-prepare")
    await browser.wait("!document.querySelector('#export-download').hidden && document.querySelector('#export-download').href.startsWith('blob:')")
    assert not list(downloads.iterdir())
    await browser.screenshot(artifacts / (label + "-export-dialog.png"))
    expected_bytes = encode_current_playlist(api.state.queue)
    expected_document = {"version": 1, "name": "Browser smoke", "index": 1, "mode": "normal",
                         "urls": [item.url for item in api.state.queue.items]}
    await browser.click("#export-download")
    downloaded = downloads / FILENAME
    async def complete():
        return downloaded.is_file() and downloaded.stat().st_size == len(expected_bytes) and not list(downloads.glob("*.crdownload"))
    await _eventually("actual Chrome download bytes on disk", complete, timeout=10)
    raw = downloaded.read_bytes()
    assert raw == expected_bytes and json.loads(raw) == expected_document
    digest = hashlib.sha256(raw).hexdigest()
    assert evidence["http_exports"][-1] == {"status": 200, "sha256": digest, "size_bytes": len(raw)}
    # Only generated local fixture URLs are in this artifact, never user media.
    (artifacts / (label + "-" + FILENAME)).write_bytes(raw)
    await browser.click("#export-close")
    await browser.wait("!document.querySelector('#export-dialog').open && document.activeElement.id === 'playlist-export'")
    assert await browser.inspect("!document.querySelector('#export-confirm').checked && !document.querySelector('#export-download').hasAttribute('href')")
    assert list(downloads.iterdir()) == [downloaded]
    assert before_export == (len(evidence["http_commands"]), evidence["media_requests"]), "Export must not send playback commands or fetch media"
    assert await _property(backend, "pause") is True
    evidence[label]["dialog"] = "Native Tab order/noninteractive BODY boundary, blocked background click, Shift+Tab, Escape, opener focus, fresh consent, navigation and close cleanup"
    evidence[label]["download"] = {"filename": FILENAME, "size_bytes": len(raw), "sha256": digest,
        "verified": "browser disk bytes == renderer response bytes == canonical JSON"}
    console = await browser.call("POST", "/log", {"type": "browser"})
    (artifacts / (label + "-console.json")).write_text(json.dumps(console, indent=2), encoding="utf-8")
    assert not [entry for entry in console if entry.get("level") == "SEVERE"], console


async def smoke(artifacts: Path, evidence: dict):
    chrome, driver, mpv = (shutil.which(name) for name in ("google-chrome", "chromedriver", "mpv"))
    assert chrome and driver and mpv, "Installed google-chrome, chromedriver and mpv are required; no fallback or download"
    assert os.geteuid() != 0, "Run as an unprivileged user with the normal Chrome sandbox"
    with tempfile.TemporaryDirectory(prefix="orr-browser-") as temporary:
        workspace = Path(temporary)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            driver_port = reservation.getsockname()[1]
        # Omit allowed-ips (it enables remote listening) and allowed-origins
        # (it changes admission). Keep ChromeDriver's localhost-only defaults.
        with (artifacts / "chromedriver.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(driver, f"--port={driver_port}",
                "--log-level=WARNING", stdout=log, stderr=log)
            try:
                async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
                    browser = Browser(session, f"http://127.0.0.1:{driver_port}")
                    async def ready():
                        assert process.returncode is None, "ChromeDriver exited during startup"
                        try:
                            return (await browser.request("GET", "/status"))["ready"]
                        except ClientConnectionError:
                            return False
                    await _eventually("installed ChromeDriver readiness", ready)
                    async with _external_player(workspace, mpv) as (backend, _):
                        evidence["mpv_version"] = await _property(backend, "mpv-version")
                        await backend.initialize_gain()
                        tracks = [workspace / name for name in ("first.wav", "second.wav")]
                        for path, frequency in zip(tracks, (440, 660)):
                            _write_tone(path, frequency)
                        cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                            state_dir=str(workspace / "state"), bind_address="127.0.0.1", advertise_ip="127.0.0.1",
                            default_volume=25, auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
                        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
                        await api.initialize_channel_balance()
                        app = api.app()
                        @web.middleware
                        async def observe(request, handler):
                            response = await handler(request)
                            if request.path == "/orr/v1/web/command":
                                # Do not re-read or pre-consume the production body stream.
                                result = json.loads(response.body)
                                evidence["http_commands"].append({"status": response.status,
                                    "request_acknowledged": result.get("request_acknowledged")})
                            if request.path == "/orr/v1/web/playlist/export" and response.headers.get("Content-Disposition"):
                                evidence["http_exports"].append({"status": response.status,
                                    "sha256": hashlib.sha256(response.body).hexdigest(), "size_bytes": len(response.body)})
                            return response
                        app.middlewares.append(observe)
                        async def media(request):
                            evidence["media_requests"] += 1
                            return web.FileResponse(tracks[int(request.match_info["name"] == "second")])
                        app.router.add_get("/browser-smoke/{name:first|second}.wav", media)
                        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
                        try:
                            await runner.setup()
                            site = web.TCPSite(runner, "127.0.0.1", 0)
                            await site.start()
                            port = site._server.sockets[0].getsockname()[1]
                            cfg.http_api_port = cfg.upnp_port = port
                            base = f"http://127.0.0.1:{port}"
                            queue = PlaybackQueue(name="Browser smoke", index=1, items=[QueueItem(
                                url=base + "/browser-smoke/" + p.name, title="Synthetic tone", duration=30) for p in tracks])
                            namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
                            xml = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                                f'<u:CreateQueue xmlns:u="{namespace}"><QueueContext>'
                                f'{escape(serialize_queue_context(queue))}</QueueContext></u:CreateQueue></s:Body></s:Envelope>')
                            async with session.post(base + "/upnp/control/PlayQueue", data=xml,
                                headers={"SOAPACTION": f'"{namespace}#CreateQueue"', "Content-Type": "text/xml"}) as response:
                                assert response.status == 200, await response.text()
                            for mobile in (False, True):
                                label = "mobile" if mobile else "desktop"
                                downloads = workspace / (label + "-downloads")
                                downloads.mkdir()
                                async with browser.opened(chrome, downloads, mobile=mobile) as capabilities:
                                    evidence["browser"] = {"name": capabilities["browserName"],
                                        "version": capabilities["browserVersion"],
                                        "driver": capabilities["chrome"]["chromedriverVersion"], "sandbox": "normal; no bypass flags"}
                                    try:
                                        await exercise(browser, api, backend, base, artifacts, downloads, mobile=mobile, evidence=evidence)
                                    except BaseException:
                                        await browser.diagnostics(artifacts, label + "-failure")
                                        raise
                            assert evidence["media_requests"] > 0
                        finally:
                            await runner.cleanup()
                            assert api._state_monitor_task is None
            finally:
                await _reap(process)
                native_log = workspace / "mpv-external.log"
                if native_log.exists():
                    shutil.copyfile(native_log, artifacts / "mpv.log")


async def worker(artifacts: Path) -> None:
    sources = sorted((ROOT / "fake_linkplay").rglob("*.py")) + sorted((ROOT / "fake_linkplay/web_assets").glob("*"))
    sources += [Path(__file__), ROOT / "tests/test_mpv_unix_integration.py", ROOT / "tools/native_probe_supervisor.py"]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources if path.is_file()}
    evidence = {"status": "FAILED", "lane": "REAL_CHROME / production HTTP / production Unix IPC / mpv null",
        "architecture": platform.machine(), "source_sha256": hashes, "http_commands": [], "http_exports": [], "media_requests": 0,
        "scope": "Generated local WAV only; mobile emulation, not hardware. No physical audio, Firefox, screen reader or broad accessibility claim."}
    try:
        async with asyncio.timeout(120):
            await smoke(artifacts, evidence)
        evidence["status"] = "PASSED"
    except BaseException:
        (artifacts / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        (artifacts / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True, help="New output directory for synthetic smoke evidence")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    if args.worker:
        asyncio.run(worker(artifacts))
        return 0
    artifacts.mkdir(parents=True, exist_ok=False)
    # Existing native guard owns the entire smoke process tree on success,
    # timeout and signals. This hard cap is separate from the native CI lanes.
    try:
        result = run_probe([sys.executable, str(Path(__file__).resolve()), "--worker", "--artifacts", str(artifacts)],
                           timeout=150, cwd=ROOT)
    except BaseException:
        (artifacts / "supervisor-failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    output = (result.stdout or "") + (result.stderr or "")
    (artifacts / "probe.log").write_text(output, encoding="utf-8")
    print(output, end="", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
