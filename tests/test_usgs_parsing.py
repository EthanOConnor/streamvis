from __future__ import annotations

import unittest
from datetime import datetime, timezone

from streamvis.usgs import ogcapi, waterservices


class USGSParsingTests(unittest.TestCase):
    def test_waterservices_parse_latest_payload(self) -> None:
        site_map = {"TANW1": "12141300"}
        payload = {
            "value": {
                "timeSeries": [
                    {
                        "sourceInfo": {"siteCode": [{"value": "12141300"}]},
                        "variable": {"variableCode": [{"value": "00065"}]},
                        "values": [
                            {
                                "value": [
                                    {
                                        "value": "10.5",
                                        "dateTime": "2025-01-01T00:00:00.000-08:00",
                                    }
                                ]
                            }
                        ],
                    },
                    {
                        "sourceInfo": {"siteCode": [{"value": "12141300"}]},
                        "variable": {"variableCode": [{"value": "00060"}]},
                        "values": [
                            {
                                "value": [
                                    {
                                        "value": "1000",
                                        "dateTime": "2025-01-01T00:00:00.000-08:00",
                                    }
                                ]
                            }
                        ],
                    },
                ]
            }
        }
        readings = waterservices.parse_latest_payload(payload, site_map)
        self.assertIn("TANW1", readings)
        self.assertAlmostEqual(readings["TANW1"]["stage"], 10.5)
        self.assertAlmostEqual(readings["TANW1"]["flow"], 1000.0)
        obs = readings["TANW1"]["observed_at"]
        self.assertIsInstance(obs, datetime)
        if isinstance(obs, datetime):
            self.assertEqual(obs.tzinfo, timezone.utc)

    def test_waterservices_parse_history_payload_merges_params(self) -> None:
        site_map = {"TANW1": "12141300"}
        payload = {
            "value": {
                "timeSeries": [
                    {
                        "sourceInfo": {"siteCode": [{"value": "12141300"}]},
                        "variable": {"variableCode": [{"value": "00065"}]},
                        "values": [
                            {
                                "value": [
                                    {"value": "10.0", "dateTime": "2025-01-01T00:00:00Z"},
                                    {"value": "10.1", "dateTime": "2025-01-01T00:15:00Z"},
                                ]
                            }
                        ],
                    },
                    {
                        "sourceInfo": {"siteCode": [{"value": "12141300"}]},
                        "variable": {"variableCode": [{"value": "00060"}]},
                        "values": [
                            {
                                "value": [
                                    {"value": "900", "dateTime": "2025-01-01T00:00:00Z"},
                                    {"value": "950", "dateTime": "2025-01-01T00:15:00Z"},
                                ]
                            }
                        ],
                    },
                ]
            }
        }
        hist = waterservices.parse_history_payload(payload, site_map)
        self.assertIn("TANW1", hist)
        self.assertEqual(len(hist["TANW1"]), 2)
        first = hist["TANW1"][0]
        self.assertEqual(first["ts"], "2025-01-01T00:00:00Z")
        self.assertAlmostEqual(first["stage"], 10.0)
        self.assertAlmostEqual(first["flow"], 900.0)

    def test_ogcapi_parse_latest_payload(self) -> None:
        site_map = {"TANW1": "12141300"}
        payload = {
            "features": [
                {
                    "properties": {
                        "monitoringLocationId": "USGS-12141300",
                        "parameterCode": "00065",
                        "value": 10.5,
                        "phenomenonTime": "2025-01-01T08:00:00Z",
                    }
                },
                {
                    "properties": {
                        "monitoringLocationId": "USGS-12141300",
                        "parameterCode": "00060",
                        "value": 1000.0,
                        "phenomenonTime": "2025-01-01T08:00:00Z",
                    }
                },
            ]
        }
        readings = ogcapi.parse_latest_payload(payload, site_map)
        self.assertIn("TANW1", readings)
        self.assertAlmostEqual(readings["TANW1"]["stage"], 10.5)
        self.assertAlmostEqual(readings["TANW1"]["flow"], 1000.0)
        self.assertIsInstance(readings["TANW1"]["observed_at"], datetime)

    def test_ogcapi_parse_history_payload(self) -> None:
        site_map = {"TANW1": "12141300"}
        payload = {
            "features": [
                {
                    "properties": {
                        "monitoringLocationId": "USGS-12141300",
                        "parameterCode": "00065",
                        "value": 10.0,
                        "phenomenonTime": "2025-01-01T00:00:00Z",
                    }
                },
                {
                    "properties": {
                        "monitoringLocationId": "USGS-12141300",
                        "parameterCode": "00060",
                        "value": 900.0,
                        "phenomenonTime": "2025-01-01T00:00:00Z",
                    }
                },
            ]
        }
        hist = ogcapi.parse_history_payload(payload, site_map)
        self.assertIn("TANW1", hist)
        self.assertEqual(len(hist["TANW1"]), 1)
        pt = hist["TANW1"][0]
        self.assertEqual(pt["ts"], "2025-01-01T00:00:00Z")
        self.assertAlmostEqual(pt["stage"], 10.0)
        self.assertAlmostEqual(pt["flow"], 900.0)


if __name__ == "__main__":
    unittest.main()

