from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from streamvis.usgs import adapter


class USGSAdapterTests(unittest.TestCase):
    def test_blended_merge_prefers_newer_observation(self) -> None:
        site_map = {"TANW1": "12141300"}
        older = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        newer = datetime(2025, 1, 1, 0, 15, 0, tzinfo=timezone.utc)

        ws_readings = {"TANW1": {"stage": 10.0, "flow": 900.0, "observed_at": older}}
        ogc_readings = {"TANW1": {"stage": 10.2, "flow": 950.0, "observed_at": newer}}

        with patch.object(adapter.waterservices, "fetch_latest", return_value=(ws_readings, 50.0)):
            with patch.object(adapter.ogcapi, "fetch_latest", return_value=(ogc_readings, 30.0)):
                readings, meta = adapter.fetch_gauge_data(site_map, {}, backend=adapter.USGSBackend.BLENDED)

        self.assertIn("TANW1", readings)
        self.assertAlmostEqual(readings["TANW1"]["stage"], 10.2)
        self.assertAlmostEqual(readings["TANW1"]["flow"], 950.0)
        self.assertEqual(readings["TANW1"]["observed_at"], newer)
        self.assertEqual(meta.get("api_backend"), "blended")
        self.assertIn(meta.get("last_backend_used"), ("blended", "waterservices", "ogc"))

    def test_blended_does_not_clobber_configured_backend(self) -> None:
        site_map = {"TANW1": "12141300"}
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        ogc_readings = {"TANW1": {"stage": 10.0, "flow": 900.0, "observed_at": now}}

        meta = {
            "api_backend": "blended",
            "waterservices": {"success_count": 25, "fail_count": 0, "latency_ewma_ms": 200.0, "latency_var_ewma_ms2": 0.0},
            "ogc": {"success_count": 25, "fail_count": 0, "latency_ewma_ms": 50.0, "latency_var_ewma_ms2": 0.0},
            "last_backend_probe_ts": "2025-01-01T00:00:00+00:00",
        }

        with patch.object(adapter, "_select_preferred_backend", return_value=adapter.USGSBackend.OGC):
            with patch.object(adapter, "_should_probe_alternate", return_value=False):
                with patch.object(adapter.ogcapi, "fetch_latest", return_value=(ogc_readings, 10.0)) as ogc_call:
                    with patch.object(adapter.waterservices, "fetch_latest") as ws_call:
                        readings, new_meta = adapter.fetch_gauge_data(
                            site_map, meta, backend=adapter.USGSBackend.BLENDED
                        )

        ogc_call.assert_called_once()
        ws_call.assert_not_called()
        self.assertTrue(readings)
        self.assertEqual(new_meta.get("api_backend"), "blended")
        self.assertEqual(new_meta.get("preferred_backend"), "ogc")
        self.assertEqual(new_meta.get("last_backend_used"), "ogc")


if __name__ == "__main__":
    unittest.main()

