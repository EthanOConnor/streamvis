#!/usr/bin/env python3
"""
Streamvis TUI - Terminal user interface for river gauge monitoring.

This module contains the main application logic:
- TUI rendering and input handling
- Forecast integration (NWPS, NWRFC)
- Community data publishing
- CLI entrypoint
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from http_client import get_json, get_text, post_json, post_json_async

# Module aliases to avoid name-shadowing recursion in thin wrappers.
import streamvis.scheduler as _streamvis_scheduler
import streamvis.state as _streamvis_state

# --- Import from extracted modules ---

# Constants
from streamvis.constants import (
    STATE_FILE_DEFAULT,
    STATE_SCHEMA_VERSION,
    CADENCE_BASE_SEC,
    CADENCE_SNAP_TOL_SEC,
    CADENCE_FIT_THRESHOLD,
    CADENCE_CLEAR_THRESHOLD,
    DEFAULT_INTERVAL_SEC,
    MIN_RETRY_SEC,
    MAX_RETRY_SEC,
    HEADSTART_SEC,
    EWMA_ALPHA,
    HISTORY_LIMIT,
    UI_TICK_SEC,
    MIN_UPDATE_GAP_SEC,
    FORECAST_REFRESH_MIN,
    MAX_LEARNABLE_INTERVAL_SEC,
    DEFAULT_BACKFILL_HOURS,
    PERIODIC_BACKFILL_INTERVAL_HOURS,
    PERIODIC_BACKFILL_LOOKBACK_HOURS,
    NEARBY_DISCOVERY_RADIUS_MILES,
    NEARBY_DISCOVERY_MAX_RADIUS_MILES,
    NEARBY_DISCOVERY_EXPAND_FACTOR,
    NEARBY_DISCOVERY_MIN_INTERVAL_HOURS,
    DYNAMIC_GAUGE_PREFIX,
    LATENCY_PRIOR_LOC_SEC,
    LATENCY_PRIOR_SCALE_SEC,
    BIWEIGHT_LOC_C,
    BIWEIGHT_SCALE_C,
    BIWEIGHT_MAX_ITERS,
    FINE_LATENCY_MAD_MAX_SEC,
    FINE_WINDOW_MIN_SEC,
    FINE_STEP_MIN_SEC,
    FINE_STEP_MAX_SEC,
    COARSE_STEP_FRACTION,
    DEFAULT_USGS_IV_URL,
    DEFAULT_USGS_SITE_URL,
    NWRFC_TEXT_BASE,
    NWRFC_REFRESH_MIN,
    FLOOD_THRESHOLDS,
    NWRFC_ID_MAP,
)

# Configuration
from streamvis.config import (
    CONFIG,
    SITE_MAP,
    STATION_LOCATIONS,
    PRIMARY_GAUGES,
    ordered_gauges,
    USGS_IV_URL,
)

# Utilities - import with underscore aliases for backward compatibility
from streamvis.utils import (
    parse_timestamp as _parse_timestamp,
    fmt_clock as _fmt_clock,
    fmt_rel as _fmt_rel,
    parse_nwrfc_timestamp as _parse_nwrfc_timestamp,
    ewma as _ewma,
    iso8601_duration as _iso8601_duration,
    median as _median,
    mad as _mad,
    tukey_biweight_location_scale,
    haversine_miles as _haversine_miles,
    bbox_for_radius as _bbox_for_radius,
    coerce_float as _coerce_float,
    compute_modified_since as _compute_modified_since,
    compute_modified_since_sec as _compute_modified_since_sec,
)

# Gauges
from streamvis.gauges import (
    classify_status,
    nearest_gauges,
    station_display_name,
    parse_usgs_site_rdb as _parse_usgs_site_rdb,
    dynamic_gauge_id as _dynamic_gauge_id,
)

# Scheduler
from streamvis.scheduler import (
    snap_delta_to_cadence as _snap_delta_to_cadence,
    estimate_cadence_multiple as _estimate_cadence_multiple,
    maybe_update_cadence_from_deltas as _maybe_update_cadence_from_deltas,
    estimate_phase_offset_sec as _estimate_phase_offset_sec,
    predict_gauge_next,
    schedule_next_poll,
    control_summary,
)

# State management
from streamvis.state import (
    StateLockError,
    state_lock,
    load_state,
    save_state,
    cleanup_state as _cleanup_state,
    slim_state_for_browser as _slim_state_for_browser,
    evict_dynamic_sites,
    backfill_state_with_history,
    maybe_backfill_state,
    maybe_periodic_backfill_check,
    update_state_with_readings,
)

# USGS adapter (dual backend)
from streamvis.usgs.adapter import (
    USGSBackend,
    fetch_gauge_data as _usgs_fetch_gauge_data,
    fetch_gauge_history as _usgs_fetch_gauge_history,
)

try:
    import fcntl  # type: ignore[import]
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# --- TUI-specific code starts here ---

# Note: _parse_timestamp, _fmt_clock, _fmt_rel, _parse_nwrfc_timestamp,
# classify_status, nearest_gauges, _haversine_miles, _parse_usgs_site_rdb,
# _dynamic_gauge_id, etc. are now imported from extracted modules above.


def fetch_gauge_data(state: Dict[str, Any] | None = None) -> Dict[str, Dict[str, Any]]:
    """
    Fetch latest stage (ft) and flow (cfs) for TANW1, GARW1, SQUW1, CRNW1
    from USGS Instantaneous Values service.

    Returns:
        {
          "TANW1": {"stage": float, "flow": float, "status": str},
          ...
        }
    On error, returns {} (caller will just skip drawing rows).
    """
    # Prepare result skeleton
    result = {
        g: {"stage": None, "flow": None, "status": "NORMAL", "observed_at": None}
        for g in SITE_MAP.keys()
    }

    meta: dict[str, Any] = {}
    backend = USGSBackend.BLENDED
    modified_since_sec: float | None = None
    if state is not None:
        meta_raw = state.setdefault("meta", {})
        if isinstance(meta_raw, dict):
            meta = meta_raw
        else:
            meta = {}
            state["meta"] = meta

        backend_raw = meta.get("api_backend") or "blended"
        if isinstance(backend_raw, str) and backend_raw:
            try:
                backend = USGSBackend(backend_raw)
            except Exception:
                backend = USGSBackend.BLENDED
        modified_since_sec = _compute_modified_since_sec(state)
        # If we are tracking any gauges that we have never successfully seen yet,
        # disable modifiedSince so the first fetch can populate baseline values.
        #
        # This is especially important for Nearby-discovered gauges where we
        # may not have any cached `last_*` values to backfill from state yet.
        if modified_since_sec is not None:
            gauges_state = state.get("gauges", {})
            if not isinstance(gauges_state, dict):
                modified_since_sec = None
            else:
                for gauge_id in SITE_MAP.keys():
                    g_state = gauges_state.get(gauge_id)
                    if not isinstance(g_state, dict):
                        modified_since_sec = None
                        break
                    last_ts = g_state.get("last_timestamp")
                    if not isinstance(last_ts, str) or not last_ts:
                        modified_since_sec = None
                        break

    try:
        readings, new_meta = _usgs_fetch_gauge_data(
            SITE_MAP,
            meta,
            backend=backend,
            modified_since_sec=modified_since_sec,
        )
    except Exception as exc:
        if state is not None and isinstance(meta, dict):
            meta["last_fetch_error"] = str(exc)
        return {}

    # Persist backend stats/decision metadata.
    if state is not None and isinstance(meta, dict) and isinstance(new_meta, dict):
        meta.update(new_meta)

    if not readings:
        if state is not None and isinstance(meta, dict):
            reasons: list[str] = []
            ws = meta.get("waterservices")
            if isinstance(ws, dict):
                r = ws.get("last_fail_reason")
                if isinstance(r, str) and r:
                    reasons.append(f"waterservices: {r}")
            ogc = meta.get("ogc")
            if isinstance(ogc, dict):
                r = ogc.get("last_fail_reason")
                if isinstance(r, str) and r:
                    reasons.append(f"ogc: {r}")
            meta["last_fetch_error"] = "; ".join(reasons) or "USGS fetch failed"
        return {}

    if state is not None and isinstance(meta, dict):
        meta.pop("last_fetch_error", None)

    for gauge_id, reading in readings.items():
        if gauge_id not in result or not isinstance(reading, dict):
            continue
        stage = reading.get("stage")
        flow = reading.get("flow")
        obs_at = reading.get("observed_at")
        if isinstance(stage, (int, float)):
            result[gauge_id]["stage"] = float(stage)
        if isinstance(flow, (int, float)):
            result[gauge_id]["flow"] = float(flow)
        if isinstance(obs_at, datetime):
            result[gauge_id]["observed_at"] = obs_at

    # Backfill missing series from state so UI does not blank out.
    if state is not None:
        gauges_state = state.get("gauges", {})
        if isinstance(gauges_state, dict):
            for gauge_id, d in result.items():
                g_state = gauges_state.get(gauge_id, {})
                if not isinstance(g_state, dict):
                    continue
                if d.get("stage") is None:
                    last_stage = g_state.get("last_stage")
                    if isinstance(last_stage, (int, float)):
                        d["stage"] = float(last_stage)
                if d.get("flow") is None:
                    last_flow = g_state.get("last_flow")
                    if isinstance(last_flow, (int, float)):
                        d["flow"] = float(last_flow)
                if d.get("observed_at") is None:
                    last_ts = _parse_timestamp(g_state.get("last_timestamp"))
                    if last_ts is not None:
                        d["observed_at"] = last_ts

    # Compute status strings based on stage thresholds
    for g, d in result.items():
        stage = d["stage"]
        d["status"] = classify_status(g, stage)

    return result


# _ewma, _iso8601_duration, _median, _mad, tukey_biweight_location_scale,
# _haversine_miles, nearest_gauges are imported from utils/gauges modules.

def fetch_usgs_sites_near(
    user_lat: float,
    user_lon: float,
    radius_miles: float,
) -> List[Dict[str, Any]]:
    """
    Fetch active USGS stream gauges with IV data near a location.

    Uses the NWIS Site Service (RDB format). Fail-soft on errors.
    """
    west, south, east, north = _bbox_for_radius(user_lat, user_lon, radius_miles)
    params = {
        "format": "rdb",
        "bBox": f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}",
        "siteStatus": "active",
        "hasDataTypeCd": "iv",
        "siteType": "ST",
        "parameterCd": "00060,00065",
    }
    try:
        text = get_text(DEFAULT_USGS_SITE_URL, params=params, timeout=10.0)
    except Exception:
        return []
    return _parse_usgs_site_rdb(text)

def apply_dynamic_sites_from_state(state: Dict[str, Any]) -> None:
    """
    Merge any previously discovered dynamic sites into SITE_MAP/STATION_LOCATIONS.
    """
    meta = state.get("meta", {})
    if not isinstance(meta, dict):
        return
    # Dynamic sites are discovered via Nearby and should only be active when
    # Nearby is enabled.
    if not bool(meta.get("nearby_enabled")):
        return
    dyn = meta.get("dynamic_sites")
    if not isinstance(dyn, dict):
        return
    global SITE_MAP, STATION_LOCATIONS
    for gauge_id, info in dyn.items():
        if not isinstance(info, dict):
            continue
        site_no = info.get("site_no")
        if isinstance(site_no, str) and site_no:
            SITE_MAP.setdefault(gauge_id, site_no)
        try:
            lat = float(info.get("lat"))
            lon = float(info.get("lon"))
        except Exception:
            continue
        STATION_LOCATIONS.setdefault(gauge_id, (lat, lon))


def maybe_discover_nearby_gauges(
    state: Dict[str, Any],
    now: datetime,
    user_lat: float,
    user_lon: float,
    n: int = 3,
) -> List[str]:
    """
    Discover the N nearest USGS IV gauges to the user and add them to SITE_MAP/state if absent.

    Returns the gauge_ids to display in Nearby order. Fail-soft on errors.
    """
    meta = state.setdefault("meta", {})
    if not isinstance(meta, dict):
        return []

    last_search = _parse_timestamp(meta.get("nearby_search_ts")) if isinstance(meta.get("nearby_search_ts"), str) else None
    if last_search is not None:
        elapsed_h = (now - last_search).total_seconds() / 3600.0
        if elapsed_h < NEARBY_DISCOVERY_MIN_INTERVAL_HOURS:
            ids = meta.get("nearby_gauges")
            if isinstance(ids, list):
                return [str(x) for x in ids if isinstance(x, str)]

    radius = NEARBY_DISCOVERY_RADIUS_MILES
    sites: List[Dict[str, Any]] = []
    for _attempt in range(4):
        sites = fetch_usgs_sites_near(user_lat, user_lon, radius)
        if len(sites) >= n:
            break
        radius *= NEARBY_DISCOVERY_EXPAND_FACTOR
        if radius > NEARBY_DISCOVERY_MAX_RADIUS_MILES:
            break

    if not sites:
        return []

    # Map existing USGS site numbers to gauge IDs.
    existing_site_to_gauge = {site_no: gid for gid, site_no in SITE_MAP.items()}
    existing_ids = list(SITE_MAP.keys())

    ranked: List[tuple[float, Dict[str, Any]]] = []
    for s in sites:
        try:
            dist = _haversine_miles(user_lat, user_lon, float(s["lat"]), float(s["lon"]))
        except Exception:
            continue
        ranked.append((dist, s))
    ranked.sort(key=lambda x: x[0])

    dyn = meta.setdefault("dynamic_sites", {})
    if not isinstance(dyn, dict):
        dyn = {}
        meta["dynamic_sites"] = dyn

    chosen_ids: List[str] = []
    for dist, s in ranked:
        if len(chosen_ids) >= n:
            break
        site_no = str(s.get("site_no") or "")
        if not site_no:
            continue
        gauge_id = existing_site_to_gauge.get(site_no)
        if gauge_id is None:
            gauge_id = _dynamic_gauge_id(site_no, existing_ids + chosen_ids)
            SITE_MAP[gauge_id] = site_no
            STATION_LOCATIONS[gauge_id] = (float(s["lat"]), float(s["lon"]))
            dyn[gauge_id] = {
                "site_no": site_no,
                "station_nm": s.get("station_nm") or site_no,
                "lat": float(s["lat"]),
                "lon": float(s["lon"]),
            }
        chosen_ids.append(gauge_id)

    meta["nearby_gauges"] = chosen_ids
    meta["nearby_search_ts"] = now.isoformat()
    return chosen_ids

def seed_user_location_from_args(state: Dict[str, Any], args: argparse.Namespace) -> None:
    lat = getattr(args, "user_lat", None)
    lon = getattr(args, "user_lon", None)
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        meta = state.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["user_lat"] = float(lat)
            meta["user_lon"] = float(lon)


def refresh_user_location_web(state: Dict[str, Any]) -> tuple[float, float] | None:
    """
    If running under Pyodide and JS geolocation has been provided, copy it into
    state.meta and return (lat, lon). Otherwise return None.
    """
    try:
        from js import window  # type: ignore[import]
    except Exception:
        return None
    try:
        loc = getattr(window, "streamvisUserLocation", None)
        lat = getattr(loc, "lat", None) if loc is not None else None
        lon = getattr(loc, "lon", None) if loc is not None else None
        if lat is None or lon is None:
            return None
        lat_f = float(lat)
        lon_f = float(lon)
        meta = state.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["user_lat"] = lat_f
            meta["user_lon"] = lon_f
        return lat_f, lon_f
    except Exception:
        return None


def maybe_request_user_location_web() -> bool:
    """
    Best-effort trigger of browser geolocation prompt when running in Pyodide.
    Returns True if a request was made.
    """
    try:
        from js import window  # type: ignore[import]
    except Exception:
        return False
    try:
        req = getattr(window, "streamvisRequestLocation", None)
        if req:
            req()
            return True
    except Exception:
        return False
    return False


def toggle_nearby(state: Dict[str, Any], args: argparse.Namespace | None = None) -> str:
    """
    Toggle Nearby mode in state.meta. When enabling, attempt to seed location
    from args or request web geolocation.
    Returns a short status message for the UI.
    """
    meta = state.setdefault("meta", {})
    if not isinstance(meta, dict):
        return ""
    enabled = not bool(meta.get("nearby_enabled", False))
    meta["nearby_enabled"] = enabled
    if enabled:
        # If we have any previously cached dynamic sites, activate them now.
        apply_dynamic_sites_from_state(state)
        if args is not None:
            seed_user_location_from_args(state, args)
        loc = refresh_user_location_web(state)
        if loc is None:
            if maybe_request_user_location_web():
                return "Nearby on (requesting location...)"
            return "Nearby on (no location yet)"

        # We have a location; discover closest USGS gauges and add them.
        try:
            ids = maybe_discover_nearby_gauges(
                state,
                datetime.now(timezone.utc),
                float(loc[0]),
                float(loc[1]),
                n=3,
            )
            if ids:
                return "Nearby on (updated stations)"
        except Exception:
            pass
        return "Nearby on"

    # Disabling: evict dynamically added stations so we stop tracking/polling them.
    evicted = evict_dynamic_sites(state)
    if evicted:
        for gid in evicted:
            SITE_MAP.pop(gid, None)
            STATION_LOCATIONS.pop(gid, None)
    meta.pop("nearby_gauges", None)
    meta.pop("nearby_search_ts", None)
    if evicted:
        return f"Nearby off (evicted {len(evicted)})"
    return "Nearby off"


def fetch_gauge_history(hours_back: int) -> Dict[str, List[Dict[str, Any]]]:
    """
    Backfill recent history for all gauges from the USGS IV service.

    Returns a mapping gauge_id -> list of points:
        {"ts": iso8601, "stage": float | None, "flow": float | None}
    """
    if hours_back <= 0:
        return {}
    try:
        # WaterServices remains the preferred historical API for now.
        return _usgs_fetch_gauge_history(SITE_MAP, hours_back, backend=USGSBackend.WATERSERVICES)
    except Exception:
        return {}

def _resolve_forecast_url(template: str, gauge_id: str, site_no: str) -> str:
    """
    Format a forecast URL from a template.

    The template may contain `{gauge_id}` and `{site_no}` placeholders, for example:
        https://example/api/stations/{gauge_id}/forecast
    """
    return template.format(gauge_id=gauge_id, site_no=site_no)


def _forecast_template_for_gauge(gauge_id: str, site_no: str, args: argparse.Namespace) -> str:
    """
    Resolve a forecast URL template for a given gauge, honoring:
    - CLI --forecast-base (highest precedence, shared across gauges)
    - Per-station forecast_endpoint in config.toml
    - Global default_forecast_template in config.toml
    Returns an empty string when no forecast configuration is available.
    """
    base = getattr(args, "forecast_base", "") or ""
    if base:
        return base

    stations_cfg = CONFIG.get("stations")
    if isinstance(stations_cfg, dict):
        entry = stations_cfg.get(gauge_id)
        if isinstance(entry, dict):
            endpoint = entry.get("forecast_endpoint")
            if isinstance(endpoint, str) and endpoint:
                return endpoint

    global_cfg = CONFIG.get("global")
    if isinstance(global_cfg, dict):
        nwps_cfg = global_cfg.get("noaa_nwps")
        if isinstance(nwps_cfg, dict):
            template = nwps_cfg.get("default_forecast_template")
            if isinstance(template, str) and template:
                return template

    return ""

def fetch_forecast_series(
    forecast_base: str,
    gauge_id: str,
    site_no: str,
    horizon_hours: int,
) -> List[Dict[str, Any]]:
    """
    Fetch forecast time series for a single gauge.

    This function deliberately treats the response as shape-agnostic and only
    assumes we can extract a sequence of (timestamp, stage, flow)-like points.

    Operators should point `forecast_base` at an appropriate NOAA / NWPS endpoint
    and adjust the parsing logic here to match the actual payload.
    """
    if not forecast_base:
        return []

    url = _resolve_forecast_url(forecast_base, gauge_id=gauge_id, site_no=site_no)
    params: Dict[str, Any] = {}
    if horizon_hours > 0:
        # Many forecast APIs accept a horizon or end-time parameter; adapt as needed.
        params["horizon_hours"] = horizon_hours

    try:
        data = get_json(url, params=params or None, timeout=10.0)
    except Exception:
        return []

    # Accept either a list of points or an object with a top-level list.
    if isinstance(data, list):
        series = data
    elif isinstance(data, dict):
        # Heuristic: look for a likely list field.
        for key in ("forecast", "values", "data", "series"):
            val = data.get(key)
            if isinstance(val, list):
                series = val
                break
        else:
            return []
    else:
        return []

    points: List[Dict[str, Any]] = []
    for entry in series:
        if not isinstance(entry, dict):
            continue

        # NOTE: The field names below are *assumptions* and may need to be
        # adjusted to match the actual NWPS API. Common patterns include
        # `validTime`, `time`, or `forecast_time` for timestamps, and
        # stage/flow values in feet / cfs.
        ts_raw = entry.get("validTime") or entry.get("time") or entry.get("ts")
        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        if dt is None:
            continue

        stage = entry.get("stage_ft") or entry.get("stage") or entry.get("value")
        flow = entry.get("flow_cfs") or entry.get("flow")

        point = {
            "ts": dt.isoformat(),
            "stage": _coerce_float(stage),
            "flow": _coerce_float(flow),
        }
        points.append(point)

    # Ensure points are sorted by time.
    points.sort(key=lambda p: p["ts"])
    return points


def summarize_forecast_points(
    points: List[Dict[str, Any]],
    now: datetime,
    horizon_hours: int,
) -> Dict[str, Any]:
    """
    Compute 3-hour, 24-hour, and full-horizon maxima for stage and flow.
    """
    if not points:
        return {}

    max_3h = {"stage": None, "flow": None, "ts": None}
    max_24h = {"stage": None, "flow": None, "ts": None}
    max_full = {"stage": None, "flow": None, "ts": None}

    horizon_sec = horizon_hours * 3600 if horizon_hours > 0 else None

    for p in points:
        ts_raw = p.get("ts")
        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        if dt is None:
            continue
        delta = (dt - now).total_seconds()
        if delta < 0:
            # Only look forward for maxima.
            continue
        if horizon_sec is not None and delta > horizon_sec:
            continue

        stage = p.get("stage")
        flow = p.get("flow")

        def bump(target: Dict[str, Any]) -> None:
            if isinstance(stage, (int, float)):
                if target["stage"] is None or stage > target["stage"]:
                    target["stage"] = stage
                    target["ts"] = dt.isoformat()
            if isinstance(flow, (int, float)):
                if target["flow"] is None or flow > target["flow"]:
                    target["flow"] = flow
                    target["ts"] = dt.isoformat()

        if delta <= 3 * 3600:
            bump(max_3h)
        if delta <= 24 * 3600:
            bump(max_24h)
        bump(max_full)

    return {
        "max_3h": max_3h,
        "max_24h": max_24h,
        "max_full": max_full,
    }


def parse_nwrfc_text(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse NW RFC textPlot output into observed and forecast series.

    We expect a header line with "Forecast/Trend Issued: <ts> <TZ>", a header
    row with "Date/Time (PST) Stage Discharge", and then rows where the first
    four columns are observed (date, time, stage, discharge) and optional
    forecast columns follow.
    """
    if not text:
        return {"observed": [], "forecast": []}

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tz_label: str | None = None

    for ln in lines:
        if "Forecast/Trend Issued:" in ln:
            parts = ln.split()
            if parts:
                tz_label = parts[-1]
            break

    observed: List[Dict[str, Any]] = []
    forecast: List[Dict[str, Any]] = []

    for ln in lines:
        # Skip obvious headers.
        if ln.startswith("SF ") or "Date/Time" in ln or ln.startswith("Observed"):
            continue
        parts = ln.split()
        if len(parts) < 4:
            continue
        # Observed block.
        o_date, o_time, o_stage_raw, o_flow_raw = parts[0], parts[1], parts[2], parts[3]
        o_dt = _parse_nwrfc_timestamp(o_date, o_time, tz_label)
        if o_dt is not None:
            try:
                o_stage = float(o_stage_raw)
            except Exception:
                o_stage = None
            try:
                o_flow = float(o_flow_raw)
            except Exception:
                o_flow = None
            observed.append(
                {
                    "ts": o_dt.isoformat(),
                    "stage": o_stage,
                    "flow": o_flow,
                }
            )

        # Forecast block may follow on the same line.
        if len(parts) >= 8:
            f_date, f_time, f_stage_raw, f_flow_raw = parts[4], parts[5], parts[6], parts[7]
            f_dt = _parse_nwrfc_timestamp(f_date, f_time, tz_label)
            if f_dt is not None:
                try:
                    f_stage = float(f_stage_raw)
                except Exception:
                    f_stage = None
                try:
                    f_flow = float(f_flow_raw)
                except Exception:
                    f_flow = None
                forecast.append(
                    {
                        "ts": f_dt.isoformat(),
                        "stage": f_stage,
                        "flow": f_flow,
                    }
                )

    observed.sort(key=lambda p: p["ts"])
    forecast.sort(key=lambda p: p["ts"])
    return {"observed": observed, "forecast": forecast}


def update_forecast_state(
    state: Dict[str, Any],
    gauge_id: str,
    points: List[Dict[str, Any]],
    now: datetime,
    horizon_hours: int,
) -> None:
    """
    Store forecast points and summary for a gauge, and compute basic bias stats
    using observed history when available.
    """
    if not points:
        return

    forecast_state = state.setdefault("forecast", {})
    g_forecast = forecast_state.setdefault(gauge_id, {})

    # De-duplicate by timestamp and trim to a reasonable time window around "now"
    # so we do not accumulate unbounded forecast history.
    by_ts: Dict[str, Dict[str, Any]] = {}
    for p in points:
        ts = p.get("ts")
        if isinstance(ts, str):
            by_ts[ts] = p
    ordered_ts = sorted(by_ts.keys())

    horizon_sec = horizon_hours * 3600 if horizon_hours > 0 else None
    trimmed_points: List[Dict[str, Any]] = []
    for ts in ordered_ts:
        p = by_ts[ts]
        dt = _parse_timestamp(ts)
        if dt is None:
            continue
        if horizon_sec is not None:
            delta = (dt - now).total_seconds()
            if delta > horizon_sec or delta < -horizon_sec:
                continue
        trimmed_points.append(p)

    g_forecast["points"] = trimmed_points

    summary = summarize_forecast_points(g_forecast["points"], now=now, horizon_hours=horizon_hours)
    g_forecast["summary"] = summary

    # Amplitude bias: compare last observed vs nearest forecast.
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    last_ts_str = g_state.get("last_timestamp")
    last_ts = _parse_timestamp(last_ts_str) if isinstance(last_ts_str, str) else None
    last_stage = g_state.get("last_stage")
    last_flow = g_state.get("last_flow")

    if last_ts is not None:
        nearest = None
        best_dt = None
        for p in g_forecast["points"]:
            ts_raw = p.get("ts")
            dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
            if dt is None:
                continue
            diff = abs((dt - last_ts).total_seconds())
            if best_dt is None or diff < best_dt:
                best_dt = diff
                nearest = p

        if nearest is not None:
            f_stage = nearest.get("stage")
            f_flow = nearest.get("flow")
            bias: Dict[str, Any] = {}
            if isinstance(last_stage, (int, float)) and isinstance(f_stage, (int, float)):
                bias["stage_delta"] = last_stage - f_stage
                bias["stage_ratio"] = (last_stage / f_stage) if f_stage not in (0, None) else None
            if isinstance(last_flow, (int, float)) and isinstance(f_flow, (int, float)):
                bias["flow_delta"] = last_flow - f_flow
                bias["flow_ratio"] = (last_flow / f_flow) if f_flow not in (0, None) else None
            if bias:
                g_forecast["bias"] = bias

    # Phase: compare forecast peak time vs observed recent peak time.
    history = (g_state.get("history") or [])[-HISTORY_LIMIT:]
    if history and summary.get("max_full", {}).get("ts"):
        forecast_peak_ts = _parse_timestamp(summary["max_full"]["ts"])
        if forecast_peak_ts is not None:
            # Observed peak over the same window.
            peak_obs_dt = None
            peak_obs_stage = None
            for entry in history:
                ts_raw = entry.get("ts")
                dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
                s = entry.get("stage")
                if dt is None or not isinstance(s, (int, float)):
                    continue
                if peak_obs_stage is None or s > peak_obs_stage:
                    peak_obs_stage = s
                    peak_obs_dt = dt

            if peak_obs_dt is not None:
                shift_sec = (peak_obs_dt - forecast_peak_ts).total_seconds()
                g_forecast["phase_shift_sec"] = shift_sec


def maybe_refresh_forecasts(state: Dict[str, Any], args: argparse.Namespace) -> None:
    """
    Refresh forecasts for all gauges at most once per FORECAST_REFRESH_MIN minutes.
    Forecasts can be enabled via CLI (--forecast-base) or config.toml.
    """
    now = datetime.now(timezone.utc)
    meta = state.setdefault("meta", {})
    last_fetch_raw = meta.get("last_forecast_fetch")
    last_fetch = _parse_timestamp(last_fetch_raw) if isinstance(last_fetch_raw, str) else None
    if last_fetch is not None:
        age_sec = (now - last_fetch).total_seconds()
        if age_sec < FORECAST_REFRESH_MIN * 60:
            return

    # Skip quickly if no gauge has any forecast configuration.
    any_configured = False
    for gauge_id, site_no in SITE_MAP.items():
        template = _forecast_template_for_gauge(gauge_id, site_no, args)
        if template:
            any_configured = True
            break
    if not any_configured:
        return

    for gauge_id, site_no in SITE_MAP.items():
        template = _forecast_template_for_gauge(gauge_id, site_no, args)
        if not template:
            continue
        points = fetch_forecast_series(template, gauge_id, site_no, args.forecast_hours)
        if points:
            update_forecast_state(state, gauge_id, points, now=now, horizon_hours=args.forecast_hours)

    meta["last_forecast_fetch"] = now.isoformat()


def update_nwrfc_state(
    state: Dict[str, Any],
    gauge_id: str,
    series: Dict[str, List[Dict[str, Any]]],
    now: datetime,
) -> None:
    """
    Store NW RFC observed/forecast series for a gauge and compute simple
    differences vs the latest USGS observation when timestamps align.
    """
    observed = series.get("observed") or []
    forecast = series.get("forecast") or []
    if not observed and not forecast:
        return

    nwrfc_state = state.setdefault("nwrfc", {})
    g_nwrfc = nwrfc_state.setdefault(gauge_id, {})
    g_nwrfc["observed"] = observed
    g_nwrfc["forecast"] = forecast
    g_nwrfc["last_fetch_at"] = now.isoformat()

    # Cross-check against the latest USGS observation at the same timestamp.
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    last_ts_str = g_state.get("last_timestamp")
    last_ts = _parse_timestamp(last_ts_str) if isinstance(last_ts_str, str) else None
    if last_ts is None:
        return

    # Find NW RFC point with matching timestamp.
    match = None
    for p in reversed(observed):
        ts_raw = p.get("ts")
        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        if dt is not None and dt == last_ts:
            match = p
            break

    if match is None:
        return

    usgs_stage = g_state.get("last_stage")
    usgs_flow = g_state.get("last_flow")
    nwrfc_stage = match.get("stage")
    nwrfc_flow = match.get("flow")

    diff: Dict[str, Any] = {"ts": last_ts.isoformat()}
    if isinstance(usgs_stage, (int, float)) and isinstance(nwrfc_stage, (int, float)):
        diff["stage_delta"] = usgs_stage - nwrfc_stage
    if isinstance(usgs_flow, (int, float)) and isinstance(nwrfc_flow, (int, float)):
        diff["flow_delta"] = usgs_flow - nwrfc_flow
    if len(diff) > 1:
        g_nwrfc["diff_vs_usgs"] = diff


def maybe_refresh_nwrfc(state: Dict[str, Any], args: argparse.Namespace) -> None:
    """
    Optionally cross-check observed stage/flow against NW RFC textPlot
    output for supported stations (currently GARW1).
    """
    enabled = getattr(args, "nwrfc_text", False)
    if not enabled:
        return

    now = datetime.now(timezone.utc)
    meta = state.setdefault("meta", {})
    last_fetch_raw = meta.get("last_nwrfc_fetch")
    last_fetch = _parse_timestamp(last_fetch_raw) if isinstance(last_fetch_raw, str) else None
    if last_fetch is not None:
        age_sec = (now - last_fetch).total_seconds()
        if age_sec < NWRFC_REFRESH_MIN * 60:
            return

    for gauge_id, nwrfc_id in NWRFC_ID_MAP.items():
        params = {"id": nwrfc_id, "pe": "HG", "bt": "on"}
        try:
            text = get_text(NWRFC_TEXT_BASE, params=params, timeout=10.0)
        except Exception:
            continue
        series = parse_nwrfc_text(text)
        if series.get("observed") or series.get("forecast"):
            update_nwrfc_state(state, gauge_id, series, now=now)

    meta["last_nwrfc_fetch"] = now.isoformat()


def maybe_refresh_community(state: Dict[str, Any], args: argparse.Namespace) -> None:
    """
    Optionally refresh shared cadence/latency priors from a community endpoint.

    If `--community-base` is provided, we fetch `{base}/summary.json` (or the URL
    directly if it already ends with `.json`) at most once per 24h and use it to
    seed gauges that have low confidence / few samples.

    This is a soft dependency: failures are ignored.
    """
    base = getattr(args, "community_base", "")
    if not isinstance(base, str) or not base:
        return

    now = datetime.now(timezone.utc)
    meta = state.setdefault("meta", {})
    last_fetch_raw = meta.get("last_community_fetch")
    last_fetch = _parse_timestamp(last_fetch_raw) if isinstance(last_fetch_raw, str) else None
    if last_fetch is not None:
        age_sec = (now - last_fetch).total_seconds()
        if age_sec < 24 * 3600:
            return

    base_clean = base.rstrip("/")
    if base_clean.endswith(".json"):
        url = base_clean
    else:
        url = f"{base_clean}/summary.json"

    try:
        summary = get_json(url, timeout=5.0)
    except Exception:
        return
    if not isinstance(summary, dict):
        return

    stations = summary.get("stations") or summary.get("gauges") or summary.get("sites")
    if not isinstance(stations, dict):
        return

    gauges_state = state.setdefault("gauges", {})
    for gauge_id, site_no in SITE_MAP.items():
        if not isinstance(site_no, str) or not site_no:
            continue
        remote = stations.get(site_no)
        if not isinstance(remote, dict):
            remote = stations.get(gauge_id) if isinstance(gauge_id, str) else None
        if not isinstance(remote, dict):
            continue

        g_state = gauges_state.setdefault(gauge_id, {})

        # Cadence multiple + fit: only adopt if we don't have a confident snap yet.
        local_mult = g_state.get("cadence_mult")
        local_fit = g_state.get("cadence_fit")
        remote_mult = remote.get("cadence_mult")
        remote_fit = remote.get("cadence_fit")
        low_confidence = (
            not isinstance(local_mult, int)
            or not isinstance(local_fit, (int, float))
            or float(local_fit) < CADENCE_FIT_THRESHOLD
        )
        if low_confidence and isinstance(remote_mult, int) and remote_mult > 0:
            g_state["cadence_mult"] = int(remote_mult)
            if isinstance(remote_fit, (int, float)):
                g_state["cadence_fit"] = float(remote_fit)
            if "mean_interval_sec" not in g_state:
                g_state["mean_interval_sec"] = float(remote_mult * CADENCE_BASE_SEC)

        # Phase offset: only seed if not learned locally.
        local_phase = g_state.get("phase_offset_sec")
        remote_phase = remote.get("phase_offset_sec")
        if not isinstance(local_phase, (int, float)) and isinstance(remote_phase, (int, float)):
            cadence = g_state.get("mean_interval_sec")
            if isinstance(cadence, (int, float)) and cadence > 0:
                g_state["phase_offset_sec"] = float(remote_phase) % float(cadence)

        # Latency priors: only seed if we have very few samples locally.
        local_samples = g_state.get("latencies_sec")
        if not isinstance(local_samples, list) or len(local_samples) < 3:
            remote_loc = remote.get("latency_loc_sec") or remote.get("latency_median_sec")
            remote_scale = remote.get("latency_scale_sec") or remote.get("latency_mad_sec")
            if isinstance(remote_loc, (int, float)) and float(remote_loc) >= 0:
                g_state["latency_loc_sec"] = float(remote_loc)
            if isinstance(remote_scale, (int, float)) and float(remote_scale) > 0:
                g_state["latency_scale_sec"] = float(remote_scale)

    meta["last_community_fetch"] = now.isoformat()


def maybe_publish_community_samples(
    state: Dict[str, Any],
    args: argparse.Namespace,
    updates: Dict[str, bool],
    poll_ts: datetime,
) -> None:
    """
    Optionally publish observed update/latency samples to a community endpoint.

    Enabled via `--community-base` + `--community-publish`. Uses POST
    `{base}/sample`. Soft failures are ignored. Under Pyodide this is a no-op.
    """
    base = getattr(args, "community_base", "")
    publish = bool(getattr(args, "community_publish", False))
    if not publish or not isinstance(base, str) or not base:
        return

    base_clean = base.rstrip("/")
    if base_clean.endswith(".json") and "/" in base_clean:
        base_clean = base_clean.rsplit("/", 1)[0]
    url = f"{base_clean}/sample"

    gauges_state = state.get("gauges", {})
    if not isinstance(gauges_state, dict):
        return

    for gauge_id, did_update in updates.items():
        if not did_update:
            continue
        g_state = gauges_state.get(gauge_id)
        if not isinstance(g_state, dict):
            continue
        site_no = SITE_MAP.get(gauge_id)
        if not isinstance(site_no, str) or not site_no:
            continue
        obs_ts = g_state.get("last_timestamp")
        if not isinstance(obs_ts, str):
            continue
        lower = g_state.get("last_latency_lower_sec")
        upper = g_state.get("last_latency_upper_sec")
        sample = g_state.get("last_latency_sample_sec")
        if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
            continue
        if not isinstance(sample, (int, float)):
            continue

        payload = {
            "version": 1,
            "site_no": site_no,
            "gauge_id": gauge_id,
            "obs_ts": obs_ts,
            "poll_ts": poll_ts.isoformat(),
            "lower_sec": float(lower),
            "upper_sec": float(upper),
            "latency_sec": float(sample),
        }
        try:
            post_json(url, payload, timeout=5.0)
        except Exception:
            continue


_WEB_COMMUNITY_QUEUE: List[Dict[str, Any]] = []
_WEB_COMMUNITY_DRAIN_TASK: Any | None = None


async def _drain_web_community_queue(url: str) -> None:
    import asyncio

    while _WEB_COMMUNITY_QUEUE:
        payload = _WEB_COMMUNITY_QUEUE.pop(0)
        try:
            await post_json_async(url, payload, timeout=5.0)
        except Exception:
            pass
        await asyncio.sleep(0)


async def maybe_publish_community_samples_async(
    state: Dict[str, Any],
    args: argparse.Namespace,
    updates: Dict[str, bool],
    poll_ts: datetime,
) -> None:
    """
    Async publisher for Pyodide/web builds.

    Mirrors `maybe_publish_community_samples`, but uses async fetch under Pyodide
    and avoids blocking the UI tick by enqueueing and draining in the background.
    """
    base = getattr(args, "community_base", "")
    publish = bool(getattr(args, "community_publish", False))
    if not publish or not isinstance(base, str) or not base:
        return

    base_clean = base.rstrip("/")
    if base_clean.endswith(".json") and "/" in base_clean:
        base_clean = base_clean.rsplit("/", 1)[0]
    url = f"{base_clean}/sample"

    gauges_state = state.get("gauges", {})
    if not isinstance(gauges_state, dict):
        return

    batch: List[Dict[str, Any]] = []
    for gauge_id, did_update in updates.items():
        if not did_update:
            continue
        g_state = gauges_state.get(gauge_id)
        if not isinstance(g_state, dict):
            continue
        site_no = SITE_MAP.get(gauge_id)
        if not isinstance(site_no, str) or not site_no:
            continue
        obs_ts = g_state.get("last_timestamp")
        if not isinstance(obs_ts, str):
            continue
        lower = g_state.get("last_latency_lower_sec")
        upper = g_state.get("last_latency_upper_sec")
        sample = g_state.get("last_latency_sample_sec")
        if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
            continue
        if not isinstance(sample, (int, float)):
            continue
        batch.append(
            {
                "version": 1,
                "site_no": site_no,
                "gauge_id": gauge_id,
                "obs_ts": obs_ts,
                "poll_ts": poll_ts.isoformat(),
                "lower_sec": float(lower),
                "upper_sec": float(upper),
                "latency_sec": float(sample),
            }
        )

    if not batch:
        return

    global _WEB_COMMUNITY_DRAIN_TASK
    _WEB_COMMUNITY_QUEUE.extend(batch)
    if len(_WEB_COMMUNITY_QUEUE) > 50:
        del _WEB_COMMUNITY_QUEUE[0 : len(_WEB_COMMUNITY_QUEUE) - 50]

    if _WEB_COMMUNITY_DRAIN_TASK is None or _WEB_COMMUNITY_DRAIN_TASK.done():
        import asyncio

        _WEB_COMMUNITY_DRAIN_TASK = asyncio.create_task(_drain_web_community_queue(url))


def update_state_with_readings(
    state: Dict[str, Any],
    readings: Dict[str, Dict[str, Any]],
    poll_ts: datetime | None = None,
) -> Dict[str, bool]:
    return _streamvis_state.update_state_with_readings(state, readings, poll_ts=poll_ts)


def predict_next_poll(state: Dict[str, Any], now: datetime) -> datetime:
    """
    Legacy helper retained for compatibility; delegates to the
    latency-aware scheduler.
    """
    return schedule_next_poll(state, now, MIN_RETRY_SEC)


def predict_gauge_next(state: Dict[str, Any], gauge_id: str, now: datetime) -> datetime | None:
    return _streamvis_scheduler.predict_gauge_next(state, gauge_id, now)


def schedule_next_poll(
    state: Dict[str, Any],
    now: datetime,
    min_retry_seconds: int,
) -> datetime:
    return _streamvis_scheduler.schedule_next_poll(state, now, min_retry_seconds)


def control_summary(state: Dict[str, Any], now: datetime) -> str:
    try:
        return json.dumps(
            _streamvis_scheduler.control_summary(state, now),
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    except Exception:
        return ""


def _history_values(state: Dict[str, Any], gauge_id: str, metric: str, limit: int = HISTORY_LIMIT) -> List[float]:
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    history = g_state.get("history", [])
    values: List[float] = []
    for entry in history[-limit:]:
        val = entry.get(metric)
        if isinstance(val, (int, float)):
            values.append(float(val))
    return values


def _render_sparkline(values: List[float], width: int = 48) -> str:
    if not values:
        return "(no data)"
    if len(values) == 1:
        return f"{values[0]:.2f}"

    chars = " .:-=+*#%@"
    vmin = min(values)
    vmax = max(values)
    span = vmax - vmin
    if span <= 0:
        return ("=" * min(len(values), width))[:width]

    step = max(1, math.ceil(len(values) / width))
    sampled = values[-step * width :: step]
    line = []
    for v in sampled[-width:]:
        level = int((v - vmin) / span * (len(chars) - 1))
        line.append(chars[level])
    return "".join(line)

def _unique_gauge_ids(items: Any) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, str) or not it:
            continue
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def compute_table_gauges(state: Dict[str, Any]) -> tuple[List[str], int | None]:
    """
    Return gauges in display order and an optional divider index.

    When Nearby is enabled and we have a cached `meta.nearby_gauges` list, we
    group those gauges at the bottom of the main table (without duplicates) and
    return a divider index between the static and nearby sections.
    """
    gauges = ordered_gauges()

    meta = state.get("meta", {})
    if not isinstance(meta, dict) or not bool(meta.get("nearby_enabled")):
        return gauges, None

    nearby_ids = _unique_gauge_ids(meta.get("nearby_gauges"))
    if not nearby_ids:
        return gauges, None

    nearby_ids = [gid for gid in nearby_ids if gid in SITE_MAP]
    if not nearby_ids:
        return gauges, None

    nearby_set = set(nearby_ids)
    static = [g for g in gauges if g not in nearby_set]
    combined = static + nearby_ids
    divider_index = len(static)
    if divider_index <= 0 or divider_index >= len(combined):
        return combined, None
    return combined, divider_index


def render_table(readings: Dict[str, Dict[str, Any]], state: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    header = (
        f"{'Gauge':<6} "
        f"{'Stage(ft)':>9} "
        f"{'Flow(cfs)':>10} "
        f"{'Status':<12} "
        f"{'Observed':>9} "
        f"{'Next ETA':>9}"
    )
    print(header)
    print("-" * len(header))

    gauges, divider_index = compute_table_gauges(state)
    for idx, gauge_id in enumerate(gauges):
        if divider_index is not None and idx == divider_index:
            print(f"-- Nearby --".center(len(header), "-"))
        reading = readings.get(gauge_id, {})
        stage = reading.get("stage")
        flow = reading.get("flow")
        status = reading.get("status", "UNKNOWN")

        gauges_state = state.get("gauges", {})
        g_state = gauges_state.get(gauge_id, {})
        observed_at = reading.get("observed_at") or _parse_timestamp(g_state.get("last_timestamp"))
        next_eta = predict_gauge_next(state, gauge_id, now)

        stage_str = f"{stage:.2f}" if isinstance(stage, (int, float)) else "--"
        flow_str = f"{int(flow):d}" if isinstance(flow, (int, float)) else "--"
        obs_str = _fmt_clock(observed_at)
        next_str = _fmt_rel(now, next_eta) if next_eta and next_eta >= now else "now"

        print(
            f"{gauge_id:<6s} "
            f"{stage_str:>9s} "
            f"{flow_str:>10s} "
            f"{status:<12s} "
            f"{obs_str:>9s} "
            f"{next_str:>9s}"
        )


def color_for_status(status: str, palette: Dict[str, int]) -> int:
    status = (status or "").upper()
    if "MAJOR" in status:
        return palette.get("major", 0)
    if "MOD" in status:
        return palette.get("moderate", 0)
    if "MINOR" in status:
        return palette.get("minor", 0)
    if "ACTION" in status:
        return palette.get("action", 0)
    return palette.get("normal", 0)


def draw_screen(
    stdscr: Any,
    curses_mod: Any,
    gauges: List[str],
    divider_index: int | None,
    readings: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    selected_idx: int,
    chart_metric: str,
    status_msg: str,
    next_poll_at: datetime | None,
    palette: Dict[str, int],
    detail_mode: bool,
    table_start: int,
    state_file: str,
    update_alert: bool,
) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    now = datetime.now(timezone.utc)
    local_now = datetime.now().astimezone()

    title = "STREAMVIS // SNOQUALMIE WATCH"
    clock_line = (
        f"Now {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    stdscr.addstr(0, 0, title[:max_x - 1], curses_mod.A_BOLD | palette.get("title", 0))
    stdscr.addstr(1, 0, clock_line[:max_x - 1], palette.get("normal", 0))

    wide = max_x >= 60
    medium = max_x >= 49
    narrow = max_x >= 39

    if wide:
        header = (
            f"{'Gauge':<6} "
            f"{'Stage(ft)':>9} "
            f"{'Flow(cfs)':>10} "
            f"{'Status':<11} "
            f"{'Observed':>9} "
            f"{'Next ETA':>9}"
        )
    elif medium:
        header = (
            f"{'Gauge':<6} "
            f"{'Stage(ft)':>9} "
            f"{'Flow(cfs)':>10} "
            f"{'Status':<11} "
            f"{'Observed':>9}"
        )
    elif narrow:
        header = (
            f"{'Gauge':<6} "
            f"{'Stage(ft)':>9} "
            f"{'Flow(cfs)':>10} "
            f"{'Status':<11}"
        )
    else:
        header = (
            f"{'Gauge':<6} "
            f"{'Stage(ft)':>9} "
            f"{'Flow(cfs)':>10}"
        )

    stdscr.addstr(table_start, 0, header[:max_x - 1], curses_mod.A_UNDERLINE | palette.get("normal", 0))

    has_divider = (
        isinstance(divider_index, int) and 0 < divider_index < len(gauges)
    )
    divider_row = table_start + 1 + divider_index if has_divider else None
    if divider_row is not None and 0 <= divider_row < max_y - 3:
        divider = "-- Nearby --"
        try:
            line = divider.center(max_x - 1, "-")
        except Exception:
            line = divider
        stdscr.addstr(divider_row, 0, line[:max_x - 1], palette.get("dim", 0))

    selected_id = None
    if gauges and 0 <= selected_idx < len(gauges):
        selected_id = gauges[selected_idx]

    last_gauge_row: int | None = None
    for idx, gauge_id in enumerate(gauges):
        row = table_start + 1 + idx
        if divider_row is not None and row >= divider_row:
            row += 1
        if row >= max_y - 5:
            break  # leave space for detail + footer
        last_gauge_row = row

        reading = readings.get(gauge_id, {})
        gauges_state = state.get("gauges", {})
        g_state = gauges_state.get(gauge_id, {}) if isinstance(gauges_state, dict) else {}
        if not isinstance(g_state, dict):
            g_state = {}

        status = reading.get("status", "UNKNOWN")
        stage = reading.get("stage")
        flow = reading.get("flow")
        if not isinstance(stage, (int, float)):
            last_stage = g_state.get("last_stage")
            if isinstance(last_stage, (int, float)):
                stage = float(last_stage)
        if not isinstance(flow, (int, float)):
            last_flow = g_state.get("last_flow")
            if isinstance(last_flow, (int, float)):
                flow = float(last_flow)

        if status == "UNKNOWN" and isinstance(stage, (int, float)):
            status = classify_status(gauge_id, float(stage))

        observed_at = reading.get("observed_at") or _parse_timestamp(g_state.get("last_timestamp"))
        next_eta = predict_gauge_next(state, gauge_id, now)

        stage_str = f"{stage:.2f}" if isinstance(stage, (int, float)) else "--"
        flow_str = f"{int(flow):d}" if isinstance(flow, (int, float)) else "--"
        obs_str = _fmt_clock(observed_at)
        next_str = _fmt_rel(now, next_eta) if next_eta and next_eta >= now else "now"

        if wide:
            line = (
                f"{gauge_id:<6s} "
                f"{stage_str:>9s} "
                f"{flow_str:>10s} "
                f"{status:<11s} "
                f"{obs_str:>9s} "
                f"{next_str:>9s}"
            )
        elif medium:
            line = (
                f"{gauge_id:<6s} "
                f"{stage_str:>9s} "
                f"{flow_str:>10s} "
                f"{status:<11s} "
                f"{obs_str:>9s}"
            )
        elif narrow:
            line = (
                f"{gauge_id:<6s} "
                f"{stage_str:>9s} "
                f"{flow_str:>10s} "
                f"{status:<11s}"
            )
        else:
            line = (
                f"{gauge_id:<6s} "
                f"{stage_str:>9s} "
                f"{flow_str:>10s}"
            )
        color = color_for_status(status, palette)

        if selected_id is not None and gauge_id == selected_id:
            stdscr.addstr(row, 0, line[:max_x - 1], curses_mod.A_REVERSE | color)
        else:
            stdscr.addstr(row, 0, line[:max_x - 1], color)

    last_row = last_gauge_row if last_gauge_row is not None else table_start
    detail_y = last_row + 2
    if detail_y < max_y - 2:
        if not gauges:
            selected = ""
        else:
            selected = gauges[min(selected_idx, len(gauges) - 1)]
        g_state = state.get("gauges", {}).get(selected, {})
        reading = readings.get(selected, {})
        observed_at = reading.get("observed_at") or _parse_timestamp(g_state.get("last_timestamp"))
        next_eta = predict_gauge_next(state, selected, now)
        stage = reading.get("stage")
        flow = reading.get("flow")
        if not isinstance(stage, (int, float)):
            last_stage = g_state.get("last_stage")
            if isinstance(last_stage, (int, float)):
                stage = float(last_stage)
        if not isinstance(flow, (int, float)):
            last_flow = g_state.get("last_flow")
            if isinstance(last_flow, (int, float)):
                flow = float(last_flow)
        status = reading.get("status", "UNKNOWN")
        if status == "UNKNOWN" and isinstance(stage, (int, float)):
            status = classify_status(selected, float(stage))
        detail = (
            f"{selected} | Stage: {stage if stage is not None else '-'} ft | "
            f"Flow: {int(flow) if isinstance(flow, (int, float)) else '-'} cfs | "
            f"Status: {status}"
        )
        timing = (
            f"Observed {_fmt_clock(observed_at, with_date=False)} ({_fmt_rel(now, observed_at)}), "
            f"Next ETA: {_fmt_rel(now, next_eta) if next_eta and next_eta >= now else 'now'}"
        )
        latency_loc = g_state.get("latency_loc_sec")
        latency_scale = g_state.get("latency_scale_sec")
        if not isinstance(latency_loc, (int, float)):
            latency_loc = g_state.get("latency_median_sec")
        if not isinstance(latency_scale, (int, float)):
            latency_scale = g_state.get("latency_mad_sec")
        if isinstance(latency_loc, (int, float)):
            ll = int(round(latency_loc))
            ls = int(round(latency_scale)) if isinstance(latency_scale, (int, float)) else 0
            timing += f" | Latency {ll}±{ls}s"
        stdscr.addstr(detail_y, 0, detail[:max_x - 1], palette.get("normal", 0) | curses_mod.A_BOLD)
        stdscr.addstr(detail_y + 1, 0, timing[:max_x - 1], palette.get("normal", 0))

        if detail_mode:
            # Expanded detail: table of recent updates with per-update deltas.
            history = g_state.get("history", []) or []
            recent = history[-6:]
            table_y = detail_y + 3
            if table_y < max_y - 2:
                header_line = (
                    f"{'Time':>8}  "
                    f"{'Stage':>8} "
                    f"{'ΔStage':>8} "
                    f"{'Flow':>8} "
                    f"{'ΔFlow':>8}"
                )
                stdscr.addstr(table_y, 0, header_line[:max_x - 1], palette.get("dim", 0))
                prev_stage = None
                prev_flow = None
                row_y = table_y + 1
                for entry in recent:
                    if row_y >= max_y - 3:
                        break
                    ts_raw = entry.get("ts")
                    ts_dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
                    ts_str = _fmt_clock(ts_dt, with_date=False)
                    stage_v = entry.get("stage")
                    flow_v = entry.get("flow")
                    ds = (
                        stage_v - prev_stage
                        if isinstance(stage_v, (int, float)) and isinstance(prev_stage, (int, float))
                        else None
                    )
                    df = (
                        flow_v - prev_flow
                        if isinstance(flow_v, (int, float)) and isinstance(prev_flow, (int, float))
                        else None
                    )
                    prev_stage = stage_v
                    prev_flow = flow_v
                    stage_str = f"{stage_v:8.2f}" if isinstance(stage_v, (int, float)) else "      --"
                    ds_str = f"{ds:+8.2f}" if isinstance(ds, (int, float)) else "      --"
                    flow_str = f"{int(flow_v):8d}" if isinstance(flow_v, (int, float)) else "      --"
                    df_str = f"{int(df):+8d}" if isinstance(df, (int, float)) else "      --"
                    line = f"{ts_str:>8s}  {stage_str} {ds_str} {flow_str} {df_str}"
                    stdscr.addstr(row_y, 0, line[:max_x - 1], palette.get("chart", 0))
                    row_y += 1

                # Simple trend summary over the recent window.
                if len(recent) >= 2:
                    times: List[datetime] = []
                    stages: List[float] = []
                    flows: List[float] = []
                    for entry in recent:
                        ts_raw = entry.get("ts")
                        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
                        if dt is None:
                            continue
                        times.append(dt)
                        s = entry.get("stage")
                        f = entry.get("flow")
                        if isinstance(s, (int, float)):
                            stages.append(float(s))
                        if isinstance(f, (int, float)):
                            flows.append(float(f))

                    if len(times) >= 2:
                        dh_hours = (times[-1] - times[0]).total_seconds() / 3600.0 or 1.0
                    else:
                        dh_hours = 1.0

                    if len(stages) >= 2:
                        stage_trend = (stages[-1] - stages[0]) / dh_hours
                    else:
                        stage_trend = 0.0

                    if len(flows) >= 2:
                        flow_trend = (flows[-1] - flows[0]) / max(dh_hours, 1e-6)
                    else:
                        flow_trend = 0.0

                    trend_line = f"Trend: stage {stage_trend:+.2f} ft/h   flow {flow_trend:+.0f} cfs/h"
                    if row_y < max_y - 2:
                        stdscr.addstr(row_y, 0, trend_line[:max_x - 1], palette.get("dim", 0))
                        row_y += 1

                # Latency stats.
                latency_loc = g_state.get("latency_loc_sec")
                latency_scale = g_state.get("latency_scale_sec")
                if not isinstance(latency_loc, (int, float)):
                    latency_loc = g_state.get("latency_median_sec")
                if not isinstance(latency_scale, (int, float)):
                    latency_scale = g_state.get("latency_mad_sec")
                if isinstance(latency_loc, (int, float)) and row_y < max_y - 2:
                    lm = int(round(latency_loc))
                    ls = int(round(latency_scale)) if isinstance(latency_scale, (int, float)) else 0
                    lat_line = f"Latency (obs→API): {lm}±{ls}s"
                    stdscr.addstr(row_y, 0, lat_line[:max_x - 1], palette.get("dim", 0))
                    row_y += 1

                # Poll efficiency (calls per real update).
                last_polls = g_state.get("last_polls_per_update")
                polls_ewma = g_state.get("polls_per_update_ewma")
                if (
                    (isinstance(last_polls, (int, float)) or isinstance(polls_ewma, (int, float)))
                    and row_y < max_y - 2
                ):
                    last_str = f"{int(last_polls)}" if isinstance(last_polls, (int, float)) else "--"
                    ewma_str = f"{float(polls_ewma):.2f}" if isinstance(polls_ewma, (int, float)) else "--"
                    calls_line = f"Calls/update: last {last_str}  ewma {ewma_str}"
                    stdscr.addstr(row_y, 0, calls_line[:max_x - 1], palette.get("dim", 0))
                    row_y += 1

                # NW RFC cross-check (if available).
                nwrfc_all = state.get("nwrfc", {}).get(selected, {})
                diff = nwrfc_all.get("diff_vs_usgs") if isinstance(nwrfc_all, dict) else None
                if diff and row_y < max_y - 2:
                    sd = diff.get("stage_delta")
                    qd = diff.get("flow_delta")
                    sd_str = f"{sd:+.2f} ft" if isinstance(sd, (int, float)) else "n/a"
                    qd_str = f"{qd:+.0f} cfs" if isinstance(qd, (int, float)) else "n/a"
                    line = f"NW RFC vs USGS (last): Δstage {sd_str}, Δflow {qd_str}"
                    stdscr.addstr(row_y, 0, line[:max_x - 1], palette.get("dim", 0))
                    row_y += 1

                # Forecast summary (if available).
                forecast_all = state.get("forecast", {}).get(selected, {})
                summary = forecast_all.get("summary") if isinstance(forecast_all, dict) else None
                bias = forecast_all.get("bias") if isinstance(forecast_all, dict) else None
                phase_shift_sec = (
                    forecast_all.get("phase_shift_sec") if isinstance(forecast_all, dict) else None
                )
                if summary and row_y < max_y - 2:

                    def fmt_peak(key: str) -> str:
                        block = summary.get(key) or {}
                        s = block.get("stage")
                        q = block.get("flow")
                        s_str = f"{s:.2f} ft" if isinstance(s, (int, float)) else "--"
                        q_str = f"{int(q)} cfs" if isinstance(q, (int, float)) else "--"
                        return f"{s_str} / {q_str}"

                    line1 = (
                        f"Forecast peaks (stage/flow): "
                        f"3h {fmt_peak('max_3h')}  |  24h {fmt_peak('max_24h')}  |  full {fmt_peak('max_full')}"
                    )
                    stdscr.addstr(row_y, 0, line1[:max_x - 1], palette.get("dim", 0))
                    row_y += 1

                    if bias and row_y < max_y - 1:
                        sd = bias.get("stage_delta")
                        sr = bias.get("stage_ratio")
                        qd = bias.get("flow_delta")
                        qr = bias.get("flow_ratio")
                        sd_str = f"{sd:+.2f} ft" if isinstance(sd, (int, float)) else "n/a"
                        sr_str = f"{sr:.2f}×" if isinstance(sr, (int, float)) else "n/a"
                        qd_str = f"{qd:+.0f} cfs" if isinstance(qd, (int, float)) else "n/a"
                        qr_str = f"{qr:.2f}×" if isinstance(qr, (int, float)) else "n/a"
                        line2 = f"Vs forecast now: Δstage {sd_str} ({sr_str}), Δflow {qd_str} ({qr_str})"
                        stdscr.addstr(row_y, 0, line2[:max_x - 1], palette.get("dim", 0))
                        row_y += 1

                    if isinstance(phase_shift_sec, (int, float)) and row_y < max_y - 1:
                        hours = phase_shift_sec / 3600.0
                        sign = "earlier" if hours < 0 else "later"
                        line3 = f"Peak timing: {abs(hours):.2f} h {sign} than forecast"
                        stdscr.addstr(row_y, 0, line3[:max_x - 1], palette.get("dim", 0))
        else:
            # Compact detail: sparkline chart and summary stats.
            chart_vals = _history_values(state, selected, chart_metric)
            chart_line = _render_sparkline(chart_vals, width=max(10, max_x - 12))
            chart_label = f"{chart_metric.upper()} history ({len(chart_vals)} pts, newest right)"
            stdscr.addstr(detail_y + 3, 0, chart_label[:max_x - 1], palette.get("dim", 0))
            stdscr.addstr(detail_y + 4, 0, chart_line[:max_x - 1], palette.get("chart", 0))
            if chart_vals:
                delta = chart_vals[-1] - chart_vals[0]
                stats = f"{chart_metric}: min {min(chart_vals):.2f}  max {max(chart_vals):.2f}  Δ {delta:+.2f}"
                stdscr.addstr(detail_y + 5, 0, stats[:max_x - 1], palette.get("dim", 0))

    # Nearby toggle line (optional).
    meta = state.get("meta", {})
    nearby_enabled = bool(meta.get("nearby_enabled")) if isinstance(meta, dict) else False
    user_lat = meta.get("user_lat") if isinstance(meta, dict) else None
    user_lon = meta.get("user_lon") if isinstance(meta, dict) else None

    footer_y = max_y - 2
    toggle_y = footer_y - 1
    if toggle_y > detail_y and 0 <= toggle_y < max_y:
        on_off = "ON" if nearby_enabled else "off"
        toggle_line = f"[n] Nearby: {on_off}"
        if nearby_enabled and divider_index is not None:
            n_nearby = max(0, len(gauges) - divider_index)
            toggle_line += f" ({n_nearby} in table)"
        if nearby_enabled and not (
            isinstance(user_lat, (int, float)) and isinstance(user_lon, (int, float))
        ):
            toggle_line += " (allow location)"
        stdscr.addstr(toggle_y, 0, toggle_line[:max_x - 1], palette.get("dim", 0))
    if footer_y >= 0:
        next_multi = _fmt_rel(now, next_poll_at) if next_poll_at else "pending"
        footer = (
            "[↑/↓] select  [Enter] details  [c] toggle chart metric  [b] toggle alert  [n] nearby  [r] refresh  [f] force refetch  [q] quit  "
            f"Next fetch: {next_multi}  |  {status_msg}"
        )
        stdscr.addstr(footer_y, 0, footer[:max_x - 1], palette.get("dim", 0))

    info_y = footer_y + 1
    if 0 <= info_y < max_y:
        info_line = (
            f"Mode: TUI adaptive | Alerts: {'on' if update_alert else 'off'} | State: {state_file}"
        )
        stdscr.addstr(info_y, 0, info_line[:max_x - 1], palette.get("dim", 0))

    stdscr.refresh()


def handle_row_click(
    target_idx: int,
    selected_idx: int,
    detail_mode: bool,
    gauges: List[str],
) -> tuple[int, bool, str]:
    """
    Handle a tap/click on a gauge row.

    UX rule (mobile-friendly):
    - Tap a new row: select it, but only enter detail mode if we were already
      viewing details.
    - Tap the selected row: toggle detail mode.
    """
    if target_idx == selected_idx:
        detail_mode = not detail_mode
        status_msg = f"{gauges[selected_idx]} details {'on' if detail_mode else 'off'}"
        return selected_idx, detail_mode, status_msg

    selected_idx = target_idx
    if detail_mode:
        status_msg = f"Selected {gauges[selected_idx]} (details)"
    else:
        status_msg = f"Selected {gauges[selected_idx]} (tap again for details)"
    return selected_idx, detail_mode, status_msg


def tui_loop(args: argparse.Namespace) -> int:
    try:
        import curses
    except Exception:
        print("Curses is required for TUI mode and is unavailable on this platform.", file=sys.stderr)
        return 1

    gauges = ordered_gauges()

    # Row index where the table header is drawn; gauge rows start at
    # TUI_TABLE_START + 1. This is shared with the web click/tap mapping.
    TUI_TABLE_START = 3

    def run(stdscr: Any) -> int:
        nonlocal gauges
        curses.curs_set(0)
        # In TUI mode we want near-zero CPU usage when idle, so we rely on
        # a small blocking timeout for getch() instead of a busy loop.
        stdscr.nodelay(False)
        ui_tick = getattr(args, "ui_tick_sec", UI_TICK_SEC)
        if not isinstance(ui_tick, (int, float)) or ui_tick <= 0:
            ui_tick = UI_TICK_SEC
        stdscr.timeout(int(ui_tick * 1000))
        palette: Dict[str, int] = {"normal": 0, "title": 0, "dim": 0, "chart": 0}

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            palette.update(
                {
                    "normal": curses.color_pair(1),
                    "action": curses.color_pair(2),
                    "minor": curses.color_pair(2),
                    "moderate": curses.color_pair(3),
                    "major": curses.color_pair(3) | curses.A_BOLD,
                    "title": curses.color_pair(1) | curses.A_BOLD,
                    "dim": curses.color_pair(4),
                    "chart": curses.color_pair(4),
                }
            )

        state_path = Path(args.state_file)
        state = load_state(state_path)
        meta = state.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["api_backend"] = getattr(args, "usgs_backend", "blended")
        apply_dynamic_sites_from_state(state)
        maybe_backfill_state(state, args.backfill_hours)
        maybe_refresh_community(state, args)
        seed_user_location_from_args(state, args)
        gauges, divider_index = compute_table_gauges(state)
        save_state(state_path, state)
        readings: Dict[str, Dict[str, Any]] = {}
        selected_idx = 0
        chart_metric = args.chart_metric
        status_msg = "Awaiting first fetch..."
        next_poll_at = datetime.now(timezone.utc)
        retry_wait = args.min_retry_seconds
        detail_mode = False
        update_alert = getattr(args, "update_alert", True)

        def refresh_gauges() -> None:
            nonlocal gauges, divider_index, selected_idx
            selected_id = None
            if gauges and 0 <= selected_idx < len(gauges):
                selected_id = gauges[selected_idx]
            new_gauges, new_divider = compute_table_gauges(state)
            if new_gauges == gauges and new_divider == divider_index:
                return
            gauges = new_gauges
            divider_index = new_divider
            if selected_id is not None and selected_id in gauges:
                selected_idx = gauges.index(selected_id)
            elif gauges:
                selected_idx = min(selected_idx, len(gauges) - 1)
            else:
                selected_idx = 0

        while True:
            now = datetime.now(timezone.utc)
            if now >= next_poll_at:
                maybe_refresh_community(state, args)
                state.setdefault("meta", {})["last_fetch_at"] = now.isoformat()
                fetched = fetch_gauge_data(state)
                if fetched:
                    readings = fetched
                    retry_wait = args.min_retry_seconds
                    updates = update_state_with_readings(state, readings, poll_ts=now)
                    if getattr(args, "backfill_hours", DEFAULT_BACKFILL_HOURS) > 0:
                        maybe_periodic_backfill_check(state, now)
                    maybe_refresh_forecasts(state, args)
                    maybe_refresh_nwrfc(state, args)
                    maybe_publish_community_samples(state, args, updates, now)
                    save_state(state_path, state)
                    next_poll_at = schedule_next_poll(
                        state,
                        datetime.now(timezone.utc),
                        args.min_retry_seconds,
                    )
                    status_msg = f"Fetched at {_fmt_clock(now)}; next {_fmt_rel(now, next_poll_at)}"
                    state["meta"]["last_success_at"] = now.isoformat()
                    state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                    save_state(state_path, state)
                    if update_alert and any(updates.values()):
                        try:
                            curses.flash()
                        except Exception:
                            pass
                        try:
                            curses.beep()
                        except Exception:
                            pass
                else:
                    fetch_err = state.get("meta", {}).get("last_fetch_error")
                    if isinstance(fetch_err, str) and fetch_err:
                        status_msg = f"Fetch failed: {fetch_err} (backing off)."
                    else:
                        status_msg = "Fetch failed; backing off."
                    retry_wait = min(args.max_retry_seconds, retry_wait * 2)
                    next_poll_at = now + timedelta(seconds=retry_wait)
                    state["meta"]["last_failure_at"] = now.isoformat()
                    state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                    save_state(state_path, state)

            if bool(state.get("meta", {}).get("nearby_enabled")):
                loc = refresh_user_location_web(state)
                if loc is not None:
                    maybe_discover_nearby_gauges(
                        state,
                        now,
                        float(loc[0]),
                        float(loc[1]),
                        n=3,
                    )
                    refresh_gauges()

            draw_screen(
                stdscr,
                curses,
                gauges,
                divider_index,
                readings,
                state,
                selected_idx,
                chart_metric,
                status_msg,
                next_poll_at,
                palette,
                detail_mode,
                TUI_TABLE_START,
                args.state_file,
                update_alert,
            )

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return 0
            # Synthetic click/tap support from the web shim:
            # keys in the range [3000, 4000) mean "click on row N".
            if 3000 <= key < 4000:
                clicked_row = key - 3000
                max_y, _max_x = stdscr.getmaxyx()
                footer_y = max_y - 2
                toggle_row = footer_y - 1
                if clicked_row == toggle_row:
                    status_msg = toggle_nearby(state, args)
                    refresh_gauges()
                    save_state(state_path, state)
                    continue
                first_gauge_row = TUI_TABLE_START + 1
                rel = clicked_row - first_gauge_row
                has_divider = isinstance(divider_index, int) and 0 < divider_index < len(gauges)
                if has_divider and rel == divider_index:
                    continue  # divider line
                if has_divider and rel > divider_index:
                    rel -= 1
                if 0 <= rel < len(gauges):
                    selected_idx, detail_mode, status_msg = handle_row_click(
                        rel, selected_idx, detail_mode, gauges
                    )
                continue

            if key in (curses.KEY_UP, ord("k")):
                selected_idx = (selected_idx - 1) % len(gauges)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected_idx = (selected_idx + 1) % len(gauges)
            elif key in (curses.KEY_ENTER, 10, 13):
                detail_mode = not detail_mode
            elif key in (ord("c"), ord("C")):
                chart_metric = "flow" if chart_metric == "stage" else "stage"
                status_msg = f"Chart metric: {chart_metric}"
            elif key in (ord("n"), ord("N")):
                status_msg = toggle_nearby(state, args)
                refresh_gauges()
                save_state(state_path, state)
            elif key in (ord("r"), ord("R"), ord("f"), ord("F")):
                next_poll_at = datetime.now(timezone.utc)
                if key in (ord("f"), ord("F")):
                    status_msg = "Forced refetch requested..."
                else:
                    status_msg = "Manual refresh requested..."

        return 0

    state_path = Path(args.state_file)
    try:
        with state_lock(state_path):
            return curses.wrapper(run)
    except StateLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


async def web_tui_main(argv: list[str] | None = None) -> int:
    """
    Async-friendly TUI driver for Pyodide/browser builds.

    This mirrors `tui_loop` but yields to the JS event loop via
    `await asyncio.sleep(...)` so mobile Safari remains responsive.
    """
    import asyncio

    args = parse_args(argv)
    try:
        import curses
    except Exception as exc:
        print("Curses backend is unavailable.", file=sys.stderr)
        return 1

    gauges = ordered_gauges()
    TUI_TABLE_START = 3

    stdscr = curses.initscr()
    stdscr.nodelay(True)
    stdscr.timeout(0)

    palette: Dict[str, int] = {"normal": 0, "title": 0, "dim": 0, "chart": 0}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        palette.update(
            {
                "normal": curses.color_pair(1),
                "action": curses.color_pair(2),
                "minor": curses.color_pair(2),
                "moderate": curses.color_pair(3),
                "major": curses.color_pair(3) | curses.A_BOLD,
                "title": curses.color_pair(1) | curses.A_BOLD,
                "dim": curses.color_pair(4),
                "chart": curses.color_pair(4),
            }
        )

    state_path = Path(args.state_file)
    try:
        with state_lock(state_path):
            state = load_state(state_path)
            meta = state.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["api_backend"] = getattr(args, "usgs_backend", "blended")
            apply_dynamic_sites_from_state(state)
            maybe_backfill_state(state, args.backfill_hours)
            maybe_refresh_community(state, args)
            seed_user_location_from_args(state, args)
            gauges, divider_index = compute_table_gauges(state)
            save_state(state_path, state)
            readings: Dict[str, Dict[str, Any]] = {}
            selected_idx = 0
            chart_metric = args.chart_metric
            status_msg = "Awaiting first fetch..."
            next_poll_at = datetime.now(timezone.utc)
            retry_wait = args.min_retry_seconds
            detail_mode = False
            update_alert = getattr(args, "update_alert", True)

            ui_tick = getattr(args, "ui_tick_sec", UI_TICK_SEC)
            if not isinstance(ui_tick, (int, float)) or ui_tick <= 0:
                ui_tick = UI_TICK_SEC

            def refresh_gauges() -> None:
                nonlocal gauges, divider_index, selected_idx
                selected_id = None
                if gauges and 0 <= selected_idx < len(gauges):
                    selected_id = gauges[selected_idx]
                new_gauges, new_divider = compute_table_gauges(state)
                if new_gauges == gauges and new_divider == divider_index:
                    return
                gauges = new_gauges
                divider_index = new_divider
                if selected_id is not None and selected_id in gauges:
                    selected_idx = gauges.index(selected_id)
                elif gauges:
                    selected_idx = min(selected_idx, len(gauges) - 1)
                else:
                    selected_idx = 0

            while True:
                now = datetime.now(timezone.utc)
                if now >= next_poll_at:
                    maybe_refresh_community(state, args)
                    state.setdefault("meta", {})["last_fetch_at"] = now.isoformat()
                    fetched = fetch_gauge_data(state)
                    if fetched:
                        readings = fetched
                        retry_wait = args.min_retry_seconds
                        updates = update_state_with_readings(state, readings, poll_ts=now)
                        maybe_refresh_forecasts(state, args)
                        maybe_refresh_nwrfc(state, args)
                        await maybe_publish_community_samples_async(state, args, updates, now)
                        save_state(state_path, state)
                        next_poll_at = schedule_next_poll(
                            state,
                            datetime.now(timezone.utc),
                            args.min_retry_seconds,
                        )
                        status_msg = f"Fetched at {_fmt_clock(now)}; next {_fmt_rel(now, next_poll_at)}"
                        state["meta"]["last_success_at"] = now.isoformat()
                        state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                        save_state(state_path, state)
                        if update_alert and any(updates.values()):
                            try:
                                curses.flash()
                            except Exception:
                                pass
                            try:
                                curses.beep()
                            except Exception:
                                pass
                    else:
                        status_msg = "Fetch failed; backing off."
                    retry_wait = min(args.max_retry_seconds, retry_wait * 2)
                    next_poll_at = now + timedelta(seconds=retry_wait)
                    state["meta"]["last_failure_at"] = now.isoformat()
                    state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                    save_state(state_path, state)

                if bool(state.get("meta", {}).get("nearby_enabled")):
                    loc = refresh_user_location_web(state)
                    if loc is not None:
                        maybe_discover_nearby_gauges(
                            state,
                            now,
                            float(loc[0]),
                            float(loc[1]),
                            n=3,
                        )
                        refresh_gauges()

                draw_screen(
                    stdscr,
                    curses,
                    gauges,
                    divider_index,
                    readings,
                    state,
                    selected_idx,
                    chart_metric,
                    status_msg,
                    next_poll_at,
                    palette,
                    detail_mode,
                    TUI_TABLE_START,
                    args.state_file,
                    update_alert,
                )

                key = stdscr.getch()
                if key in (ord("q"), ord("Q")):
                    return 0
                if 3000 <= key < 4000:
                    clicked_row = key - 3000
                    max_y, _max_x = stdscr.getmaxyx()
                    footer_y = max_y - 2
                    toggle_row = footer_y - 1
                    if clicked_row == toggle_row:
                        status_msg = toggle_nearby(state, args)
                        refresh_gauges()
                        save_state(state_path, state)
                        await asyncio.sleep(0)
                        continue
                    first_gauge_row = TUI_TABLE_START + 1
                    rel = clicked_row - first_gauge_row
                    has_divider = isinstance(divider_index, int) and 0 < divider_index < len(gauges)
                    if has_divider and rel == divider_index:
                        await asyncio.sleep(0)
                        continue  # divider line
                    if has_divider and rel > divider_index:
                        rel -= 1
                    if 0 <= rel < len(gauges):
                        selected_idx, detail_mode, status_msg = handle_row_click(
                            rel, selected_idx, detail_mode, gauges
                        )
                    await asyncio.sleep(0)
                    continue

                if key in (curses.KEY_UP, ord("k")):
                    selected_idx = (selected_idx - 1) % len(gauges)
                elif key in (curses.KEY_DOWN, ord("j")):
                    selected_idx = (selected_idx + 1) % len(gauges)
                elif key in (curses.KEY_ENTER, 10, 13):
                    detail_mode = not detail_mode
                elif key in (ord("c"), ord("C")):
                    chart_metric = "flow" if chart_metric == "stage" else "stage"
                    status_msg = f"Chart metric: {chart_metric}"
                elif key in (ord("b"), ord("B")):
                    update_alert = not update_alert
                    status_msg = f"Alerts: {'on' if update_alert else 'off'}"
                elif key in (ord("n"), ord("N")):
                    status_msg = toggle_nearby(state, args)
                    refresh_gauges()
                    save_state(state_path, state)
                elif key in (ord("r"), ord("R"), ord("f"), ord("F")):
                    next_poll_at = datetime.now(timezone.utc)
                    if key in (ord("f"), ord("F")):
                        status_msg = "Forced refetch requested..."
                    else:
                        status_msg = "Manual refresh requested..."

                await asyncio.sleep(ui_tick)
    except StateLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def adaptive_loop(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file)
    try:
        with state_lock(state_path):
            state = load_state(state_path)
            meta = state.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["api_backend"] = getattr(args, "usgs_backend", "blended")
            apply_dynamic_sites_from_state(state)
            maybe_backfill_state(state, args.backfill_hours)
            maybe_refresh_community(state, args)
            save_state(state_path, state)
            retry_wait = args.min_retry_seconds
            next_poll_at: datetime | None = None

            while True:
                now = datetime.now(timezone.utc)
                if next_poll_at and next_poll_at > now:
                    sleep_for = max(0.0, (next_poll_at - now).total_seconds())
                    if sleep_for:
                        time.sleep(sleep_for)
                    now = datetime.now(timezone.utc)

                maybe_refresh_community(state, args)
                state.setdefault("meta", {})["last_fetch_at"] = now.isoformat()
                readings = fetch_gauge_data(state)
                if not readings:
                    time.sleep(min(args.max_retry_seconds, retry_wait))
                    retry_wait = min(args.max_retry_seconds, retry_wait * 2)
                    next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=retry_wait)
                    state["meta"]["last_failure_at"] = datetime.now(timezone.utc).isoformat()
                    state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                    save_state(state_path, state)
                    continue

                retry_wait = args.min_retry_seconds
                updates = update_state_with_readings(state, readings, poll_ts=now)
                if getattr(args, "backfill_hours", DEFAULT_BACKFILL_HOURS) > 0:
                    maybe_periodic_backfill_check(state, now)
                maybe_refresh_forecasts(state, args)
                maybe_publish_community_samples(state, args, updates, now)
                save_state(state_path, state)

                if next_poll_at is None or any(updates.values()):
                    render_table(readings, state)
                else:
                    # We were early; gently widen the interval and try again soon.
                    for g_state in state.get("gauges", {}).values():
                        if "mean_interval_sec" in g_state:
                            g_state["mean_interval_sec"] *= 1.05
                    save_state(state_path, state)
                    next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=args.min_retry_seconds)
                    continue

                now = datetime.now(timezone.utc)
                next_poll_at = schedule_next_poll(
                    state,
                    now,
                    args.min_retry_seconds,
                )
                state["meta"]["last_success_at"] = now.isoformat()
                state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                save_state(state_path, state)
                if getattr(args, "debug", False):
                    try:
                        print(control_summary(state, now), file=sys.stderr)
                    except Exception:
                        pass
    except StateLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Snoqualmie River USGS gauge watcher.")
    parser.add_argument(
        "--mode",
        choices=["once", "adaptive", "tui"],
        default="once",
        help="Run once, adaptively learn update cadence, or launch the TUI.",
    )
    parser.add_argument(
        "--state-file",
        default=str(STATE_FILE_DEFAULT),
        help="Path to persist observed update cadence and last timestamps.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Emit scheduler/control debug summaries to stderr.",
    )
    parser.add_argument(
        "--min-retry-seconds",
        type=int,
        default=MIN_RETRY_SEC,
        help="Minimum retry delay when we polled before an update arrived.",
    )
    parser.add_argument(
        "--max-retry-seconds",
        type=int,
        default=MAX_RETRY_SEC,
        help="Maximum retry delay when backing off.",
    )
    parser.add_argument(
        "--forecast-base",
        default="",
        help=(
            "URL template for NOAA/NWPS forecast API. "
            "May contain {gauge_id} and {site_no} placeholders; "
            "if empty, forecast integration is disabled."
        ),
    )
    parser.add_argument(
        "--forecast-hours",
        type=int,
        default=72,
        help="Forecast horizon (hours) to consider when summarizing peaks if forecast is enabled.",
    )
    parser.add_argument(
        "--backfill-hours",
        type=int,
        default=DEFAULT_BACKFILL_HOURS,
        help=(
            "On start, backfill this many hours of recent history from USGS IV "
            f"(default {DEFAULT_BACKFILL_HOURS}; 0 to disable)."
        ),
    )
    parser.add_argument(
        "--community-base",
        default="",
        help=(
            "Optional base URL for shared cadence/latency priors. "
            "If set, streamvis will GET {base}/summary.json at most once per day."
        ),
    )
    parser.add_argument(
        "--community-publish",
        action="store_true",
        help="Publish observed update/latency samples to {community-base}/sample (native only).",
    )
    parser.add_argument(
        "--user-lat",
        type=float,
        default=None,
        help="Optional user latitude for Nearby gauges (native TUI).",
    )
    parser.add_argument(
        "--user-lon",
        type=float,
        default=None,
        help="Optional user longitude for Nearby gauges (native TUI).",
    )
    parser.add_argument(
        "--chart-metric",
        choices=["stage", "flow"],
        default="stage",
        help="Metric to chart in TUI mode.",
    )
    parser.add_argument(
        "--ui-tick-sec",
        type=float,
        default=UI_TICK_SEC,
        help="UI refresh tick in TUI mode (seconds).",
    )
    parser.add_argument(
        "--nwrfc-text",
        action="store_true",
        help=(
            "Enable NW RFC textPlot cross-check for supported gauges "
            "(currently GARW1) to compare observed stage/flow against USGS."
        ),
    )
    parser.add_argument(
        "--no-update-alert",
        dest="update_alert",
        action="store_false",
        help="Disable bell/flash when new data is fetched in TUI mode.",
    )
    parser.add_argument(
        "--usgs-backend",
        choices=["blended", "waterservices", "ogc"],
        default="blended",
        help=(
            "USGS API backend selection: 'blended' (default) fetches from both APIs "
            "and learns which is faster, 'waterservices' uses legacy API only, "
            "'ogc' uses new OGC API only."
        ),
    )
    parser.set_defaults(update_alert=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.mode == "tui":
        return tui_loop(args) or 0

    if args.mode == "adaptive":
        return adaptive_loop(args) or 0

    state_path = Path(args.state_file)
    try:
        with state_lock(state_path):
            state = load_state(state_path)
            meta = state.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["api_backend"] = getattr(args, "usgs_backend", "blended")
            apply_dynamic_sites_from_state(state)
            maybe_backfill_state(state, args.backfill_hours)
            maybe_refresh_community(state, args)
            save_state(state_path, state)

            data = fetch_gauge_data(state)
            if not data:
                print("No data available from USGS Instantaneous Values service.", file=sys.stderr)
                return 1

            now = datetime.now(timezone.utc)
            updates = update_state_with_readings(state, data, poll_ts=now)
            maybe_refresh_forecasts(state, args)
            maybe_refresh_nwrfc(state, args)
            maybe_publish_community_samples(state, args, updates, now)
            save_state(state_path, state)
            if getattr(args, "debug", False):
                try:
                    print(control_summary(state, datetime.now(timezone.utc)), file=sys.stderr)
                except Exception:
                    pass
            render_table(data, state)
    except StateLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
