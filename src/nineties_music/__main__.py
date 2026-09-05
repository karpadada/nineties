import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import AppConfig
from .web import create_app


def _storage_arguments(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--local", action="store_true", help="use a local music library")
    selection.add_argument("--simulator", action="store_true", help="use the virtual player")
    parser.add_argument("--simulator-dir", type=Path, help="virtual player directory")


def _config(arguments: argparse.Namespace) -> AppConfig:
    mode = None
    if arguments.local:
        mode = "local"
    elif arguments.simulator:
        mode = "simulator"
    if arguments.simulator_dir is not None:
        if mode == "local":
            raise ValueError("--simulator-dir cannot be used with --local")
        mode = "simulator"
    return AppConfig.from_environment(
        storage_mode=mode, simulator_dir=arguments.simulator_dir
    )


def main(argv: Sequence[str] | None = None) -> None:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments and raw_arguments[0] == "simulator":
        from .cli import run_simulator_cli

        raise SystemExit(run_simulator_cli(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "agent":
        from .cli import run_agent_cli

        storage_parser = argparse.ArgumentParser(add_help=False)
        _storage_arguments(storage_parser)
        selection, agent_arguments = storage_parser.parse_known_args(raw_arguments[1:])
        try:
            config = _config(selection)
        except ValueError as exc:
            storage_parser.error(str(exc))
        raise SystemExit(run_agent_cli(config, agent_arguments))

    parser = argparse.ArgumentParser(description="Run the 90s Music Browser")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("web",),
        default="web",
        help="serve the local web interface",
    )
    _storage_arguments(parser)
    arguments = parser.parse_args(raw_arguments)
    try:
        config = _config(arguments)
    except ValueError as exc:
        parser.error(str(exc))
    app = create_app(config)
    app.run(host=config.host, port=config.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
