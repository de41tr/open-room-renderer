#!/usr/bin/env python3
"""Serve one explicitly selected local audio file, read-only, until Ctrl-C.

Standalone foreground helper for a trusted LAN. It does not start playback,
change the renderer, install a service, or expose a directory.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
from pathlib import Path
import re
import socket
import stat
import threading


AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
               ".ogg": "audio/ogg", ".opus": "audio/ogg", ".aac": "audio/aac",
               ".m4a": "audio/mp4"}
LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def bind_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a literal loopback or private LAN address") from exc
    if not address.is_loopback and not any(address in network for network in LAN_NETWORKS):
        raise argparse.ArgumentTypeError("Use a specific loopback or private LAN address, not a wildcard/public address")
    return str(address)


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a port from 1025 to 65535") from exc
    if not 1025 <= port <= 65535:
        raise argparse.ArgumentTypeError("Use a port from 1025 to 65535")
    return port


def byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", value)
    if not match or not any(match.groups()):
        raise ValueError("Only one byte range is supported")
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
        if start >= size or start > end:
            raise ValueError("Unsatisfiable byte range")
    else:
        length = int(last)
        if length == 0:
            raise ValueError("Unsatisfiable byte range")
        start, end = max(0, size - length), size - 1
    return start, end, True


class AudioServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, handler, fd, size, content_type):
        self.fd, self.size, self.content_type = fd, size, content_type
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class AudioHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes every response; no upload or persistent request body.
    server_version = "ORR-local-audio"
    sys_version = ""

    def log_message(self, *_args):
        pass  # No request URLs, file paths, or client addresses in access logs.

    def do_HEAD(self):
        self.audio(head=True)

    def do_GET(self):
        self.audio(head=False)

    def audio(self, *, head):
        if self.path != "/audio":
            self.send_error(404, "Only the selected audio resource is available")
            return
        server = self.server
        try:
            ranges = [] if head else self.headers.get_all("Range", [])
            if len(ranges) > 1:
                raise ValueError("Multiple ranges")
            start, end, partial = byte_range(ranges[0] if ranges else None, server.size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{server.size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", server.content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{server.size}")
        self.end_headers()
        if not head:
            try:
                while start <= end:
                    chunk = os.pread(server.fd, min(128 * 1024, end - start + 1), start)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    start += len(chunk)
            except OSError:
                pass  # Pausing/stopping a decoder can close its request.


@contextmanager
def selected_audio(path: Path):
    content_type = AUDIO_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise ValueError("Choose MP3, WAV, FLAC, Ogg/Opus, AAC or M4A audio, not a playlist")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError("Choose a nonempty regular audio file, not a directory, link or device")
        yield fd, info.st_size, content_type
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="One local audio file you have permission to play")
    parser.add_argument("--bind", type=bind_address, default="127.0.0.1", help="Specific private LAN address or loopback (default: 127.0.0.1)")
    parser.add_argument("--port", type=port_number, default=8099)
    args = parser.parse_args()
    try:
        with selected_audio(args.file) as asset:
            with AudioServer((args.bind, args.port), AudioHandler, *asset) as server:
                host = f"[{args.bind}]" if ":" in args.bind else args.bind
                print(f"Audio URL: http://{host}:{args.port}/audio", flush=True)
                print("One file, read-only. No playback was started. Stop playback, then Ctrl-C to close this server.", flush=True)
                server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot serve the selected file: {type(exc).__name__}. Check the audio file, address and port.\n")


if __name__ == "__main__":
    main()
