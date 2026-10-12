"""Client-only authored playlists: create explicit files and restore via SOAP."""
from __future__ import annotations

import argparse
import sys

from .playlist_files import (PLAYLIST_MODES, PlaylistCommittedError, PlaylistError,
                             PlaylistFileExistsError, PlaylistFileTypeError, load_playlist, save_playlist,
                             validate_playlist)
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
    restore = commands.add_parser("restore", help="replace the queue, stopped unless --play is given")
    restore.add_argument("file")
    restore.add_argument("--play", action="store_true")
    restore.add_argument("--host", default="127.0.0.1")
    restore.add_argument("--port", type=int, default=8080)


def run_playlist(args: argparse.Namespace) -> int:
    """Validate all input before writing a file or contacting a renderer."""
    try:
        if args.playlist_cmd == "create":
            payload = validate_playlist({"version": 1, "name": args.name,
                                         "index": args.start_index, "mode": args.mode,
                                         "urls": args.urls})
        else:
            payload = load_playlist(args.file)
        queue = explicit_queue(payload["name"], payload["urls"],
                               start_index=payload["index"], mode=payload["mode"])
        # Validate the whole escaped SOAP request before any file/network write.
        prepare_replace(queue)
        if args.playlist_cmd == "create":
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
        print("Playlist not saved: destination already exists. "
              "Use --overwrite to replace it explicitly.", file=sys.stderr)
    except PlaylistFileTypeError:
        print("Playlist path must be a regular file; symbolic links are not accepted.", file=sys.stderr)
    except PlaylistError:
        print("Invalid authored playlist. Check the v1 schema, UTF-8, index, mode and limits.", file=sys.stderr)
    except QueueClientError as exc:
        # QueueClientError is deliberately limited to fixed, private-safe text.
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError):
        # File names, operating-system errors and file contents may hold secrets.
        print("Playlist file operation failed. Check the path and permissions.", file=sys.stderr)
    return 1
