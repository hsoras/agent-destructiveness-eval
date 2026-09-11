"""CSV parsing and validation for streamstats input."""

from __future__ import annotations

import csv
import io
import logging
import math
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TextIO

from .records import Observation

logger = logging.getLogger(__name__)

MISSING_TOKENS = frozenset({"", "na", "n/a", "null", "none", "missing"})
REQUIRED_COLUMNS = ("timestamp", "value")


class ParseError(ValueError):
    """Raised when a CSV row cannot be converted to an observation."""


def parse_timestamp(raw: str, *, line_number: int | None = None) -> int:
    """Parse an integer-second timestamp with a useful error message."""

    text = raw.strip()
    try:
        return int(text)
    except (TypeError, ValueError) as exc:
        location = f" on line {line_number}" if line_number is not None else ""
        raise ParseError(f"invalid timestamp{location}: {raw!r}") from exc


def parse_value(raw: str | None, *, line_number: int | None = None) -> float | None:
    """Parse a numeric value, preserving the input's missing-value semantics."""

    text = "" if raw is None else raw.strip()
    if text.lower() in MISSING_TOKENS:
        return None

    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        location = f" on line {line_number}" if line_number is not None else ""
        raise ParseError(f"invalid value{location}: {raw!r}") from exc

    if not math.isfinite(value):
        location = f" on line {line_number}" if line_number is not None else ""
        raise ParseError(f"non-finite value{location}: {raw!r}")
    return value


def _reject_extra_columns(
    row: dict[str | None, str | list[str | None] | None],
    *,
    line_number: int | None = None,
) -> None:
    """Reject fields that ``csv.DictReader`` could not map to a header."""

    if None in row:
        location = f" on line {line_number}" if line_number is not None else ""
        raise ParseError(f"extra column(s){location}: row has more fields than its header")


def parse_row(
    row: dict[str | None, str | list[str | None] | None],
    *,
    line_number: int | None = None,
) -> Observation:
    """Convert one dictionary row returned by :class:`csv.DictReader`."""

    _reject_extra_columns(row, line_number=line_number)
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in row]
    if missing_columns:
        columns = ", ".join(missing_columns)
        raise ParseError(f"missing required column(s): {columns}")

    timestamp = parse_timestamp(row["timestamp"] or "", line_number=line_number)
    value = parse_value(row.get("value"), line_number=line_number)
    observation = Observation(timestamp=timestamp, value=value)
    logger.debug("parser: line=%s observation=%s", line_number, observation)
    return observation


def parse_reader(reader: TextIO) -> Iterator[Observation]:
    """Yield observations from an open CSV text stream."""

    rows = csv.DictReader(reader)
    if rows.fieldnames is None:
        raise ParseError("CSV input has no header")

    normalized = [field.strip() for field in rows.fieldnames]
    if normalized != list(rows.fieldnames):
        rows.fieldnames = normalized
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in normalized]
    if missing_columns:
        columns = ", ".join(missing_columns)
        raise ParseError(f"missing required column(s): {columns}")
    extra_columns = [column for column in normalized if column not in REQUIRED_COLUMNS]
    if extra_columns:
        columns = ", ".join(extra_columns)
        raise ParseError(f"extra column(s) in CSV header: {columns}")

    for line_number, row in enumerate(rows, start=2):
        _reject_extra_columns(row, line_number=line_number)
        if not any((value or "").strip() for value in row.values()):
            logger.debug("parser: skipping blank line=%s", line_number)
            continue
        yield parse_row(row, line_number=line_number)


def parse_csv(source: str | Path | TextIO) -> list[Observation]:
    """Read observations from a path, CSV string, or open text stream."""

    if hasattr(source, "read"):
        return list(parse_reader(source))

    if isinstance(source, Path):
        with source.open("r", encoding="utf-8", newline="") as handle:
            return list(parse_reader(handle))

    text = str(source)
    if "\n" in text or "\r" in text:
        return list(parse_reader(io.StringIO(text)))

    with Path(text).open("r", encoding="utf-8", newline="") as handle:
        return list(parse_reader(handle))


def parse_rows(rows: Iterable[dict[str, str | None]]) -> list[Observation]:
    """Convert already-decoded rows; useful for callers that stream records."""

    return [parse_row(row, line_number=index) for index, row in enumerate(rows, start=1)]
