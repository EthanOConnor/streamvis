"""
Streamvis: Snoqualmie River gauge monitoring with adaptive polling.

This package provides real-time USGS river gauge monitoring with:
- Adaptive polling that learns update cadences
- TUI interface with sparklines and forecasts
- Dual-stack USGS API support (WaterServices + OGC API-Features)
- Browser support via Pyodide

Public API:
    main(argv=None) - CLI entrypoint
    web_tui_main(argv=None) - Async browser entrypoint
    fetch_gauge_data(state=None) - Fetch latest readings
    schedule_next_poll(state, now, min_retry) - Next poll timing
"""

from __future__ import annotations

# Version
__version__ = "0.3.0"

# Re-export public API from tui module
from streamvis.tui import (
    # Core functions
    main,
    web_tui_main,
    fetch_gauge_data,
    fetch_gauge_history,
    schedule_next_poll,
    predict_gauge_next,
    update_state_with_readings,
    backfill_state_with_history,
    maybe_backfill_state,
    # State management  
    load_state,
    save_state,
    state_lock,
    StateLockError,
    # Utilities
    tukey_biweight_location_scale,
)

# Configuration from config module
from streamvis.config import (
    SITE_MAP,
    STATION_LOCATIONS,
    PRIMARY_GAUGES,
    ordered_gauges,
    CONFIG,
)

# Constants
from streamvis.constants import (
    CADENCE_BASE_SEC,
    CADENCE_FIT_THRESHOLD,
    MIN_RETRY_SEC,
    MAX_RETRY_SEC,
    EWMA_ALPHA,
    HISTORY_LIMIT,
    UI_TICK_SEC,
    FINE_STEP_MIN_SEC,
)

# Utilities from gauges module
from streamvis.gauges import (
    classify_status,
    nearest_gauges,
)

# Re-export private functions for test compatibility
from streamvis.tui import (
    _parse_usgs_site_rdb,
    _dynamic_gauge_id,
    _iso8601_duration,
    _compute_modified_since,
    _estimate_cadence_multiple,
    _estimate_phase_offset_sec,
    _snap_delta_to_cadence,
    _parse_timestamp,
    _fmt_clock,
    _fmt_rel,
    _ewma,
    _median,
    _mad,
    _haversine_miles,
    _cleanup_state,
    _slim_state_for_browser,
    _coerce_float,
)

# Type exports
from streamvis.types import (
    AppState,
    GaugeState,
    MetaState,
    HistoryPoint,
    GaugeReading,
    BackendStats,
)

__all__ = [
    # Version
    "__version__",
    # Core functions
    "main",
    "web_tui_main", 
    "fetch_gauge_data",
    "fetch_gauge_history",
    "schedule_next_poll",
    "predict_gauge_next",
    "update_state_with_readings",
    # State management
    "load_state",
    "save_state",
    "state_lock",
    "StateLockError",
    # Configuration
    "SITE_MAP",
    "STATION_LOCATIONS", 
    "PRIMARY_GAUGES",
    "ordered_gauges",
    "CONFIG",
    # Constants
    "CADENCE_BASE_SEC",
    "CADENCE_FIT_THRESHOLD",
    "MIN_RETRY_SEC",
    "MAX_RETRY_SEC",
    "EWMA_ALPHA",
    "HISTORY_LIMIT",
    "UI_TICK_SEC",
    "FINE_STEP_MIN_SEC",
    # Utilities
    "classify_status",
    "tukey_biweight_location_scale",
    "nearest_gauges",
    # Types
    "AppState",
    "GaugeState",
    "MetaState",
    "HistoryPoint",
    "GaugeReading",
    "BackendStats",
]
