"""Config flow for the MeteoSwiss Weather integration.

Setup: the user chooses between a postal-code forecast point (default) or a
mountain point of interest, then picks a SwissMetNet station. Reconfigure
re-offers the same choice, pre-filled from the current entry. All choices are
derived from the official open data (ADR-0001); no app API is involved.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    BACKFILL_AVAILABLE,
    CONF_HISTORY_ACTION,
    CONF_HOURLY_CLOUD_LAYERS,
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_HORIZON_DAYS,
    CONF_HOURLY_TEMP_PERCENTILES,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POLLEN,
    CONF_POLLEN_STATION,
    CONF_POSTAL_CODE,
    CONF_PRECIP_STATION_ABBR,
    CONF_PRECIP_STATION_NAME,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
    HISTORY_BACKFILL,
    HISTORY_DISCARD,
    HISTORY_KEEP,
    HOURLY_HORIZON_CHOICES,
    HOURLY_HORIZON_FULL_RUN,
)
from .demand import TrafficEstimate, estimate_traffic
from .history import async_discard_station_history, async_log_station_switch
from .ogd import (
    POINT_TYPE_MOUNTAIN,
    ForecastPoint,
    OgdError,
    PollenStation,
    Station,
    fetch_points,
    fetch_pollen_stations,
    fetch_precip_stations,
    fetch_stations,
    mountain_points,
    nearest_point,
    nearest_pollen_stations,
    nearest_stations,
    points_for_postal_code,
)

# Flow-internal mode constants; never stored in the config entry.
_CONF_MODE = "forecast_mode"
_MODE_POSTAL_CODE = "postal_code"
_MODE_MOUNTAIN = "mountain"

# Sentinel option for "no precipitation station" in the station step's optional
# precipitation pick (ADR-0006). An empty string is never a valid abbreviation,
# so it cleanly marks the opt-out; the feature is off by default.
_PRECIP_NONE = ""


def _mode_schema(default: str = _MODE_POSTAL_CODE) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(_CONF_MODE, default=default): SelectSelector(
                SelectSelectorConfig(
                    options=[_MODE_POSTAL_CODE, _MODE_MOUNTAIN],
                    translation_key="forecast_mode",
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _postal_code_schema(default_plz: int | None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_POSTAL_CODE,
                default=default_plz,
            ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=9999))
        }
    )


def _mountain_label(point: ForecastPoint) -> str:
    if point.height_masl is not None:
        return f"{point.name} ({int(point.height_masl)} m)"
    return point.name


class MeteoSwissWeatherConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup for MeteoSwiss Weather."""

    VERSION = 1

    def __init__(self) -> None:
        self._all_points: list[ForecastPoint] | None = None
        self._all_stations: list[Station] | None = None
        # Precipitation-only network for the optional station pick (ADR-0006).
        # ``None`` means its metadata was unavailable; the pick is then dropped.
        self._all_precip_stations: list[Station] | None = None
        self._point_choices: list[ForecastPoint] = []
        self._point: ForecastPoint | None = None
        # Set while the reconfigure flow (A9, #52) resolves a station change and
        # waits on the history-choice step.
        self._new_station: Station | None = None
        self._pending_data: dict[str, Any] | None = None
        self._pending_unique_id: str | None = None
        self._old_station_name: str = ""

    async def _load_metadata(self) -> bool:
        """Fetch point and station metadata from OGD (cached after first success).

        Returns False on a network or parse error so the caller can map it to
        the ``cannot_connect`` error key without the exception leaking out.
        """
        if self._all_points is not None and self._all_stations is not None:
            return True
        session = async_get_clientsession(self.hass)
        try:
            self._all_points = await fetch_points(session)
            self._all_stations = await fetch_stations(session)
        except OgdError:
            # Any OGD failure (network or malformed metadata) becomes the
            # ``cannot_connect`` error key rather than leaking out of the flow.
            self._all_points = None
            self._all_stations = None
            return False
        # The precipitation-station list powers the optional pick in the station
        # step (ADR-0006). It is opt-in, so a failure here only drops the pick —
        # it never fails the whole flow.
        try:
            self._all_precip_stations = await fetch_precip_stations(session)
        except OgdError:
            self._all_precip_stations = None
        return True

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: choose postal-code or mountain-point mode."""
        errors: dict[str, str] = {}

        if not await self._load_metadata():
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=_mode_schema(),
                errors=errors,
            )

        if user_input is not None:
            mode = user_input[_CONF_MODE]
            if mode == _MODE_MOUNTAIN:
                return await self.async_step_mountain()
            return await self.async_step_postal_code()

        return self.async_show_form(
            step_id="user",
            data_schema=_mode_schema(),
            errors=errors,
        )

    async def async_step_postal_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Postal-code entry: pre-filled from the HA location (setup) or
        current entry (reconfigure)."""
        assert self._all_points is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            postal_code = int(user_input[CONF_POSTAL_CODE])
            candidates = points_for_postal_code(self._all_points, postal_code)
            if not candidates:
                errors["base"] = "unknown_postal_code"
            else:
                self._point_choices = candidates
                if len(candidates) == 1:
                    self._point = candidates[0]
                    if self.source != SOURCE_RECONFIGURE:
                        await self.async_set_unique_id(
                            f"{self._point.point_type_id}-{self._point.point_id}"
                        )
                        self._abort_if_unique_id_configured()
                    return await self.async_step_station()
                return await self.async_step_point()

        # Pre-fill: the current entry's postal code on reconfigure, or the
        # nearest type-2 point to the HA location on initial setup.
        suggested_plz: int | None = None
        if self.source == SOURCE_RECONFIGURE:
            entry_plz = self._get_reconfigure_entry().data.get(CONF_POSTAL_CODE, "")
            try:
                suggested_plz = int(entry_plz) if entry_plz else None
            except (ValueError, TypeError):
                pass
        else:
            try:
                near = nearest_point(
                    self._all_points,
                    self.hass.config.latitude,
                    self.hass.config.longitude,
                )
                if near.postal_code:
                    suggested_plz = int(near.postal_code)
            except Exception:  # noqa: BLE001
                pass

        return self.async_show_form(
            step_id="postal_code",
            data_schema=_postal_code_schema(suggested_plz),
            errors=errors,
        )

    async def async_step_mountain(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Mountain-point selection: dropdown over all type-3 points, sorted by name.

        The nearest mountain point to the HA location is pre-selected. On
        reconfigure the current mountain point is pre-selected.
        """
        assert self._all_points is not None
        all_mountain = mountain_points(self._all_points)
        if not all_mountain:
            return self.async_abort(reason="no_mountain_points")

        if user_input is not None:
            point_id = int(user_input[CONF_POINT_ID])
            self._point = next(p for p in all_mountain if p.point_id == point_id)
            if self.source != SOURCE_RECONFIGURE:
                await self.async_set_unique_id(
                    f"{self._point.point_type_id}-{self._point.point_id}"
                )
                self._abort_if_unique_id_configured()
            return await self.async_step_station()

        # Build the dropdown options dict keyed by point_id (as str for the selector).
        options = [
            {"value": str(p.point_id), "label": _mountain_label(p)}
            for p in all_mountain
        ]

        # Pre-select: on reconfigure use the current entry's point; on setup
        # use the nearest mountain point to the HA location.
        default_id: str | vol.Undefined = vol.UNDEFINED
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            if int(entry.data.get(CONF_POINT_TYPE_ID, 0)) == POINT_TYPE_MOUNTAIN:
                default_id = str(entry.data[CONF_POINT_ID])
        if default_id is vol.UNDEFINED:
            try:
                near = nearest_point(
                    self._all_points,
                    self.hass.config.latitude,
                    self.hass.config.longitude,
                    point_type=POINT_TYPE_MOUNTAIN,
                )
                default_id = str(near.point_id)
            except Exception:  # noqa: BLE001
                pass

        point_key = vol.Required(CONF_POINT_ID, default=default_id)

        return self.async_show_form(
            step_id="mountain",
            data_schema=vol.Schema(
                {
                    point_key: SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step: choose among multiple forecast points for the postal code.

        Skipped when there is exactly one point for the given postal code.
        """
        if user_input is not None:
            point_id = int(user_input[CONF_POINT_ID])
            self._point = next(
                p for p in self._point_choices if p.point_id == point_id
            )
            # Reconfigure resolves duplicates itself (excluding the entry being
            # reconfigured) when it finalises; only the setup flow aborts here.
            if self.source != SOURCE_RECONFIGURE:
                await self.async_set_unique_id(
                    f"{self._point.point_type_id}-{self._point.point_id}"
                )
                self._abort_if_unique_id_configured()
            return await self.async_step_station()

        options = {p.point_id: p.name for p in self._point_choices}
        point_key = vol.Required(CONF_POINT_ID)
        if self.source == SOURCE_RECONFIGURE:
            current_point_id = int(self._get_reconfigure_entry().data[CONF_POINT_ID])
            if current_point_id in options:
                point_key = vol.Required(CONF_POINT_ID, default=current_point_id)
        return self.async_show_form(
            step_id="point",
            data_schema=vol.Schema({point_key: vol.In(options)}),
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step: choose a SwissMetNet station and, optionally, a precip station.

        The main station (3 nearest, nearest pre-selected) supplies every
        current value. A second field offers the three nearest precipitation-only
        stations (ADR-0006), **none selected by default**; when set, only the
        precipitation reading is sourced from it.
        """
        assert self._point is not None
        assert self._all_stations is not None

        nearby = nearest_stations(
            self._all_stations, self._point.lat, self._point.lon, limit=3
        )
        precip_nearby = self._nearest_precip_stations()

        if user_input is not None:
            abbr = user_input[CONF_STATION_ABBR]
            station = next(s for s in nearby if s.abbr == abbr)
            precip_data = self._resolve_precip(user_input, precip_nearby)
            if self.source == SOURCE_RECONFIGURE:
                return await self._async_reconfigure_station(station, precip_data)
            return self.async_create_entry(
                title=self._point.name,
                data={
                    CONF_POINT_ID: self._point.point_id,
                    CONF_POINT_TYPE_ID: self._point.point_type_id,
                    CONF_POSTAL_CODE: self._point.postal_code,
                    CONF_POINT_NAME: self._point.name,
                    CONF_STATION_ABBR: station.abbr,
                    CONF_STATION_NAME: station.name,
                    **precip_data,
                },
            )

        options = {s.abbr: f"{s.name} ({s.canton})" for s in nearby}
        default_abbr = nearby[0].abbr if nearby else vol.UNDEFINED
        # On reconfigure, pre-select the currently configured station when it is
        # still among the three nearest.
        if self.source == SOURCE_RECONFIGURE:
            current_abbr = self._get_reconfigure_entry().data.get(CONF_STATION_ABBR)
            if any(s.abbr == current_abbr for s in nearby):
                default_abbr = current_abbr

        schema: dict[Any, Any] = {
            vol.Required(CONF_STATION_ABBR, default=default_abbr): vol.In(options)
        }
        # Optional precipitation station: only offered when the network's
        # metadata loaded. Defaults to "none" — the feature is opt-in (ADR-0006).
        if precip_nearby:
            precip_options = {_PRECIP_NONE: "None (use the main station)"}
            precip_options.update(
                {s.abbr: f"{s.name} ({s.canton})" for s in precip_nearby}
            )
            precip_default = _PRECIP_NONE
            if self.source == SOURCE_RECONFIGURE:
                current_precip = self._get_reconfigure_entry().data.get(
                    CONF_PRECIP_STATION_ABBR, ""
                )
                if current_precip and any(
                    s.abbr == current_precip for s in precip_nearby
                ):
                    precip_default = current_precip
            schema[
                vol.Required(CONF_PRECIP_STATION_ABBR, default=precip_default)
            ] = vol.In(precip_options)

        # The description always references {radar_hint}; supply it in both
        # cases (empty when the radar integration is already installed) so the
        # frontend never renders an unfilled placeholder.
        radar_hint = ""
        if "meteoswiss_radar" not in self.hass.config.components:
            radar_hint = (
                " The animated radar is available in the separate "
                "MeteoSwiss Radar integration (hass-meteoswiss-radar)."
            )
        description_placeholders = {"radar_hint": radar_hint}

        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(schema),
            description_placeholders=description_placeholders,
        )

    def _nearest_precip_stations(self) -> list[Station]:
        """The three nearest precipitation stations to the point, or empty.

        Empty when the precipitation metadata was unavailable (the pick is then
        dropped) — the feature is opt-in and must never block the station step.
        """
        assert self._point is not None
        if not self._all_precip_stations:
            return []
        return nearest_stations(
            self._all_precip_stations, self._point.lat, self._point.lon, limit=3
        )

    @staticmethod
    def _resolve_precip(
        user_input: dict[str, Any], precip_nearby: list[Station]
    ) -> dict[str, str]:
        """Map the station step's precip pick to the entry keys (ADR-0006).

        Returns empty abbreviation/name when the "none" sentinel was chosen or
        the field was absent, so the feature stays off.
        """
        chosen = str(user_input.get(CONF_PRECIP_STATION_ABBR, _PRECIP_NONE) or "")
        precip = next((s for s in precip_nearby if s.abbr == chosen), None)
        return {
            CONF_PRECIP_STATION_ABBR: precip.abbr if precip else "",
            CONF_PRECIP_STATION_NAME: precip.name if precip else "",
        }

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure entry point (A9, #52): choose mode, then re-run point/station.

        Pre-fills the mode from the current entry's point type. Routes to
        ``async_step_postal_code`` or ``async_step_mountain``, both shared
        with the setup flow.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if not await self._load_metadata():
            errors["base"] = "cannot_connect"
            current_type = int(entry.data.get(CONF_POINT_TYPE_ID, 2))
            is_mountain = current_type == POINT_TYPE_MOUNTAIN
            default_mode = _MODE_MOUNTAIN if is_mountain else _MODE_POSTAL_CODE
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_mode_schema(default_mode),
                errors=errors,
            )

        if user_input is not None:
            mode = user_input[_CONF_MODE]
            if mode == _MODE_MOUNTAIN:
                return await self.async_step_mountain()
            return await self.async_step_postal_code()

        current_type = int(entry.data.get(CONF_POINT_TYPE_ID, 2))
        is_mountain = current_type == POINT_TYPE_MOUNTAIN
        default_mode = _MODE_MOUNTAIN if is_mountain else _MODE_POSTAL_CODE
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_mode_schema(default_mode),
            errors=errors,
        )

    async def _async_reconfigure_station(
        self, station: Station, precip_data: dict[str, str]
    ) -> ConfigFlowResult:
        """Resolve the station pick on reconfigure and route the history choice.

        Rejects a point that already belongs to another entry. When the station
        actually changed, defers to the history-choice step; otherwise finishes
        immediately (a point-only change never touches history). The optional
        precipitation pick (ADR-0006) is carried on the new data either way — it
        never triggers the history step, which is about the main station only.
        """
        assert self._point is not None
        entry = self._get_reconfigure_entry()

        new_unique_id = f"{self._point.point_type_id}-{self._point.point_id}"
        # Reconfiguring onto a point another entry already owns is a duplicate.
        for other in self._async_current_entries():
            if other.entry_id != entry.entry_id and other.unique_id == new_unique_id:
                return self.async_abort(reason="already_configured")

        new_data = {
            CONF_POINT_ID: self._point.point_id,
            CONF_POINT_TYPE_ID: self._point.point_type_id,
            CONF_POSTAL_CODE: self._point.postal_code,
            CONF_POINT_NAME: self._point.name,
            CONF_STATION_ABBR: station.abbr,
            CONF_STATION_NAME: station.name,
            **precip_data,
        }

        if station.abbr == entry.data.get(CONF_STATION_ABBR):
            # Station unchanged: a point/postal-only change leaves history alone.
            return self.async_update_reload_and_abort(
                entry, unique_id=new_unique_id, data=new_data
            )

        # Station changed: ask what to do with the recorded history.
        self._new_station = station
        self._pending_data = new_data
        self._pending_unique_id = new_unique_id
        self._old_station_name = str(entry.data.get(CONF_STATION_NAME, ""))
        return await self.async_step_history()

    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """History choice on a station change: keep, discard or backfill (A9).

        The backfill choice is only offered once the recorder-import layer lands
        (``BACKFILL_AVAILABLE``); until then keep/discard ship.
        """
        assert self._new_station is not None
        assert self._pending_data is not None
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            choice = user_input[CONF_HISTORY_ACTION]
            if choice == HISTORY_DISCARD:
                await async_discard_station_history(self.hass, entry)
            elif choice == HISTORY_KEEP:
                async_log_station_switch(
                    self.hass,
                    entry,
                    self._old_station_name,
                    self._new_station.name,
                )
            # HISTORY_BACKFILL is unreachable while BACKFILL_AVAILABLE is False;
            # its recorder-import path is wired with issue #51's follow-up.
            return self.async_update_reload_and_abort(
                entry,
                unique_id=self._pending_unique_id,
                data=self._pending_data,
            )

        actions = [HISTORY_KEEP, HISTORY_DISCARD]
        if BACKFILL_AVAILABLE:
            actions.append(HISTORY_BACKFILL)
        return self.async_show_form(
            step_id="history",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HISTORY_ACTION, default=HISTORY_KEEP
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=actions,
                            translation_key="history_action",
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={
                "old_station": self._old_station_name,
                "new_station": self._new_station.name,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return MeteoSwissWeatherOptionsFlow()


def _horizon_label(days: int) -> str:
    """Human label for a horizon choice in the options select."""
    if days == HOURLY_HORIZON_FULL_RUN:
        return "Full run (all ~220 h)"
    if days == 0:
        return "Rest of today only (0 days ahead)"
    if days == 1:
        return "Today plus 1 full day"
    return f"Today plus {days} full days"


# The forecast fields each hourly option adds, as user-facing labels. Only the
# toggleable fields appear here: the daily forecast is always on, so it never
# shows up in the added/removed diff of the summary step (issue #145). The labels
# are dynamic values passed through ``description_placeholders`` — the surrounding
# prose is translated in strings.json, following the overview page's pattern.
_HOURLY_BASE_FIELDS: tuple[str, ...] = (
    "hourly condition",
    "hourly temperature",
    "hourly precipitation",
    "hourly precipitation probability",
    "hourly wind speed",
    "hourly wind gusts",
    "hourly wind direction",
    "hourly global radiation",
    "hourly zero-degree level",
)
_CLOUD_FIELDS: tuple[str, ...] = (
    "cloud coverage",
    "high/mid/low cloud layers",
)
_PERCENTILE_FIELDS: tuple[str, ...] = (
    "temperature p10",
    "temperature p90",
)


def _forecast_fields(
    *, hourly: bool, cloud_layers: bool, temp_percentiles: bool
) -> list[str]:
    """The toggleable forecast fields a hourly options combination produces."""
    fields: list[str] = []
    if hourly:
        fields.extend(_HOURLY_BASE_FIELDS)
        if cloud_layers:
            fields.extend(_CLOUD_FIELDS)
        if temp_percentiles:
            fields.extend(_PERCENTILE_FIELDS)
    return fields


def _format_bytes(num: int) -> str:
    """Human-readable byte size (KB below 1 MB, else MB) for the summary text."""
    if num < 1_000_000:
        return f"{num / 1_000:.0f} KB"
    return f"{num / 1_000_000:.2f} MB"


def _confidence_label(estimate: TrafficEstimate) -> str:
    """Whether the figures are measured, estimated, or a mix (issue #145)."""
    if estimate.all_measured:
        return "measured"
    if estimate.any_measured:
        return "measured where the file is already active, otherwise estimated"
    return "estimated"


def _traffic_text(estimate: TrafficEstimate) -> str:
    """The per-refresh and per-day traffic sentence for the summary placeholders."""
    confidence = _confidence_label(estimate)
    per_refresh = _format_bytes(estimate.bytes_per_refresh)
    per_day = _format_bytes(estimate.bytes_per_day)
    if estimate.has_unbounded:
        # At this horizon the date-major files no longer fit row addressing and
        # fall to a large prefix of the ~30 MB files (ADR-0008 §4): there is no
        # fixed number to quote, so state the consequence honestly (issue #145).
        return (
            f"about {per_refresh} per refresh for the row-addressed files "
            f"(~{per_day} per day, {confidence}), plus the temperature and any "
            "cloud or percentile files, which at this horizon no longer fit row "
            "addressing and are fetched as a large prefix of the ~30 MB source "
            "files — not a fixed number."
        )
    return (
        f"about {per_refresh} per refresh, roughly {per_day} per day "
        f"(~{estimate.refreshes_per_day} refreshes; {confidence})."
    )


class MeteoSwissWeatherOptionsFlow(OptionsFlow):
    """Options flow: hourly forecast (ADR-0002) and pollen opt-in (ADR-0005).

    Presented as a menu (issue #144) rather than a hidden wizard:

      init     — a menu with three entries
      hourly   — toggle, horizon, cloud layers and percentiles on one page;
                 the fields are simply ignored when the toggle is off
      summary  — confirmation after the hourly page (issue #145): the forecast
                 fields the choice adds or removes and its estimated traffic per
                 refresh and per day, before the change is saved
      pollen   — toggle and station on one page
      overview — a read-only summary of what is on now, which forecast fields
                 that produces, and how many of the entry's entities are
                 currently disabled in the entity registry

    Each page merges its own keys into the stored options and leaves the other
    page's keys untouched, so saving one never resets the other. The stored keys
    and their semantics are unchanged, so no migration is needed.
    """

    def __init__(self) -> None:
        # The pollen station list is fetched once and cached on the instance.
        self._pollen_stations: list[PollenStation] | None = None
        self._pollen_ref_lat: float = 0.0
        self._pollen_ref_lon: float = 0.0
        # The hourly page's chosen changes, held while the summary step confirms
        # them before they are saved (issue #145).
        self._pending_hourly: dict[str, Any] | None = None

    def _merge_and_create(self, changes: dict[str, Any]) -> ConfigFlowResult:
        """Persist ``changes`` merged over the current options.

        Only the keys a page owns are passed in, so the other page's stored
        options survive untouched (issue #144 acceptance).
        """
        data = dict(self.config_entry.options)
        data.update(changes)
        return self.async_create_entry(data=data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu (issue #144)."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["hourly", "pollen", "overview"],
        )

    async def async_step_hourly(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Hourly forecast page: toggle, horizon and the B9/B11 gated additions.

        The cloud-layer and percentile toggles live here because each only makes
        sense with the hourly forecast on, and each turns on extra expensive
        date-major files (issue #69, ADR-0002 gating). When the hourly toggle is
        off the gated additions are stored off regardless of their field values,
        matching how the runtime gates them (``__init__.py``).
        """
        current = self.config_entry.options
        if user_input is not None:
            hourly = bool(user_input[CONF_HOURLY_FORECAST])
            # Stash the normalised changes and route to the summary step, which
            # spells out the field and traffic consequences before saving (#145).
            self._pending_hourly = {
                CONF_HOURLY_FORECAST: hourly,
                CONF_HOURLY_HORIZON_DAYS: int(user_input[CONF_HOURLY_HORIZON_DAYS]),
                CONF_HOURLY_CLOUD_LAYERS: hourly
                and bool(user_input[CONF_HOURLY_CLOUD_LAYERS]),
                CONF_HOURLY_TEMP_PERCENTILES: hourly
                and bool(user_input[CONF_HOURLY_TEMP_PERCENTILES]),
            }
            return await self.async_step_summary()

        choices = {days: _horizon_label(days) for days in HOURLY_HORIZON_CHOICES}
        return self.async_show_form(
            step_id="hourly",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOURLY_FORECAST,
                        default=current.get(CONF_HOURLY_FORECAST, False),
                    ): bool,
                    vol.Required(
                        CONF_HOURLY_HORIZON_DAYS,
                        default=current.get(
                            CONF_HOURLY_HORIZON_DAYS, DEFAULT_HOURLY_HORIZON_DAYS
                        ),
                    ): vol.In(choices),
                    vol.Required(
                        CONF_HOURLY_CLOUD_LAYERS,
                        default=current.get(CONF_HOURLY_CLOUD_LAYERS, False),
                    ): bool,
                    vol.Required(
                        CONF_HOURLY_TEMP_PERCENTILES,
                        default=current.get(CONF_HOURLY_TEMP_PERCENTILES, False),
                    ): bool,
                }
            ),
        )

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirmation step after the hourly page (issue #145).

        A form with no fields: it states, through ``description_placeholders``,
        which forecast fields the chosen hourly options add or remove compared
        with the current ones, and the estimated traffic per refresh and per day
        for the combination. Submitting it saves the pending changes; the entry
        then reloads with the new feature set (``__init__.py`` update listener).
        """
        assert self._pending_hourly is not None
        if user_input is not None:
            return self._merge_and_create(self._pending_hourly)

        return self.async_show_form(
            step_id="summary",
            data_schema=vol.Schema({}),
            description_placeholders=self._summary_placeholders(),
        )

    def _summary_placeholders(self) -> dict[str, str]:
        """Build the summary step's field-diff and traffic placeholders (#145)."""
        assert self._pending_hourly is not None
        current = self.config_entry.options
        pending = self._pending_hourly

        current_fields = _forecast_fields(
            hourly=bool(current.get(CONF_HOURLY_FORECAST, False)),
            cloud_layers=bool(current.get(CONF_HOURLY_CLOUD_LAYERS, False)),
            temp_percentiles=bool(current.get(CONF_HOURLY_TEMP_PERCENTILES, False)),
        )
        pending_fields = _forecast_fields(
            hourly=bool(pending[CONF_HOURLY_FORECAST]),
            cloud_layers=bool(pending[CONF_HOURLY_CLOUD_LAYERS]),
            temp_percentiles=bool(pending[CONF_HOURLY_TEMP_PERCENTILES]),
        )
        added = [f for f in pending_fields if f not in current_fields]
        removed = [f for f in current_fields if f not in pending_fields]

        estimate = estimate_traffic(
            hourly=bool(pending[CONF_HOURLY_FORECAST]),
            horizon_days=int(pending[CONF_HOURLY_HORIZON_DAYS]),
            cloud_layers=bool(pending[CONF_HOURLY_CLOUD_LAYERS]),
            temp_percentiles=bool(pending[CONF_HOURLY_TEMP_PERCENTILES]),
            measured_bytes=self._measured_bytes(),
        )

        return {
            "added": ", ".join(added) if added else "none",
            "removed": ", ".join(removed) if removed else "none",
            "traffic": _traffic_text(estimate),
        }

    def _measured_bytes(self) -> dict[str, int]:
        """Measured fetch bytes per parameter from the loaded entry, if any.

        The estimate prefers a file's last measured fetch over its table figure
        (issue #145). The numbers live in the forecast coordinator's store, on
        ``entry.runtime_data`` — absent when the entry is not currently loaded,
        in which case the estimate is entirely from the table.
        """
        runtime = getattr(self.config_entry, "runtime_data", None)
        try:
            return runtime.forecast_coordinator.store.measured_bytes()
        except AttributeError:
            return {}

    async def async_step_pollen(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pollen page: toggle and station on one page (issue #144).

        The station picker offers the three nearest pollen stations to the
        configured forecast point (fetched once, cached on the instance), with
        the current or nearest one pre-selected. A submission that carries the
        toggle is saved without (re)fetching, so pollen can be turned off even
        when the OGD service is unreachable; showing the picker needs the fetch.
        """
        current = self.config_entry.options

        # A submission carrying the toggle is saved directly: the station list
        # is not needed to store the choice, and the picker's fetch may be down.
        if user_input is not None and CONF_POLLEN in user_input:
            changes: dict[str, Any] = {CONF_POLLEN: bool(user_input[CONF_POLLEN])}
            if CONF_POLLEN_STATION in user_input:
                changes[CONF_POLLEN_STATION] = str(user_input[CONF_POLLEN_STATION])
            return self._merge_and_create(changes)

        # Showing the form (or retrying after a fetch error): load the stations.
        errors: dict[str, str] = {}
        if self._pollen_stations is None:
            session = async_get_clientsession(self.hass)
            try:
                self._pollen_stations = await fetch_pollen_stations(session)
                all_points = await fetch_points(session)
                point_id = int(self.config_entry.data.get(CONF_POINT_ID, 0))
                point_type_id = int(self.config_entry.data.get(CONF_POINT_TYPE_ID, 0))
                ref = next(
                    (
                        p
                        for p in all_points
                        if p.point_id == point_id and p.point_type_id == point_type_id
                    ),
                    None,
                )
                self._pollen_ref_lat = (
                    ref.lat if ref and ref.lat else self.hass.config.latitude
                )
                self._pollen_ref_lon = (
                    ref.lon if ref and ref.lon else self.hass.config.longitude
                )
            except OgdError:
                errors["base"] = "cannot_connect"

        toggle = vol.Required(CONF_POLLEN, default=current.get(CONF_POLLEN, False))
        if self._pollen_stations is None:
            # Offline: still offer the toggle so pollen can be turned off, but
            # drop the station picker we could not build.
            return self.async_show_form(
                step_id="pollen",
                data_schema=vol.Schema({toggle: bool}),
                errors=errors,
            )

        nearby = nearest_pollen_stations(
            self._pollen_stations,
            self._pollen_ref_lat,
            self._pollen_ref_lon,
            limit=3,
        )
        options = {s.abbr: f"{s.name} ({s.canton})" for s in nearby}
        current_abbr = current.get(CONF_POLLEN_STATION)
        default_abbr: str | vol.Undefined = (
            current_abbr
            if current_abbr and current_abbr in options
            else (nearby[0].abbr if nearby else vol.UNDEFINED)
        )

        return self.async_show_form(
            step_id="pollen",
            data_schema=vol.Schema(
                {
                    toggle: bool,
                    vol.Required(CONF_POLLEN_STATION, default=default_abbr): vol.In(
                        options
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_overview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read-only summary of the entry's current configuration (issue #144).

        A form with no fields: submitting it changes nothing and returns to the
        menu. The dynamic parts are supplied through ``description_placeholders``
        — what is on now, which forecast fields that produces, and how many of
        the entry's entities are currently disabled in the entity registry.
        """
        if user_input is not None:
            return await self.async_step_init()

        return self.async_show_form(
            step_id="overview",
            data_schema=vol.Schema({}),
            description_placeholders=self._overview_placeholders(),
        )

    def _overview_placeholders(self) -> dict[str, str]:
        """Build the overview page's description placeholders.

        The surrounding prose is translated in ``strings.json``; the values here
        are the dynamic, non-translatable facts (like ``radar_hint`` on the
        setup station step): the current on/off state, the forecast fields it
        produces, and the count of disabled entities.
        """
        current = self.config_entry.options
        hourly = bool(current.get(CONF_HOURLY_FORECAST, False))
        pollen = bool(current.get(CONF_POLLEN, False))
        cloud = hourly and bool(current.get(CONF_HOURLY_CLOUD_LAYERS, False))
        percentiles = hourly and bool(current.get(CONF_HOURLY_TEMP_PERCENTILES, False))

        active_lines: list[str] = []
        if hourly:
            horizon = int(
                current.get(CONF_HOURLY_HORIZON_DAYS, DEFAULT_HOURLY_HORIZON_DAYS)
            )
            extras = []
            if cloud:
                extras.append("cloud layers")
            if percentiles:
                extras.append("temperature percentiles")
            extra = f"; extras: {', '.join(extras)}" if extras else ""
            active_lines.append(
                f"- Hourly forecast: on ({_horizon_label(horizon).lower()}{extra})"
            )
        else:
            active_lines.append("- Hourly forecast: off")
        if pollen:
            station = str(current.get(CONF_POLLEN_STATION, "")) or "none selected"
            active_lines.append(f"- Pollen monitoring: on (station {station})")
        else:
            active_lines.append("- Pollen monitoring: off")

        field_lines = [
            "- Daily forecast (always on): 9 days of high/low temperature, "
            "precipitation and its probability, wind and condition"
        ]
        if hourly:
            hourly_fields = (
                "condition, temperature, precipitation and its probability, "
                "wind, gusts, direction, global radiation, zero-degree level"
            )
            if cloud:
                hourly_fields += ", cloud coverage (and the high/mid/low layers)"
            if percentiles:
                hourly_fields += ", temperature p10/p90"
            field_lines.append(f"- Hourly forecast fields: {hourly_fields}")

        registry = er.async_get(self.hass)
        entities = er.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        )
        disabled_count = sum(1 for entity in entities if entity.disabled)

        return {
            "active": "\n".join(active_lines),
            "forecast_fields": "\n".join(field_lines),
            "disabled_count": str(disabled_count),
        }
