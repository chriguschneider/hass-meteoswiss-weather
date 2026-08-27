"""Prove the ForecastBackend seam (issue #18, ADR-0002).

Sets up the integration with an in-memory FakeBackend instead of BulkCsvBackend
and asserts the weather entity and forecast services work unchanged. This is the
proof-of-concept for the per-point OGC Features API announced by MeteoSwiss for
end-2026: swapping backends is a contained change to __init__._backend_factory
plus a new ogd/ module, leaving the coordinator and entities untouched.

The test deliberately mocks only the station observation and the STAC run-stamp
endpoint. By not registering any forecast CSV URLs, any call to BulkCsvBackend
would raise a connection error and fail the setup — which proves the FakeBackend
is actually being used instead.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.sun import STATE_ABOVE_HORIZON
from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_HOURLY_FORECAST,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.ogd import (
    DailyForecast,
    ForecastBackend,
    ForecastPoint,
    HourlyForecast,
)
from custom_components.meteoswiss_weather.ogd.const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    stac_items_url,
    station_now_url,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_STATION_ABBR = "BER"
_ENTITY_ID = "weather.koniz"
_RUN_TS = "202608270200"
_ASSET_BASE = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/20260827-ch"
)


# ---------------------------------------------------------------------------
# FakeBackend: an in-memory ForecastBackend implementation
# ---------------------------------------------------------------------------


class FakeBackend:
    """In-memory ForecastBackend; returns fixed data with no network I/O.

    Three daily entries and four hourly entries, distinct enough from the
    CSV fixture data to verify the coordinator is using this backend rather
    than the real BulkCsvBackend.
    """

    DAILY: list[DailyForecast] = [
        DailyForecast(
            date=date(2026, 8, 27),
            temp_max=25.0,
            temp_min=15.0,
            precipitation=0.0,
            symbol=2,  # mostly sunny, some clouds → partlycloudy
        ),
        DailyForecast(
            date=date(2026, 8, 28),
            temp_max=20.0,
            temp_min=12.0,
            precipitation=1.5,
            symbol=6,  # sunny intervals, isolated showers → rainy
        ),
        DailyForecast(
            date=date(2026, 8, 29),
            temp_max=18.0,
            temp_min=11.0,
            precipitation=3.0,
            symbol=13,  # sunny intervals, possible thunderstorms → lightning-rainy
        ),
    ]
    HOURLY: list[HourlyForecast] = [
        HourlyForecast(
            time=datetime(2026, 8, 27, h, tzinfo=UTC),
            temperature=20.0,
            precipitation=0.0,
            symbol=1,
            wind_speed_kmh=10.0,
            gust_kmh=15.0,
            wind_bearing=270,
        )
        for h in range(4)
    ]

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]:
        return self.DAILY

    async def fetch_hourly(self, point: ForecastPoint) -> list[HourlyForecast]:
        return self.HOURLY


# FakeBackend satisfies the ForecastBackend protocol.
_: ForecastBackend = FakeBackend()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _entry_data() -> dict:
    return {
        CONF_POINT_ID: 309800,
        CONF_POINT_TYPE_ID: 2,
        CONF_POSTAL_CODE: "3098",
        CONF_POINT_NAME: "Köniz",
        CONF_STATION_ABBR: _STATION_ABBR,
        CONF_STATION_NAME: "Bern / Zollikofen",
    }


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Config entry wired to Köniz, matching the shape the flow produces."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(),
        title="Köniz",
        unique_id="2-309800",
    )


@pytest.fixture
def hourly_config_entry() -> MockConfigEntry:
    """Config entry with the opt-in hourly forecast enabled."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(),
        options={CONF_HOURLY_FORECAST: True},
        title="Köniz",
        unique_id="2-309800",
    )


@pytest.fixture
def mock_station_and_stac(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Minimal mock: only the station observation and the STAC run-stamp.

    Forecast CSV file URLs are intentionally *not* registered here. If the
    coordinator tried to call BulkCsvBackend.fetch_daily it would hit an
    unregistered URL and raise an OgdConnectionError, causing setup to fail.
    A successful setup therefore proves FakeBackend was used end-to-end.
    """
    aioclient_mock.get(
        station_now_url(_STATION_ABBR),
        content=(_FIXTURES / "ogd-smn_ber_t_now.csv").read_bytes(),
    )
    aioclient_mock.get(
        stac_items_url(COLLECTION_FORECAST),
        content=(_FIXTURES / "ogd-local-forecasting_items.json").read_bytes(),
    )
    return aioclient_mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _forecast_csv_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Count calls to any daily or hourly forecast CSV URL."""
    all_params = set(DAILY_REQUIRED_PARAMS) | set(HOURLY_REQUIRED_PARAMS)
    suffixes = tuple(f"{_RUN_TS}.{p}.csv" for p in all_params)
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffixes)
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.states.async_set("sun.sun", STATE_ABOVE_HORIZON)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fake_backend_is_used_no_csv_downloads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_station_and_stac: AiohttpClientMocker,
) -> None:
    """FakeBackend delivers data without downloading any forecast CSV files.

    Setup succeeds *only* if no forecast CSV URL is hit (those URLs are not
    registered in mock_station_and_stac; an unregistered request raises
    OgdConnectionError). This is the strongest proof that the backend seam
    encapsulates all per-parameter file I/O.
    """
    fake = FakeBackend()
    with patch(
        "custom_components.meteoswiss_weather._backend_factory",
        return_value=fake,
    ):
        await _setup(hass, config_entry)

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    assert state.state != "unavailable"

    assert _forecast_csv_calls(mock_station_and_stac) == 0


async def test_daily_forecast_from_fake_backend(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_station_and_stac: AiohttpClientMocker,
) -> None:
    """Daily forecast service returns FakeBackend's 3 days verbatim.

    The entity does not know (or care) that the data came from memory rather
    than from downloaded CSV files.
    """
    fake = FakeBackend()
    with patch(
        "custom_components.meteoswiss_weather._backend_factory",
        return_value=fake,
    ):
        await _setup(hass, config_entry)

    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": _ENTITY_ID, "type": "daily"},
        blocking=True,
        return_response=True,
    )
    forecasts = response[_ENTITY_ID]["forecast"]

    assert len(forecasts) == len(FakeBackend.DAILY)

    first = forecasts[0]
    assert first["datetime"] == "2026-08-27"
    assert first["temperature"] == 25.0
    assert first["templow"] == 15.0
    assert first["precipitation"] == 0.0
    assert first["condition"] == "partlycloudy"  # symbol 2 → partlycloudy

    last = forecasts[-1]
    assert last["datetime"] == "2026-08-29"
    assert last["temperature"] == 18.0


async def test_hourly_forecast_from_fake_backend(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_station_and_stac: AiohttpClientMocker,
) -> None:
    """Hourly forecast service returns FakeBackend's 4 hours verbatim.

    FORECAST_HOURLY is advertised and the coordinator calls fetch_hourly;
    no hourly CSV URLs are touched.
    """
    fake = FakeBackend()
    with patch(
        "custom_components.meteoswiss_weather._backend_factory",
        return_value=fake,
    ):
        await _setup(hass, hourly_config_entry)

    features = hass.states.get(_ENTITY_ID).attributes["supported_features"]
    assert features & WeatherEntityFeature.FORECAST_HOURLY

    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": _ENTITY_ID, "type": "hourly"},
        blocking=True,
        return_response=True,
    )
    forecasts = response[_ENTITY_ID]["forecast"]

    assert len(forecasts) == len(FakeBackend.HOURLY)

    first = forecasts[0]
    assert first["datetime"] == "2026-08-27T00:00:00+00:00"
    assert first["temperature"] == 20.0
    assert first["wind_speed"] == 10.0
    assert first["wind_gust_speed"] == 15.0
    assert first["wind_bearing"] == 270

    assert _forecast_csv_calls(mock_station_and_stac) == 0


async def test_current_conditions_unchanged_with_fake_backend(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_station_and_stac: AiohttpClientMocker,
) -> None:
    """Station observation attributes are unaffected by which backend is used.

    The StationCoordinator is independent of ForecastBackend; current
    conditions must still come from the BER fixture regardless of the swap.
    """
    fake = FakeBackend()
    with patch(
        "custom_components.meteoswiss_weather._backend_factory",
        return_value=fake,
    ):
        await _setup(hass, config_entry)

    attrs = hass.states.get(_ENTITY_ID).attributes
    assert attrs["temperature"] == 19.5
    assert attrs["humidity"] == 88.0
    assert attrs["attribution"] == "Source: MeteoSwiss"
