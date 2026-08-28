"""Data update coordinators for MeteoSwiss Weather.

Two coordinators back a config entry (ADR-0002):

- :class:`StationCoordinator` polls the configured SwissMetNet station's
  10-minute ``now`` file, revalidating it conditionally so an unchanged file
  costs a single 304.
- :class:`ForecastCoordinator` checks the newest local-forecast run once an
  hour and only downloads the (small) daily parameter files when the run
  stamp actually changed, so a quiet hour costs one small STAC request.

The hourly forecast is the whole traffic budget (~30 MB per file), so it is no
longer fetched from the coordinator's eager path. It hangs off a lazy
:class:`HourlyForecastProvider` that ``weather.async_forecast_hourly`` awaits:
Home Assistant only calls that method while a card or automation subscribes, or
on a ``weather.get_forecasts`` service call, so an instance nobody looks at pays
nothing (ADR-0002 revision 2, issue #54). The coordinator keeps tracking the run
stamp; the weather entity turns a run change into an ``async_update_listeners``
push, which downloads only when someone is actually listening.

Everything upstream-specific lives in the pure ``ogd`` client (ADR-0001);
the coordinators only translate its :class:`OgdError` into ``UpdateFailed``
and hand CSV parsing to the executor via the backend.

Exponential backoff for transient :class:`~ogd.OgdConnectionError` is built
into :class:`~homeassistant.helpers.update_coordinator.DataUpdateCoordinator`
(it marks the update as failed and HA's listener machinery reschedules it with
back-off automatically). No second layer is added here.

A structural :class:`~ogd.OgdParseError` (upstream changed its file layout)
is different: it will recur on every poll until the integration is updated.
The coordinators therefore post a HA repair issue on the first occurrence and
clear it as soon as parsing succeeds again.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
    FORECAST_CHECK_INTERVAL,
    HOURLY_FAR_MAX_AGE,
    HOURLY_FAR_RUN_HOURS,
    HOURLY_NEAR_HORIZON_DAYS,
    HOURLY_NEAR_MAX_AGE,
    HOURLY_NEAR_RUN_HOURS,
    POLLEN_UPDATE_INTERVAL,
    STATION_UPDATE_INTERVAL,
)
from .ogd import (
    CachedResponse,
    DailyForecast,
    ForecastBackend,
    ForecastPoint,
    HourlyForecast,
    Observation,
    OgdConnectionError,
    OgdParseError,
    PollenObservation,
    fetch_current,
    fetch_pollen_current,
    latest_run,
)
from .ogd.const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    HOURLY_POINT_MAJOR_PARAMS,
    hourly_date_major_params,
)

# Issue IDs used in the HA repair-issue registry.
_ISSUE_STATION_PARSE = "parse_error_station"
_ISSUE_FORECAST_PARSE = "parse_error_forecast"
_ISSUE_POLLEN_PARSE = "parse_error_pollen"

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForecastData:
    """The forecast coordinator's payload: the daily forecast.

    The hourly forecast is not carried here — it is fetched lazily through
    :class:`HourlyForecastProvider` only when something asks for it (issue #54).
    """

    daily: list[DailyForecast]


def _tier_due(
    *,
    run: datetime,
    landing_hours: frozenset[int],
    max_age: timedelta,
    last_run: datetime | None,
    last_fetch: datetime | None,
    now: datetime,
) -> bool:
    """Whether a tier is due to refresh (ADR-0002 revision 2, issue #68).

    A pure decision so it can be exhaustively unit-tested without a fetch. A
    tier is due when it has never been fetched, when its cached data is older
    than the tier's staleness fallback (``max_age``), or when a **new** run has
    landed and that run's UTC hour is one of the tier's model-cycle hours. A
    non-landing run, or an unchanged run within the fallback, is not due.
    """
    if last_fetch is None or last_run is None:
        return True
    if now - last_fetch >= max_age:
        return True
    return run != last_run and run.hour in landing_hours


class HourlyForecastProvider:
    """Lazily fetches and caches the bulk hourly forecast (ADR-0002 revision 2).

    The hourly parameter files are ~30 MB each — the whole traffic budget — so
    they are downloaded only when the hourly forecast is actually requested:
    ``weather.async_forecast_hourly`` awaits :meth:`async_get_hourly`, which
    Home Assistant calls while a card or automation subscribes or on a
    ``weather.get_forecasts`` service call. No caller means no download.

    The set is fetched in three independently scheduled groups tied to the
    measured model run rhythm (docs/ogd.md, "Change rhythm across runs";
    issue #68), instead of the flat 3 h floor B14a used:

    - the **near tier** — the date-major temperature prefix up to the end of
      tomorrow — refreshes at the ICON-CH1 landing hours or after 3 h;
    - the **far tier** — the temperature out to the configured horizon —
      refreshes at the ICON-CH2 landing hours or after 6 h; a far fetch spans
      the near window too, so it doubles as a near refresh;
    - the **point-major group** (precipitation, symbol, wind, gust, direction)
      refreshes with every new run — each file's point block is ~5 KB.

    The three groups are merged by hour into one forecast, cached until the next
    tier is due. A :class:`asyncio.Lock` serialises concurrent callers so a card
    and a service call arriving together still download the set once.

    Errors never propagate out of the forecast method: a structural
    :class:`~ogd.OgdParseError` posts the shared forecast repair issue and a
    transient :class:`~ogd.OgdConnectionError` is logged, both keeping the last
    good data so the entity degrades rather than raises.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        backend: ForecastBackend,
        point: ForecastPoint,
        *,
        enabled: bool,
        horizon_days: int,
        cloud_layers: bool = False,
        temp_percentiles: bool = False,
    ) -> None:
        self._hass = hass
        self._backend = backend
        self._point = point
        self._enabled = enabled
        self._horizon_days = horizon_days
        # The date-major files to fetch on the near/far schedule: always the
        # temperature file, plus the B9 cloud and B11 percentile files only when
        # their option is on (the fetch-set registry, issue #69). Nothing extra
        # is fetched when neither is enabled.
        self._date_major_params = hourly_date_major_params(
            cloud_layers=cloud_layers, temp_percentiles=temp_percentiles
        )
        self._lock = asyncio.Lock()
        # The merged forecast last built from the groups below.
        self._hourly: list[HourlyForecast] | None = None
        # The date-major fields by hour (temperature, and — when enabled — cloud
        # layers and temperature percentiles) from the near/far fetches, and the
        # point-major fields (precip, symbol, wind, gust, bearing) by hour.
        self._date_major: dict[datetime, HourlyForecast] = {}
        self._point_major: dict[datetime, HourlyForecast] = {}
        # Per-group bookkeeping: the run each was last fetched at and when.
        self._near_run: datetime | None = None
        self._near_fetch: datetime | None = None
        self._far_run: datetime | None = None
        self._far_fetch: datetime | None = None
        self._point_major_run: datetime | None = None
        self._point_major_fetch: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Whether the hourly opt-in is on for this entry."""
        return self._enabled

    @property
    def last_fetch(self) -> datetime | None:
        """When the most recent tier download completed; for diagnostics."""
        stamps = [
            stamp
            for stamp in (self._near_fetch, self._far_fetch, self._point_major_fetch)
            if stamp is not None
        ]
        return max(stamps) if stamps else None

    @property
    def cached_hourly(self) -> list[HourlyForecast] | None:
        """The cached hourly forecast without triggering a fetch.

        The weather ``condition`` reads this so it can sharpen to the current
        hour's symbol *if* the data is already here, without paying the download
        just to compute a condition (issue #54).
        """
        return self._hourly

    async def async_get_hourly(
        self, run: datetime | None
    ) -> list[HourlyForecast] | None:
        """Return the hourly forecast for ``run``, fetching only if due.

        ``run`` is the newest run stamp the coordinator tracked. Each tier is
        refreshed only when :func:`_tier_due` says so; a run that no tier is due
        for serves the cache untouched.
        """
        if not self._enabled or run is None:
            return None
        async with self._lock:
            await self._refresh(run)
            return self._hourly

    async def _refresh(self, run: datetime) -> None:
        """Refresh whichever tiers are due for ``run``; keep last-good on error."""
        now = dt_util.utcnow()
        try:
            changed = await self._refresh_date_major(run, now)
            changed = await self._refresh_point_major(run, now) or changed
        except OgdParseError as err:
            async_create_issue(
                self._hass,
                DOMAIN,
                _ISSUE_FORECAST_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_forecast",
            )
            _LOGGER.warning("hourly forecast parse failed: %s", err)
            return
        except OgdConnectionError as err:
            _LOGGER.warning("hourly forecast fetch failed: %s", err)
            return

        async_delete_issue(self._hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        if changed:
            self._hourly = self._merge()

    async def _refresh_date_major(self, run: datetime, now: datetime) -> bool:
        """Refresh the date-major group via the far then near tiers.

        The date-major group is the temperature file plus, when enabled, the B9
        cloud and B11 percentile files (issue #69) — all fetched together as one
        horizon prefix on this schedule. The far tier spans the near window, so a
        due far fetch supersedes and also satisfies the near tier; only when far
        is not due but near is does the cheaper near prefix run.
        """
        if _tier_due(
            run=run,
            landing_hours=HOURLY_FAR_RUN_HOURS,
            max_age=HOURLY_FAR_MAX_AGE,
            last_run=self._far_run,
            last_fetch=self._far_fetch,
            now=now,
        ):
            far = await self._backend.fetch_hourly(
                self._point,
                horizon_days=self._horizon_days,
                params=self._date_major_params,
            )
            self._date_major = {hour.time: hour for hour in far}
            self._far_run = self._near_run = run
            self._far_fetch = self._near_fetch = now
            return True

        if _tier_due(
            run=run,
            landing_hours=HOURLY_NEAR_RUN_HOURS,
            max_age=HOURLY_NEAR_MAX_AGE,
            last_run=self._near_run,
            last_fetch=self._near_fetch,
            now=now,
        ):
            near = await self._backend.fetch_hourly(
                self._point,
                horizon_days=HOURLY_NEAR_HORIZON_DAYS,
                params=self._date_major_params,
            )
            # Overwrite only the near-window hours; keep the far-window values
            # from the last far fetch (that tier refreshes slower).
            for hour in near:
                self._date_major[hour.time] = hour
            self._near_run = run
            self._near_fetch = now
            return True

        return False

    async def _refresh_point_major(self, run: datetime, now: datetime) -> bool:
        """Refresh the point-major group whenever a new run has landed."""
        if run == self._point_major_run:
            return False
        point_major = await self._backend.fetch_hourly(
            self._point,
            horizon_days=self._horizon_days,
            params=HOURLY_POINT_MAJOR_PARAMS,
        )
        self._point_major = {hour.time: hour for hour in point_major}
        self._point_major_run = run
        self._point_major_fetch = now
        return True

    def _merge(self) -> list[HourlyForecast]:
        """Combine the date-major and point-major groups by hour, sorted.

        The date-major group carries temperature and — when their option is on —
        the cloud layers and temperature percentiles (issue #69); the
        point-major group carries precipitation, symbol, wind and the B7/B8/B10
        additions. Each hour reads its fields from whichever group holds them.
        """
        result: list[HourlyForecast] = []
        for when in sorted(set(self._date_major) | set(self._point_major)):
            dm = self._date_major.get(when)
            block = self._point_major.get(when)
            result.append(
                HourlyForecast(
                    time=when,
                    temperature=dm.temperature if dm else None,
                    precipitation=block.precipitation if block else None,
                    symbol=block.symbol if block else None,
                    wind_speed_kmh=block.wind_speed_kmh if block else None,
                    gust_kmh=block.gust_kmh if block else None,
                    wind_bearing=block.wind_bearing if block else None,
                    precipitation_probability=(
                        block.precipitation_probability if block else None
                    ),
                    zero_degree_level=block.zero_degree_level if block else None,
                    radiation=block.radiation if block else None,
                    cloud_high=dm.cloud_high if dm else None,
                    cloud_mid=dm.cloud_mid if dm else None,
                    cloud_low=dm.cloud_low if dm else None,
                    temperature_p10=dm.temperature_p10 if dm else None,
                    temperature_p90=dm.temperature_p90 if dm else None,
                )
            )
        return result


class StationCoordinator(DataUpdateCoordinator[Observation]):
    """Poll one station's latest 10-minute observation."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        station_abbr: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{station_abbr} observations",
            update_interval=STATION_UPDATE_INTERVAL,
        )
        self._session = session
        self._station_abbr = station_abbr
        # Reused across polls so the station file is revalidated conditionally
        # (If-None-Match / If-Modified-Since); get_text mutates it in place.
        self._cache = CachedResponse(body="")
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None

    async def _async_update_data(self) -> Observation:
        try:
            obs = await fetch_current(
                self._session, self._station_abbr, cache=self._cache
            )
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_STATION_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_station",
            )
            raise UpdateFailed(
                f"station {self._station_abbr} parse failed: {err}"
            ) from err
        except OgdConnectionError as err:
            raise UpdateFailed(
                f"station {self._station_abbr} update failed: {err}"
            ) from err

        async_delete_issue(self.hass, DOMAIN, _ISSUE_STATION_PARSE)
        self.last_success = dt_util.utcnow()
        return obs


class ForecastCoordinator(DataUpdateCoordinator[ForecastData]):
    """Refresh the daily local forecast, skipping unchanged runs (ADR-0002).

    Keeps the daily forecast fresh from the newest complete run and tracks the
    run stamp so the weather entity can push a lazy hourly refresh when it
    changes. The bulk hourly download itself lives in :attr:`hourly_provider`,
    off the coordinator's eager path (issue #54).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        backend: ForecastBackend,
        point: ForecastPoint,
        *,
        hourly_enabled: bool = False,
        hourly_horizon_days: int = DEFAULT_HOURLY_HORIZON_DAYS,
        hourly_cloud_layers: bool = False,
        hourly_temp_percentiles: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"forecast {point.name}",
            update_interval=FORECAST_CHECK_INTERVAL,
        )
        self._session = session
        self._backend = backend
        self._point = point
        # Timestamp of the run the current daily data came from; exposed for
        # diagnostics and used to skip re-downloading an unchanged run. The
        # weather entity also watches it to trigger the lazy hourly refresh.
        self.last_run: datetime | None = None
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None
        # The lazy hourly download hangs here; the coordinator never calls it,
        # the weather entity does when HA asks (ADR-0002 revision 2, issue #54).
        self.hourly_provider = HourlyForecastProvider(
            hass,
            backend,
            point,
            enabled=hourly_enabled,
            horizon_days=hourly_horizon_days,
            cloud_layers=hourly_cloud_layers,
            temp_percentiles=hourly_temp_percentiles,
        )

    async def _async_update_data(self) -> ForecastData:
        try:
            run = await latest_run(
                self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
            )
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_FORECAST_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_forecast",
            )
            raise UpdateFailed(f"forecast run discovery parse failed: {err}") from err
        except OgdConnectionError as err:
            raise UpdateFailed(f"forecast run discovery failed: {err}") from err

        # An unchanged run means the MB-scale daily files would be identical:
        # skip the download entirely and keep serving what we have (ADR-0002).
        if (
            self.last_run is not None
            and run.timestamp == self.last_run
            and self.data is not None
        ):
            daily = self.data.daily
        else:
            try:
                # The backend downloads the small daily files and parses them
                # off the event loop; a future per-point backend swaps in here.
                daily = await self._backend.fetch_daily(self._point)
            except OgdParseError as err:
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    _ISSUE_FORECAST_PARSE,
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="parse_error_forecast",
                )
                raise UpdateFailed(f"daily forecast parse failed: {err}") from err
            except OgdConnectionError as err:
                raise UpdateFailed(f"daily forecast fetch failed: {err}") from err
            self.last_run = run.timestamp

        async_delete_issue(self.hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        self.last_success = dt_util.utcnow()
        return ForecastData(daily=daily)


class PollenCoordinator(DataUpdateCoordinator[PollenObservation]):
    """Poll one pollen station's latest hourly observation (ADR-0005).

    Fetches ``_h_now.csv`` at most once per hour, conditional on the ETag so an
    unchanged file costs only a 304. Raises a HA repair issue on a structural
    parse failure and clears it as soon as parsing succeeds again.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        station_abbr: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{station_abbr} pollen",
            update_interval=POLLEN_UPDATE_INTERVAL,
        )
        self._session = session
        self._station_abbr = station_abbr
        self._cache = CachedResponse(body="")
        self.last_success: datetime | None = None

    async def _async_update_data(self) -> PollenObservation:
        try:
            obs = await fetch_pollen_current(
                self._session, self._station_abbr, cache=self._cache
            )
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_POLLEN_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_pollen",
            )
            raise UpdateFailed(
                f"pollen {self._station_abbr} parse failed: {err}"
            ) from err
        except OgdConnectionError as err:
            raise UpdateFailed(
                f"pollen {self._station_abbr} update failed: {err}"
            ) from err

        async_delete_issue(self.hass, DOMAIN, _ISSUE_POLLEN_PARSE)
        self.last_success = dt_util.utcnow()
        return obs
