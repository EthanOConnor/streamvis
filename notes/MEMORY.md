# MEMORY.md — streamvis

## 2025-12-09 – Initial agent pass

- `streamvis` is a single-file Python 3.10+ tool that:
  - Fetches multi-gauge Snoqualmie River observations from USGS NWIS IV.
  - Learns per-gauge update cadence via an EWMA of observed intervals.
  - Uses that cadence to schedule future polls just before the next expected update, aiming for ~1 HTTP call per true data update.
  - Persists learned cadence and a small rolling history per gauge to `~/.streamvis_state.json`.
- TUI mode is curses-based:
  - Top section: local + UTC time, one row per gauge with status, observed time, and next ETA.
  - Detail pane: selectable station, compact sparkline chart, and an expanded table view with per-update deltas and trends.
- Backfill support:
  - The tool can optionally backfill recent history from USGS IV (`--backfill-hours`) to seed cadence learning and charts, so adaptive polling starts “smart” on first run.
- Design constraint:
  - Polling must be “polite first”: default cadence mimics the data’s natural interval (starting at 8 minutes), and the system should never fall into a 1/minute polling regime except on deliberate manual refresh.

