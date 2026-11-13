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
from custom_components.solarforecast.hourly import Irradiance
from custom_components.solarforecast.weather_api import HOURLY_FIELDS

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def no_frontend(hass):
    """The card registration needs the real frontend; not part of these tests."""
    hass.config.components.update({"frontend", "lovelace", "http", "energy"})
    with patch("custom_components.solarforecast._async_register_frontend_resources"):
        yield


def _forecast_payload(peak_hour: int = 12) -> dict:
    today = dt_util.now().astimezone(BERLIN).date()
    days = [today - timedelta(days=1) + timedelta(days=i) for i in range(9)]
    midnight = [int(datetime(d.year, d.month, d.day, tzinfo=BERLIN).timestamp()) for d in days]
    first = midnight[0]
    hours = [first + 3600 * (i + 1) for i in range(24 * 9)]

    def irr(t):
        h = datetime.fromtimestamp(t - 1800, BERLIN).hour
        return max(0, 400 - 60 * abs(h - peak_hour)) if 8 <= h <= 16 else 0

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
        "hourly": {
            "time": hours,
            "shortwave_radiation": [irr(t) for t in hours],
            "direct_normal_irradiance": [irr(t) * 1.5 for t in hours],
            "diffuse_radiation": [irr(t) * 0.3 for t in hours],
        },
    }


@pytest.mark.parametrize("tilted", [False, True])
async def test_setup_sensors_and_energy(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock, tilted):
    await hass.config.async_set_time_zone("Europe/Berlin")
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload())
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
    # The orientation is applied locally, Open-Meteo always gets the same request
    assert params["hourly"] == HOURLY_FIELDS
    assert "tilt" not in params and "azimuth" not in params

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
    assert datetime.fromisoformat(peak.state).astimezone(BERLIN).hour in ((11, 12) if tilted else (12,))

    # No learned hours yet: manual orientation if set, horizontal otherwise
    power = [s for e, s in states.items() if e.endswith("power_now")][0]
    if tilted:
        assert power.attributes["orientation_source"] == "manual"
        assert (power.attributes["tilt"], power.attributes["azimuth"]) == (46, 150)
    else:
        assert power.attributes["orientation_source"] == "horizontal"
        assert power.attributes["tilt"] is None


async def test_options_flow_orientation(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock):
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload())
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
        json={"hourly": {
            "time": hist_times,
            "shortwave_radiation": hist_vals,
            "direct_normal_irradiance": [v * 2 for v in hist_vals],
            "diffuse_radiation": [v / 3 for v in hist_vals],
        }},
    )
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload())
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
        assert sample.irradiance == Irradiance(300, 600, 100)
    hist_calls = [c for c in aioclient_mock.mock_calls if "historical-forecast" in str(c[1])]
    assert hist_calls[0][1].query["timezone"] == "GMT"
    assert hist_calls[0][1].query["hourly"] == HOURLY_FIELDS
    assert coordinator.data["hourly_learned_hours"] == 40
    # Five days are too few to detect an orientation: horizontal, checked today
    assert coordinator.data["hourly_source"] == "horizontal"
    assert coordinator._detected_on == dt_util.now().date()


async def test_old_hourly_store_is_relearned(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock, hass_storage):
    """Hours stored by 1.1.0b1 (one irradiance value) are dropped and learned again."""
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload())
    aioclient_mock.get("https://archive-api.open-meteo.com/v1/archive", json={"daily": {"time": []}})
    aioclient_mock.get("https://historical-forecast-api.open-meteo.com/v1/forecast", json={"hourly": {}})
    hass.states.async_set(
        "sensor.pv", "100", {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=3, title="Home",
        data={"latitude": 51.0, "longitude": 6.9, "solar_sensor": "sensor.pv", "location_name": "Home"},
    )
    hass_storage[f"{DOMAIN}_{entry.entry_id}_hourly"] = {
        "version": 1, "minor_version": 1, "key": f"{DOMAIN}_{entry.entry_id}_hourly",
        "data": {"orientation": "46/150", "samples": [[1792476000, 0.12, 50.0]]},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id]._samples == []


async def test_offered_as_energy_forecast(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock):
    """The energy settings dialog lists only 'service' integrations as solar forecasts."""
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload())
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

    # The frontend's solar settings dialog asks for config entries with
    # type_filter ["service"]; other integration types are not listed there.
    from homeassistant.components.energy.websocket_api import async_get_energy_platforms
    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN)
    assert integration.integration_type == "service"
    assert DOMAIN in await async_get_energy_platforms(hass)


async def test_started_hours_frozen_and_morning_kept(recorder_mock, enable_custom_integrations, hass: HomeAssistant, no_frontend, aioclient_mock, freezer):
    """A later forecast does not rewrite hours that have begun; the morning forecast stays."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(datetime(2026, 11, 13, 9, 30, tzinfo=BERLIN))
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload(peak_hour=11))
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
    coordinator = hass.data[DOMAIN][entry.entry_id]
    first = dict(coordinator.data["hourly"])
    hour = {h: int(datetime(2026, 11, 13, h, tzinfo=BERLIN).timestamp()) for h in range(24)}
    assert coordinator.data["morning"]["date"] == "2026-11-13"

    # One hour later the weather forecast moved the peak into the afternoon
    freezer.move_to(datetime(2026, 11, 13, 10, 30, tzinfo=BERLIN))
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=_forecast_payload(peak_hour=14))
    aioclient_mock.get("https://archive-api.open-meteo.com/v1/archive", json={"daily": {"time": []}})
    aioclient_mock.get("https://historical-forecast-api.open-meteo.com/v1/forecast", json={"hourly": {}})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    second = dict(coordinator.data["hourly"])

    # 9:00 and 10:00 had begun: they keep the first forecast; 11:00 and later follow the new one
    assert second[hour[9]] == first[hour[9]]
    assert second[hour[10]] == first[hour[10]]
    assert second[hour[14]] > first[hour[14]]
    assert second[hour[11]] != first[hour[11]]

    # The morning forecast of this hour, recorded as a measurement
    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    morning = [s for e, s in states.items() if e.endswith("current_hour_morning_forecast")][0]
    assert float(morning.state) == pytest.approx(round(first[hour[10]], 3))
    assert morning.attributes["state_class"] == "measurement"
    current = [s for e, s in states.items() if e.endswith("energy_current_hour")][0]
    assert float(current.state) == pytest.approx(round(first[hour[10]], 3))
