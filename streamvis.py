#!/usr/bin/env python3
"""
Backward-compatible streamvis.py shim.

This file maintains backward compatibility for:
- Direct execution: python streamvis.py
- Old-style imports: from streamvis import ...
- pip install entry point

All functionality is now in the streamvis package.
"""

from __future__ import annotations

# Re-export public API from the package
from streamvis.tui import *  # noqa: F401, F403

# Explicitly re-export functions used by tests
from streamvis.tui import (
    _parse_usgs_site_rdb,
    _dynamic_gauge_id,
    _iso8601_duration,
    _compute_modified_since,
    _estimate_cadence_multiple,
    _estimate_phase_offset_sec,
    _snap_delta_to_cadence,
    _parse_timestamp,
    _fmt_clock,
    _fmt_rel,
    _ewma,
    _median,
    _mad,
    _haversine_miles,
    _cleanup_state,
    _slim_state_for_browser,
    _coerce_float,
)

if __name__ == "__main__":
    from streamvis.tui import main
    raise SystemExit(main())


