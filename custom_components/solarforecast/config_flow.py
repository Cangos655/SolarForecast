"""Config flow for Solar Forecast integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CELL_TEMP_OFFSET,
    CONF_CITY,
    CONF_LATITUDE,
    CONF_LOCATION_MODE,
    CONF_LOCATION_NAME,
    CONF_LONGITUDE,
    CONF_SOLAR_SENSOR,
    CONF_TEMP_COEFFICIENT,
    DEFAULT_CELL_TEMP_OFFSET,
    DEFAULT_TEMP_COEFFICIENT,
    DOMAIN,
    LOCATION_MODE_CITY,
    LOCATION_MODE_HOME,
)
from .weather_api import search_location

_LOGGER = logging.getLogger(__name__)


async def _get_energy_sensors(hass: HomeAssistant) -> dict[str, str]:
    """Return {entity_id: friendly_name} for all energy sensors in HA."""
    entity_reg = er.async_get(hass)
    sensors: dict[str, str] = {}

    for state in hass.states.async_all("sensor"):
        entity_id = state.entity_id
        device_class = state.attributes.get("device_class")
        state_class = state.attributes.get("state_class")
        unit = state.attributes.get("unit_of_measurement", "")

        if device_class == SensorDeviceClass.ENERGY or (
            state_class in ("total", "total_increasing") and "kWh" in unit
        ):
            entry = entity_reg.async_get(entity_id)
            friendly_name = state.attributes.get("friendly_name", entity_id)
            if entry and entry.name:
                friendly_name = entry.name
            sensors[entity_id] = f"{friendly_name} ({entity_id})"

    return sensors


class SolarForecastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Solar Forecast config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._location_results: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Choose location mode and solar sensor."""
        errors: dict[str, str] = {}
        energy_sensors = await _get_energy_sensors(self.hass)

        if not energy_sensors:
            errors["base"] = "no_energy_sensors"

        if user_input is not None and not errors:
            location_mode = user_input[CONF_LOCATION_MODE]
            self._data.update(user_input)

            if location_mode == LOCATION_MODE_HOME:
                self._data[CONF_LATITUDE] = self.hass.config.latitude
                self._data[CONF_LONGITUDE] = self.hass.config.longitude
                self._data[CONF_LOCATION_NAME] = self.hass.config.location_name or "Home"
                return await self.async_step_advanced()

            # City search mode
            city = user_input.get(CONF_CITY, "").strip()
            if not city:
                errors[CONF_CITY] = "city_required"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    results = await search_location(session, city)
                    if not results:
                        errors[CONF_CITY] = "city_not_found"
                    elif len(results) == 1:
                        self._data[CONF_LATITUDE] = results[0]["latitude"]
                        self._data[CONF_LONGITUDE] = results[0]["longitude"]
                        self._data[CONF_LOCATION_NAME] = results[0]["display_name"]
                        return await self.async_step_advanced()
                    else:
                        self._location_results = results
                        return await self.async_step_pick_location()
                except aiohttp.ClientError:
                    errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LOCATION_MODE, default=LOCATION_MODE_HOME
                ): vol.In(
                    {
                        LOCATION_MODE_HOME: "Use HA home location",
                        LOCATION_MODE_CITY: "Search for a city",
                    }
                ),
                vol.Optional(CONF_CITY, default=""): str,
                vol.Required(CONF_SOLAR_SENSOR): vol.In(energy_sensors),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_pick_location(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1b: Let user pick from multiple city results."""
        if user_input is not None:
            chosen = user_input["location"]
            for r in self._location_results:
                if r["display_name"] == chosen:
                    self._data[CONF_LATITUDE] = r["latitude"]
                    self._data[CONF_LONGITUDE] = r["longitude"]
                    self._data[CONF_LOCATION_NAME] = r["display_name"]
                    break
            return await self.async_step_advanced()

        choices = {r["display_name"]: r["display_name"] for r in self._location_results}
        schema = vol.Schema({vol.Required("location"): vol.In(choices)})
        return self.async_show_form(step_id="pick_location", data_schema=schema)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Skipped during setup – defaults are applied automatically.

        Advanced parameters (temp coefficient, cell temp offset) are available
        for adjustment via the Options Flow (gear icon) after setup.
        """
        self._data.setdefault(CONF_TEMP_COEFFICIENT, DEFAULT_TEMP_COEFFICIENT)
        self._data.setdefault(CONF_CELL_TEMP_OFFSET, DEFAULT_CELL_TEMP_OFFSET)
        title = self._data.get(CONF_LOCATION_NAME, "Solar Forecast")
        return self.async_create_entry(title=title, data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SolarForecastOptionsFlow:
        return SolarForecastOptionsFlow(config_entry)


class SolarForecastOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to change the solar sensor and parameters after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        energy_sensors = await _get_energy_sensors(self.hass)

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Options override data – same merge as coordinator
        current = {**self._config_entry.data, **self._config_entry.options}

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SOLAR_SENSOR,
                    default=current.get(CONF_SOLAR_SENSOR, ""),
                ): vol.In(energy_sensors),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
