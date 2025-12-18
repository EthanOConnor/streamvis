"""
USGS WaterServices IV API client.

Legacy API (retiring EOY 2025) for fetching instantaneous values
from USGS NWIS.
"""

from __future__ import annotations

import time
from typing import Any

from http_client import get_json, get_text

from streamvis.constants import (
    DEFAULT_USGS_IV_URL,
    DEFAULT_USGS_SITE_URL,
)
from streamvis.utils import parse_timestamp, iso8601_duration


def parse_latest_payload(
    payload: dict[str, Any] | None,
    site_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """
    Parse a WaterServices IV JSON payload into normalized gauge readings.

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

    ts_list = payload.get("value", {}).get("timeSeries", [])
    if not isinstance(ts_list, list):
        return result

    for ts in ts_list:
        try:
            if not isinstance(ts, dict):
                continue
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

        current_obs = result[gauge_id].get("observed_at")
        if obs_at and (current_obs is None or obs_at > current_obs):
            result[gauge_id]["observed_at"] = obs_at

    return result


def parse_history_payload(
    payload: dict[str, Any] | None,
    site_map: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """
    Parse a WaterServices IV JSON payload into per-gauge history points.

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

    ts_list = payload.get("value", {}).get("timeSeries", [])
    if not isinstance(ts_list, list):
        return result

    for ts in ts_list:
        try:
            if not isinstance(ts, dict):
                continue
            site_no = ts["sourceInfo"]["siteCode"][0]["value"]
            param = ts["variable"]["variableCode"][0]["value"]
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue

            values = ts.get("values", [])
            if not values:
                continue
            series_values = values[0].get("value", [])
            if not isinstance(series_values, list):
                continue
        except Exception:
            continue

        for v in series_values:
            try:
                if not isinstance(v, dict):
                    continue
                ts_raw = v.get("dateTime", "")
                if not isinstance(ts_raw, str) or not ts_raw:
                    continue
                val = float(v.get("value", 0))
            except Exception:
                continue

            key = (gauge_id, ts_raw)
            if key not in points:
                points[key] = {"ts": ts_raw, "stage": None, "flow": None}
            if param == "00060":
                points[key]["flow"] = val
            elif param == "00065":
                points[key]["stage"] = val

    for (gauge_id, _), point in points.items():
        result[gauge_id].append(point)
    for gauge_id in result:
        result[gauge_id].sort(key=lambda p: p.get("ts", ""))
    return result


def fetch_latest(
    site_map: dict[str, str],
    modified_since_sec: float | None = None,
    timeout: float = 5.0,
    base_url: str = DEFAULT_USGS_IV_URL,
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
        payload = get_json(base_url, params=params, timeout=timeout)
    except Exception:
        return {}, time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms

    readings = parse_latest_payload(payload, site_map)
    return readings, latency_ms


def fetch_history(
    site_map: dict[str, str],
    period_hours: int = 6,
    timeout: float = 10.0,
    base_url: str = DEFAULT_USGS_IV_URL,
) -> tuple[dict[str, list[dict[str, Any]]], float]:
    """
    Fetch historical IV readings for backfill.
    
    Returns:
        Tuple of (history dict, request_latency_ms)
        history: {gauge_id: [{"ts": str, "stage": float|None, "flow": float|None}, ...]}
    """
    if not site_map:
        return {}, 0.0

    params = {
        "format": "json",
        "sites": ",".join(site_map.values()),
        "parameterCd": "00060,00065",
        "period": f"PT{period_hours}H",
        "siteStatus": "all",
    }
    
    start_ms = time.monotonic() * 1000
    try:
        payload = get_json(base_url, params=params, timeout=timeout)
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
    site_url: str = DEFAULT_USGS_SITE_URL,
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
        text = get_text(site_url, params=params, timeout=timeout)
    except Exception:
        return [], time.monotonic() * 1000 - start_ms
    latency_ms = time.monotonic() * 1000 - start_ms
    
    return parse_site_rdb(text), latency_ms


def parse_site_rdb(text: str) -> list[dict[str, Any]]:
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
