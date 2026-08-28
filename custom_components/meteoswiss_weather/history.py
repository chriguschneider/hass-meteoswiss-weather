"""Home-Assistant-side history operations for the reconfigure flow (A9, #52).

The pure upstream parsing lives in ``ogd/history.py`` (ADR-0001); this module is
the thin integration layer that touches Home Assistant's recorder. It currently
implements the **discard** choice of the reconfigure flow (purge the station
sensors' recorded states and clear their long-term statistics) and the logbook
note for the **keep** choice. The **backfill** choice's recorder-import path is
the follow-up to ADR-0007 (issue #51) and is not wired here yet — see
``BACKFILL_AVAILABLE`` in ``const.py``.

Every recorder call is guarded: the recorder is a default component but not
guaranteed to be loaded, so the integration never hard-depends on it.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_POINT_ID, CONF_POINT_TYPE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


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
