"""
USGS API adapter with blended backend and latency learning.

This module provides unified access to USGS data through both:
- WaterServices (legacy, retiring EOY 2025)
- OGC API–Features (new, recommended)

Backend Selection Strategy:
1. Default: BLENDED - fetch from both, merge results
2. Once statistical confidence is reached that one is better:
   - Switch to single backend
   - Periodically probe the other to confirm
3. "Better" is defined as:
   - Lower latency with same variance
   - Same latency but lower variance
   
User can override with --usgs-backend flag.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from streamvis.constants import (
    BACKEND_LATENCY_EWMA_ALPHA,
    BACKEND_VARIANCE_EWMA_ALPHA,
    BACKEND_SWITCH_HYSTERESIS,
    BACKEND_CONFIDENCE_SAMPLES,
    BACKEND_PROBE_INTERVAL_HOURS,
)
from streamvis.types import BackendStats, MetaState
from streamvis.utils import ewma, ewma_variance, parse_timestamp
from streamvis.usgs import waterservices, ogcapi


class USGSBackend(Enum):
    """Available USGS API backends."""
    BLENDED = "blended"
    WATERSERVICES = "waterservices"
    OGC = "ogc"


def _init_backend_stats() -> BackendStats:
    """Initialize empty backend statistics."""
    return BackendStats(
        latency_ewma_ms=0.0,
        latency_var_ewma_ms2=0.0,
        success_count=0,
        fail_count=0,
        last_success_ts="",
        last_fail_ts="",
        last_fail_reason="",
    )


def _update_backend_stats(
    stats: BackendStats,
    latency_ms: float,
    success: bool,
    fail_reason: str = "",
) -> BackendStats:
    """Update backend statistics with new request result."""
    now_ts = datetime.now(timezone.utc).isoformat()
    
    new_stats = BackendStats(**stats)  # Copy
    
    if success:
        new_stats["success_count"] = stats.get("success_count", 0) + 1
        new_stats["last_success_ts"] = now_ts
        
        # Update latency EWMA
        current_latency = stats.get("latency_ewma_ms", 0.0)
        if current_latency <= 0:
            new_stats["latency_ewma_ms"] = latency_ms
            new_stats["latency_var_ewma_ms2"] = 0.0
        else:
            new_stats["latency_ewma_ms"] = ewma(
                current_latency, latency_ms, BACKEND_LATENCY_EWMA_ALPHA
            )
            new_stats["latency_var_ewma_ms2"] = ewma_variance(
                stats.get("latency_var_ewma_ms2", 0.0),
                current_latency,
                latency_ms,
                BACKEND_VARIANCE_EWMA_ALPHA,
            )
    else:
        new_stats["fail_count"] = stats.get("fail_count", 0) + 1
        new_stats["last_fail_ts"] = now_ts
        new_stats["last_fail_reason"] = fail_reason
    
    return new_stats


def _select_preferred_backend(meta: MetaState) -> USGSBackend | None:
    """
    Determine if we should switch to a single backend based on statistics.
    
    Returns the preferred backend if confidence is high, None otherwise.
    """
    ws_stats = meta.get("waterservices", _init_backend_stats())
    ogc_stats = meta.get("ogc", _init_backend_stats())
    
    ws_samples = ws_stats.get("success_count", 0)
    ogc_samples = ogc_stats.get("success_count", 0)
    
    # Need enough samples from both
    if ws_samples < BACKEND_CONFIDENCE_SAMPLES or ogc_samples < BACKEND_CONFIDENCE_SAMPLES:
        return None
    
    ws_latency = ws_stats.get("latency_ewma_ms", float("inf"))
    ogc_latency = ogc_stats.get("latency_ewma_ms", float("inf"))
    ws_var = ws_stats.get("latency_var_ewma_ms2", 0.0)
    ogc_var = ogc_stats.get("latency_var_ewma_ms2", 0.0)
    
    # Check for failures - penalize backends with high failure rates
    ws_fail_rate = ws_stats.get("fail_count", 0) / max(1, ws_samples)
    ogc_fail_rate = ogc_stats.get("fail_count", 0) / max(1, ogc_samples)
    
    if ws_fail_rate > 0.1 and ogc_fail_rate < 0.05:
        return USGSBackend.OGC
    if ogc_fail_rate > 0.1 and ws_fail_rate < 0.05:
        return USGSBackend.WATERSERVICES
    
    # Compare latencies with hysteresis
    if ogc_latency < ws_latency * (1 - BACKEND_SWITCH_HYSTERESIS):
        return USGSBackend.OGC
    if ws_latency < ogc_latency * (1 - BACKEND_SWITCH_HYSTERESIS):
        return USGSBackend.WATERSERVICES
    
    # Same latency - prefer lower variance
    if abs(ogc_latency - ws_latency) < ws_latency * BACKEND_SWITCH_HYSTERESIS:
        if ogc_var < ws_var * 0.8:
            return USGSBackend.OGC
        if ws_var < ogc_var * 0.8:
            return USGSBackend.WATERSERVICES
    
    # Can't decide - stay blended
    return None


def _should_probe_alternate(meta: MetaState, preferred: USGSBackend) -> bool:
    """Check if we should probe the non-preferred backend."""
    now = datetime.now(timezone.utc)
    last_probe = parse_timestamp(meta.get("last_backend_probe_ts", ""))
    
    if last_probe is None:
        return True
    
    hours_since = (now - last_probe).total_seconds() / 3600.0
    return hours_since >= BACKEND_PROBE_INTERVAL_HOURS


def _merge_readings(
    ws_readings: dict[str, dict[str, Any]],
    ogc_readings: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Merge readings from both backends, preferring the one with newer data.
    """
    result: dict[str, dict[str, Any]] = {}
    
    all_gauges = set(ws_readings.keys()) | set(ogc_readings.keys())
    
    for gauge_id in all_gauges:
        ws = ws_readings.get(gauge_id, {})
        ogc = ogc_readings.get(gauge_id, {})
        
        ws_ts = ws.get("observed_at")
        ogc_ts = ogc.get("observed_at")
        
        # Prefer the reading with more recent observation
        if ws_ts is not None and ogc_ts is not None:
            if ogc_ts > ws_ts:
                result[gauge_id] = ogc
            else:
                result[gauge_id] = ws
        elif ogc_ts is not None:
            result[gauge_id] = ogc
        elif ws_ts is not None:
            result[gauge_id] = ws
        else:
            # No observation time - merge whatever we have
            result[gauge_id] = {
                "stage": ogc.get("stage") or ws.get("stage"),
                "flow": ogc.get("flow") or ws.get("flow"),
                "observed_at": None,
            }
    
    return result


def fetch_gauge_data(
    site_map: dict[str, str],
    meta: MetaState,
    backend: USGSBackend = USGSBackend.BLENDED,
    modified_since_sec: float | None = None,
) -> tuple[dict[str, dict[str, Any]], MetaState]:
    """
    Fetch latest gauge readings using the specified backend strategy.
    
    Args:
        site_map: Mapping of gauge_id -> USGS site_no
        meta: Current metadata state (for backend stats)
        backend: Backend selection (default: BLENDED)
        modified_since_sec: For WaterServices modifiedSince filter
        
    Returns:
        Tuple of (readings dict, updated meta state)
    """
    if not site_map:
        return {}, meta
    
    new_meta = MetaState(**meta)  # Copy
    
    # Initialize backend stats if missing
    if "waterservices" not in new_meta:
        new_meta["waterservices"] = _init_backend_stats()
    if "ogc" not in new_meta:
        new_meta["ogc"] = _init_backend_stats()
    
    # Check if we've converged on a preferred backend
    preferred = _select_preferred_backend(new_meta)
    if preferred is not None and backend == USGSBackend.BLENDED:
        # Use preferred, but occasionally probe the other
        if _should_probe_alternate(new_meta, preferred):
            new_meta["last_backend_probe_ts"] = datetime.now(timezone.utc).isoformat()
            backend = USGSBackend.BLENDED  # Probe both
        else:
            backend = preferred
    
    ws_readings: dict[str, dict[str, Any]] = {}
    ogc_readings: dict[str, dict[str, Any]] = {}
    
    # Fetch from WaterServices
    if backend in (USGSBackend.BLENDED, USGSBackend.WATERSERVICES):
        try:
            ws_readings, ws_latency = waterservices.fetch_latest(
                site_map, modified_since_sec
            )
            success = bool(ws_readings)
            new_meta["waterservices"] = _update_backend_stats(
                new_meta["waterservices"], ws_latency, success
            )
        except Exception as e:
            new_meta["waterservices"] = _update_backend_stats(
                new_meta["waterservices"], 0.0, False, str(e)
            )
    
    # Fetch from OGC API
    if backend in (USGSBackend.BLENDED, USGSBackend.OGC):
        try:
            ogc_readings, ogc_latency = ogcapi.fetch_latest(site_map)
            success = bool(ogc_readings)
            new_meta["ogc"] = _update_backend_stats(
                new_meta["ogc"], ogc_latency, success
            )
        except Exception as e:
            new_meta["ogc"] = _update_backend_stats(
                new_meta["ogc"], 0.0, False, str(e)
            )
    
    # Merge or select readings
    if backend == USGSBackend.BLENDED:
        readings = _merge_readings(ws_readings, ogc_readings)
    elif backend == USGSBackend.WATERSERVICES:
        readings = ws_readings
    else:
        readings = ogc_readings
    
    return readings, new_meta


def fetch_gauge_history(
    site_map: dict[str, str],
    period_hours: int = 6,
    backend: USGSBackend = USGSBackend.WATERSERVICES,
) -> dict[str, list[dict[str, Any]]]:
    """
    Fetch historical gauge readings.
    
    Uses WaterServices by default as it has better historical query support.
    """
    if backend == USGSBackend.OGC:
        from datetime import timedelta
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=period_hours)
        history, _ = ogcapi.fetch_history(site_map, start_time, end_time)
        return history
    else:
        history, _ = waterservices.fetch_history(site_map, period_hours)
        return history


def fetch_sites_near(
    lat: float,
    lon: float, 
    radius_miles: float,
) -> list[dict[str, Any]]:
    """
    Fetch USGS sites near a location.
    
    Uses WaterServices as primary source for site discovery.
    """
    sites, _ = waterservices.fetch_sites_near(lat, lon, radius_miles)
    return sites
