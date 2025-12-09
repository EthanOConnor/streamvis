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

## 2025-12-09 – Forecast integration model

- Forecasts are treated as an optional overlay sourced from NOAA’s National Water Prediction Service (NWPS) via an operator-configurable `--forecast-base` URL template.
- The code assumes a generic “time, stage, flow” forecast series and:
  - Stores forecast points per gauge in state.
  - Computes forward-looking maxima for 3h, 24h, and the full configured horizon (`--forecast-hours`).
  - Compares the latest observation to the nearest forecast point to estimate amplitude bias (delta + ratio) for stage and flow.
  - Compares observed vs forecast peak times to estimate a simple phase shift (peak earlier/later than forecast).
- In TUI detail mode, forecast summaries are displayed only when forecast data is present, keeping the core UX clean when forecasts are disabled.
- We intentionally do not lock in a specific NWPS endpoint or JSON shape; operators are expected to align the URL template and parsing with NOAA’s current documentation while keeping our cadence and caching design intact.
