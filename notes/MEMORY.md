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

## 2025-12-09 – Meta notes from central scrutiny

- Scheduler semantics:
  - Current implementation ties the maximum normal poll horizon to `max_retry_seconds`, not just error backoff. For slow-updating gauges, this means we may poll significantly more often than their natural cadence unless `--max-retry-seconds` is raised.
  - Fine-window polling can be as fast as every 5 seconds when latency is stable, which conflicts with the informal “avoid 1/min” guardrail unless we explicitly embrace “short, targeted bursts” as part of the design.
- Design intent vs reality:
  - The documented goal (“~1 call per real update”) is conceptually per-station, but the implementation effectively optimizes for a shared multi-gauge call. A single fast gauge can drag the whole system’s poll rate up; this is probably acceptable but should be treated as a deliberate trade-off.
  - We should decide whether `max_retry_seconds` is an error/backoff concept only, or also a hard cap on normal polling; future work should make that distinction explicit in both code and docs.
- TUI behavior:
  - The “Next ETA” wording in the UI should never render past times; aligning the TUI with the CLI’s “now” behavior will avoid confusion.
  - Detail view trend computation assumes both stage and flow are present; if we ever add gauges that only export one metric, we must harden that logic.
- Forecast state:
  - Forecast points are currently unbounded in state; adding a trim similar to `HISTORY_LIMIT` would future-proof long-running sessions and high-resolution forecast feeds.
  - All forecast parsing is intentionally labeled as “shape assumptions”; any future integration with real NWPS APIs should update `config.toml` and this memory to reflect the chosen endpoints and field mappings.

## 2025-12-10 – Meta scrutinizer clarifications

- Polling design clarity:
  - The current code caps normal scheduling with `max_retry_seconds`, which is documented as an error-backoff ceiling. Desired direction: decouple normal cadence from error backoff so the steady-state interval tracks the learned per-gauge cadence (clamped) while a separate backoff governs failures.
  - Fine-window bursts are now clamped to a minimum of 15 seconds between polls to keep short-term bursts polite while still converging on low-latency updates.
- Forecast/state hygiene:
  - Stored forecast points are trimmed to a window around “now” based on the configured horizon to avoid stale peaks and unbounded state growth before running bias/phase calculations.
- Configuration source of truth:
  - `config.toml` initially mirrored USGS URLs, station metadata, and thresholds hard-coded in `streamvis.py`. We have now wired a minimal TOML loader so that:
    - Station bindings (`gauge_id` → `usgs_site_no`) and the primary USGS IV base URL are read from `config.toml` when present.
    - Built-in Snoqualmie defaults remain as a fallback when `config.toml` is missing or incomplete, so the tool still runs out of the box.
    - Forecast configuration in `config.toml` (per-station `forecast_endpoint` and a global `default_forecast_template`) is honored when non-empty, but CLI `--forecast-base` continues to override config.

## 2025-12-10 – Browser TUI architecture (Pyodide + web_curses)

- We now support running the existing curses-based TUI directly in a browser using Pyodide and a small compatibility layer:
  - HTTP is routed through a new `http_client` abstraction:
    - Native CPython uses `requests` under the hood (preserving current behavior).
    - Pyodide uses `pyodide.http.open_url`, relying on browser `fetch` and CORS for USGS/NWPS/NWRFC endpoints.
  - Curses is abstracted via `web_curses`, which implements just the subset of the curses API that `tui_loop` uses and renders into a `<div id="terminal">` in the DOM.
  - A small `web_entrypoint` module launches the TUI in `--mode tui` with a fixed `--state-file` pointing at `streamvis_state.json` so that JS can bridge it to `localStorage`.
- Design intent:
  - Keep `streamvis.py` as the single source of truth for scheduler behavior and TUI layout; the browser build is a thin shell that swaps out HTTP and terminal backends.
  - Make the browser path completely static-host friendly (e.g., GitHub Pages) by loading Pyodide from a CDN and serving only static assets (HTML/JS/Python).
  - Persist adaptive state across browser sessions by syncing the chosen state file to `localStorage` on boot/exit; this means mobile users see the same learned cadences the CLI would, as long as they occasionally quit the TUI to flush state back.

## 2025-12-10 – No-update polls and history usage

- Each poll (whether or not a new observation appears) now records `last_poll_ts` per gauge; when a new point arrives, latency windows use the last “no-update” poll as the lower bound and the current poll as the upper bound for the observation→API delay.
- A per-gauge `no_update_polls` counter is persisted so runs can see how many consecutive polls have seen no change; this is currently logging/diagnostic data but can inform future cadence heuristics.
- Cadence learning continues to rely on all observed update deltas via EWMA and, when backfill is enabled, on the full `HISTORY_LIMIT` window of recent history; older points naturally down-weight via the EWMA rather than explicit re-computation on every startup. When at least three deltas have been observed and the learned mean is still significantly shorter than the empirical average, we snap the mean upward toward that average to avoid underestimating slow gauges (e.g., hourly stations) for too long.

## 2025-12-11 – Packaging and browser-loop decisions

- **Packaging source of truth**:
  - Although `streamvis` remains conceptually a single‑file core, it now depends on a few small peer modules (`http_client.py`, optional web shims).
  - Decision: keep the flat, multi‑module layout and include those modules explicitly in `pyproject.toml` `py-modules` so the installed console script works without refactoring into a package.
  - Trade‑off: minimal disruption and preserves “single‑file core” ergonomics; a package refactor remains available if module count grows materially.

- **Browser TUI throttling**:
  - The native TUI relies on `curses.timeout()` to avoid busy‑looping between UI ticks.
  - Decision: implement synchronous timeout semantics in `web_curses.getch()` (sleeping for the configured timeout) as a short‑term, dependency‑free fix to prevent pegged CPU in Pyodide.
  - Trade‑off: input is only serviced between ticks (≤ UI_TICK_SEC), which is acceptable for interactive use; longer‑term async `web_tui_main()` remains on `notes/BACKLOG.md` for a fully cooperative browser loop.
