from __future__ import annotations

import unittest

from streamvis import state as sv_state
from streamvis import tui as sv_tui


class NearbyOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._site_map = dict(sv_tui.SITE_MAP)

    def tearDown(self) -> None:
        sv_tui.SITE_MAP.clear()
        sv_tui.SITE_MAP.update(self._site_map)

    def test_compute_table_gauges_groups_nearby_at_bottom(self) -> None:
        sv_tui.SITE_MAP.clear()
        sv_tui.SITE_MAP.update({"A": "1", "B": "2", "C": "3"})

        state = {"gauges": {}, "meta": {"nearby_enabled": True, "nearby_gauges": ["B", "A", "B"]}}
        gauges, divider = sv_tui.compute_table_gauges(state)  # type: ignore[attr-defined]
        self.assertEqual(gauges, ["C", "B", "A"])
        self.assertEqual(divider, 1)

    def test_compute_table_gauges_no_nearby(self) -> None:
        sv_tui.SITE_MAP.clear()
        sv_tui.SITE_MAP.update({"A": "1", "B": "2"})

        state = {"gauges": {}, "meta": {"nearby_enabled": False, "nearby_gauges": ["B"]}}
        gauges, divider = sv_tui.compute_table_gauges(state)  # type: ignore[attr-defined]
        self.assertEqual(gauges, ["A", "B"])
        self.assertIsNone(divider)


class NearbyEvictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._site_map = dict(sv_tui.SITE_MAP)
        self._station_locations = dict(sv_tui.STATION_LOCATIONS)

    def tearDown(self) -> None:
        sv_tui.SITE_MAP.clear()
        sv_tui.SITE_MAP.update(self._site_map)
        sv_tui.STATION_LOCATIONS.clear()
        sv_tui.STATION_LOCATIONS.update(self._station_locations)

    def test_evict_dynamic_sites_removes_state_and_cache(self) -> None:
        state = {
            "gauges": {"U12345": {"mean_interval_sec": 900.0}, "A": {"mean_interval_sec": 900.0}},
            "meta": {
                "dynamic_sites": {"U12345": {"site_no": "99999999"}},
                "nearby_gauges": ["U12345", "A"],
                "nearby_search_ts": "2025-01-01T00:00:00+00:00",
            },
        }
        removed = sv_state.evict_dynamic_sites(state)
        self.assertEqual(removed, ["U12345"])
        self.assertNotIn("dynamic_sites", state["meta"])
        self.assertNotIn("nearby_search_ts", state["meta"])
        self.assertEqual(state["meta"].get("nearby_gauges"), ["A"])
        self.assertNotIn("U12345", state["gauges"])

    def test_toggle_nearby_off_evicts_new_dynamic_gauges(self) -> None:
        sv_tui.SITE_MAP.clear()
        sv_tui.SITE_MAP.update({"A": "1", "U12345": "99999999"})
        sv_tui.STATION_LOCATIONS.clear()
        sv_tui.STATION_LOCATIONS.update({"A": (0.0, 0.0), "U12345": (1.0, 1.0)})

        state = {
            "gauges": {"U12345": {"mean_interval_sec": 900.0}},
            "meta": {"nearby_enabled": True, "dynamic_sites": {"U12345": {"site_no": "99999999"}}},
        }
        msg = sv_tui.toggle_nearby(state, None)
        self.assertIn("Nearby off", msg)
        self.assertFalse(state["meta"].get("nearby_enabled", True))
        self.assertNotIn("U12345", sv_tui.SITE_MAP)
        self.assertNotIn("U12345", sv_tui.STATION_LOCATIONS)
        self.assertNotIn("dynamic_sites", state["meta"])


if __name__ == "__main__":
    unittest.main()

