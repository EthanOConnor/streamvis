# WORKLOG.md — streamvis

## 2025-12-09

- Initialized Git repository and project metadata.
- Hardened `streamvis.py` into a typed, single-file CLI with:
  - Adaptive polling and persistent cadence learning per gauge.
  - A curses-based TUI with per-station selection, sparklines, and trend summaries.
- Added state normalization and backfill support from USGS IV so history is “one row per real update,” not per fetch.
- Designed and documented a notes system (`notes/`) for cross-session coordination.

## 2025-12-09 – Forecast scaffolding and detail UX

- Tuned the cadence learner to start from an 8-minute baseline, ignoring sub-8-minute deltas when inferring update intervals.
- Added optional history backfill (`--backfill-hours`) and state cleanup on load to seed cadence learning from past USGS IV data.
- Introduced a pluggable forecast integration path:
  - `--forecast-base` URL template and `--forecast-hours` horizon.
  - Helpers to fetch a generic forecast series, summarize 3h/24h/full peaks, and compute basic amplitude and phase bias vs observations.
- Extended the TUI detail view (Enter on a station) to show:
  - A table of the last ~6 updates with per-update stage/flow deltas and trend per hour.
  - Forecast peak summaries and “vs forecast now” metrics when forecast data is available.

## 2025-12-09 – Latency-window scheduler and TUI polish

- Implemented per-update latency windows (lower/upper bounds) and midpoints, with robust median/MAD per gauge.
- Replaced the simple next-poll logic with a latency-aware scheduler that:
  - Uses coarse polling far from expected updates.
  - Uses short bursts of finer polling inside a narrow latency window for stations with stable latency.
- Updated the TUI to display latency stats per station in the expanded detail view.
- Improved column alignment for the main table and recent-updates table for better readability.
