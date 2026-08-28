"""Weather platform: one entity per config entry (issue #10).

The current conditions come from the SwissMetNet station observation
(:class:`StationCoordinator`); the daily forecast and the current ``condition``
come from the local-forecast daily files (:class:`ForecastCoordinator`). The
station carries no weather symbol, so ``condition`` is read from today's daily
symbol (``jp2000d0``) and turned into a Home Assistant condition, with the
day/night variant chosen from ``sun.sun`` (ADR-0001/0002).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from homeassistant.components.sun import STATE_ABOVE_HORIZON
from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MeteoSwissConfigEntry
from .const import ATTRIBUTION, CONF_HOURLY_FORECAST, DOMAIN
from .coordinator import (
    ForecastCoordinator,
    PrecipStationCoordinator,
    StationCoordinator,
)
from .ogd import DailyForecast, ForecastPoint, HourlyForecast
from .symbols import condition_for_symbol

# Home Assistant's well-known sun entity; its state is the day/night flag.
_SUN_ENTITY_ID = "sun.sun"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the single weather entity for a config entry."""
    async_add_entities([MeteoSwissWeather(entry)])


class MeteoSwissWeather(CoordinatorEntity[StationCoordinator], WeatherEntity):
    """Current conditions and 9-day daily forecast for one forecast point."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_attribution = ATTRIBUTION

    # Native units the OGD files report in (docs/ogd.md §A1/§E4); Home Assistant
    # converts to the user's configured units from these.
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS

    def __init__(self, entry: MeteoSwissConfigEntry) -> None:
        runtime = entry.runtime_data
        # The station coordinator backs CoordinatorEntity; the forecast
        # coordinator is subscribed separately in async_added_to_hass so a
        # forecast refresh also writes state.
        super().__init__(runtime.station_coordinator)
        self._forecast_coordinator: ForecastCoordinator = runtime.forecast_coordinator
        # Optional precipitation-only station (ADR-0006, #70): when set, the
        # current precipitation attribute reads from it instead of the main
        # station. ``None`` unless the user picked one.
        self._precip_coordinator: PrecipStationCoordinator | None = (
            runtime.precip_coordinator
        )
        self._precip_station_name: str = runtime.precip_station_name
        point: ForecastPoint = runtime.point

        # Hourly is an opt-in feature (ADR-0002): advertise FORECAST_HOURLY only
        # when the option is on, so HA never asks for hourly data we do not fetch.
        # The entry reloads on an options change, so this is read once at build.
        features = WeatherEntityFeature.FORECAST_DAILY
        self._hourly_enabled = bool(entry.options.get(CONF_HOURLY_FORECAST, False))
        if self._hourly_enabled:
            features |= WeatherEntityFeature.FORECAST_HOURLY
        self._attr_supported_features = features

        # Whether a card or automation currently subscribes to the hourly
        # forecast, tracked via the subscription hooks below. It gates the lazy
        # download: a new run with no subscriber must not fetch (issue #54).
        self._hourly_subscribed = False
        # The run stamp last turned into an hourly listener push, so a run that
        # has not changed does not re-trigger a fetch.
        self._last_hourly_run: datetime | None = None

        unique_id = f"{point.point_type_id}-{point.point_id}"
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            name=point.name,
            manufacturer="MeteoSwiss",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://opendatadocs.meteoswiss.ch",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the forecast coordinator in addition to the station one."""
        await super().async_added_to_hass()
        # Seed the run watermark with the run already loaded so the first
        # coordinator tick does not look like a change.
        self._last_hourly_run = self._forecast_coordinator.last_run
        self.async_on_remove(
            self._forecast_coordinator.async_add_listener(self._handle_forecast_update)
        )
        # A precipitation-station refresh must re-write the current precipitation
        # attribute too (ADR-0006). Its failure never affects availability —
        # precipitation is one opt-in attribute, not a core condition.
        if self._precip_coordinator is not None:
            self.async_on_remove(
                self._precip_coordinator.async_add_listener(
                    self._handle_coordinator_update
                )
            )

    @property
    def available(self) -> bool:
        """Available only while both coordinators are succeeding."""
        return (
            self.coordinator.last_update_success
            and self._forecast_coordinator.last_update_success
        )

    # -- current conditions (station observation) ---------------------------

    @property
    def native_temperature(self) -> float | None:
        obs = self.coordinator.data
        return obs.temperature if obs else None

    @property
    def humidity(self) -> float | None:
        obs = self.coordinator.data
        return obs.humidity if obs else None

    @property
    def native_dew_point(self) -> float | None:
        obs = self.coordinator.data
        return obs.dew_point if obs else None

    @property
    def native_pressure(self) -> float | None:
        # QFF: pressure reduced to sea level, the value HA expects (docs/ogd.md).
        obs = self.coordinator.data
        return obs.pressure_qff if obs else None

    @property
    def native_wind_speed(self) -> float | None:
        obs = self.coordinator.data
        return obs.wind_speed_kmh if obs else None

    @property
    def wind_bearing(self) -> float | None:
        obs = self.coordinator.data
        return obs.wind_bearing if obs else None

    @property
    def native_wind_gust_speed(self) -> float | None:
        obs = self.coordinator.data
        return obs.gust_kmh if obs else None

    @property
    def extra_state_attributes(self) -> dict[str, float | str | None]:
        """Current precipitation (mm, 10-minute sum), sourced per ADR-0006.

        Home Assistant's ``WeatherEntity`` has no first-class current
        precipitation, so it rides here. It comes from the optional
        precipitation station when one is configured — with ``precipitation_station``
        naming it — and from the main station otherwise.
        """
        if self._precip_coordinator is not None:
            obs = self._precip_coordinator.data
            attrs: dict[str, float | str | None] = {
                "current_precipitation": obs.precipitation_10min if obs else None,
                "precipitation_station": self._precip_station_name,
            }
            return attrs
        obs = self.coordinator.data
        return {"current_precipitation": obs.precipitation_10min if obs else None}

    # -- condition (today's daily symbol) -----------------------------------

    @property
    def condition(self) -> str | None:
        """HA condition, preferring the current hour's symbol when available.

        The station reports no weather symbol. When the hourly forecast is on,
        the current hour's symbol (``jww003i0``, which already carries the
        day/night variant) gives the sharpest condition; otherwise it falls
        back to today's daily symbol (``jp2000d0``) with the day/night variant
        chosen from the sun's position (ADR-0002).
        """
        hourly_symbol = self._current_hour_symbol()
        if hourly_symbol is not None:
            return condition_for_symbol(hourly_symbol)

        today = self._today_forecast()
        if today is None:
            return None
        return condition_for_symbol(today.symbol, is_daytime=self._is_daytime())

    def _current_hour_symbol(self) -> int | None:
        """The hourly symbol for the current UTC hour, if hourly data is cached.

        Reads only what the lazy provider already downloaded — computing the
        condition never triggers the bulk hourly fetch (issue #54). With no
        hourly subscriber the cache is empty and the condition falls back to the
        daily symbol.
        """
        hourly = self._forecast_coordinator.hourly_provider.cached_hourly
        if not hourly:
            return None
        this_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        for hour in hourly:
            if hour.time == this_hour:
                return hour.symbol
        return None

    def _today_forecast(self) -> DailyForecast | None:
        """The daily forecast entry for today, or the earliest one available."""
        data = self._forecast_coordinator.data
        if data is None or not data.daily:
            return None
        daily = data.daily
        today = dt_util.now().date()
        for day in daily:
            if day.date == today:
                return day
        # The forecast normally starts today; fall back to its first (earliest)
        # entry if today's row is missing (e.g. just before a run rolls over).
        return daily[0]

    def _is_daytime(self) -> bool | None:
        """Whether the sun is up, from ``sun.sun`` (``None`` if unknown)."""
        sun = self.hass.states.get(_SUN_ENTITY_ID)
        if sun is None:
            return None
        return sun.state == STATE_ABOVE_HORIZON

    # -- daily forecast -----------------------------------------------------

    async def async_forecast_daily(self) -> list[Forecast] | None:
        """Return the 9-day daily forecast as HA ``Forecast`` dicts."""
        data = self._forecast_coordinator.data
        if data is None or not data.daily:
            return None
        return [self._as_daily_forecast(day) for day in data.daily]

    @staticmethod
    def _as_daily_forecast(day: DailyForecast) -> Forecast:
        # A daily summary uses the daytime symbol variant.
        forecast: Forecast = {
            "datetime": day.date.isoformat(),
            "condition": condition_for_symbol(day.symbol, is_daytime=True),
            "native_temperature": day.temp_max,
            "native_templow": day.temp_min,
            "native_precipitation": day.precipitation,
        }
        if day.precipitation_probability is not None:
            forecast["precipitation_probability"] = round(day.precipitation_probability)
        if day.native_wind_speed is not None:
            forecast["native_wind_speed"] = day.native_wind_speed
        if day.native_wind_gust_speed is not None:
            forecast["native_wind_gust_speed"] = day.native_wind_gust_speed
        if day.wind_bearing is not None:
            forecast["wind_bearing"] = day.wind_bearing
        return forecast

    # -- hourly forecast (opt-in, ADR-0002) ---------------------------------

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """Return the hourly forecast as HA ``Forecast`` dicts, or ``None``.

        Only reachable when ``FORECAST_HOURLY`` is advertised, i.e. the option
        is on. This is the lazy download point (issue #54): HA calls it while a
        card or automation subscribes or on a ``weather.get_forecasts`` call, and
        the provider fetches the bulk hourly files only when the run changed and
        its cache is stale. ``None`` until the first fetch completes.
        """
        hourly = await self._forecast_coordinator.hourly_provider.async_get_hourly(
            self._forecast_coordinator.last_run
        )
        if not hourly:
            return None
        return [self._as_hourly_forecast(hour) for hour in hourly]

    @staticmethod
    def _as_hourly_forecast(hour: HourlyForecast) -> Forecast:
        # The hourly symbol (jww003i0) already encodes the day/night variant.
        forecast: Forecast = {
            "datetime": hour.time.isoformat(),
            "condition": condition_for_symbol(hour.symbol),
            "native_temperature": hour.temperature,
            "native_precipitation": hour.precipitation,
            "native_wind_speed": hour.wind_speed_kmh,
            "native_wind_gust_speed": hour.gust_kmh,
            "wind_bearing": hour.wind_bearing,
        }
        # B7: precipitation probability is a standard HA forecast field.
        if hour.precipitation_probability is not None:
            forecast["precipitation_probability"] = round(
                hour.precipitation_probability
            )
        # B9 (issue #69): cloud_coverage is HA's single number — documented as
        # the maximum of the three layers, since a card shows one figure. The
        # three layers ride along as extra hourly attributes for anyone who wants
        # the breakdown. Only present when the cloud-layer option is on (the
        # files are otherwise not fetched).
        layers = [
            layer
            for layer in (hour.cloud_high, hour.cloud_mid, hour.cloud_low)
            if layer is not None
        ]
        if layers:
            forecast["cloud_coverage"] = round(max(layers))
            forecast["cloud_coverage_high"] = hour.cloud_high
            forecast["cloud_coverage_mid"] = hour.cloud_mid
            forecast["cloud_coverage_low"] = hour.cloud_low
        # B11 (issue #69): the temperature uncertainty band, as extra attributes
        # next to the median temperature. Only present when the percentile option
        # is on. These are custom keys, not the standard native_temperature that
        # Home Assistant unit-converts, so they carry the native °C value and are
        # named without the ``native_`` prefix to avoid implying a conversion HA
        # does not perform on unknown keys.
        if hour.temperature_p10 is not None:
            forecast["temperature_p10"] = hour.temperature_p10
        if hour.temperature_p90 is not None:
            forecast["temperature_p90"] = hour.temperature_p90
        return forecast

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on a refresh of the station coordinator."""
        self.async_write_ha_state()

    @callback
    def _handle_forecast_update(self) -> None:
        """Write state on a forecast refresh and, on a new run, refresh hourly.

        The coordinator only tracks the run stamp; the bulk hourly download is
        lazy. When the run changes we push the ``hourly`` forecast to its
        listeners, which downloads *only* if a card or automation is subscribed
        (ADR-0002 revision 2, issue #54). With no subscriber the run change is
        logged and nothing is fetched.
        """
        self.async_write_ha_state()
        if not self._hourly_enabled:
            return
        run = self._forecast_coordinator.last_run
        if run is None or run == self._last_hourly_run:
            return
        self._last_hourly_run = run
        if not self._hourly_subscribed:
            _LOGGER.debug(
                "hourly forecast: run %s changed but nothing subscribes; "
                "skipping the bulk download",
                run.isoformat(),
            )
            return
        # A subscriber exists: HA calls async_forecast_hourly, which pulls the
        # new run lazily through the provider.
        self.hass.async_create_task(self.async_update_listeners(["hourly"]))

    @callback
    def _async_subscription_started(
        self, forecast_type: Literal["daily", "hourly", "twice_daily"]
    ) -> None:
        """Track the first hourly subscriber (a card open, an automation)."""
        if forecast_type == "hourly":
            self._hourly_subscribed = True

    @callback
    def _async_subscription_ended(
        self, forecast_type: Literal["daily", "hourly", "twice_daily"]
    ) -> None:
        """Track the last hourly subscriber going away."""
        if forecast_type == "hourly":
            self._hourly_subscribed = False
