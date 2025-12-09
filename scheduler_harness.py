#!/usr/bin/env python3

from __future__ import annotations

"""
Lightweight synthetic harness for the adaptive scheduler and cadence learner.

Run with:

    python scheduler_harness.py

from the project root (after activating your virtualenv).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import streamvis as sv


def _make_gauge_state(
    last_obs: datetime,
    mean_interval_sec: float,
    latency_median_sec: float | None = None,
    latency_mad_sec: float | None = None,
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "last_timestamp": last_obs.isoformat(),
        "mean_interval_sec": mean_interval_sec,
        "last_stage": 10.0,
        "last_flow": 1000.0,
    }
    if latency_median_sec is not None:
        state["latency_median_sec"] = latency_median_sec
    if latency_mad_sec is not None:
        state["latency_mad_sec"] = latency_mad_sec
    return state


def run_scenarios() -> None:
    now = datetime.now(timezone.utc)

    scenarios: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # Scenario 1: single 15-minute gauge with stable latency.
    last_obs = now - timedelta(minutes=15)
    scenarios["single_fast_stable"] = {
        "gauges": {
            "TANW1": _make_gauge_state(
                last_obs=last_obs,
                mean_interval_sec=15 * 60,
                latency_median_sec=60.0,
                latency_mad_sec=10.0,
            )
        },
        "meta": {},
    }

    # Scenario 2: single slow gauge (2-hour cadence).
    last_obs_slow = now - timedelta(hours=2)
    scenarios["single_slow"] = {
        "gauges": {
            "GARW1": _make_gauge_state(
                last_obs=last_obs_slow,
                mean_interval_sec=2 * 3600,
                latency_median_sec=120.0,
                latency_mad_sec=60.0,
            )
        },
        "meta": {},
    }

    # Scenario 3: mixed fast + slow gauges sharing one multi-gauge call.
    scenarios["mixed_fast_and_slow"] = {
        "gauges": {
            "SQUW1": _make_gauge_state(
                last_obs=last_obs,
                mean_interval_sec=15 * 60,
                latency_median_sec=45.0,
                latency_mad_sec=20.0,
            ),
            "CRNW1": _make_gauge_state(
                last_obs=last_obs_slow,
                mean_interval_sec=2 * 3600,
                latency_median_sec=90.0,
                latency_mad_sec=45.0,
            ),
        },
        "meta": {},
    }

    for name, state in scenarios.items():
        next_poll = sv.schedule_next_poll(state, now, sv.MIN_RETRY_SEC)
        delta_sec = (next_poll - now).total_seconds()
        print(f"[{name}] next_poll in {delta_sec:.1f}s at {next_poll.isoformat()}")

        # Also show per-gauge predicted next API-visible time for intuition.
        for gauge_id in sorted(state["gauges"].keys()):
            next_api = sv.predict_gauge_next(state, gauge_id, now)
            if next_api is None:
                continue
            api_delta = (next_api - now).total_seconds()
            print(f"  - {gauge_id}: next_api in {api_delta:.1f}s at {next_api.isoformat()}")

        print()


def main() -> None:
    run_scenarios()


if __name__ == "__main__":
    main()

