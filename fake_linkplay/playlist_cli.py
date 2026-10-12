"""Client-only authored playlists: create/import offline and restore via SOAP."""
from __future__ import annotations

import argparse
import sys

from .playlist_files import (PLAYLIST_MODES, PlaylistCommittedError, PlaylistError,
                             PlaylistFileExistsError, PlaylistFileTypeError, load_playlist,
                             read_playlist_bytes, save_playlist,
                             validate_playlist)
from .playlist_m3u import M3UError, decode_m3u_urls
from .queue_client import QueueClient, QueueClientError, explicit_queue, prepare_replace


def add_playlist_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add authored-file commands without an export-from-live-queue command."""
    parser = subparsers.add_parser("playlist", help="author and restore explicit-URL playlist files")
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
    restore = commands.add_parser("restore", help="replace the queue, stopped unless --play is given")
    restore.add_argument("file")
    restore.add_argument("--play", action="store_true")
    restore.add_argument("--host", default="127.0.0.1")
    restore.add_argument("--port", type=int, default=8080)


def run_playlist(args: argparse.Namespace) -> int:
    """Validate all input before writing a file or contacting a renderer."""
    try:
        if args.playlist_cmd == "import-m3u":
            payload = validate_playlist({"version": 1, "name": args.name,
                                         "index": 1, "mode": "normal",
                                         "urls": decode_m3u_urls(read_playlist_bytes(args.input))})
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
        if args.playlist_cmd == "import-m3u":
            save_playlist(args.output, payload)
            print("M3U URL list imported. Titles and durations were not saved.")
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
        if args.playlist_cmd == "import-m3u":
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
