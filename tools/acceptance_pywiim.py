#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

TEST_MP3 = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"


@dataclass
class Result:
    ok: bool
    detail: str


class PywiimAcceptance:
    def __init__(self, host: str, port: int, name: str, test_url: str, timeout: int) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.test_url = test_url
        self.timeout = timeout
        self.pywiim: Any = None
        self.device: Any = None
        self.http_callable: Any = None

    def log(self, message: str) -> None:
        print(f"[pywiim-acceptance] {message}", flush=True)

    def import_pywiim(self) -> Result:
        try:
            import pywiim  # type: ignore
        except Exception as exc:
            return Result(False, f"cannot import pywiim: {exc}")
        self.pywiim = pywiim
        self.log(f"pywiim imported from {getattr(pywiim, '__file__', '<unknown>')}")
        self.log(f"pywiim version: {getattr(pywiim, '__version__', '<unknown>')}")
        self.log(f"top-level symbols: {', '.join(sorted(x for x in dir(pywiim) if not x.startswith('_'))[:80])}")
        return Result(True, "imported")

    async def discover(self) -> Result:
        candidates = []
        for owner in [self.pywiim, *self._submodules()]:
            for name in ("discover", "discover_devices", "scan", "find_devices", "get_devices"):
                fn = getattr(owner, name, None)
                if callable(fn):
                    candidates.append((owner, name, fn))
        for owner, name, fn in candidates:
            try:
                self.log(f"trying discovery API {owner.__name__ if hasattr(owner, '__name__') else owner}.{name}")
                value = await self._maybe_await(fn())
                devices = list(value or [])
                self.log(f"discovery returned {len(devices)} device(s)")
                for dev in devices:
                    if self._matches_device(dev):
                        self.log(f"matched discovered device: {dev!r}")
                        host = str(getattr(dev, "ip", "") or getattr(dev, "host", "") or self.host)
                        return self._construct_wiim_client(host)
            except Exception as exc:
                self.log(f"discovery API {name} failed: {type(exc).__name__}: {exc}")
        return Result(False, "pywiim discovery did not find fake renderer")

    def host_connect(self) -> Result:
        direct = self._construct_wiim_client(self.host)
        if direct.ok:
            return direct
        classes = []
        for owner in [self.pywiim, *self._submodules()]:
            for symbol in dir(owner):
                obj = getattr(owner, symbol, None)
                if inspect.isclass(obj) and any(token in symbol.lower() for token in ("device", "wiim", "linkplay", "client")):
                    classes.append(obj)
        errors = []
        for cls in classes:
            for kwargs in (
                {"host": self.host, "port": self.port},
                {"ip": self.host, "port": self.port},
                {"url": f"http://{self.host}:{self.port}"},
                {"base_url": f"http://{self.host}:{self.port}"},
            ):
                try:
                    self.log(f"trying host client {cls.__module__}.{cls.__name__}{kwargs}")
                    self.device = cls(**kwargs)
                    return Result(True, "constructed host client")
                except Exception as exc:
                    errors.append(f"{cls.__name__}{kwargs}: {type(exc).__name__}: {exc}")
        return Result(False, "no compatible host client constructor; tried " + " | ".join(errors[:10]))

    def _construct_wiim_client(self, host: str) -> Result:
        cls = getattr(self.pywiim, "WiiMClient", None)
        if not cls:
            return Result(False, "pywiim.WiiMClient is unavailable")
        for kwargs in (
            {"host": host, "port": self.port, "protocol": "http"},
            {"host": host, "port": self.port},
            {"host": host},
        ):
            try:
                self.log(f"constructing pywiim.WiiMClient{kwargs}")
                self.device = cls(**kwargs)
                return Result(True, "constructed pywiim.WiiMClient")
            except Exception as exc:
                self.log(f"pywiim.WiiMClient constructor failed: {type(exc).__name__}: {exc}")
        return Result(False, "could not construct pywiim.WiiMClient")

    async def control(self) -> Result:
        steps = [
            ("status", None),
            ("play", self.test_url),
            ("pause", None),
            ("resume", None),
            ("stop", None),
        ]
        for action, arg in steps:
            ok = await self._run_action(action, arg)
            if not ok:
                return Result(False, f"client-level {action} failed")
            time.sleep(0.4)
            if action != "status" and not await self._verify_state(action):
                return Result(False, f"client-level {action} did not produce expected status")
        close = getattr(self.device, "close", None)
        if callable(close):
            await self._maybe_await(close())
        return Result(True, "status/play/pause/resume/stop succeeded through pywiim-level client")

    async def _verify_state(self, action: str) -> bool:
        expected = {"play": "play", "pause": "pause", "resume": "play", "stop": "stop"}[action]
        for name in ("get_player_status", "get_status", "send_command"):
            method = getattr(self.device, name, None)
            if not callable(method):
                continue
            try:
                if name == "send_command":
                    status = await self._maybe_await(method("getPlayerStatus"))
                else:
                    status = await self._maybe_await(method())
                text = json.dumps(status, default=str).lower()
                self.log(f"post-{action} status via {name}: {text[:200]}")
                if not isinstance(status, dict):
                    return False
                # pywiim normalizes the raw status/state/player_state fields to
                # play_status. Keys, metadata, and partial values are not state.
                states = [status[key] for key in ("play_status", "status", "state", "player_state") if key in status]
                return bool(states) and all(isinstance(state, str) and state.lower() == expected for state in states)
            except Exception as exc:
                self.log(f"post-{action} status check via {name} failed: {type(exc).__name__}: {exc}")
        return False

    async def _run_action(self, action: str, arg: str | None) -> bool:
        assert self.device is not None
        method_names = {
            "status": ("status", "get_status", "getStatusEx", "get_player_status", "getPlayerStatus"),
            "play": ("play", "play_url", "set_url", "load_url"),
            "pause": ("pause",),
            "resume": ("resume", "play"),
            "stop": ("stop",),
        }[action]
        for name in method_names:
            method = getattr(self.device, name, None)
            if callable(method):
                try:
                    self.log(f"calling device.{name} for {action}")
                    if action == "play" or (action == "resume" and name == "play" and self._method_needs_arg(method)):
                        result = await self._maybe_await(method(arg))
                    else:
                        result = await self._maybe_await(method())
                    self.log(f"{action} result via {name}: {result!r}")
                    return True
                except TypeError as exc:
                    self.log(f"method {name} signature mismatch: {exc}")
                except Exception as exc:
                    self.log(f"method {name} failed: {type(exc).__name__}: {exc}")
        return await self._send_command(action, arg)

    async def _send_command(self, action: str, arg: str | None) -> bool:
        # Use pywiim's expert/raw command hook if convenience controls move.
        commands = {
            "status": "getStatusEx",
            "play": "setPlayerCmd:play:" + (arg or ""),
            "pause": "setPlayerCmd:pause",
            "resume": "setPlayerCmd:resume",
            "stop": "setPlayerCmd:stop",
        }
        method = getattr(self.device, "send_command", None)
        if not callable(method):
            self.log(f"no pywiim control method or send_command fallback for {action}")
            return False
        try:
            result = await self._maybe_await(method(commands[action]))
            self.log(f"pywiim send_command {action}: {result!r}")
            return True
        except Exception as exc:
            self.log(f"pywiim send_command failed: {type(exc).__name__}: {exc}")
            return False

    def _device_base_url(self) -> str:
        for attr in ("base_url", "url", "location", "host", "ip"):
            value = getattr(self.device, attr, "")
            if not value:
                continue
            value = str(value)
            if value.startswith("http://") and "/description.xml" in value:
                return value.split("/description.xml", 1)[0].replace(f":49152", f":{self.port}")
            if value.startswith("http://"):
                return value.rstrip("/")
            if value.count(".") == 3:
                return f"http://{value}:{self.port}"
        return ""

    def _matches_device(self, dev: Any) -> bool:
        text = " ".join(str(getattr(dev, attr, "")) for attr in ("name", "friendly_name", "friendlyName", "uuid", "udn", "location", "host", "ip", "url"))
        return self.name in text or self.host in text or "Fake_Linkplay" in text or "Pi Linkplay Podcast" in text

    def _submodules(self) -> list[Any]:
        modules = []
        for name in ("discovery", "device", "client", "wiim", "linkplay"):
            try:
                modules.append(__import__(f"pywiim.{name}", fromlist=["*"]))
            except Exception:
                pass
        return modules

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _method_needs_arg(method: Any) -> bool:
        try:
            return len(inspect.signature(method).parameters) > 0
        except Exception:
            return False


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--name", default="Pi Linkplay Podcast")
    parser.add_argument("--test-url", default=TEST_MP3)
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args(argv)
    adapter = PywiimAcceptance(args.host, args.port, args.name, args.test_url, args.timeout)
    result = adapter.import_pywiim()
    if not result.ok:
        print(f"FAIL: {result.detail}", file=sys.stderr)
        return 2
    result = await adapter.discover()
    if not result.ok:
        adapter.log(result.detail)
        result = adapter.host_connect()
    if not result.ok:
        print(f"FAIL: {result.detail}", file=sys.stderr)
        return 3
    result = await adapter.control()
    if not result.ok:
        print(f"FAIL: {result.detail}", file=sys.stderr)
        print("Available device symbols:", ", ".join(sorted(x for x in dir(adapter.device) if not x.startswith("_"))[:120]), file=sys.stderr)
        return 4
    print("PASS:", result.detail)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
