"""Solar Forecast – solar yield forecasting integration for Home Assistant.

Bundles its own Lovelace card (see www/) and registers it automatically as
a frontend resource on setup, so no separate HACS frontend install is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, CARD_URL_PATH, DOMAIN, FORECAST_DAY_LABELS
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


def _get_lovelace_resources(hass: HomeAssistant):
    """Return the storage-mode Lovelace resource collection, else None."""
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    return resources if hasattr(resources, "async_create_item") else None


async def _async_sync_lovelace_resource(
    hass: HomeAssistant, url: str | None
) -> None:
    """Create/update (url given) or remove (url None) our dashboard resource.

    Dashboard resources are loaded before cards render, unlike extra JS
    modules, which race against HA's ~2s wait for a card element.
    """
    resources = _get_lovelace_resources(hass)
    if resources is None:
        return  # YAML mode or no Lovelace resources: extra JS URL still applies
    try:
        if not getattr(resources, "loaded", True):
            await resources.async_load()
            resources.loaded = True
        base = f"{CARD_URL_PATH}/{CARD_FILENAME}"
        ours = [i for i in resources.async_items() if i["url"].split("?")[0] == base]
        if url is None:
            for item in ours:
                await resources.async_delete_item(item["id"])
            return
        if not ours:
            await resources.async_create_item({"res_type": "module", "url": url})
            return
        if ours[0]["url"] != url:
            await resources.async_update_item(ours[0]["id"], {"url": url})
        for extra in ours[1:]:
            await resources.async_delete_item(extra["id"])
    except Exception:  # noqa: BLE001 - never block setup over a dashboard resource
        _LOGGER.warning("Could not sync Lovelace resource for the card", exc_info=True)


async def _async_register_frontend_resources(hass: HomeAssistant) -> None:
    """Serve the bundled card from www/ and register it as an extra JS module.

    Idempotent: safe to call for every config entry, registration only
    happens once per HA run.
    """
    if hass.data.get(DOMAIN, {}).get("_frontend_registered"):
        return

    www_dir = Path(__file__).parent / "www"
    version = (await async_get_integration(hass, DOMAIN)).version

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(www_dir), False)]
        )
    except ImportError:
        # Older Home Assistant core without StaticPathConfig
        hass.http.register_static_path(CARD_URL_PATH, str(www_dir), False)

    # Two deliberately different URLs: the browser loads each as its own
    # module. Page loads were observed receiving an empty (0 byte) response
    # for the card, which a shared URL would make permanent for both paths;
    # the card guards against double definition, so whichever loads wins.
    base_url = f"{CARD_URL_PATH}/{CARD_FILENAME}?v={version}"
    add_extra_js_url(hass, base_url)
    await _async_sync_lovelace_resource(hass, f"{base_url}&via=resource")
    hass.data.setdefault(DOMAIN, {})["_frontend_registered"] = True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry from an earlier version."""
    if entry.version == 1 and entry.minor_version < 2:
        _clear_forecast_statistics(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


def _clear_forecast_statistics(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the long-term statistics the forecast sensors left behind.

    Up to v1.0.9 they had a state class. Without this cleanup Home Assistant
    reports one repair per sensor for statistics nothing uses any more.
    """
    registry = er.async_get(hass)
    entity_ids = [
        entity_id
        for label in FORECAST_DAY_LABELS
        if (
            entity_id := registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{label}"
            )
        )
    ]
    if not entity_ids:
        return
    get_instance(hass).async_clear_statistics(entity_ids)
    for entity_id in entity_ids:
        ir.async_delete_issue(hass, "sensor", f"state_class_removed_{entity_id}")


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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the dashboard resource once the last entry is deleted."""
    remaining = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
    ]
    if not remaining:
        await _async_sync_lovelace_resource(hass, None)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update (e.g. sensor changed)."""
    await hass.config_entries.async_reload(entry.entry_id)
