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

## 2025-12-11 – P1 scheduler + web hardening

- Removed the coarse-step hard cap so adaptive polling for slow gauges scales with learned cadence; added per-gauge “calls/update” instrumentation (last + EWMA) and surfaced it in expanded TUI detail.
- Hardened the Pyodide browser harness by letting `web/main.js` try both local and parent module paths; clarified GitHub Pages hosting layouts in `README.md`.
- Added stdlib regression tests for scheduler and cadence snap‑up behavior in `tests/test_scheduler.py`.
- Adjusted `http_client.py` to lazy-import `requests` so offline/unit-test environments can import `streamvis` without the dependency installed.

## 2025-12-11 – P2 config and robustness cleanup

- Updated `config.toml` header to reflect live runtime wiring and marked unused fields as advisory.
- Added `EDGW1` to built-in defaults so primary gauge ordering is consistent out of the box.
- Implemented a best-effort single-writer lock for state files using `fcntl` and documented it in README.
- Preserved last non-None stage/flow on partial USGS reads and added numeric-string coercion for forecast parsing.

## 2025-12-11 – Browser colors and iOS usability

- Implemented per-cell color/attribute rendering in the Pyodide `web_curses` shim using span-based HTML output.
- Added `--ui-tick-sec` CLI flag and set a slower default tick for the browser entrypoint to reduce CPU on mobile/iOS.
- Tweaked web CSS for responsive font sizing, safe-area padding, and touch-friendly interaction.

## 2025-12-11 – Pyodide loading UX and async web TUI

- Refactored TUI rendering helpers (`color_for_status`, `draw_screen`) to top-level so both native and web drivers share layout/formatting.
- Added an async `web_tui_main` that yields via `asyncio.sleep` to keep mobile Safari responsive; browser entrypoint now uses it.
- Added a fixed loading bar with fake progress and step text in `web/main.js` + `web/index.html` to show Pyodide startup progress and avoid “black screen” confusion on slow loads.

## 2025-12-11 – Upstream check + P3 cleanup

- Performed an up-to-date (mid‑Dec 2025) review of USGS WaterServices, NOAA NWPS, and Pyodide changes; captured impacts and follow-ups in `notes/MEMORY.md` and `notes/BACKLOG.md`.
- Added a state schema version (`meta.state_version`) and optional `--debug` control-summary logging for cadence/latency tuning.

## 2025-12-11 – P4 mobile/table UX polish

- Improved mobile tap targeting by accounting for scroll and padding in the JS click mapper; guarded against double-firing pointer/click events.
- Adjusted tap UX so first tap selects a row and a second tap opens/closes details; switching rows in list view no longer auto-enters detail mode.
- Made browser terminal sizing truly responsive (reduced hard minimum columns) and added adaptive table columns in `draw_screen` to avoid right-edge overflow on iOS.
- Added dynamic font adaptation on resize/orientation: browser shrinks font to fit table columns before dropping columns, and grows font/table automatically when orientation widens.

## 2025-12-11 – Web terminal fit rigor

- Reworked browser sizing to be measurement-driven: `web_curses._measure_terminal` now subtracts DOM padding and measures real monospace char width/row height via a hidden span, preventing optimistic col counts that could cut off the last column.
- Updated `web/main.js` font adaptation to calibrate `charFactor` from real DOM text metrics and target the full 59‑column wide header in portrait before dropping columns.

## 2025-12-11 – 15‑minute cadence prior + periodic backfill

- Updated cadence learning to start from a 15‑minute base prior and snap to the best‑fitting 15‑minute multiple (15/30/60 min, etc.) once enough deltas support it; persisted `cadence_mult` and `cadence_fit` per gauge.
- Adjusted EWMA updates to prefer snapped multiples when deltas land near the grid, while retaining a fallback snap‑up for slow irregular gauges.
- Enabled default startup backfill (`--backfill-hours` now defaults to 6) and added low‑frequency periodic backfill checks to detect missed updates or cadence shifts.
- Added regression tests for cadence snapping and missed‑update robustness.

## 2025-12-11 – Web favicon

- Added a simple STREAMVIS favicon (`web/favicon.svg`) with Snoqualmie wave lines and a gauge marker, and wired it into `web/index.html`.

## 2025-12-11 – Nearby stations toggle

- Added per‑station lat/lon metadata (defaults in code + floats in `config.toml`) and a small haversine distance helper.
- Implemented a `[n] Nearby` text toggle in the TUI (native + web) that, when enabled, shows the three closest gauges to the user and prompts for browser geolocation under Pyodide.

## 2025-12-11 – Dynamic Nearby discovery

- Extended Nearby mode to query the USGS NWIS Site Service for active IV stream gauges near the user, select the 3 closest, and add/persist them as dynamic stations (`Uxxxxx` ids) when not already tracked.

## 2025-12-12 – USGS `modifiedSince` optimization

- Added an opportunistic `modifiedSince` duration filter to USGS IV fetches when all tracked gauges have ≤1h cadences, reducing payload by omitting unchanged stations without risking missed updates on slow gauges.
- `fetch_gauge_data(state)` now backfills omitted series from persisted state so the UI remains stable while still counting “no‑update” polls correctly.

## 2025-12-12 – Phase + biweight latency

- Added per‑gauge phase offset estimation for snapped cadences and use it to predict next *API‑visible* update times.
- Switched latency stats from median/MAD to Tukey biweight location/scale with a 600s±100s prior and clamped per‑update latency samples within visibility windows.

## 2025-12-12 – Latency visibility in compact detail

- Surfaced per‑gauge latency location/scale (`latency_loc_sec ± latency_scale_sec`) in the always‑visible station detail timing line, not just expanded detail mode.

## 2025-12-12 – Browser persistence + community priors

- Synced `streamvis_state.json` to browser `localStorage` on every `save_state()` so mid‑run reloads keep learned cadence/latency.
- Added optional community read/publish hooks (`--community-base`, `--community-publish`) to seed cold starts from shared priors and contribute per‑update latency samples (native only).
- Documented the community aggregator contract in `notes/COMMUNITY_AGGREGATOR.md` and added a minimal Cloudflare Worker example in `serverless/community_worker.js`.

## 2025-12-14 – Web publish + storage fallback

- Added async `post_json_async()` (Pyodide fetch + timeout) and a queued web publishing path so browser clients can contribute community samples without blocking the UI tick.
- Added compact JSON + slim-state fallback for browser `localStorage` persistence to better survive iOS/Safari storage quotas.
- Wired browser configuration for community flags via URL query params (`?community=...&publish=1`) with cached settings in localStorage.

## 2025-12-17 – P1 Implementation: Modularization + Dual-Stack USGS API

Major refactor to address P1 priorities from codebase review:

### Package Structure
- Transformed monolith into `streamvis/` package while maintaining backward compatibility
- Renamed original to `streamvis_monolith.py` with shim at `streamvis.py` for existing imports
- Created `streamvis/__main__.py` for `python -m streamvis` execution

### Type Safety (195 lines)
- Added `streamvis/types.py` with comprehensive TypedDict definitions:
  - `GaugeState`, `AppState`, `MetaState`, `HistoryPoint`, `ForecastState`
  - `BackendStats` for dual-stack API latency tracking
  - `CommunityPrior`, `USGSSite`, `GaugeReading`

### Module Extraction
- `streamvis/constants.py` (115 lines): All configuration constants + new OGC API endpoints
- `streamvis/utils.py` (225 lines): Pure utility functions with new `ewma_variance()` for backend tracking
- `streamvis/location.py` (195 lines): Platform-adaptive native location layer
  - macOS: CoreLocation via osascript
  - Linux: GeoClue D-Bus
  - Fallback: IP-based geolocation

### Dual-Stack USGS API (770 lines total)
- `streamvis/usgs/waterservices.py`: Legacy WaterServices IV API client
- `streamvis/usgs/ogcapi.py`: New OGC API–Features client for `api.waterdata.usgs.gov`
- `streamvis/usgs/adapter.py`: Blended backend with:
  - Parallel fetches from both APIs in BLENDED mode
  - Per-backend EWMA latency and variance tracking
  - Statistical backend selection when confidence reached
  - 10% hysteresis to avoid flip-flopping
  - Periodic probing of non-preferred backend

### Verification
- All 12 existing scheduler tests pass
- Package imports verified from both `import streamvis` and `from streamvis.* import *`
- Backward compatibility maintained for direct execution and imports

## 2025-12-17 – Complete Modularization + CLI Flag

Completed extraction of remaining core modules:

### Additional Modules
- `streamvis/scheduler.py` (285 lines): Cadence learning, phase offset estimation, two-regime poll scheduling
- `streamvis/state.py` (462 lines): Load/save with atomic writes, single-writer locking, cleanup, backfill, observation update logic with latency learning

### CLI Enhancement
- Added `--usgs-backend {blended,waterservices,ogc}` flag
  - `blended` (default): Fetches from both APIs, learns which is faster
  - `waterservices`: Legacy API only
  - `ogc`: New OGC API only

### Verification
- All 12 tests pass
- CLI help shows new `--usgs-backend` option
- Package imports work correctly

## 2025-12-17 – Monolith Elimination

Completed elimination of `streamvis_monolith.py`:

- Renamed monolith to `streamvis/tui.py` (git detected as 100% rename)
- Created `streamvis/config.py` (195 lines): TOML loading, SITE_MAP, STATION_LOCATIONS
- Created `streamvis/gauges.py` (145 lines): classify_status, nearest_gauges, RDB parsing
- Updated all imports to use package modules
- Updated CI workflow to verify `streamvis/tui.py` instead of monolith
- Updated `web/main.js` to load `streamvis/tui.py`

Package structure now:
```
streamvis/
├── __init__.py      # Public API
├── __main__.py      # python -m streamvis
├── config.py        # Configuration loading
├── constants.py     # Constants
├── gauges.py        # Gauge utilities
├── location.py      # Native geolocation
├── scheduler.py     # Cadence learning
├── state.py         # State persistence
├── tui.py           # Main TUI application
├── types.py         # TypedDict definitions
├── utils.py         # Pure utilities
└── usgs/            # USGS API clients
```

All 12 tests pass. Web deployment now loads from package.

## 2025-12-18 – Fix Pyodide Loader For Modular Package

- Updated `web/main.js` so Pyodide installs Python sources preserving package paths (e.g., `streamvis/tui.py` → `streamvis/tui.py`) and derives dotted module names for `import streamvis.*`.
- Browser build now loads the full `streamvis/` package before importing it, and no longer loads the top-level `streamvis.py` shim (avoids module/package shadowing).
- Fixed a native TUI crash by importing `compute_modified_since` as `_compute_modified_since` in `streamvis/tui.py` (NameError during IV fetch gating).
- Fixed `fetch_gauge_data()` in `streamvis/tui.py` to actually return the populated `result` dict (previously returned `None`, leaving the UI with empty readings).
- Improved native TUI degradation: when live fetch fails, the table/detail view now fall back to persisted `last_stage`/`last_flow`, and the footer shows the underlying fetch error (e.g., missing `requests`) while backing off.
- Fixed packaging so `pip install .` installs the `streamvis/` package (and `streamvis.usgs`) instead of the legacy `streamvis.py` module; also aligned `pyproject.toml` version to `0.3.0`.
- Verification: `node --check web/main.js`, `python -m unittest discover -s tests` (all 12 pass), `python -m streamvis --help`.

## 2025-12-18 – Infra Finalization Sprint (Core Stabilization)

- Made the extracted modules (`streamvis/state.py`, `streamvis/scheduler.py`, `streamvis/usgs/*`) the single source of truth by porting the mature cadence/latency logic that was still duplicated in `streamvis/tui.py`.
- Wired `--usgs-backend` end-to-end: all modes now persist `meta.api_backend`, and live fetches route through the dual-backend adapter with per-backend latency stats.
- Threaded the configured WaterServices base URL (`config.toml` `[global.usgs].iv_base_url`) through the USGS adapter so “observed data source” is controlled in one place.
- Added offline parsing helpers and unit tests for USGS WaterServices + OGC payloads and adapter behavior (`tests/test_usgs_parsing.py`, `tests/test_usgs_adapter.py`).
- Added a guardrail test to keep `web/main.js`’s `streamvisFiles` list in sync with `streamvis/**/*.py` (`tests/test_web_bundle.py`).
- Removed shadowed “gauge helper” implementations from `streamvis/tui.py` and standardized on `streamvis/gauges.py` + `streamvis/utils.py` (avoids subtle drift in Nearby/dynamic-station behavior).
- Hardened TypedDict copying in `streamvis/usgs/adapter.py` and documented `meta.last_backend_used` in `streamvis/types.py`.
- Updated CI to include Python 3.10 and to `pip install .` before running tests.
- Verification: `python -m unittest discover -s tests` (19 tests).

## 2025-12-18 – Nearby Gauge Lifecycle + Table Divider

- When Nearby is enabled, group the “nearby” gauges at the bottom of the main table under a divider (no duplicate rows).
- When Nearby is toggled off, evict dynamically added Nearby gauges so they stop being tracked/polled and don’t bloat state/browser storage.
- Added `streamvis/state.py` `evict_dynamic_sites()` plus unit coverage for ordering/eviction (`tests/test_nearby.py`).
- Updated README Nearby-mode description to match the new UX/eviction semantics.
- Verification: `python -m unittest discover -s tests` (23 tests).

## 2025-12-18 – Web Hosting Fix: `.nojekyll` + Better Loader Hints

- Added `.nojekyll` (repo root + `web/`) so GitHub Pages publishes Python package files like `streamvis/__init__.py`.
- Updated browser loader error messaging (`web/main.js`) to hint about missing Python sources / `file://` usage.
- Updated README GitHub Pages instructions to include publishing the `streamvis/` package directory.
- Made the browser build resilient to hosts that do not publish `__init__.py` / `__main__.py`:
  - `web_entrypoint.py` imports `streamvis.tui` directly (no reliance on `streamvis/__init__.py`).
  - `streamvis/tui.py` and `streamvis/state.py` import USGS adapter directly (no reliance on `streamvis/usgs/__init__.py`).
  - `web/main.js` treats `__init__.py`/`__main__.py` as optional during module fetch/install.

## 2025-12-18 – Nearby First-Fetch: disable `modifiedSince` until seen

- Fixed a Nearby UX glitch where newly discovered gauges could show blank fields on the first refresh due to WaterServices `modifiedSince` omitting stations we have never seen before.
- `fetch_gauge_data()` now disables `modifiedSince` until every tracked gauge has at least one `last_timestamp` in state; after that, the bandwidth optimization can resume safely.
- Added a regression test to keep this behavior stable (`tests/test_nearby.py`).
