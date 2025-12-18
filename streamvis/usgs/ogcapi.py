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


def parse_latest_payload(
    payload: dict[str, Any] | None,
    site_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """
    Parse an OGC API latest-continuous GeoJSON payload into gauge readings.

    Returns:
        {gauge_id: {"stage": float|None, "flow": float|None, "observed_at": datetime|None}}
    """
    if not site_map:
        return {}

    result: dict[str, dict[str, Any]] = {
        g: {"stage": None, "flow": None, "observed_at": None} for g in site_map.keys()
    }
    if not isinstance(payload, dict):
        return result

    site_to_gauge = {v: k for k, v in site_map.items()}
    features = payload.get("features", [])
    if not isinstance(features, list):
        return result

    for feature in features:
        try:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties", {})
            if not isinstance(props, dict):
                continue

            loc_id = props.get("monitoringLocationId", "")
            if isinstance(loc_id, str) and loc_id.startswith("USGS-"):
                site_no = loc_id[5:]
            else:
                site_no = str(loc_id)

            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue

            param_code = props.get("parameterCode", "")
            value = props.get("value")
            if value is None:
                continue
            val = float(value)

            time_str = props.get("phenomenonTime")
            obs_at = parse_timestamp(time_str if isinstance(time_str, str) else None)
        except Exception:
            continue

        if param_code == "00060":  # discharge, cfs
            result[gauge_id]["flow"] = val
        elif param_code == "00065":  # gage height, ft
            result[gauge_id]["stage"] = val

        current_obs = result[gauge_id].get("observed_at")
        if obs_at and (current_obs is None or obs_at > current_obs):
            result[gauge_id]["observed_at"] = obs_at

    return result


def parse_history_payload(
    payload: dict[str, Any] | None,
    site_map: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """
    Parse an OGC API continuous GeoJSON payload into per-gauge history points.

    Returns:
        {gauge_id: [{"ts": str, "stage": float|None, "flow": float|None}, ...]}
    """
    if not site_map:
        return {}

    result: dict[str, list[dict[str, Any]]] = {g: [] for g in site_map.keys()}
    if not isinstance(payload, dict):
        return result

    site_to_gauge = {v: k for k, v in site_map.items()}
    points: dict[tuple[str, str], dict[str, Any]] = {}

    features = payload.get("features", [])
    if not isinstance(features, list):
        return result

    for feature in features:
        try:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties", {})
            if not isinstance(props, dict):
                continue

            loc_id = props.get("monitoringLocationId", "")
            if isinstance(loc_id, str) and loc_id.startswith("USGS-"):
                site_no = loc_id[5:]
            else:
                site_no = str(loc_id)
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue

            param_code = props.get("parameterCode", "")
            value = props.get("value")
            if value is None:
                continue
            val = float(value)

            time_str = props.get("phenomenonTime", "")
            if not isinstance(time_str, str) or not time_str:
                continue

            key = (gauge_id, time_str)
            if key not in points:
                points[key] = {"ts": time_str, "stage": None, "flow": None}

            if param_code == "00060":
                points[key]["flow"] = val
            elif param_code == "00065":
                points[key]["stage"] = val
        except Exception:
            continue

    for (gauge_id, _), point in points.items():
        result[gauge_id].append(point)
    for gauge_id in result:
        result[gauge_id].sort(key=lambda p: p.get("ts", ""))
    return result


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
    
    # OGC API parameters
    params: dict[str, str] = {
        "f": "json",
        "monitoringLocationId": ",".join(monitoring_ids),
        "parameterCode": "00060,00065",  # discharge, stage
        "limit": str(len(site_nos) * 2 + 10),  # Enough for both params per site
    }
    
    start_ms = time.monotonic() * 1000
    payload = get_json(OGC_LATEST_CONTINUOUS, params=params, timeout=timeout)
    latency_ms = time.monotonic() * 1000 - start_ms

    readings = parse_latest_payload(payload, site_map)
    return readings, latency_ms


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
        return {g: [] for g in site_map.keys()}, time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms

    history = parse_history_payload(payload, site_map)
    return history, latency_ms


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
