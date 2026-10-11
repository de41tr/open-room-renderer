"""Command-line entry points and service startup orchestration.

This module intentionally stays small: it wires config, state, HTTP servers,
discovery responders, and the mpv backend together. Most compatibility behavior
lives in ``http_api.py`` and should be tested there.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from urllib.parse import quote

import requests
from aiohttp import web

from .config import load_config
from .http_api import HttpApi
from .mdns import MdnsAdvertiser
from .mpv_backend import BackendError, MpvBackend
from .ssdp import SsdpServer
from .state import RendererState
from .upnp_description import description_xml
from .utils import setup_logging

LOG = logging.getLogger(__name__)


async def serve(config_path: str) -> None:
    """Start HTTP, discovery, mDNS, and mpv services until interrupted.

    Args:
        config_path: Path to the YAML config file.

    Side Effects:
        Starts network listeners, may start an mpv subprocess, and persists
        runtime state through lower-level handlers.
    """

    cfg = load_config(config_path)
    setup_logging(cfg.log_level)
    state = RendererState.from_config(cfg)
    mpv = MpvBackend(cfg.mpv_socket, cfg.auto_spawn_mpv)
    await mpv.start()
    try:
        await mpv.stop()
    except BackendError:
        state.mark_error()
        LOG.warning("backend unavailable at startup; control API will report failures")
    api = HttpApi(cfg, state, mpv)
    if cfg.channel_balance != "0.0":
        try:
            await mpv.channel_balance(float(cfg.channel_balance))
        except ValueError:
            LOG.warning("ignoring invalid persisted channel balance: %s", cfg.channel_balance)
    api_runner = web.AppRunner(api.app())
    desc_runner = None
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, cfg.bind_address, cfg.http_api_port)
    await api_site.start()
    if cfg.upnp_port != cfg.http_api_port:
        desc_runner = web.AppRunner(api.app())
        await desc_runner.setup()
        desc_site = web.TCPSite(desc_runner, cfg.bind_address, cfg.upnp_port)
        await desc_site.start()
    port80_runner = None
    if cfg.mirror_port_80 and cfg.http_api_port != 80 and cfg.upnp_port != 80:
        port80_runner = web.AppRunner(api.app())
        await port80_runner.setup()
        port80_site = web.TCPSite(port80_runner, cfg.bind_address, 80)
        await port80_site.start()
    ssdp = SsdpServer(cfg) if cfg.enable_ssdp else None
    if ssdp:
        await ssdp.start()
    mdns = MdnsAdvertiser(cfg)
    await mdns.start()
    LOG.info("API URL: http://%s:%s/httpapi.asp?command=getStatusEx", cfg.resolved_advertise_ip, cfg.http_api_port)
    LOG.info("Description URL: %s", cfg.description_url)
    LOG.info("UUID: uuid:%s", cfg.upnp_uuid)
    if ssdp:
        LOG.info("SSDP LOCATION: %s", cfg.description_url)
    else:
        LOG.info("SSDP disabled")
    LOG.info("mpv socket: %s", cfg.mpv_socket)
    LOG.info("Acceptance: ./scripts/acceptance-open-source-client.sh")
    LOG.info('Curl: curl "http://%s:%s/httpapi.asp?command=getStatusEx" | jq', cfg.resolved_advertise_ip, cfg.http_api_port)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    await mdns.stop()
    if ssdp:
        await ssdp.stop()
    await api_runner.cleanup()
    if desc_runner:
        await desc_runner.cleanup()
    if port80_runner:
        await port80_runner.cleanup()
    await mpv.stop_process()


def api_get(host: str, port: int, command: str) -> None:
    """Run one HTTP API command and print the raw response body."""

    url = f"http://{host}:{port}/httpapi.asp?command={command}"
    print(requests.get(url, timeout=10).text)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser used by the console script."""

    parser = argparse.ArgumentParser(prog="open-room-renderer")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--config", default="configs/config.example.yaml")
    for name in ("status", "pause", "resume", "stop"):
        p = sub.add_parser(name)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=80)
    p = sub.add_parser("play")
    p.add_argument("url")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=80)
    p = sub.add_parser("volume")
    p.add_argument("value", type=int)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=80)
    p = sub.add_parser("print-description")
    p.add_argument("--config", default="configs/config.example.yaml")
    p = sub.add_parser("print-discovery-info")
    p.add_argument("--config", default="configs/config.example.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector for tests. ``None`` uses
            ``sys.argv`` via ``argparse``.

    Returns:
        Process exit status.
    """

    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        asyncio.run(serve(args.config))
        return 0
    if args.cmd == "status":
        api_get(args.host, args.port, "getStatusEx")
    elif args.cmd == "play":
        api_get(args.host, args.port, "setPlayerCmd:play:" + quote(args.url, safe=""))
    elif args.cmd in {"pause", "resume", "stop"}:
        api_get(args.host, args.port, f"setPlayerCmd:{args.cmd}")
    elif args.cmd == "volume":
        api_get(args.host, args.port, f"setPlayerCmd:vol:{args.value}")
    elif args.cmd == "print-description":
        print(description_xml(load_config(args.config)))
    elif args.cmd == "print-discovery-info":
        cfg = load_config(args.config)
        print(f"API: {cfg.api_base_url}/httpapi.asp?command=getStatusEx")
        print(f"Description: {cfg.description_url}")
        print(f"UUID: {cfg.upnp_uuid}")
        if cfg.wiim_home_profile:
            print(f"mDNS: {cfg.linkplay_instance_name}._linkplay._tcp.local:{cfg.http_api_port}")
            print(f"security: {cfg.security}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
