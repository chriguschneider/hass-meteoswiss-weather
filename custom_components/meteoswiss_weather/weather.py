"""Weather platform: one entity per config entry (issue #10).

The current conditions come from the SwissMetNet station observation
(:class:`StationCoordinator`); the daily forecast and the current ``condition``
come from the local-forecast daily files (:class:`ForecastCoordinator`). The
station carries no weather symbol, so ``condition`` is read from today's daily
symbol (``jp2000d0``) and turned into a Home Assistant condition, with the
day/night variant chosen from ``sun.sun`` (ADR-0001/0002).
"""

from __future__ import annotations

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
from .coordinator import ForecastCoordinator, StationCoordinator
from .ogd import DailyForecast, ForecastPoint, HourlyForecast
from .symbols import condition_for_symbol

# Home Assistant's well-known sun entity; its state is the day/night flag.
_SUN_ENTITY_ID = "sun.sun"


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
        point: ForecastPoint = runtime.point

        # Hourly is an opt-in feature (ADR-0002): advertise FORECAST_HOURLY only
        # when the option is on, so HA never asks for hourly data we do not fetch.
        # The entry reloads on an options change, so this is read once at build.
        features = WeatherEntityFeature.FORECAST_DAILY
        if entry.options.get(CONF_HOURLY_FORECAST, False):
            features |= WeatherEntityFeature.FORECAST_HOURLY
        self._attr_supported_features = features

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
        self.async_on_remove(
            self._forecast_coordinator.async_add_listener(
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
        """The hourly symbol for the current UTC hour, if hourly data is here."""
        data = self._forecast_coordinator.data
        if data is None or not data.hourly:
            return None
        this_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        for hour in data.hourly:
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
        return forecast

    # -- hourly forecast (opt-in, ADR-0002) ---------------------------------

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """Return the hourly forecast as HA ``Forecast`` dicts, or ``None``.

        Only reachable when ``FORECAST_HOURLY`` is advertised, i.e. the option
        is on; ``None`` until the first throttled hourly download completes.
        """
        data = self._forecast_coordinator.data
        if data is None or not data.hourly:
            return None
        return [self._as_hourly_forecast(hour) for hour in data.hourly]

    @staticmethod
    def _as_hourly_forecast(hour: HourlyForecast) -> Forecast:
        # The hourly symbol (jww003i0) already encodes the day/night variant.
        return {
            "datetime": hour.time.isoformat(),
            "condition": condition_for_symbol(hour.symbol),
            "native_temperature": hour.temperature,
            "native_precipitation": hour.precipitation,
            "native_wind_speed": hour.wind_speed_kmh,
            "native_wind_gust_speed": hour.gust_kmh,
            "wind_bearing": hour.wind_bearing,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on a refresh of either coordinator."""
        self.async_write_ha_state()
