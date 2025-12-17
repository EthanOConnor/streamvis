"""
Adaptive scheduler for streamvis.

Implements the cadence learning and poll scheduling logic:
- Snaps observed update intervals to 15-minute grid
- Estimates phase offset within cadence period
- Two-regime polling: coarse far from expected update, fine near it
- Latency-aware prediction of next observation visibility
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List

from streamvis.constants import (
    CADENCE_BASE_SEC,
    CADENCE_SNAP_TOL_SEC,
    CADENCE_FIT_THRESHOLD,
    CADENCE_CLEAR_THRESHOLD,
    DEFAULT_INTERVAL_SEC,
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
from streamvis.utils import parse_timestamp, median


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
    return best_k, best_fit


def maybe_update_cadence_from_deltas(g_state: dict[str, Any]) -> None:
    """
    If recent deltas strongly support a 15-minute multiple cadence, snap the
    gauge's mean_interval_sec to that multiple and record confidence.
    If confidence falls below CADENCE_CLEAR_THRESHOLD, clear the multiple so
    the EWMA can adapt to irregular behavior.
    """
    history = g_state.get("history", [])
    if not isinstance(history, list) or len(history) < 4:
        return

    deltas: List[float] = []
    prev_ts: datetime | None = None
    for pt in history:
        ts = parse_timestamp(pt.get("ts"))
        if ts is not None and prev_ts is not None:
            delta = (ts - prev_ts).total_seconds()
            if delta > 0:
                deltas.append(delta)
        prev_ts = ts

    k, fit = estimate_cadence_multiple(deltas)
    if k is not None and fit >= CADENCE_FIT_THRESHOLD:
        g_state["cadence_mult"] = k
        g_state["cadence_fit"] = fit
        g_state["mean_interval_sec"] = float(k * CADENCE_BASE_SEC)
    elif fit < CADENCE_CLEAR_THRESHOLD and "cadence_mult" in g_state:
        del g_state["cadence_mult"]
        if "cadence_fit" in g_state:
            del g_state["cadence_fit"]


def estimate_phase_offset_sec(g_state: dict[str, Any]) -> float | None:
    """
    Estimate a stable phase offset for a snapped cadence.

    We treat observation timestamps as lying on a grid of period
    mean_interval_sec, and estimate the typical offset within that period.
    Returns phase in seconds within [0, cadence).
    """
    k = g_state.get("cadence_mult")
    if k is None or k < 1:
        return None
    cadence = float(k * CADENCE_BASE_SEC)

    history = g_state.get("history", [])
    if not isinstance(history, list) or len(history) < 3:
        return None

    offsets: List[float] = []
    for pt in history:
        ts = parse_timestamp(pt.get("ts"))
        if ts is None:
            continue
        epoch = ts.timestamp()
        offset = epoch % cadence
        offsets.append(offset)

    if len(offsets) < 3:
        return None

    # Circular median: unwrap offsets near 0/cadence boundary
    unwrapped = []
    for o in offsets:
        if o > cadence * 0.75:
            unwrapped.append(o - cadence)
        else:
            unwrapped.append(o)
    med = median(unwrapped)
    if med < 0:
        med += cadence
    return float(med)


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

    interval = g_state.get("mean_interval_sec")
    if not isinstance(interval, (int, float)) or interval <= 0:
        interval = DEFAULT_INTERVAL_SEC

    # Base prediction: last observation + interval
    base_next = last_ts + timedelta(seconds=interval)

    # Use phase offset if available
    phase = g_state.get("phase_offset_sec")
    k = g_state.get("cadence_mult")
    if phase is not None and k is not None and k >= 1:
        cadence = float(k * CADENCE_BASE_SEC)
        epoch = base_next.timestamp()
        current_phase = epoch % cadence
        shift = phase - current_phase
        if shift < -cadence / 2:
            shift += cadence
        elif shift > cadence / 2:
            shift -= cadence
        base_next = base_next + timedelta(seconds=shift)

    # Add latency estimate
    latency_loc = g_state.get("latency_loc_sec")
    if not isinstance(latency_loc, (int, float)) or latency_loc <= 0:
        latency_loc = LATENCY_PRIOR_LOC_SEC
    
    return base_next + timedelta(seconds=latency_loc)


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
    if not gauges_state:
        return now + timedelta(seconds=min_retry_seconds)

    earliest_poll: datetime | None = None
    min_coarse_step: float = float("inf")

    for gauge_id, g_state in gauges_state.items():
        if not isinstance(g_state, dict):
            continue

        last_ts = parse_timestamp(g_state.get("last_timestamp"))
        if last_ts is None:
            continue

        interval = g_state.get("mean_interval_sec")
        if not isinstance(interval, (int, float)) or interval <= 0:
            interval = DEFAULT_INTERVAL_SEC

        # Coarse step scales with interval
        coarse_step = max(min_retry_seconds, interval * COARSE_STEP_FRACTION)
        min_coarse_step = min(min_coarse_step, coarse_step)

        predicted = predict_gauge_next(state, gauge_id, now)
        if predicted is None:
            continue

        # Latency variance determines fine window eligibility
        latency_mad = g_state.get("latency_mad_sec")
        latency_scale = g_state.get("latency_scale_sec")
        if latency_scale is not None:
            effective_mad = latency_scale
        elif latency_mad is not None:
            effective_mad = latency_mad
        else:
            effective_mad = LATENCY_PRIOR_SCALE_SEC

        # Fine-window half-width
        fine_half = max(FINE_WINDOW_MIN_SEC, effective_mad * 2)
        window_start = predicted - timedelta(seconds=fine_half)
        window_end = predicted + timedelta(seconds=fine_half)

        if now < window_start:
            # Coarse regime: schedule based on coarse step or headstart
            next_coarse = min(
                window_start - timedelta(seconds=HEADSTART_SEC),
                now + timedelta(seconds=coarse_step),
            )
            if earliest_poll is None or next_coarse < earliest_poll:
                earliest_poll = next_coarse
        elif now <= window_end:
            # Fine regime: poll soon if latency is stable
            if effective_mad <= FINE_LATENCY_MAD_MAX_SEC:
                fine_step = min(FINE_STEP_MAX_SEC, max(FINE_STEP_MIN_SEC, effective_mad))
            else:
                fine_step = min_retry_seconds
            next_fine = now + timedelta(seconds=fine_step)
            if earliest_poll is None or next_fine < earliest_poll:
                earliest_poll = next_fine
        else:
            # Past the window: schedule based on next expected
            next_expected = predicted + timedelta(seconds=interval)
            if earliest_poll is None or next_expected < earliest_poll:
                earliest_poll = next_expected

    if earliest_poll is None:
        earliest_poll = now + timedelta(seconds=min_retry_seconds)
    
    # Never schedule in the past
    if earliest_poll < now:
        earliest_poll = now + timedelta(seconds=min_retry_seconds)

    return earliest_poll


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
