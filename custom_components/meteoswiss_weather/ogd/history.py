"""Station hourly history: asset discovery, range selection, streaming parse.

Implements ADR-0007 (issue #51). Pure Python, no Home Assistant imports (ADR-0001).

Flow:
  1. Fetch the station's STAC item to learn which history files exist.
  2. Select the files whose content overlaps the requested [start, end] window.
  3. Fetch each file once (largest: ~13 MB), parse in the executor line-by-line,
     and collect only the rows that fall inside the window.

Nothing here polls; the caller (a service or the reconfigure flow) drives the
fetch. Conditional requests via ``get_text`` are available but omitted here
because this is a one-off, user-triggered operation (ADR-0007).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from datetime import UTC, datetime

import aiohttp

from .const import (
    CSV_SEPARATOR,
    STATION_ENCODING,
    station_stac_item_url,
)
from .http import get_text
from .models import HourlyHistoryRow, OgdParseError

# Matches ``_h_recent.csv`` at the end of an asset key or href.
_RECENT_RE = re.compile(r"_h_recent\.csv$")
# Matches ``_h_historical_YYYY-YYYY.csv``; groups 1/2 are decade start/end.
_HISTORICAL_RE = re.compile(r"_h_historical_(\d{4})-(\d{4})\.csv$")

_TS_COL = "reference_timestamp"
_TS_FORMAT = "%d.%m.%Y %H:%M"

# Hourly history parameter codes (docs/ogd.md §A1, "History files").
_HISTORY_CODES: dict[str, str] = {
    "temp_mean": "tre200h0",
    "temp_min": "tre200hn",
    "temp_max": "tre200hx",
    "humidity": "ure200h0",
    "dew_point": "tde200h0",
    "pressure_qff": "pp0qffh0",
    "wind_speed_kmh": "fu3010h0",
    "gust_kmh": "fu3010h1",
    "precipitation_sum": "rre150h0",
    "sunshine": "sre000h0",
    "global_radiation": "gre000h0",
}


async def _fetch_assets(session: aiohttp.ClientSession, abbr: str) -> dict[str, str]:
    """Return ``{asset_key: href}`` for every asset in the station's STAC item."""
    url = station_stac_item_url(abbr)
    response = await get_text(session, url)
    try:
        item = json.loads(response.body)
    except ValueError as err:
        raise OgdParseError(f"{abbr}: station STAC item was not JSON") from err

    assets: dict[str, str] = {}
    for key, asset in (item.get("assets") or {}).items():
        href = asset.get("href") if isinstance(asset, dict) else None
        if href:
            assets[key] = href
    return assets


def select_history_files(
    assets: dict[str, str],
    start: datetime,
    end: datetime,
    *,
    _current_year: int | None = None,
) -> list[str]:
    """Return ordered hrefs for history files that overlap ``[start, end]``.

    - ``_h_recent.csv`` is included when the current calendar year falls in
      ``[start.year, end.year]``.
    - ``_h_historical_YYYY-YYYY+9.csv`` is included when its *effective* data
      range overlaps ``[start.year, end.year]``.  For the decade that contains
      the current year, the effective end is ``current_year − 1`` (the last
      completed year); for fully past decades the nominal end is used.

    Files are returned oldest-first so the caller fetches them chronologically.
    ``_current_year`` is exposed for deterministic testing; it defaults to
    ``datetime.now(UTC).year``.
    """
    current_year = (
        _current_year if _current_year is not None else datetime.now(UTC).year
    )
    selected: list[tuple[int, str]] = []  # (sort_key, href)

    for key, href in assets.items():
        if _RECENT_RE.search(key) or _RECENT_RE.search(href):
            # The recent file covers the current calendar year.
            if start.year <= current_year <= end.year:
                selected.append((current_year, href))
        elif m := (_HISTORICAL_RE.search(key) or _HISTORICAL_RE.search(href)):
            decade_start = int(m.group(1))
            decade_end = int(m.group(2))
            # Cap the effective end at current_year-1 for the incomplete decade.
            effective_end = min(decade_end, current_year - 1)
            if decade_start <= end.year and effective_end >= start.year:
                selected.append((decade_start, href))

    selected.sort(key=lambda t: t[0])
    return [href for _, href in selected]


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_body(body: str, start: datetime, end: datetime) -> list[HourlyHistoryRow]:
    """Parse a history CSV body; return rows within ``[start, end]``.

    Iterates the CSV row by row (streaming) so the full decoded row list is
    never materialised — only the matching subset is. Intended to run inside
    ``loop.run_in_executor`` to avoid blocking the event loop.
    """
    reader = csv.DictReader(io.StringIO(body), delimiter=CSV_SEPARATOR)
    if reader.fieldnames is None or _TS_COL not in reader.fieldnames:
        raise OgdParseError("history file is missing its header or timestamp column")

    rows: list[HourlyHistoryRow] = []
    for row in reader:
        ts_str = (row.get(_TS_COL) or "").strip()
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            continue
        if ts < start or ts > end:
            continue
        values = {
            field: _to_float(row.get(code)) for field, code in _HISTORY_CODES.items()
        }
        rows.append(HourlyHistoryRow(ts_utc=ts, **values))
    return rows


async def fetch_station_history(
    session: aiohttp.ClientSession,
    abbr: str,
    start: datetime,
    end: datetime,
) -> list[HourlyHistoryRow]:
    """Fetch hourly history rows for ``abbr`` within ``[start, end]``.

    Discovers the available history assets from the station STAC item, selects
    files that overlap the range, fetches them one at a time (largest ≈ 13 MB),
    and parses each in the executor. The returned list is sorted by timestamp.
    """
    assets = await _fetch_assets(session, abbr)
    urls = select_history_files(assets, start, end)
    if not urls:
        return []

    loop = asyncio.get_running_loop()
    all_rows: list[HourlyHistoryRow] = []
    for url in urls:
        response = await get_text(session, url, encoding=STATION_ENCODING)
        rows = await loop.run_in_executor(None, _parse_body, response.body, start, end)
        all_rows.extend(rows)

    return all_rows
