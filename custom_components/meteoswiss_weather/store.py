"""The run-scoped forecast store (ADR-0008).

One store per config entry holds, per upstream parameter, the configured
point's series ``{hour → value}`` together with where it came from. Every
writer — the daily refresh and the hourly provider today — puts what it
fetched here, and entities that show "the current hour" read it from here, so
no entity depends on *which* path happened to download a file.

Deliberately free of Home Assistant imports: it is plain bookkeeping and is
unit-tested as such.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a stored series came from."""

    run: datetime
    fetched_at: datetime
    # Which path wrote it ("daily" / "hourly"). The escalation level, bytes and
    # request count join here with the fetch ladder (ADR-0008 section 4).
    source: str


@dataclass(frozen=True, slots=True)
class Series:
    """One parameter's values for the configured point, keyed by UTC hour."""

    values: Mapping[datetime, float | int]
    provenance: Provenance


class ForecastStore:
    """Per-parameter series of the newest run, keeping the last good one."""

    def __init__(self) -> None:
        self._series: dict[str, Series] = {}

    def put(
        self,
        param: str,
        values: Mapping[datetime, float | int],
        *,
        run: datetime,
        fetched_at: datetime,
        source: str,
    ) -> bool:
        """Store ``values`` for ``param``; return whether the store changed.

        Last-good retention (ADR-0008 section 1): an empty series never
        replaces a stored one — a refresh that could not deliver keeps the
        previous run, which still covers the coming hours. A series from an
        older run than the stored one is ignored. A series from the *same* run
        is merged, because the two paths cover different windows of one run
        (the daily block is the whole run, the hourly fetch is trimmed to the
        horizon).
        """
        if not values:
            return False
        current = self._series.get(param)
        merged: Mapping[datetime, float | int] = values
        if current is not None:
            if run < current.provenance.run:
                return False
            if run == current.provenance.run:
                merged = {**current.values, **values}
                if merged == current.values:
                    return False
        self._series[param] = Series(
            values=dict(merged),
            provenance=Provenance(run=run, fetched_at=fetched_at, source=source),
        )
        return True

    def get(self, param: str) -> Series | None:
        """The stored series for ``param``, or ``None``."""
        return self._series.get(param)

    def value_at(self, param: str, when: datetime) -> float | int | None:
        """The value of ``param`` at the UTC hour ``when``, or ``None``."""
        series = self._series.get(param)
        if series is None:
            return None
        return series.values.get(when)

    def is_stale(self, param: str, run: datetime | None) -> bool:
        """Whether ``param`` is missing or held over from an earlier run."""
        series = self._series.get(param)
        if series is None or run is None:
            return True
        return series.provenance.run < run

    def as_diagnostics(self, run: datetime | None) -> dict[str, Any]:
        """A JSON-friendly summary per parameter, for the diagnostics dump."""
        return {
            param: {
                "run": series.provenance.run.isoformat(),
                "fetched_at": series.provenance.fetched_at.isoformat(),
                "source": series.provenance.source,
                "hours": len(series.values),
                "stale": self.is_stale(param, run),
            }
            for param, series in sorted(self._series.items())
        }
