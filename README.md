# streamvis

Small command-line tool that fetches current Snoqualmie River USGS gauge readings and prints a simple status table.

## Requirements

- Python 3.10 or newer
- Internet access to query the USGS Instantaneous Values API

## Installing and running

From a clean system:

```bash
git clone https://github.com/your-user/streamvis.git
cd streamvis

# Create a virtual environment (optional but recommended)
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies and CLI entry point
pip install .

# Run via installed console script
streamvis

# Or run the script directly without installing
python streamvis.py
```

## What it does

- Calls the USGS Instantaneous Values service for the gauges defined in `config.toml` (or the built-in defaults if no config is present).
- Extracts the most recent stage (ft) and flow (cfs).
- Classifies each gauge as `NORMAL`, `ACTION`, `MINOR FLOOD`, `MOD FLOOD`, or `MAJOR FLOOD` using `FLOOD_THRESHOLDS`.
- Prints a compact table to standard output with the observation time and the per-gauge expected next update.

If the USGS service is unreachable or returns unexpected data, `streamvis` exits with a message and non-zero status.

## Adaptive polling (learned cadence)

To minimize needless polling while keeping latency low, run in adaptive mode:

```bash
streamvis --mode adaptive
```

Behavior:

- Fetches once immediately, then learns the typical update cadence per gauge (EWMA of observed intervals) starting from an 8-minute baseline.
- Schedules the next multi-gauge request just before the next expected update (shared single call for all gauges).
- If the prediction was early (no new timestamps), it widens the interval slightly and does a short retry (default 60s) so it converges toward ~1 call per new update.
- Persisted state lives at `~/.streamvis_state.json` (override with `--state-file PATH`). Only the last timestamps, learned intervals, and last values are stored—no heavy history.
- Learning has sensible floors/ceilings: sub-60-second deltas are ignored when learning cadence, and learned intervals are clamped to a reasonable range before scheduling the next fetch.

Latency-aware scheduling:

- For each station, `streamvis` tracks:
  - Observation cadence (seconds between gauge timestamps).
  - Observation→API latency as a window (lower/upper bounds) and robust stats (median, MAD).
- The scheduler uses a two-regime strategy:
  - Coarse polling while far from the expected next update (fraction of the learned interval, capped).
  - Short bursts of finer polling inside a narrow latency window for stations whose latency is stable (small MAD), to converge on update timing at second-level resolution without hammering the API.

Options:

- `--min-retry-seconds` (default 60): retry delay if the prediction was early.
- `--max-retry-seconds` (default 300): ceiling when backing off on errors.
- `--backfill-hours` (default 0): on startup, optionally backfill this many hours of recent history from USGS IV to seed the cadence learner and charts.

## Configuration (config.toml)

`streamvis` reads station metadata and USGS/NWPS base URLs from `config.toml` in the project/module directory when present:

- `[global.usgs]` defines the USGS IV base URL(s); if omitted, the tool falls back to `https://waterservices.usgs.gov/nwis/iv/`.
- `[stations.<GAUGE_ID>]` blocks define per-station metadata, including `usgs_site_no` used to build IV queries.
- `[global.noaa_nwps]` and per-station `forecast_endpoint` fields can supply forecast URL templates; these are only used when non-empty.

If `config.toml` is missing or incomplete, `streamvis` uses its built-in Snoqualmie defaults so it remains runnable out of the box.

## Forecast integration (optional, via NWPS)

`streamvis` can optionally overlay official river forecasts from NOAA’s National Water Prediction Service (NWPS) when configured with an appropriate API endpoint.

CLI options:

- `--forecast-base`: URL template for the forecast API. It may contain `{gauge_id}` and `{site_no}` placeholders from `SITE_MAP`.  
  - Example template (you must align this with NOAA’s current API docs before use):
    - `--forecast-base 'https://api.water.noaa.gov/.../stations/{gauge_id}/forecast'`
- `--forecast-hours` (default 72): forecast horizon (in hours) considered when computing peak summaries.

You can also configure a default forecast template and/or per-station `forecast_endpoint` values in `config.toml`. When both are present, precedence is:

1. CLI `--forecast-base`
2. Per-station `forecast_endpoint` in `config.toml`
3. `[global.noaa_nwps].default_forecast_template` in `config.toml`

Behavior when enabled:

- Forecasts are refreshed for all gauges at most once per 60 minutes (`FORECAST_REFRESH_MIN`).
- For each station, `streamvis`:
  - Stores a forecast time series (time, stage, flow) in local state.
  - Computes 3-hour, 24-hour, and full-horizon maxima for stage and flow.
  - Compares the latest observation to the nearest forecast point to estimate amplitude bias (delta and ratio).
  - Compares observed vs forecast peak times to estimate a simple phase shift (peak earlier/later than forecast).
- In the TUI detail view (select station + `Enter`):
  - Shows a table of the last few updates and trends.
  - If forecast data is available:
    - Displays forecast peak summaries: 3h / 24h / full (stage/flow).
    - Shows “vs forecast now” deltas and ratios for stage and flow.
    - Shows an estimated peak timing offset (hours earlier/later than forecast).

Important:

- This repository is developed offline, so the exact NOAA/NWPS endpoint and JSON shape are **not** hard-coded as authoritative.
- The forecast parsing logic in `streamvis.py` is intentionally conservative and documented as making shape assumptions; you should adapt the URL template and field mapping to match NOAA’s current NWPS API documentation for your gauges.

## APIs and sources of truth

- **Observed data** – USGS NWIS Instantaneous Values (IV): `https://waterservices.usgs.gov/nwis/iv/`
  - Official USGS hydrologic observations (stage and flow).
  - Near-real-time, typically 15-minute resolution.
  - Supports multi-station, multi-parameter JSON responses in a single call; ideal for polite, batched polling.

- **Forecast data** – NOAA National Water Prediction Service (NWPS): `water.noaa.gov` APIs.
  - Official river stage/flow forecasts produced by River Forecast Centers.
  - Designed for low-latency access once products are issued.
  - Integrated here via the configurable `--forecast-base` URL template so operators can target the right low-latency endpoint for their stations.

- **Cross-check data (optional)** – NW RFC text flowplots: `https://www.nwrfc.noaa.gov/station/flowplot/textPlot.cgi`
  - Text hydrologic summaries (observed + forecast) for selected stations.
  - When `--nwrfc-text` is enabled, `streamvis` periodically fetches the text plot for supported stations (currently `GARW1`) and:
    - Parses observed stage/flow time series in local time (PST/PDT) and converts to UTC.
    - Stores the series alongside USGS history in state.
    - Computes a simple per-timestamp difference vs the latest USGS observation when timestamps align (Δstage, Δflow).
  - In the TUI detail view, if NW RFC data is available for the selected station, a compact “NW RFC vs USGS (last)” line shows the latest deltas so you can see whether the downstream RFC view agrees with raw USGS IV.
  - USGS IV remains the authoritative source; NW RFC is treated strictly as a secondary cross-check.

Lightweight batching/caching:

- All gauges are fetched in a single USGS call to avoid per-station chatter.
- Only minimal state (last timestamps + EWMA interval + last values, plus a small rolling history for charts) is persisted to keep I/O small and avoid historical bloat.

## Engaging TUI (wargames-style, text-mode)

Launch the full-screen TUI that shows current time/date, observation times, per-station next-ETA, and interactive detail with a text-mode chart:

```bash
streamvis --mode tui  # optional: --chart-metric flow
```

- Arrow keys / `j`/`k` to select a station; selected row highlights.
- `c` toggles the chart metric between stage and flow; `r` requests an immediate refresh; `f` forces an immediate refetch even if the last observation time has not advanced; `q` quits.
- Detail pane shows the selected station’s last reading, when it was observed, per-gauge next expected update, and a sparkline of recent history (lightweight, persisted in state).
- The loop reuses the adaptive cadence learner to keep requests near 1 call per new update.
- State also records last fetch/success/failure timestamps and learned intervals across runs so the TUI stays conservative even after restart.

Note: TUI mode uses Python `curses` (available on macOS/Linux; Windows users may need WSL or an environment with `curses` support).

## Running the TUI in a browser (Pyodide build)

`streamvis` also ships a small browser harness that runs the existing curses TUI inside a web page using Pyodide (Python → WebAssembly) and a minimal `web_curses` shim:

- The core logic in `streamvis.py` is unchanged; only HTTP and curses are abstracted behind `http_client.py` and `web_curses.py`.
- In the browser, HTTP calls use `pyodide.http.open_url`, so requests go directly from the page to USGS/NWPS/NWRFC with CORS.
- The TUI draws into a `<div id="terminal">` using `web_curses`, and key events (`q`, arrows, `c`, `r`, `f`, `Enter`) are forwarded from JS into `getch()`.
- State is persisted between browser sessions by mapping the `--state-file streamvis_state.json` used by the web entrypoint to `localStorage` (`streamvis_state_json`).
- The browser shim adapts the virtual terminal size to the viewport and lets you tap/click on a station row to select it and toggle its detail/table view, which works well on devices like an iPhone in landscape mode.

To host this on GitHub Pages:

1. Commit `web/index.html` and `web/main.js` along with the Python modules in the repo root (`streamvis.py`, `http_client.py`, `web_curses.py`, `web_entrypoint.py`, `config.toml`).
2. Configure GitHub Pages to serve the `web/` directory (or copy these files into your chosen Pages root).
3. Open the Pages URL on your phone or desktop; the page will load Pyodide from a CDN and start `streamvis` in TUI mode inside the browser.

## Backlog

See `BACKLOG.md` for future work ideas.
