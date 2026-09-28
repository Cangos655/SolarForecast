"""Open-Meteo API wrapper for Solar Forecast."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    ARCHIVE_LOOKBACK_DAYS,
    FORECAST_DAYS,
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_FORECAST_URL,
    OPEN_METEO_GEOCODING_URL,
)
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


def _parse_day(daily: dict[str, Any], index: int) -> ForecastDay:
    """Extract a single day from an Open-Meteo daily response dict."""
    sunshine_s = daily.get("sunshine_duration", [0])[index] or 0
    daylight_s = daily.get("daylight_duration", [1])[index] or 1
    return {
        "date": daily["time"][index],
        "weather_code": daily.get("weather_code", [0])[index] or 0,
        "radiation_sum": daily.get("shortwave_radiation_sum", [0])[index] or 0.0,
        "temp_max": daily.get("temperature_2m_max", [20])[index] or 20.0,
        "temp_min": daily.get("temperature_2m_min", [10])[index] or 10.0,
        "sunshine_duration": sunshine_s / 3600,   # seconds → hours
        "daylight_duration": daylight_s / 3600,   # seconds → hours
        "sunrise": (daily.get("sunrise") or [None])[index],
        "sunset": (daily.get("sunset") or [None])[index],
    }


async def fetch_forecast(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    """Fetch current forecast (8 days) + yesterday from Open-Meteo.

    Returns:
        {
            "yesterday": ForecastDay,
            "forecast": [ForecastDay, ...]   # today + 7 days
        }
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": _DAILY_PARAMS,
        "timezone": "auto",
        "past_days": 1,
        "forecast_days": FORECAST_DAYS,
    }

    async with session.get(OPEN_METEO_FORECAST_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json()

    daily = data.get("daily", {})
    times = daily.get("time", [])

    if not times:
        raise ValueError("Open-Meteo returned empty daily data")

    yesterday = _parse_day(daily, 0)
    forecast = [_parse_day(daily, i) for i in range(1, len(times))]

    return {"yesterday": yesterday, "forecast": forecast}


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
