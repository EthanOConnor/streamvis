"""
USGS API clients for streamvis.

Provides dual-stack access to USGS water data through:
- waterservices: Legacy WaterServices IV API (retiring EOY 2025)
- ogcapi: New OGC API–Features (recommended)
- adapter: Blended interface with latency learning
"""

from __future__ import annotations

from streamvis.usgs.adapter import (
    USGSBackend,
    fetch_gauge_data,
    fetch_gauge_history,
    fetch_sites_near,
)

__all__ = [
    "USGSBackend",
    "fetch_gauge_data",
    "fetch_gauge_history", 
    "fetch_sites_near",
]

