"""The forecast demand registry (ADR-0008 section 2).

Each forecast consumer declares the upstream parameters it needs; the union of
the demands of the **enabled** features is a run's fetch plan. A feature that is
off demands nothing, so the traffic cost of the hourly option, the cloud layers
and the temperature percentiles gates on the **option** — never on whether a
card happens to be open. The lazy provider of ADR-0002 revision 2 and its
card-driven refresh are gone: when the option is on, the data is fetched whether
or not anything subscribes (ADR-0008, "Decided by the owner", item 1).

Pure bookkeeping: no Home Assistant imports, no network. *When* a demanded file
is refreshed stays with the coordinator's near/far/point-major schedule for now
(the canary is a separate issue); this module answers only *what* a run must
deliver, split by the fetch strategy each file's layout admits so the schedule
can group them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .ogd.const import (
    DAILY_BLOCK_PARAMS,
    DAILY_REQUIRED_PARAMS,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_POINT_MAJOR_PARAMS,
    HOURLY_ZERO_DEGREE,
    SERIES_REQUEST_CAP,
    hourly_date_major_params,
)


@dataclass(frozen=True, slots=True)
class HourlyDemand:
    """The hourly parameters the enabled features need, split by fetch strategy.

    ``date_major`` files ride the near/far horizon tiers (a horizon prefix or a
    row-addressed window each); ``point_major`` files are one ~5 KB point block
    per run. ``params`` is their union — the whole hourly fetch plan for a run.
    """

    date_major: tuple[str, ...]
    point_major: tuple[str, ...]

    @property
    def params(self) -> tuple[str, ...]:
        """Every hourly parameter the enabled features demand for a run."""
        return (*self.date_major, *self.point_major)


def hourly_demand(
    *,
    enabled: bool,
    cloud_layers: bool = False,
    temp_percentiles: bool = False,
) -> HourlyDemand | None:
    """The hourly fetch plan for the enabled features, or ``None`` when off.

    With the hourly option off nothing is demanded. With it on, the base set is
    the point-major group (precipitation, symbol, wind, gust, direction, the
    B7/B8/B10 additions) plus the date-major temperature file; the B9 cloud
    layers and B11 temperature percentiles join the date-major group only when
    their own option is on (ADR-0002 per-entity gating, issue #69).
    """
    if not enabled:
        return None
    return HourlyDemand(
        date_major=hourly_date_major_params(
            cloud_layers=cloud_layers, temp_percentiles=temp_percentiles
        ),
        point_major=HOURLY_POINT_MAJOR_PARAMS,
    )


# --- Traffic estimate (issue #145) -----------------------------------------
#
# A summary step in the options flow tells the user what a chosen combination
# costs before it is saved. The estimate is pure bookkeeping — options → the set
# of demanded files → bytes — so it lives next to the demand registry it derives
# from and stays free of Home Assistant imports (ADR-0001), unit-tested as such.

# File "kinds" the estimate distinguishes, and the typical **warm** bytes one
# fetch of each costs. Measured on the owner's live instance 2026-09-20 (issue
# #145). These are deliberately estimates: upstream re-sorts files without notice
# (ADR-0008), so a file's real cost is whatever its last fetch spent — the
# store's measured bytes override the table for any file already active.
KIND_DAILY_ROW = "daily_row"
KIND_POINT_MAJOR = "point_major"
KIND_HOURLY_DATE_MAJOR = "hourly_date_major"
KIND_ZERO_DEGREE = "zero_degree"

# A row-addressed daily file is ~20–100 KB (ADR-0008 verified 2026-09-18); take
# the middle of that range. A warm point-major block is one point's ~5 KB of rows
# plus its verifying probes. A row-addressed hourly date-major file is ~250 KB at
# the **default** horizon (~72 h) and scales with the horizon (see below).
# ``zprfr0hs`` is its own kind: row-addressed over the 48 h zero-degree window,
# ~130 KB regardless of the hourly horizon.
_KIND_BYTES: dict[str, int] = {
    KIND_DAILY_ROW: 60_000,
    KIND_POINT_MAJOR: 18_000,
    KIND_HOURLY_DATE_MAJOR: 250_000,
    KIND_ZERO_DEGREE: 130_000,
}

# The horizon the date-major table figure is anchored to (the default, ~72 h):
# the date-major cost scales linearly with the number of demanded hours.
_DATE_MAJOR_ANCHOR_HOURS = 72
# Hours in a "full run" horizon (~9 days), used to size the full-run estimate.
_FULL_RUN_HOURS = 220
# The forecast coordinator runs on the hourly tick (ADR-0002); in the worst case
# — upstream changing every run — every demanded file is re-fetched each tick.
# The canary makes most ticks cheap (ADR-0008 section 3), so a per-day figure of
# per-refresh × this is an honest upper bound. Matches "~24 refreshes a day".
REFRESHES_PER_DAY = 24


@dataclass(frozen=True, slots=True)
class FileEstimate:
    """One demanded file's contribution to a refresh's traffic."""

    param: str
    kind: str
    # Bytes one fetch of this file costs; 0 when ``unbounded`` (no fixed number).
    bytes: int
    # True when the figure is the file's last measured fetch (store provenance,
    # ADR-0008 §5), False when it is the table estimate for the kind.
    measured: bool
    # True for a date-major file whose horizon no longer fits row addressing
    # under the request cap: it falls to a large prefix of the ~30 MB file, so
    # there is no fixed number to quote (issue #145; the long-horizon issue).
    unbounded: bool = False


@dataclass(frozen=True, slots=True)
class TrafficEstimate:
    """The estimated traffic for a chosen options combination (issue #145)."""

    files: tuple[FileEstimate, ...]
    refreshes_per_day: int = REFRESHES_PER_DAY

    @property
    def bytes_per_refresh(self) -> int:
        """Bytes one full refresh of the demanded files costs (unbounded = 0)."""
        return sum(f.bytes for f in self.files)

    @property
    def bytes_per_day(self) -> int:
        """The per-refresh figure across a day's refreshes (upper bound)."""
        return self.bytes_per_refresh * self.refreshes_per_day

    @property
    def has_unbounded(self) -> bool:
        """Whether any date-major file exceeds row addressing at this horizon."""
        return any(f.unbounded for f in self.files)

    @property
    def any_measured(self) -> bool:
        """Whether at least one file's figure came from a measured fetch."""
        return any(f.measured for f in self.files)

    @property
    def all_measured(self) -> bool:
        """Whether every file's figure came from a measured fetch."""
        return bool(self.files) and all(f.measured for f in self.files)


def _horizon_hours(horizon_days: int) -> int:
    """Demanded hours for a horizon: the rest of today plus ``horizon_days``.

    An upper bound (the rest of today is counted as a whole day), which is what
    the request-cap check wants. ``HOURLY_HORIZON_FULL_RUN`` is the whole run.
    """
    if horizon_days == HOURLY_HORIZON_FULL_RUN:
        return _FULL_RUN_HOURS
    return (horizon_days + 1) * 24


def _fits_row_addressing(horizon_days: int) -> bool:
    """Whether a date-major file's horizon stays on row addressing.

    A window wider than :data:`SERIES_REQUEST_CAP` hours needs more than one
    request per hour beyond the cap and the ladder falls to a prefix of the
    ~30 MB file (ADR-0008 section 4). 72 h (the default) fits; the full run and
    the long horizons do not.
    """
    return _horizon_hours(horizon_days) <= SERIES_REQUEST_CAP


def _demanded_files(
    *, hourly: bool, cloud_layers: bool, temp_percentiles: bool
) -> dict[str, str]:
    """The whole run's demanded files as ``param → kind`` (deduplicated).

    Derived from the demand registry: the always-on daily baseline (ADR-0002 /
    ADR-0008 — the four daily files plus the point-major blocks behind the daily
    wind, probability and zero-degree) plus, when the hourly option is on, the
    date-major and point-major hourly groups. ``zprfr0hs`` is its own kind
    wherever it appears; a file demanded by more than one group is counted once.
    """
    files: dict[str, str] = {}
    for param in DAILY_REQUIRED_PARAMS:
        files[param] = KIND_DAILY_ROW
    for param in DAILY_BLOCK_PARAMS:
        files[param] = _kind_of(param, date_major=False)
    demand = hourly_demand(
        enabled=hourly, cloud_layers=cloud_layers, temp_percentiles=temp_percentiles
    )
    if demand is not None:
        for param in demand.date_major:
            files[param] = _kind_of(param, date_major=True)
        for param in demand.point_major:
            files[param] = _kind_of(param, date_major=False)
    return files


def _kind_of(param: str, *, date_major: bool) -> str:
    """Classify a demanded ``param`` into a byte kind.

    ``zprfr0hs`` is its own kind (row-addressed over the zero-degree window,
    ADR-0008); a date-major group member is a horizon prefix/row window; a daily
    required file is a daily row read; everything else is a point-major block.
    """
    if param == HOURLY_ZERO_DEGREE:
        return KIND_ZERO_DEGREE
    if param in DAILY_REQUIRED_PARAMS:
        return KIND_DAILY_ROW
    if date_major:
        return KIND_HOURLY_DATE_MAJOR
    return KIND_POINT_MAJOR


def _kind_bytes(kind: str, *, horizon_days: int) -> int:
    """Table bytes for a file ``kind``; date-major scales with the horizon."""
    if kind == KIND_HOURLY_DATE_MAJOR:
        hours = _horizon_hours(horizon_days)
        return round(
            _KIND_BYTES[KIND_HOURLY_DATE_MAJOR] * hours / _DATE_MAJOR_ANCHOR_HOURS
        )
    return _KIND_BYTES[kind]


def estimate_traffic(
    *,
    hourly: bool,
    horizon_days: int,
    cloud_layers: bool = False,
    temp_percentiles: bool = False,
    measured_bytes: Mapping[str, int] | None = None,
) -> TrafficEstimate:
    """Estimate the traffic a chosen options combination costs (issue #145).

    Maps the options to the set of demanded files (the demand registry), then to
    bytes: the last **measured** fetch of a file already active (from the store's
    provenance, ADR-0008 §5) beats the table estimate for its kind, so an
    instance quotes its own numbers as they warm up. Date-major files scale with
    the horizon and, past the row-addressing cap, become an unbounded prefix
    with no fixed number (:data:`FileEstimate.unbounded`).
    """
    measured = measured_bytes or {}
    fits = _fits_row_addressing(horizon_days)
    files: list[FileEstimate] = []
    for param, kind in _demanded_files(
        hourly=hourly, cloud_layers=cloud_layers, temp_percentiles=temp_percentiles
    ).items():
        if param in measured:
            # A measured fetch is a real number for the file's actual horizon, so
            # it is never treated as unbounded even past the cap.
            files.append(FileEstimate(param, kind, measured[param], measured=True))
            continue
        if kind == KIND_HOURLY_DATE_MAJOR and not fits:
            files.append(
                FileEstimate(param, kind, 0, measured=False, unbounded=True)
            )
            continue
        files.append(
            FileEstimate(
                param,
                kind,
                _kind_bytes(kind, horizon_days=horizon_days),
                measured=False,
            )
        )
    files.sort(key=lambda f: f.param)
    return TrafficEstimate(files=tuple(files))
