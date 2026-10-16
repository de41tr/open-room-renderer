"""Command-line entry points and service startup orchestration.

This module intentionally stays small: it wires config, state, HTTP servers,
discovery responders, and the mpv backend together. Most compatibility behavior
lives in ``http_api.py`` and should be tested there.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import errno
import logging
import re
import signal
import sys
from urllib.parse import quote

import requests
from aiohttp import web

from .audio_diagnostic import add_audio_parser, run_audio_diagnostic
from .config import StartupConfigError, load_config, load_startup_config
from .config_check import add_config_parser, run_config_check
from .discovery_client import add_discovery_parser, run_discovery
from .dsp_client import add_dsp_parser, run_dsp_command
from .favorites_client import favorite_play_id, play_favorite
from .favorites_target_client import set_favorite_url
from .http_api import HttpApi
from .mdns import MdnsAdvertiser
from .mpv_backend import BackendError, MpvBackend
from .playlist_cli import add_playlist_parser, run_playlist
from .queue_client import (PrivateClientArgumentParser, QueueClientError, _endpoint,
                           add_queue_commands, run_queue_command)
from .spotify_diagnostic import add_spotify_parser, run_spotify_diagnostic
from .ssdp import SsdpServer
from .state import RendererState
from .startup_state import StartupStateError
from .transport_client import COMMANDS, add_transport_commands, run_transport_command
from .upnp_description import description_xml
from .utils import setup_logging

LOG = logging.getLogger(__name__)
RESOURCE_CLEANUP_TIMEOUT = 10.0


class StartupListenerError(Exception):
    """A configured HTTP listener cannot start because its port is occupied."""


async def _start_catalog_proxy(api: HttpApi, runners: list[web.AppRunner]) -> None:
    """Cover specific/IPv6 binds without expanding the configured LAN listener.

    Ephemeral loopback port avoids competing for the configured API port. This
    optional proxy has no control routes or observer; the normal CLI teardown
    owns its runner even when setup is interrupted or fails.
    """
    if api.cfg.bind_address in {"0.0.0.0", "127.0.0.1", ""}:
        return
    api.catalog_proxy_port = 0
    app = web.Application()
    app.router.add_get("/proxy/audio", api.proxy_audio)
    runner = web.AppRunner(app)
    runners.append(runner)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        api.catalog_proxy_port = site._server.sockets[0].getsockname()[1]
    except OSError:
        LOG.warning("Radio catalog loopback proxy unavailable; ordinary renderer startup continues")


async def serve(config_path: str | None = None, *, spotify_source=None) -> None:
    """Start HTTP, discovery, mDNS, and mpv services until interrupted.

    Args:
        config_path: Explicit YAML file path, which must be readable. None uses
            the historical example-path default, including its missing fallback.

    Side Effects:
        Starts network listeners, may start an mpv subprocess, and persists
        runtime state through lower-level handlers.
    """

    # Opt-in reserves the existing listener addresses before identity/state or
    # mpv access. A duplicate invocation cannot stop/adopt the running renderer.
    with ExitStack() as resources:
        listener_sockets = None
        def prepare(cfg):
            nonlocal spotify_source, listener_sockets
            if cfg.spotify_eq_profile is None and spotify_source is None:
                return
            from .spotify_eq import prepare_activation
            spotify_source, listener_sockets = prepare_activation(cfg, spotify_source, resources)
        cfg = (load_startup_config("configs/config.example.yaml", missing_ok=True, before_state=prepare)
               if config_path is None else load_startup_config(config_path, before_state=prepare))
        await _serve(cfg, spotify_source=spotify_source, listener_sockets=listener_sockets)


async def _serve(cfg, *, spotify_source=None, listener_sockets=None) -> None:
    setup_logging(cfg.log_level)
    state = RendererState.from_config(cfg)
    mpv = MpvBackend(
        cfg.mpv_socket, cfg.auto_spawn_mpv,
        initial_volume=state.volume, initial_mute=state.mute,
        audio_device=cfg.mpv_audio_device,
    )
    api = None
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
        if spotify_source is not None:
            resources.append(("Spotify PCM source", api.close_spotify if api is not None else spotify_source.close))
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
        # yet require asynchronous cleanup. On Linux, SIGHUP requests the
        # same graceful stop as SIGINT/SIGTERM; it does not reload config.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, request_shutdown)
            registered_signals.append(sig)
        try:
            await mpv.start()
            await mpv.stop()
            await mpv.initialize_gain()
        except BackendError as exc:
            state.mark_error()
            LOG.warning("backend unavailable at startup; control API will report failures: %s", exc)
        api = HttpApi(cfg, state, mpv, _startup=True)
        try:
            # Restore the complete active EQ/balance graph, including bypass,
            # before listeners. Backend load/resume will retry this desired
            # graph if startup is temporarily unavailable.
            await api.initialize_channel_balance()
        except (BackendError, OSError, ValueError):
            state.mark_error("DSP initialization failed")
            LOG.warning("could not confirm initial active DSP state")
        ports = [cfg.http_api_port]
        if cfg.upnp_port not in ports:
            ports.append(cfg.upnp_port)
        if cfg.mirror_port_80 and 80 not in ports:
            ports.append(80)
        for port in ports:
            runner = web.AppRunner(api.app())
            runners.append(runner)
            await runner.setup()
            try:
                if listener_sockets is None:
                    await web.TCPSite(runner, cfg.bind_address, port).start()
                else:
                    await web.SockSite(runner, listener_sockets[port]).start()
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                raise StartupListenerError(
                    "An HTTP listener port is already in use. Check for another running "
                    "renderer or service, or choose unused http_api_port/upnp_port "
                    "settings and review mirror_port_80."
                ) from None
        await _start_catalog_proxy(api, runners)
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
        if spotify_source is not None:
            try:
                await spotify_source.start(api)
            except (BackendError, OSError, ValueError):
                # Keep the normal service/UI available; do not turn an account
                # or receiver failure into the unit's automatic restart loop.
                if not spotify_source.stopping and api.spotify_source is spotify_source:
                    spotify_source.failed = True
                    state.mark_error("Spotify startup unconfirmed; Stop before changing sources")
                LOG.warning("Spotify startup unconfirmed; no automatic receiver retry")
            # Keep only the prevalidated local profile, never a browser path or
            # credentials. A click creates one new disposable runtime/child.
            from .spotify_eq import SpotifyEqSource
            api.spotify_source_factory = lambda: SpotifyEqSource(spotify_source.root, dict(spotify_source.profile))
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


def api_get(host: str, port: int, command: str) -> int:
    """Print a successful response or a safe error and return an exit status."""

    try:
        url = _endpoint(host, port, path=f"/httpapi.asp?command={command}")
    except QueueClientError:
        print("Request failed: invalid renderer host or port", file=sys.stderr)
        return 1
    try:
        # Use only the explicit endpoint, without ambient proxy/netrc credentials.
        # Default Requests adapters do not retry; redirects remain disabled.
        with requests.Session() as session:
            # Skip hidden Location parsing despite allow_redirects=False, keeping
            # errors private. Existing non-streaming body reads remain unbounded.
            session.resolve_redirects = lambda *_args, **_kwargs: iter(())
            session.trust_env = False
            with session.get(url, timeout=10, allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    # Do not print the exception, request URL or response body: a play
                    # command can contain a credential-bearing media URL in its query.
                    print(f"Request failed: HTTP {response.status_code}", file=sys.stderr)
                    return 1
                print(response.text)
                return 0
    except requests.Timeout:
        print("Request failed: timed out", file=sys.stderr)
        return 1
    except requests.RequestException:
        print("Request failed: could not contact renderer", file=sys.stderr)
        return 1


def favorite_id(value: str) -> str:
    """Accept only renderer-generated IDs, never arbitrary URL path input."""

    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise argparse.ArgumentTypeError("expected a 32-character lowercase hexadecimal favorite ID")
    return value


def favorites_request(
    host: str, port: int, method: str, path: str = "",
    payload: dict[str, str] | None = None,
) -> int:
    """Manage favorites through the renderer, keeping failed requests private."""

    try:
        url = _endpoint(host, port, path=f"/orr/v1/favorites{path}")
    except QueueClientError:
        print("Request failed: invalid renderer host or port", file=sys.stderr)
        return 1
    try:
        kwargs = {"json": payload} if payload is not None else {}
        with requests.Session() as session:
            # Skip hidden Location parsing despite allow_redirects=False, keeping
            # errors private. Existing non-streaming body reads remain unbounded.
            session.resolve_redirects = lambda *_args, **_kwargs: iter(())
            session.trust_env = False
            with session.request(method, url, timeout=10, allow_redirects=False, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    # Inspect only a fixed, owned error discriminator. Never print server
                    # text, media URLs, names, or exceptions from an unsuccessful request.
                    try:
                        error = response.json()
                    except ValueError:
                        error = None
                    if (response.status_code == 503 and isinstance(error, dict)
                            and error.get("error") == "durability_unconfirmed" and error.get("committed") is True):
                        print("Favorites were replaced, but crash durability is unconfirmed. "
                              "Run favorites list before retrying.", file=sys.stderr)
                    else:
                        print(f"Request failed: HTTP {response.status_code}", file=sys.stderr)
                    return 1
                print(response.text)
                return 0
    except requests.Timeout:
        print("Request failed: timed out", file=sys.stderr)
        return 1
    except requests.RequestException:
        print("Request failed: could not contact renderer", file=sys.stderr)
        return 1


def build_parser(*, private_client_errors: bool = False) -> argparse.ArgumentParser:
    """Build the command-line parser used by the console script."""

    parser_type = PrivateClientArgumentParser if private_client_errors else argparse.ArgumentParser
    parser = parser_type(
        prog="open-room-renderer",
        epilog="Offline native upgrade preparation: use the new installation\'s Python with "
               "-m fake_linkplay.migration_profile --help (see GETTING_STARTED.md).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--config", help="YAML file (default: configs/config.example.yaml)")
    add_config_parser(sub)
    add_audio_parser(sub)
    for name in ("status", "pause", "resume", "stop"):
        p = sub.add_parser(name)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8080)
    p = sub.add_parser("play")
    p.add_argument("url")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p = sub.add_parser("volume")
    p.add_argument("value", type=int)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    add_discovery_parser(sub)
    add_dsp_parser(sub)
    add_spotify_parser(sub)
    add_transport_commands(sub)
    add_queue_commands(sub)
    add_playlist_parser(sub)
    favorites = sub.add_parser("favorites", help="manage renderer-owned local favorites")
    favorites_sub = favorites.add_subparsers(dest="favorites_cmd", required=True)
    for name in ("list", "add", "save-current", "rename", "delete", "play", "set-url"):
        p = favorites_sub.add_parser(name)
        if name in {"rename", "delete", "set-url"}:
            p.add_argument("favorite_id", type=favorite_id)
        if name == "play":
            p.add_argument("favorite_id", type=favorite_play_id)
        if name == "set-url":
            p.add_argument("url")
            p.add_argument("--metadata", choices=("keep", "clear"), required=True)
        if name in {"add", "save-current", "rename"}:
            p.add_argument("name")
        if name == "add":
            p.add_argument("url")
            for field in ("title", "artist", "album", "artwork"):
                p.add_argument(f"--{field}")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8080)
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

    argv = sys.argv[1:] if argv is None else argv
    legacy_favorite = (len(argv) > 1 and argv[0] == "favorites"
                       and argv[1] in {"list", "add", "save-current", "rename", "delete"})
    # There are no global CLI options. Preserve existing diagnostics when a
    # legacy command uses a new command name as a value, while keeping malformed
    # standalone discovery/config/audio/Spotify invocations (including leading flags) private.
    legacy_command = bool(argv) and argv[0] in {
        "serve", "status", "pause", "resume", "stop", "play", "volume", "queue",
        "playlist", "favorites", "dsp", "print-description", "print-discovery-info", *COMMANDS,
    }
    private_discovery = "discover" in argv and not legacy_command
    private_spotify = "spotify" in argv and not legacy_command
    private_config = "config" in argv and not legacy_command
    private_audio = "audio" in argv and not legacy_command
    private_errors = (private_discovery or private_spotify or private_config or private_audio or any(arg in {"queue", "playlist", "dsp", *COMMANDS} for arg in argv)
                      or {"favorites", "play"} <= set(argv)
                      or ({"favorites", "set-url"} <= set(argv) and not legacy_favorite))
    args = build_parser(private_client_errors=private_errors).parse_args(argv)
    if args.cmd == "audio":
        return run_audio_diagnostic(args)
    if args.cmd == "config":
        return run_config_check(args)
    if args.cmd == "discover":
        return run_discovery(args)
    if args.cmd == "spotify":
        if args.spotify_cmd == "eq":
            from .spotify_eq import run_eq
            return run_eq(args)
        if args.spotify_cmd in {"receiver", "install-client"}:
            from .spotify_receiver import run_receiver
            return run_receiver(args)
        if args.spotify_cmd == "prepare-exclusive":
            from .spotify_exclusive import run_prepare_exclusive
            return run_prepare_exclusive(args)
        return run_spotify_diagnostic(args)
    if args.cmd == "dsp":
        return run_dsp_command(args)
    if args.cmd in COMMANDS:
        return run_transport_command(args)
    if args.cmd == "queue":
        return run_queue_command(args)
    if args.cmd == "playlist":
        return run_playlist(args)
    if args.cmd == "serve":
        try:
            asyncio.run(serve(args.config))
        except (StartupConfigError, StartupStateError, StartupListenerError) as exc:
            print(f"Startup error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "status":
        return api_get(args.host, args.port, "getStatusEx")
    elif args.cmd == "play":
        return api_get(args.host, args.port, "setPlayerCmd:play:" + quote(args.url, safe=""))
    elif args.cmd in {"pause", "resume", "stop"}:
        return api_get(args.host, args.port, f"setPlayerCmd:{args.cmd}")
    elif args.cmd == "volume":
        return api_get(args.host, args.port, f"setPlayerCmd:vol:{args.value}")
    elif args.cmd == "favorites":
        if args.favorites_cmd == "set-url":
            return set_favorite_url(args.host, args.port, args.favorite_id, args.url, args.metadata)
        if args.favorites_cmd == "play":
            return play_favorite(args.host, args.port, args.favorite_id)
        if args.favorites_cmd == "list":
            return favorites_request(args.host, args.port, "GET")
        if args.favorites_cmd == "add":
            payload = {"name": args.name, "url": args.url}
            payload.update({field: getattr(args, field) for field in ("title", "artist", "album", "artwork")
                            if getattr(args, field) is not None})
            return favorites_request(args.host, args.port, "POST", payload=payload)
        if args.favorites_cmd == "save-current":
            return favorites_request(args.host, args.port, "POST", "/current", {"name": args.name})
        if args.favorites_cmd == "rename":
            return favorites_request(args.host, args.port, "PATCH", f"/{args.favorite_id}", {"name": args.name})
        if args.favorites_cmd == "delete":
            return favorites_request(args.host, args.port, "DELETE", f"/{args.favorite_id}")
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
