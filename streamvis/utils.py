"""
Streamvis utility functions.

Pure functions for timestamp parsing, formatting, statistics, and geometry.
No side effects, no state access.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List

from streamvis.constants import BIWEIGHT_LOC_C, BIWEIGHT_SCALE_C, BIWEIGHT_MAX_ITERS


def parse_timestamp(ts: str | None) -> datetime | None:
    """
    Parse an ISO8601 timestamp to a UTC-aware datetime.
    
    Handles both 'Z' suffix and numeric timezone offsets.
    Returns None if parsing fails.
    """
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def fmt_clock(dt: datetime | None, with_date: bool = False) -> str:
    """Format a datetime as local clock time (HH:MM:SS or full with date)."""
    if dt is None:
        return "-"
    local_dt = dt.astimezone()
    if with_date:
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    return local_dt.strftime("%H:%M:%S")


def fmt_rel(now: datetime, target: datetime | None) -> str:
    """Format a relative time as 'in Xm' or 'ago Xs'."""
    if target is None:
        return "unknown"
    delta = (target - now).total_seconds()
    if abs(delta) < 1:
        return "now"
    suffix = "ago" if delta < 0 else "in"
    delta = abs(delta)
    if delta < 60:
        val = int(delta)
        unit = "s"
    elif delta < 3600:
        val = int(delta // 60)
        unit = "m"
    else:
        val = int(delta // 3600)
        unit = "h"
    return f"{suffix} {val}{unit}"


def parse_nwrfc_timestamp(
    date_str: str, time_str: str, tz_label: str | None
) -> datetime | None:
    """
    Parse NW RFC local timestamp (e.g., '2025-12-08' '19:00' 'PST') to UTC.
    """
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except Exception:
        return None
    tz_label = (tz_label or "").upper()
    offset = -7 if tz_label == "PDT" else -8
    local = dt.replace(tzinfo=timezone(timedelta(hours=offset)))
    return local.astimezone(timezone.utc)


def ewma(current_mean: float, new_value: float, alpha: float = 0.30) -> float:
    """Exponentially Weighted Moving Average update."""
    if current_mean <= 0:
        return new_value
    return (1 - alpha) * current_mean + alpha * new_value


def ewma_variance(
    current_var: float, current_mean: float, new_value: float, alpha: float = 0.10
) -> float:
    """Update EWMA of variance given new sample."""
    if current_var < 0:
        current_var = 0.0
    diff = new_value - current_mean
    return (1 - alpha) * current_var + alpha * (diff * diff)


def iso8601_duration(seconds: float) -> str:
    """Render a duration as ISO8601 'PT..H..M..S' string."""
    total = int(max(0.0, float(seconds)))
    if total <= 0:
        return "PT0S"
    minutes, sec_rem = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if sec_rem and not parts:
        parts.append(f"{sec_rem}S")
    return "PT" + "".join(parts)


def median(values: List[float]) -> float:
    """Compute median of a list of floats."""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(vals[mid])
    return 0.5 * (float(vals[mid - 1]) + float(vals[mid]))


def mad(values: List[float], center: float) -> float:
    """Median Absolute Deviation."""
    devs = [abs(v - center) for v in values]
    return median(devs) if devs else 0.0


def tukey_biweight_location_scale(
    values: List[float],
    initial_loc: float,
    initial_scale: float,
    c_loc: float = BIWEIGHT_LOC_C,
    c_scale: float = BIWEIGHT_SCALE_C,
    max_iters: int = BIWEIGHT_MAX_ITERS,
) -> tuple[float, float]:
    """
    Tukey's biweight (bisquare) robust location and scale estimator.
    
    Returns (location, scale) tuple. Robust to outliers.
    """
    clean = [
        float(v) for v in values
        if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0
    ]
    if not clean:
        return float(initial_loc), float(max(0.0, initial_scale))

    loc = float(initial_loc)
    scale = float(max(initial_scale, 1e-6))

    # Iterative biweight location
    for _ in range(max(1, int(max_iters))):
        denom = c_loc * scale
        if denom <= 0:
            break
        num = 0.0
        den = 0.0
        for v in clean:
            u = (v - loc) / denom
            if abs(u) >= 1.0:
                continue
            w = (1.0 - u * u) ** 2
            num += (v - loc) * w
            den += w
        if den <= 1e-12:
            break
        delta = num / den
        if abs(delta) < 1e-3:
            loc += delta
            break
        loc += delta

    # Biweight scale (midvariance)
    denom = c_scale * scale
    if denom <= 0:
        return loc, 0.0
    num = 0.0
    den = 0.0
    for v in clean:
        u = (v - loc) / denom
        if abs(u) >= 1.0:
            continue
        one_minus = 1.0 - u * u
        num += (v - loc) ** 2 * (one_minus ** 4)
        den += one_minus * (1.0 - 5.0 * u * u)
    den = abs(den)
    if den <= 1e-12:
        return loc, 0.0
    scale_bi = math.sqrt(len(clean) * num) / den
    return loc, float(max(scale_bi, 0.0))


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    r_miles = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    return r_miles * c


def bbox_for_radius(
    lat: float, lon: float, radius_miles: float
) -> tuple[float, float, float, float]:
    """Compute bounding box (west, south, east, north) for a radius."""
    lat_deg = radius_miles / 69.0
    lon_deg = radius_miles / (69.0 * max(0.2, math.cos(math.radians(lat))))
    return lon - lon_deg, lat - lat_deg, lon + lon_deg, lat + lat_deg


def coerce_float(val) -> float | None:
    """Safely coerce a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def compute_modified_since(state: dict) -> str | None:
    """
    Compute a safe modifiedSince window for a batched IV request.

    USGS IV `modifiedSince` is an ISO8601 duration, filtering to stations with
    values that have changed within that recent window. We only enable it when
    all tracked gauges are on <= 1 hour cadences; otherwise a narrow window could
    suppress legitimate older updates for slow gauges.
    """
    window_sec = compute_modified_since_sec(state)
    if window_sec is None:
        return None
    return iso8601_duration(window_sec)


def compute_modified_since_sec(state: dict) -> float | None:
    """
    Compute a safe modifiedSince window in seconds.

    This is the numeric form used internally by the dual-backend USGS adapter;
    WaterServices expects an ISO8601 duration string (see compute_modified_since()).
    """
    gauges_state = state.get("gauges", {})
    if not isinstance(gauges_state, dict):
        return None
    intervals: list[float] = []
    for g_state in gauges_state.values():
        if not isinstance(g_state, dict):
            continue
        mi = g_state.get("mean_interval_sec")
        if isinstance(mi, (int, float)) and mi > 0:
            intervals.append(float(mi))
    if not intervals:
        return None
    max_interval = max(intervals)
    min_interval = min(intervals)
    if max_interval > 3600.0:
        return None
    window_sec = max(2.0 * min_interval, 30.0 * 60.0)
    return float(window_sec)
