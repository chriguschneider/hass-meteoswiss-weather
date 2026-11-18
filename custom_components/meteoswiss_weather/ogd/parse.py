"""CSV cell and timestamp helpers shared by the OGD parsers.

Pure Python, no Home Assistant imports (ADR-0001). Named ``parse`` rather than
``csv`` so it never shadows the stdlib module for readers.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from .const import CSV_SEPARATOR
from .models import OgdParseError

# ``reference_timestamp`` cells of the station and pollen files (UTC).
TIMESTAMP_FORMAT = "%d.%m.%Y %H:%M"


def csv_reader(body: str) -> csv.DictReader:
    """Return a ``DictReader`` over a ``;``-separated OGD CSV body."""
    return csv.DictReader(io.StringIO(body), delimiter=CSV_SEPARATOR)


def to_float(value: str | None) -> float | None:
    """Parse a numeric cell; empty or non-numeric means "no value", not zero."""
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def to_int(value: str | None) -> int | None:
    """Parse an integer cell (point/type ids, symbol codes)."""
    number = to_float(value)
    return int(number) if number is not None else None


def parse_timestamp(
    value: str | None, abbr: str, *, subject: str = "observation"
) -> datetime:
    """Parse ``dd.mm.yyyy HH:MM`` (UTC) into an aware datetime.

    ``subject`` names the kind of row in the "has no timestamp" error so the
    station and pollen parsers stay distinguishable.
    """
    if not value or value.strip() == "":
        raise OgdParseError(f"{abbr}: {subject} row has no timestamp")
    try:
        naive = datetime.strptime(value.strip(), TIMESTAMP_FORMAT)
    except ValueError as err:
        raise OgdParseError(f"{abbr}: bad timestamp {value!r}") from err
    return naive.replace(tzinfo=UTC)


def parse_stamp(stamp: str | None) -> datetime | None:
    """Parse a ``YYYYMMDDHHMM`` stamp (UTC) into an aware datetime.

    Returns ``None`` for a missing or too short/non-numeric stamp.
    """
    if stamp is None:
        return None
    digits = stamp.strip()
    if len(digits) < 12 or not digits[:12].isdigit():
        return None
    return datetime(
        int(digits[:4]),
        int(digits[4:6]),
        int(digits[6:8]),
        int(digits[8:10]),
        int(digits[10:12]),
        tzinfo=UTC,
    )
