"""Sun position (azimuth / elevation) for a place and a moment.

NOAA solar position approximation, accurate to well under a degree, which is
plenty for sorting hours into sun-position bins. Pure Python, no Home
Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def sun_position(when: datetime, latitude: float, longitude: float) -> tuple[float, float]:
    """Return (azimuth, elevation) in degrees for an aware datetime.

    Azimuth: 0 = north, 90 = east, 180 = south, 270 = west.
    Elevation: degrees above the horizon (negative below), without refraction.
    """
    if when.tzinfo is None:
        raise ValueError("when must be timezone aware")
    utc = when.astimezone(timezone.utc)

    day_of_year = utc.timetuple().tm_yday
    hours = utc.hour + utc.minute / 60 + utc.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hours - 12) / 24)

    eq_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    true_solar_minutes = hours * 60 + eq_time + 4 * longitude
    hour_angle = math.radians(true_solar_minutes / 4 - 180)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90 - math.degrees(zenith)

    # Azimuth measured from north, clockwise
    azimuth = math.degrees(
        math.atan2(
            math.sin(hour_angle),
            math.cos(hour_angle) * math.sin(lat) - math.tan(decl) * math.cos(lat),
        )
    ) + 180
    return azimuth % 360, elevation
