"""
Streamvis configuration constants.

All tunable parameters for the adaptive scheduler, polling behavior,
latency estimation, and nearby discovery are defined here.
"""

from __future__ import annotations

from pathlib import Path

# --- State persistence ---
STATE_FILE_DEFAULT = Path.home() / ".streamvis_state.json"
STATE_SCHEMA_VERSION = 1  # Increment on backward-incompatible changes

# --- Cadence learning ---
# Most Snoqualmie gauges update on 15-minute multiples (15/30/60 min).
CADENCE_BASE_SEC = 15 * 60           # Base grid for cadence snapping
CADENCE_SNAP_TOL_SEC = 3 * 60        # Acceptable jitter when snapping
CADENCE_FIT_THRESHOLD = 0.60         # Fraction of deltas to fit a cadence
CADENCE_CLEAR_THRESHOLD = 0.45       # Below this, clear cadence multiple

# --- Retry and scheduling ---
DEFAULT_INTERVAL_SEC = CADENCE_BASE_SEC  # Default cadence prior
MIN_RETRY_SEC = 60                   # Short retry on early/error
MAX_RETRY_SEC = 5 * 60               # Cap retry wait
HEADSTART_SEC = 30                   # Poll slightly before expected update
EWMA_ALPHA = 0.30                    # Cadence learning rate
HISTORY_LIMIT = 120                  # Rolling observation window size
MIN_UPDATE_GAP_SEC = 60              # Ignore sub-60-second deltas
MAX_LEARNABLE_INTERVAL_SEC = 6 * 3600  # Don't learn cadences > 6 hours

# --- UI ---
UI_TICK_SEC = 0.15                   # TUI refresh interval

# --- Forecast ---
FORECAST_REFRESH_MIN = 60            # Minutes between forecast fetches

# --- Backfill ---
DEFAULT_BACKFILL_HOURS = 6           # History to fetch on startup
PERIODIC_BACKFILL_INTERVAL_HOURS = 6 # How often to re-check
PERIODIC_BACKFILL_LOOKBACK_HOURS = 6 # How much to re-fetch

# --- Nearby discovery ---
NEARBY_DISCOVERY_RADIUS_MILES = 30.0
NEARBY_DISCOVERY_MAX_RADIUS_MILES = 180.0
NEARBY_DISCOVERY_EXPAND_FACTOR = 2.0
NEARBY_DISCOVERY_MIN_INTERVAL_HOURS = 24.0
DYNAMIC_GAUGE_PREFIX = "U"           # Prefix for dynamic gauges

# --- Latency estimation (Tukey biweight) ---
LATENCY_PRIOR_LOC_SEC = 600.0        # Default latency location
LATENCY_PRIOR_SCALE_SEC = 100.0      # Default latency scale
BIWEIGHT_LOC_C = 6.0                 # Tuning constant for location
BIWEIGHT_SCALE_C = 9.0               # Tuning constant for scale
BIWEIGHT_MAX_ITERS = 5               # Iteration limit

# --- Fine/coarse polling control ---
FINE_LATENCY_MAD_MAX_SEC = 60        # Only fine-poll if MAD <= 1 min
FINE_WINDOW_MIN_SEC = 30             # Minimum fine window half-width
FINE_STEP_MIN_SEC = 15               # Minimum fine-mode step
FINE_STEP_MAX_SEC = 30               # Maximum fine-mode step
COARSE_STEP_FRACTION = 0.5           # Coarse step as fraction of interval

# --- API backends ---
# Legacy WaterServices (retiring EOY 2025)
DEFAULT_USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
DEFAULT_USGS_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"

# New OGC API–Features (recommended)
OGC_API_BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0"
OGC_LATEST_CONTINUOUS = f"{OGC_API_BASE_URL}/collections/latest-continuous/items"
OGC_CONTINUOUS = f"{OGC_API_BASE_URL}/collections/continuous/items"

# Backend selection thresholds
BACKEND_LATENCY_EWMA_ALPHA = 0.2     # Learning rate for API latency
BACKEND_VARIANCE_EWMA_ALPHA = 0.1    # Learning rate for latency variance
BACKEND_SWITCH_HYSTERESIS = 0.10    # 10% latency difference to switch
BACKEND_CONFIDENCE_SAMPLES = 20      # Samples before statistical comparison
BACKEND_PROBE_INTERVAL_HOURS = 24.0  # How often to probe non-preferred backend

# --- NW RFC cross-check ---
NWRFC_TEXT_BASE = "https://www.nwrfc.noaa.gov/station/flowplot/textPlot.cgi"
NWRFC_REFRESH_MIN = 15               # Minutes between cross-checks

# --- Default stations ---
DEFAULT_SITE_MAP: dict[str, str] = {
    "TANW1": "12141300",  # Middle Fork Snoqualmie R near Tanner
    "GARW1": "12143400",  # SF Snoqualmie R ab Alice Cr nr Garcia
    "EDGW1": "12143600",  # SF Snoqualmie R at Edgewick
    "SQUW1": "12144500",  # Snoqualmie R near Snoqualmie
    "CRNW1": "12149000",  # Snoqualmie R near Carnation
}

DEFAULT_STATION_LOCATIONS: dict[str, tuple[float, float]] = {
    "TANW1": (47.485912, -121.647864),   # USGS 12141300
    "GARW1": (47.4151086, -121.5873213), # USGS 12143400
    "EDGW1": (47.4527778, -121.7166667), # USGS 12143600
    "SQUW1": (47.5451019, -121.8423360), # USGS 12144500
    "CRNW1": (47.6659340, -121.9253969), # USGS 12149000
    "CONW1": (48.5382169, -121.7489830), # USGS 12194000
}

PRIMARY_GAUGES: list[str] = ["TANW1", "GARW1", "EDGW1", "SQUW1", "CRNW1"]

# Flood thresholds for status coloring
FLOOD_THRESHOLDS: dict[str, dict[str, float | None]] = {
    "CRNW1": {"action": 50.7, "minor": 54.0, "moderate": 56.0, "major": 58.0},
    "SQUW1": {"action": 11.94, "minor": 13.54, "moderate": 16.21, "major": 17.42},
    "TANW1": {"action": None, "minor": None, "moderate": None, "major": None},
    "GARW1": {"action": None, "minor": None, "moderate": None, "major": None},
    "EDGW1": {"action": None, "minor": None, "moderate": None, "major": None},
}

NWRFC_ID_MAP: dict[str, str] = {
    "GARW1": "GARW1",
    "CONW1": "CONW1",
}
