"""DataUpdateCoordinator for Solar Forecast – fetches weather, auto-trains ML model."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
    CONF_AZIMUTH,
    CONF_CELL_TEMP_OFFSET,
    CONF_LATITUDE,
    CONF_LEARN_FROM,
    CONF_LONGITUDE,
    CONF_SOLAR_SENSOR,
    CONF_TEMP_COEFFICIENT,
    CONF_TILT,
    DEFAULT_CELL_TEMP_OFFSET,
    DEFAULT_TEMP_COEFFICIENT,
    DOMAIN,
    HOURLY_BOOTSTRAP_DAYS,
    HOURLY_MAX_DAYS,
    MIN_YIELD_KWH,
    UPDATE_INTERVAL,
)
from .hourly import (
    Irradiance,
    Orientation,
    Sample,
    detect_orientation,
    hourly_forecast,
    learn_factors,
)
from .ml_engine import (
    TrainingEntry,
    calculate_expected_yield,
    get_bucket,
    get_model_accuracy,
    save_training_entry,
)
from .weather_api import fetch_forecast, fetch_historical_range, fetch_hourly_history

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = f"{DOMAIN}_{{entry_id}}_history"
HOURLY_STORAGE_KEY_TEMPLATE = f"{DOMAIN}_{{entry_id}}_hourly"
# Learned hours keep global, direct and diffuse irradiance (format 2), so any
# orientation can be computed from them without fetching again.
HOURLY_FORMAT = 2

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
        tilt = _optional_float(cfg.get(CONF_TILT))
        azimuth = _optional_float(cfg.get(CONF_AZIMUTH))
        # Set in the options it wins; otherwise the orientation is detected
        self._manual_orientation: Orientation | None = (
            Orientation(tilt, azimuth) if tilt is not None and azimuth is not None else None
        )
        self._detected_orientation: Orientation | None = None
        self._detected_on: date | None = None
        self._learn_from: date | None = _optional_date(cfg.get(CONF_LEARN_FROM))

        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_TEMPLATE.format(entry_id=config_entry.entry_id),
        )
        self._history: list[TrainingEntry] = []

        # Hourly model: learned hours (see hourly.py)
        self._hourly_store: Store = Store(
            hass,
            STORAGE_VERSION,
            HOURLY_STORAGE_KEY_TEMPLATE.format(entry_id=config_entry.entry_id),
        )
        self._samples: list[Sample] = []
        self._hourly_checked: date | None = None

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

    @property
    def orientation(self) -> Orientation | None:
        """Orientation used for the hourly shape, None for horizontal."""
        return self._manual_orientation or self._detected_orientation

    @property
    def orientation_source(self) -> str:
        if self._manual_orientation is not None:
            return "manual"
        if self._detected_orientation is not None:
            return "detected"
        return "horizontal"

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
        if self._learn_from is not None:
            cutoff = self._learn_from.isoformat()
            kept = [e for e in self._history if e.get("date", "") >= cutoff]
            if len(kept) != len(self._history):
                _LOGGER.info("Dropping %d training entries before %s", len(self._history) - len(kept), cutoff)
                self._history = kept
                await self._save_history()

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

    async def _load_hourly(self) -> None:
        stored = await self._hourly_store.async_load()
        if not stored or stored.get("format") != HOURLY_FORMAT:
            # New install, or hours stored by 1.1.0b1 (irradiance of one
            # orientation only): learn again from the recorder.
            self._samples = []
            return
        self._samples = [
            Sample(int(t), float(e), Irradiance(float(g), float(b), float(d)))
            for t, e, g, b, d in stored.get("samples", [])
        ]
        detected = stored.get("detected")
        if detected:
            self._detected_orientation = Orientation(float(detected[0]), float(detected[1]))
        self._detected_on = _optional_date(stored.get("detected_on"))
        _LOGGER.debug("Loaded %d learned hours", len(self._samples))

    async def _save_hourly(self) -> None:
        detected = self._detected_orientation
        await self._hourly_store.async_save({
            "format": HOURLY_FORMAT,
            "samples": [
                [s.start, round(s.energy_kwh, 4), *(round(v, 1) for v in s.irradiance)]
                for s in self._samples
            ],
            "detected": [detected.tilt, detected.azimuth] if detected else None,
            "detected_on": self._detected_on.isoformat() if self._detected_on else None,
        })

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

    async def _get_hourly_solar_yields(self, start: datetime, end: datetime) -> dict[int, float]:
        """Return {hour start epoch: kWh} from the recorder's hourly statistics."""
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                {self._solar_sensor},
                "hour",
                None,
                {"change"},
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not read hourly recorder statistics: %s", exc)
            return {}

        yields: dict[int, float] = {}
        for row in stats.get(self._solar_sensor, []):
            row_start = row["start"] if isinstance(row, dict) else row.start
            change = row.get("change") if isinstance(row, dict) else getattr(row, "change", None)
            if change is None:
                continue
            if isinstance(row_start, datetime):
                row_start = row_start.timestamp()
            # Negative changes are meter resets, not production
            yields[int(row_start)] = max(0.0, float(change))
        return yields

    async def _update_hourly_samples(self) -> bool:
        """Learn the hours of days not learned yet.

        Succeeds once per day; after a failed request it tries again with the
        next update. The last learned day is fetched again, so hours the
        weather archive did not have yet are filled in later.
        """
        today = dt_util.now().date()
        if self._hourly_checked == today:
            return False

        yesterday = today - timedelta(days=1)
        if self._samples:
            first_day = dt_util.as_local(dt_util.utc_from_timestamp(max(s.start for s in self._samples))).date()
        else:
            first_day = today - timedelta(days=HOURLY_BOOTSTRAP_DAYS)
        if self._learn_from is not None:
            first_day = max(first_day, self._learn_from)
        first_day = max(first_day, today - timedelta(days=HOURLY_BOOTSTRAP_DAYS))
        if first_day > yesterday:
            self._hourly_checked = today
            return False

        start = dt_util.start_of_local_day(first_day)
        end = dt_util.start_of_local_day(today)
        yields = await self._get_hourly_solar_yields(start, end)
        if not yields:
            return False

        session = async_get_clientsession(self.hass)
        # UTC dates around the local range, the API is queried in UTC
        irradiance = await fetch_hourly_history(
            session,
            self._latitude,
            self._longitude,
            (first_day - timedelta(days=1)).isoformat(),
            yesterday.isoformat(),
        )
        if not irradiance:
            return False

        known = {s.start for s in self._samples}
        start_epoch, end_epoch = start.timestamp(), end.timestamp()
        new = [
            Sample(epoch, yields[epoch], irradiance[epoch])
            for epoch in sorted(yields)
            if epoch in irradiance
            and epoch not in known
            and start_epoch <= epoch < end_epoch
            and (irradiance[epoch].ghi > 0 or yields[epoch] > 0)
        ]
        self._hourly_checked = today
        if not new:
            return False

        oldest = (dt_util.utcnow() - timedelta(days=HOURLY_MAX_DAYS)).timestamp()
        self._samples = [s for s in self._samples + new if s.start >= oldest]
        _LOGGER.info("Hourly model: learned %d new hours (%d in total)", len(new), len(self._samples))
        return True

    async def _update_detection(self, tz: ZoneInfo) -> bool:
        """Detect the orientation once a day from the learned hours.

        Runs on the recent days only, so the result follows the season
        (and with it the shade of trees or houses). Returns True when the
        stored state changed.
        """
        today = dt_util.now().date()
        if self._manual_orientation is not None or self._detected_on == today or not self._samples:
            return False
        found = await self.hass.async_add_executor_job(
            detect_orientation,
            list(self._samples),
            self._latitude,
            self._longitude,
            dt_util.utcnow(),
            tz,
            self._learn_from,
        )
        if found != self._detected_orientation:
            if found is None:
                _LOGGER.info("Hourly model: not enough data to detect the orientation, using horizontal")
            else:
                _LOGGER.info("Hourly model: detected orientation %g° tilt, %g° azimuth", found.tilt, found.azimuth)
        self._detected_orientation = found
        self._detected_on = today
        return True

    # ------------------------------------------------------------------
    # Auto-training
    # ------------------------------------------------------------------

    async def _auto_train(self, daily_yields: dict[str, float]) -> bool:
        """Add new training entries for days not yet in history. Returns True if changed."""
        if self._learn_from is not None:
            cutoff = self._learn_from.isoformat()
            daily_yields = {d: v for d, v in daily_yields.items() if d >= cutoff}
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

        # 5. Hourly series: each day's total spread by learned sun-position shape
        tz = ZoneInfo(weather_data.get("timezone") or "UTC")
        try:
            changed = await self._update_hourly_samples()
            changed = await self._update_detection(tz) or changed
            if changed:
                await self._save_hourly()
        except Exception:  # noqa: BLE001 - the daily forecast must not fail over this
            _LOGGER.warning("Hourly learning failed", exc_info=True)

        orientation = self.orientation
        factors = learn_factors(
            self._samples, self._latitude, self._longitude, dt_util.utcnow(), tz, self._learn_from, orientation
        )
        hourly = hourly_forecast(
            weather_data.get("hourly", {}),
            {date.fromisoformat(f["date"]): f["expected_kwh"] for f in forecasts},
            factors,
            self._latitude,
            self._longitude,
            tz,
            orientation,
        )

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
            "hourly": hourly,
            "timezone": str(tz),
            "hourly_learned_hours": len(self._samples),
            "hourly_bins": len(factors),
            "hourly_source": self.orientation_source,
            "orientation": (
                {"tilt": orientation.tilt, "azimuth": orientation.azimuth} if orientation else None
            ),
        }


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
