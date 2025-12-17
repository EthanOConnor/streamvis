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

# Explicitly re-export functions used by tests (from extracted modules)
from streamvis.gauges import (
    parse_usgs_site_rdb as _parse_usgs_site_rdb,
    dynamic_gauge_id as _dynamic_gauge_id,
)
from streamvis.utils import (
    parse_timestamp as _parse_timestamp,
    fmt_clock as _fmt_clock,
    fmt_rel as _fmt_rel,
    ewma as _ewma,
    iso8601_duration as _iso8601_duration,
    median as _median,
    mad as _mad,
    haversine_miles as _haversine_miles,
    coerce_float as _coerce_float,
)
from streamvis.scheduler import (
    snap_delta_to_cadence as _snap_delta_to_cadence,
    estimate_cadence_multiple as _estimate_cadence_multiple,
    estimate_phase_offset_sec as _estimate_phase_offset_sec,
)
from streamvis.state import (
    cleanup_state as _cleanup_state,
    slim_state_for_browser as _slim_state_for_browser,
    backfill_state_with_history,
)

if __name__ == "__main__":
    from streamvis.tui import main
    raise SystemExit(main())
