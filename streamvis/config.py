"""
Configuration loading for streamvis.

Loads config.toml and provides site map, station locations, and other
user-configurable values. Falls back to built-in defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Config file path
CONFIG_PATH = Path(__file__).parent.parent / "config.toml"


def _parse_toml_value(raw: str) -> Any:
    """
    Minimal TOML value parser for the subset used in config.toml.
    Supports strings, integers, floats, and booleans.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] == raw[-1] == '"':
        inner = raw[1:-1]
        return inner.replace(r"\\", "\\").replace(r"\"", '"').replace(r"\n", "\n")
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if any(ch in raw for ch in (".", "e", "E")):
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def load_toml_config(path: Path) -> dict[str, Any]:
    """
    Minimal, dependency-free TOML loader tailored to this project's config.toml.
    It understands:
      - Comment lines starting with '#'
      - Section headers like [section] or [a.b]
      - Simple key = value pairs where value is a scalar.
    Any parse error results in an empty config so the runtime can fall back
    to built-in defaults.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}

    root: dict[str, Any] = {}
    current: dict[str, Any] = root

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                current = root
                continue
            parts = [p.strip() for p in section.split(".") if p.strip()]
            current = root
            for part in parts:
                child = current.setdefault(part, {})
                if not isinstance(child, dict):
                    child = {}
                current = child
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        current[key] = _parse_toml_value(value)

    return root


# Load configuration
CONFIG: dict[str, Any] = load_toml_config(CONFIG_PATH)


# --- Site map extraction ---

DEFAULT_SITE_MAP: dict[str, str] = {
    "TANW1": "12141300",  # Middle Fork Snoqualmie R near Tanner
    "GARW1": "12143400",  # SF Snoqualmie R ab Alice Cr nr Garcia
    "EDGW1": "12143600",  # SF Snoqualmie R at Edgewick
    "SQUW1": "12144500",  # Snoqualmie R near Snoqualmie
    "CRNW1": "12149000",  # Snoqualmie R near Carnation
}


def _site_map_from_config(cfg: dict[str, Any]) -> dict[str, str]:
    stations = cfg.get("stations")
    if not isinstance(stations, dict):
        return {}
    site_map: dict[str, str] = {}
    for key, entry in stations.items():
        if not isinstance(entry, dict):
            continue
        gauge_id = entry.get("gauge_id") or key
        site_no = entry.get("usgs_site_no")
        if isinstance(gauge_id, str) and isinstance(site_no, str) and site_no:
            site_map[gauge_id] = site_no
    return site_map


def _usgs_iv_url_from_config(cfg: dict[str, Any]) -> str:
    from streamvis.constants import DEFAULT_USGS_IV_URL
    global_cfg = cfg.get("global")
    if isinstance(global_cfg, dict):
        usgs_cfg = global_cfg.get("usgs")
        if isinstance(usgs_cfg, dict):
            base = usgs_cfg.get("iv_base_url")
            if isinstance(base, str) and base:
                return base
    return DEFAULT_USGS_IV_URL


# USGS gauge IDs for the Snoqualmie system we care about.
SITE_MAP: dict[str, str] = _site_map_from_config(CONFIG) or DEFAULT_SITE_MAP

# Preferred ordering for gauges in CLI/TUI
PRIMARY_GAUGES: list[str] = ["TANW1", "GARW1", "EDGW1", "SQUW1", "CRNW1"]


def ordered_gauges() -> list[str]:
    """Return gauges in display order: primary first, then extras alphabetically."""
    primary = [g for g in PRIMARY_GAUGES if g in SITE_MAP]
    extras = [g for g in sorted(SITE_MAP.keys()) if g not in PRIMARY_GAUGES]
    return primary + extras


USGS_IV_URL = _usgs_iv_url_from_config(CONFIG)


# --- Station locations ---

DEFAULT_STATION_LOCATIONS: dict[str, tuple[float, float]] = {
    "TANW1": (47.485912, -121.647864),   # USGS 12141300
    "GARW1": (47.4151086, -121.5873213), # USGS 12143400
    "EDGW1": (47.4527778, -121.7166667), # USGS 12143600
    "SQUW1": (47.5451019, -121.8423360), # USGS 12144500
    "CRNW1": (47.6659340, -121.9253969), # USGS 12149000
    "CONW1": (48.5382169, -121.7489830), # USGS 12194000
}


def _station_locations_from_config(cfg: dict[str, Any]) -> dict[str, tuple[float, float]]:
    stations = cfg.get("stations")
    if not isinstance(stations, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for gauge_id, entry in stations.items():
        if not isinstance(entry, dict):
            continue
        lat_raw = entry.get("lat") or entry.get("latitude")
        lon_raw = entry.get("lon") or entry.get("longitude")
        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except Exception:
            continue
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            out[str(gauge_id)] = (lat, lon)
    return out


STATION_LOCATIONS: dict[str, tuple[float, float]] = {
    **DEFAULT_STATION_LOCATIONS,
    **_station_locations_from_config(CONFIG),
}


# --- Flood thresholds ---

FLOOD_THRESHOLDS: dict[str, dict[str, float | None]] = {
    "CRNW1": {"action": 50.7, "minor": 54.0, "moderate": 56.0, "major": 58.0},
    "SQUW1": {"action": 11.94, "minor": 13.54, "moderate": 16.21, "major": 17.42},
    "TANW1": {"action": None, "minor": None, "moderate": None, "major": None},
    "GARW1": {"action": None, "minor": None, "moderate": None, "major": None},
    "EDGW1": {"action": None, "minor": None, "moderate": None, "major": None},
}

NWRFC_ID_MAP: dict[str, str] = {
    "GARW1": "GARW1",
    "CONW1": "CONW1",
}
