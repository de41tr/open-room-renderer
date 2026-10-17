"""Focused bounds for the optional single-file LAN helper; no audio claim."""
from contextlib import contextmanager
from http.client import HTTPConnection
import importlib.util
from pathlib import Path
import threading

import pytest

SPEC = importlib.util.spec_from_file_location("demo_audio", Path(__file__).parents[1] / "tools/serve_demo_audio.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


@contextmanager
def serving(path):
    with demo.selected_audio(path) as asset:
        with demo.AudioServer(("127.0.0.1", 0), demo.AudioHandler, *asset) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server.server_address[1]
            finally:
                server.shutdown()
                thread.join(timeout=2)


def request(port, method="GET", path="/audio", headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_http_serves_only_selected_file_and_bounded_ranges(tmp_path):
    source = tmp_path / "private-name.mp3"
    source.write_bytes(b"0123456789")
    (tmp_path / "private.txt").write_text("must not be served")
    with serving(source) as port:
        status, headers, body = request(port)
        assert (status, body) == (200, b"0123456789")
        assert headers["Content-Type"] == "audio/mpeg"
        assert headers["Content-Length"] == "10"
        assert request(port, "HEAD")[2] == b""
        assert request(port, "HEAD", headers={"Range": "bytes=0-1"})[0] == 200
        for value, expected in [("bytes=2-4", b"234"), ("bytes=7-", b"789"), ("bytes=-2", b"89")]:
            status, headers, body = request(port, headers={"Range": value})
            assert status == 206 and body == expected
            assert int(headers["Content-Length"]) == len(body)
        for value in ("bytes=10-", "bytes=4-2", "bytes=-0", "bytes=0-1,4-5", "bytes=-"):
            status, headers, body = request(port, headers={"Range": value})
            assert (status, headers["Content-Range"], body) == (416, "bytes */10", b"")
        for path in ("/", "/private-name.mp3", "/../private.txt", "/%2e%2e/private.txt", "/audio?file=private.txt"):
            status, _, body = request(port, path=path)
            assert status == 404 and b"must not be served" not in body
        assert request(port, "POST")[0] == 501
        # Renaming/replacing the pathname cannot expose a different file.
        source.unlink()
        source.symlink_to(tmp_path / "private.txt")
        assert request(port)[2] == b"0123456789"


def test_rejects_unsafe_file_bind_and_port_inputs(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    link = tmp_path / "link.mp3"
    link.symlink_to(source)
    empty = tmp_path / "empty.wav"
    empty.touch()
    directory = tmp_path / "directory.mp3"
    directory.mkdir()
    playlist = tmp_path / "tracks.m3u"
    playlist.write_text("https://example.invalid/audio")
    for path in (link, empty, directory, playlist):
        with pytest.raises((OSError, ValueError)):
            with demo.selected_audio(path):
                pytest.fail("unsafe input was accepted")
    for address in ("0.0.0.0", "::", "example.invalid", "8.8.8.8"):
        with pytest.raises(demo.argparse.ArgumentTypeError):
            demo.bind_address(address)
    for value in ("0", "1024", "65536", "no"):
        with pytest.raises(demo.argparse.ArgumentTypeError):
            demo.port_number(value)
    assert demo.bind_address("127.0.0.1") == "127.0.0.1"
    assert demo.bind_address("::1") == "::1"
