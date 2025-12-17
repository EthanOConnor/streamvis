"""
State persistence for streamvis.

Handles:
- Loading/saving state to JSON file with atomic writes
- Single-writer locking (fcntl on Unix, no-op elsewhere)
- State cleanup and normalization
- History backfill from USGS API
- Cadence learning from historical data
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from streamvis.constants import (
    STATE_SCHEMA_VERSION,
    HISTORY_LIMIT,
    DEFAULT_INTERVAL_SEC,
    MIN_UPDATE_GAP_SEC,
    MAX_LEARNABLE_INTERVAL_SEC,
    EWMA_ALPHA,
    PERIODIC_BACKFILL_INTERVAL_HOURS,
    PERIODIC_BACKFILL_LOOKBACK_HOURS,
    LATENCY_PRIOR_LOC_SEC,
    LATENCY_PRIOR_SCALE_SEC,
)
from streamvis.utils import parse_timestamp, ewma, tukey_biweight_location_scale

try:
    import fcntl  # type: ignore[import]
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class StateLockError(Exception):
    """Raised when state file is locked by another process."""


class _StateFileLock:
    """
    Best-effort single-writer lock for a given state file.

    Uses a sibling `.lock` file and `fcntl.flock` when available. On platforms
    without `fcntl` (e.g., Windows, Pyodide), this becomes a no-op.
    """

    def __init__(self, state_path: Path) -> None:
        self._lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        self._fh = None

    def __enter__(self) -> "_StateFileLock":
        if fcntl is None:
            return self
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            fh.close()
            raise StateLockError(
                f"State file is locked by another streamvis process: {self._lock_path}"
            ) from exc
        self._fh = fh
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is None or fcntl is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


def state_lock(state_path: Path) -> contextlib.AbstractContextManager:
    """
    Return a context manager that holds a single-writer lock for `state_path`.
    On platforms without file-lock support this is a no-op.
    """
    if fcntl is None:
        return contextlib.nullcontext()
    return _StateFileLock(state_path)


def load_state(state_path: Path) -> dict[str, Any]:
    """Load state from JSON file, returning empty state on error."""
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        state = {"gauges": {}, "meta": {}}
    
    if not isinstance(state, dict):
        state = {"gauges": {}, "meta": {}}
    if "gauges" not in state or not isinstance(state.get("gauges"), dict):
        state["gauges"] = {}
    if "meta" not in state or not isinstance(state.get("meta"), dict):
        state["meta"] = {}
    
    cleanup_state(state)
    return state


def slim_state_for_browser(state: dict[str, Any]) -> dict[str, Any]:
    """
    Create a smaller persistence-friendly subset of state for browser localStorage.

    This is used only as a fallback when a full JSON write exceeds localStorage
    quotas (notably on iOS Safari). It prioritizes keeping cadence/latency
    learning and the latest readings, while dropping bulky forecast overlays and
    trimming histories.
    """
    slim: dict[str, Any] = {"gauges": {}, "meta": {}}
    
    # Copy meta, excluding large fields
    meta = state.get("meta", {})
    if isinstance(meta, dict):
        for k, v in meta.items():
            if k not in ("dynamic_sites",):
                slim["meta"][k] = v
    
    # Slim gauges: keep learning state, trim history
    gauges = state.get("gauges", {})
    for gid, g_state in gauges.items():
        if not isinstance(g_state, dict):
            continue
        slim_g: dict[str, Any] = {}
        # Keep essential learning state
        for key in (
            "last_timestamp", "last_stage", "last_flow",
            "mean_interval_sec", "cadence_mult", "cadence_fit",
            "phase_offset_sec", "latency_loc_sec", "latency_scale_sec",
        ):
            if key in g_state:
                slim_g[key] = g_state[key]
        # Trim history to last 20 points
        history = g_state.get("history", [])
        if isinstance(history, list):
            slim_g["history"] = history[-20:]
        slim["gauges"][gid] = slim_g
    
    return slim


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Save state to JSON file atomically."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".tmp")
    
    # Ensure version is set
    if "meta" not in state:
        state["meta"] = {}
    state["meta"]["state_version"] = STATE_SCHEMA_VERSION
    
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, default=str)
        tmp_path.replace(state_path)
    except Exception:
        # Fail silently - state persistence is best-effort
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def cleanup_state(state: dict[str, Any]) -> None:
    """
    Normalize and de-duplicate cached state so that:
    - history has at most one entry per timestamp
    - last_timestamp aligns with the latest history entry
    - we never keep more than HISTORY_LIMIT points per gauge
    """
    gauges = state.get("gauges", {})
    if not isinstance(gauges, dict):
        return
    
    for g_state in gauges.values():
        if not isinstance(g_state, dict):
            continue
        
        history = g_state.get("history", [])
        if not isinstance(history, list):
            g_state["history"] = []
            continue
        
        # De-duplicate by timestamp
        seen: dict[str, dict[str, Any]] = {}
        for pt in history:
            if not isinstance(pt, dict):
                continue
            ts = pt.get("ts")
            if not isinstance(ts, str):
                continue
            # Keep latest values for each timestamp
            if ts not in seen:
                seen[ts] = pt
            else:
                # Merge: prefer non-None values
                existing = seen[ts]
                for key in ("stage", "flow"):
                    if pt.get(key) is not None:
                        existing[key] = pt[key]
        
        # Sort by timestamp and limit
        sorted_history = sorted(seen.values(), key=lambda p: p.get("ts", ""))
        g_state["history"] = sorted_history[-HISTORY_LIMIT:]
        
        # Align last_timestamp with newest history entry
        if g_state["history"]:
            g_state["last_timestamp"] = g_state["history"][-1].get("ts")


def backfill_state_with_history(
    state: dict[str, Any],
    history_map: dict[str, list[dict[str, Any]]],
) -> None:
    """
    Merge backfilled history into the existing state, enforcing:
    - at most one point per timestamp
    - at most HISTORY_LIMIT points per gauge
    - a reasonable learned cadence from the observed deltas
    """
    from streamvis.scheduler import maybe_update_cadence_from_deltas
    
    gauges = state.setdefault("gauges", {})
    
    for gauge_id, points in history_map.items():
        if not isinstance(points, list):
            continue
        
        g_state = gauges.setdefault(gauge_id, {})
        if not isinstance(g_state, dict):
            g_state = {}
            gauges[gauge_id] = g_state
        
        existing = g_state.get("history", [])
        if not isinstance(existing, list):
            existing = []
        
        # Merge by timestamp
        by_ts: dict[str, dict[str, Any]] = {}
        for pt in existing:
            ts = pt.get("ts")
            if isinstance(ts, str):
                by_ts[ts] = pt
        for pt in points:
            ts = pt.get("ts")
            if not isinstance(ts, str):
                continue
            if ts not in by_ts:
                by_ts[ts] = {"ts": ts, "stage": None, "flow": None}
            if pt.get("stage") is not None:
                by_ts[ts]["stage"] = pt["stage"]
            if pt.get("flow") is not None:
                by_ts[ts]["flow"] = pt["flow"]
        
        # Sort and limit
        sorted_history = sorted(by_ts.values(), key=lambda p: p.get("ts", ""))
        g_state["history"] = sorted_history[-HISTORY_LIMIT:]
        
        # Update last values
        if g_state["history"]:
            latest = g_state["history"][-1]
            g_state["last_timestamp"] = latest.get("ts")
            if latest.get("stage") is not None:
                g_state["last_stage"] = latest["stage"]
            if latest.get("flow") is not None:
                g_state["last_flow"] = latest["flow"]
        
        # Learn cadence from deltas
        if len(g_state["history"]) >= 4:
            deltas = []
            prev_ts = None
            for pt in g_state["history"]:
                ts = parse_timestamp(pt.get("ts"))
                if ts is not None and prev_ts is not None:
                    delta = (ts - prev_ts).total_seconds()
                    if MIN_UPDATE_GAP_SEC <= delta <= MAX_LEARNABLE_INTERVAL_SEC:
                        deltas.append(delta)
                prev_ts = ts
            
            if deltas:
                # Initialize or update mean_interval
                if "mean_interval_sec" not in g_state:
                    from streamvis.utils import median
                    g_state["mean_interval_sec"] = median(deltas)
                
                # Try to snap to cadence
                maybe_update_cadence_from_deltas(g_state)


def maybe_backfill_state(state: dict[str, Any], hours_back: int) -> None:
    """
    Backfill state once per requested horizon; if a larger horizon is requested
    later, it will extend the history.
    """
    meta = state.setdefault("meta", {})
    prev_hours = meta.get("backfill_hours", 0)
    
    if hours_back <= prev_hours:
        return
    
    from streamvis.usgs import fetch_gauge_history
    from streamvis.config import SITE_MAP
    
    history = fetch_gauge_history(SITE_MAP, hours_back)
    backfill_state_with_history(state, history)
    meta["backfill_hours"] = hours_back


def maybe_periodic_backfill_check(
    state: dict[str, Any],
    now: datetime,
    lookback_hours: int = PERIODIC_BACKFILL_LOOKBACK_HOURS,
) -> None:
    """
    Occasionally re-fetch a recent history window to detect missed updates
    or cadence shifts.

    This is intentionally low-frequency (hours) and uses a modest lookback
    window so it remains polite while improving low-latency accuracy.
    """
    meta = state.setdefault("meta", {})
    last_check = parse_timestamp(meta.get("last_periodic_backfill_ts"))
    
    if last_check is not None:
        hours_since = (now - last_check).total_seconds() / 3600.0
        if hours_since < PERIODIC_BACKFILL_INTERVAL_HOURS:
            return
    
    from streamvis.usgs import fetch_gauge_history
    from streamvis.config import SITE_MAP
    
    history = fetch_gauge_history(SITE_MAP, lookback_hours)
    backfill_state_with_history(state, history)
    meta["last_periodic_backfill_ts"] = now.isoformat()


def update_state_with_readings(
    state: dict[str, Any],
    readings: dict[str, dict[str, Any]],
    poll_ts: datetime | None = None,
) -> dict[str, bool]:
    """
    Update persisted state with latest observations and learn per-gauge cadence.
    Returns a dict of gauge_id -> bool indicating whether a new observation was seen.
    """
    from streamvis.scheduler import maybe_update_cadence_from_deltas, estimate_phase_offset_sec
    
    if poll_ts is None:
        poll_ts = datetime.now(timezone.utc)
    
    gauges = state.setdefault("gauges", {})
    updates: dict[str, bool] = {}
    
    for gauge_id, reading in readings.items():
        if not isinstance(reading, dict):
            continue
        
        g_state = gauges.setdefault(gauge_id, {})
        if not isinstance(g_state, dict):
            g_state = {}
            gauges[gauge_id] = g_state
        
        obs_at = reading.get("observed_at")
        if isinstance(obs_at, datetime):
            obs_ts = obs_at.isoformat()
        elif isinstance(obs_at, str):
            obs_ts = obs_at
        else:
            obs_ts = None
        
        last_ts_str = g_state.get("last_timestamp")
        is_new = obs_ts is not None and obs_ts != last_ts_str
        updates[gauge_id] = is_new
        
        if not is_new:
            # Update no-update counter
            g_state["no_update_polls"] = g_state.get("no_update_polls", 0) + 1
            continue
        
        # New observation
        g_state["no_update_polls"] = 0
        
        # Calculate delta for cadence learning
        if last_ts_str:
            last_ts = parse_timestamp(last_ts_str)
            new_ts = parse_timestamp(obs_ts)
            if last_ts and new_ts:
                delta = (new_ts - last_ts).total_seconds()
                if MIN_UPDATE_GAP_SEC <= delta <= MAX_LEARNABLE_INTERVAL_SEC:
                    old_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
                    g_state["mean_interval_sec"] = ewma(old_interval, delta, EWMA_ALPHA)
        
        # Update last values
        g_state["last_timestamp"] = obs_ts
        if reading.get("stage") is not None:
            g_state["last_stage"] = reading["stage"]
        if reading.get("flow") is not None:
            g_state["last_flow"] = reading["flow"]
        
        # Append to history
        history = g_state.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            g_state["history"] = history
        
        history.append({
            "ts": obs_ts,
            "stage": reading.get("stage"),
            "flow": reading.get("flow"),
        })
        
        # Limit history size
        if len(history) > HISTORY_LIMIT:
            g_state["history"] = history[-HISTORY_LIMIT:]
        
        # Update cadence learning
        maybe_update_cadence_from_deltas(g_state)
        
        # Update phase offset
        phase = estimate_phase_offset_sec(g_state)
        if phase is not None:
            g_state["phase_offset_sec"] = phase
        
        # Update latency estimate
        if obs_ts and poll_ts:
            obs_dt = parse_timestamp(obs_ts)
            if obs_dt:
                latency = (poll_ts - obs_dt).total_seconds()
                if latency > 0:
                    latencies = g_state.setdefault("latencies_sec", [])
                    if not isinstance(latencies, list):
                        latencies = []
                        g_state["latencies_sec"] = latencies
                    latencies.append(latency)
                    if len(latencies) > 20:
                        g_state["latencies_sec"] = latencies[-20:]
                    
                    # Update robust latency estimates
                    loc, scale = tukey_biweight_location_scale(
                        latencies,
                        LATENCY_PRIOR_LOC_SEC,
                        LATENCY_PRIOR_SCALE_SEC,
                    )
                    g_state["latency_loc_sec"] = loc
                    g_state["latency_scale_sec"] = scale
    
    return updates
