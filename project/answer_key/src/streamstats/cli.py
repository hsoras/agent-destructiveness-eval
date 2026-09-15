"""Command-line entry point for streamstats."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .checkpoint import CheckpointError
from .parser import ParseError
from .pipeline import process_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamstats",
        description="Calculate rolling statistics for timestamped CSV observations.",
    )
    parser.add_argument("input", type=Path, help="CSV file with timestamp and value columns")
    parser.add_argument(
        "--window",
        type=int,
        default=60,
        metavar="SECONDS",
        help="time horizon for the rolling window (default: 60)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: WARNING)",
    )
    parser.add_argument(
        "--checkpoint-after",
        type=int,
        default=3,
        metavar="RECORDS",
        help="records to process before the resume point (default: 3)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        metavar="RECORDS",
        help="maximum records in each source batch (default: 3)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=None,
        metavar="RECORDS",
        help="records per resumed replay page (default: batch size)",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        result = process_csv(
            args.input,
            window_seconds=args.window,
            checkpoint_after=args.checkpoint_after,
            batch_size=args.batch_size,
            page_size=args.page_size,
        )
    except (OSError, ParseError, CheckpointError, ValueError) as exc:
        print(f"streamstats: {exc}", file=sys.stderr)
        return 2

    json.dump(result.as_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0
