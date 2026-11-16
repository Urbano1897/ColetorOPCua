"""Command line entry point for the collector."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from opcua_collector.collector import Collector
from opcua_collector.config import AppConfig, ConfigError, load_config
from opcua_collector.logging import configure_logging


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="opcua-collector",
        description="Subscribe to an OPC UA cycle counter and store one PostgreSQL row per cycle.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the YAML configuration.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="DEBUG, INFO, WARNING or ERROR. Overrides LOG_LEVEL.",
    )
    parser.add_argument(
        "--log-format",
        choices=("json", "text"),
        default=None,
        help="json (default) or text. Overrides LOG_FORMAT.",
    )
    args = parser.parse_args(argv)
    # A file in the working directory fills in anything the environment
    # does not already have. It never overrides a variable that is set.
    load_dotenv(override=False)
    level = args.log_level or os.environ.get("LOG_LEVEL", "INFO")
    log_format = args.log_format or os.environ.get("LOG_FORMAT", "json")
    configure_logging(level, log_format)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"error: {exc}") from exc
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


async def _run(config: AppConfig) -> None:
    collector = Collector(config)
    _install_stop_signals(collector)
    await collector.run()


def _install_stop_signals(collector: Collector) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, collector.request_stop)
        except NotImplementedError:
            return


if __name__ == "__main__":
    main(sys.argv[1:])
