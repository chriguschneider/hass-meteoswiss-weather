"""STAC discovery for the local-forecast runs (docs/ogd.md §E4).

The local forecast is published as one CSV per parameter per hourly run; the
files of a run land over a few minutes, so the newest run in the catalogue is
often still incomplete. :func:`latest_run` picks the newest run that already
carries every parameter the caller needs, so a half-published run is skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from .const import stac_items_url
from .http import get_text
from .models import OgdParseError

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


async def latest_run(
    session: aiohttp.ClientSession,
    collection: str,
    required_params: tuple[str, ...] | list[str],
) -> Run:
    """Newest run in ``collection`` that carries every ``required_params`` file.

    Lists the collection's STAC items, groups the assets by the run timestamp
    in their filename, and returns the newest complete run. Raises
    :class:`OgdParseError` if the listing is unreadable or no run is complete.
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
            for name, asset in (feature.get("assets") or {}).items():
                href = asset.get("href") if isinstance(asset, dict) else None
                match = _ASSET_RE.search(name) or (
                    _ASSET_RE.search(href) if href else None
                )
                if match is None:
                    continue
                runs.setdefault(match["ts"], {})[match["param"]] = href or name

        url = _next_link(document)
        pages += 1

    complete = {ts: assets for ts, assets in runs.items() if needed <= assets.keys()}
    if not complete:
        raise OgdParseError(
            f"no complete run for {collection}: none carried {sorted(needed)}"
        )

    newest = max(complete)
    return Run(timestamp=_parse_timestamp(newest), assets=complete[newest])


def _next_link(document: dict) -> str | None:
    """The ``rel: next`` pagination href of a STAC listing, if any."""
    for link in document.get("links", []):
        if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None
