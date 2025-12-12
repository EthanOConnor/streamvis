from __future__ import annotations

import unittest
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


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_site_map = sv.SITE_MAP

    def tearDown(self) -> None:
        sv.SITE_MAP = self._orig_site_map

    def test_coarse_step_scales_for_slow_gauge(self) -> None:
        sv.SITE_MAP = {"GARW1": "00000000"}
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        gauges = {
            "GARW1": _make_gauge_state(
                last_obs=now,
                mean_interval_sec=2 * 3600,
                latency_median_sec=120.0,
                latency_mad_sec=60.0,
            )
        }
        state = {"gauges": gauges, "meta": {}}
        next_poll = sv.schedule_next_poll(state, now, sv.MIN_RETRY_SEC)
        self.assertEqual(next_poll, now + timedelta(hours=1))

    def test_coarse_step_allows_very_slow_gauge(self) -> None:
        sv.SITE_MAP = {"GARW1": "00000000"}
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        gauges = {
            "GARW1": _make_gauge_state(
                last_obs=now,
                mean_interval_sec=6 * 3600,
                latency_median_sec=0.0,
                latency_mad_sec=120.0,
            )
        }
        state = {"gauges": gauges, "meta": {}}
        next_poll = sv.schedule_next_poll(state, now, sv.MIN_RETRY_SEC)
        self.assertEqual(next_poll, now + timedelta(hours=3))

    def test_fine_window_uses_min_step_when_inside(self) -> None:
        sv.SITE_MAP = {"TANW1": "00000000"}
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mean_interval = 15 * 60
        latency_med = 60.0
        latency_mad = 10.0
        last_obs = now - timedelta(seconds=mean_interval + latency_med)
        gauges = {
            "TANW1": _make_gauge_state(
                last_obs=last_obs,
                mean_interval_sec=mean_interval,
                latency_median_sec=latency_med,
                latency_mad_sec=latency_mad,
            )
        }
        state = {"gauges": gauges, "meta": {}}
        next_poll = sv.schedule_next_poll(state, now, sv.MIN_RETRY_SEC)
        self.assertAlmostEqual(
            (next_poll - now).total_seconds(),
            sv.FINE_STEP_MIN_SEC,
            delta=0.01,
        )

    def test_cadence_snaps_up_after_three_long_deltas(self) -> None:
        sv.SITE_MAP = {"GARW1": "00000000"}
        state: Dict[str, Any] = {"gauges": {}, "meta": {}}
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(4):
            ts = start + timedelta(hours=i)
            readings = {
                "GARW1": {
                    "stage": 10.0,
                    "flow": 1000.0,
                    "status": "NORMAL",
                    "observed_at": ts,
                }
            }
            sv.update_state_with_readings(state, readings, poll_ts=ts)

        mean_interval = state["gauges"]["GARW1"]["mean_interval_sec"]
        self.assertEqual(mean_interval, 3600.0)

    def test_backfill_snaps_to_30min_multiple(self) -> None:
        sv.SITE_MAP = {"GARW1": "00000000"}
        state: Dict[str, Any] = {"gauges": {}, "meta": {}}
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        points = []
        for i in range(5):
            ts = start + timedelta(minutes=30 * i)
            points.append({"ts": ts.isoformat(), "stage": 10.0, "flow": 1000.0})
        sv.backfill_state_with_history(state, {"GARW1": points})
        g_state = state["gauges"]["GARW1"]
        self.assertEqual(g_state["mean_interval_sec"], 1800.0)
        self.assertEqual(g_state.get("cadence_mult"), 2)

    def test_estimator_handles_missed_updates(self) -> None:
        deltas = [900.0, 1800.0, 2700.0, 900.0]
        k, fit = sv._estimate_cadence_multiple(deltas)  # type: ignore[attr-defined]
        self.assertEqual(k, 1)
        self.assertGreaterEqual(fit, sv.CADENCE_FIT_THRESHOLD)

    def test_irregular_deltas_do_not_snap(self) -> None:
        sv.SITE_MAP = {"GARW1": "00000000"}
        state: Dict[str, Any] = {"gauges": {}, "meta": {}}
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(4):
            ts = start + timedelta(minutes=20 * i)
            readings = {
                "GARW1": {
                    "stage": 10.0,
                    "flow": 1000.0,
                    "status": "NORMAL",
                    "observed_at": ts,
                }
            }
            sv.update_state_with_readings(state, readings, poll_ts=ts)

        g_state = state["gauges"]["GARW1"]
        self.assertNotIn("cadence_mult", g_state)
        mean_interval = g_state["mean_interval_sec"]
        self.assertTrue(900.0 < mean_interval < 1800.0)
        self.assertAlmostEqual(mean_interval, 1200.0, delta=250.0)

    def test_parse_usgs_site_rdb_basic(self) -> None:
        text = (
            "# comment\n"
            "agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va\n"
            "5s\t15s\t50s\t10s\t10s\n"
            "USGS\t12141300\tTest River\t47.5\t-121.6\n"
        )
        sites = sv._parse_usgs_site_rdb(text)  # type: ignore[attr-defined]
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]["site_no"], "12141300")
        self.assertAlmostEqual(sites[0]["lat"], 47.5)

    def test_dynamic_gauge_id_collision(self) -> None:
        gid1 = sv._dynamic_gauge_id("12345678", ["U5678", "U56781"])  # type: ignore[attr-defined]
        self.assertTrue(gid1.startswith("U"))

    def test_iso8601_duration(self) -> None:
        self.assertEqual(sv._iso8601_duration(30), "PT30S")  # type: ignore[attr-defined]
        self.assertEqual(sv._iso8601_duration(1800), "PT30M")  # type: ignore[attr-defined]
        self.assertEqual(sv._iso8601_duration(5400), "PT1H30M")  # type: ignore[attr-defined]

    def test_compute_modified_since_gating(self) -> None:
        state = {
            "gauges": {
                "A": {"mean_interval_sec": 900},
                "B": {"mean_interval_sec": 1800},
            }
        }
        ms = sv._compute_modified_since(state)  # type: ignore[attr-defined]
        self.assertEqual(ms, "PT30M")
        slow_state = {"gauges": {"A": {"mean_interval_sec": 7200}}}
        self.assertIsNone(sv._compute_modified_since(slow_state))  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
