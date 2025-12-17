"""
USGS OGC API–Features client.

New API (recommended, WaterServices retiring EOY 2025) for fetching
instantaneous values from USGS water data.

API docs: https://api.waterdata.usgs.gov
Collections:
  - latest-continuous: Most recent IV reading per site
  - continuous: Historical IV data with datetime filter
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from http_client import get_json

from streamvis.constants import OGC_LATEST_CONTINUOUS, OGC_CONTINUOUS
from streamvis.utils import parse_timestamp


def fetch_latest(
    site_map: dict[str, str],
    timeout: float = 5.0,
) -> tuple[dict[str, dict[str, Any]], float]:
    """
    Fetch latest IV readings from USGS OGC API–Features.
    
    Args:
        site_map: Mapping of gauge_id -> USGS site_no
        timeout: HTTP timeout in seconds
        
    Returns:
        Tuple of (readings dict, request_latency_ms)
        readings: {gauge_id: {"stage": float|None, "flow": float|None, "observed_at": datetime|None}}
    """
    if not site_map:
        return {}, 0.0
    
    # OGC API uses USGS-prefixed monitoring location IDs
    site_nos = list(site_map.values())
    monitoring_ids = [f"USGS-{s}" for s in site_nos]
    
    # Result skeleton
    result: dict[str, dict[str, Any]] = {
        g: {"stage": None, "flow": None, "observed_at": None}
        for g in site_map.keys()
    }
    
    # OGC API parameters
    params: dict[str, str] = {
        "f": "json",
        "monitoringLocationId": ",".join(monitoring_ids),
        "parameterCode": "00060,00065",  # discharge, stage
        "limit": str(len(site_nos) * 2 + 10),  # Enough for both params per site
    }
    
    start_ms = time.monotonic() * 1000
    try:
        payload = get_json(OGC_LATEST_CONTINUOUS, params=params, timeout=timeout)
    except Exception:
        return {}, time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms
    
    # Reverse map: USGS site_no -> gauge_id
    site_to_gauge = {v: k for k, v in site_map.items()}
    
    # Parse GeoJSON features
    features = payload.get("features", [])
    for feature in features:
        try:
            props = feature.get("properties", {})
            
            # Extract site number from monitoringLocationId (e.g., "USGS-12141300")
            loc_id = props.get("monitoringLocationId", "")
            if loc_id.startswith("USGS-"):
                site_no = loc_id[5:]
            else:
                site_no = loc_id
            
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue
            
            # Get parameter code and value
            param_code = props.get("parameterCode", "")
            value = props.get("value")
            if value is None:
                continue
            val = float(value)
            
            # Get observation time
            time_str = props.get("phenomenonTime")
            obs_at = parse_timestamp(time_str)
            
            if param_code == "00060":  # discharge, cfs
                result[gauge_id]["flow"] = val
            elif param_code == "00065":  # gage height, ft
                result[gauge_id]["stage"] = val
            
            # Track freshest observation time
            current_obs = result[gauge_id].get("observed_at")
            if obs_at and (current_obs is None or obs_at > current_obs):
                result[gauge_id]["observed_at"] = obs_at
                
        except Exception:
            continue
    
    return result, latency_ms


def fetch_history(
    site_map: dict[str, str],
    start_time: datetime,
    end_time: datetime | None = None,
    timeout: float = 10.0,
) -> tuple[dict[str, list[dict[str, Any]]], float]:
    """
    Fetch historical IV readings from OGC API continuous collection.
    
    Args:
        site_map: Mapping of gauge_id -> USGS site_no
        start_time: Start of time range (UTC)
        end_time: End of time range (UTC), defaults to now
        timeout: HTTP timeout
        
    Returns:
        Tuple of (history dict, request_latency_ms)
        history: {gauge_id: [{"ts": str, "stage": float|None, "flow": float|None}, ...]}
    """
    if not site_map:
        return {}, 0.0
    
    if end_time is None:
        end_time = datetime.now(timezone.utc)
    
    site_nos = list(site_map.values())
    monitoring_ids = [f"USGS-{s}" for s in site_nos]
    
    result: dict[str, list[dict[str, Any]]] = {g: [] for g in site_map.keys()}
    
    # Format datetime range for OGC API
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    params: dict[str, str] = {
        "f": "json",
        "monitoringLocationId": ",".join(monitoring_ids),
        "parameterCode": "00060,00065",
        "datetime": f"{start_str}/{end_str}",
        "limit": "10000",  # High limit for historical data
    }
    
    start_ms = time.monotonic() * 1000
    try:
        payload = get_json(OGC_CONTINUOUS, params=params, timeout=timeout)
    except Exception:
        return result, time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms
    
    site_to_gauge = {v: k for k, v in site_map.items()}
    
    # Collect points by (gauge_id, timestamp)
    points: dict[tuple[str, str], dict[str, Any]] = {}
    
    features = payload.get("features", [])
    for feature in features:
        try:
            props = feature.get("properties", {})
            
            loc_id = props.get("monitoringLocationId", "")
            site_no = loc_id[5:] if loc_id.startswith("USGS-") else loc_id
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue
            
            param_code = props.get("parameterCode", "")
            value = props.get("value")
            if value is None:
                continue
            val = float(value)
            
            time_str = props.get("phenomenonTime", "")
            key = (gauge_id, time_str)
            
            if key not in points:
                points[key] = {"ts": time_str, "stage": None, "flow": None}
            
            if param_code == "00060":
                points[key]["flow"] = val
            elif param_code == "00065":
                points[key]["stage"] = val
                
        except Exception:
            continue
    
    # Group by gauge and sort
    for (gauge_id, _), point in points.items():
        result[gauge_id].append(point)
    
    for gauge_id in result:
        result[gauge_id].sort(key=lambda p: p.get("ts", ""))
    
    return result, latency_ms


def fetch_sites_near(
    lat: float,
    lon: float,
    radius_miles: float,
    timeout: float = 10.0,
) -> tuple[list[dict[str, Any]], float]:
    """
    Fetch USGS sites near a location using OGC API.
    
    Note: OGC API may have different site discovery endpoints.
    This is a stub that falls back to an empty result.
    The WaterServices API remains the primary source for site discovery.
    
    Returns:
        Tuple of (sites list, request_latency_ms)
    """
    # OGC API doesn't have a direct bbox site search in latest-continuous
    # For now, return empty - use waterservices for site discovery
    return [], 0.0
