"""Solar Forecast sensor entities."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, FORECAST_DAY_LABELS, HOURLY_SENSOR_TICK
from .coordinator import SolarForecastCoordinator
from .hourly import best_window, energy_between, local_date, peak_hour, power_at

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Forecast sensors from a config entry."""
    coordinator: SolarForecastCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # Forecast sensors (today + 7 days)
    for i, label in enumerate(FORECAST_DAY_LABELS):
        entities.append(SolarForecastForecastSensor(coordinator, entry, i, label))

    # Meta sensors
    entities.append(SolarForecastAccuracySensor(coordinator, entry))
    entities.append(SolarForecastTrainingCountSensor(coordinator, entry))
    entities.append(SolarForecastConditionSensor(coordinator, entry))

    # Hourly sensors (1.1)
    entities.append(SolarForecastPowerNowSensor(coordinator, entry))
    entities.append(SolarForecastHourEnergySensor(coordinator, entry, 0, "energy_current_hour", "Energy Current Hour"))
    entities.append(SolarForecastHourEnergySensor(coordinator, entry, 1, "energy_next_hour", "Energy Next Hour"))
    entities.append(SolarForecastMorningHourSensor(coordinator, entry))
    entities.append(SolarForecastRemainingTodaySensor(coordinator, entry))
    entities.append(SolarForecastPeakTimeSensor(coordinator, entry, 0, "peak_time_today", "Peak Time Today"))
    entities.append(SolarForecastPeakTimeSensor(coordinator, entry, 1, "peak_time_tomorrow", "Peak Time Tomorrow"))
    entities.append(SolarForecastBestWindowSensor(coordinator, entry, 2))
    entities.append(SolarForecastBestWindowSensor(coordinator, entry, 3))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class SolarForecastBaseSensor(CoordinatorEntity[SolarForecastCoordinator], SensorEntity):
    """Base class for all Solar Forecast sensors."""

    def __init__(
        self,
        coordinator: SolarForecastCoordinator,
        entry: ConfigEntry,
        unique_suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_has_entity_name = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Solar Forecast",
            model="Solar Yield Forecaster",
            entry_type=DeviceEntryType.SERVICE,
        )


# ---------------------------------------------------------------------------
# Forecast sensors
# ---------------------------------------------------------------------------

class SolarForecastForecastSensor(SolarForecastBaseSensor):
    """Forecasted kWh yield for a specific day."""

    # The hourly series changes every hour; keep it out of the database.
    _unrecorded_attributes = frozenset({"hourly"})

    def __init__(
        self,
        coordinator: SolarForecastCoordinator,
        entry: ConfigEntry,
        day_index: int,
        label: str,
    ) -> None:
        pretty = label.replace("_", " ").title()
        super().__init__(coordinator, entry, label, f"Solar Forecast {pretty}")
        self._day_index = day_index
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        # No state class: a forecast is neither a present measurement nor a
        # running total, and energy sensors only allow total/total_increasing.
        self._attr_icon = "mdi:solar-power"

    @property
    def native_value(self) -> float | None:
        forecasts = (self.coordinator.data or {}).get("forecasts", [])
        if self._day_index < len(forecasts):
            return forecasts[self._day_index].get("expected_kwh")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        forecasts = (self.coordinator.data or {}).get("forecasts", [])
        if self._day_index >= len(forecasts):
            return {}
        day = forecasts[self._day_index]
        return {
            "hourly": _hourly_for_day(self.coordinator.data, day.get("date")),
            "date": day.get("date"),
            "weather_code": day.get("weather_code"),
            "radiation_mj_m2": day.get("radiation_sum"),
            "temp_max_c": day.get("temp_max"),
            "temp_min_c": day.get("temp_min"),
            "condition": day.get("condition"),
            "sunshine_hours": round(day.get("sunshine_duration", 0), 1),
            "daylight_hours": round(day.get("daylight_duration", 0), 1),
            "sunrise": day.get("sunrise"),
            "sunset": day.get("sunset"),
        }


# ---------------------------------------------------------------------------
# Meta sensors
# ---------------------------------------------------------------------------

class SolarForecastAccuracySensor(SolarForecastBaseSensor):
    """Model training accuracy in percent."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "model_accuracy", "Solar Forecast Model Accuracy")
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:brain"

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("model_accuracy")


class SolarForecastTrainingCountSensor(SolarForecastBaseSensor):
    """Number of real training entries."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "training_count", "Solar Forecast Training Count")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("training_count")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        per_bucket = (self.coordinator.data or {}).get("training_per_bucket", {})
        history = (self.coordinator.data or {}).get("training_history", [])
        return {
            "sunny": per_bucket.get("sunny", 0),
            "mixed": per_bucket.get("mixed", 0),
            "overcast": per_bucket.get("overcast", 0),
            "max_per_bucket": 10,
            "history": history,
        }


class SolarForecastConditionSensor(SolarForecastBaseSensor):
    """Today's weather condition bucket (sunny / mixed / overcast)."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "today_condition", "Solar Forecast Today Condition")
        self._attr_icon = "mdi:weather-partly-cloudy"

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("today_condition")


# ---------------------------------------------------------------------------
# Hourly sensors
# ---------------------------------------------------------------------------

def _tz(data: dict[str, Any] | None) -> ZoneInfo:
    return ZoneInfo((data or {}).get("timezone") or "UTC")


def _series(data: dict[str, Any] | None) -> list[tuple[int, float]]:
    return [(int(e), float(k)) for e, k in (data or {}).get("hourly", [])]


def _iso(epoch: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(epoch, tz).isoformat()


def _hourly_for_day(data: dict[str, Any] | None, day_str: str | None) -> list[dict[str, Any]]:
    """Hourly series of one local day for the card: [{start, kwh}, ...]."""
    if not day_str:
        return []
    tz = _tz(data)
    day = date.fromisoformat(day_str)
    return [
        {"start": _iso(e, tz), "kwh": round(k, 3)}
        for e, k in _series(data)
        if local_date(e, tz) == day
    ]


class SolarForecastHourlyBaseSensor(SolarForecastBaseSensor):
    """Hourly-based sensor: recalculated every few minutes, not only per update."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry, unique_suffix: str, name: str) -> None:
        super().__init__(coordinator, entry, unique_suffix, name)
        # Device name in front, so the entity IDs follow the existing ones
        # (sensor.home_solar_forecast_today -> sensor.home_solar_forecast_power_now)
        self._attr_has_entity_name = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, HOURLY_SENSOR_TICK)
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._data.get("hourly"))


class SolarForecastPowerNowSensor(SolarForecastHourlyBaseSensor):
    """Expected power right now."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "power_now", "Solar Forecast Power Now")
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_icon = "mdi:solar-power-variant"

    @property
    def native_value(self) -> int | None:
        return round(power_at(_series(self._data), dt_util.utcnow().timestamp()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Which orientation shapes the hours: set in the options, detected, or horizontal."""
        orientation = self._data.get("orientation") or {}
        return {
            "orientation_source": self._data.get("hourly_source"),
            "tilt": orientation.get("tilt"),
            "azimuth": orientation.get("azimuth"),
            "learned_hours": self._data.get("hourly_learned_hours"),
        }


class SolarForecastHourEnergySensor(SolarForecastHourlyBaseSensor):
    """Expected energy in the current or the next clock hour."""

    def __init__(
        self,
        coordinator: SolarForecastCoordinator,
        entry: ConfigEntry,
        offset: int,
        suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, entry, suffix, f"Solar Forecast {name}")
        self._offset = offset
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:solar-power"

    @property
    def native_value(self) -> float | None:
        start = int(dt_util.utcnow().timestamp() // 3600 * 3600) + self._offset * 3600
        return round(energy_between(_series(self._data), start, start + 3600), 3)


class SolarForecastMorningHourSensor(SolarForecastHourlyBaseSensor):
    """This hour's energy as forecast in the morning (from 06:00 local).

    Recorded as a measurement, so the recorder keeps it for good as an
    hourly statistic: the honest forecast to compare with the real yield.
    """

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, "energy_current_hour_morning", "Solar Forecast Current Hour Morning Forecast"
        )
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:weather-sunset-up"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Switch exactly on the hour, so each hourly statistic holds one value
        self.async_on_remove(
            async_track_time_change(self.hass, self._async_tick, minute=0, second=0)
        )

    @property
    def _morning(self) -> dict[str, Any]:
        return self._data.get("morning") or {}

    @property
    def available(self) -> bool:
        return super().available and self._morning.get("date") == dt_util.now(_tz(self._data)).date().isoformat()

    @property
    def native_value(self) -> float | None:
        start = int(dt_util.utcnow().timestamp() // 3600 * 3600)
        hours = dict(self._morning.get("hours") or [])
        return round(float(hours.get(start, 0.0)), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        computed = self._morning.get("computed")
        hours = self._morning.get("hours") or []
        return {
            "forecast_made": _iso(computed, _tz(self._data)) if computed else None,
            "day_total_kwh": round(sum(k for _, k in hours), 2),
        }


class SolarForecastRemainingTodaySensor(SolarForecastHourlyBaseSensor):
    """Expected energy from now until the end of the local day."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "energy_remaining_today", "Solar Forecast Remaining Today")
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:solar-power"

    @property
    def native_value(self) -> float | None:
        tz = _tz(self._data)
        now = datetime.now(tz)
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tz)
        return round(energy_between(_series(self._data), now.timestamp(), midnight.timestamp()), 3)


class SolarForecastPeakTimeSensor(SolarForecastHourlyBaseSensor):
    """Start of the strongest hour of today or tomorrow."""

    def __init__(
        self,
        coordinator: SolarForecastCoordinator,
        entry: ConfigEntry,
        day_offset: int,
        suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, entry, suffix, f"Solar Forecast {name}")
        self._day_offset = day_offset
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-star-four-points-outline"

    def _peak(self) -> tuple[int, float] | None:
        tz = _tz(self._data)
        day = datetime.now(tz).date() + timedelta(days=self._day_offset)
        return peak_hour(_series(self._data), day, tz)

    @property
    def native_value(self) -> datetime | None:
        peak = self._peak()
        return datetime.fromtimestamp(peak[0], timezone.utc) if peak else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        peak = self._peak()
        return {"energy_kwh": round(peak[1], 3) if peak else None}


class SolarForecastBestWindowSensor(SolarForecastHourlyBaseSensor):
    """Start of the best run of 2 or 3 hours still ahead (today, else tomorrow)."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry, hours: int) -> None:
        super().__init__(
            coordinator, entry, f"best_window_{hours}h", f"Solar Forecast Best {hours}h Window"
        )
        self._hours = hours
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:washing-machine"

    def _window(self) -> tuple[int, float] | None:
        tz = _tz(self._data)
        now = datetime.now(tz)
        series = _series(self._data)
        window = best_window(series, self._hours, now.date(), tz, now.timestamp())
        if window is None:
            tomorrow = now.date() + timedelta(days=1)
            window = best_window(series, self._hours, tomorrow, tz, now.timestamp())
        return window

    @property
    def native_value(self) -> datetime | None:
        window = self._window()
        return datetime.fromtimestamp(window[0], timezone.utc) if window else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        window = self._window()
        if window is None:
            return {"end": None, "energy_kwh": None}
        tz = _tz(self._data)
        return {
            "end": _iso(window[0] + self._hours * 3600, tz),
            "energy_kwh": window[1],
        }
