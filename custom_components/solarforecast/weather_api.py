"""Open-Meteo API wrapper for Solar Forecast."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    ARCHIVE_LOOKBACK_DAYS,
    FORECAST_DAYS,
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_FORECAST_URL,
    OPEN_METEO_GEOCODING_URL,
    OPEN_METEO_HISTORICAL_FORECAST_URL,
)
from .hourly import hours_from_open_meteo
from .ml_engine import ForecastDay

_LOGGER = logging.getLogger(__name__)

_DAILY_PARAMS = ",".join([
    "weather_code",
    "shortwave_radiation_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "sunshine_duration",
    "daylight_duration",
    "sunrise",
    "sunset",
])


def _unix_to_local(value: Any, tz: ZoneInfo | None, fmt: str) -> Any:
    """Turn a unix timestamp into a local string; leave other values as they are."""
    if tz is None or not isinstance(value, (int, float)):
        return value
    return datetime.fromtimestamp(value, tz).strftime(fmt)


def _parse_day(daily: dict[str, Any], index: int, tz: ZoneInfo | None = None) -> ForecastDay:
    """Extract a single day from an Open-Meteo daily response dict.

    With ``tz`` the response uses ``timeformat=unixtime``: dates and sunrise/
    sunset come back as timestamps and are turned into the same local ISO
    strings the integration has always used.
    """
    sunshine_s = daily.get("sunshine_duration", [0])[index] or 0
    daylight_s = daily.get("daylight_duration", [1])[index] or 1
    return {
        "date": _unix_to_local(daily["time"][index], tz, "%Y-%m-%d"),
        "weather_code": daily.get("weather_code", [0])[index] or 0,
        "radiation_sum": daily.get("shortwave_radiation_sum", [0])[index] or 0.0,
        "temp_max": daily.get("temperature_2m_max", [20])[index] or 20.0,
        "temp_min": daily.get("temperature_2m_min", [10])[index] or 10.0,
        "sunshine_duration": sunshine_s / 3600,   # seconds → hours
        "daylight_duration": daylight_s / 3600,   # seconds → hours
        "sunrise": _unix_to_local((daily.get("sunrise") or [None])[index], tz, "%Y-%m-%dT%H:%M"),
        "sunset": _unix_to_local((daily.get("sunset") or [None])[index], tz, "%Y-%m-%dT%H:%M"),
    }


def orientation_params(tilt: float | None, azimuth: float | None) -> dict[str, Any]:
    """Open-Meteo parameters for tilted irradiance, empty without orientation.

    The integration stores the azimuth like Forecast.Solar in Home Assistant
    (0 = north, 90 = east, 180 = south, 270 = west); Open-Meteo counts from
    south (0 = south, -90 = east, 90 = west).
    """
    if tilt is None or azimuth is None:
        return {}
    om_azimuth = ((float(azimuth) - 180 + 180) % 360) - 180
    return {"tilt": float(tilt), "azimuth": om_azimuth}


def _hourly_field(tilt: float | None, azimuth: float | None) -> str:
    return "global_tilted_irradiance" if orientation_params(tilt, azimuth) else "shortwave_radiation"


def _parse_hourly(data: dict[str, Any], field: str) -> dict[int, float]:
    hourly = data.get("hourly") or {}
    return hours_from_open_meteo(hourly.get("time") or [], hourly.get(field) or [])


async def fetch_forecast(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> dict[str, Any]:
    """Fetch current forecast (8 days) + yesterday from Open-Meteo.

    Daily values and the hourly irradiance come in one request.

    Returns:
        {
            "yesterday": ForecastDay,
            "forecast": [ForecastDay, ...],   # today + 7 days
            "hourly": {hour start epoch: Wh/m²},
            "timezone": "Europe/Berlin",      # time zone of the location
        }
    """
    field = _hourly_field(tilt, azimuth)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": _DAILY_PARAMS,
        "hourly": field,
        "timezone": "auto",
        "timeformat": "unixtime",
        "past_days": 1,
        "forecast_days": FORECAST_DAYS,
        **orientation_params(tilt, azimuth),
    }

    async with session.get(OPEN_METEO_FORECAST_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json()

    return parse_forecast_response(data, field)


def parse_forecast_response(data: dict[str, Any], field: str) -> dict[str, Any]:
    """Parse a ``timeformat=unixtime`` forecast response."""
    tz_name = data.get("timezone") or "UTC"
    tz = ZoneInfo(tz_name)
    daily = data.get("daily", {})
    times = daily.get("time", [])

    if not times:
        raise ValueError("Open-Meteo returned empty daily data")

    yesterday = _parse_day(daily, 0, tz)
    forecast = [_parse_day(daily, i, tz) for i in range(1, len(times))]

    return {
        "yesterday": yesterday,
        "forecast": forecast,
        "hourly": _parse_hourly(data, field),
        "timezone": tz_name,
    }


async def fetch_hourly_history(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> dict[int, float]:
    """Hourly irradiance of past days for learning, {hour start epoch: Wh/m²}.

    Uses the Historical Forecast API (stored forecasts, so the learned
    factors match what the live forecast will deliver). Requested in UTC so
    the hours line up with the recorder's hourly statistics everywhere,
    including time zones with half-hour offsets.
    """
    field = _hourly_field(tilt, azimuth)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": field,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "GMT",
        "timeformat": "unixtime",
        **orientation_params(tilt, azimuth),
    }
    try:
        async with session.get(
            OPEN_METEO_HISTORICAL_FORECAST_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001 - learning is best effort
        _LOGGER.warning("Could not fetch hourly history %s–%s: %s", start_date, end_date, exc)
        return {}
    return _parse_hourly(data, field)


async def fetch_historical_day(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    date_str: str,
) -> ForecastDay | None:
    """Fetch weather data for a single past date from the archive API."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": date_str,
        "end_date": date_str,
        "daily": _DAILY_PARAMS,
        "timezone": "auto",
    }

    try:
        async with session.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        daily = data.get("daily", {})
        if not daily.get("time"):
            return None
        return _parse_day(daily, 0)
    except Exception as exc:
        _LOGGER.warning("Could not fetch archive data for %s: %s", date_str, exc)
        return None


async def fetch_historical_range(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
) -> list[ForecastDay]:
    """Fetch weather data for a date range from the archive API."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "daily": _DAILY_PARAMS,
        "timezone": "auto",
    }

    try:
        async with session.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        daily = data.get("daily", {})
        times = daily.get("time", [])
        return [_parse_day(daily, i) for i in range(len(times))]
    except Exception as exc:
        _LOGGER.warning("Could not fetch archive range %s–%s: %s", start_date, end_date, exc)
        return []


async def search_location(
    session: aiohttp.ClientSession,
    city: str,
    count: int = 5,
) -> list[dict[str, Any]]:
    """Search for a city via Open-Meteo Geocoding API.

    Returns list of dicts with keys: name, latitude, longitude, country, admin1.
    """
    params = {
        "name": city,
        "count": count,
        "language": "en",
        "format": "json",
    }

    async with session.get(OPEN_METEO_GEOCODING_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        data = await resp.json()

    results = data.get("results", [])
    return [
        {
            "name": r.get("name", ""),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "country": r.get("country", ""),
            "admin1": r.get("admin1", ""),
            "display_name": f"{r.get('name', '')}, {r.get('admin1', '')}, {r.get('country', '')}".strip(", "),
        }
        for r in results
        if r.get("latitude") is not None and r.get("longitude") is not None
    ]
