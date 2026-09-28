"""Solar Forecast – solar yield forecasting integration for Home Assistant.

Bundles its own Lovelace card (see www/) and registers it automatically as
a frontend resource on setup, so no separate HACS frontend install is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CARD_FILENAME, CARD_URL_PATH, DOMAIN
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def _async_register_frontend_resources(hass: HomeAssistant) -> None:
    """Serve the bundled card from www/ and register it as an extra JS module.

    Idempotent: safe to call for every config entry, registration only
    happens once per HA run.
    """
    if hass.data.get(DOMAIN, {}).get("_frontend_registered"):
        return

    www_dir = Path(__file__).parent / "www"

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(www_dir), False)]
        )
    except ImportError:
        # Older Home Assistant core without StaticPathConfig
        hass.http.register_static_path(CARD_URL_PATH, str(www_dir), False)

    add_extra_js_url(hass, f"{CARD_URL_PATH}/{CARD_FILENAME}")
    hass.data.setdefault(DOMAIN, {})["_frontend_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Forecast from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    await _async_register_frontend_resources(hass)

    coordinator = SolarForecastCoordinator(hass, entry)
    await coordinator._load_history()
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update (e.g. sensor changed)."""
    await hass.config_entries.async_reload(entry.entry_id)
