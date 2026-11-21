"""Tests for the recorder-side history operations of the reconfigure flow (A9).

The pure upstream parser lives in ``ogd/history.py`` (tested in
``test_ogd_history.py``); this module covers the thin Home Assistant layer:
the discard path (purge states + clear statistics), the keep logbook note and
the reconfigure flow's backfill (range from the earliest recorded statistic,
background import with its notification). The recorder and the history fetch
are mocked — the tests never touch a real database or the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant, ServiceCall
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
from custom_components.meteoswiss_weather.history import (
    async_backfill_plan,
    async_backfill_station_change,
    async_discard_station_history,
    async_log_station_switch,
)
from custom_components.meteoswiss_weather.ogd import OgdConnectionError

_DEVICE_UID = "2-800100"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=_DEVICE_UID,
        data={
            CONF_POINT_ID: 800100,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "8001",
            CONF_POINT_NAME: "Zürich",
            CONF_STATION_ABBR: "SMA",
            CONF_STATION_NAME: "Zürich / Fluntern",
        },
        title="Zürich",
    )


def _spy_purge_service(hass: HomeAssistant) -> list[ServiceCall]:
    """Register a stand-in ``recorder.purge_entities`` and capture its calls."""
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("recorder", "purge_entities", _handler)
    return calls


async def test_discard_purges_states_and_clears_statistics(
    hass: HomeAssistant,
) -> None:
    """Discard purges the station sensors' states and clears their statistics.

    A forecast-derived sensor sharing the device prefix is registered too and
    must be left alone — only the station-backed sensors carry station history.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    station_entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    forecast_entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temp_max_today", config_entry=entry
    )
    hass.config.components.add("recorder")
    calls = _spy_purge_service(hass)

    instance = Mock()
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        await async_discard_station_history(hass, entry)

    assert len(calls) == 1
    purged = calls[0].data["entity_id"]
    assert station_entity.entity_id in purged
    assert forecast_entity.entity_id not in purged

    instance.async_clear_statistics.assert_called_once()
    cleared = instance.async_clear_statistics.call_args.args[0]
    assert station_entity.entity_id in cleared
    assert forecast_entity.entity_id not in cleared


async def test_discard_noop_without_recorder(hass: HomeAssistant) -> None:
    """With the recorder unloaded, discard neither purges nor clears."""
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    # No "recorder" in hass.config.components.
    calls = _spy_purge_service(hass)

    with patch("homeassistant.components.recorder.get_instance") as get_instance:
        await async_discard_station_history(hass, entry)

    assert calls == []
    get_instance.assert_not_called()


async def test_discard_noop_without_entities(hass: HomeAssistant) -> None:
    """No station entities yet → nothing to purge, recorder untouched."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config.components.add("recorder")
    calls = _spy_purge_service(hass)

    with patch("homeassistant.components.recorder.get_instance") as get_instance:
        await async_discard_station_history(hass, entry)

    assert calls == []
    get_instance.assert_not_called()


async def test_log_station_switch_writes_logbook(hass: HomeAssistant) -> None:
    """The keep note goes to the logbook when the logbook is loaded."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config.components.add("logbook")

    with patch(
        "homeassistant.components.logbook.async_log_entry"
    ) as log_entry:
        async_log_station_switch(hass, entry, "Zürich / Fluntern", "Zürich / Kloten")

    log_entry.assert_called_once()
    message = log_entry.call_args.args[2]
    assert "Zürich / Fluntern" in message
    assert "Zürich / Kloten" in message


async def test_log_station_switch_without_logbook(hass: HomeAssistant) -> None:
    """Without the logbook component the note is a no-op (only the debug log)."""
    entry = _entry()
    entry.add_to_hass(hass)
    # No "logbook" in hass.config.components.

    with patch(
        "homeassistant.components.logbook.async_log_entry"
    ) as log_entry:
        async_log_station_switch(hass, entry, "A", "B")

    log_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Backfill on a station change (#245)
# ---------------------------------------------------------------------------


def _ts(*args: int) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def _recorder_instance(hass: HomeAssistant) -> Mock:
    """A recorder stand-in whose executor runs the job in HA's executor."""
    instance = Mock()
    instance.async_add_executor_job = hass.async_add_executor_job
    return instance


async def test_backfill_plan_starts_at_earliest_statistic(hass: HomeAssistant) -> None:
    """The range starts at the earliest hourly statistic of the written sensors.

    The monthly query locates the first month; the hourly query over that month
    pins the hour. Only sensors the backfill writes are queried (not the
    forecast-derived ``*_today`` ones).
    """
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    temp = registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    rain = registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_precipitation", config_entry=entry
    )
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temp_max_today", config_entry=entry
    )
    hass.config.components.add("recorder")

    calls: list[tuple] = []

    def _stats(_hass, start, end, ids, period, units, types):
        calls.append((start, end, set(ids), period))
        if period == "month":
            return {
                temp.entity_id: [{"start": _ts(2025, 3, 1)}],
                rain.entity_id: [{"start": _ts(2025, 4, 1)}],
            }
        return {
            temp.entity_id: [{"start": _ts(2025, 3, 4, 7)}],
            rain.entity_id: [{"start": _ts(2025, 3, 4, 5)}],
        }

    with (
        patch(
            "homeassistant.components.recorder.get_instance",
            return_value=_recorder_instance(hass),
        ),
        patch(
            "homeassistant.components.recorder.statistics.statistics_during_period",
            side_effect=_stats,
        ),
    ):
        plan = await async_backfill_plan(hass, entry)

    assert plan is not None
    stat_ids, start = plan
    assert stat_ids == {
        "temperature": temp.entity_id,
        "precipitation": rain.entity_id,
    }
    assert start == datetime(2025, 3, 4, 5, tzinfo=UTC)
    assert [c[3] for c in calls] == ["month", "hour"]
    assert calls[1][0] == datetime(2025, 3, 1, tzinfo=UTC)
    assert all(c[2] == {temp.entity_id, rain.entity_id} for c in calls)


async def test_backfill_plan_none_without_statistics(hass: HomeAssistant) -> None:
    """No statistic recorded yet → no plan, so no fetch and no import."""
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    hass.config.components.add("recorder")

    with (
        patch(
            "homeassistant.components.recorder.get_instance",
            return_value=_recorder_instance(hass),
        ),
        patch(
            "homeassistant.components.recorder.statistics.statistics_during_period",
            return_value={},
        ),
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(),
        ) as fetch,
    ):
        assert await async_backfill_plan(hass, entry) is None
    fetch.assert_not_called()


async def test_backfill_plan_none_without_recorder(hass: HomeAssistant) -> None:
    """With the recorder unloaded there is nothing to look up."""
    entry = _entry()
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    with patch("homeassistant.components.recorder.get_instance") as get_instance:
        assert await async_backfill_plan(hass, entry) is None
    get_instance.assert_not_called()


async def test_backfill_station_change_notifies_success(hass: HomeAssistant) -> None:
    """The background import reports rows, series and range like the service."""
    entry = _entry()
    entry.add_to_hass(hass)
    temp = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    hass.config.components.add("recorder")
    from custom_components.meteoswiss_weather.ogd import HourlyHistoryRow

    rows = [HourlyHistoryRow(ts_utc=datetime(2025, 3, 4, 5, tzinfo=UTC), temp_mean=3.0)]
    with (
        patch(
            "custom_components.meteoswiss_weather.history.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(return_value=rows),
        ) as fetch,
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics"
        ) as import_stats,
        patch(
            "homeassistant.components.persistent_notification.async_create"
        ) as notify,
    ):
        await async_backfill_station_change(
            hass,
            entry,
            "KLO",
            datetime(2025, 3, 4, 5, tzinfo=UTC),
            datetime(2026, 11, 18, tzinfo=UTC),
            {"temperature": temp.entity_id},
        )

    assert fetch.call_args.args[1] == "KLO"
    import_stats.assert_called_once()
    assert import_stats.call_args.args[1]["statistic_id"] == temp.entity_id
    message = notify.call_args.args[1]
    assert "Imported 1 hourly history rows" in message
    assert "(KLO) across 1 statistics" in message
    assert "2025-03-04 to 2026-11-18" in message


async def test_backfill_range_is_statistic_hours_not_reference_timestamps(
    hass: HomeAssistant,
) -> None:
    """Earliest statistic 05:00 → first imported statistic starts 05:00, none at 04:00.

    A row stamped ``ts`` becomes the statistic starting at ``ts - 1 h``, so the
    fetch must ask for ``[start + 1 h, end + 1 h]``. The fake fetch filters on
    the reference timestamp like ``ogd/history.py`` does.
    """
    from datetime import timedelta

    from custom_components.meteoswiss_weather.ogd import HourlyHistoryRow

    entry = _entry()
    entry.add_to_hass(hass)
    temp = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    hass.config.components.add("recorder")
    first = datetime(2026, 10, 31, 5, tzinfo=UTC)
    all_rows = [
        HourlyHistoryRow(ts_utc=first + timedelta(hours=h), temp_mean=float(h))
        for h in range(-1, 4)  # reference timestamps 04:00 .. 08:00
    ]

    async def _fetch(_session, _abbr, start, end):
        return [r for r in all_rows if start <= r.ts_utc <= end]

    end = first + timedelta(hours=2)  # last statistic hour: 07:00
    with (
        patch(
            "custom_components.meteoswiss_weather.history.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(side_effect=_fetch),
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics"
        ) as import_stats,
        patch("homeassistant.components.persistent_notification.async_create"),
    ):
        await async_backfill_station_change(
            hass, entry, "KLO", first, end, {"temperature": temp.entity_id}
        )

    starts = [s["start"] for s in import_stats.call_args.args[2]]
    assert starts == [first + timedelta(hours=h) for h in range(3)]
    assert first - timedelta(hours=1) not in starts


async def test_backfill_station_change_error_notifies_and_does_not_raise(
    hass: HomeAssistant,
) -> None:
    """An OgdError in the background import posts the failure, never raises."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config.components.add("recorder")

    with (
        patch(
            "custom_components.meteoswiss_weather.history.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.meteoswiss_weather.history.fetch_station_history",
            AsyncMock(side_effect=OgdConnectionError("boom: some_file.csv")),
        ),
        patch(
            "homeassistant.components.persistent_notification.async_create"
        ) as notify,
    ):
        await async_backfill_station_change(
            hass,
            entry,
            "KLO",
            datetime(2025, 3, 4, tzinfo=UTC),
            datetime(2026, 11, 18, tzinfo=UTC),
            {"temperature": "sensor.zurich_temperature"},
        )

    notify.assert_called_once()
    assert "failed" in notify.call_args.args[1]
    assert "some_file.csv" in notify.call_args.args[1]
