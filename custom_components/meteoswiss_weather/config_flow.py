"""Config flow for the MeteoSwiss Weather integration.

Three-step setup: the user confirms a postal code (pre-filled from the HA
location), optionally picks among multiple forecast points for that postal
code, then picks a SwissMetNet station. All choices are derived from the
official open data (ADR-0001); no app API is involved.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_HOURLY_FORECAST,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from .ogd import (
    ForecastPoint,
    OgdError,
    Station,
    fetch_points,
    fetch_stations,
    nearest_point,
    nearest_stations,
    points_for_postal_code,
)


def _user_schema(default_plz: int | None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_POSTAL_CODE,
                default=default_plz,
            ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=9999))
        }
    )


class MeteoSwissWeatherConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup for MeteoSwiss Weather."""

    VERSION = 1

    def __init__(self) -> None:
        self._all_points: list[ForecastPoint] | None = None
        self._all_stations: list[Station] | None = None
        self._point_choices: list[ForecastPoint] = []
        self._point: ForecastPoint | None = None

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
        """Step 1: postal code, pre-filled from the HA location."""
        errors: dict[str, str] = {}

        if not await self._load_metadata():
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(None),
                errors=errors,
            )

        if user_input is not None:
            postal_code = int(user_input[CONF_POSTAL_CODE])
            candidates = points_for_postal_code(self._all_points, postal_code)
            if not candidates:
                errors["base"] = "unknown_postal_code"
            else:
                self._point_choices = candidates
                if len(candidates) == 1:
                    self._point = candidates[0]
                    await self.async_set_unique_id(
                        f"{self._point.point_type_id}-{self._point.point_id}"
                    )
                    self._abort_if_unique_id_configured()
                    return await self.async_step_station()
                return await self.async_step_point()

        # Suggest the postal code of the nearest forecast point to the HA location.
        suggested_plz: int | None = None
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
            step_id="user",
            data_schema=_user_schema(suggested_plz),
            errors=errors,
        )

    async def async_step_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: choose among multiple forecast points for the postal code.

        Skipped when there is exactly one point for the given postal code.
        """
        if user_input is not None:
            point_id = int(user_input[CONF_POINT_ID])
            self._point = next(
                p for p in self._point_choices if p.point_id == point_id
            )
            await self.async_set_unique_id(
                f"{self._point.point_type_id}-{self._point.point_id}"
            )
            self._abort_if_unique_id_configured()
            return await self.async_step_station()

        options = {p.point_id: p.name for p in self._point_choices}
        return self.async_show_form(
            step_id="point",
            data_schema=vol.Schema({vol.Required(CONF_POINT_ID): vol.In(options)}),
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: choose a SwissMetNet station (3 nearest, nearest pre-selected)."""
        assert self._point is not None
        assert self._all_stations is not None

        nearby = nearest_stations(
            self._all_stations, self._point.lat, self._point.lon, limit=3
        )

        if user_input is not None:
            abbr = user_input[CONF_STATION_ABBR]
            station = next(s for s in nearby if s.abbr == abbr)
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return MeteoSwissWeatherOptionsFlow()


class MeteoSwissWeatherOptionsFlow(OptionsFlow):
    """Options flow: toggle the hourly forecast opt-in (ADR-0002)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
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
