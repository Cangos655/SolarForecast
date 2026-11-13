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
import math
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


def clear_sky_irradiance(start: int):
    """Rough clear-sky irradiance of an hour (Meinel beam, simple diffuse)."""
    _, elev = solar_position.sun_position(datetime.fromtimestamp(start + 1800, timezone.utc), LAT, LON)
    if elev <= 0:
        return hourly.Irradiance(0.0, 0.0, 0.0)
    cos_z = math.sin(math.radians(elev))
    dni = 1000 * 0.7 ** ((1 / max(cos_z, 0.05)) ** 0.678)
    dhi = 100 * cos_z
    return hourly.Irradiance(dni * cos_z + dhi, dni, dhi)


def one_value(times, values):
    """Open-Meteo hours with the same value for global, direct and diffuse."""
    return hourly.hours_from_open_meteo(times, values, values, values)


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
        result = hourly.hours_from_open_meteo([epoch(2026, 11, 12, 13)], [500], [600], [100])
        self.assertEqual(result, {epoch(2026, 11, 12, 12): hourly.Irradiance(500.0, 600.0, 100.0)})

    def test_none_values_become_zero(self):
        result = one_value([epoch(2026, 11, 12, 1)], [None])
        self.assertEqual(result, {epoch(2026, 11, 12, 0): hourly.Irradiance(0.0, 0.0, 0.0)})

    def test_recorder_hour_matches(self):
        # Recorder hour starting 12:00 UTC lines up with Open-Meteo's 13:00 value
        recorder_start = epoch(2026, 11, 12, 12)
        om = one_value([epoch(2026, 11, 12, 13)], [420])
        self.assertIn(recorder_start, om)


class PlaneIrradianceTest(unittest.TestCase):
    noon = epoch(2026, 11, 12, 12, BERLIN)
    morning = epoch(2026, 11, 12, 9, BERLIN)

    def _plane(self, start, orientation):
        return hourly.plane_irradiance(start, clear_sky_irradiance(start), LAT, LON, orientation)

    def test_horizontal_is_global(self):
        irr = clear_sky_irradiance(self.noon)
        self.assertEqual(self._plane(self.noon, None), irr.ghi)
        # A flat plane in the sky model gives the same, whatever the azimuth
        self.assertAlmostEqual(self._plane(self.noon, hourly.Orientation(0, 90)), irr.ghi, places=6)

    def test_south_beats_north_in_winter(self):
        flat = self._plane(self.noon, None)
        self.assertGreater(self._plane(self.noon, hourly.Orientation(45, 180)), flat * 1.3)
        self.assertLess(self._plane(self.noon, hourly.Orientation(45, 0)), flat)

    def test_east_beats_west_in_the_morning(self):
        east = self._plane(self.morning, hourly.Orientation(40, 90))
        west = self._plane(self.morning, hourly.Orientation(40, 270))
        self.assertGreater(east, west * 2)

    def test_night_is_zero(self):
        night = epoch(2026, 11, 12, 2, BERLIN)
        self.assertEqual(self._plane(night, hourly.Orientation(30, 180)), 0.0)


def _hourly_times(start_local: datetime, hours: int) -> list[int]:
    """Unix times Open-Meteo returns for consecutive hours (UTC based, DST-safe)."""
    first = int(start_local.timestamp())
    return [first + i * 3600 for i in range(hours)]


class DstTest(unittest.TestCase):
    def _hours_per_local_day(self, day: date, tz) -> int:
        start = datetime(day.year, day.month, day.day, tzinfo=tz) - timedelta(days=1)
        times = _hourly_times(start, 24 * 3 + 2)
        values = [100.0] * len(times)
        hours = one_value(times, values)
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
            irr = {e: clear_sky_irradiance(e) for e in one_value(times, [0] * 30)}
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
        hours = one_value(times, [1] * 24)
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
            "hourly": {
                "time": [midnight[1] + 3600 * 12],
                "shortwave_radiation": [300],
                "direct_normal_irradiance": [450],
                "diffuse_radiation": [80],
            },
        }
        parsed = weather_api.parse_forecast_response(data)
        self.assertEqual(parsed["yesterday"]["date"], "2026-10-24")
        self.assertEqual([d["date"] for d in parsed["forecast"]], ["2026-10-25", "2026-10-26"])
        self.assertEqual(parsed["forecast"][0]["sunrise"], "2026-10-25T07:30")
        self.assertEqual(parsed["forecast"][1]["sunset"], "2026-10-26T17:05")
        self.assertEqual(parsed["timezone"], "Europe/Berlin")
        self.assertEqual(parsed["hourly"], {midnight[1] + 3600 * 11: hourly.Irradiance(300.0, 450.0, 80.0)})

    def test_request_has_no_orientation(self):
        # Orientation is applied locally; Open-Meteo is asked for the components
        self.assertEqual(
            weather_api.HOURLY_FIELDS, "shortwave_radiation,direct_normal_irradiance,diffuse_radiation"
        )

    def test_iso_daily_still_parsed(self):
        # Archive API keeps the ISO format
        daily = {"time": ["2026-11-12"], "sunrise": ["2026-11-12T07:45"], "sunset": ["2026-11-12T16:50"]}
        day = weather_api._parse_day(daily, 0)
        self.assertEqual(day["date"], "2026-11-12")
        self.assertEqual(day["sunrise"], "2026-11-12T07:45")


def _day_irradiance(day: date, tz=BERLIN) -> dict:
    start = int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())
    return {start + i * 3600: clear_sky_irradiance(start + i * 3600) for i in range(24)}


def _samples(days, orientation, yield_factor=4.5, shade_before_hour=None, shade_from_hour=None):
    """Real yields of a system with the given orientation (and optional shade)."""
    samples = []
    for d in days:
        for e, irr in _day_irradiance(d).items():
            energy = hourly.plane_irradiance(e, irr, LAT, LON, orientation) / 1000 * yield_factor
            hour = datetime.fromtimestamp(e, BERLIN).hour
            if shade_before_hour is not None and hour < shade_before_hour:
                energy *= 0.4
            if shade_from_hour is not None and hour >= shade_from_hour:
                energy *= 0.3
            if irr.ghi > 0 or energy > 0:
                samples.append(hourly.Sample(e, energy, irr))
    return samples


class DetectionTest(unittest.TestCase):
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    days = [date(2026, 10, 20) + timedelta(days=i) for i in range(20)]

    def _detect(self, samples, **kw):
        return hourly.detect_orientation(samples, LAT, LON, self.now, BERLIN, **kw)

    def test_finds_south_east_roof(self):
        found = self._detect(_samples(self.days, hourly.Orientation(46, 150)))
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found.tilt, 46, delta=6)
        self.assertAlmostEqual(found.azimuth, 150, delta=10)

    def test_finds_west_roof(self):
        found = self._detect(_samples(self.days, hourly.Orientation(30, 240)))
        self.assertAlmostEqual(found.azimuth, 240, delta=10)

    def test_flat_system_stays_horizontal(self):
        self.assertIsNone(self._detect(_samples(self.days, None)))

    def test_too_few_days(self):
        few = self.days[: hourly.DETECT_MIN_DAYS - 1]
        self.assertIsNone(self._detect(_samples(few, hourly.Orientation(46, 150))))

    def test_learn_from_and_window(self):
        old = [date(2026, 8, 1) + timedelta(days=i) for i in range(20)]  # outside the window
        samples = _samples(old, hourly.Orientation(30, 240)) + _samples(self.days, hourly.Orientation(46, 150))
        found = self._detect(samples)
        self.assertAlmostEqual(found.azimuth, 150, delta=10)
        self.assertIsNone(self._detect(samples, learn_from=date(2026, 11, 20)))

    def test_afternoon_shade_turns_it_east(self):
        # Shade from 13:00 makes the system behave like an east-facing one:
        # the detected orientation is an effective one, which the forecast needs
        found = self._detect(_samples(self.days, hourly.Orientation(46, 150), shade_from_hour=13))
        self.assertLess(found.azimuth, 140)

    def test_detected_orientation_shapes_the_forecast(self):
        day = date(2026, 11, 14)
        flat = dict(hourly.hourly_forecast(_day_irradiance(day), {day: 10}, {}, LAT, LON, BERLIN))
        west = dict(hourly.hourly_forecast(
            _day_irradiance(day), {day: 10}, {}, LAT, LON, BERLIN, hourly.Orientation(40, 240)
        ))
        afternoon = epoch(2026, 11, 14, 14, BERLIN)
        self.assertGreater(west[afternoon], flat[afternoon])
        self.assertAlmostEqual(sum(west.values()), 10, places=2)


class LearningTest(unittest.TestCase):
    def _samples(self, days: list[date], shade_before_hour: int | None = None, yield_factor=4.5):
        return _samples(days, None, yield_factor, shade_before_hour=shade_before_hour)

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
            if shaded(s.start) and s.irradiance.ghi >= hourly.MIN_SAMPLE_IRRADIANCE
        }
        east = [k for k in without_old if k in old_shaded_bins]
        self.assertTrue(east)
        for key in east:
            self.assertLess(with_old[key], without_old[key])
            self.assertAlmostEqual(without_old[key], 1.0, delta=0.05)

    def test_outage_days_ignored(self):
        days = [date(2026, 10, 20) + timedelta(days=i) for i in range(10)]
        samples = self._samples(days)
        outage = [hourly.Sample(e, 0.0, irr) for e, irr in _day_irradiance(date(2026, 11, 5)).items() if irr.ghi > 0]
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
