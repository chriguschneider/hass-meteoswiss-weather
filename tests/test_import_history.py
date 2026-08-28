"""Tests for the ``import_history`` service and the ``async_backfill`` layer.

Slice 2 of the statistics backfill (B12b, ADR-0007). The pure upstream parser
lives in ``ogd/history.py`` (tested in ``test_ogd_history.py``); here the history
fetch and the recorder import are both mocked, so the network and a real
database are never touched. Covered: statistic-id mapping and unit handling,
replace-not-duplicate on overlap, service argument validation, and the absence
of any history fetch until the service is called.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_weather import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_END,
    ATTR_START,
    SERVICE_IMPORT_HISTORY,
    _async_register_services,
)
from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.history import (
    _STAT_MAPS,
    _build_statistics,
    async_backfill,
)
from custom_components.meteoswiss_weather.ogd import (
    HourlyHistoryRow,
    OgdConnectionError,
)

_DEVICE_UID = "2-309800"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=_DEVICE_UID,
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
    )


def _row(ts: datetime, **kw) -> HourlyHistoryRow:
    return HourlyHistoryRow(ts_utc=ts, **kw)


_ROWS = [
    _row(
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        temp_mean=2.3,
        temp_min=1.8,
        temp_max=2.5,
        humidity=80.5,
        precipitation_sum=0.1,
    ),
    _row(
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        temp_mean=2.0,
        temp_min=1.5,
        temp_max=2.2,
        humidity=82.0,
        precipitation_sum=0.4,
    ),
    _row(
        datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
        temp_mean=1.9,
        humidity=None,  # missing → this hour is skipped for humidity
        precipitation_sum=None,  # missing → carried, not a data point
    ),
]


@pytest.fixture(autouse=True)
def _no_clientsession():
    """Stub the aiohttp session so the backfill never opens a real connection.

    ``async_backfill`` asks for the shared client session before it calls the
    (mocked) history fetch; without a real network there is nothing to fetch,
    and stubbing it keeps the test from leaking a resolver thread.
    """
    with patch(
        "custom_components.meteoswiss_weather.history.async_get_clientsession",
        return_value=object(),
    ):
        yield


def _register_station_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, *keys: str
) -> dict[str, str]:
    """Register station sensors for ``keys`` and return ``key -> entity_id``."""
    registry = er.async_get(hass)
    result: dict[str, str] = {}
    for key in keys:
        ent = registry.async_get_or_create(
            "sensor", DOMAIN, f"{_DEVICE_UID}_{key}", config_entry=entry
        )
        result[key] = ent.entity_id
    return result


# ---------------------------------------------------------------------------
# _build_statistics: mapping and unit handling (pure)
# ---------------------------------------------------------------------------


def _spec(key: str):
    return next(spec for spec in _STAT_MAPS if spec.sensor_key == key)


def test_temperature_statistics_carry_mean_min_max() -> None:
    stats = _build_statistics(_spec("temperature"), _ROWS)
    assert [s["start"] for s in stats] == [r.ts_utc for r in _ROWS]
    assert stats[0]["mean"] == pytest.approx(2.3)
    assert stats[0]["min"] == pytest.approx(1.8)
    assert stats[0]["max"] == pytest.approx(2.5)
    # The third row has only a mean; no min/max keys are invented.
    assert "min" not in stats[2]
    assert "max" not in stats[2]


def test_mean_statistic_skips_missing_hours() -> None:
    stats = _build_statistics(_spec("humidity"), _ROWS)
    # Two of three rows carry humidity; the missing one is not emitted.
    assert len(stats) == 2
    assert all("mean" in s and "sum" not in s for s in stats)


def test_precipitation_statistics_are_cumulative_sum() -> None:
    stats = _build_statistics(_spec("precipitation"), _ROWS)
    # Third row's cell is empty → skipped; the sum runs over the present hours.
    assert len(stats) == 2
    assert stats[0]["state"] == pytest.approx(0.1)
    assert stats[0]["sum"] == pytest.approx(0.1)
    assert stats[1]["state"] == pytest.approx(0.4)
    assert stats[1]["sum"] == pytest.approx(0.5)


def test_units_match_the_sensor_native_units() -> None:
    units = {spec.sensor_key: spec.unit for spec in _STAT_MAPS}
    assert units["temperature"] == "°C"
    assert units["humidity"] == "%"
    assert units["precipitation"] == "mm"
    assert units["wind_speed"] == "km/h"
    # Precipitation is the only sum; everything else is a mean.
    assert [s.sensor_key for s in _STAT_MAPS if s.has_sum] == ["precipitation"]


# ---------------------------------------------------------------------------
# async_backfill: statistic-id mapping and replace-not-duplicate
# ---------------------------------------------------------------------------


async def test_backfill_imports_under_sensor_statistic_ids(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    ids = _register_station_sensors(hass, entry, "temperature", "precipitation")
    hass.config.components.add("recorder")

    with (
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(return_value=list(_ROWS)),
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics"
        ) as import_stats,
    ):
        result = await async_backfill(
            hass,
            entry,
            "BER",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 12, 31, tzinfo=UTC),
        )

    assert result.rows == 3
    assert result.series == 2  # temperature + precipitation

    by_id = {
        call.args[1]["statistic_id"]: call.args for call in import_stats.call_args_list
    }
    temp_meta = by_id[ids["temperature"]][1]
    assert temp_meta["has_mean"] is True
    assert temp_meta["has_sum"] is False
    assert temp_meta["unit_of_measurement"] == "°C"
    assert temp_meta["source"] == "recorder"

    precip_meta = by_id[ids["precipitation"]][1]
    assert precip_meta["has_mean"] is False
    assert precip_meta["has_sum"] is True
    assert precip_meta["unit_of_measurement"] == "mm"


async def test_backfill_skips_sensors_without_an_entity(hass: HomeAssistant) -> None:
    """Only registered sensors get statistics; a station without one is skipped."""
    entry = _entry()
    entry.add_to_hass(hass)
    _register_station_sensors(hass, entry, "temperature")
    hass.config.components.add("recorder")

    with (
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(return_value=list(_ROWS)),
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics"
        ) as import_stats,
    ):
        result = await async_backfill(
            hass, entry, "BER",
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
        )

    assert result.series == 1
    assert import_stats.call_count == 1


async def test_backfill_replaces_not_duplicates_on_overlap(
    hass: HomeAssistant,
) -> None:
    """Re-running the same range imports one row per hour, identical each time.

    The recorder upserts on ``(statistic_id, start)``; the same start set on the
    second run replaces the first, it does not append duplicates.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    _register_station_sensors(hass, entry, "temperature")
    hass.config.components.add("recorder")

    # Duplicate the first hour in the raw feed to prove dedupe by timestamp.
    raw = [*_ROWS, _row(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), temp_mean=9.9)]

    payloads = []
    with (
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(return_value=raw),
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            side_effect=lambda _hass, _meta, stats: payloads.append(list(stats)),
        ),
    ):
        args = (
            hass, entry, "BER",
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
        )
        await async_backfill(*args)
        await async_backfill(*args)

    first, second = payloads
    starts = [s["start"] for s in first]
    assert len(starts) == len(set(starts))  # one row per hour, no duplicates
    # The duplicate 00:00 row collapsed to the last value seen (9.9).
    assert first[0]["mean"] == pytest.approx(9.9)
    # Overlapping re-run produces the identical payload (replace, not append).
    assert first == second


async def test_backfill_without_recorder_raises(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    # No "recorder" in hass.config.components.
    with (
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(return_value=list(_ROWS)),
        ) as fetch,
        pytest.raises(Exception, match="recorder is not loaded"),
    ):
        await async_backfill(
            hass, entry, "BER",
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
        )
    fetch.assert_not_called()  # bail out before any fetch


# ---------------------------------------------------------------------------
# The import_history service
# ---------------------------------------------------------------------------


async def test_service_defaults_to_current_year(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-08-28")
    entry = _entry()
    entry.add_to_hass(hass)
    _register_station_sensors(hass, entry, "temperature")
    hass.config.components.add("recorder")
    _async_register_services(hass)

    with patch(
        "custom_components.meteoswiss_weather.history.fetch_station_history",
        AsyncMock(return_value=list(_ROWS)),
    ) as fetch, patch(
        "homeassistant.components.recorder.statistics.async_import_statistics"
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id},
            blocking=True,
        )

    fetch.assert_called_once()
    _, _abbr, start, end = fetch.call_args.args
    assert start == datetime(2026, 1, 1, tzinfo=UTC)
    assert end.year == 2026 and end.month == 8


async def test_service_end_before_start_rejected(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    _register_station_sensors(hass, entry, "temperature")
    hass.config.components.add("recorder")
    _async_register_services(hass)

    with patch(
        "custom_components.meteoswiss_weather.history.fetch_station_history",
        AsyncMock(return_value=list(_ROWS)),
    ) as fetch, pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_START: "2024-01-01 00:00:00",
                ATTR_END: "2023-01-01 00:00:00",
            },
            blocking=True,
        )
    fetch.assert_not_called()


async def test_service_no_fetch_until_called(hass: HomeAssistant) -> None:
    """Registering the service performs no history fetch; calling it does."""
    entry = _entry()
    entry.add_to_hass(hass)
    _register_station_sensors(hass, entry, "temperature")
    hass.config.components.add("recorder")

    with patch(
        "custom_components.meteoswiss_weather.history.fetch_station_history",
        AsyncMock(return_value=list(_ROWS)),
    ) as fetch, patch(
        "homeassistant.components.recorder.statistics.async_import_statistics"
    ):
        _async_register_services(hass)
        assert fetch.call_count == 0  # registration alone hits no network

        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id},
            blocking=True,
        )
        assert fetch.call_count == 1


async def test_service_reports_fetch_error(hass: HomeAssistant) -> None:
    """A fetch failure surfaces the file-naming error and notifies the user."""
    entry = _entry()
    entry.add_to_hass(hass)
    _register_station_sensors(hass, entry, "temperature")
    hass.config.components.add("recorder")
    _async_register_services(hass)

    with patch(
        "custom_components.meteoswiss_weather.history.fetch_station_history",
        AsyncMock(side_effect=OgdConnectionError("boom: some_file.csv")),
    ), patch(
        "homeassistant.components.persistent_notification.async_create"
    ) as notify, pytest.raises(Exception, match="boom: some_file.csv"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id},
            blocking=True,
        )

    notify.assert_called_once()
    assert "some_file.csv" in notify.call_args.args[1]
