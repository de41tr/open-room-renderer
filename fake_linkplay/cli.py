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
RESOURCE_CLEANUP_TIMEOUT = 10.0


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
    mpv = MpvBackend(
        cfg.mpv_socket, cfg.auto_spawn_mpv,
        initial_volume=state.volume, initial_mute=state.mute,
    )
    runners: list[web.AppRunner] = []
    ssdp: SsdpServer | None = None
    mdns: MdnsAdvertiser | None = None
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    serve_task = asyncio.current_task()
    assert serve_task is not None
    signal_requested = False
    cleaning_up = False

    def request_shutdown() -> None:
        nonlocal signal_requested
        # Cancel startup as well as the normal serving wait. Leave these
        # handlers installed during teardown, when further signals are harmless.
        if not signal_requested and not cleaning_up:
            signal_requested = True
            serve_task.cancel()

    async def cleanup() -> None:
        # Register resources before their first await, so partially completed
        # setup receives the same cleanup as a fully running service. One
        # cleanup failure must not strand the remaining listeners or child.
        resources = []
        if mdns is not None:
            resources.append(("mDNS", mdns.stop))
        if ssdp is not None:
            resources.append(("SSDP", ssdp.stop))
        resources.extend(("HTTP runner", runner.cleanup) for runner in reversed(runners))
        resources.append(("mpv", mpv.stop_process))
        for name, close in resources:
            try:
                async with asyncio.timeout(RESOURCE_CLEANUP_TIMEOUT):
                    await close()
                    if name == "SSDP" and ssdp is not None and ssdp.tasks:
                        await asyncio.gather(*ssdp.tasks, return_exceptions=True)
            except Exception:
                LOG.exception("failed to clean up %s", name)

    try:
        # Install before the first startup await, while no child/listener can
        # yet require asynchronous cleanup after SIGINT or SIGTERM.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_shutdown)
            registered_signals.append(sig)
        try:
            await mpv.start()
            await mpv.stop()
            await mpv.initialize_gain()
        except BackendError as exc:
            state.mark_error()
            LOG.warning("backend unavailable at startup; control API will report failures: %s", exc)
        api = HttpApi(cfg, state, mpv)
        try:
            # Reconcile even zero with an externally managed backend's old
            # named graph; publish only through the API's confirmed transaction.
            await api.initialize_channel_balance()
        except (BackendError, OSError, ValueError):
            state.mark_error("Channel balance initialization failed")
            LOG.warning("could not confirm initial channel balance")
        ports = [cfg.http_api_port]
        if cfg.upnp_port not in ports:
            ports.append(cfg.upnp_port)
        if cfg.mirror_port_80 and 80 not in ports:
            ports.append(80)
        for port in ports:
            runner = web.AppRunner(api.app())
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, cfg.bind_address, port).start()
        if cfg.enable_ssdp:
            ssdp = SsdpServer(cfg)
            await ssdp.start()
        mdns = MdnsAdvertiser(cfg)
        await mdns.start()
        api.on_rename = mdns.refresh
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
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if not signal_requested:
            raise
    finally:
        cleaning_up = True
        # Signals are ignored until cleanup completes. Repeated task.cancel()
        # calls also cannot abandon the independent, bounded teardown task.
        cleanup_task = asyncio.create_task(cleanup())
        cancelled_during_cleanup = False
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled_during_cleanup = True
            cleanup_task.result()
        finally:
            for sig in registered_signals:
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    LOG.exception("failed to remove signal handler %s", sig)
        if cancelled_during_cleanup and not signal_requested:
            raise asyncio.CancelledError


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
