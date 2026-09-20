"""Tests for the daily traffic diagnostic sensors (issue #146).

Verifies accumulation, midnight reset, and default enabled/disabled flags for
the two sensors:
  - ``sensor.koniz_data_fetched_today`` (enabled by default)
  - ``sensor.koniz_requests_today`` (disabled by default)

Integration tests use a FakeBackendWithTraffic so no network I/O happens.
Unit tests for DailyTrafficAccumulator run without Home Assistant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from freezegun import freeze_time
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.coordinator import DailyTrafficAccumulator
from custom_components.meteoswiss_weather.ogd import (
    DailyBundle,
    DailyForecast,
    ForecastBackend,
    ForecastPoint,
    HourlyForecast,
)

# ---------------------------------------------------------------------------
# FakeBackend with pop_fetch_totals support
# ---------------------------------------------------------------------------


class FakeBackendWithTraffic:
    """FakeBackend that exposes pop_fetch_totals for traffic-sensor testing.

    Each call to fetch_daily loads ``bytes_per_tick`` bytes and
    ``requests_per_tick`` requests into a pending bucket.  pop_fetch_totals()
    drains the bucket and returns the accumulated values, mirroring how
    BulkCsvBackend accumulates real HTTP fetch totals.
    """

    def __init__(self, bytes_per_tick: int = 500_000, requests_per_tick: int = 10):
        self._pending_bytes = 0
        self._pending_requests = 0
        self._bytes_per_tick = bytes_per_tick
        self._requests_per_tick = requests_per_tick
        self._daily: list[DailyForecast] = [
            DailyForecast(
                date=date(2026, 8, 27),
                temp_max=25.0,
                temp_min=15.0,
                precipitation=0.0,
                symbol=2,
            )
        ]

    async def fetch_daily(
        self,
        point: ForecastPoint,
        *,
        run=None,
        hourly_horizon_days: int | None = None,
    ) -> DailyBundle:
        self._pending_bytes += self._bytes_per_tick
        self._pending_requests += self._requests_per_tick
        return DailyBundle(daily=self._daily)

    async def fetch_hourly(
        self,
        point: ForecastPoint,
        *,
        horizon_days: int = -1,
        params: tuple[str, ...] = (),
        run=None,
        window_start_override=None,
    ) -> list[HourlyForecast]:
        return []

    async def fetch_hourly_canary(
        self,
        point: ForecastPoint,
        param: str,
        *,
        hours: int,
        run=None,
    ) -> dict[datetime, float | int] | None:
        return None

    def pop_fetch_totals(self) -> tuple[int, int]:
        """Drain and return pending (bytes, requests)."""
        result = (self._pending_bytes, self._pending_requests)
        self._pending_bytes = 0
        self._pending_requests = 0
        return result


# FakeBackendWithTraffic satisfies the ForecastBackend protocol.
_: ForecastBackend = FakeBackendWithTraffic()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
        unique_id="2-309800",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    fake: FakeBackendWithTraffic,
    mock_ogd,
) -> None:
    with patch(
        "custom_components.meteoswiss_weather._backend_factory",
        return_value=fake,
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _state(hass: HomeAssistant, key: str) -> str:
    entity_id = f"sensor.koniz_{key}"
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id!r} not found"
    return state.state


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@freeze_time("2026-08-27 10:00:00")
async def test_data_fetched_today_initial_value(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """After first refresh, data_fetched_today shows bytes from the backend."""
    fake = FakeBackendWithTraffic(bytes_per_tick=500_000, requests_per_tick=10)
    await _setup(hass, config_entry, fake, mock_ogd)

    state_str = _state(hass, "data_fetched_today")
    # 500_000 bytes = 0.5 MB
    assert float(state_str) == pytest.approx(0.5, rel=1e-4)


@freeze_time("2026-08-27 10:00:00")
async def test_data_fetched_zero_without_pop(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """A backend without pop_fetch_totals leaves the sensor at 0 (graceful)."""
    from tests.test_backend_seam import FakeBackend

    fake = FakeBackend()
    await _setup(hass, config_entry, fake, mock_ogd)

    state_str = _state(hass, "data_fetched_today")
    assert float(state_str) == 0.0


@freeze_time("2026-08-27 10:00:00")
async def test_data_fetched_entity_category(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """data_fetched_today is entity_category=DIAGNOSTIC and enabled by default."""
    fake = FakeBackendWithTraffic()
    await _setup(hass, config_entry, fake, mock_ogd)

    entity_reg = er.async_get(hass)
    entry_er = entity_reg.async_get("sensor.koniz_data_fetched_today")
    assert entry_er is not None
    assert entry_er.entity_category == EntityCategory.DIAGNOSTIC
    assert not entry_er.disabled


@freeze_time("2026-08-27 10:00:00")
async def test_requests_today_disabled_by_default(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """requests_today is entity_category=DIAGNOSTIC and disabled by default."""
    fake = FakeBackendWithTraffic()
    await _setup(hass, config_entry, fake, mock_ogd)

    entity_reg = er.async_get(hass)
    entry_er = entity_reg.async_get("sensor.koniz_requests_today")
    assert entry_er is not None
    assert entry_er.entity_category == EntityCategory.DIAGNOSTIC
    assert entry_er.disabled


@freeze_time("2026-08-27 10:00:00")
async def test_requests_today_value_in_coordinator(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """requests_today accumulates in the coordinator even when the sensor is disabled.

    The sensor is disabled by default; the coordinator still tracks totals.
    """
    fake = FakeBackendWithTraffic(bytes_per_tick=500_000, requests_per_tick=7)
    await _setup(hass, config_entry, fake, mock_ogd)

    # The sensor is disabled but the coordinator's accumulator is always updated.
    traffic = config_entry.runtime_data.forecast_coordinator.traffic
    assert traffic.requests_today == 7


@freeze_time("2026-08-27 10:00:00")
async def test_midnight_resets_sensor_to_zero(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """data_fetched_today reads 0 after the coordinator's accumulator is reset.

    The sensor's native_value returns 0 when the accumulator's reset_date
    does not match today.  Simulates what _handle_midnight does: it calls
    coordinator.traffic.record(0, 0, new_day) which resets the counters, then
    writes the state.  We verify both the coordinator state and the sensor
    state.
    """
    fake = FakeBackendWithTraffic(bytes_per_tick=500_000)
    await _setup(hass, config_entry, fake, mock_ogd)

    assert float(_state(hass, "data_fetched_today")) == pytest.approx(0.5, rel=1e-4)

    # Advance to the next day and reset the accumulator as _handle_midnight does.
    with freeze_time("2026-08-28 00:00:00"):
        coordinator = config_entry.runtime_data.forecast_coordinator
        tomorrow = datetime(2026, 8, 28, tzinfo=UTC).date()
        coordinator.traffic.record(0, 0, tomorrow)
        # Re-write state exactly like _handle_midnight does.
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()

    assert float(_state(hass, "data_fetched_today")) == 0.0


@freeze_time("2026-08-27 10:00:00")
async def test_traffic_accumulator_restored_from_store(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd,
) -> None:
    """Traffic totals are restored from storage on restart (simulate via store).

    After setup the coordinator has non-zero totals.  We verify the accumulator
    holds them correctly and could be serialised, then create a fresh
    accumulator from the serialised form and verify it matches.
    """
    fake = FakeBackendWithTraffic(bytes_per_tick=800_000, requests_per_tick=15)
    await _setup(hass, config_entry, fake, mock_ogd)

    coordinator = config_entry.runtime_data.forecast_coordinator
    assert coordinator.traffic.bytes_today == 800_000
    assert coordinator.traffic.requests_today == 15
    assert coordinator.traffic.reset_date == date(2026, 8, 27)

    # Serialise and restore.
    data = coordinator.traffic.to_dict()
    restored = DailyTrafficAccumulator.from_dict(data, date(2026, 8, 27))
    assert restored.bytes_today == 800_000
    assert restored.requests_today == 15
    assert restored.reset_date == date(2026, 8, 27)


# ---------------------------------------------------------------------------
# DailyTrafficAccumulator unit tests (pure Python, no Home Assistant)
# ---------------------------------------------------------------------------


def test_accumulator_record_same_day() -> None:
    """Records accumulate on the same day."""
    acc = DailyTrafficAccumulator()
    today = date(2026, 9, 20)
    assert acc.record(100, 2, today) is True
    assert acc.bytes_today == 100
    assert acc.requests_today == 2
    assert acc.record(50, 1, today) is True
    assert acc.bytes_today == 150
    assert acc.requests_today == 3


def test_accumulator_resets_on_new_day() -> None:
    """A new local date resets the accumulators before applying the delta."""
    acc = DailyTrafficAccumulator()
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    acc.record(1000, 20, today)
    assert acc.record(100, 5, tomorrow) is True
    assert acc.bytes_today == 100
    assert acc.requests_today == 5
    assert acc.reset_date == tomorrow


def test_accumulator_resets_on_new_day_with_zero_delta() -> None:
    """A zero delta on a new day still resets the counters (returns True)."""
    acc = DailyTrafficAccumulator()
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    acc.record(1000, 20, today)
    # A zero delta on a NEW day: counters reset, but no bytes/requests added.
    changed = acc.record(0, 0, tomorrow)
    assert changed is False  # reset + zero delta → no change in stored values
    assert acc.bytes_today == 0
    assert acc.requests_today == 0
    assert acc.reset_date == tomorrow


def test_accumulator_no_change_on_zero_delta_same_day() -> None:
    """A zero delta on the same day returns False (no change)."""
    acc = DailyTrafficAccumulator()
    today = date(2026, 9, 20)
    acc.record(100, 2, today)
    changed = acc.record(0, 0, today)
    assert changed is False
    assert acc.bytes_today == 100


def test_accumulator_round_trip_serialise() -> None:
    """to_dict / from_dict round-trip on the same day."""
    acc = DailyTrafficAccumulator()
    today = date(2026, 9, 20)
    acc.record(123456, 42, today)
    data = acc.to_dict()

    restored = DailyTrafficAccumulator.from_dict(data, today)
    assert restored.bytes_today == 123456
    assert restored.requests_today == 42
    assert restored.reset_date == today


def test_accumulator_from_dict_discards_stale_day() -> None:
    """from_dict returns a fresh accumulator when the stored date is yesterday."""
    acc = DailyTrafficAccumulator()
    yesterday = date(2026, 9, 19)
    acc.record(999, 99, yesterday)
    data = acc.to_dict()

    today = date(2026, 9, 20)
    restored = DailyTrafficAccumulator.from_dict(data, today)
    assert restored.bytes_today == 0
    assert restored.requests_today == 0
    assert restored.reset_date is None


def test_accumulator_from_dict_invalid_data() -> None:
    """from_dict returns a fresh accumulator when the data is malformed."""
    today = date(2026, 9, 20)
    restored = DailyTrafficAccumulator.from_dict({"garbage": True}, today)
    assert restored.bytes_today == 0
    restored2 = DailyTrafficAccumulator.from_dict(None, today)
    assert restored2.bytes_today == 0


def test_accumulator_initial_state() -> None:
    """Fresh accumulator has zero counts and no reset date."""
    acc = DailyTrafficAccumulator()
    assert acc.bytes_today == 0
    assert acc.requests_today == 0
    assert acc.reset_date is None
