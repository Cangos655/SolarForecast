"""Energy platform: hands the hourly forecast to the Energy Dashboard.

Home Assistant collects ``async_get_solar_forecast`` from every integration
that has an ``energy`` platform. Once this entry is chosen as the solar
production forecast of a solar source, the dashboard draws its curve.
"""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    """Return {"wh_hours": {hour start (ISO, UTC): Wh}} for a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
    data = getattr(coordinator, "data", None)
    if not data:
        return None

    return {
        "wh_hours": {
            datetime.fromtimestamp(epoch, timezone.utc).isoformat(): round(kwh * 1000)
            for epoch, kwh in data.get("hourly", [])
        }
    }
