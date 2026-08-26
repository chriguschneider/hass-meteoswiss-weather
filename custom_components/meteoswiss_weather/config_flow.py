"""Config flow for the MeteoSwiss Weather integration.

Scaffold: asks for a Swiss postal code and stores it. The full flow
(nearest local-forecast point and SwissMetNet station derived from the
Home Assistant location, with an override) is a tracked issue.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_POSTAL_CODE, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POSTAL_CODE): vol.All(
            vol.Coerce(int), vol.Range(min=1000, max=9999)
        ),
    }
)


class MeteoSwissWeatherConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the postal code."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

        postal_code = user_input[CONF_POSTAL_CODE]
        await self.async_set_unique_id(str(postal_code))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"MeteoSwiss {postal_code}",
            data={CONF_POSTAL_CODE: postal_code},
        )
