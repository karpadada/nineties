from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .agent import MusicAgentAPI, _summary
from .config import AppConfig
from .downloader import DownloadError
from .services import create_services
from .store import ManifestError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nineties agent",
        description="Use Nineties through a one-shot JSON command-line interface.",
        epilog="Storage selection: nineties agent [--local | --simulator] [--simulator-dir PATH] <command> ...",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="search for albums and playlists")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)

    track_search = commands.add_parser(
        "track-search", help="search YouTube Music for individual track matches"
    )
    track_search.add_argument("query")
    track_search.add_argument("--limit", type=int, default=5)

    download = commands.add_parser("download", help="download one collection and wait")
    download.add_argument("source_url")
    download.add_argument("--kind", choices=("album", "playlist"))

    status = commands.add_parser("status", help="inspect one or all active downloads")
    status.add_argument("job_id", nargs="?")

    library = commands.add_parser("library", help="inspect the managed library")
    library.add_argument("--query")
    library.add_argument("--limit", type=int, default=50)

    commands.add_parser(
        "safely-remove",
        help="eject the connected Music device after downloads finish",
    )

    spotify = commands.add_parser(
        "spotify", help="connect and sync Spotify playlists"
    )
    spotify_commands = spotify.add_subparsers(dest="spotify_command", required=True)
    spotify_commands.add_parser("status", help="inspect Spotify connection status")
    spotify_commands.add_parser("playlists", help="list available Spotify playlists")
    spotify_sync = spotify_commands.add_parser(
        "sync", help="mirror one Spotify playlist to the music device"
    )
    spotify_sync.add_argument("playlist_id")
    spotify_sync.add_argument(
        "--match",
        action="append",
        default=[],
        metavar="SPOTIFY_TRACK_ID=YOUTUBE_URL",
        help="override a missing or incorrect automatic track match",
    )

    return parser


def _write(payload: dict[str, Any], *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def run_agent_cli(config: AppConfig, argv: Sequence[str]) -> int:
    arguments = _parser().parse_args(argv)
    try:
        services = create_services(
            config,
            start_worker=arguments.command == "download",
            recover_interrupted=True,
        )
        api = MusicAgentAPI(services)
        if arguments.command == "search":
            result = api.search(arguments.query, arguments.limit)
        elif arguments.command == "track-search":
            result = api.track_search(arguments.query, arguments.limit)
        elif arguments.command == "download":
            queued = api.download(arguments.source_url, arguments.kind)["collection"]
            completed = services.downloads.wait(queued["id"])
            result = {"collection": _summary(completed)}
        elif arguments.command == "status":
            result = api.status(arguments.job_id)
        elif arguments.command == "library":
            result = api.library(arguments.query, arguments.limit)
        elif arguments.command == "safely-remove":
            result = api.safely_remove()
        elif arguments.spotify_command == "status":
            result = api.spotify_status()
        elif arguments.spotify_command == "playlists":
            result = api.spotify_playlists()
        else:
            result = api.spotify_sync(
                arguments.playlist_id, _parse_matches(arguments.match)
            )
    except (DownloadError, ManifestError, OSError, sqlite3.Error, ValueError) as exc:
        _write({"error": str(exc)}, error=True)
        return 2

    _write(result)
    return 0


def _parse_matches(values: list[str]) -> dict[str, str]:
    matches: dict[str, str] = {}
    for value in values:
        track_id, separator, url = value.partition("=")
        if not separator or not track_id.strip() or not url.strip():
            raise ValueError(
                "Each --match must be SPOTIFY_TRACK_ID=YOUTUBE_URL."
            )
        matches[track_id.strip()] = url.strip()
    return matches


def run_simulator_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nineties simulator", description="Run or control a persistent virtual player."
    )
    parser.add_argument(
        "action", nargs="?", default="web",
        choices=("web", "connect", "disconnect", "status", "agent"),
    )
    parser.add_argument("--directory", type=Path, help="virtual player directory")
    arguments, remaining = parser.parse_known_args(argv)
    if remaining and arguments.action != "agent":
        parser.error(f"unrecognized arguments: {' '.join(remaining)}")
    try:
        config = AppConfig.from_environment(
            storage_mode="simulator", simulator_dir=arguments.directory
        )
        if arguments.action == "agent":
            return run_agent_cli(config, remaining)
        if arguments.action == "web":
            from .web import create_app

            app = create_app(config)
            app.run(host=config.host, port=config.port, debug=False, threaded=True)
            return 0
        services = create_services(config, start_worker=False)
        simulator = services.simulator
        assert simulator is not None
        if arguments.action == "connect":
            simulator.connect()
        elif arguments.action == "disconnect":
            simulator.disconnect(services.store)
        _write(simulator.status())
        return 0
    except (DownloadError, ManifestError, OSError, sqlite3.Error, ValueError) as exc:
        _write({"error": str(exc)}, error=True)
        return 2
