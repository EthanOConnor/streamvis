"""
USGS WaterServices IV API client.

Legacy API (retiring EOY 2025) for fetching instantaneous values
from USGS NWIS.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from http_client import get_json, get_text

from streamvis.constants import (
    DEFAULT_USGS_IV_URL,
    DEFAULT_USGS_SITE_URL,
)
from streamvis.utils import parse_timestamp, iso8601_duration


def fetch_latest(
    site_map: dict[str, str],
    modified_since_sec: float | None = None,
    timeout: float = 5.0,
) -> tuple[dict[str, dict[str, Any]], float]:
    """
    Fetch latest IV readings from USGS WaterServices.
    
    Args:
        site_map: Mapping of gauge_id -> USGS site_no
        modified_since_sec: If set, use modifiedSince filter (seconds)
        timeout: HTTP timeout in seconds
        
    Returns:
        Tuple of (readings dict, request_latency_ms)
        readings: {gauge_id: {"stage": float|None, "flow": float|None, "observed_at": datetime|None}}
    """
    if not site_map:
        return {}, 0.0
    
    # Prepare result skeleton
    result: dict[str, dict[str, Any]] = {
        g: {"stage": None, "flow": None, "observed_at": None}
        for g in site_map.keys()
    }
    
    params: dict[str, str] = {
        "format": "json",
        "sites": ",".join(site_map.values()),
        "parameterCd": "00060,00065",  # discharge, stage
        "siteStatus": "all",
    }
    if modified_since_sec is not None and modified_since_sec > 0:
        params["modifiedSince"] = iso8601_duration(modified_since_sec)
    
    start_ms = time.monotonic() * 1000
    try:
        payload = get_json(DEFAULT_USGS_IV_URL, params=params, timeout=timeout)
    except Exception:
        return {}, time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms
    
    # Reverse map: USGS site -> gauge ID
    site_to_gauge = {v: k for k, v in site_map.items()}
    
    ts_list = payload.get("value", {}).get("timeSeries", [])
    for ts in ts_list:
        try:
            site_no = ts["sourceInfo"]["siteCode"][0]["value"]
            param = ts["variable"]["variableCode"][0]["value"]
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue
            
            values = ts.get("values", [])
            if not values or not values[0].get("value"):
                continue
            
            last_point = values[0]["value"][-1]
            val = float(last_point["value"])
            ts_raw = last_point.get("dateTime")
            obs_at = parse_timestamp(ts_raw)
        except Exception:
            continue
        
        if param == "00060":  # discharge, cfs
            result[gauge_id]["flow"] = val
        elif param == "00065":  # gage height, ft
            result[gauge_id]["stage"] = val
        
        # Track freshest observation time
        current_obs = result[gauge_id].get("observed_at")
        if obs_at and (current_obs is None or obs_at > current_obs):
            result[gauge_id]["observed_at"] = obs_at
    
    return result, latency_ms


def fetch_history(
    site_map: dict[str, str],
    period_hours: int = 6,
    timeout: float = 10.0,
) -> tuple[dict[str, list[dict[str, Any]]], float]:
    """
    Fetch historical IV readings for backfill.
    
    Returns:
        Tuple of (history dict, request_latency_ms)
        history: {gauge_id: [{"ts": str, "stage": float|None, "flow": float|None}, ...]}
    """
    if not site_map:
        return {}, 0.0
    
    result: dict[str, list[dict[str, Any]]] = {g: [] for g in site_map.keys()}
    
    params = {
        "format": "json",
        "sites": ",".join(site_map.values()),
        "parameterCd": "00060,00065",
        "period": f"PT{period_hours}H",
        "siteStatus": "all",
    }
    
    start_ms = time.monotonic() * 1000
    try:
        payload = get_json(DEFAULT_USGS_IV_URL, params=params, timeout=timeout)
    except Exception:
        return result, time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms
    
    site_to_gauge = {v: k for k, v in site_map.items()}
    
    # Collect all values by (gauge_id, timestamp)
    points: dict[tuple[str, str], dict[str, Any]] = {}
    
    ts_list = payload.get("value", {}).get("timeSeries", [])
    for ts in ts_list:
        try:
            site_no = ts["sourceInfo"]["siteCode"][0]["value"]
            param = ts["variable"]["variableCode"][0]["value"]
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue
            
            values = ts.get("values", [])
            if not values:
                continue
            
            for v in values[0].get("value", []):
                ts_raw = v.get("dateTime", "")
                val = float(v.get("value", 0))
                
                key = (gauge_id, ts_raw)
                if key not in points:
                    points[key] = {"ts": ts_raw, "stage": None, "flow": None}
                
                if param == "00060":
                    points[key]["flow"] = val
                elif param == "00065":
                    points[key]["stage"] = val
        except Exception:
            continue
    
    # Group by gauge and sort by timestamp
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
    Fetch active USGS stream gauges near a location.
    
    Returns:
        Tuple of (sites list, request_latency_ms)
        sites: [{"site_no": str, "station_nm": str, "lat": float, "lon": float}, ...]
    """
    from streamvis.utils import bbox_for_radius
    
    west, south, east, north = bbox_for_radius(lat, lon, radius_miles)
    params = {
        "format": "rdb",
        "bBox": f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}",
        "siteStatus": "active",
        "hasDataTypeCd": "iv",
        "siteType": "ST",
        "parameterCd": "00060,00065",
    }
    
    start_ms = time.monotonic() * 1000
    try:
        text = get_text(DEFAULT_USGS_SITE_URL, params=params, timeout=timeout)
    except Exception:
        return [], time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms
    
    return _parse_rdb(text), latency_ms


def _parse_rdb(text: str) -> list[dict[str, Any]]:
    """Parse USGS RDB format into site dicts."""
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 3:
        return []
    
    header = lines[0].split("\t")
    idx = {name: i for i, name in enumerate(header)}
    required = ("site_no", "station_nm", "dec_lat_va", "dec_long_va")
    if not all(k in idx for k in required):
        return []
    
    sites: list[dict[str, Any]] = []
    for ln in lines[2:]:
        parts = ln.split("\t")
        if len(parts) < len(header):
            continue
        try:
            site_no = parts[idx["site_no"]].strip()
            name = parts[idx["station_nm"]].strip()
            lat = float(parts[idx["dec_lat_va"]])
            lon = float(parts[idx["dec_long_va"]])
        except Exception:
            continue
        if site_no:
            sites.append({
                "site_no": site_no,
                "station_nm": name or site_no,
                "lat": lat,
                "lon": lon,
            })
    return sites
