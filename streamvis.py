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
MIN_RETRY_SEC = 60               # Short retry when we were early or on error.
MAX_RETRY_SEC = 5 * 60           # Cap retry wait when backing off on errors.
HEADSTART_SEC = 30               # Poll slightly before expected update.
EWMA_ALPHA = 0.30                # How quickly to learn update cadence.
HISTORY_LIMIT = 120              # Keep a small rolling window of observations.
UI_TICK_SEC = 0.15               # UI refresh tick for TUI mode.
MIN_UPDATE_GAP_SEC = 60          # Ignore sub-60-second deltas when learning cadence.
FORECAST_REFRESH_MIN = 60        # Do not refetch forecasts more often than this.
MAX_LEARNABLE_INTERVAL_SEC = 6 * 3600  # Do not learn cadences longer than 6 hours.

# Fine/coarse polling control for adaptive scheduling.
FINE_LATENCY_MAD_MAX_SEC = 60    # Only micro-poll if latency MAD <= 1 minute.
FINE_WINDOW_MIN_SEC = 30         # Minimum half-width of fine window.
FINE_STEP_MIN_SEC = 15           # Minimum fine-mode poll step (keep bursts polite).
FINE_STEP_MAX_SEC = 30           # Maximum fine-mode poll step.
COARSE_STEP_FRACTION = 0.5       # Coarse step ~ fraction of mean interval.
COARSE_STEP_MAX_SEC = 5 * 60     # Do not coarse-poll more often than this.

# USGS gauge IDs for the Snoqualmie system we care about.

SITE_MAP = {
    "TANW1": "12141300",  # Middle Fork Snoqualmie R near Tanner
    "GARW1": "12143400",  # SF Snoqualmie R ab Alice Cr nr Garcia
    "SQUW1": "12144500",  # Snoqualmie R near Snoqualmie
    "CRNW1": "12149000",  # Snoqualmie R near Carnation
}

USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"

# Optional NW RFC text-plot endpoint used for cross-checking observed
# stage/flow for selected stations. We treat USGS IV as authoritative
# and use NW RFC as a secondary view for comparison.
NWRFC_TEXT_BASE = "https://www.nwrfc.noaa.gov/station/flowplot/textPlot.cgi"
NWRFC_REFRESH_MIN = 15  # Minutes between NW RFC cross-checks when enabled.

# For now we only wire GARW1; others can be added after verifying their IDs.
NWRFC_ID_MAP: Dict[str, str] = {
    "GARW1": "GARW1",
}

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


def _parse_nwrfc_timestamp(date_str: str, time_str: str, tz_label: str | None) -> datetime | None:
    """
    Parse a NW RFC local timestamp (e.g., 2025-12-08 19:00) plus a timezone
    label like PST or PDT into a UTC datetime.
    """
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except Exception:
        return None
    # Treat PST/PDT as fixed offsets; this is sufficient for the local use here.
    tz_label = (tz_label or "").upper()
    if tz_label == "PDT":
        offset = -7
    else:
        offset = -8
    local = dt.replace(tzinfo=timezone(timedelta(hours=offset)))
    return local.astimezone(timezone.utc)


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
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    tmp_path.replace(state_path)


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

        # Clamp any stored latency stats.
        latencies = g_state.get("latencies_sec")
        if isinstance(latencies, list):
            clean_lat = [float(x) for x in latencies if isinstance(x, (int, float)) and x >= 0]
            if clean_lat:
                g_state["latencies_sec"] = clean_lat[-HISTORY_LIMIT:]
            else:
                g_state.pop("latencies_sec", None)

        for key in ("latency_lower_sec", "latency_upper_sec"):
            vals = g_state.get(key)
            if isinstance(vals, list):
                clean = [float(x) for x in vals if isinstance(x, (int, float)) and x >= 0]
                if clean:
                    g_state[key] = clean[-HISTORY_LIMIT:]
                else:
                    g_state.pop(key, None)


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


def _resolve_forecast_url(template: str, gauge_id: str, site_no: str) -> str:
    """
    Format a forecast URL from a template.

    The template may contain `{gauge_id}` and `{site_no}` placeholders, for example:
        https://example/api/stations/{gauge_id}/forecast
    """
    return template.format(gauge_id=gauge_id, site_no=site_no)


def fetch_forecast_series(
    forecast_base: str,
    gauge_id: str,
    site_no: str,
    horizon_hours: int,
) -> List[Dict[str, Any]]:
    """
    Fetch forecast time series for a single gauge.

    This function deliberately treats the response as shape-agnostic and only
    assumes we can extract a sequence of (timestamp, stage, flow)-like points.

    Operators should point `forecast_base` at an appropriate NOAA / NWPS endpoint
    and adjust the parsing logic here to match the actual payload.
    """
    if not forecast_base:
        return []

    url = _resolve_forecast_url(forecast_base, gauge_id=gauge_id, site_no=site_no)
    params: Dict[str, Any] = {}
    if horizon_hours > 0:
        # Many forecast APIs accept a horizon or end-time parameter; adapt as needed.
        params["horizon_hours"] = horizon_hours

    try:
        resp = requests.get(url, params=params or None, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    # Accept either a list of points or an object with a top-level list.
    if isinstance(data, list):
        series = data
    elif isinstance(data, dict):
        # Heuristic: look for a likely list field.
        for key in ("forecast", "values", "data", "series"):
            val = data.get(key)
            if isinstance(val, list):
                series = val
                break
        else:
            return []
    else:
        return []

    points: List[Dict[str, Any]] = []
    for entry in series:
        if not isinstance(entry, dict):
            continue

        # NOTE: The field names below are *assumptions* and may need to be
        # adjusted to match the actual NWPS API. Common patterns include
        # `validTime`, `time`, or `forecast_time` for timestamps, and
        # stage/flow values in feet / cfs.
        ts_raw = entry.get("validTime") or entry.get("time") or entry.get("ts")
        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        if dt is None:
            continue

        stage = entry.get("stage_ft") or entry.get("stage") or entry.get("value")
        flow = entry.get("flow_cfs") or entry.get("flow")

        point = {
            "ts": dt.isoformat(),
            "stage": float(stage) if isinstance(stage, (int, float)) else None,
            "flow": float(flow) if isinstance(flow, (int, float)) else None,
        }
        points.append(point)

    # Ensure points are sorted by time.
    points.sort(key=lambda p: p["ts"])
    return points


def summarize_forecast_points(
    points: List[Dict[str, Any]],
    now: datetime,
    horizon_hours: int,
) -> Dict[str, Any]:
    """
    Compute 3-hour, 24-hour, and full-horizon maxima for stage and flow.
    """
    if not points:
        return {}

    max_3h = {"stage": None, "flow": None, "ts": None}
    max_24h = {"stage": None, "flow": None, "ts": None}
    max_full = {"stage": None, "flow": None, "ts": None}

    horizon_sec = horizon_hours * 3600 if horizon_hours > 0 else None

    for p in points:
        ts_raw = p.get("ts")
        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        if dt is None:
            continue
        delta = (dt - now).total_seconds()
        if delta < 0:
            # Only look forward for maxima.
            continue
        if horizon_sec is not None and delta > horizon_sec:
            continue

        stage = p.get("stage")
        flow = p.get("flow")

        def bump(target: Dict[str, Any]) -> None:
            if isinstance(stage, (int, float)):
                if target["stage"] is None or stage > target["stage"]:
                    target["stage"] = stage
                    target["ts"] = dt.isoformat()
            if isinstance(flow, (int, float)):
                if target["flow"] is None or flow > target["flow"]:
                    target["flow"] = flow
                    target["ts"] = dt.isoformat()

        if delta <= 3 * 3600:
            bump(max_3h)
        if delta <= 24 * 3600:
            bump(max_24h)
        bump(max_full)

    return {
        "max_3h": max_3h,
        "max_24h": max_24h,
        "max_full": max_full,
    }


def parse_nwrfc_text(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse NW RFC textPlot output into observed and forecast series.

    We expect a header line with "Forecast/Trend Issued: <ts> <TZ>", a header
    row with "Date/Time (PST) Stage Discharge", and then rows where the first
    four columns are observed (date, time, stage, discharge) and optional
    forecast columns follow.
    """
    if not text:
        return {"observed": [], "forecast": []}

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tz_label: str | None = None

    for ln in lines:
        if "Forecast/Trend Issued:" in ln:
            parts = ln.split()
            if parts:
                tz_label = parts[-1]
            break

    observed: List[Dict[str, Any]] = []
    forecast: List[Dict[str, Any]] = []

    for ln in lines:
        # Skip obvious headers.
        if ln.startswith("SF ") or "Date/Time" in ln or ln.startswith("Observed"):
            continue
        parts = ln.split()
        if len(parts) < 4:
            continue
        # Observed block.
        o_date, o_time, o_stage_raw, o_flow_raw = parts[0], parts[1], parts[2], parts[3]
        o_dt = _parse_nwrfc_timestamp(o_date, o_time, tz_label)
        if o_dt is not None:
            try:
                o_stage = float(o_stage_raw)
            except Exception:
                o_stage = None
            try:
                o_flow = float(o_flow_raw)
            except Exception:
                o_flow = None
            observed.append(
                {
                    "ts": o_dt.isoformat(),
                    "stage": o_stage,
                    "flow": o_flow,
                }
            )

        # Forecast block may follow on the same line.
        if len(parts) >= 8:
            f_date, f_time, f_stage_raw, f_flow_raw = parts[4], parts[5], parts[6], parts[7]
            f_dt = _parse_nwrfc_timestamp(f_date, f_time, tz_label)
            if f_dt is not None:
                try:
                    f_stage = float(f_stage_raw)
                except Exception:
                    f_stage = None
                try:
                    f_flow = float(f_flow_raw)
                except Exception:
                    f_flow = None
                forecast.append(
                    {
                        "ts": f_dt.isoformat(),
                        "stage": f_stage,
                        "flow": f_flow,
                    }
                )

    observed.sort(key=lambda p: p["ts"])
    forecast.sort(key=lambda p: p["ts"])
    return {"observed": observed, "forecast": forecast}


def update_forecast_state(
    state: Dict[str, Any],
    gauge_id: str,
    points: List[Dict[str, Any]],
    now: datetime,
    horizon_hours: int,
) -> None:
    """
    Store forecast points and summary for a gauge, and compute basic bias stats
    using observed history when available.
    """
    if not points:
        return

    forecast_state = state.setdefault("forecast", {})
    g_forecast = forecast_state.setdefault(gauge_id, {})

    # De-duplicate by timestamp and trim to a reasonable time window around "now"
    # so we do not accumulate unbounded forecast history.
    by_ts: Dict[str, Dict[str, Any]] = {}
    for p in points:
        ts = p.get("ts")
        if isinstance(ts, str):
            by_ts[ts] = p
    ordered_ts = sorted(by_ts.keys())

    horizon_sec = horizon_hours * 3600 if horizon_hours > 0 else None
    trimmed_points: List[Dict[str, Any]] = []
    for ts in ordered_ts:
        p = by_ts[ts]
        dt = _parse_timestamp(ts)
        if dt is None:
            continue
        if horizon_sec is not None:
            delta = (dt - now).total_seconds()
            if delta > horizon_sec or delta < -horizon_sec:
                continue
        trimmed_points.append(p)

    g_forecast["points"] = trimmed_points

    summary = summarize_forecast_points(g_forecast["points"], now=now, horizon_hours=horizon_hours)
    g_forecast["summary"] = summary

    # Amplitude bias: compare last observed vs nearest forecast.
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    last_ts_str = g_state.get("last_timestamp")
    last_ts = _parse_timestamp(last_ts_str) if isinstance(last_ts_str, str) else None
    last_stage = g_state.get("last_stage")
    last_flow = g_state.get("last_flow")

    if last_ts is not None:
        nearest = None
        best_dt = None
        for p in g_forecast["points"]:
            ts_raw = p.get("ts")
            dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
            if dt is None:
                continue
            diff = abs((dt - last_ts).total_seconds())
            if best_dt is None or diff < best_dt:
                best_dt = diff
                nearest = p

        if nearest is not None:
            f_stage = nearest.get("stage")
            f_flow = nearest.get("flow")
            bias: Dict[str, Any] = {}
            if isinstance(last_stage, (int, float)) and isinstance(f_stage, (int, float)):
                bias["stage_delta"] = last_stage - f_stage
                bias["stage_ratio"] = (last_stage / f_stage) if f_stage not in (0, None) else None
            if isinstance(last_flow, (int, float)) and isinstance(f_flow, (int, float)):
                bias["flow_delta"] = last_flow - f_flow
                bias["flow_ratio"] = (last_flow / f_flow) if f_flow not in (0, None) else None
            if bias:
                g_forecast["bias"] = bias

    # Phase: compare forecast peak time vs observed recent peak time.
    history = (g_state.get("history") or [])[-HISTORY_LIMIT:]
    if history and summary.get("max_full", {}).get("ts"):
        forecast_peak_ts = _parse_timestamp(summary["max_full"]["ts"])
        if forecast_peak_ts is not None:
            # Observed peak over the same window.
            peak_obs_dt = None
            peak_obs_stage = None
            for entry in history:
                ts_raw = entry.get("ts")
                dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
                s = entry.get("stage")
                if dt is None or not isinstance(s, (int, float)):
                    continue
                if peak_obs_stage is None or s > peak_obs_stage:
                    peak_obs_stage = s
                    peak_obs_dt = dt

            if peak_obs_dt is not None:
                shift_sec = (peak_obs_dt - forecast_peak_ts).total_seconds()
                g_forecast["phase_shift_sec"] = shift_sec


def maybe_refresh_forecasts(state: Dict[str, Any], args: argparse.Namespace) -> None:
    """
    Refresh forecasts for all gauges at most once per FORECAST_REFRESH_MIN minutes.
    """
    base = getattr(args, "forecast_base", "") or ""
    if not base:
        return

    now = datetime.now(timezone.utc)
    meta = state.setdefault("meta", {})
    last_fetch_raw = meta.get("last_forecast_fetch")
    last_fetch = _parse_timestamp(last_fetch_raw) if isinstance(last_fetch_raw, str) else None
    if last_fetch is not None:
        age_sec = (now - last_fetch).total_seconds()
        if age_sec < FORECAST_REFRESH_MIN * 60:
            return

    for gauge_id, site_no in SITE_MAP.items():
        points = fetch_forecast_series(base, gauge_id, site_no, args.forecast_hours)
        if points:
            update_forecast_state(state, gauge_id, points, now=now, horizon_hours=args.forecast_hours)

    meta["last_forecast_fetch"] = now.isoformat()


def update_nwrfc_state(
    state: Dict[str, Any],
    gauge_id: str,
    series: Dict[str, List[Dict[str, Any]]],
    now: datetime,
) -> None:
    """
    Store NW RFC observed/forecast series for a gauge and compute simple
    differences vs the latest USGS observation when timestamps align.
    """
    observed = series.get("observed") or []
    forecast = series.get("forecast") or []
    if not observed and not forecast:
        return

    nwrfc_state = state.setdefault("nwrfc", {})
    g_nwrfc = nwrfc_state.setdefault(gauge_id, {})
    g_nwrfc["observed"] = observed
    g_nwrfc["forecast"] = forecast
    g_nwrfc["last_fetch_at"] = now.isoformat()

    # Cross-check against the latest USGS observation at the same timestamp.
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    last_ts_str = g_state.get("last_timestamp")
    last_ts = _parse_timestamp(last_ts_str) if isinstance(last_ts_str, str) else None
    if last_ts is None:
        return

    # Find NW RFC point with matching timestamp.
    match = None
    for p in reversed(observed):
        ts_raw = p.get("ts")
        dt = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        if dt is not None and dt == last_ts:
            match = p
            break

    if match is None:
        return

    usgs_stage = g_state.get("last_stage")
    usgs_flow = g_state.get("last_flow")
    nwrfc_stage = match.get("stage")
    nwrfc_flow = match.get("flow")

    diff: Dict[str, Any] = {"ts": last_ts.isoformat()}
    if isinstance(usgs_stage, (int, float)) and isinstance(nwrfc_stage, (int, float)):
        diff["stage_delta"] = usgs_stage - nwrfc_stage
    if isinstance(usgs_flow, (int, float)) and isinstance(nwrfc_flow, (int, float)):
        diff["flow_delta"] = usgs_flow - nwrfc_flow
    if len(diff) > 1:
        g_nwrfc["diff_vs_usgs"] = diff


def maybe_refresh_nwrfc(state: Dict[str, Any], args: argparse.Namespace) -> None:
    """
    Optionally cross-check observed stage/flow against NW RFC textPlot
    output for supported stations (currently GARW1).
    """
    enabled = getattr(args, "nwrfc_text", False)
    if not enabled:
        return

    now = datetime.now(timezone.utc)
    meta = state.setdefault("meta", {})
    last_fetch_raw = meta.get("last_nwrfc_fetch")
    last_fetch = _parse_timestamp(last_fetch_raw) if isinstance(last_fetch_raw, str) else None
    if last_fetch is not None:
        age_sec = (now - last_fetch).total_seconds()
        if age_sec < NWRFC_REFRESH_MIN * 60:
            return

    for gauge_id, nwrfc_id in NWRFC_ID_MAP.items():
        params = {"id": nwrfc_id, "pe": "HG", "bt": "on"}
        try:
            resp = requests.get(NWRFC_TEXT_BASE, params=params, timeout=10.0)
            resp.raise_for_status()
            text = resp.text
        except Exception:
            continue
        series = parse_nwrfc_text(text)
        if series.get("observed") or series.get("forecast"):
            update_nwrfc_state(state, gauge_id, series, now=now)

    meta["last_nwrfc_fetch"] = now.isoformat()


def update_state_with_readings(
    state: Dict[str, Any],
    readings: Dict[str, Dict[str, Any]],
    poll_ts: datetime | None = None,
) -> Dict[str, bool]:
    """
    Update persisted state with latest observations and learn per-gauge cadence.
    Returns a dict of gauge_id -> bool indicating whether a new observation was seen.
    """
    seen_updates: Dict[str, bool] = {}
    gauges_state = state.setdefault("gauges", {})
    meta_state = state.setdefault("meta", {})
    now = poll_ts or datetime.now(timezone.utc)

    for gauge_id, reading in readings.items():
        observed_at: datetime | None = reading.get("observed_at")
        if observed_at is None:
            seen_updates[gauge_id] = False
            continue

        g_state = gauges_state.setdefault(gauge_id, {})
        prev_ts = _parse_timestamp(g_state.get("last_timestamp"))
        prev_poll_ts = _parse_timestamp(g_state.get("last_poll_ts"))
        prev_mean = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        last_delta = g_state.get("last_delta_sec")
        no_update_polls = g_state.get("no_update_polls", 0)
        is_update = False
        delta_sec: float | None = None

        # Only treat strictly newer observation timestamps as updates.
        if prev_ts is not None and observed_at <= prev_ts:
            # No new point; keep existing cadence and history as-is.
            seen_updates[gauge_id] = False
            # Still keep last known values in sync with the latest reading.
            stage_now = reading.get("stage")
            flow_now = reading.get("flow")
            g_state["last_stage"] = stage_now
            g_state["last_flow"] = flow_now

            # If this reading shares the same timestamp as our last stored
            # point (e.g., one parameter was updated slightly later by USGS),
            # refresh the last history entry so the table reflects the
            # latest stage/flow pair rather than freezing the older value.
            if prev_ts is not None and observed_at == prev_ts:
                history = g_state.get("history")
                if isinstance(history, list) and history:
                    last_entry = history[-1]
                    ts_str = last_entry.get("ts")
                    if isinstance(ts_str, str) and _parse_timestamp(ts_str) == observed_at:
                        if stage_now is not None:
                            last_entry["stage"] = stage_now
                        if flow_now is not None:
                            last_entry["flow"] = flow_now

            g_state["no_update_polls"] = int(no_update_polls) + 1
            # Record the time of this poll so future latency windows can use it
            # as the last "no-update" bound.
            g_state["last_poll_ts"] = now.isoformat()
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

            # If we have accumulated enough intervals and our learned mean
            # is still significantly shorter than the typical observed delta,
            # snap the mean upward toward the empirical average. This prevents
            # slow-updating gauges (e.g., hourly) from being biased for too
            # long by the short initial prior.
            if len(deltas) >= 3:
                avg_delta = sum(deltas) / len(deltas)
                mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
                if isinstance(mean_interval, (int, float)) and mean_interval < 0.75 * avg_delta:
                    mean_interval = max(MIN_UPDATE_GAP_SEC, min(avg_delta, MAX_LEARNABLE_INTERVAL_SEC))
                    g_state["mean_interval_sec"] = mean_interval

            # Latency window: when did this observation appear in the API?
            # Lower bound: last poll where it was *not* yet visible.
            # Upper bound: this poll where it *is* visible.
            lower = 0.0
            if prev_poll_ts is not None:
                lower = max(0.0, (prev_poll_ts - observed_at).total_seconds())
            upper = max(0.0, (now - observed_at).total_seconds())

            lat_l = g_state.setdefault("latency_lower_sec", [])
            lat_u = g_state.setdefault("latency_upper_sec", [])
            lat_l.append(lower)
            lat_u.append(upper)
            if len(lat_l) > HISTORY_LIMIT:
                del lat_l[0 : len(lat_l) - HISTORY_LIMIT]
            if len(lat_u) > HISTORY_LIMIT:
                del lat_u[0 : len(lat_u) - HISTORY_LIMIT]

            # Use the midpoints as our primary latency samples.
            midpoints = g_state.setdefault("latencies_sec", [])
            mid = 0.5 * (lower + upper)
            midpoints.append(mid)
            if len(midpoints) > HISTORY_LIMIT:
                del midpoints[0 : len(midpoints) - HISTORY_LIMIT]

            # Robust location/scale (median and MAD) on midpoints.
            values = sorted(midpoints)
            n = len(values)
            if n:
                if n % 2 == 1:
                    median = values[n // 2]
                else:
                    median = 0.5 * (values[n // 2 - 1] + values[n // 2])
                devs = [abs(v - median) for v in values]
                devs.sort()
                if devs:
                    if n % 2 == 1:
                        mad = devs[n // 2]
                    else:
                        mad = 0.5 * (devs[n // 2 - 1] + devs[n // 2])
                else:
                    mad = 0.0
                g_state["latency_median_sec"] = median
                g_state["latency_mad_sec"] = mad

            # Reset the consecutive no-update counter now that we saw a new point.
            g_state["no_update_polls"] = 0

        # Record the time of this poll for future latency windows.
        g_state["last_poll_ts"] = now.isoformat()

        seen_updates[gauge_id] = is_update

    meta_state["last_update_run"] = datetime.now(timezone.utc).isoformat()

    return seen_updates


def predict_next_poll(state: Dict[str, Any], now: datetime) -> datetime:
    """
    Legacy helper retained for compatibility; delegates to the
    latency-aware scheduler.
    """
    return schedule_next_poll(state, now, MIN_RETRY_SEC)


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

    # Predict the next observation time.
    delta_since_last = (now - last_ts).total_seconds()
    if delta_since_last <= 0:
        # We are viewing the world at or before the last observation timestamp;
        # the next observation is one cadence step ahead.
        next_obs = last_ts + timedelta(seconds=mean_interval)
    elif delta_since_last <= 2 * mean_interval:
        # We are within roughly one cadence interval of the last observation.
        # Do not "skip" the next expected update just because we are slightly
        # late relative to the nominal cadence; assume the immediate next
        # observation is still pending.
        next_obs = last_ts + timedelta(seconds=mean_interval)
    else:
        # We are far beyond the last observation; assume we may have missed
        # one or more intervals (e.g., the process was offline) and advance
        # by enough whole intervals that the next prediction lies in the future.
        multiples = max(1, math.ceil(delta_since_last / mean_interval))
        next_obs = last_ts + timedelta(seconds=mean_interval * multiples)

    # Add a robust latency estimate (time from observation to appearance in API).
    latency_med = g_state.get("latency_median_sec", 0.0)
    if not isinstance(latency_med, (int, float)) or latency_med < 0:
        latency_med = 0.0

    return next_obs + timedelta(seconds=latency_med)


def schedule_next_poll(
    state: Dict[str, Any],
    now: datetime,
    min_retry_seconds: int,
) -> datetime:
    """
    Choose the next time to poll USGS IV, using:
    - Per-gauge observation cadence (mean_interval_sec)
    - Per-gauge latency stats (median & MAD)
    - A two-regime strategy:
      * Coarse polling far from the expected update time.
      * Short bursts of finer polling inside a narrow latency window
        for gauges with stable, low-variance latency.
    This function governs *normal* cadence; error backoff is handled separately.
    """
    gauges_state = state.get("gauges", {})
    if not isinstance(gauges_state, dict) or not gauges_state:
        return now + timedelta(seconds=DEFAULT_INTERVAL_SEC)

    best_time: datetime | None = None

    for gauge_id in SITE_MAP.keys():
        g_state = gauges_state.get(gauge_id, {})
        if not isinstance(g_state, dict):
            continue

        last_ts = _parse_timestamp(g_state.get("last_timestamp"))
        mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        if last_ts is None or not isinstance(mean_interval, (int, float)) or mean_interval <= 0:
            continue

        mean_interval = max(MIN_UPDATE_GAP_SEC, min(mean_interval, MAX_LEARNABLE_INTERVAL_SEC))
        next_api = predict_gauge_next(state, gauge_id, now)
        if next_api is None:
            continue

        latency_mad = g_state.get("latency_mad_sec")
        fine_eligible = (
            isinstance(latency_mad, (int, float))
            and latency_mad > 0
            and latency_mad <= FINE_LATENCY_MAD_MAX_SEC
            and mean_interval <= 3600
        )

        if fine_eligible:
            lat_width = max(FINE_WINDOW_MIN_SEC, 2.0 * float(latency_mad))
            fine_start = next_api - timedelta(seconds=lat_width)
            fine_end = next_api + timedelta(seconds=lat_width)

            if fine_start <= now <= fine_end:
                # Inside the fine window: poll more frequently, but only as
                # fast as the latency stability justifies.
                fine_step = max(
                    FINE_STEP_MIN_SEC,
                    min(FINE_STEP_MAX_SEC, lat_width / 4.0),
                )
                candidate = now + timedelta(seconds=fine_step)
            else:
                # Coarse region around a known fine window: walk towards it.
                coarse_step = max(
                    min_retry_seconds,
                    min(COARSE_STEP_MAX_SEC, mean_interval * COARSE_STEP_FRACTION),
                )
                target = fine_start if now < fine_start else next_api
                candidate = max(
                    now + timedelta(seconds=min_retry_seconds),
                    min(target - timedelta(seconds=HEADSTART_SEC), now + timedelta(seconds=coarse_step)),
                )
        else:
            # No stable latency yet: use a simple coarse strategy around
            # the predicted next API time.
            coarse_step = max(
                min_retry_seconds,
                min(COARSE_STEP_MAX_SEC, mean_interval * COARSE_STEP_FRACTION),
            )
            candidate = max(
                now + timedelta(seconds=min_retry_seconds),
                min(next_api - timedelta(seconds=HEADSTART_SEC), now + timedelta(seconds=coarse_step)),
            )

        if candidate <= now:
            candidate = now + timedelta(seconds=min_retry_seconds)
        if best_time is None or candidate < best_time:
            best_time = candidate

    if best_time is None:
        best_time = now + timedelta(seconds=DEFAULT_INTERVAL_SEC)

    return best_time


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
    header = (
        f"{'Gauge':<6} "
        f"{'Stage(ft)':>9} "
        f"{'Flow(cfs)':>10} "
        f"{'Status':<12} "
        f"{'Observed':>9} "
        f"{'Next ETA':>9}"
    )
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

        stage_str = f"{stage:.2f}" if isinstance(stage, (int, float)) else "--"
        flow_str = f"{int(flow):d}" if isinstance(flow, (int, float)) else "--"
        obs_str = _fmt_clock(observed_at)
        next_str = _fmt_rel(now, next_eta) if next_eta and next_eta >= now else "now"

        print(
            f"{gauge_id:<6s} "
            f"{stage_str:>9s} "
            f"{flow_str:>10s} "
            f"{status:<12s} "
            f"{obs_str:>9s} "
            f"{next_str:>9s}"
        )


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

        stdscr.addstr(0, 0, title[:max_x - 1], curses.A_BOLD | palette.get("title", 0))
        stdscr.addstr(1, 0, clock_line[:max_x - 1], palette.get("normal", 0))

        table_start = 3
        header = (
            f"{'Gauge':<6} "
            f"{'Stage(ft)':>9} "
            f"{'Flow(cfs)':>10} "
            f"{'Status':<11} "
            f"{'Observed':>9} "
            f"{'Next ETA':>9}"
        )
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
            next_str = _fmt_rel(now, next_eta) if next_eta and next_eta >= now else "now"

            line = (
                f"{gauge_id:<6s} "
                f"{stage_str:>9s} "
                f"{flow_str:>10s} "
                f"{status:<11s} "
                f"{obs_str:>9s} "
                f"{next_str:>9s}"
            )
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
                f"Next ETA: {_fmt_rel(now, next_eta) if next_eta and next_eta >= now else 'now'}"
            )
            stdscr.addstr(detail_y, 0, detail[:max_x - 1], palette.get("normal", 0) | curses.A_BOLD)
            stdscr.addstr(detail_y + 1, 0, timing[:max_x - 1], palette.get("normal", 0))

            if detail_mode:
                # Expanded detail: table of recent updates with per-update deltas.
                history = g_state.get("history", []) or []
                recent = history[-6:]
                table_y = detail_y + 3
                if table_y < max_y - 2:
                    header_line = (
                        f"{'Time':>8}  "
                        f"{'Stage':>8} "
                        f"{'ΔStage':>8} "
                        f"{'Flow':>8} "
                        f"{'ΔFlow':>8}"
                    )
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
                        stage_str = f"{stage_v:8.2f}" if isinstance(stage_v, (int, float)) else "      --"
                        ds_str = f"{ds:+8.2f}" if isinstance(ds, (int, float)) else "      --"
                        flow_str = f"{int(flow_v):8d}" if isinstance(flow_v, (int, float)) else "      --"
                        df_str = f"{int(df):+8d}" if isinstance(df, (int, float)) else "      --"
                        line = f"{ts_str:>8s}  {stage_str} {ds_str} {flow_str} {df_str}"
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
                            times.append(dt)
                            s = entry.get("stage")
                            f = entry.get("flow")
                            if isinstance(s, (int, float)):
                                stages.append(float(s))
                            if isinstance(f, (int, float)):
                                flows.append(float(f))

                        if len(times) >= 2:
                            dh_hours = (times[-1] - times[0]).total_seconds() / 3600.0 or 1.0
                        else:
                            dh_hours = 1.0

                        if len(stages) >= 2:
                            stage_trend = (stages[-1] - stages[0]) / dh_hours
                        else:
                            stage_trend = 0.0

                        if len(flows) >= 2:
                            flow_trend = (flows[-1] - flows[0]) / max(dh_hours, 1e-6)
                        else:
                            flow_trend = 0.0

                        trend_line = f"Trend: stage {stage_trend:+.2f} ft/h   flow {flow_trend:+.0f} cfs/h"
                        if row_y < max_y - 2:
                            stdscr.addstr(row_y, 0, trend_line[:max_x - 1], palette.get("dim", 0))
                            row_y += 1

                    # Latency stats.
                    latency_med = g_state.get("latency_median_sec")
                    latency_mad = g_state.get("latency_mad_sec")
                    if isinstance(latency_med, (int, float)) and row_y < max_y - 2:
                        lm = int(round(latency_med))
                        ls = int(round(latency_mad)) if isinstance(latency_mad, (int, float)) else 0
                        lat_line = f"Latency (obs→API): median {lm}s, MAD {ls}s"
                        stdscr.addstr(row_y, 0, lat_line[:max_x - 1], palette.get("dim", 0))
                        row_y += 1

                    # NW RFC cross-check (if available).
                    nwrfc_all = state.get("nwrfc", {}).get(selected, {})
                    diff = nwrfc_all.get("diff_vs_usgs") if isinstance(nwrfc_all, dict) else None
                    if diff and row_y < max_y - 2:
                        sd = diff.get("stage_delta")
                        qd = diff.get("flow_delta")
                        sd_str = f"{sd:+.2f} ft" if isinstance(sd, (int, float)) else "n/a"
                        qd_str = f"{qd:+.0f} cfs" if isinstance(qd, (int, float)) else "n/a"
                        line = f"NW RFC vs USGS (last): Δstage {sd_str}, Δflow {qd_str}"
                        stdscr.addstr(row_y, 0, line[:max_x - 1], palette.get("dim", 0))
                        row_y += 1

                    # Forecast summary (if available).
                    forecast_all = state.get("forecast", {}).get(selected, {})
                    summary = forecast_all.get("summary") if isinstance(forecast_all, dict) else None
                    bias = forecast_all.get("bias") if isinstance(forecast_all, dict) else None
                    phase_shift_sec = forecast_all.get("phase_shift_sec") if isinstance(forecast_all, dict) else None
                    if summary and row_y < max_y - 2:
                        def fmt_peak(key: str) -> str:
                            block = summary.get(key) or {}
                            s = block.get("stage")
                            q = block.get("flow")
                            s_str = f"{s:.2f} ft" if isinstance(s, (int, float)) else "--"
                            q_str = f"{int(q)} cfs" if isinstance(q, (int, float)) else "--"
                            return f"{s_str} / {q_str}"

                        line1 = (
                            f"Forecast peaks (stage/flow): "
                            f"3h {fmt_peak('max_3h')}  |  24h {fmt_peak('max_24h')}  |  full {fmt_peak('max_full')}"
                        )
                        stdscr.addstr(row_y, 0, line1[:max_x - 1], palette.get("dim", 0))
                        row_y += 1

                        if bias and row_y < max_y - 1:
                            sd = bias.get("stage_delta")
                            sr = bias.get("stage_ratio")
                            qd = bias.get("flow_delta")
                            qr = bias.get("flow_ratio")
                            sd_str = f"{sd:+.2f} ft" if isinstance(sd, (int, float)) else "n/a"
                            sr_str = f"{sr:.2f}×" if isinstance(sr, (int, float)) else "n/a"
                            qd_str = f"{qd:+.0f} cfs" if isinstance(qd, (int, float)) else "n/a"
                            qr_str = f"{qr:.2f}×" if isinstance(qr, (int, float)) else "n/a"
                            line2 = f"Vs forecast now: Δstage {sd_str} ({sr_str}), Δflow {qd_str} ({qr_str})"
                            stdscr.addstr(row_y, 0, line2[:max_x - 1], palette.get("dim", 0))
                            row_y += 1

                        if isinstance(phase_shift_sec, (int, float)) and row_y < max_y - 1:
                            hours = phase_shift_sec / 3600.0
                            sign = "earlier" if hours < 0 else "later"
                            line3 = f"Peak timing: {abs(hours):.2f} h {sign} than forecast"
                            stdscr.addstr(row_y, 0, line3[:max_x - 1], palette.get("dim", 0))
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
                "[↑/↓] select  [Enter] details  [c] toggle chart metric  [r] refresh  [f] force refetch  [q] quit  "
                f"Next fetch: {next_multi}  |  {status_msg}"
            )
            stdscr.addstr(footer_y, 0, footer[:max_x - 1], palette.get("dim", 0))

        info_y = footer_y + 1
        if 0 <= info_y < max_y:
            info_line = f"Mode: TUI adaptive | State: {args.state_file}"
            stdscr.addstr(info_y, 0, info_line[:max_x - 1], palette.get("dim", 0))

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
                    update_state_with_readings(state, readings, poll_ts=now)
                    maybe_refresh_forecasts(state, args)
                    maybe_refresh_nwrfc(state, args)
                    save_state(state_path, state)
                    next_poll_at = schedule_next_poll(
                        state,
                        datetime.now(timezone.utc),
                        args.min_retry_seconds,
                    )
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
            elif key in (ord("r"), ord("R"), ord("f"), ord("F")):
                next_poll_at = datetime.now(timezone.utc)
                if key in (ord("f"), ord("F")):
                    status_msg = "Forced refetch requested..."
                else:
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
        now = datetime.now(timezone.utc)
        if next_poll_at and next_poll_at > now:
            sleep_for = max(0.0, (next_poll_at - now).total_seconds())
            if sleep_for:
                time.sleep(sleep_for)
            now = datetime.now(timezone.utc)

        state.setdefault("meta", {})["last_fetch_at"] = now.isoformat()
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
        updates = update_state_with_readings(state, readings, poll_ts=now)
        maybe_refresh_forecasts(state, args)
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
        next_poll_at = schedule_next_poll(
            state,
            now,
            args.min_retry_seconds,
        )
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
        "--forecast-base",
        default="",
        help=(
            "URL template for NOAA/NWPS forecast API. "
            "May contain {gauge_id} and {site_no} placeholders; "
            "if empty, forecast integration is disabled."
        ),
    )
    parser.add_argument(
        "--forecast-hours",
        type=int,
        default=72,
        help="Forecast horizon (hours) to consider when summarizing peaks if forecast is enabled.",
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
    parser.add_argument(
        "--nwrfc-text",
        action="store_true",
        help=(
            "Enable NW RFC textPlot cross-check for supported gauges "
            "(currently GARW1) to compare observed stage/flow against USGS."
        ),
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

    update_state_with_readings(state, data, poll_ts=datetime.now(timezone.utc))
    maybe_refresh_forecasts(state, args)
    maybe_refresh_nwrfc(state, args)
    save_state(state_path, state)
    render_table(data, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
