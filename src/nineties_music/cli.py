from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .agent import MusicAgentAPI, _summary
from .config import AppConfig
from .downloader import DownloadError
from .services import create_services


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nineties agent",
        description="Use Nineties through a one-shot JSON command-line interface.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="search for albums and playlists")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)

    download = commands.add_parser("download", help="download one collection and wait")
    download.add_argument("source_url")
    download.add_argument("--kind", choices=("album", "playlist"))

    status = commands.add_parser("status", help="inspect one or all active downloads")
    status.add_argument("job_id", nargs="?")

    library = commands.add_parser("library", help="inspect the managed library")
    library.add_argument("--query")
    library.add_argument("--limit", type=int, default=50)

    return parser


def _write(payload: dict[str, Any], *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def run_agent_cli(config: AppConfig, argv: Sequence[str]) -> int:
    arguments = _parser().parse_args(argv)
    services = create_services(
        config,
        start_worker=arguments.command == "download",
        recover_interrupted=True,
    )
    api = MusicAgentAPI(services)

    try:
        if arguments.command == "search":
            result = api.search(arguments.query, arguments.limit)
        elif arguments.command == "download":
            queued = api.download(arguments.source_url, arguments.kind)["collection"]
            completed = services.downloads.wait(queued["id"])
            result = {"collection": _summary(completed)}
        elif arguments.command == "status":
            result = api.status(arguments.job_id)
        else:
            result = api.library(arguments.query, arguments.limit)
    except (DownloadError, ValueError) as exc:
        _write({"error": str(exc)}, error=True)
        return 2

    _write(result)
    return 0
