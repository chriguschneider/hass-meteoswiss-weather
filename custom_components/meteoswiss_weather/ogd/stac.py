"""STAC discovery for the local-forecast runs (docs/ogd.md §E4).

The local forecast is published as one CSV per parameter per hourly run; the
files of a run land over a few minutes, so the newest run in the catalogue is
often still incomplete. :func:`latest_run_from_day_item` is the primary entry
point: it fetches today's UTC day item conditionally (ETag → 304 when unchanged)
and falls back to yesterday's item then the full listing. :func:`latest_run`
remains the last-resort listing fallback, used when day items are unavailable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import aiohttp

from .const import stac_day_item_url, stac_items_url
from .http import CachedResponse, get_text
from .models import OgdConnectionError, OgdParseError

# Asset filenames look like ``vnut12.lssw.<YYYYMMDDHHMM>.<param>.csv``; the run
# timestamp groups the per-parameter files, the param names the column.
_ASSET_RE = re.compile(
    r"vnut12\.lssw\.(?P<ts>\d{12})\.(?P<param>[^.]+)\.csv$"
)

# Cap on pagination follow-through: the items listing spans ~three days of
# runs (docs/ogd.md), a handful of pages; the cap only guards a broken cursor.
_MAX_PAGES = 50


@dataclass(frozen=True, slots=True)
class Run:
    """One hourly forecast run: its UTC timestamp and per-parameter file URLs."""

    timestamp: datetime
    assets: dict[str, str]

    def asset_url(self, param: str) -> str:
        """URL of this run's file for ``param``.

        Raises :class:`KeyError` if the run does not carry the parameter —
        :func:`latest_run` only returns runs that carry the requested ones.
        """
        return self.assets[param]


def _parse_timestamp(ts: str) -> datetime:
    """Parse a ``YYYYMMDDHHMM`` run stamp (UTC) into an aware datetime."""
    return datetime.strptime(ts, "%Y%m%d%H%M").replace(tzinfo=UTC)


def _accumulate_runs(
    runs: dict[str, dict[str, str]], assets: dict
) -> None:
    """Merge one STAC Feature's assets into ``runs`` ({ts: {param: href}})."""
    for name, asset in assets.items():
        href = asset.get("href") if isinstance(asset, dict) else None
        match = _ASSET_RE.search(name) or (_ASSET_RE.search(href) if href else None)
        if match is None:
            continue
        runs.setdefault(match["ts"], {})[match["param"]] = href or name


def _best_complete_run(
    runs: dict[str, dict[str, str]], needed: set[str]
) -> Run | None:
    """Return the newest run that satisfies ``needed``, or ``None``."""
    complete = {ts: assets for ts, assets in runs.items() if needed <= assets.keys()}
    if not complete:
        return None
    newest = max(complete)
    return Run(timestamp=_parse_timestamp(newest), assets=complete[newest])


async def latest_run(
    session: aiohttp.ClientSession,
    collection: str,
    required_params: tuple[str, ...] | list[str],
) -> Run:
    """Newest run in ``collection`` that carries every ``required_params`` file.

    Lists the collection's STAC items, groups the assets by the run timestamp
    in their filename, and returns the newest complete run. Raises
    :class:`OgdParseError` if the listing is unreadable or no run is complete.

    This is the last-resort fallback used by :func:`latest_run_from_day_item`
    when the day items are unavailable; prefer that function in callers.
    """
    needed = set(required_params)
    # param URLs keyed by run timestamp, accumulated across pages.
    runs: dict[str, dict[str, str]] = {}

    url: str | None = stac_items_url(collection)
    pages = 0
    while url is not None and pages < _MAX_PAGES:
        response = await get_text(session, url)
        try:
            document = json.loads(response.body)
        except ValueError as err:
            raise OgdParseError(f"STAC items for {collection} were not JSON") from err

        for feature in document.get("features", []):
            _accumulate_runs(runs, feature.get("assets") or {})

        url = _next_link(document)
        pages += 1

    run = _best_complete_run(runs, needed)
    if run is None:
        raise OgdParseError(
            f"no complete run for {collection}: none carried {sorted(needed)}"
        )
    return run


async def latest_run_from_day_item(
    session: aiohttp.ClientSession,
    collection: str,
    required_params: tuple[str, ...] | list[str],
    today_cache: CachedResponse,
    yesterday_cache: CachedResponse,
) -> Run:
    """Newest run via the UTC day item, with fallback to yesterday then listing.

    Fetches today's day item conditionally (``ETag`` → 304 with 0 bytes when
    unchanged, docs/ogd.md §E4 "Run discovery"). Falls back to yesterday's day
    item when today's has no complete run yet (e.g. the minutes after 00:00 UTC
    when the new day's item is not yet published), then to the full items
    listing as the last resort.

    ``today_cache`` and ``yesterday_cache`` are mutated in place by
    :func:`~.http.get_text` and must be held by the caller across ticks so
    the ETag survives between polls. Rotate them on a UTC day boundary:
    promote the old today cache to yesterday and create a fresh today cache.

    Both day-item fetches degrade gracefully on any connection error (the
    listing is the definitive fallback). A malformed JSON response raises
    :class:`OgdParseError` immediately without trying further fallbacks.
    """
    needed = set(required_params)
    today: date = datetime.now(UTC).date()
    today_id = today.strftime("%Y%m%d") + "-ch"
    yesterday_id = (today - timedelta(days=1)).strftime("%Y%m%d") + "-ch"

    run = await _run_from_day_item(session, collection, today_id, today_cache, needed)
    if run is not None:
        return run

    run = await _run_from_day_item(
        session, collection, yesterday_id, yesterday_cache, needed
    )
    if run is not None:
        return run

    return await latest_run(session, collection, required_params)


async def _run_from_day_item(
    session: aiohttp.ClientSession,
    collection: str,
    day_id: str,
    cache: CachedResponse,
    needed: set[str],
) -> Run | None:
    """Fetch one day item and return the newest complete run, or ``None``.

    Returns ``None`` (instead of raising) on any ``OgdConnectionError`` so the
    caller can try the next fallback without special-casing 404 vs. other HTTP
    errors. A malformed JSON response propagates as ``OgdParseError``.
    """
    url = stac_day_item_url(collection, day_id)
    try:
        response = await get_text(session, url, cache=cache)
    except OgdConnectionError:
        return None

    try:
        document = json.loads(response.body)
    except ValueError as err:
        raise OgdParseError(f"day item {url} was not JSON") from err

    runs: dict[str, dict[str, str]] = {}
    _accumulate_runs(runs, document.get("assets") or {})
    return _best_complete_run(runs, needed)


def _next_link(document: dict) -> str | None:
    """The ``rel: next`` pagination href of a STAC listing, if any."""
    for link in document.get("links", []):
        if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None
