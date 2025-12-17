"""
Streamvis type definitions using TypedDict for type-safe state management.

These types document the shape of persisted state and runtime data structures,
enabling IDE autocomplete and mypy static analysis.
"""

from __future__ import annotations

from typing import TypedDict


class HistoryPoint(TypedDict, total=False):
    """A single observation point in gauge history."""
    ts: str  # ISO8601 UTC timestamp
    stage: float | None  # Gage height in feet
    flow: float | None   # Discharge in cfs


class ForecastPoint(TypedDict, total=False):
    """A single point in a forecast time series."""
    ts: str  # ISO8601 UTC timestamp
    stage: float | None
    flow: float | None


class ForecastSummary(TypedDict, total=False):
    """Peak summaries for a forecast horizon."""
    stage_max: float | None
    flow_max: float | None
    stage_time: str | None  # ISO8601
    flow_time: str | None   # ISO8601


class ForecastState(TypedDict, total=False):
    """Forecast data for a single gauge."""
    points: list[ForecastPoint]
    summary_3h: ForecastSummary
    summary_24h: ForecastSummary
    summary_full: ForecastSummary
    # Bias estimation vs observations
    delta_stage: float | None
    delta_flow: float | None
    ratio_stage: float | None
    ratio_flow: float | None
    phase_shift_hours: float | None


class GaugeState(TypedDict, total=False):
    """
    Per-gauge persistent state.
    
    Tracks observation history, learned cadence/phase, and latency statistics.
    """
    # Latest observation
    last_timestamp: str  # ISO8601 UTC
    last_stage: float | None
    last_flow: float | None
    
    # Observation history (rolling window, capped by HISTORY_LIMIT)
    history: list[HistoryPoint]
    
    # Cadence learning
    mean_interval_sec: float  # EWMA of observed update intervals
    cadence_mult: int         # Best-fit multiple of CADENCE_BASE_SEC (1=15min, 2=30min, etc.)
    cadence_fit: float        # Confidence in cadence_mult (0.0-1.0)
    phase_offset_sec: float   # Typical offset within cadence period
    
    # Latency learning (observation → API visibility)
    latencies_sec: list[float]  # Recent latency samples
    latency_loc_sec: float      # Tukey biweight location (robust mean)
    latency_scale_sec: float    # Tukey biweight scale (robust std)
    latency_median_sec: float   # Legacy: median latency
    latency_mad_sec: float      # Legacy: median absolute deviation
    
    # Polling statistics
    last_poll_ts: str           # ISO8601 of last poll attempt
    no_update_polls: int        # Consecutive polls with no new data
    last_polls_per_update: int  # Polls needed for last successful update
    polls_per_update_ewma: float  # EWMA of polls per update


class BackendStats(TypedDict, total=False):
    """
    Per-API-backend latency and reliability statistics.
    
    Used for intelligent backend selection in blended mode.
    """
    latency_ewma_ms: float      # EWMA of request latency
    latency_var_ewma_ms2: float # EWMA of variance (for statistical comparison)
    success_count: int
    fail_count: int
    last_success_ts: str        # ISO8601
    last_fail_ts: str           # ISO8601
    last_fail_reason: str


class NWRFCState(TypedDict, total=False):
    """NW RFC cross-check data for a gauge."""
    observed: list[HistoryPoint]
    forecast: list[ForecastPoint]
    diff_vs_usgs: dict[str, float]  # timestamp -> delta
    last_refresh_ts: str


class DynamicSiteInfo(TypedDict, total=False):
    """Metadata for dynamically discovered nearby stations."""
    site_no: str
    station_nm: str
    lat: float
    lon: float
    distance_miles: float


class MetaState(TypedDict, total=False):
    """
    Global application metadata persisted across sessions.
    """
    # Schema versioning
    state_version: int
    
    # Backfill tracking
    backfill_hours: int
    last_periodic_backfill_ts: str
    
    # Fetch timestamps
    last_fetch_ts: str
    last_success_ts: str
    last_failure_ts: str
    last_failure_reason: str
    
    # Forecast tracking
    last_forecast_refresh_ts: str
    
    # NW RFC tracking
    last_nwrfc_refresh_ts: str
    
    # User location (for Nearby feature)
    nearby_enabled: bool
    user_lat: float
    user_lon: float
    nearby_search_ts: str
    nearby_gauges: list[str]  # Ordered list of nearby gauge_ids
    dynamic_sites: dict[str, DynamicSiteInfo]
    
    # API backend selection
    api_backend: str  # "blended" | "waterservices" | "ogc"
    waterservices: BackendStats
    ogc: BackendStats
    preferred_backend: str | None  # Set when statistical confidence reached
    last_backend_probe_ts: str     # For periodic re-evaluation
    
    # Community priors
    community_base: str
    community_publish: bool
    last_community_fetch_ts: str
    
    # Alert/UI preferences
    alert_enabled: bool
    chart_metric: str  # "stage" | "flow"


class AppState(TypedDict, total=False):
    """
    Root application state structure.
    
    This is what gets persisted to ~/.streamvis_state.json
    """
    gauges: dict[str, GaugeState]
    meta: MetaState
    forecast: dict[str, ForecastState]
    nwrfc: dict[str, NWRFCState]


class GaugeReading(TypedDict, total=False):
    """
    A single gauge reading returned from fetch operations.
    """
    stage: float | None
    flow: float | None
    status: str  # "NORMAL" | "ACTION" | "MINOR FLOOD" | "MOD FLOOD" | "MAJOR FLOOD"
    observed_at: str  # ISO8601 UTC


class CommunityPrior(TypedDict, total=False):
    """
    Shared cadence/latency priors from community aggregator.
    """
    cadence_mult: int | None
    cadence_fit: float | None
    phase_offset_sec: float | None
    latency_loc_sec: float
    latency_scale_sec: float
    samples: int
    updated_at: str
    # Backend stats for community sharing
    waterservices_latency_ms: float | None
    ogc_latency_ms: float | None


class CommunitySummary(TypedDict, total=False):
    """
    Response from community aggregator GET /summary.json
    """
    version: int
    generated_at: str
    stations: dict[str, CommunityPrior]  # Keyed by USGS site_no


class USGSSite(TypedDict):
    """
    USGS site metadata from Site Service.
    """
    site_no: str
    station_nm: str
    lat: float
    lon: float
