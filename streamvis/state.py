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
    CADENCE_BASE_SEC,
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
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    
    # Ensure version is set
    if "meta" not in state:
        state["meta"] = {}
    state["meta"]["state_version"] = STATE_SCHEMA_VERSION
    
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, default=str)
        tmp_path.replace(state_path)
    except Exception:
        # Fail silently - state persistence is best-effort
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return

    # In browser/Pyodide builds, keep localStorage in sync on every save so a
    # reload mid-run does not discard learned cadence/latency state.
    try:
        import js  # type: ignore[import]
    except Exception:
        js = None  # type: ignore[assignment]

    if js is None:
        return

    serialized = json.dumps(state, separators=(",", ":"), sort_keys=True, default=str)
    try:
        js.window.localStorage.setItem("streamvis_state_json", serialized)
    except Exception:
        # If iOS/Safari quota is tight, fall back to a slimmed state that
        # preserves cadence/latency learning but drops bulky overlays.
        try:
            slim = slim_state_for_browser(state)
            js.window.localStorage.setItem(
                "streamvis_state_json",
                json.dumps(slim, separators=(",", ":"), sort_keys=True, default=str),
            )
        except Exception:
            pass


def evict_dynamic_sites(state: dict[str, Any]) -> list[str]:
    """
    Evict dynamically discovered sites (Nearby) from persisted state.

    This intentionally removes both:
    - `meta.dynamic_sites` entries (site metadata)
    - any per-gauge learned state for those dynamic gauge_ids under `state["gauges"]`

    Returns the list of gauge_ids removed.
    """
    meta = state.get("meta", {})
    if not isinstance(meta, dict):
        return []

    dyn = meta.get("dynamic_sites")
    if not isinstance(dyn, dict) or not dyn:
        return []

    removed_ids = [gid for gid in dyn.keys() if isinstance(gid, str)]

    # Remove dynamic sites metadata.
    meta.pop("dynamic_sites", None)

    # Remove per-gauge state for evicted gauges.
    gauges_state = state.get("gauges", {})
    if isinstance(gauges_state, dict):
        for gid in removed_ids:
            gauges_state.pop(gid, None)

    # Clear cached nearby search so a future enable re-discovers fresh stations.
    meta.pop("nearby_search_ts", None)

    # Remove evicted dynamic gauges from any cached nearby list.
    cached = meta.get("nearby_gauges")
    if isinstance(cached, list):
        kept = [gid for gid in cached if isinstance(gid, str) and gid not in set(removed_ids)]
        if kept:
            meta["nearby_gauges"] = kept
        else:
            meta.pop("nearby_gauges", None)

    return removed_ids


def cleanup_state(state: dict[str, Any]) -> None:
    """
    Normalize and de-duplicate cached state so that:
    - history has at most one entry per timestamp
    - last_timestamp aligns with the latest history entry
    - we never keep more than HISTORY_LIMIT points per gauge
    """
    gauges = state.get("gauges", {})
    if not isinstance(gauges, dict):
        state["gauges"] = {}
        return
    
    for g_state in gauges.values():
        if not isinstance(g_state, dict):
            continue
        
        history = g_state.get("history", [])
        if not isinstance(history, list):
            g_state["history"] = []
            continue
        
        if isinstance(history, list) and history:
            by_ts: dict[str, dict[str, Any]] = {}
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                ts = entry.get("ts")
                if not isinstance(ts, str):
                    continue
                existing = by_ts.get(ts)
                if existing is None:
                    by_ts[ts] = entry
                    continue
                # Prefer non-None values when merging duplicates.
                for key in ("stage", "flow"):
                    if entry.get(key) is not None:
                        existing[key] = entry[key]

            if by_ts:
                ordered = sorted(by_ts.items(), key=lambda kv: kv[0])
                trimmed = ordered[-HISTORY_LIMIT:]
                g_state["history"] = [e for _, e in trimmed]
                latest_ts = trimmed[-1][0]
                g_state["last_timestamp"] = latest_ts
                latest = trimmed[-1][1]
                if "stage" in latest:
                    g_state["last_stage"] = latest["stage"]
                if "flow" in latest:
                    g_state["last_flow"] = latest["flow"]

        # Clamp learned interval into sane bounds.
        mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        if not isinstance(mean_interval, (int, float)) or mean_interval <= 0:
            mean_interval = DEFAULT_INTERVAL_SEC
        mean_interval = max(MIN_UPDATE_GAP_SEC, min(float(mean_interval), MAX_LEARNABLE_INTERVAL_SEC))
        cad_mult = g_state.get("cadence_mult")
        if isinstance(cad_mult, int) and cad_mult > 0:
            snapped = cad_mult * CADENCE_BASE_SEC
            mean_interval = max(MIN_UPDATE_GAP_SEC, min(float(snapped), MAX_LEARNABLE_INTERVAL_SEC))
        g_state["mean_interval_sec"] = mean_interval

        # Clamp stored latency samples.
        latencies = g_state.get("latencies_sec")
        if isinstance(latencies, list):
            clean_lat = [float(x) for x in latencies if isinstance(x, (int, float)) and x >= 0]
            if clean_lat:
                g_state["latencies_sec"] = clean_lat[-HISTORY_LIMIT:]
            else:
                g_state.pop("latencies_sec", None)

        # Initialize/normalize robust latency location/scale.
        loc = g_state.get("latency_loc_sec")
        scale = g_state.get("latency_scale_sec")
        if not isinstance(loc, (int, float)) or loc < 0:
            loc_old = g_state.get("latency_median_sec")
            loc = float(loc_old) if isinstance(loc_old, (int, float)) and loc_old >= 0 else LATENCY_PRIOR_LOC_SEC
        if not isinstance(scale, (int, float)) or scale < 0:
            scale_old = g_state.get("latency_mad_sec")
            scale = (
                float(scale_old)
                if isinstance(scale_old, (int, float)) and scale_old >= 0
                else LATENCY_PRIOR_SCALE_SEC
            )
        g_state["latency_loc_sec"] = float(loc)
        g_state["latency_scale_sec"] = float(scale)

        for key in ("latency_lower_sec", "latency_upper_sec"):
            vals = g_state.get(key)
            if isinstance(vals, list):
                clean = [float(x) for x in vals if isinstance(x, (int, float)) and x >= 0]
                if clean:
                    g_state[key] = clean[-HISTORY_LIMIT:]
                else:
                    g_state.pop(key, None)


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
    from streamvis.scheduler import maybe_update_cadence_from_deltas, estimate_phase_offset_sec
    
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
        
        # Estimate cadence from deltas.
        deltas: list[float] = []
        prev_dt: datetime | None = None
        for pt in g_state.get("history", []) or []:
            ts_raw = pt.get("ts") if isinstance(pt, dict) else None
            if not isinstance(ts_raw, str):
                continue
            dt = parse_timestamp(ts_raw)
            if dt is None:
                continue
            if prev_dt is not None:
                delta = (dt - prev_dt).total_seconds()
                if delta >= MIN_UPDATE_GAP_SEC:
                    deltas.append(delta)
            prev_dt = dt

        if deltas:
            mean_interval = sum(deltas) / len(deltas)
            mean_interval = max(MIN_UPDATE_GAP_SEC, min(float(mean_interval), MAX_LEARNABLE_INTERVAL_SEC))
            g_state["mean_interval_sec"] = mean_interval
            g_state["last_delta_sec"] = deltas[-1]
            g_state["deltas"] = deltas[-HISTORY_LIMIT:]
            maybe_update_cadence_from_deltas(g_state)
            estimate_phase_offset_sec(g_state)
        else:
            mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
            if not isinstance(mean_interval, (int, float)) or mean_interval <= 0:
                mean_interval = DEFAULT_INTERVAL_SEC
            mean_interval = max(MIN_UPDATE_GAP_SEC, min(float(mean_interval), MAX_LEARNABLE_INTERVAL_SEC))
            g_state["mean_interval_sec"] = mean_interval


def maybe_backfill_state(state: dict[str, Any], hours_back: int) -> None:
    """
    Backfill state once per requested horizon; if a larger horizon is requested
    later, it will extend the history.
    """
    meta = state.setdefault("meta", {})
    prev_hours = meta.get("backfill_hours", 0)
    if not isinstance(prev_hours, (int, float)):
        prev_hours = 0
    if hours_back <= prev_hours:
        return
    
    from streamvis.usgs.adapter import fetch_gauge_history
    from streamvis.config import SITE_MAP
    
    history = fetch_gauge_history(SITE_MAP, hours_back)
    backfill_state_with_history(state, history)
    meta["backfill_hours"] = max(int(prev_hours), int(hours_back))


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
    if last_check is None:
        last_check = parse_timestamp(meta.get("last_backfill_check"))
    
    if last_check is not None:
        hours_since = (now - last_check).total_seconds() / 3600.0
        if hours_since < PERIODIC_BACKFILL_INTERVAL_HOURS:
            return
    
    from streamvis.usgs.adapter import fetch_gauge_history
    from streamvis.config import SITE_MAP
    
    history = fetch_gauge_history(SITE_MAP, lookback_hours)
    backfill_state_with_history(state, history)
    meta["last_periodic_backfill_ts"] = now.isoformat()
    meta["last_backfill_check"] = now.isoformat()


def update_state_with_readings(
    state: dict[str, Any],
    readings: dict[str, dict[str, Any]],
    poll_ts: datetime | None = None,
) -> dict[str, bool]:
    """
    Update persisted state with latest observations and learn per-gauge cadence.
    Returns a dict of gauge_id -> bool indicating whether a new observation was seen.
    """
    from streamvis.scheduler import snap_delta_to_cadence, maybe_update_cadence_from_deltas, estimate_phase_offset_sec

    seen_updates: dict[str, bool] = {}
    gauges_state = state.setdefault("gauges", {})
    meta_state = state.setdefault("meta", {})
    now = poll_ts or datetime.now(timezone.utc)

    for gauge_id, reading in readings.items():
        if not isinstance(reading, dict):
            continue

        observed_at_raw = reading.get("observed_at")
        if isinstance(observed_at_raw, datetime):
            observed_at = observed_at_raw
        elif isinstance(observed_at_raw, str):
            observed_at = parse_timestamp(observed_at_raw)
        else:
            observed_at = None

        g_state = gauges_state.setdefault(gauge_id, {})
        if not isinstance(g_state, dict):
            g_state = {}
            gauges_state[gauge_id] = g_state

        if observed_at is None:
            g_state["last_poll_ts"] = now.isoformat()
            seen_updates[gauge_id] = False
            continue

        prev_ts = parse_timestamp(g_state.get("last_timestamp"))
        prev_poll_ts = parse_timestamp(g_state.get("last_poll_ts"))
        prev_mean = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        if not isinstance(prev_mean, (int, float)) or prev_mean <= 0:
            prev_mean = DEFAULT_INTERVAL_SEC
        last_delta = g_state.get("last_delta_sec")
        no_update_polls = g_state.get("no_update_polls", 0)
        is_update = False

        # Only treat strictly newer observation timestamps as updates.
        if prev_ts is not None and observed_at <= prev_ts:
            seen_updates[gauge_id] = False

            # Still keep last known values in sync with the latest reading.
            stage_now = reading.get("stage")
            flow_now = reading.get("flow")
            if stage_now is not None:
                g_state["last_stage"] = stage_now
            if flow_now is not None:
                g_state["last_flow"] = flow_now

            # Same-timestamp parameter refresh: update the last history entry
            # rather than freezing a stale stage/flow pair.
            if prev_ts is not None and observed_at == prev_ts:
                history = g_state.get("history")
                if isinstance(history, list) and history:
                    last_entry = history[-1]
                    ts_str = last_entry.get("ts") if isinstance(last_entry, dict) else None
                    if isinstance(ts_str, str) and parse_timestamp(ts_str) == observed_at:
                        if stage_now is not None:
                            last_entry["stage"] = stage_now
                        if flow_now is not None:
                            last_entry["flow"] = flow_now

            g_state["no_update_polls"] = int(no_update_polls) + 1
            g_state["last_poll_ts"] = now.isoformat()
            continue

        if prev_ts is not None and observed_at > prev_ts:
            delta_sec = (observed_at - prev_ts).total_seconds()
            if delta_sec >= MIN_UPDATE_GAP_SEC:
                clamped = min(max(delta_sec, MIN_UPDATE_GAP_SEC), MAX_LEARNABLE_INTERVAL_SEC)
                snapped, _k = snap_delta_to_cadence(clamped)
                if snapped is not None:
                    prev_mean = ewma(float(prev_mean), float(snapped), EWMA_ALPHA)
                else:
                    prev_mean = ewma(float(prev_mean), float(clamped), EWMA_ALPHA)
                last_delta = delta_sec
                is_update = True
        elif prev_ts is None:
            is_update = True

        obs_ts_str = observed_at.isoformat()
        g_state["last_timestamp"] = obs_ts_str
        g_state["mean_interval_sec"] = max(float(prev_mean), float(MIN_UPDATE_GAP_SEC))
        if last_delta is not None:
            g_state["last_delta_sec"] = last_delta

        stage_now = reading.get("stage")
        flow_now = reading.get("flow")
        if stage_now is not None:
            g_state["last_stage"] = stage_now
        if flow_now is not None:
            g_state["last_flow"] = flow_now

        history = g_state.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            g_state["history"] = history
        if not history or history[-1].get("ts") != obs_ts_str:
            history.append({"ts": obs_ts_str, "stage": stage_now, "flow": flow_now})
        if len(history) > HISTORY_LIMIT:
            del history[0 : len(history) - HISTORY_LIMIT]

        # Instrumentation: polls per update (counts only real observation advances).
        if is_update:
            polls_since_update = int(no_update_polls) if isinstance(no_update_polls, (int, float)) else 0
            polls_this_update = polls_since_update + 1
            prev_polls_ewma = g_state.get("polls_per_update_ewma")
            if isinstance(prev_polls_ewma, (int, float)) and prev_polls_ewma > 0:
                g_state["polls_per_update_ewma"] = ewma(float(prev_polls_ewma), float(polls_this_update), EWMA_ALPHA)
            else:
                g_state["polls_per_update_ewma"] = float(polls_this_update)
            g_state["last_polls_per_update"] = polls_this_update

        if is_update and last_delta is not None:
            deltas = g_state.setdefault("deltas", [])
            if not isinstance(deltas, list):
                deltas = []
                g_state["deltas"] = deltas
            deltas.append(last_delta)
            if len(deltas) > HISTORY_LIMIT:
                del deltas[0 : len(deltas) - HISTORY_LIMIT]

            maybe_update_cadence_from_deltas(g_state)
            estimate_phase_offset_sec(g_state)

            # If we do not have a strong cadence multiple yet, ensure that a slow
            # gauge can still snap upward quickly from the prior.
            if "cadence_mult" not in g_state and len(deltas) >= 3:
                avg_delta = sum(float(d) for d in deltas) / len(deltas)
                mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
                if isinstance(mean_interval, (int, float)) and mean_interval < 0.75 * avg_delta:
                    mean_interval = max(MIN_UPDATE_GAP_SEC, min(avg_delta, MAX_LEARNABLE_INTERVAL_SEC))
                    g_state["mean_interval_sec"] = float(mean_interval)

            # Latency window: when did this observation appear in the API?
            lower = 0.0
            if prev_poll_ts is not None:
                lower = max(0.0, (prev_poll_ts - observed_at).total_seconds())
            upper = max(0.0, (now - observed_at).total_seconds())

            lat_l = g_state.setdefault("latency_lower_sec", [])
            lat_u = g_state.setdefault("latency_upper_sec", [])
            if isinstance(lat_l, list):
                lat_l.append(float(lower))
                if len(lat_l) > HISTORY_LIMIT:
                    del lat_l[0 : len(lat_l) - HISTORY_LIMIT]
            if isinstance(lat_u, list):
                lat_u.append(float(upper))
                if len(lat_u) > HISTORY_LIMIT:
                    del lat_u[0 : len(lat_u) - HISTORY_LIMIT]

            samples = g_state.setdefault("latencies_sec", [])
            if not isinstance(samples, list):
                samples = []
                g_state["latencies_sec"] = samples

            prior_loc = g_state.get("latency_loc_sec")
            if not isinstance(prior_loc, (int, float)) or prior_loc < 0:
                prior_loc = g_state.get("latency_median_sec", LATENCY_PRIOR_LOC_SEC)
            prior_scale = g_state.get("latency_scale_sec")
            if not isinstance(prior_scale, (int, float)) or prior_scale <= 0:
                prior_scale = g_state.get("latency_mad_sec", LATENCY_PRIOR_SCALE_SEC)

            sample = min(max(float(prior_loc), lower), upper)
            samples.append(float(sample))
            g_state["last_latency_lower_sec"] = float(lower)
            g_state["last_latency_upper_sec"] = float(upper)
            g_state["last_latency_sample_sec"] = float(sample)
            if len(samples) > HISTORY_LIMIT:
                del samples[0 : len(samples) - HISTORY_LIMIT]

            if len(samples) < 3:
                loc = float(prior_loc)
                scale = float(prior_scale)
            else:
                loc, scale = tukey_biweight_location_scale(
                    [float(x) for x in samples if isinstance(x, (int, float)) and x >= 0],
                    initial_loc=float(prior_loc),
                    initial_scale=float(prior_scale),
                )
            g_state["latency_loc_sec"] = float(loc)
            g_state["latency_scale_sec"] = float(scale)

            # Reset consecutive no-update counter now that we saw a new point.
            g_state["no_update_polls"] = 0

        g_state["last_poll_ts"] = now.isoformat()
        seen_updates[gauge_id] = is_update

    if isinstance(meta_state, dict):
        meta_state["last_update_run"] = datetime.now(timezone.utc).isoformat()

    return seen_updates
