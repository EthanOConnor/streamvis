#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests

STATE_FILE_DEFAULT = Path.home() / ".streamvis_state.json"
# Start with a conservative 8-minute cadence and only speed up if
# the data clearly updates more frequently.
DEFAULT_INTERVAL_SEC = 8 * 60
MIN_RETRY_SEC = 60              # Short retry when we were early or on error.
MAX_RETRY_SEC = 5 * 60          # Cap retry wait to avoid hammering.
HEADSTART_SEC = 30              # Poll slightly before expected update.
EWMA_ALPHA = 0.30               # How quickly to learn update cadence.
HISTORY_LIMIT = 120            # Keep a small rolling window of observations.
UI_TICK_SEC = 0.15             # UI refresh tick for TUI mode.
MIN_UPDATE_GAP_SEC = 8 * 60     # Ignore sub-8-minute deltas when learning cadence.
MAX_LEARNABLE_INTERVAL_SEC = 6 * 3600  # Do not learn cadences longer than 6 hours.

# USGS gauge IDs for the Snoqualmie system we care about.

SITE_MAP = {
    "TANW1": "12141300",  # Middle Fork Snoqualmie R near Tanner
    "GARW1": "12143400",  # SF Snoqualmie R ab Alice Cr nr Garcia
    "SQUW1": "12144500",  # Snoqualmie R near Snoqualmie
    "CRNW1": "12149000",  # Snoqualmie R near Carnation
}

USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"

# Optional: rough flood thresholds for status coloring.
# These are real for CRNW1 & SQUW1; TANW1 & GARW1 are placeholders you can tune.
FLOOD_THRESHOLDS: Dict[str, Dict[str, float | None]] = {
    "CRNW1": {  # Snoqualmie near Carnation
        "action": 50.7,
        "minor": 54.0,
        "moderate": 56.0,
        "major": 58.0,
    },
    "SQUW1": {  # Snoqualmie at the Falls (stage equivalents)
        "action": 11.94,
        "minor": 13.54,
        "moderate": 16.21,
        "major": 17.42,
    },
    # You can fill these in later if you find good numbers:
    "TANW1": {
        "action": None,
        "minor": None,
        "moderate": None,
        "major": None,
    },
    "GARW1": {
        "action": None,
        "minor": None,
        "moderate": None,
        "major": None,
    },
}


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # USGS returns ISO8601, sometimes with Z, sometimes with offset.
        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def _fmt_clock(dt: datetime | None, with_date: bool = False) -> str:
    if dt is None:
        return "-"
    local_dt = dt.astimezone()
    if with_date:
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    return local_dt.strftime("%H:%M:%S")


def _fmt_rel(now: datetime, target: datetime | None) -> str:
    if target is None:
        return "unknown"
    delta = (target - now).total_seconds()
    if abs(delta) < 1:
        return "now"
    suffix = "ago" if delta < 0 else "in"
    delta = abs(delta)
    if delta < 60:
        val = int(delta)
        unit = "s"
    elif delta < 3600:
        val = int(delta // 60)
        unit = "m"
    else:
        val = int(delta // 3600)
        unit = "h"
    return f"{suffix} {val}{unit}"


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


def fetch_gauge_data() -> Dict[str, Dict[str, Any]]:
    """
    Fetch latest stage (ft) and flow (cfs) for TANW1, GARW1, SQUW1, CRNW1
    from USGS Instantaneous Values service.

    Returns:
        {
          "TANW1": {"stage": float, "flow": float, "status": str},
          ...
        }
    On error, returns {} (caller will just skip drawing rows).
    """
    # Prepare result skeleton
    result = {
        g: {"stage": None, "flow": None, "status": "NORMAL", "observed_at": None}
        for g in SITE_MAP.keys()
    }

    params = {
        "format": "json",
        "sites": ",".join(SITE_MAP.values()),
        "parameterCd": "00060,00065",   # discharge, stage
        "siteStatus": "all",
    }

    try:
        resp = requests.get(USGS_IV_URL, params=params, timeout=5.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        # Network / JSON issue; show nothing but fail gracefully.
        return {}

    # Reverse map: USGS site -> gauge ID like TANW1
    site_to_gauge = {v: k for k, v in SITE_MAP.items()}

    ts_list = payload.get("value", {}).get("timeSeries", [])
    for ts in ts_list:
        try:
            site_no = ts["sourceInfo"]["siteCode"][0]["value"]
            param = ts["variable"]["variableCode"][0]["value"]  # '00060' or '00065'
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue

            values = ts.get("values", [])
            if not values or not values[0].get("value"):
                continue

            last_point = values[0]["value"][-1]
            val = float(last_point["value"])
            ts_raw = last_point.get("dateTime")
            obs_at = _parse_timestamp(ts_raw)
        except Exception:
            continue

        if param == "00060":        # discharge, cfs
            result[gauge_id]["flow"] = val
        elif param == "00065":      # gage height, ft
            result[gauge_id]["stage"] = val
        # Track the freshest observation time across parameters for scheduling.
        current_obs = result[gauge_id].get("observed_at")
        if obs_at and (current_obs is None or obs_at > current_obs):
            result[gauge_id]["observed_at"] = obs_at

    # Compute status strings based on stage thresholds
    for g, d in result.items():
        stage = d["stage"]
        d["status"] = classify_status(g, stage)

    return result


def _ewma(current_mean: float, new_value: float, alpha: float = EWMA_ALPHA) -> float:
    if current_mean <= 0:
        return new_value
    return (1 - alpha) * current_mean + alpha * new_value


def load_state(state_path: Path) -> Dict[str, Any]:
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        state = {"gauges": {}, "meta": {}}
    if not isinstance(state, dict):
        state = {"gauges": {}, "meta": {}}
    state.setdefault("gauges", {})
    state.setdefault("meta", {})
    _cleanup_state(state)
    return state


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def _cleanup_state(state: Dict[str, Any]) -> None:
    """
    Normalize and de-duplicate cached state so that:
    - history has at most one entry per timestamp
    - last_timestamp aligns with the latest history entry
    - we never keep more than HISTORY_LIMIT points per gauge
    """
    gauges_state = state.get("gauges", {})
    if not isinstance(gauges_state, dict):
        state["gauges"] = {}
        return

    for g_state in gauges_state.values():
        if not isinstance(g_state, dict):
            continue
        history = g_state.get("history")
        if isinstance(history, list) and history:
            # De-duplicate by timestamp, keeping the most recent entry.
            by_ts: Dict[str, Dict[str, Any]] = {}
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                ts = entry.get("ts")
                if isinstance(ts, str):
                    by_ts[ts] = entry
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
        mean_interval = max(MIN_UPDATE_GAP_SEC, min(mean_interval, MAX_LEARNABLE_INTERVAL_SEC))
        g_state["mean_interval_sec"] = mean_interval


def fetch_gauge_history(hours_back: int) -> Dict[str, List[Dict[str, Any]]]:
    """
    Backfill recent history for all gauges from the USGS IV service.

    Returns a mapping gauge_id -> list of points:
        {"ts": iso8601, "stage": float | None, "flow": float | None}
    """
    if hours_back <= 0:
        return {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)

    params = {
        "format": "json",
        "sites": ",".join(SITE_MAP.values()),
        "parameterCd": "00060,00065",   # discharge, stage
        "siteStatus": "all",
        "startDT": start.isoformat(timespec="minutes").replace("+00:00", "Z"),
        "endDT": end.isoformat(timespec="minutes").replace("+00:00", "Z"),
    }

    try:
        resp = requests.get(USGS_IV_URL, params=params, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return {}

    site_to_gauge = {v: k for k, v in SITE_MAP.items()}
    by_gauge: Dict[str, Dict[str, Dict[str, Any]]] = {
        g: {} for g in SITE_MAP.keys()
    }

    ts_list = payload.get("value", {}).get("timeSeries", [])
    for ts in ts_list:
        try:
            site_no = ts["sourceInfo"]["siteCode"][0]["value"]
            param = ts["variable"]["variableCode"][0]["value"]  # '00060' or '00065'
            gauge_id = site_to_gauge.get(site_no)
            if gauge_id is None:
                continue

            values = ts.get("values", [])
            if not values or not values[0].get("value"):
                continue
        except Exception:
            continue

        for point in values[0]["value"]:
            try:
                val = float(point["value"])
                ts_raw = point.get("dateTime")
                obs_at = _parse_timestamp(ts_raw)
                if obs_at is None:
                    continue
                ts_key = obs_at.isoformat()
            except Exception:
                continue

            entry = by_gauge[gauge_id].setdefault(
                ts_key, {"ts": ts_key, "stage": None, "flow": None}
            )
            if param == "00060":
                entry["flow"] = val
            elif param == "00065":
                entry["stage"] = val

    result: Dict[str, List[Dict[str, Any]]] = {}
    for gauge_id, points_by_ts in by_gauge.items():
        if not points_by_ts:
            continue
        ordered = sorted(points_by_ts.items(), key=lambda kv: kv[0])
        result[gauge_id] = [entry for _, entry in ordered]

    return result


def backfill_state_with_history(state: Dict[str, Any], history_map: Dict[str, List[Dict[str, Any]]]) -> None:
    """
    Merge backfilled history into the existing state, enforcing:
    - at most one point per timestamp
    - at most HISTORY_LIMIT points per gauge
    - a reasonable learned cadence from the observed deltas
    """
    gauges_state = state.setdefault("gauges", {})

    for gauge_id, points in history_map.items():
        if not points:
            continue
        g_state = gauges_state.setdefault(gauge_id, {})
        existing_history = g_state.get("history", [])
        combined: Dict[str, Dict[str, Any]] = {}

        if isinstance(existing_history, list):
            for entry in existing_history:
                if isinstance(entry, dict) and isinstance(entry.get("ts"), str):
                    combined[entry["ts"]] = entry

        for p in points:
            ts = p.get("ts")
            if isinstance(ts, str):
                combined[ts] = {
                    "ts": ts,
                    "stage": p.get("stage"),
                    "flow": p.get("flow"),
                }

        if not combined:
            continue

        ordered_ts = sorted(combined.keys())
        trimmed_ts = ordered_ts[-HISTORY_LIMIT:]
        new_history = [combined[ts] for ts in trimmed_ts]
        g_state["history"] = new_history

        latest = new_history[-1]
        latest_ts = latest.get("ts")
        if isinstance(latest_ts, str):
            g_state["last_timestamp"] = latest_ts
        if "stage" in latest:
            g_state["last_stage"] = latest["stage"]
        if "flow" in latest:
            g_state["last_flow"] = latest["flow"]

        # Estimate cadence from deltas.
        deltas: List[float] = []
        prev_dt: datetime | None = None
        for entry in new_history:
            ts = entry.get("ts")
            if not isinstance(ts, str):
                continue
            dt = _parse_timestamp(ts)
            if dt is None:
                continue
            if prev_dt is not None:
                delta = (dt - prev_dt).total_seconds()
                if delta >= MIN_UPDATE_GAP_SEC:
                    deltas.append(delta)
            prev_dt = dt

        if deltas:
            mean_interval = sum(deltas) / len(deltas)
        else:
            mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)

        mean_interval = max(MIN_UPDATE_GAP_SEC, min(mean_interval, MAX_LEARNABLE_INTERVAL_SEC))
        g_state["mean_interval_sec"] = mean_interval
        if deltas:
            g_state["last_delta_sec"] = deltas[-1]
            g_state["deltas"] = deltas[-HISTORY_LIMIT:]


def maybe_backfill_state(state: Dict[str, Any], hours_back: int) -> None:
    """
    Backfill state once per requested horizon; if a larger horizon is requested
    later, it will extend the history.
    """
    if hours_back <= 0:
        return

    meta = state.setdefault("meta", {})
    previous = meta.get("backfill_hours", 0)
    if isinstance(previous, (int, float)) and previous >= hours_back:
        return

    history_map = fetch_gauge_history(hours_back)
    if not history_map:
        return

    backfill_state_with_history(state, history_map)
    meta["backfill_hours"] = max(int(previous or 0), int(hours_back))


def update_state_with_readings(state: Dict[str, Any], readings: Dict[str, Dict[str, Any]]) -> Dict[str, bool]:
    """
    Update persisted state with latest observations and learn per-gauge cadence.
    Returns a dict of gauge_id -> bool indicating whether a new observation was seen.
    """
    seen_updates: Dict[str, bool] = {}
    gauges_state = state.setdefault("gauges", {})
    meta_state = state.setdefault("meta", {})

    for gauge_id, reading in readings.items():
        observed_at: datetime | None = reading.get("observed_at")
        if observed_at is None:
            seen_updates[gauge_id] = False
            continue

        g_state = gauges_state.setdefault(gauge_id, {})
        prev_ts = _parse_timestamp(g_state.get("last_timestamp"))
        prev_mean = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        last_delta = g_state.get("last_delta_sec")
        is_update = False
        delta_sec: float | None = None

        # Only treat strictly newer observation timestamps as updates.
        if prev_ts is not None and observed_at <= prev_ts:
            # No new point; keep existing cadence and history as-is.
            seen_updates[gauge_id] = False
            # Still keep last known values in sync with the latest reading.
            g_state["last_stage"] = reading.get("stage")
            g_state["last_flow"] = reading.get("flow")
            continue

        if prev_ts is not None and observed_at > prev_ts:
            delta_sec = (observed_at - prev_ts).total_seconds()
            if delta_sec >= MIN_UPDATE_GAP_SEC:
                clamped = min(max(delta_sec, MIN_UPDATE_GAP_SEC), MAX_LEARNABLE_INTERVAL_SEC)
                prev_mean = _ewma(prev_mean, clamped)
                last_delta = delta_sec
                is_update = True
        elif prev_ts is None:
            is_update = True

        g_state["last_timestamp"] = observed_at.isoformat()
        g_state["mean_interval_sec"] = max(prev_mean, MIN_UPDATE_GAP_SEC)
        if last_delta is not None:
            g_state["last_delta_sec"] = last_delta
        g_state["last_stage"] = reading.get("stage")
        g_state["last_flow"] = reading.get("flow")
        history = g_state.setdefault("history", [])
        # Append at most one history point per new observation timestamp.
        if not history or history[-1].get("ts") != observed_at.isoformat():
            history.append(
                {
                    "ts": observed_at.isoformat(),
                    "stage": reading.get("stage"),
                    "flow": reading.get("flow"),
                }
            )
        if len(history) > HISTORY_LIMIT:
            del history[0 : len(history) - HISTORY_LIMIT]

        if is_update and last_delta is not None:
            deltas = g_state.setdefault("deltas", [])
            deltas.append(last_delta)
            if len(deltas) > HISTORY_LIMIT:
                del deltas[0 : len(deltas) - HISTORY_LIMIT]

        seen_updates[gauge_id] = is_update

    meta_state["last_update_run"] = datetime.now(timezone.utc).isoformat()

    return seen_updates


def predict_next_poll(state: Dict[str, Any], now: datetime) -> datetime:
    gauges_state = state.get("gauges", {})
    candidates = []
    for gauge_id in SITE_MAP.keys():
        g_state = gauges_state.get(gauge_id, {})
        last_ts = _parse_timestamp(g_state.get("last_timestamp"))
        mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        mean_interval = max(MIN_UPDATE_GAP_SEC, min(mean_interval, MAX_LEARNABLE_INTERVAL_SEC))
        if last_ts is None:
            last_ts = now
        candidates.append(last_ts + timedelta(seconds=mean_interval))

    if not candidates:
        return now + timedelta(seconds=DEFAULT_INTERVAL_SEC)

    target = min(candidates) - timedelta(seconds=HEADSTART_SEC)
    if target < now:
        return now + timedelta(seconds=MIN_UPDATE_GAP_SEC)
    return target


def predict_gauge_next(state: Dict[str, Any], gauge_id: str, now: datetime) -> datetime | None:
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id)
    if not g_state:
        return None
    last_ts = _parse_timestamp(g_state.get("last_timestamp"))
    mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
    if last_ts is None:
        return None
    # Clamp learned interval into the same sane bounds used elsewhere.
    mean_interval = max(MIN_UPDATE_GAP_SEC, min(mean_interval, MAX_LEARNABLE_INTERVAL_SEC))

    # Predict the first expected update time that is >= now, assuming a
    # roughly periodic cadence.
    delta_since_last = (now - last_ts).total_seconds()
    if delta_since_last <= 0:
        # We are at or before the last observation; simplest is last + interval.
        return last_ts + timedelta(seconds=mean_interval)

    multiples = max(1, math.ceil(delta_since_last / mean_interval))
    return last_ts + timedelta(seconds=mean_interval * multiples)


def _history_values(state: Dict[str, Any], gauge_id: str, metric: str, limit: int = HISTORY_LIMIT) -> List[float]:
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    history = g_state.get("history", [])
    values: List[float] = []
    for entry in history[-limit:]:
        val = entry.get(metric)
        if isinstance(val, (int, float)):
            values.append(float(val))
    return values


def _render_sparkline(values: List[float], width: int = 48) -> str:
    if not values:
        return "(no data)"
    if len(values) == 1:
        return f"{values[0]:.2f}"

    chars = " .:-=+*#%@"
    vmin = min(values)
    vmax = max(values)
    span = vmax - vmin
    if span <= 0:
        return ("=" * min(len(values), width))[:width]

    step = max(1, math.ceil(len(values) / width))
    sampled = values[-step * width :: step]
    line = []
    for v in sampled[-width:]:
        level = int((v - vmin) / span * (len(chars) - 1))
        line.append(chars[level])
    return "".join(line)


def render_table(readings: Dict[str, Dict[str, Any]], state: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    header = "Gauge   Stage(ft)   Flow(cfs)   Status         Observed    Next ETA"
    print(header)
    print("-" * len(header))

    for gauge_id in sorted(SITE_MAP.keys()):
        reading = readings.get(gauge_id, {})
        stage = reading.get("stage")
        flow = reading.get("flow")
        status = reading.get("status", "UNKNOWN")

        gauges_state = state.get("gauges", {})
        g_state = gauges_state.get(gauge_id, {})
        observed_at = reading.get("observed_at") or _parse_timestamp(g_state.get("last_timestamp"))
        next_eta = predict_gauge_next(state, gauge_id, now)

        stage_str = f"{stage:.2f}" if isinstance(stage, (int, float)) else "-"
        flow_str = f"{int(flow):d}" if isinstance(flow, (int, float)) else "-"
        obs_str = _fmt_clock(observed_at)
        next_str = _fmt_rel(now, next_eta) if next_eta and next_eta >= now else "now"

        print(f"{gauge_id:6s} {stage_str:>9s} {flow_str:>10s}   {status:<12s} {obs_str:>10s}   {next_str}")


def tui_loop(args: argparse.Namespace) -> int:
    try:
        import curses
    except Exception:
        print("Curses is required for TUI mode and is unavailable on this platform.", file=sys.stderr)
        return 1

    gauges = sorted(SITE_MAP.keys())

    def color_for_status(status: str, palette: Dict[str, int]) -> int:
        status = (status or "").upper()
        if "MAJOR" in status:
            return palette.get("major", 0)
        if "MOD" in status:
            return palette.get("moderate", 0)
        if "MINOR" in status:
            return palette.get("minor", 0)
        if "ACTION" in status:
            return palette.get("action", 0)
        return palette.get("normal", 0)

    def draw_screen(stdscr: Any, readings: Dict[str, Dict[str, Any]], state: Dict[str, Any], selected_idx: int,
                    chart_metric: str, status_msg: str, next_poll_at: datetime | None, palette: Dict[str, int],
                    detail_mode: bool) -> None:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        now = datetime.now(timezone.utc)
        local_now = datetime.now().astimezone()

        title = "STREAMVIS // SNOQUALMIE WATCH"
        clock_line = (
            f"Now {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        sub_line = f"Mode: TUI adaptive | State: {args.state_file}"

        stdscr.addstr(0, 0, title[:max_x - 1], curses.A_BOLD | palette.get("title", 0))
        stdscr.addstr(1, 0, clock_line[:max_x - 1], palette.get("normal", 0))
        stdscr.addstr(2, 0, sub_line[:max_x - 1], palette.get("dim", 0))

        table_start = 4
        header = "Gauge  Stage(ft)  Flow(cfs)  Status       Observed   Next ETA"
        stdscr.addstr(table_start, 0, header[:max_x - 1], curses.A_UNDERLINE | palette.get("normal", 0))

        for row, gauge_id in enumerate(gauges, start=table_start + 1):
            if row >= max_y - 5:
                break  # leave space for detail + footer

            reading = readings.get(gauge_id, {})
            status = reading.get("status", "UNKNOWN")
            stage = reading.get("stage")
            flow = reading.get("flow")
            observed_at = reading.get("observed_at") or _parse_timestamp(
                state.get("gauges", {}).get(gauge_id, {}).get("last_timestamp")
            )
            next_eta = predict_gauge_next(state, gauge_id, now)

            stage_str = f"{stage:.2f}" if isinstance(stage, (int, float)) else "--"
            flow_str = f"{int(flow):d}" if isinstance(flow, (int, float)) else "--"
            obs_str = _fmt_clock(observed_at)
            next_str = _fmt_rel(now, next_eta)

            line = f"{gauge_id:5s}  {stage_str:>8s}  {flow_str:>8s}  {status:<11s} {obs_str:>9s}  {next_str:>8s}"
            color = color_for_status(status, palette)

            if gauge_id == gauges[selected_idx]:
                stdscr.addstr(row, 0, line[:max_x - 1], curses.A_REVERSE | color)
            else:
                stdscr.addstr(row, 0, line[:max_x - 1], color)

        detail_y = row + 2 if 'row' in locals() else table_start + len(gauges) + 2
        if detail_y < max_y - 2:
            selected = gauges[selected_idx]
            g_state = state.get("gauges", {}).get(selected, {})
            reading = readings.get(selected, {})
            observed_at = reading.get("observed_at") or _parse_timestamp(g_state.get("last_timestamp"))
            next_eta = predict_gauge_next(state, selected, now)
            stage = reading.get("stage")
            flow = reading.get("flow")
            detail = (
                f"{selected} | Stage: {stage if stage is not None else '-'} ft | "
                f"Flow: {int(flow) if isinstance(flow, (int, float)) else '-'} cfs | "
                f"Status: {reading.get('status', 'UNKNOWN')}"
            )
            timing = (
                f"Observed {_fmt_clock(observed_at, with_date=False)} ({_fmt_rel(now, observed_at)}), "
                f"Next ETA: {_fmt_rel(now, next_eta)}"
            )
            stdscr.addstr(detail_y, 0, detail[:max_x - 1], palette.get("normal", 0) | curses.A_BOLD)
            stdscr.addstr(detail_y + 1, 0, timing[:max_x - 1], palette.get("normal", 0))

            if detail_mode:
                # Expanded detail: table of recent updates with per-update deltas.
                history = g_state.get("history", []) or []
                recent = history[-6:]
                table_y = detail_y + 3
                if table_y < max_y - 2:
                    header_line = "Recent updates (local)   Stage   ΔStage   Flow   ΔFlow"
                    stdscr.addstr(table_y, 0, header_line[:max_x - 1], palette.get("dim", 0))
                    prev_stage = None
                    prev_flow = None
                    row_y = table_y + 1
                    for entry in recent:
                        if row_y >= max_y - 3:
                            break
                        ts_raw = entry.get("ts")
                        ts_dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
                        ts_str = _fmt_clock(ts_dt, with_date=False)
                        stage_v = entry.get("stage")
                        flow_v = entry.get("flow")
                        ds = stage_v - prev_stage if isinstance(stage_v, (int, float)) and isinstance(prev_stage, (int, float)) else None
                        df = flow_v - prev_flow if isinstance(flow_v, (int, float)) and isinstance(prev_flow, (int, float)) else None
                        prev_stage = stage_v
                        prev_flow = flow_v
                        stage_str = f"{stage_v:6.2f}" if isinstance(stage_v, (int, float)) else "   -- "
                        ds_str = f"{ds:+6.2f}" if isinstance(ds, (int, float)) else "   -- "
                        flow_str = f"{int(flow_v):6d}" if isinstance(flow_v, (int, float)) else "   -- "
                        df_str = f"{int(df):+6d}" if isinstance(df, (int, float)) else "   -- "
                        line = f"{ts_str:>8s}   {stage_str}  {ds_str}  {flow_str}  {df_str}"
                        stdscr.addstr(row_y, 0, line[:max_x - 1], palette.get("chart", 0))
                        row_y += 1

                    # Simple trend summary over the recent window.
                    if len(recent) >= 2:
                        times: List[datetime] = []
                        stages: List[float] = []
                        flows: List[float] = []
                        for entry in recent:
                            ts_raw = entry.get("ts")
                            dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
                            if dt is None:
                                continue
                            s = entry.get("stage")
                            f = entry.get("flow")
                            if isinstance(s, (int, float)):
                                times.append(dt)
                                stages.append(float(s))
                            if isinstance(f, (int, float)):
                                flows.append(float(f))
                        if times and stages:
                            dh = (times[-1] - times[0]).total_seconds() / 3600.0 or 1.0
                            stage_trend = (stages[-1] - stages[0]) / dh
                        else:
                            stage_trend = 0.0
                        if flows:
                            flow_trend = (flows[-1] - flows[0]) / max(dh, 1e-6)
                        else:
                            flow_trend = 0.0
                        trend_line = f"Trend: stage {stage_trend:+.2f} ft/h   flow {flow_trend:+.0f} cfs/h"
                        if row_y < max_y - 2:
                            stdscr.addstr(row_y, 0, trend_line[:max_x - 1], palette.get("dim", 0))
            else:
                # Compact detail: sparkline chart and summary stats.
                chart_vals = _history_values(state, selected, chart_metric)
                chart_line = _render_sparkline(chart_vals, width=max(10, max_x - 12))
                chart_label = f"{chart_metric.upper()} history ({len(chart_vals)} pts, newest right)"
                stdscr.addstr(detail_y + 3, 0, chart_label[:max_x - 1], palette.get("dim", 0))
                stdscr.addstr(detail_y + 4, 0, chart_line[:max_x - 1], palette.get("chart", 0))
                if chart_vals:
                    delta = chart_vals[-1] - chart_vals[0]
                    stats = f"{chart_metric}: min {min(chart_vals):.2f}  max {max(chart_vals):.2f}  Δ {delta:+.2f}"
                    stdscr.addstr(detail_y + 5, 0, stats[:max_x - 1], palette.get("dim", 0))

        footer_y = max_y - 2
        if footer_y >= 0:
            next_multi = _fmt_rel(now, next_poll_at) if next_poll_at else "pending"
            footer = (
                "[↑/↓] select  [Enter] details  [c] toggle chart metric  [r] refresh now  [q] quit  "
                f"Next fetch: {next_multi}  |  {status_msg}"
            )
            stdscr.addstr(footer_y, 0, footer[:max_x - 1], palette.get("dim", 0))

        stdscr.refresh()

    def run(stdscr: Any) -> int:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(int(UI_TICK_SEC * 1000))
        palette: Dict[str, int] = {"normal": 0, "title": 0, "dim": 0, "chart": 0}

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            palette.update(
                {
                    "normal": curses.color_pair(1),
                    "action": curses.color_pair(2),
                    "minor": curses.color_pair(2),
                    "moderate": curses.color_pair(3),
                    "major": curses.color_pair(3) | curses.A_BOLD,
                    "title": curses.color_pair(1) | curses.A_BOLD,
                    "dim": curses.color_pair(4),
                    "chart": curses.color_pair(4),
                }
            )

        state_path = Path(args.state_file)
        state = load_state(state_path)
        maybe_backfill_state(state, args.backfill_hours)
        save_state(state_path, state)
        readings: Dict[str, Dict[str, Any]] = {}
        selected_idx = 0
        chart_metric = args.chart_metric
        status_msg = "Awaiting first fetch..."
        next_poll_at = datetime.now(timezone.utc)
        retry_wait = args.min_retry_seconds
        detail_mode = False

        while True:
            now = datetime.now(timezone.utc)
            if now >= next_poll_at:
                state.setdefault("meta", {})["last_fetch_at"] = now.isoformat()
                fetched = fetch_gauge_data()
                if fetched:
                    readings = fetched
                    retry_wait = args.min_retry_seconds
                    update_state_with_readings(state, readings)
                    save_state(state_path, state)
                    next_poll_at = predict_next_poll(state, now)
                    if next_poll_at <= now:
                        next_poll_at = now + timedelta(seconds=args.min_retry_seconds)
                    status_msg = f"Fetched at {_fmt_clock(now)}; next {_fmt_rel(now, next_poll_at)}"
                    state["meta"]["last_success_at"] = now.isoformat()
                    state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                    save_state(state_path, state)
                else:
                    status_msg = "Fetch failed; backing off."
                    retry_wait = min(args.max_retry_seconds, retry_wait * 2)
                    next_poll_at = now + timedelta(seconds=retry_wait)
                    state["meta"]["last_failure_at"] = now.isoformat()
                    state["meta"]["next_poll_at"] = next_poll_at.isoformat()
                    save_state(state_path, state)

            draw_screen(stdscr, readings, state, selected_idx, chart_metric, status_msg, next_poll_at, palette, detail_mode)

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return 0
            if key in (curses.KEY_UP, ord("k")):
                selected_idx = (selected_idx - 1) % len(gauges)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected_idx = (selected_idx + 1) % len(gauges)
            elif key in (curses.KEY_ENTER, 10, 13):
                detail_mode = not detail_mode
            elif key in (ord("c"), ord("C")):
                chart_metric = "flow" if chart_metric == "stage" else "stage"
                status_msg = f"Chart metric: {chart_metric}"
            elif key in (ord("r"), ord("R")):
                next_poll_at = datetime.now(timezone.utc)
                status_msg = "Manual refresh requested..."

        return 0

    return curses.wrapper(run)


def adaptive_loop(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file)
    state = load_state(state_path)
    maybe_backfill_state(state, args.backfill_hours)
    save_state(state_path, state)
    retry_wait = args.min_retry_seconds
    next_poll_at: datetime | None = None

    while True:
        if next_poll_at:
            sleep_for = max(0, (next_poll_at - datetime.now(timezone.utc)).total_seconds())
            if sleep_for:
                time.sleep(sleep_for)

        state.setdefault("meta", {})["last_fetch_at"] = datetime.now(timezone.utc).isoformat()
        readings = fetch_gauge_data()
        if not readings:
            time.sleep(min(args.max_retry_seconds, retry_wait))
            retry_wait = min(args.max_retry_seconds, retry_wait * 2)
            next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=retry_wait)
            state["meta"]["last_failure_at"] = datetime.now(timezone.utc).isoformat()
            state["meta"]["next_poll_at"] = next_poll_at.isoformat()
            save_state(state_path, state)
            continue

        retry_wait = args.min_retry_seconds
        updates = update_state_with_readings(state, readings)
        save_state(state_path, state)

        if next_poll_at is None or any(updates.values()):
            render_table(readings, state)
        else:
            # We were early; gently widen the interval and try again soon.
            for g_state in state.get("gauges", {}).values():
                if "mean_interval_sec" in g_state:
                    g_state["mean_interval_sec"] *= 1.05
            save_state(state_path, state)
            next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=args.min_retry_seconds)
            continue

        now = datetime.now(timezone.utc)
        next_poll_at = predict_next_poll(state, now)
        if next_poll_at <= now:
            next_poll_at = now + timedelta(seconds=args.min_retry_seconds)
        state["meta"]["last_success_at"] = now.isoformat()
        state["meta"]["next_poll_at"] = next_poll_at.isoformat()
        save_state(state_path, state)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Snoqualmie River USGS gauge watcher.")
    parser.add_argument(
        "--mode",
        choices=["once", "adaptive", "tui"],
        default="once",
        help="Run once, adaptively learn update cadence, or launch the TUI.",
    )
    parser.add_argument(
        "--state-file",
        default=str(STATE_FILE_DEFAULT),
        help="Path to persist observed update cadence and last timestamps.",
    )
    parser.add_argument(
        "--min-retry-seconds",
        type=int,
        default=MIN_RETRY_SEC,
        help="Minimum retry delay when we polled before an update arrived.",
    )
    parser.add_argument(
        "--max-retry-seconds",
        type=int,
        default=MAX_RETRY_SEC,
        help="Maximum retry delay when backing off.",
    )
    parser.add_argument(
        "--backfill-hours",
        type=int,
        default=0,
        help="On start, backfill this many hours of recent history from USGS IV (0 to disable).",
    )
    parser.add_argument(
        "--chart-metric",
        choices=["stage", "flow"],
        default="stage",
        help="Metric to chart in TUI mode.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.mode == "tui":
        return tui_loop(args) or 0

    if args.mode == "adaptive":
        return adaptive_loop(args) or 0

    state_path = Path(args.state_file)
    state = load_state(state_path)
    maybe_backfill_state(state, args.backfill_hours)
    save_state(state_path, state)

    data = fetch_gauge_data()
    if not data:
        print("No data available from USGS Instantaneous Values service.", file=sys.stderr)
        return 1

    update_state_with_readings(state, data)
    save_state(state_path, state)
    render_table(data, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
