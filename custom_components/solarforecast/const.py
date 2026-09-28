"""Constants for the Solar Forecast integration."""
from datetime import timedelta

DOMAIN = "solarforecast"
UPDATE_INTERVAL = timedelta(hours=6)

# Config entry keys
CONF_SOLAR_SENSOR = "solar_sensor"
CONF_LOCATION_MODE = "location_mode"
CONF_CITY = "city"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_LOCATION_NAME = "location_name"
CONF_TEMP_COEFFICIENT = "temp_coefficient"
CONF_CELL_TEMP_OFFSET = "cell_temp_offset"

LOCATION_MODE_HOME = "home"
LOCATION_MODE_CITY = "city"

# ML algorithm constants
MAX_HISTORY = 30
MAX_PER_BUCKET = 10

BUCKET_SUNNY = "sunny"
BUCKET_MIXED = "mixed"
BUCKET_OVERCAST = "overcast"

BUCKET_SUNNY_THRESHOLD = 0.7
BUCKET_MIXED_THRESHOLD = 0.3

# Physical efficiency ratios per bucket
BUCKET_RATIOS = {
    BUCKET_SUNNY: 1.0,
    BUCKET_MIXED: 0.88,
    BUCKET_OVERCAST: 0.75,
}

# Temperature defaults (can be overridden by user)
DEFAULT_TEMP_COEFFICIENT = 0.004   # 0.4% loss per °C above STC
DEFAULT_CELL_TEMP_OFFSET = 10      # Estimated cell temp offset above air temp
STC_REFERENCE_TEMP = 25            # Standard Test Conditions reference temperature

# Weighted average: newest entry gets weight 10, oldest gets weight 1
WEIGHT_NEWEST = 10
WEIGHT_OLDEST = 1

# Open-Meteo API
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

FORECAST_DAYS = 8
ARCHIVE_LOOKBACK_DAYS = 30
MIN_YIELD_KWH = 0.5   # Minimum daily yield to accept as a valid training entry
MAX_OPTICAL_INDEX = 4.0  # Physical sanity limit – values above this indicate a wrong sensor

# Sensor names
SENSOR_TODAY = "today"
SENSOR_TOMORROW = "tomorrow"
SENSOR_DAY_PREFIX = "day_"
SENSOR_MODEL_ACCURACY = "model_accuracy"
SENSOR_TRAINING_COUNT = "training_count"
SENSOR_TODAY_CONDITION = "today_condition"

# Frontend card bundled with this integration (auto-registered as a Lovelace resource)
CARD_FILENAME = "solarforecast-card.js"
CARD_URL_PATH = f"/{DOMAIN}_files"
