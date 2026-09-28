"""Solar Forecast ML Engine.

Weather-Clustered Machine Learning:
  Classifies days into 3 weather buckets (sunny / mixed / overcast) based on
  the clear-sky ratio (sunshine_hours / daylight_hours) and learns a separate
  optical efficiency index per bucket.  Forecasts are calculated as:

      expected_yield = radiation_sum × optical_index × temperature_penalty
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TypedDict

from .const import (
    BUCKET_MIXED,
    BUCKET_MIXED_THRESHOLD,
    BUCKET_OVERCAST,
    BUCKET_RATIOS,
    BUCKET_SUNNY,
    BUCKET_SUNNY_THRESHOLD,
    DEFAULT_CELL_TEMP_OFFSET,
    DEFAULT_TEMP_COEFFICIENT,
    MAX_HISTORY,
    MAX_OPTICAL_INDEX,
    MAX_PER_BUCKET,
    STC_REFERENCE_TEMP,
    WEIGHT_NEWEST,
    WEIGHT_OLDEST,
)

_LOGGER = logging.getLogger(__name__)


class TrainingEntry(TypedDict):
    date: str          # ISO date string "YYYY-MM-DD"
    yield_kwh: float   # Real kWh produced
    radiation: float   # MJ/m²
    temp_max: float    # °C
    bucket: str        # sunny | mixed | overcast
    optical_index: float
    is_auto_fill: bool


class ForecastDay(TypedDict):
    date: str
    radiation_sum: float   # MJ/m²
    temp_max: float        # °C
    sunshine_duration: float  # hours
    daylight_duration: float  # hours
    weather_code: int


# ---------------------------------------------------------------------------
# Bucket classification
# ---------------------------------------------------------------------------

def get_bucket(sunshine_hours: float, daylight_hours: float) -> str:
    """Classify a day into a weather bucket based on clear-sky ratio."""
    if not daylight_hours or daylight_hours == 0:
        return BUCKET_OVERCAST
    ratio = sunshine_hours / daylight_hours
    if ratio >= BUCKET_SUNNY_THRESHOLD:
        return BUCKET_SUNNY
    if ratio >= BUCKET_MIXED_THRESHOLD:
        return BUCKET_MIXED
    return BUCKET_OVERCAST


# ---------------------------------------------------------------------------
# Temperature penalty
# ---------------------------------------------------------------------------

def get_temp_penalty(
    temp_max: float,
    temp_coefficient: float = DEFAULT_TEMP_COEFFICIENT,
    cell_temp_offset: float = DEFAULT_CELL_TEMP_OFFSET,
) -> float:
    """Return efficiency factor accounting for panel temperature losses."""
    cell_temp = temp_max + cell_temp_offset
    penalty = 1.0 - ((cell_temp - STC_REFERENCE_TEMP) * temp_coefficient)
    return max(0.1, penalty)  # never below 10 % to avoid division issues


# ---------------------------------------------------------------------------
# Weighted average of optical indices for a given bucket
# ---------------------------------------------------------------------------

def get_average_index(history: list[TrainingEntry], bucket: str) -> float | None:
    """Return weighted average optical index for a bucket, or None if no data."""
    bucket_entries = [e for e in history if e["bucket"] == bucket]
    if not bucket_entries:
        return None

    # Sort newest first (date string sort works for ISO dates)
    bucket_entries.sort(key=lambda e: e["date"], reverse=True)

    weighted_sum = 0.0
    weight_total = 0.0
    for i, entry in enumerate(bucket_entries):
        weight = max(WEIGHT_OLDEST, WEIGHT_NEWEST - i)
        weighted_sum += entry["optical_index"] * weight
        weight_total += weight

    return weighted_sum / weight_total if weight_total > 0 else None


def get_effective_index(
    history: list[TrainingEntry],
    bucket: str,
) -> float:
    """Return the best available optical index for a bucket.

    Falls back to estimating from other buckets using physical ratios when
    the target bucket has not been trained yet.
    """
    direct = get_average_index(history, bucket)
    if direct is not None:
        return direct

    # Estimate from other buckets
    other_indices: list[float] = []
    for other_bucket, ratio in BUCKET_RATIOS.items():
        if other_bucket == bucket:
            continue
        idx = get_average_index(history, other_bucket)
        if idx is not None:
            # Convert to "sunny equivalent" then scale for target bucket
            sunny_equivalent = idx / ratio
            other_indices.append(sunny_equivalent)

    if other_indices:
        sunny_avg = sum(other_indices) / len(other_indices)
        return sunny_avg * BUCKET_RATIOS[bucket]

    # No training data at all – return physical ratio as placeholder
    return BUCKET_RATIOS[bucket]


# ---------------------------------------------------------------------------
# Forecast calculation
# ---------------------------------------------------------------------------

def calculate_expected_yield(
    day: ForecastDay,
    history: list[TrainingEntry],
    temp_coefficient: float = DEFAULT_TEMP_COEFFICIENT,
    cell_temp_offset: float = DEFAULT_CELL_TEMP_OFFSET,
) -> float:
    """Calculate expected kWh yield for a single forecast day."""
    if not day.get("radiation_sum") or day["radiation_sum"] <= 0:
        return 0.0

    bucket = get_bucket(day.get("sunshine_duration", 0), day.get("daylight_duration", 1))
    optical_index = get_effective_index(history, bucket)
    temp_penalty = get_temp_penalty(
        day.get("temp_max", 20),
        temp_coefficient,
        cell_temp_offset,
    )
    return round(day["radiation_sum"] * optical_index * temp_penalty, 2)


# ---------------------------------------------------------------------------
# Training: save a new daily entry
# ---------------------------------------------------------------------------

def save_training_entry(
    date_str: str,
    yield_kwh: float,
    weather_day: ForecastDay,
    history: list[TrainingEntry],
    temp_coefficient: float = DEFAULT_TEMP_COEFFICIENT,
    cell_temp_offset: float = DEFAULT_CELL_TEMP_OFFSET,
) -> list[TrainingEntry]:
    """Add a real yield observation to the training history.

    Removes any existing entry for the same date, applies auto-fill for
    untrained buckets on the first ever entry, and enforces per-bucket caps.
    Returns the updated history list.
    """
    radiation = weather_day.get("radiation_sum", 0)
    if radiation <= 0:
        _LOGGER.warning("Cannot train: radiation is 0 for %s", date_str)
        return history

    # Remove existing entry for same date
    updated = [e for e in history if e["date"] != date_str]

    bucket = get_bucket(
        weather_day.get("sunshine_duration", 0),
        weather_day.get("daylight_duration", 1),
    )
    temp_penalty = get_temp_penalty(
        weather_day.get("temp_max", 20),
        temp_coefficient,
        cell_temp_offset,
    )

    raw_index = yield_kwh / radiation
    optical_index = raw_index / temp_penalty if temp_penalty > 0 else raw_index

    if optical_index > MAX_OPTICAL_INDEX:
        _LOGGER.warning(
            "Skipping %s: optical_index %.2f > %.1f – sensor data likely invalid "
            "(wrong sensor configured or multi-day statistics gap)",
            date_str, optical_index, MAX_OPTICAL_INDEX,
        )
        return history

    new_entry: TrainingEntry = {
        "date": date_str,
        "yield_kwh": yield_kwh,
        "radiation": radiation,
        "temp_max": weather_day.get("temp_max", 20),
        "bucket": bucket,
        "optical_index": optical_index,
        "is_auto_fill": False,
    }
    updated.append(new_entry)

    # Auto-fill other buckets on very first real entry
    real_entries = [e for e in updated if not e.get("is_auto_fill", False)]
    if len(real_entries) == 1:
        updated = _auto_fill_missing_buckets(new_entry, updated, date_str)

    updated = _enforce_caps(updated)
    return updated


def _auto_fill_missing_buckets(
    real_entry: TrainingEntry,
    history: list[TrainingEntry],
    base_date: str,
) -> list[TrainingEntry]:
    """Generate plausible entries for untrained buckets based on physical ratios."""
    user_bucket = real_entry["bucket"]
    user_ratio = BUCKET_RATIOS[user_bucket]
    sunny_equivalent = real_entry["optical_index"] / user_ratio

    for bucket, ratio in BUCKET_RATIOS.items():
        if bucket == user_bucket:
            continue
        # Skip if real data already exists for this bucket
        if any(e["bucket"] == bucket and not e.get("is_auto_fill") for e in history):
            continue
        # Use a synthetic date offset to avoid collision
        offset = {"sunny": 1, "mixed": 2, "overcast": 3}
        try:
            synth_date = (
                date.fromisoformat(base_date) - timedelta(days=offset.get(bucket, 1))
            ).strftime("%Y-%m-%d")
        except Exception:
            synth_date = f"{base_date}_auto_{bucket}"

        auto_optical = sunny_equivalent * ratio
        auto_yield = real_entry["radiation"] * auto_optical * get_temp_penalty(real_entry["temp_max"])

        history.append({
            "date": synth_date,
            "yield_kwh": round(auto_yield, 2),
            "radiation": real_entry["radiation"],
            "temp_max": real_entry["temp_max"],
            "bucket": bucket,
            "optical_index": round(auto_optical, 4),
            "is_auto_fill": True,
        })

    return history


def _enforce_caps(history: list[TrainingEntry]) -> list[TrainingEntry]:
    """Enforce MAX_PER_BUCKET and MAX_HISTORY limits."""
    result: list[TrainingEntry] = []
    for bucket in (BUCKET_SUNNY, BUCKET_MIXED, BUCKET_OVERCAST):
        bucket_entries = [e for e in history if e["bucket"] == bucket]
        # Sort: real entries first (is_auto_fill=False sorts before True), then newest first
        bucket_entries.sort(
            key=lambda e: (e.get("is_auto_fill", False), e["date"]),
            reverse=True,
        )
        result.extend(bucket_entries[:MAX_PER_BUCKET])

    # Final overall cap
    result.sort(key=lambda e: e["date"], reverse=True)
    return result[:MAX_HISTORY]


# ---------------------------------------------------------------------------
# Model accuracy
# ---------------------------------------------------------------------------

def get_model_accuracy(history: list[TrainingEntry]) -> float:
    """Return model completeness as a percentage 0–100."""
    real_entries = [e for e in history if not e.get("is_auto_fill", False)]
    total_real = len(real_entries)

    # Bucket coverage bonus
    trained_buckets = len({e["bucket"] for e in real_entries})
    bucket_bonus = (trained_buckets / 3) * 10  # up to +10 % for full coverage

    base = min(90, (total_real / MAX_HISTORY) * 90)
    return round(min(100.0, base + bucket_bonus), 1)
