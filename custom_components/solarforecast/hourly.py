"""Hourly solar forecast: daily-shape model learned per sun position.

Pure Python (no Home Assistant imports) so the time handling can be tested
on its own. All hours are handled as timezone-aware datetimes; an hour is
identified by its START, matching the Home Assistant recorder and energy
dashboard.

How the hourly forecast is built:
  1. Open-Meteo gives, per hour, global, direct and diffuse irradiance. The
     irradiance on the panels is computed from them for an orientation:
     the one set in the options, or else the one that fits the system's own
     hourly yields best (``detect_orientation``). The detected orientation
     is an effective one: shade from a tree or a house shifts it, which is
     what the forecast needs.
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

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable, NamedTuple

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

# Orientation detection
GROUND_ALBEDO = 0.2
DETECT_WINDOW_DAYS = 60       # recent days only, so the result follows the season
DETECT_MIN_DAYS = 10          # days with enough yield before a result is trusted
DETECT_MIN_DAY_KWH = 1.0
DETECT_MIN_GAIN = 0.03        # must fit at least 3 % better than horizontal
DETECT_TILTS = range(0, 75, 5)
DETECT_AZIMUTH_STEP = 10


def detect_azimuths(latitude: float) -> list[int]:
    """Azimuths searched: the half facing the equator, east to west.

    A wider search fits the shade of the training days too closely (in a
    test on real data it gave a north-east plane that forecast worse).
    """
    first = 90 if latitude >= 0 else 270
    return [(first + step) % 360 for step in range(0, 181, DETECT_AZIMUTH_STEP)]


class Irradiance(NamedTuple):
    """Irradiance of one hour in Wh/m² (= mean W/m² over the hour)."""

    ghi: float  # global horizontal
    dni: float  # direct normal
    dhi: float  # diffuse horizontal


class Orientation(NamedTuple):
    """Panel orientation. Azimuth: 0 = north, 90 = east, 180 = south."""

    tilt: float
    azimuth: float


@dataclass(frozen=True)
class Sample:
    """One learned hour: start (UTC epoch seconds), real yield, irradiance."""

    start: int          # epoch seconds, start of the hour
    energy_kwh: float   # real yield in that hour
    irradiance: Irradiance


# ---------------------------------------------------------------------------
# Open-Meteo parsing
# ---------------------------------------------------------------------------

def hours_from_open_meteo(
    times: Iterable[int],
    ghi: Iterable[float | None],
    dni: Iterable[float | None],
    dhi: Iterable[float | None],
) -> dict[int, Irradiance]:
    """Map Open-Meteo hourly radiation to {hour start epoch: Irradiance}.

    Open-Meteo reports radiation as the mean of the PRECEDING hour: the value
    at 13:00 covers 12:00–13:00. Times must be unix timestamps
    (``timeformat=unixtime``), which keeps DST changes unambiguous.
    """
    result: dict[int, Irradiance] = {}
    for t, g, b, d in zip(times, ghi, dni, dhi):
        if t is None:
            continue
        result[int(t) - 3600] = Irradiance(float(g or 0.0), float(b or 0.0), float(d or 0.0))
    return result


def local_date(epoch: int, tz: tzinfo) -> date:
    """Local calendar date of an hour, judged by its start."""
    return datetime.fromtimestamp(epoch, tz).date()


# ---------------------------------------------------------------------------
# Irradiance on the panels
# ---------------------------------------------------------------------------

class _Sun(NamedTuple):
    cos_zenith: float
    sin_zenith: float
    azimuth: float  # radians


def _sun(epoch: int, latitude: float, longitude: float) -> _Sun | None:
    """Sun at the middle of the hour, None when it is below the horizon."""
    mid = datetime.fromtimestamp(epoch + 1800, timezone.utc)
    azimuth, elevation = sun_position(mid, latitude, longitude)
    if elevation <= 0:
        return None
    zenith = math.radians(90 - elevation)
    return _Sun(math.cos(zenith), math.sin(zenith), math.radians(azimuth))


def _plane(irr: Irradiance, sun: _Sun | None, orientation: Orientation | None) -> float:
    """Irradiance on a plane (isotropic sky model), Wh/m²."""
    if orientation is None:
        return irr.ghi
    if sun is None:
        return 0.0
    tilt = math.radians(orientation.tilt)
    cos_tilt = math.cos(tilt)
    cos_incidence = sun.cos_zenith * cos_tilt + sun.sin_zenith * math.sin(tilt) * math.cos(
        sun.azimuth - math.radians(orientation.azimuth)
    )
    beam = irr.dni * max(0.0, cos_incidence)
    sky = irr.dhi * (1 + cos_tilt) / 2
    ground = irr.ghi * GROUND_ALBEDO * (1 - cos_tilt) / 2
    return beam + sky + ground


def plane_irradiance(
    epoch: int,
    irr: Irradiance,
    latitude: float,
    longitude: float,
    orientation: Orientation | None,
) -> float:
    """Irradiance on the panels in one hour; horizontal without orientation."""
    if orientation is None:
        return irr.ghi
    return _plane(irr, _sun(epoch, latitude, longitude), orientation)


# ---------------------------------------------------------------------------
# Orientation detection
# ---------------------------------------------------------------------------

def detect_orientation(
    samples: Iterable[Sample],
    latitude: float,
    longitude: float,
    now: datetime,
    tz: tzinfo,
    learn_from: date | None = None,
) -> Orientation | None:
    """Find the orientation whose daily shape fits the real yields best.

    Each day's hours are compared as shares of the day, so only the shape
    counts, not the size of the system. Uses the last DETECT_WINDOW_DAYS
    days. Returns None (horizontal) without enough days, or when no
    orientation fits clearly better than horizontal.
    """
    first = (now - timedelta(days=DETECT_WINDOW_DAYS)).timestamp()
    by_day: dict[date, list[Sample]] = {}
    for s in samples:
        if s.start < first:
            continue
        d = local_date(s.start, tz)
        if learn_from is not None and d < learn_from:
            continue
        by_day.setdefault(d, []).append(s)

    days: list[tuple[float, list[tuple[float, Irradiance, _Sun | None]]]] = []
    for hours in by_day.values():
        total = sum(s.energy_kwh for s in hours)
        if total < DETECT_MIN_DAY_KWH:
            continue
        days.append((total, [(s.energy_kwh, s.irradiance, _sun(s.start, latitude, longitude)) for s in hours]))
    if len(days) < DETECT_MIN_DAYS:
        return None

    def error(orientation: Orientation | None) -> float:
        err = weight = 0.0
        for total, hours in days:
            plane = [_plane(irr, sun, orientation) for _, irr, sun in hours]
            plane_total = sum(plane)
            if plane_total <= 0:
                continue
            for (energy, _, _), p in zip(hours, plane):
                err += total * (energy / total - p / plane_total) ** 2
            weight += total
        return err / weight if weight else math.inf

    best = (math.inf, Orientation(0, 180))
    for tilt in DETECT_TILTS:
        for azimuth in detect_azimuths(latitude):
            candidate = Orientation(tilt, azimuth)
            best = min(best, (error(candidate), candidate))
    _, coarse = best
    for tilt in range(max(0, int(coarse.tilt) - 4), min(90, int(coarse.tilt) + 5)):
        for azimuth in range(int(coarse.azimuth) - 8, int(coarse.azimuth) + 9, 2):
            candidate = Orientation(tilt, azimuth % 360)
            best = min(best, (error(candidate), candidate))

    best_error, orientation = best
    flat_error = error(None)
    if not best_error < flat_error * (1 - DETECT_MIN_GAIN):
        return None
    return orientation


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
    orientation: Orientation | None = None,
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
        irradiance = plane_irradiance(s.start, s.irradiance, latitude, longitude, orientation)
        if irradiance < MIN_SAMPLE_IRRADIANCE:
            continue
        key = sun_bin(s.start, latitude, longitude)
        if key is None:
            continue
        age_days = max(0.0, (now_epoch - s.start) / 86400)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        irr_kwh = irradiance / 1000 * weight
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
    irradiance: dict[int, Irradiance],
    daily_totals: dict[date, float],
    factors: dict[str, float],
    latitude: float,
    longitude: float,
    tz: tzinfo,
    orientation: Orientation | None = None,
) -> list[tuple[int, float]]:
    """Spread each day's forecast total over its hours.

    irradiance: {hour start epoch: Irradiance}; daily_totals: {local date: kWh}.
    Returns a sorted list of (hour start epoch, kWh). Hours of days without a
    daily total are left out.
    """
    shape: dict[date, dict[int, float]] = {}
    for epoch, irr in irradiance.items():
        d = local_date(epoch, tz)
        if d not in daily_totals:
            continue
        key = sun_bin(epoch, latitude, longitude)
        plane = plane_irradiance(epoch, irr, latitude, longitude, orientation)
        weight = plane * factors.get(key, 1.0) if key is not None else 0.0
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


# ---------------------------------------------------------------------------
# Honest past: frozen hours and the morning forecast
# ---------------------------------------------------------------------------

MORNING_HOUR = 6         # local hour from which the day's morning forecast is kept
FROZEN_KEEP_HOURS = 48   # frozen hours older than this are dropped


def freeze_started_hours(
    previous: list[tuple[int, float]],
    frozen: dict[int, float],
    now: float,
) -> dict[int, float]:
    """Keep the forecast of hours that have begun.

    ``previous`` is the series of the last update, calculated before ``now``.
    Every hour that has begun since keeps the value it had then, so a later
    forecast cannot rewrite it. Hours already frozen stay unchanged.
    """
    result = {e: k for e, k in frozen.items() if e >= now - FROZEN_KEEP_HOURS * 3600}
    for epoch, kwh in previous:
        if epoch <= now and epoch not in result:
            result[epoch] = kwh
    return result


def apply_frozen(series: list[tuple[int, float]], frozen: dict[int, float]) -> list[tuple[int, float]]:
    """The series with frozen hours put back to the value they were forecast with."""
    return [(epoch, frozen.get(epoch, kwh)) for epoch, kwh in series]


def morning_due(morning_day: date | None, now: datetime, tz: tzinfo) -> bool:
    """True when today's morning forecast has not been kept yet and it is time."""
    local = now.astimezone(tz)
    return morning_day != local.date() and local.hour >= MORNING_HOUR


def day_hours(series: list[tuple[int, float]], day: date, tz: tzinfo) -> dict[int, float]:
    """Hours of one local day from a series."""
    return {e: k for e, k in series if local_date(e, tz) == day}
