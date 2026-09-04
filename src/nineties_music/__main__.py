import argparse
import sys
from collections.abc import Sequence

from .config import AppConfig
from .web import create_app


def main(argv: Sequence[str] | None = None) -> None:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments and raw_arguments[0] == "agent":
        from .cli import run_agent_cli

        config = AppConfig.from_environment()
        raise SystemExit(run_agent_cli(config, raw_arguments[1:]))

    parser = argparse.ArgumentParser(description="Run the 90s Music Browser")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("web",),
        default="web",
        help="serve the local web interface",
    )
    arguments = parser.parse_args(raw_arguments)
    config = AppConfig.from_environment()
    app = create_app(config)
    app.run(host=config.host, port=config.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
