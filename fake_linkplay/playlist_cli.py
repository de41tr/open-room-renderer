"""Local URL playlists: author/edit offline, snapshot, and restore via SOAP."""
from __future__ import annotations

import argparse
import re
import sys

from .playlist_client import fetch_current_playlist
from .playlist_edits import append_playlist, move_playlist, remove_playlist
from .playlist_files import (PLAYLIST_MODES, PlaylistCommittedError, PlaylistError,
                             PlaylistFileExistsError, PlaylistFileTypeError, load_playlist,
                             read_playlist_bytes, save_playlist,
                             validate_playlist)
from .playlist_m3u import M3UError, decode_m3u_urls
from .queue_client import QueueClient, QueueClientError, explicit_queue, prepare_replace


def _positive_index(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("Index must be positive ASCII digits")
    return int(value)


def add_playlist_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add explicit URL-file commands; snapshots never change playback."""
    parser = subparsers.add_parser("playlist", help="save and restore explicit-URL playlist files")
    commands = parser.add_subparsers(dest="playlist_cmd", required=True)
    create = commands.add_parser("create", help="write an authored playlist; files may contain URL secrets")
    create.add_argument("file")
    create.add_argument("--name", required=True)
    create.add_argument("--start-index", type=int, default=1)
    create.add_argument("--mode", choices=PLAYLIST_MODES, default="normal")
    create.add_argument("--overwrite", action="store_true")
    create.add_argument("urls", nargs="+")
    importer = commands.add_parser("import-m3u", help="convert a strict M3U URL list offline",
        description="Import the supported UTF-8 M3U URL-list subset into a new private JSON file. "
                    "Titles and durations are discarded; no media is fetched or played.")
    importer.add_argument("input", metavar="INPUT")
    importer.add_argument("output", metavar="OUTPUT")
    importer.add_argument("--name", required=True)
    for command in ("append", "remove", "move"):
        editor = commands.add_parser(command, help=f"{command} URL occurrences into a new file offline",
            description="Edit a valid authored URL playlist into a new private file. "
                        "The source is unchanged; no media is fetched or played.")
        editor.add_argument("input", metavar="INPUT")
        editor.add_argument("output", metavar="OUTPUT")
        if command == "append":
            editor.add_argument("urls", nargs="+", metavar="URL")
        elif command == "remove":
            editor.add_argument("--index", required=True, type=_positive_index)
        else:
            editor.add_argument("--from-index", required=True, type=_positive_index)
            editor.add_argument("--to-index", required=True, type=_positive_index)
    current = commands.add_parser("save-current", help="save the current queue as a private URL playlist",
        description="Save one current-queue URL snapshot without changing playback. "
                    "This is not a lossless backup; metadata and playback state are omitted. "
                    "Files may contain URL secrets and URLs may expire.")
    current.add_argument("file")
    current.add_argument("--host", default="127.0.0.1")
    current.add_argument("--port", type=int, default=8080)
    restore = commands.add_parser("restore", help="replace the queue, stopped unless --play is given")
    restore.add_argument("file")
    restore.add_argument("--play", action="store_true")
    restore.add_argument("--host", default="127.0.0.1")
    restore.add_argument("--port", type=int, default=8080)


def run_playlist(args: argparse.Namespace) -> int:
    """Validate all input before writing a file or contacting a renderer."""
    try:
        if args.playlist_cmd == "save-current":
            payload = fetch_current_playlist(args.host, args.port)
        elif args.playlist_cmd == "import-m3u":
            payload = validate_playlist({"version": 1, "name": args.name,
                                         "index": 1, "mode": "normal",
                                         "urls": decode_m3u_urls(read_playlist_bytes(args.input))})
        elif args.playlist_cmd == "append":
            payload = append_playlist(load_playlist(args.input), args.urls)
        elif args.playlist_cmd == "remove":
            payload = remove_playlist(load_playlist(args.input), args.index)
        elif args.playlist_cmd == "move":
            payload = move_playlist(load_playlist(args.input), args.from_index, args.to_index)
        elif args.playlist_cmd == "create":
            payload = validate_playlist({"version": 1, "name": args.name,
                                         "index": args.start_index, "mode": args.mode,
                                         "urls": args.urls})
        else:
            payload = load_playlist(args.file)
        queue = explicit_queue(payload["name"], payload["urls"],
                               start_index=payload["index"], mode=payload["mode"])
        # Validate the whole escaped SOAP request before any file/network write.
        prepare_replace(queue)
        if args.playlist_cmd == "save-current":
            save_playlist(args.file, payload)
            print("Current URL playlist saved. Metadata and playback state were not saved. "
                  "Keep the file private; URLs may contain secrets or expire.")
        elif args.playlist_cmd == "import-m3u":
            save_playlist(args.output, payload)
            print("M3U URL list imported. Titles and durations were not saved.")
        elif args.playlist_cmd in {"append", "remove", "move"}:
            save_playlist(args.output, payload)
            print("Edited URL playlist saved. Playback was not changed.")
        elif args.playlist_cmd == "create":
            save_playlist(args.file, payload, overwrite=args.overwrite)
            print("Authored playlist saved.")
        else:
            QueueClient(args.host, args.port).replace(queue, play=args.play)
            print("Playlist restored; playback request acknowledged." if args.play
                  else "Playlist restored; playback was not requested.")
        return 0
    except PlaylistCommittedError:
        print("Playlist saved, but crash durability is unconfirmed. "
              "Inspect the destination before retrying.", file=sys.stderr)
    except PlaylistFileExistsError:
        if args.playlist_cmd in {"import-m3u", "save-current", "append", "remove", "move"}:
            print("Playlist not saved: destination already exists. Choose a new output file.", file=sys.stderr)
        else:
            print("Playlist not saved: destination already exists. "
                  "Use --overwrite to replace it explicitly.", file=sys.stderr)
    except PlaylistFileTypeError:
        print("Playlist path must be a regular file; symbolic links are not accepted.", file=sys.stderr)
    except M3UError:
        print("Invalid M3U URL list. Check the supported UTF-8 subset and limits.", file=sys.stderr)
    except PlaylistError:
        print("Invalid authored playlist. Check the v1 schema, UTF-8, index, mode and limits.", file=sys.stderr)
    except QueueClientError as exc:
        # QueueClientError is deliberately limited to fixed, private-safe text.
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError):
        # File names, operating-system errors and file contents may hold secrets.
        print("Playlist file operation failed. Check the path and permissions.", file=sys.stderr)
    return 1
