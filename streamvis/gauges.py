"""
Gauge utilities for streamvis.

Functions for:
- Classifying flood status from stage readings
- Finding nearest gauges to a location
- Station display names
- RDB parsing for USGS site discovery
"""

from __future__ import annotations

from typing import Any

from streamvis.config import FLOOD_THRESHOLDS, STATION_LOCATIONS, SITE_MAP
from streamvis.utils import haversine_miles


def classify_status(gauge_id: str, stage_ft: float | None) -> str:
    """Return NORMAL / ACTION / MINOR FLOOD / MOD FLOOD / MAJOR FLOOD."""
    thr = FLOOD_THRESHOLDS.get(gauge_id) or {}
    a = thr.get("action")
    n = thr.get("minor")
    m = thr.get("moderate")
    j = thr.get("major")

    # If we don't have thresholds, just say NORMAL.
    if stage_ft is None or all(t is None for t in (a, n, m, j)):
        return "NORMAL"

    if j is not None and stage_ft >= j:
        return "MAJOR FLOOD"
    if m is not None and stage_ft >= m:
        return "MOD FLOOD"
    if n is not None and stage_ft >= n:
        return "MINOR FLOOD"
    if a is not None and stage_ft >= a:
        return "ACTION"
    return "NORMAL"


def nearest_gauges(
    user_lat: float,
    user_lon: float,
    n: int = 3,
) -> list[tuple[str, float]]:
    """
    Return the n nearest gauges to the given user location.

    Returns a list of (gauge_id, distance_miles) sorted nearest-first.
    """
    distances: list[tuple[str, float]] = []
    for gauge_id, (lat, lon) in STATION_LOCATIONS.items():
        dist = haversine_miles(user_lat, user_lon, lat, lon)
        distances.append((gauge_id, dist))
    distances.sort(key=lambda x: x[1])
    return distances[:n]


def station_display_name(gauge_id: str, state: dict[str, Any] | None = None) -> str:
    """
    Return a human-readable display name for a gauge.
    
    Checks dynamic sites in state first, then falls back to gauge_id.
    """
    if state is not None:
        meta = state.get("meta", {})
        if isinstance(meta, dict):
            dyn_sites = meta.get("dynamic_sites", {})
            if isinstance(dyn_sites, dict):
                site = dyn_sites.get(gauge_id)
                if isinstance(site, dict):
                    name = site.get("station_nm") or site.get("name")
                    if isinstance(name, str) and name:
                        return name
    
    # Fallback: use gauge_id as display name
    return gauge_id


def parse_usgs_site_rdb(text: str) -> list[dict[str, Any]]:
    """
    Parse USGS NWIS site-service RDB into a list of station dicts.

    RDB is a tab-delimited format with:
      - comment lines starting with '#'
      - header row of column names
      - type row
      - data rows
    """
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
    for ln in lines[2:]:  # Skip header and type row
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


def dynamic_gauge_id(site_no: str, existing_ids: list[str]) -> str:
    """
    Derive a short, stable gauge_id for a USGS site_no, avoiding collisions.
    """
    from streamvis.constants import DYNAMIC_GAUGE_PREFIX
    
    # Try to create a unique ID from the site number
    base_id = f"{DYNAMIC_GAUGE_PREFIX}{site_no[-5:]}" if len(site_no) >= 5 else f"{DYNAMIC_GAUGE_PREFIX}{site_no}"
    
    if base_id not in existing_ids:
        return base_id
    
    # Handle collision by appending a suffix
    for suffix in range(2, 100):
        candidate = f"{base_id}{suffix}"
        if candidate not in existing_ids:
            return candidate
    
    # Fallback: use full site number
    return f"{DYNAMIC_GAUGE_PREFIX}{site_no}"
