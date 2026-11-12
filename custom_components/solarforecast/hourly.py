"""Hourly solar forecast: daily-shape model learned per sun position.

Pure Python (no Home Assistant imports) so the time handling can be tested
on its own. All hours are handled as timezone-aware datetimes; an hour is
identified by its START, matching the Home Assistant recorder and energy
dashboard.

How the hourly forecast is built:
  1. Open-Meteo gives, per hour, the irradiance on the panels (tilted
     irradiance when tilt and azimuth are configured, horizontal otherwise).
  2. A correction factor is learned per sun-position bin from real hourly
     yields (recorder) against the irradiance of the same hours. Shade from
     a tree, a house or the horizon is tied to the sun's position, so a bin
     learned in one season carries over to others.
  3. The corrected irradiance only sets the SHAPE of the day: each day's
     hours are scaled so they add up to the daily forecast of the existing
     model. Daily sensors, the hourly series and the energy dashboard thus
     always agree on the total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable

from .solar_position import sun_position

ONE_HOUR = timedelta(hours=1)

# Sun-position bins
AZIMUTH_STEP = 15  # degrees
ELEVATION_BANDS = (0, 10, 25, 45, 91)

# Learning
HALF_LIFE_DAYS = 45          # older samples count half after this many days
PRIOR_IRRADIANCE_KWH = 3.0   # pull of each bin towards the overall factor (kWh/m², ~6 bright hours)
MIN_SAMPLE_IRRADIANCE = 20   # Wh/m² – darker hours carry no shape information
MIN_DAY_YIELD_KWH = 0.5      # days below this (outage, snow) are not learned
FACTOR_LIMITS = (0.2, 3.0)   # relative factor clamp, protects against odd data


@dataclass(frozen=True)
class Sample:
    """One learned hour: start (UTC epoch seconds), real yield, irradiance."""

    start: int          # epoch seconds, start of the hour
    energy_kwh: float   # real yield in that hour
    irradiance: float   # Wh/m² in that hour (mean W/m² over the hour)


# ---------------------------------------------------------------------------
# Open-Meteo parsing
# ---------------------------------------------------------------------------

def hours_from_open_meteo(times: Iterable[int], values: Iterable[float | None]) -> dict[int, float]:
    """Map Open-Meteo hourly radiation to {hour start epoch: Wh/m²}.

    Open-Meteo reports radiation as the mean of the PRECEDING hour: the value
    at 13:00 covers 12:00–13:00. Times must be unix timestamps
    (``timeformat=unixtime``), which keeps DST changes unambiguous.
    """
    result: dict[int, float] = {}
    for t, v in zip(times, values):
        if t is None:
            continue
        result[int(t) - 3600] = float(v or 0.0)
    return result


def local_date(epoch: int, tz: tzinfo) -> date:
    """Local calendar date of an hour, judged by its start."""
    return datetime.fromtimestamp(epoch, tz).date()


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

def sun_bin(epoch: int, latitude: float, longitude: float) -> str | None:
    """Sun-position bin of an hour (sun at the middle of the hour)."""
    mid = datetime.fromtimestamp(epoch + 1800, timezone.utc)
    azimuth, elevation = sun_position(mid, latitude, longitude)
    if elevation <= 0:
        return None
    az = int(azimuth // AZIMUTH_STEP) * AZIMUTH_STEP
    for low, high in zip(ELEVATION_BANDS, ELEVATION_BANDS[1:]):
        if low <= elevation < high:
            return f"{az}/{low}"
    return None


def learn_factors(
    samples: Iterable[Sample],
    latitude: float,
    longitude: float,
    now: datetime,
    tz: tzinfo,
    learn_from: date | None = None,
) -> dict[str, float]:
    """Learn a relative correction factor per sun-position bin.

    Returns {bin: factor}, where 1.0 means "behaves like the overall average".
    Bins without data are absent; callers treat them as 1.0.
    """
    samples = [s for s in samples if learn_from is None or local_date(s.start, tz) >= learn_from]

    # Skip whole days with (almost) no yield: outages, snow on the panels
    per_day: dict[date, float] = {}
    for s in samples:
        d = local_date(s.start, tz)
        per_day[d] = per_day.get(d, 0.0) + s.energy_kwh
    samples = [s for s in samples if per_day[local_date(s.start, tz)] >= MIN_DAY_YIELD_KWH]

    now_epoch = now.timestamp()
    total_e = total_i = 0.0
    bins: dict[str, list[float]] = {}
    for s in samples:
        if s.irradiance < MIN_SAMPLE_IRRADIANCE:
            continue
        key = sun_bin(s.start, latitude, longitude)
        if key is None:
            continue
        age_days = max(0.0, (now_epoch - s.start) / 86400)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        irr_kwh = s.irradiance / 1000 * weight
        energy = s.energy_kwh * weight
        total_e += energy
        total_i += irr_kwh
        acc = bins.setdefault(key, [0.0, 0.0])
        acc[0] += energy
        acc[1] += irr_kwh

    if total_i <= 0 or total_e <= 0:
        return {}
    overall = total_e / total_i
    low, high = FACTOR_LIMITS
    factors: dict[str, float] = {}
    for key, (energy, irr) in bins.items():
        shrunk = (energy + PRIOR_IRRADIANCE_KWH * overall) / (irr + PRIOR_IRRADIANCE_KWH)
        factors[key] = round(min(high, max(low, shrunk / overall)), 4)
    return factors


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

def hourly_forecast(
    irradiance: dict[int, float],
    daily_totals: dict[date, float],
    factors: dict[str, float],
    latitude: float,
    longitude: float,
    tz: tzinfo,
) -> list[tuple[int, float]]:
    """Spread each day's forecast total over its hours.

    irradiance: {hour start epoch: Wh/m²}; daily_totals: {local date: kWh}.
    Returns a sorted list of (hour start epoch, kWh). Hours of days without a
    daily total are left out.
    """
    shape: dict[date, dict[int, float]] = {}
    for epoch, irr in irradiance.items():
        d = local_date(epoch, tz)
        if d not in daily_totals:
            continue
        key = sun_bin(epoch, latitude, longitude)
        weight = irr * factors.get(key, 1.0) if key is not None else 0.0
        shape.setdefault(d, {})[epoch] = max(0.0, weight)

    result: list[tuple[int, float]] = []
    for d, hours in shape.items():
        total_shape = sum(hours.values())
        total = max(0.0, daily_totals[d] or 0.0)
        for epoch, weight in hours.items():
            kwh = total * weight / total_shape if total_shape > 0 else 0.0
            result.append((epoch, round(kwh, 4)))
    result.sort()
    return result


# ---------------------------------------------------------------------------
# Reading the series
# ---------------------------------------------------------------------------

def energy_between(series: list[tuple[int, float]], start: float, end: float) -> float:
    """Energy (kWh) between two epochs, hours counted pro rata."""
    total = 0.0
    for epoch, kwh in series:
        overlap = min(end, epoch + 3600) - max(start, epoch)
        if overlap > 0:
            total += kwh * overlap / 3600
    return total


def power_at(series: list[tuple[int, float]], moment: float) -> float:
    """Expected power (W) at a moment.

    Each hour's mean power sits at the middle of the hour; between two
    middles the power is interpolated linearly. Before the first and after
    the last daylight hour it falls to zero at the hour edge.
    """
    by_start = dict(series)
    hour = int(moment // 3600 * 3600)
    mid = hour + 1800
    if moment >= mid:
        a_start, b_start = hour, hour + 3600
    else:
        a_start, b_start = hour - 3600, hour
    a = by_start.get(a_start, 0.0) * 1000
    b = by_start.get(b_start, 0.0) * 1000
    frac = (moment - (a_start + 1800)) / 3600
    return max(0.0, a + (b - a) * frac)


def peak_hour(series: list[tuple[int, float]], day: date, tz: tzinfo) -> tuple[int, float] | None:
    """Strongest hour of a local day as (start epoch, kWh), None if no sun."""
    hours = [(e, k) for e, k in series if local_date(e, tz) == day and k > 0]
    if not hours:
        return None
    return max(hours, key=lambda h: (h[1], -h[0]))


def best_window(
    series: list[tuple[int, float]],
    hours: int,
    day: date,
    tz: tzinfo,
    not_before: float,
) -> tuple[int, float] | None:
    """Best run of consecutive hours within a local day.

    Considers only windows that start at or after the hour containing
    ``not_before``. Returns (start epoch, kWh) or None if nothing is left.
    """
    by_start = dict(series)
    first = int(not_before // 3600 * 3600)
    starts = sorted(e for e in by_start if local_date(e, tz) == day and e >= first)
    best: tuple[int, float] | None = None
    for start in starts:
        window = [start + i * 3600 for i in range(hours)]
        if any(w not in by_start or local_date(w, tz) != day for w in window):
            continue
        energy = sum(by_start[w] for w in window)
        if energy <= 0:
            continue
        if best is None or energy > best[1] + 1e-9:
            best = (start, round(energy, 3))
    return best
