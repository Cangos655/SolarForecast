"""Integration test with a real Home Assistant core (optional).

Needs:  pip install pytest-homeassistant-custom-component
Run from the repository root:  pytest tests/ha -o asyncio_mode=auto
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solarforecast import energy
from custom_components.solarforecast.const import DOMAIN

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def no_frontend(hass):
    """The card registration needs the real frontend; not part of these tests."""
    hass.config.components.update({"frontend", "lovelace", "http", "energy"})
    with patch("custom_components.solarforecast._async_register_frontend_resources"):
        yield


def _forecast_payload(tilted: bool) -> dict:
    today = dt_util.now().astimezone(BERLIN).date()
    days = [today - timedelta(days=1) + timedelta(days=i) for i in range(9)]
    midnight = [int(datetime(d.year, d.month, d.day, tzinfo=BERLIN).timestamp()) for d in days]
    first = midnight[0]
    hours = [first + 3600 * (i + 1) for i in range(24 * 9)]
    field = "global_tilted_irradiance" if tilted else "shortwave_radiation"

    def irr(t):
        h = datetime.fromtimestamp(t - 1800, BERLIN).hour
        return max(0, 400 - 60 * abs(h - 12)) if 8 <= h <= 16 else 0

    return {
        "timezone": "Europe/Berlin",
        "daily": {
            "time": midnight,
            "weather_code": [1] * 9,
            "shortwave_radiation_sum": [5.0] * 9,
            "temperature_2m_max": [10] * 9,
            "temperature_2m_min": [3] * 9,
            "sunshine_duration": [20000] * 9,
            "daylight_duration": [32000] * 9,
            "sunrise": [m + 7 * 3600 for m in midnight],
            "sunset": [m + 17 * 3600 for m in midnight],
        },
        "hourly": {"time": hours, field: [irr(t) for t in hours]},
    }


@pytest.mark.parametrize("tilted", [False, True])
async def test_setup_sensors_and_energy(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock, tilted):
    await hass.config.async_set_time_zone("Europe/Berlin")
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload(tilted))
    aioclient_mock.get("https://archive-api.open-meteo.com/v1/archive", json={"daily": {"time": []}})
    aioclient_mock.get("https://historical-forecast-api.open-meteo.com/v1/forecast", json={"hourly": {}})

    hass.states.async_set(
        "sensor.pv", "100", {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
    )
    options = {"tilt": 46, "azimuth": 150} if tilted else {}
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        title="Home",
        data={"latitude": 51.0, "longitude": 6.9, "solar_sensor": "sensor.pv", "location_name": "Home"},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 3
    sent = [call for call in aioclient_mock.mock_calls if "api.open-meteo.com" in str(call[1])]
    params = sent[0][1].query
    assert params["timeformat"] == "unixtime"
    if tilted:
        assert params["tilt"] == "46.0" and params["azimuth"] == "-30.0"
        assert params["hourly"] == "global_tilted_irradiance"
    else:
        assert "tilt" not in params and params["hourly"] == "shortwave_radiation"

    coordinator = hass.data[DOMAIN][entry.entry_id]
    series = coordinator.data["hourly"]
    assert series
    today_total = coordinator.data["forecasts"][0]["expected_kwh"]
    today = dt_util.now().astimezone(BERLIN).date()
    day_sum = sum(k for e, k in series if datetime.fromtimestamp(e, BERLIN).date() == today)
    assert day_sum == pytest.approx(today_total, abs=0.01)

    forecast = await energy.async_get_solar_forecast(hass, entry.entry_id)
    wh = forecast["wh_hours"]
    assert len(wh) == len(series)
    first_key = next(iter(wh))
    assert datetime.fromisoformat(first_key).tzinfo is not None
    assert await energy.async_get_solar_forecast(hass, "unknown") is None

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    for suffix in (
        "power_now", "energy_current_hour", "energy_next_hour", "remaining_today",
        "peak_time_today", "peak_time_tomorrow", "best_2h_window", "best_3h_window",
    ):
        matches = [e for e in states if e.endswith(suffix)]
        assert matches, f"missing sensor *{suffix}: {sorted(states)}"
        assert states[matches[0]].state not in ("unavailable",), matches[0]
    today_sensor = [s for e, s in states.items() if e.endswith("solar_forecast_today")][0]
    assert today_sensor.attributes["hourly"]
    peak = [s for e, s in states.items() if e.endswith("peak_time_tomorrow")][0]
    assert datetime.fromisoformat(peak.state).astimezone(BERLIN).hour == 12


async def test_options_flow_orientation(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock):
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload(False))
    aioclient_mock.get("https://archive-api.open-meteo.com/v1/archive", json={"daily": {"time": []}})
    aioclient_mock.get("https://historical-forecast-api.open-meteo.com/v1/forecast", json={"hourly": {}})
    hass.states.async_set(
        "sensor.pv", "100", {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=3, title="Home",
        data={"latitude": 51.0, "longitude": 6.9, "solar_sensor": "sensor.pv", "location_name": "Home"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_sensor": "sensor.pv", "tilt": 46}
    )
    assert result["errors"] == {"base": "orientation_incomplete"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_sensor": "sensor.pv", "tilt": 46, "azimuth": 150, "learn_from": "2026-10-20"}
    )
    assert result["type"] == "create_entry"
    assert entry.options == {"solar_sensor": "sensor.pv", "tilt": 46, "azimuth": 150, "learn_from": "2026-10-20"}


async def test_hourly_learning_from_recorder(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock):
    """Hours learned from recorder statistics, matched with Open-Meteo hours."""
    from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    await hass.config.async_set_time_zone("Europe/Berlin")
    now = dt_util.utcnow()
    start_day = (now - timedelta(days=5)).astimezone(BERLIN).replace(hour=0, minute=0, second=0, microsecond=0)

    # 5 days of hourly statistics: production 08-16 local, 1 kWh per hour
    rows, total = [], 0.0
    t = start_day
    while t < now.astimezone(BERLIN).replace(hour=0, minute=0, second=0, microsecond=0):
        if 8 <= t.hour < 16:
            total += 1.0
        rows.append({"start": t.astimezone(timezone.utc), "state": total, "sum": total})
        t += timedelta(hours=1)
    metadata = StatisticMetaData(
        has_sum=True, mean_type=StatisticMeanType.NONE, name=None, source="recorder",
        statistic_id="sensor.pv", unit_of_measurement="kWh", unit_class="energy",
    )
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)

    # Historical irradiance: same hours sunny (value at T covers T-1h..T)
    hist_times = [int(r["start"].timestamp()) + 3600 for r in rows]
    hist_vals = [300 if 8 <= r["start"].astimezone(BERLIN).hour < 16 else 0 for r in rows]
    aioclient_mock.get(
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        json={"hourly": {"time": hist_times, "shortwave_radiation": hist_vals}},
    )
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload(False))
    aioclient_mock.get("https://archive-api.open-meteo.com/v1/archive", json={"daily": {"time": []}})
    hass.states.async_set(
        "sensor.pv", str(total), {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=3, title="Home",
        data={"latitude": 51.0, "longitude": 6.9, "solar_sensor": "sensor.pv", "location_name": "Home"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    learned = coordinator._samples
    assert len(learned) == 5 * 8
    for sample in learned:
        assert 8 <= datetime.fromtimestamp(sample.start, BERLIN).hour < 16
        assert sample.energy_kwh == pytest.approx(1.0)
        assert sample.irradiance == 300
    hist_calls = [c for c in aioclient_mock.mock_calls if "historical-forecast" in str(c[1])]
    assert hist_calls[0][1].query["timezone"] == "GMT"
    assert coordinator.data["hourly_learned_hours"] == 40
