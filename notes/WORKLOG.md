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

## 2025-12-09 – Central scrutinizer pass (LLM)

- Performed a full-project scrutiny pass over `streamvis.py`, `README.md`, `config.toml`, and all `notes/*.md`.
- Recorded detailed findings, risks, and recommendations in `notes/SCRUTINY.md` and added meta-level design notes about scheduler semantics, TUI assumptions, and forecast state to `notes/MEMORY.md`.

## 2025-12-10 – Meta scrutinizer pass

- Reviewed central scrutinizer notes and expanded them with prioritized severity, file/line references, and concrete remedies in `notes/SCRUTINY.md`.
- Added design-memory clarifications about separating normal cadence vs error backoff, fine-window philosophy, forecast trimming, and config authority in `notes/MEMORY.md`.

## 2025-12-10 – Scheduler and TUI hardening (staff engineer)

- Decoupled normal polling cadence from error backoff by updating `schedule_next_poll` to ignore `max_retry_seconds` and rely only on learned intervals/latency; `--max-retry-seconds` now governs error backoff exclusively.
- Raised the fine-window polling floor to 15 seconds to keep bursty micro-polling polite while still converging on low-latency updates.
- Fixed the TUI trend calculation bug by deriving a robust time span from recent timestamps and only computing stage/flow trends when there are at least two valid samples, preventing `dh` from being used uninitialized.
- Aligned “Next ETA” semantics in the TUI with the CLI so past or unknown next times render as `now` rather than “ago …”.
- Trimmed stored forecast points in `update_forecast_state` to a window around “now” based on the configured horizon to avoid stale peaks and unbounded state growth.
- Made state writes more robust by writing to a temporary file and atomically renaming it into place, reducing the risk of partial file corruption on crash.
- Captured remaining follow-ups (scheduler test harness, state locking for multi-writer scenarios) in `notes/BACKLOG.md` and updated `notes/SCRUTINY.md` / `notes/MEMORY.md` to mark addressed items as resolved.

## 2025-12-10 – Synthetic scheduler harness and no-update logging

- Added `scheduler_harness.py`, a small synthetic harness that constructs representative gauge states (fast/slow, stable/variable latency) and prints both per-gauge `predict_gauge_next` times and the global `schedule_next_poll` decision for quick, manual inspection of scheduler behavior.
- Updated `update_state_with_readings` to:
  - Record `last_poll_ts` even when no new observation is seen so latency windows can use the last “no-update” poll as their lower bound on the next successful update.
  - Track a `no_update_polls` counter per gauge to persist how many consecutive polls have seen no change, making “no-change” information available cross-session for future heuristics.

## 2025-12-10 – TUI forced refetch key

- Added an `f` keybinding in TUI mode that, like `r`, schedules an immediate fetch but is labeled as a “forced refetch” to make it obvious the user is re-querying USGS even if the last observation timestamp has not advanced.
- Updated the footer help text and README TUI section to advertise `f` alongside `r`, clarifying that both trigger a fresh network call and re-parse of stage/flow regardless of whether the upstream update time changed.

## 2025-12-10 – Same-timestamp parameter updates

- Noticed that USGS sometimes updates stage and flow for a gauge at the same observation timestamp but at slightly different times, leading to a temporary mismatch where history showed a repeated flow value even after stage updated.
- Updated `update_state_with_readings` so that when a new fetch has the same `observed_at` as the last stored point but different stage/flow, the last history entry for that timestamp is refreshed in place; cadence/latency learning still only advances on strictly newer timestamps.

## 2025-12-10 – NW RFC textPlot cross-check (GARW1)

- Introduced NW RFC textPlot integration via `NWRFC_TEXT_BASE` and a new `--nwrfc-text` flag that, when enabled, periodically fetches `textPlot.cgi?id=<lid>&pe=HG&bt=on` for supported stations (currently `GARW1`).
- Implemented `parse_nwrfc_text` to parse observed/forecast stage and discharge from the text output, treating timestamps as PST/PDT and converting them to UTC.
- Stored parsed series in `state["nwrfc"][gauge_id]` and computed a simple per-timestamp difference vs the latest USGS observation when timestamps align, recording Δstage/Δflow under `diff_vs_usgs`.
- Extended the TUI expanded detail view to display a concise “NW RFC vs USGS (last)” line when cross-check data is available, so users can see whether the downstream NW RFC view closely matches raw USGS IV.

## 2025-12-10 – Config-driven stations and forecast wiring

- Added a minimal, dependency-free TOML loader in `streamvis.py` that reads `config.toml` when present.
- Switched `SITE_MAP` and the USGS IV base URL to be derived from `config.toml` (`[stations.*].usgs_site_no` and `[global.usgs].iv_base_url`), with the existing Snoqualmie constants preserved as a fallback when config is missing or incomplete.
- Updated forecast integration so that, in addition to CLI `--forecast-base`, per-station `forecast_endpoint` values and a global `[global.noaa_nwps].default_forecast_template` in `config.toml` can supply forecast URL templates; CLI still takes precedence.
- Documented the new configuration behavior in `README.md` and captured the “config as source of truth, code as fallback” decision in `notes/MEMORY.md`.

## 2025-12-10 – Browser TUI via Pyodide (static web harness)

- Introduced `http_client.py` as a thin HTTP abstraction so `streamvis.py` can run both natively (via `requests`) and in a browser (via `pyodide.http.open_url`) without changing its core logic or error-handling semantics.
- Added `web_curses.py`, a minimal curses shim that draws into a `#terminal` div and consumes key codes from a JS-managed queue, implementing only the small subset of curses APIs actually used by the TUI.
- Added `web_entrypoint.py` with `run_default()` that launches `streamvis` in `--mode tui` using a local `streamvis_state.json` state file suitable for mapping to browser `localStorage`.
- Created `web/index.html` and `web/main.js`, which:
  - Load Pyodide from a CDN.
  - Load the Python modules (`http_client.py`, `web_curses.py`, `streamvis.py`, `web_entrypoint.py`) into the Pyodide runtime.
  - Patch `sys.modules["curses"]` to point at `web_curses`.
  - Bridge `streamvis_state.json` to `localStorage` (`streamvis_state_json`) on startup/shutdown so browser sessions retain adaptive polling state.
- Updated `README.md` with a “Running the TUI in a browser” section describing the Pyodide-based GitHub Pages flow and the fact that the existing curses TUI runs unchanged inside the browser.
  - Made the web shim responsive to the actual viewport by deriving rows/cols from the `#terminal` element’s size and font metrics, and added click/tap support on gauge rows by encoding per-row selection/toggle events into synthetic key codes consumed by `tui_loop`.

## 2025-12-11 – P0 fixes from repo review

- Fixed packaging so `pip install .` includes required peer modules (`http_client.py`, `web_curses.py`, `web_entrypoint.py`) and installed `streamvis` runs cleanly.
- Added timeout-aware `getch()` behavior in `web_curses.py` to prevent Pyodide/browser TUI busy-looping.
- Recorded a comprehensive, prioritized review and follow-ups in `notes/SCRUTINY.md` and design decisions in `notes/MEMORY.md`.
