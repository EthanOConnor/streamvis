"""
Adaptive scheduler for streamvis.

Implements the cadence learning and poll scheduling logic:
- Snaps observed update intervals to 15-minute grid
- Estimates phase offset within cadence period
- Two-regime polling: coarse far from expected update, fine near it
- Latency-aware prediction of next observation visibility
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, List

from streamvis.constants import (
    CADENCE_BASE_SEC,
    CADENCE_SNAP_TOL_SEC,
    CADENCE_FIT_THRESHOLD,
    CADENCE_CLEAR_THRESHOLD,
    DEFAULT_INTERVAL_SEC,
    MIN_UPDATE_GAP_SEC,
    MAX_LEARNABLE_INTERVAL_SEC,
    HISTORY_LIMIT,
    MIN_RETRY_SEC,
    HEADSTART_SEC,
    FINE_LATENCY_MAD_MAX_SEC,
    FINE_WINDOW_MIN_SEC,
    FINE_STEP_MIN_SEC,
    FINE_STEP_MAX_SEC,
    COARSE_STEP_FRACTION,
    LATENCY_PRIOR_LOC_SEC,
    LATENCY_PRIOR_SCALE_SEC,
)
from streamvis.utils import parse_timestamp, median, tukey_biweight_location_scale


def snap_delta_to_cadence(delta_sec: float) -> tuple[float | None, int | None]:
    """
    Snap an observed update delta to the nearest 15-minute multiple.

    Returns (snapped_delta, k) where k is the multiple of CADENCE_BASE_SEC.
    If the delta is not close enough to a multiple, returns (None, None).
    """
    if delta_sec <= 0:
        return None, None
    k = int(round(delta_sec / CADENCE_BASE_SEC))
    if k < 1:
        return None, None
    snapped = float(k * CADENCE_BASE_SEC)
    if abs(snapped - delta_sec) <= CADENCE_SNAP_TOL_SEC:
        return snapped, k
    return None, None


def estimate_cadence_multiple(deltas_sec: List[float]) -> tuple[int | None, float]:
    """
    Estimate the underlying cadence multiple k (where cadence = k*CADENCE_BASE_SEC)
    from a list of observed deltas.

    The estimator is robust to missed updates: it chooses the largest k such that
    a high fraction of deltas are integer multiples of k.
    Returns (k, fit_fraction). k is None when confidence is low.
    """
    k_samples: List[int] = []
    for d in deltas_sec:
        snapped, k = snap_delta_to_cadence(d)
        if snapped is None or k is None:
            continue
        k_samples.append(k)

    if len(k_samples) < 3:
        return None, 0.0

    max_k = max(k_samples)
    best_k = 1
    best_fit = 0.0
    n = float(len(k_samples))
    for cand in range(1, max_k + 1):
        fit = sum(1 for k in k_samples if (k % cand) == 0) / n
        if fit > best_fit + 1e-9 or (abs(fit - best_fit) <= 1e-9 and cand > best_k):
            best_fit = fit
            best_k = cand
    if best_fit >= CADENCE_FIT_THRESHOLD:
        return best_k, best_fit
    return None, best_fit


def maybe_update_cadence_from_deltas(g_state: dict[str, Any]) -> None:
    """
    If recent deltas strongly support a 15-minute multiple cadence, snap the
    gauge's mean_interval_sec to that multiple and record confidence.
    If confidence falls below CADENCE_CLEAR_THRESHOLD, clear the multiple so
    the EWMA can adapt to irregular behavior.
    """
    deltas = g_state.get("deltas")
    clean: List[float] = []
    if isinstance(deltas, list) and deltas:
        clean = [
            float(d)
            for d in deltas
            if isinstance(d, (int, float)) and d >= MIN_UPDATE_GAP_SEC
        ]
    else:
        history = g_state.get("history", [])
        if not isinstance(history, list) or len(history) < 4:
            return
        prev_ts: datetime | None = None
        for pt in history:
            ts = parse_timestamp(pt.get("ts") if isinstance(pt, dict) else None)
            if ts is not None and prev_ts is not None:
                delta = (ts - prev_ts).total_seconds()
                if delta >= MIN_UPDATE_GAP_SEC:
                    clean.append(delta)
            prev_ts = ts

    if len(clean) < 3:
        return

    k, fit = estimate_cadence_multiple(clean[-HISTORY_LIMIT:])
    if k is not None and fit >= CADENCE_FIT_THRESHOLD:
        g_state["cadence_mult"] = k
        g_state["cadence_fit"] = fit
        g_state["mean_interval_sec"] = float(k * CADENCE_BASE_SEC)
    else:
        g_state["cadence_fit"] = float(fit)
        if fit < CADENCE_CLEAR_THRESHOLD and "cadence_mult" in g_state:
            g_state.pop("cadence_mult", None)


def estimate_phase_offset_sec(g_state: dict[str, Any]) -> float | None:
    """
    Estimate a stable phase offset for a snapped cadence.

    We treat observation timestamps as lying on a grid of period
    mean_interval_sec, and estimate the typical offset within that period.
    Returns phase in seconds within [0, cadence).
    """
    mean_interval = g_state.get("mean_interval_sec")
    if not isinstance(mean_interval, (int, float)) or mean_interval <= 0:
        return None
    cadence = float(mean_interval)

    cad_mult = g_state.get("cadence_mult")
    if not isinstance(cad_mult, int) or cad_mult <= 0:
        return None

    history = g_state.get("history", [])
    if not isinstance(history, list) or len(history) < 3:
        return None

    offsets: List[float] = []
    seed: float | None = None
    for pt in history[-HISTORY_LIMIT:]:
        if not isinstance(pt, dict):
            continue
        ts = parse_timestamp(pt.get("ts"))
        if ts is None:
            continue
        off = ts.timestamp() % cadence
        if seed is None:
            seed = off
        if seed is not None:
            if off - seed > cadence / 2:
                off -= cadence
            elif seed - off > cadence / 2:
                off += cadence
        offsets.append(off)

    if not offsets or seed is None:
        return None

    loc, scale = tukey_biweight_location_scale(
        offsets,
        initial_loc=float(seed),
        initial_scale=float(CADENCE_SNAP_TOL_SEC),
    )
    phase = float(loc % cadence)
    g_state["phase_offset_sec"] = phase
    g_state["phase_scale_sec"] = float(scale)
    return phase


def predict_gauge_next(
    state: dict[str, Any],
    gauge_id: str,
    now: datetime,
) -> datetime | None:
    """
    Predict when the next observation will appear for a single gauge.
    Returns None if insufficient data.
    """
    gauges_state = state.get("gauges", {})
    g_state = gauges_state.get(gauge_id, {})
    if not isinstance(g_state, dict):
        return None

    last_ts = parse_timestamp(g_state.get("last_timestamp"))
    if last_ts is None:
        return None

    mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
    if not isinstance(mean_interval, (int, float)) or mean_interval <= 0:
        mean_interval = DEFAULT_INTERVAL_SEC
    mean_interval = max(MIN_UPDATE_GAP_SEC, min(float(mean_interval), MAX_LEARNABLE_INTERVAL_SEC))
    cadence = float(mean_interval)

    phase = g_state.get("phase_offset_sec")
    if not isinstance(phase, (int, float)) or phase < 0 or phase >= cadence:
        phase = estimate_phase_offset_sec(g_state)

    if isinstance(phase, (int, float)):
        base_t = max(last_ts.timestamp(), now.timestamp())
        k = math.floor((base_t - float(phase)) / cadence) + 1
        next_obs_t = k * cadence + float(phase)
        next_obs = datetime.fromtimestamp(next_obs_t, tz=timezone.utc)
    else:
        delta_since_last = (now - last_ts).total_seconds()
        if delta_since_last <= 0 or delta_since_last <= 2 * cadence:
            next_obs = last_ts + timedelta(seconds=cadence)
        else:
            multiples = max(1, math.ceil(delta_since_last / cadence))
            next_obs = last_ts + timedelta(seconds=cadence * multiples)

    latency_loc = g_state.get("latency_loc_sec")
    if not isinstance(latency_loc, (int, float)) or latency_loc < 0:
        latency_loc = g_state.get("latency_median_sec", LATENCY_PRIOR_LOC_SEC)
    if not isinstance(latency_loc, (int, float)) or latency_loc < 0:
        latency_loc = LATENCY_PRIOR_LOC_SEC

    return next_obs + timedelta(seconds=float(latency_loc))


def schedule_next_poll(
    state: dict[str, Any],
    now: datetime,
    min_retry_seconds: int = MIN_RETRY_SEC,
) -> datetime:
    """
    Choose the next time to poll USGS IV, using:
    - Per-gauge observation cadence (mean_interval_sec)
    - Per-gauge latency stats (median & MAD)
    - A two-regime strategy:
      * Coarse polling far from the expected update time.
      * Fine-grained polling inside a tight window around the expected update
        for gauges with stable, low-variance latency.
    This function governs *normal* cadence; error backoff is handled separately.
    """
    gauges_state = state.get("gauges", {})
    if not isinstance(gauges_state, dict) or not gauges_state:
        return now + timedelta(seconds=DEFAULT_INTERVAL_SEC)

    best_time: datetime | None = None

    for gauge_id, g_state in gauges_state.items():
        if not isinstance(g_state, dict):
            continue

        last_ts = parse_timestamp(g_state.get("last_timestamp"))
        mean_interval = g_state.get("mean_interval_sec", DEFAULT_INTERVAL_SEC)
        if last_ts is None or not isinstance(mean_interval, (int, float)) or mean_interval <= 0:
            continue

        mean_interval = max(MIN_UPDATE_GAP_SEC, min(float(mean_interval), MAX_LEARNABLE_INTERVAL_SEC))
        next_api = predict_gauge_next(state, gauge_id, now)
        if next_api is None:
            continue

        latency_scale = g_state.get("latency_scale_sec")
        if not isinstance(latency_scale, (int, float)) or latency_scale < 0:
            latency_scale = g_state.get("latency_mad_sec")

        fine_eligible = (
            isinstance(latency_scale, (int, float))
            and latency_scale > 0
            and latency_scale <= FINE_LATENCY_MAD_MAX_SEC
            and mean_interval <= 3600
        )

        if fine_eligible:
            lat_width = max(FINE_WINDOW_MIN_SEC, 2.0 * float(latency_scale))
            fine_start = next_api - timedelta(seconds=lat_width)
            fine_end = next_api + timedelta(seconds=lat_width)

            if fine_start <= now <= fine_end:
                fine_step = max(
                    FINE_STEP_MIN_SEC,
                    min(FINE_STEP_MAX_SEC, lat_width / 4.0),
                )
                candidate = now + timedelta(seconds=float(fine_step))
            else:
                coarse_step = max(
                    min_retry_seconds,
                    mean_interval * COARSE_STEP_FRACTION,
                )
                target = fine_start if now < fine_start else next_api
                candidate = max(
                    now + timedelta(seconds=min_retry_seconds),
                    min(
                        target - timedelta(seconds=HEADSTART_SEC),
                        now + timedelta(seconds=float(coarse_step)),
                    ),
                )
        else:
            coarse_step = max(
                min_retry_seconds,
                mean_interval * COARSE_STEP_FRACTION,
            )
            candidate = max(
                now + timedelta(seconds=min_retry_seconds),
                min(
                    next_api - timedelta(seconds=HEADSTART_SEC),
                    now + timedelta(seconds=float(coarse_step)),
                ),
            )

        if candidate <= now:
            candidate = now + timedelta(seconds=min_retry_seconds)
        if best_time is None or candidate < best_time:
            best_time = candidate

    if best_time is None:
        best_time = now + timedelta(seconds=DEFAULT_INTERVAL_SEC)

    return best_time


def control_summary(state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """
    Build a concise per-gauge control summary for debugging/tuning.
    """
    gauges_state = state.get("gauges", {})
    summaries = []
    
    for gauge_id, g_state in gauges_state.items():
        if not isinstance(g_state, dict):
            continue
            
        predicted = predict_gauge_next(state, gauge_id, now)
        eta_sec = (predicted - now).total_seconds() if predicted else None
        
        summaries.append({
            "gauge_id": gauge_id,
            "interval_sec": g_state.get("mean_interval_sec"),
            "cadence_mult": g_state.get("cadence_mult"),
            "cadence_fit": g_state.get("cadence_fit"),
            "phase_offset_sec": g_state.get("phase_offset_sec"),
            "latency_loc_sec": g_state.get("latency_loc_sec"),
            "latency_scale_sec": g_state.get("latency_scale_sec"),
            "eta_sec": eta_sec,
        })
    
    return summaries
