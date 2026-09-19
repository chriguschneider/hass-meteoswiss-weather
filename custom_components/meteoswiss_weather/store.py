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
    # Which path wrote it ("daily" / "hourly").
    source: str
    # True when this run's values were not fetched but a cheap canary read proved
    # them equal to the series already stored, so it was re-stamped to the run
    # rather than downloaded again (ADR-0008 section 3, issue #125). A confirmed
    # series is as current as a fetched one — it just cost a few KB, not a fetch.
    confirmed: bool = False
    # Escalation ladder level (0–4), HTTP requests and bytes of the fetch that
    # produced this series (ADR-0008 section 4/5). ``None`` when the series was
    # written by a path that did not pass metadata (e.g. a canary confirmation).
    level: int | None = None
    requests: int | None = None
    bytes_fetched: int | None = None
    # Layout of the upstream file as last classified (e.g. "date_major",
    # "point_major_type", "point_major_id", "fallback"). ``None`` when unknown.
    layout: str | None = None


@dataclass(frozen=True, slots=True)
class Series:
    """One parameter's values for the configured point, keyed by UTC hour."""

    values: Mapping[datetime, float | int]
    provenance: Provenance


class ForecastStore:
    """Per-parameter series of the newest run, keeping the last good one."""

    def __init__(self) -> None:
        self._series: dict[str, Series] = {}
        # Consecutive escalated (level >= 3) fetch count per parameter (ADR-0008 §5).
        # Reset to 0 on a successful non-escalated fetch. Never decremented by
        # ``confirm()`` — a canary confirmation is not a new fetch.
        self._escalation_streaks: dict[str, int] = {}

    def put(
        self,
        param: str,
        values: Mapping[datetime, float | int],
        *,
        run: datetime,
        fetched_at: datetime,
        source: str,
        level: int | None = None,
        requests: int | None = None,
        bytes_fetched: int | None = None,
        layout: str | None = None,
    ) -> bool:
        """Store ``values`` for ``param``; return whether the store changed.

        Last-good retention (ADR-0008 section 1): an empty series never
        replaces a stored one — a refresh that could not deliver keeps the
        previous run, which still covers the coming hours. A series from an
        older run than the stored one is ignored. A series from the *same* run
        is merged, because the two paths cover different windows of one run
        (the daily block is the whole run, the hourly fetch is trimmed to the
        horizon).

        When ``level`` is given it also updates the escalation streak for
        ``param`` (ADR-0008 section 5).
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
            provenance=Provenance(
                run=run,
                fetched_at=fetched_at,
                source=source,
                level=level,
                requests=requests,
                bytes_fetched=bytes_fetched,
                layout=layout,
            ),
        )
        if level is not None:
            if level >= 3:
                self._escalation_streaks[param] = (
                    self._escalation_streaks.get(param, 0) + 1
                )
            else:
                self._escalation_streaks[param] = 0
        return True

    def confirm(
        self, param: str, *, run: datetime, fetched_at: datetime
    ) -> bool:
        """Re-stamp ``param``'s series to ``run`` after a canary proved it unchanged.

        The escalating fetch is skipped when a cheap canary read shows the new
        run carries the same values as the one already stored (ADR-0008 section
        3, issue #125): the values are kept as-is but their provenance is moved
        forward to ``run`` and flagged ``confirmed``, so diagnostics show the
        parameter as current for the run rather than "stale, held over from an
        earlier one". The fetch metadata (level, requests, bytes, layout) is
        carried over from the previous provenance unchanged — a confirmation is
        not a new fetch. A no-op when nothing is stored or the stored series is
        not older than ``run`` (there is nothing to move forward).
        """
        current = self._series.get(param)
        if current is None or current.provenance.run >= run:
            return False
        prev = current.provenance
        self._series[param] = Series(
            values=current.values,
            provenance=Provenance(
                run=run,
                fetched_at=fetched_at,
                source=prev.source,
                confirmed=True,
                level=prev.level,
                requests=prev.requests,
                bytes_fetched=prev.bytes_fetched,
                layout=prev.layout,
            ),
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

    def escalation_streak(self, param: str) -> int:
        """Consecutive L3+ fetch count for ``param`` (0 when not tracked or reset)."""
        return self._escalation_streaks.get(param, 0)

    def as_diagnostics(self, run: datetime | None) -> dict[str, Any]:
        """A JSON-friendly summary per parameter, for the diagnostics dump."""
        result: dict[str, Any] = {}
        for param, series in sorted(self._series.items()):
            prov = series.provenance
            entry: dict[str, Any] = {
                "run": prov.run.isoformat(),
                "fetched_at": prov.fetched_at.isoformat(),
                "source": prov.source,
                "confirmed": prov.confirmed,
                "hours": len(series.values),
                "stale": self.is_stale(param, run),
            }
            if prov.level is not None:
                entry["level"] = prov.level
            if prov.requests is not None:
                entry["requests"] = prov.requests
            if prov.bytes_fetched is not None:
                entry["bytes"] = prov.bytes_fetched
            if prov.layout is not None:
                entry["layout"] = prov.layout
            result[param] = entry
        return result
