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

from dataclasses import dataclass

from .ogd.const import HOURLY_POINT_MAJOR_PARAMS, hourly_date_major_params


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
