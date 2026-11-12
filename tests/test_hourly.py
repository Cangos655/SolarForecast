"""Tests for the hourly forecast: time zones, DST, alignment, learning.

Run with:  python3 -m unittest discover -s tests
No Home Assistant needed: the pure modules are loaded without the
integration's __init__ (which imports Home Assistant).
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "solarforecast"
PKG = "sf_under_test"


def _load(name: str):
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG] = pkg
    full = f"{PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


# weather_api imports aiohttp at module level; the parsing code doesn't use it
if "aiohttp" not in sys.modules:
    stub = types.ModuleType("aiohttp")
    stub.ClientSession = object
    stub.ClientTimeout = lambda **kw: None
    stub.ClientError = Exception
    sys.modules["aiohttp"] = stub

solar_position = _load("solar_position")
hourly = _load("hourly")
weather_api = _load("weather_api")

BERLIN = ZoneInfo("Europe/Berlin")
KOLKATA = ZoneInfo("Asia/Kolkata")
LAT, LON = 51.0, 6.9


def epoch(y, m, d, h, tz=timezone.utc, minute=0) -> int:
    return int(datetime(y, m, d, h, minute, tzinfo=tz).timestamp())


def clear_sky_irradiance(start: int) -> float:
    """Rough clear-sky Wh/m² of an hour from the sun elevation."""
    _, elev = solar_position.sun_position(datetime.fromtimestamp(start + 1800, timezone.utc), LAT, LON)
    return max(0.0, 900 * max(0.0, elev) / 60)


class SunPositionTest(unittest.TestCase):
    def test_winter_noon_cologne(self):
        # Solar noon in Cologne mid-November is about 12:15 CET
        az, el = solar_position.sun_position(datetime(2026, 11, 12, 12, 15, tzinfo=BERLIN), LAT, LON)
        self.assertAlmostEqual(az, 180, delta=3)
        self.assertAlmostEqual(el, 21, delta=1.5)

    def test_morning_is_east_afternoon_is_west(self):
        az_m, _ = solar_position.sun_position(datetime(2026, 6, 21, 8, 0, tzinfo=BERLIN), LAT, LON)
        az_a, _ = solar_position.sun_position(datetime(2026, 6, 21, 17, 0, tzinfo=BERLIN), LAT, LON)
        self.assertLess(az_m, 180)
        self.assertGreater(az_a, 180)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            solar_position.sun_position(datetime(2026, 1, 1, 12), LAT, LON)


class OpenMeteoAlignmentTest(unittest.TestCase):
    def test_value_belongs_to_preceding_hour(self):
        # Open-Meteo: the value at 13:00 is the mean of 12:00-13:00
        result = hourly.hours_from_open_meteo([epoch(2026, 11, 12, 13)], [500])
        self.assertEqual(result, {epoch(2026, 11, 12, 12): 500.0})

    def test_none_values_become_zero(self):
        result = hourly.hours_from_open_meteo([epoch(2026, 11, 12, 1)], [None])
        self.assertEqual(result, {epoch(2026, 11, 12, 0): 0.0})

    def test_recorder_hour_matches(self):
        # Recorder hour starting 12:00 UTC lines up with Open-Meteo's 13:00 value
        recorder_start = epoch(2026, 11, 12, 12)
        om = hourly.hours_from_open_meteo([epoch(2026, 11, 12, 13)], [420])
        self.assertIn(recorder_start, om)

    def test_orientation_params(self):
        self.assertEqual(weather_api.orientation_params(46, 150), {"tilt": 46.0, "azimuth": -30.0})
        self.assertEqual(weather_api.orientation_params(30, 180)["azimuth"], 0.0)
        self.assertEqual(weather_api.orientation_params(30, 90)["azimuth"], -90.0)
        self.assertEqual(weather_api.orientation_params(30, 270)["azimuth"], 90.0)
        self.assertEqual(weather_api.orientation_params(None, 180), {})
        self.assertEqual(weather_api.orientation_params(30, None), {})


def _hourly_times(start_local: datetime, hours: int) -> list[int]:
    """Unix times Open-Meteo returns for consecutive hours (UTC based, DST-safe)."""
    first = int(start_local.timestamp())
    return [first + i * 3600 for i in range(hours)]


class DstTest(unittest.TestCase):
    def _hours_per_local_day(self, day: date, tz) -> int:
        start = datetime(day.year, day.month, day.day, tzinfo=tz) - timedelta(days=1)
        times = _hourly_times(start, 24 * 3 + 2)
        values = [100.0] * len(times)
        hours = hourly.hours_from_open_meteo(times, values)
        return sum(1 for e in hours if hourly.local_date(e, tz) == day)

    def test_spring_forward_has_23_hours(self):
        self.assertEqual(self._hours_per_local_day(date(2026, 3, 29), BERLIN), 23)

    def test_fall_back_has_25_hours(self):
        self.assertEqual(self._hours_per_local_day(date(2026, 10, 25), BERLIN), 25)

    def test_normal_day_has_24_hours(self):
        self.assertEqual(self._hours_per_local_day(date(2026, 11, 12), BERLIN), 24)

    def test_daily_totals_kept_over_dst_change(self):
        for day in (date(2026, 3, 29), date(2026, 10, 25)):
            start = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
            times = _hourly_times(start, 30)
            irr = {e: clear_sky_irradiance(e) for e in hourly.hours_from_open_meteo(times, [0] * 30)}
            series = hourly.hourly_forecast(irr, {day: 10.0}, {}, LAT, LON, BERLIN)
            self.assertAlmostEqual(sum(k for _, k in series), 10.0, places=2)
            self.assertTrue(all(hourly.local_date(e, BERLIN) == day for e, _ in series))
            # Strongest hour around local solar noon (12-13 in winter time, 13-14 in summer time)
            peak = max(series, key=lambda x: x[1])[0]
            self.assertIn(datetime.fromtimestamp(peak, BERLIN).hour, (11, 12, 13))

    def test_half_hour_time_zone(self):
        # India (UTC+5:30): local days start at 18:30 UTC
        day = date(2026, 11, 12)
        start = datetime(2026, 11, 12, tzinfo=KOLKATA)
        times = _hourly_times(start, 24)
        hours = hourly.hours_from_open_meteo(times, [1] * 24)
        local = [hourly.local_date(e, KOLKATA) for e in hours]
        self.assertEqual(sum(1 for d in local if d == day), 23)  # first value covers the hour before midnight
        self.assertEqual(sum(1 for d in local if d == day - timedelta(days=1)), 1)


class ForecastResponseTest(unittest.TestCase):
    def test_unixtime_daily_dates_and_sun_times(self):
        days = [date(2026, 10, 24) + timedelta(days=i) for i in range(3)]
        midnight = [int(datetime(d.year, d.month, d.day, tzinfo=BERLIN).timestamp()) for d in days]
        data = {
            "timezone": "Europe/Berlin",
            "daily": {
                "time": midnight,
                "sunrise": [int(datetime(d.year, d.month, d.day, 7, 30, tzinfo=BERLIN).timestamp()) for d in days],
                "sunset": [int(datetime(d.year, d.month, d.day, 17, 5, tzinfo=BERLIN).timestamp()) for d in days],
                "weather_code": [3, 1, 61],
                "temperature_2m_max": [12, 13, 11],
                "temperature_2m_min": [5, 6, 4],
                "shortwave_radiation_sum": [5, 6, 7],
                "sunshine_duration": [3600, 7200, 0],
                "daylight_duration": [36000, 36000, 36000],
            },
            "hourly": {"time": [midnight[1] + 3600 * 12], "shortwave_radiation": [300]},
        }
        parsed = weather_api.parse_forecast_response(data, "shortwave_radiation")
        self.assertEqual(parsed["yesterday"]["date"], "2026-10-24")
        self.assertEqual([d["date"] for d in parsed["forecast"]], ["2026-10-25", "2026-10-26"])
        self.assertEqual(parsed["forecast"][0]["sunrise"], "2026-10-25T07:30")
        self.assertEqual(parsed["forecast"][1]["sunset"], "2026-10-26T17:05")
        self.assertEqual(parsed["timezone"], "Europe/Berlin")
        self.assertEqual(parsed["hourly"], {midnight[1] + 3600 * 11: 300.0})

    def test_iso_daily_still_parsed(self):
        # Archive API keeps the ISO format
        daily = {"time": ["2026-11-12"], "sunrise": ["2026-11-12T07:45"], "sunset": ["2026-11-12T16:50"]}
        day = weather_api._parse_day(daily, 0)
        self.assertEqual(day["date"], "2026-11-12")
        self.assertEqual(day["sunrise"], "2026-11-12T07:45")


def _day_irradiance(day: date, tz=BERLIN) -> dict[int, float]:
    start = int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())
    return {start + i * 3600: clear_sky_irradiance(start + i * 3600) for i in range(24)}


class LearningTest(unittest.TestCase):
    def _samples(self, days: list[date], shade_before_hour: int | None = None, yield_factor=4.5):
        samples = []
        for d in days:
            for e, irr in _day_irradiance(d).items():
                energy = irr / 1000 * yield_factor
                if shade_before_hour is not None and datetime.fromtimestamp(e, BERLIN).hour < shade_before_hour:
                    energy *= 0.4
                if irr > 0 or energy > 0:
                    samples.append(hourly.Sample(e, energy, irr))
        return samples

    def test_no_samples_no_factors(self):
        self.assertEqual(
            hourly.learn_factors([], LAT, LON, datetime(2026, 11, 13, tzinfo=timezone.utc), BERLIN), {}
        )

    def test_uniform_system_gives_factors_near_one(self):
        days = [date(2026, 10, 20) + timedelta(days=i) for i in range(20)]
        factors = hourly.learn_factors(self._samples(days), LAT, LON, datetime(2026, 11, 13, tzinfo=timezone.utc), BERLIN)
        self.assertTrue(factors)
        for value in factors.values():
            self.assertAlmostEqual(value, 1.0, delta=0.05)

    def test_morning_shade_is_learned(self):
        days = [date(2026, 10, 20) + timedelta(days=i) for i in range(20)]
        samples = self._samples(days, shade_before_hour=11)
        factors = hourly.learn_factors(samples, LAT, LON, datetime(2026, 11, 13, tzinfo=timezone.utc), BERLIN)
        morning = hourly.sun_bin(epoch(2026, 11, 10, 9, BERLIN), LAT, LON)
        afternoon = hourly.sun_bin(epoch(2026, 11, 10, 14, BERLIN), LAT, LON)
        self.assertLess(factors[morning], 0.8)
        self.assertGreater(factors[afternoon], 1.0)

        # The forecast moves energy from morning to afternoon, total unchanged
        day = date(2026, 11, 14)
        plain = dict(hourly.hourly_forecast(_day_irradiance(day), {day: 10}, {}, LAT, LON, BERLIN))
        shaded = dict(hourly.hourly_forecast(_day_irradiance(day), {day: 10}, factors, LAT, LON, BERLIN))
        nine = epoch(2026, 11, 14, 9, BERLIN)
        fourteen = epoch(2026, 11, 14, 14, BERLIN)
        self.assertLess(shaded[nine], plain[nine])
        self.assertGreater(shaded[fourteen], plain[fourteen])
        self.assertAlmostEqual(sum(shaded.values()), 10, places=2)

    def test_learn_from_drops_older_days(self):
        # Shade on the eastern sky (sun azimuth below 150°) before the cut date only
        old = [date(2026, 9, 30) + timedelta(days=i) for i in range(20)]
        new = [date(2026, 10, 20) + timedelta(days=i) for i in range(20)]

        def shaded(e: int) -> bool:
            az, _ = solar_position.sun_position(datetime.fromtimestamp(e + 1800, timezone.utc), LAT, LON)
            return az < 150

        samples = [
            hourly.Sample(s.start, s.energy_kwh * (0.4 if shaded(s.start) else 1.0), s.irradiance)
            for s in self._samples(old)
        ] + self._samples(new)
        now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        with_old = hourly.learn_factors(samples, LAT, LON, now, BERLIN)
        without_old = hourly.learn_factors(samples, LAT, LON, now, BERLIN, learn_from=date(2026, 10, 20))
        old_shaded_bins = {
            hourly.sun_bin(s.start, LAT, LON)
            for s in self._samples(old)
            if shaded(s.start) and s.irradiance >= hourly.MIN_SAMPLE_IRRADIANCE
        }
        east = [k for k in without_old if k in old_shaded_bins]
        self.assertTrue(east)
        for key in east:
            self.assertLess(with_old[key], without_old[key])
            self.assertAlmostEqual(without_old[key], 1.0, delta=0.05)

    def test_outage_days_ignored(self):
        days = [date(2026, 10, 20) + timedelta(days=i) for i in range(10)]
        samples = self._samples(days)
        outage = [hourly.Sample(e, 0.0, irr) for e, irr in _day_irradiance(date(2026, 11, 5)).items() if irr > 0]
        now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        self.assertEqual(
            hourly.learn_factors(samples, LAT, LON, now, BERLIN),
            hourly.learn_factors(samples + outage, LAT, LON, now, BERLIN),
        )


class SeriesReadingTest(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 11, 12)
        self.series = hourly.hourly_forecast(_day_irradiance(self.day), {self.day: 12.0}, {}, LAT, LON, BERLIN)

    def test_days_without_total_are_left_out(self):
        irr = {**_day_irradiance(self.day), **_day_irradiance(self.day + timedelta(days=1))}
        series = hourly.hourly_forecast(irr, {self.day: 5.0}, {}, LAT, LON, BERLIN)
        self.assertTrue(all(hourly.local_date(e, BERLIN) == self.day for e, _ in series))

    def test_energy_between_whole_day(self):
        start = epoch(2026, 11, 12, 0, BERLIN)
        self.assertAlmostEqual(hourly.energy_between(self.series, start, start + 86400), 12.0, places=2)

    def test_energy_between_pro_rata(self):
        noon = epoch(2026, 11, 12, 12, BERLIN)
        hour = dict(self.series)[noon]
        self.assertAlmostEqual(hourly.energy_between(self.series, noon + 1800, noon + 3600), hour / 2, places=4)

    def test_power_at_middle_of_hour_is_mean_power(self):
        noon = epoch(2026, 11, 12, 12, BERLIN)
        hour = dict(self.series)[noon]
        self.assertAlmostEqual(hourly.power_at(self.series, noon + 1800), hour * 1000, places=3)

    def test_power_zero_at_night(self):
        self.assertEqual(hourly.power_at(self.series, epoch(2026, 11, 12, 2, BERLIN)), 0.0)

    def test_power_is_continuous(self):
        t = epoch(2026, 11, 12, 11, BERLIN) + 1800
        before = hourly.power_at(self.series, t - 1)
        after = hourly.power_at(self.series, t + 1)
        self.assertAlmostEqual(before, after, delta=5)

    def test_peak_hour(self):
        peak = hourly.peak_hour(self.series, self.day, BERLIN)
        self.assertIn(datetime.fromtimestamp(peak[0], BERLIN).hour, (11, 12))

    def test_best_window_respects_now(self):
        late = epoch(2026, 11, 12, 14, BERLIN, minute=20)
        window = hourly.best_window(self.series, 2, self.day, BERLIN, late)
        self.assertEqual(window[0], epoch(2026, 11, 12, 14, BERLIN))

    def test_best_window_none_after_sunset(self):
        evening = epoch(2026, 11, 12, 20, BERLIN)
        self.assertIsNone(hourly.best_window(self.series, 3, self.day, BERLIN, evening))

    def test_best_window_does_not_cross_midnight(self):
        window = hourly.best_window(self.series, 3, self.day, BERLIN, epoch(2026, 11, 12, 0, BERLIN))
        start = datetime.fromtimestamp(window[0], BERLIN)
        self.assertEqual(start.date(), self.day)
        self.assertIn(start.hour, (10, 11))


if __name__ == "__main__":
    unittest.main()
