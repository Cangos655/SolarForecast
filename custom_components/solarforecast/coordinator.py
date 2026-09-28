"""DataUpdateCoordinator for Solar Forecast – fetches weather, auto-trains ML model."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ARCHIVE_LOOKBACK_DAYS,
    CONF_CELL_TEMP_OFFSET,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_SOLAR_SENSOR,
    CONF_TEMP_COEFFICIENT,
    DEFAULT_CELL_TEMP_OFFSET,
    DEFAULT_TEMP_COEFFICIENT,
    DOMAIN,
    MIN_YIELD_KWH,
    UPDATE_INTERVAL,
)
from .ml_engine import (
    TrainingEntry,
    calculate_expected_yield,
    get_bucket,
    get_model_accuracy,
    save_training_entry,
)
from .weather_api import fetch_forecast, fetch_historical_range

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = f"{DOMAIN}_{{entry_id}}_history"

# Bump this when the date attribution logic changes to force a clean retrain
CURRENT_DATA_VERSION = 5

# A stored training entry is retrained once the recorder shows at least this
# many times more yield for the same day. Daily-resetting yield sensors (e.g.
# cloud-polled inverters) can briefly go unavailable right around their peak
# reading, undercounting that day when it's first trained; this threshold is
# low enough to catch that (typically ~2x) without reacting to normal
# day-to-day weather variance.
STALE_ENTRY_FACTOR = 1.5


class SolarForecastCoordinator(DataUpdateCoordinator):
    """Manages data fetching, auto-training, and forecast calculation."""

    def __init__(self, hass: HomeAssistant, config_entry) -> None:
        self.config_entry = config_entry
        # Options override data (changed via the gear icon after initial setup)
        cfg = {**config_entry.data, **config_entry.options}
        self._latitude: float = cfg[CONF_LATITUDE]
        self._longitude: float = cfg[CONF_LONGITUDE]
        self._solar_sensor: str = cfg[CONF_SOLAR_SENSOR]
        self._temp_coefficient: float = cfg.get(CONF_TEMP_COEFFICIENT, DEFAULT_TEMP_COEFFICIENT)
        self._cell_temp_offset: int = cfg.get(CONF_CELL_TEMP_OFFSET, DEFAULT_CELL_TEMP_OFFSET)

        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_TEMPLATE.format(entry_id=config_entry.entry_id),
        )
        self._history: list[TrainingEntry] = []

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=UPDATE_INTERVAL,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def history(self) -> list[TrainingEntry]:
        return self._history

    @property
    def training_count(self) -> int:
        return len([e for e in self._history if not e.get("is_auto_fill", False)])

    @property
    def training_per_bucket(self) -> dict[str, int]:
        """Return real training entry count per bucket."""
        real = [e for e in self._history if not e.get("is_auto_fill", False)]
        counts = {"sunny": 0, "mixed": 0, "overcast": 0}
        for e in real:
            bucket = e.get("bucket", "mixed")
            if bucket in counts:
                counts[bucket] += 1
        return counts

    @property
    def model_accuracy(self) -> float:
        return get_model_accuracy(self._history)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid_iso_date(value: str) -> bool:
        try:
            datetime.fromisoformat(str(value))
            return len(str(value)) == 10  # strict YYYY-MM-DD
        except (ValueError, TypeError):
            return False

    async def _load_history(self) -> None:
        stored = await self._store.async_load()
        if not stored or not isinstance(stored.get("history"), list):
            return

        # Clear history when data format changed (e.g. date attribution fix)
        data_version = stored.get("data_version", 1)
        if data_version < CURRENT_DATA_VERSION:
            _LOGGER.info(
                "Training data format changed (v%d → v%d), clearing history for clean retrain",
                data_version, CURRENT_DATA_VERSION,
            )
            self._history = []
            await self._save_history()
            return

        self._history = stored["history"]

        # Remove any stale entries with invalid ISO date format (e.g. "2026-03-20_auto_overcast")
        valid = [e for e in self._history if self._is_valid_iso_date(e.get("date", ""))]
        if len(valid) != len(self._history):
            removed = len(self._history) - len(valid)
            self._history = valid
            await self._save_history()
            _LOGGER.info("Removed %d stale entries with invalid date format", removed)
        else:
            _LOGGER.debug("Loaded %d training entries from storage", len(self._history))

    async def _save_history(self) -> None:
        await self._store.async_save({"history": self._history, "data_version": CURRENT_DATA_VERSION})

    # ------------------------------------------------------------------
    # HA Recorder: read daily solar statistics
    # ------------------------------------------------------------------

    async def _get_daily_solar_yields(self) -> dict[str, float]:
        """Return {date_str: kwh} for the past ARCHIVE_LOOKBACK_DAYS days."""
        now = dt_util.now()
        end_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_time = end_time - timedelta(days=ARCHIVE_LOOKBACK_DAYS)

        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                end_time,
                {self._solar_sensor},
                "day",
                {},
                {"sum"},
            )
        except Exception as exc:
            _LOGGER.warning("Could not read recorder statistics: %s", exc)
            return {}

        sensor_stats = stats.get(self._solar_sensor, [])
        if not sensor_stats:
            return {}

        today_str = dt_util.now().strftime("%Y-%m-%d")
        daily_yields: dict[str, float] = {}
        for i, row in enumerate(sensor_stats):
            if i == 0:
                continue  # need delta from previous row
            prev = sensor_stats[i - 1]
            # Support both dict (older HA) and object (newer HA) row formats
            row_sum = row["sum"] if isinstance(row, dict) else row.sum
            prev_sum = prev["sum"] if isinstance(prev, dict) else prev.sum
            if row_sum is None or prev_sum is None:
                continue
            delta = row_sum - prev_sum
            if delta < MIN_YIELD_KWH:
                continue

            # Resolve timestamps for both rows
            prev_start = prev["start"] if isinstance(prev, dict) else prev.start
            row_start = row["start"] if isinstance(row, dict) else row.start
            if isinstance(prev_start, (int, float)):
                prev_start = datetime.fromtimestamp(prev_start, tz=timezone.utc)
            if isinstance(row_start, (int, float)):
                row_start = datetime.fromtimestamp(row_start, tz=timezone.utc)

            # Skip entries where the statistics gap spans more than ~1 day.
            # This happens when the recorder has missing rows: the delta then
            # covers multiple days and would corrupt one training entry.
            gap_hours = (row_start - prev_start).total_seconds() / 3600
            if gap_hours > 30:
                _LOGGER.warning(
                    "Skipping recorder gap of %.1f h ending at %s – likely missing statistics",
                    gap_hours, dt_util.as_local(row_start).strftime("%Y-%m-%d"),
                )
                continue

            # HA statistics store the cumulative sum at the START of each day.
            # The delta between row[i-1] and row[i] is therefore the production
            # of the day that ENDS at row[i].start (= the day whose midnight is row_start).
            # Example: row_start = midnight local March 21 → delta = March 20 production
            #          → correct label is "2026-03-21" because that is when the day closed.
            # today_str exclusion below naturally prevents incomplete days from being trained.
            date_str = dt_util.as_local(row_start).strftime("%Y-%m-%d")
            if date_str == today_str:
                continue  # row_start is today's midnight → delta covers yesterday, but
                           # today's statistics row may not be final yet → skip
            daily_yields[date_str] = round(delta, 3)

        _LOGGER.debug("Read %d daily solar yield entries from recorder", len(daily_yields))
        return daily_yields

    # ------------------------------------------------------------------
    # Auto-training
    # ------------------------------------------------------------------

    async def _auto_train(self, daily_yields: dict[str, float]) -> bool:
        """Add new training entries for days not yet in history. Returns True if changed."""
        if not daily_yields:
            return False

        # Detect stale entries: recorder now shows a value clearly higher than what
        # was trained (e.g. the day was trained while still incomplete, or the
        # inverter had a brief unavailable gap that made the recorder undercount
        # that day when it was first read).
        stale_dates: set[str] = set()
        for entry in self._history:
            date = entry.get("date", "")
            if entry.get("is_auto_fill", False) or date not in daily_yields:
                continue
            stored = entry.get("yield_kwh", 0)
            recorded = daily_yields[date]
            if stored > 0 and recorded >= stored * STALE_ENTRY_FACTOR:
                stale_dates.add(date)
                _LOGGER.info(
                    "Stale entry %s: stored %.2f kWh but recorder shows %.2f kWh – will retrain",
                    date, stored, recorded,
                )

        if stale_dates:
            self._history = [e for e in self._history if e["date"] not in stale_dates]

        trained_dates = {e["date"] for e in self._history}
        new_dates = sorted(
            [d for d in daily_yields if d not in trained_dates], reverse=True
        )[:10]  # process at most 10 new days per update cycle

        if not new_dates and not stale_dates:
            return False

        session = async_get_clientsession(self.hass)
        start = min(new_dates)
        end = max(new_dates)

        weather_days = await fetch_historical_range(session, self._latitude, self._longitude, start, end)
        weather_by_date = {w["date"]: w for w in weather_days}

        changed = False
        for date_str in new_dates:
            weather = weather_by_date.get(date_str)
            if not weather:
                _LOGGER.debug("No archive weather for %s, skipping", date_str)
                continue
            yield_kwh = daily_yields[date_str]
            self._history = save_training_entry(
                date_str,
                yield_kwh,
                weather,
                self._history,
                self._temp_coefficient,
                self._cell_temp_offset,
            )
            _LOGGER.info("Auto-trained: %s → %.2f kWh (%s)", date_str, yield_kwh, weather.get("bucket", "?"))
            changed = True

        return changed

    # ------------------------------------------------------------------
    # DataUpdateCoordinator refresh
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch weather, auto-train, calculate forecasts."""
        session = async_get_clientsession(self.hass)

        # 1. Fetch forecast
        try:
            weather_data = await fetch_forecast(session, self._latitude, self._longitude)
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Open-Meteo API error: {exc}") from exc
        except Exception as exc:
            raise UpdateFailed(f"Unexpected error fetching weather: {exc}") from exc

        # 2. Read solar yields from HA recorder
        daily_yields = await self._get_daily_solar_yields()

        # 3. Auto-train ML model
        if await self._auto_train(daily_yields):
            await self._save_history()

        # 4. Calculate forecasts for each day
        forecast_list = weather_data.get("forecast", [])
        forecasts = []
        for day in forecast_list:
            expected_kwh = calculate_expected_yield(
                day,
                self._history,
                self._temp_coefficient,
                self._cell_temp_offset,
            )
            bucket = get_bucket(
                day.get("sunshine_duration", 0),
                day.get("daylight_duration", 1),
            )
            forecasts.append({
                **day,
                "expected_kwh": expected_kwh,
                "condition": bucket,
            })

        # Build compact history for sensor attributes
        training_history = []
        for e in self._history:
            training_history.append({
                "date": e.get("date"),
                "kwh": round(e.get("yield_kwh", 0), 2),
                "radiation": round(e.get("radiation", 0), 1),
                "bucket": e.get("bucket"),
                "optical_index": round(e.get("optical_index", 0), 4),
                "auto": e.get("is_auto_fill", False),
            })
        training_history.sort(key=lambda x: x["date"], reverse=True)

        return {
            "forecasts": forecasts,
            "yesterday": weather_data.get("yesterday"),
            "model_accuracy": self.model_accuracy,
            "training_count": self.training_count,
            "training_per_bucket": self.training_per_bucket,
            "training_history": training_history,
            "today_condition": forecasts[0]["condition"] if forecasts else "unknown",
        }
