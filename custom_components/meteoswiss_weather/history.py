"""Home-Assistant-side history operations for the reconfigure flow (A9, #52)
and the ``import_history`` service (B12b, ADR-0007).

The pure upstream parsing lives in ``ogd/history.py`` (ADR-0001); this module is
the thin integration layer that touches Home Assistant's recorder. It implements
the **discard** choice of the reconfigure flow (purge the station sensors'
recorded states and clear their long-term statistics), the logbook note for the
**keep** choice, and — via :func:`async_backfill` — the **backfill** path shared
by the ``import_history`` service and the reconfigure flow's backfill choice.

``async_backfill`` maps the parsed hourly history rows onto Home Assistant's
long-term statistics under the integration's own sensor statistic ids
(temperature mean/min/max, mean for the other continuous quantities, hourly sum
for precipitation) and imports them through the recorder. The recorder's import
upserts on ``(statistic_id, start)``, so re-running over an overlapping range
**replaces** those hours rather than duplicating them (ADR-0007).

Every recorder call is guarded: the recorder is a default component but not
guaranteed to be loaded, so the integration never hard-depends on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfIrradiance,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_POINT_ID, CONF_POINT_TYPE_ID, DOMAIN
from .ogd import HourlyHistoryRow, fetch_station_history

_LOGGER = logging.getLogger(__name__)

# Recorder domain; ``async_import_statistics`` requires the metadata ``source``
# to equal it exactly when importing under a sensor's own statistic id.
_RECORDER_DOMAIN = "recorder"


def _station_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Entity ids of this entry's station-backed sensors (the ones with history).

    Only the SwissMetNet observation sensors carry measurement history tied to
    the station; the forecast-derived sensors (``*_today``) come from the
    forecast point and are unaffected by a station change, so they are excluded.
    """
    # Imported lazily to avoid a module-level import cycle with the sensor
    # platform (which imports the integration package for its config-entry type).
    from .sensor import _SENSORS

    device_unique_id = f"{entry.data[CONF_POINT_TYPE_ID]}-{entry.data[CONF_POINT_ID]}"
    station_unique_ids = {f"{device_unique_id}_{desc.key}" for desc in _SENSORS}

    registry = er.async_get(hass)
    return [
        ent.entity_id
        for ent in er.async_entries_for_config_entry(registry, entry.entry_id)
        if ent.unique_id in station_unique_ids
    ]


async def async_discard_station_history(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Purge the station sensors' states and clear their long-term statistics.

    A clean start at the new station: the recorded short-term states are purged
    and the long-term statistics (keyed by the same statistic ids as the sensor
    entities) are cleared. No-op when there are no station entities yet or the
    recorder is not loaded.
    """
    entity_ids = _station_entity_ids(hass, entry)
    if not entity_ids:
        return

    if "recorder" not in hass.config.components:
        _LOGGER.warning(
            "Recorder not loaded; cannot discard history for %s", entity_ids
        )
        return

    # Purge the recorded states via the recorder's own service (keep_days=0).
    if hass.services.has_service("recorder", "purge_entities"):
        await hass.services.async_call(
            "recorder",
            "purge_entities",
            {"entity_id": entity_ids, "keep_days": 0},
            blocking=True,
        )

    # Clear the long-term statistics; for these sensors the statistic id is the
    # entity id. Overlapping ranges are the point — a clean slate at the new site.
    from homeassistant.components.recorder import get_instance

    get_instance(hass).async_clear_statistics(entity_ids)


@dataclass(frozen=True, slots=True)
class _StatMap:
    """Maps one station sensor onto its long-term statistic shape.

    ``sensor_key`` is the :class:`~sensor.MeteoSwissSensorDescription` key that
    forms the sensor's unique-id suffix, hence its statistic id. ``value_field``
    is the :class:`~ogd.HourlyHistoryRow` attribute carrying the hourly value.
    A ``has_sum`` map imports a cumulative sum (precipitation); otherwise the
    value is a mean, optionally with per-hour ``min``/``max`` (temperature).
    """

    sensor_key: str
    unit: str
    value_field: str
    has_sum: bool = False
    min_field: str | None = None
    max_field: str | None = None


# Which statistics the backfill writes and how (ADR-0007, issue #66). Units must
# match the sensor's native unit so the statistic and the live sensor line up.
# Sunshine is parsed by the client but intentionally not imported here.
_STAT_MAPS: tuple[_StatMap, ...] = (
    _StatMap(
        "temperature",
        UnitOfTemperature.CELSIUS,
        "temp_mean",
        min_field="temp_min",
        max_field="temp_max",
    ),
    _StatMap("humidity", PERCENTAGE, "humidity"),
    _StatMap("dew_point", UnitOfTemperature.CELSIUS, "dew_point"),
    _StatMap("pressure_qff", UnitOfPressure.HPA, "pressure_qff"),
    _StatMap("wind_speed", UnitOfSpeed.KILOMETERS_PER_HOUR, "wind_speed_kmh"),
    _StatMap("gust_speed", UnitOfSpeed.KILOMETERS_PER_HOUR, "gust_kmh"),
    _StatMap(
        "global_radiation",
        UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        "global_radiation",
    ),
    _StatMap(
        "precipitation",
        UnitOfPrecipitationDepth.MILLIMETERS,
        "precipitation_sum",
        has_sum=True,
    ),
)


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Outcome of :func:`async_backfill`, for the caller's notification."""

    rows: int  # history rows parsed from the upstream files
    series: int  # statistic ids imported (sensors present for this entry)
    start: datetime
    end: datetime


def _station_stat_ids(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Map ``sensor_key -> statistic id`` for this entry's station sensors.

    A sensor's statistic id is its entity id; the entity's unique id is
    ``{point_type_id}-{point_id}_{sensor_key}``. Only registered station sensors
    appear (a station that does not carry a parameter has no entity, so no
    statistic is written for it).
    """
    device_unique_id = f"{entry.data[CONF_POINT_TYPE_ID]}-{entry.data[CONF_POINT_ID]}"
    prefix = f"{device_unique_id}_"
    registry = er.async_get(hass)
    stat_ids: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id.startswith(prefix):
            stat_ids[ent.unique_id[len(prefix):]] = ent.entity_id
    return stat_ids


def _dedupe_rows(rows: list[HourlyHistoryRow]) -> list[HourlyHistoryRow]:
    """Return rows sorted by timestamp with one row per hour (last wins).

    Statistics are keyed by hour; collapsing duplicate timestamps first keeps
    the cumulative precipitation sum monotonic and the import one-row-per-hour.
    """
    by_ts: dict[datetime, HourlyHistoryRow] = {row.ts_utc: row for row in rows}
    return [by_ts[ts] for ts in sorted(by_ts)]


def _build_statistics(
    spec: _StatMap, rows: list[HourlyHistoryRow]
) -> list[dict[str, Any]]:
    """Build the recorder ``StatisticData`` rows for one statistic.

    Rows are ``StatisticData`` TypedDicts at runtime (plain dicts), built here
    to keep the recorder import lazy. Hours whose value is missing are skipped;
    for a sum the running total simply carries across the gap.
    """
    stats: list[dict[str, Any]] = []
    running_sum = 0.0
    for row in rows:
        value = getattr(row, spec.value_field)
        if value is None:
            continue
        if spec.has_sum:
            running_sum += value
            stats.append({"start": row.ts_utc, "state": value, "sum": running_sum})
            continue
        data: dict[str, Any] = {"start": row.ts_utc, "mean": value}
        if spec.min_field is not None:
            mn = getattr(row, spec.min_field)
            if mn is not None:
                data["min"] = mn
        if spec.max_field is not None:
            mx = getattr(row, spec.max_field)
            if mx is not None:
                data["max"] = mx
        stats.append(data)
    return stats


async def async_backfill(
    hass: HomeAssistant,
    entry: ConfigEntry,
    station_abbr: str,
    start: datetime,
    end: datetime,
) -> BackfillResult:
    """Import ``station_abbr``'s official hourly history into long-term statistics.

    Fetches the history files that overlap ``[start, end]`` (ADR-0007; one at a
    time, parsed in the executor), maps the rows onto the entry's own sensor
    statistic ids and imports them. Re-running over an overlapping range replaces
    those hours rather than duplicating them (the recorder upserts on
    ``(statistic_id, start)``). Raises :class:`HomeAssistantError` if the
    recorder is not loaded; propagates :class:`~ogd.OgdError` on a fetch/parse
    failure (its message names the file).

    Shared by the ``import_history`` service and the reconfigure flow's backfill
    choice (#52).
    """
    if _RECORDER_DOMAIN not in hass.config.components:
        raise HomeAssistantError(
            "The recorder is not loaded; cannot import long-term statistics"
        )

    # Imported lazily so the integration never hard-depends on the recorder.
    from homeassistant.components.recorder.statistics import async_import_statistics

    session = async_get_clientsession(hass)
    rows = _dedupe_rows(await fetch_station_history(session, station_abbr, start, end))

    stat_ids = _station_stat_ids(hass, entry)
    series = 0
    for spec in _STAT_MAPS:
        statistic_id = stat_ids.get(spec.sensor_key)
        if statistic_id is None:
            continue
        stats = _build_statistics(spec, rows)
        if not stats:
            continue
        metadata = {
            "has_mean": not spec.has_sum,
            "has_sum": spec.has_sum,
            "name": None,
            "source": _RECORDER_DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": spec.unit,
        }
        async_import_statistics(hass, metadata, stats)
        series += 1

    _LOGGER.info(
        "Imported %d history rows across %d statistics for %s (%s → %s)",
        len(rows),
        series,
        station_abbr,
        start.isoformat(),
        end.isoformat(),
    )
    return BackfillResult(rows=len(rows), series=series, start=start, end=end)


@callback
def async_log_station_switch(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_station_name: str,
    new_station_name: str,
) -> None:
    """Record the station switch so the seam in the kept history is findable.

    The ``keep`` choice leaves the recorded values in place even though they came
    from the previous station; a logbook entry (when the logbook is loaded) marks
    when the switch happened.
    """
    message = (
        f"weather station changed from {old_station_name} to {new_station_name}; "
        "history recorded before this point came from the previous station"
    )
    _LOGGER.info("%s: %s", entry.title, message)
    if "logbook" in hass.config.components:
        from homeassistant.components.logbook import async_log_entry

        async_log_entry(hass, entry.title or DOMAIN, message, DOMAIN)
