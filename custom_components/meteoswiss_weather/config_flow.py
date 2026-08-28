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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    BACKFILL_AVAILABLE,
    CONF_HISTORY_ACTION,
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_HORIZON_DAYS,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
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
from .history import async_discard_station_history, async_log_station_switch
from .ogd import (
    POINT_TYPE_MOUNTAIN,
    ForecastPoint,
    OgdError,
    Station,
    fetch_points,
    fetch_stations,
    mountain_points,
    nearest_point,
    nearest_stations,
    points_for_postal_code,
)

# Flow-internal mode constants; never stored in the config entry.
_CONF_MODE = "forecast_mode"
_MODE_POSTAL_CODE = "postal_code"
_MODE_MOUNTAIN = "mountain"


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
        """Step: choose a SwissMetNet station (3 nearest, nearest pre-selected)."""
        assert self._point is not None
        assert self._all_stations is not None

        nearby = nearest_stations(
            self._all_stations, self._point.lat, self._point.lon, limit=3
        )

        if user_input is not None:
            abbr = user_input[CONF_STATION_ABBR]
            station = next(s for s in nearby if s.abbr == abbr)
            if self.source == SOURCE_RECONFIGURE:
                return await self._async_reconfigure_station(station)
            return self.async_create_entry(
                title=self._point.name,
                data={
                    CONF_POINT_ID: self._point.point_id,
                    CONF_POINT_TYPE_ID: self._point.point_type_id,
                    CONF_POSTAL_CODE: self._point.postal_code,
                    CONF_POINT_NAME: self._point.name,
                    CONF_STATION_ABBR: station.abbr,
                    CONF_STATION_NAME: station.name,
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
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION_ABBR, default=default_abbr): vol.In(
                        options
                    )
                }
            ),
            description_placeholders=description_placeholders,
        )

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
        self, station: Station
    ) -> ConfigFlowResult:
        """Resolve the station pick on reconfigure and route the history choice.

        Rejects a point that already belongs to another entry. When the station
        actually changed, defers to the history-choice step; otherwise finishes
        immediately (a point-only change never touches history).
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


class MeteoSwissWeatherOptionsFlow(OptionsFlow):
    """Options flow: the hourly forecast opt-in and its horizon (ADR-0002).

    Two steps so the horizon is only shown when the hourly forecast is on
    (issue #50): enabling it leads to the horizon select; disabling it saves
    straight away and leaves the stored horizon (unused) untouched.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.options
        if user_input is not None:
            if user_input[CONF_HOURLY_FORECAST]:
                # Hourly on: the horizon step finishes the flow.
                return await self.async_step_hourly()
            # Hourly off: nothing more to ask; keep the previous horizon value.
            return self.async_create_entry(
                data={
                    CONF_HOURLY_FORECAST: False,
                    CONF_HOURLY_HORIZON_DAYS: current.get(
                        CONF_HOURLY_HORIZON_DAYS, DEFAULT_HOURLY_HORIZON_DAYS
                    ),
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOURLY_FORECAST,
                        default=current.get(CONF_HOURLY_FORECAST, False),
                    ): bool
                }
            ),
        )

    async def async_step_hourly(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step (hourly on): pick how far ahead the hourly forecast goes."""
        current = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_HOURLY_FORECAST: True,
                    CONF_HOURLY_HORIZON_DAYS: int(
                        user_input[CONF_HOURLY_HORIZON_DAYS]
                    ),
                }
            )

        choices = {days: _horizon_label(days) for days in HOURLY_HORIZON_CHOICES}
        return self.async_show_form(
            step_id="hourly",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOURLY_HORIZON_DAYS,
                        default=current.get(
                            CONF_HOURLY_HORIZON_DAYS, DEFAULT_HOURLY_HORIZON_DAYS
                        ),
                    ): vol.In(choices)
                }
            ),
        )
