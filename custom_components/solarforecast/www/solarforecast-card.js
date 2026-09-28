/**
 * Solar Forecast Lovelace Card
 * Custom element for Home Assistant dashboards.
 *
 * Config:
 *   type: custom:solarforecast-card
 *   entity_prefix: sensor.solarforecast   (optional, default: "sensor.solarforecast")
 *   title: "My Solar Forecast"         (optional)
 */

const CARD_VERSION = "1.0.0";

const WEATHER_ICONS = {
  0: "☀️", 1: "🌤", 2: "⛅", 3: "☁️",
  45: "🌫", 48: "🌫",
  51: "🌦", 53: "🌦", 55: "🌧",
  61: "🌧", 63: "🌧", 65: "🌧",
  71: "🌨", 73: "🌨", 75: "❄️",
  80: "🌦", 81: "🌧", 82: "⛈",
  95: "⛈", 96: "⛈", 99: "⛈",
};

const CONDITION_COLORS = {
  sunny: "#f59e0b",
  mixed: "#6366f1",
  overcast: "#64748b",
};

const CONDITION_LABELS = {
  sunny: "Sonnig",
  mixed: "Wechselhaft",
  overcast: "Bewölkt",
};

const DAY_KEYS = [
  "today", "tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7", "day_8"
];

const DAY_LABELS = [
  "Today", "Tomorrow", "Day 3", "Day 4", "Day 5", "Day 6", "Day 7", "Day 8"
];

function getWeatherIcon(code) {
  if (code === undefined || code === null) return "🌤";
  for (const key of Object.keys(WEATHER_ICONS).map(Number).sort((a, b) => b - a)) {
    if (code >= key) return WEATHER_ICONS[key];
  }
  return "🌤";
}

function formatDate(dateStr) {
  if (!dateStr) return "";
  try {
    const d = new Date(dateStr + "T00:00:00");
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  } catch {
    return dateStr;
  }
}

function shortWeekday(dateStr) {
  if (!dateStr) return "";
  try {
    const d = new Date(dateStr + "T00:00:00");
    return d.toLocaleDateString(undefined, { weekday: "short" });
  } catch {
    return "";
  }
}

// Shared auto-discovery function (used by card + editor)
function discoverSolarForecastEntities(hass) {
  if (!hass) return null;
  const states = hass.states;
  const d = {};
  const suffixes = {
    entity_today: ["_today"], entity_tomorrow: ["_tomorrow"],
    entity_day3: ["_day_3"], entity_day4: ["_day_4"], entity_day5: ["_day_5"],
    entity_day6: ["_day_6"], entity_day7: ["_day_7"], entity_day8: ["_day_8"],
    entity_accuracy: ["_model_accuracy"],
    entity_training: ["_training_count", "_training_entries"],
    entity_condition: ["_today_condition"],
  };
  // Find "today" sensor
  for (const [id, state] of Object.entries(states)) {
    if (id.startsWith("sensor.") && id.endsWith("_today") && state.attributes?.date) {
      d.entity_today = id; break;
    }
  }
  if (!d.entity_today) return null;
  const forecastPrefix = d.entity_today.slice(0, -6);
  const metaPrefix = forecastPrefix.replace(/_forecast$/, "");
  for (const [key, suffixList] of Object.entries(suffixes)) {
    if (key === "entity_today") continue;
    const isMeta = ["entity_accuracy", "entity_training", "entity_condition"].includes(key);
    const prefix = isMeta ? metaPrefix : forecastPrefix;
    let found = false;
    for (const suffix of suffixList) {
      const candidate = prefix + suffix;
      if (states[candidate]) { d[key] = candidate; found = true; break; }
    }
    if (found) continue;
    for (const suffix of suffixList) {
      for (const id of Object.keys(states)) {
        if (id.startsWith("sensor.") && id.endsWith(suffix)) {
          d[key] = id; found = true; break;
        }
      }
      if (found) break;
    }
  }
  return d;
}

class SolarForecastCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
  }

  static getConfigElement() {
    return document.createElement("solarforecast-card-editor");
  }

  static getStubConfig() {
    return { title: "Solar Forecast" };
  }

  setConfig(config) {
    this._config = { title: "Solar Forecast", ...config };
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _s(entityId) {
    if (!entityId || !this._hass) return undefined;
    return this._hass.states[entityId];
  }

  _discoverEntities() {
    return discoverSolarForecastEntities(this._hass);
  }

  _render() {
    if (!this._hass) return;

    const cfg = this._config;

    // Auto-discover defaults, then let manual config override per field
    const disc = this._discoverEntities();
    if (!disc && !cfg.entity_today) {
      this.shadowRoot.innerHTML = `
        <style>:host{display:block;}.card{background:var(--card-background-color);border-radius:16px;padding:24px;font-family:sans-serif;color:var(--primary-text-color);text-align:center;opacity:0.7;}</style>
        <div class="card">
          <div style="text-align:right;font-size:10px;opacity:0.3;margin-bottom:8px;">v${CARD_VERSION}</div>
          <div style="font-size:40px;margin-bottom:12px;">☀️</div>
          <div style="font-weight:600;margin-bottom:6px;">Solar Forecast</div>
          <div style="font-size:13px;opacity:0.6;">Warte auf Daten…<br>Integration einrichten oder Sensoren manuell konfigurieren.</div>
        </div>`;
      return;
    }
    const entityToday     = cfg.entity_today     || (disc && disc.entity_today);
    const entityTomorrow  = cfg.entity_tomorrow  || (disc && disc.entity_tomorrow);
    const entityDay3      = cfg.entity_day3      || (disc && disc.entity_day3);
    const entityDay4      = cfg.entity_day4      || (disc && disc.entity_day4);
    const entityDay5      = cfg.entity_day5      || (disc && disc.entity_day5);
    const entityDay6      = cfg.entity_day6      || (disc && disc.entity_day6);
    const entityDay7      = cfg.entity_day7      || (disc && disc.entity_day7);
    const entityDay8      = cfg.entity_day8      || (disc && disc.entity_day8);
    const entityAccuracy  = cfg.entity_accuracy  || (disc && disc.entity_accuracy);
    const entityTraining  = cfg.entity_training  || (disc && disc.entity_training);
    const entityCondition = cfg.entity_condition || (disc && disc.entity_condition);

    const DAY_ENTITY_KEYS = [
      entityToday, entityTomorrow,
      entityDay3, entityDay4, entityDay5,
      entityDay6, entityDay7, entityDay8,
    ];

    const forecasts = DAY_ENTITY_KEYS.map((entityId, i) => {
      const state = this._s(entityId);
      if (!state) return null;
      const attrs = state.attributes || {};
      return {
        label: DAY_LABELS[i],
        kwh: parseFloat(state.state) || 0,
        date: attrs.date,
        condition: attrs.condition || "mixed",
        weather_code: attrs.weather_code,
        temp_max: attrs.temp_max_c,
        temp_min: attrs.temp_min_c,
        sunrise: attrs.sunrise,
        sunset: attrs.sunset,
      };
    }).filter(Boolean);

    const accuracyState  = this._s(entityAccuracy);
    const countState     = this._s(entityTraining);
    const conditionState = this._s(entityCondition);

    const accuracy = accuracyState ? parseFloat(accuracyState.state) || 0 : 0;
    const trainingCount = countState ? parseInt(countState.state) || 0 : 0;
    const countAttrs = countState ? countState.attributes || {} : {};
    const bucketSunny = countAttrs.sunny ?? "—";
    const bucketMixed = countAttrs.mixed ?? "—";
    const bucketOvercast = countAttrs.overcast ?? "—";
    const maxPerBucket = countAttrs.max_per_bucket ?? 10;
    // Fall back to today's forecast condition attribute if sensor not found
    const todayCondition = conditionState
      ? conditionState.state
      : (forecasts[0]?.condition || "mixed");
    const today = forecasts[0] || {};

    const styles = `
      :host { display: block; }
      .card {
        background: var(--card-background-color, #1c1c1e);
        border-radius: 16px;
        padding: 20px;
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
        color: var(--primary-text-color, #fff);
        overflow: hidden;
      }
      .card-title {
        font-size: 13px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        opacity: 0.5;
        margin-bottom: 16px;
      }
      .today-section {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 20px;
        padding: 16px;
        background: rgba(255,255,255,0.05);
        border-radius: 12px;
      }
      .today-kwh {
        font-size: 42px;
        font-weight: 700;
        line-height: 1;
      }
      .today-kwh span {
        font-size: 16px;
        font-weight: 400;
        opacity: 0.6;
        margin-left: 4px;
      }
      .today-meta {
        text-align: right;
      }
      .condition-badge {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 6px;
      }
      .today-details {
        font-size: 12px;
        opacity: 0.6;
        line-height: 1.6;
      }
      .weather-icon { font-size: 36px; }

      /* 7-day strip */
      .forecast-strip {
        display: flex;
        gap: 8px;
        overflow-x: auto;
        padding-bottom: 4px;
        margin-bottom: 20px;
        scrollbar-width: none;
      }
      .forecast-strip::-webkit-scrollbar { display: none; }
      .forecast-day {
        flex: 0 0 72px;
        background: rgba(255,255,255,0.05);
        border-radius: 10px;
        padding: 10px 6px;
        text-align: center;
        font-size: 11px;
      }
      .forecast-day.today-day {
        background: rgba(99,102,241,0.2);
        border: 1px solid rgba(99,102,241,0.5);
      }
      .forecast-day-label {
        opacity: 0.5;
        margin-bottom: 4px;
        font-size: 10px;
      }
      .forecast-day-icon { font-size: 18px; margin-bottom: 4px; }
      .forecast-day-kwh {
        font-weight: 700;
        font-size: 13px;
      }
      .forecast-day-temp {
        font-size: 10px;
        opacity: 0.5;
        margin-top: 2px;
      }

      /* Training progress – compact single row */
      .training-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: rgba(255,255,255,0.04);
        border-radius: 10px;
        gap: 8px;
      }
      .training-label {
        font-size: 11px;
        opacity: 0.5;
        white-space: nowrap;
      }
      .training-buckets-inline {
        display: flex;
        gap: 10px;
        align-items: center;
      }
      .training-bucket-inline {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 12px;
        font-weight: 700;
      }
      .training-bucket-inline span:first-child { font-size: 13px; }
      .training-accuracy {
        font-size: 12px;
        font-weight: 700;
        background: rgba(99,102,241,0.15);
        color: #818cf8;
        border-radius: 20px;
        padding: 2px 8px;
        white-space: nowrap;
      }
    `;

    const conditionColor = CONDITION_COLORS[todayCondition] || "#6366f1";
    const conditionLabel = CONDITION_LABELS[todayCondition] || todayCondition;

    const forecastDaysHtml = forecasts.slice(1, 5).map((f) => `
      <div class="forecast-day">
        <div class="forecast-day-label">${shortWeekday(f.date)}</div>
        <div class="forecast-day-icon">${getWeatherIcon(f.weather_code)}</div>
        <div class="forecast-day-kwh">${f.kwh.toFixed(1)}</div>
        ${f.temp_min != null || f.temp_max != null ? `<div class="forecast-day-temp">${f.temp_min ?? "—"}° / ${f.temp_max ?? "—"}°</div>` : ""}
      </div>
    `).join("");

    this.shadowRoot.innerHTML = `
      <style>${styles}</style>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
          <div class="card-title" style="margin-bottom:0;">${this._config.title}</div>
          <div style="font-size:10px;opacity:0.3;">v${CARD_VERSION}</div>
        </div>

        <!-- Today -->
        <div class="today-section">
          <div>
            <div class="today-kwh">${today.kwh?.toFixed(1) ?? "—"}<span>kWh</span></div>
            <div style="font-size:11px;opacity:0.5;margin-top:4px;">${formatDate(today.date)}</div>
          </div>
          <div class="today-meta">
            <div>
              <span class="condition-badge" style="background:${conditionColor}22;color:${conditionColor};">
                ${conditionLabel}
              </span>
            </div>
            <div class="weather-icon">${getWeatherIcon(today.weather_code)}</div>
            <div class="today-details">
              ${today.temp_max != null || today.temp_min != null ? `🌡 ${today.temp_min ?? "—"}° / ${today.temp_max ?? "—"}°C` : ""}
            </div>
          </div>
        </div>

        <!-- 7-day strip -->
        <div class="forecast-strip">
          ${forecastDaysHtml}
        </div>

        <!-- Training progress – compact row -->
        <div class="training-row">
          <span class="training-label">🧠 Training</span>
          <div class="training-buckets-inline">
            <div class="training-bucket-inline"><span>☀️</span><span style="color:#f59e0b;">${bucketSunny}/${maxPerBucket}</span></div>
            <div class="training-bucket-inline"><span>⛅</span><span style="color:#6366f1;">${bucketMixed}/${maxPerBucket}</span></div>
            <div class="training-bucket-inline"><span>☁️</span><span style="color:#64748b;">${bucketOvercast}/${maxPerBucket}</span></div>
          </div>
          <span class="training-accuracy">${accuracy.toFixed(0)}%</span>
        </div>
      </div>
    `;
  }

  getCardSize() {
    return 5;
  }
}

customElements.define("solarforecast-card", SolarForecastCard);

// ---------------------------------------------------------------------------
// Card Editor (GUI configuration) – uses native ha-form with entity pickers
// ---------------------------------------------------------------------------

const EDITOR_SCHEMA = [
  { name: "title",            label: "Titel",                      selector: { text: {} } },
  { name: "entity_today",     label: "Sensor: Heute",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_tomorrow",  label: "Sensor: Morgen",             selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_day3",      label: "Sensor: Tag 3",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_day4",      label: "Sensor: Tag 4",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_day5",      label: "Sensor: Tag 5",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_day6",      label: "Sensor: Tag 6",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_day7",      label: "Sensor: Tag 7",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_day8",      label: "Sensor: Tag 8",              selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_accuracy",  label: "Sensor: Modell-Genauigkeit", selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_training",  label: "Sensor: Trainings-Einträge", selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
  { name: "entity_condition", label: "Sensor: Wetterbedingung",    selector: { entity: { filter: { integration: "solarforecast", domain: "sensor" } } } },
];

class SolarForecastCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._initialized = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
    // Auto-fill entities on first hass set if config is empty
    if (hass && !this._autoFilled && !this._config.entity_today) {
      this._autoFilled = true;
      const disc = discoverSolarForecastEntities(hass);
      if (disc) {
        this._config = { ...this._config, ...disc };
        if (this._form) this._form.data = this._config;
        this.dispatchEvent(new CustomEvent("config-changed", {
          detail: { config: this._config },
          bubbles: true,
          composed: true,
        }));
      }
    }
  }

  setConfig(config) {
    this._config = config || {};
    if (this._form) {
      this._form.data = this._config;
    }
    if (!this._initialized) this._initialize();
  }

  connectedCallback() {
    if (!this._initialized) this._initialize();
  }

  _initialize() {
    this._initialized = true;
    this.shadowRoot.innerHTML = `<ha-form></ha-form>`;
    this._form = this.shadowRoot.querySelector("ha-form");
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = EDITOR_SCHEMA;
    this._form.computeLabel = (s) => s.label;
    this._form.addEventListener("value-changed", (e) => {
      this.dispatchEvent(new CustomEvent("config-changed", {
        detail: { config: e.detail.value },
        bubbles: true,
        composed: true,
      }));
    });
  }
}

customElements.define("solarforecast-card-editor", SolarForecastCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "solarforecast-card",
  name: "Solar Forecast Card",
  description: "Solar yield forecast with ML-based training progress.",
  preview: false,
});
