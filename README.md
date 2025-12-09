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

- Calls the USGS Instantaneous Values service for the gauges defined in `SITE_MAP`.
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
- Learning has sensible floors/ceilings: sub-minute deltas are ignored to avoid over-polling; learned intervals are clamped to a reasonable range before scheduling the next fetch.

Options:

- `--min-retry-seconds` (default 60): retry delay if the prediction was early.
- `--max-retry-seconds` (default 300): ceiling when backing off on errors.
- `--backfill-hours` (default 0): on startup, optionally backfill this many hours of recent history from USGS IV to seed the cadence learner and charts.

Lightweight batching/caching:

- All gauges are fetched in a single USGS call to avoid per-station chatter.
- Only minimal state (last timestamps + EWMA interval + last values, plus a small rolling history for charts) is persisted to keep I/O small and avoid historical bloat.

## Engaging TUI (wargames-style, text-mode)

Launch the full-screen TUI that shows current time/date, observation times, per-station next-ETA, and interactive detail with a text-mode chart:

```bash
streamvis --mode tui  # optional: --chart-metric flow
```

- Arrow keys / `j`/`k` to select a station; selected row highlights.
- `c` toggles the chart metric between stage and flow; `r` forces a refresh; `q` quits.
- Detail pane shows the selected station’s last reading, when it was observed, per-gauge next expected update, and a sparkline of recent history (lightweight, persisted in state).
- The loop reuses the adaptive cadence learner to keep requests near 1 call per new update.
- State also records last fetch/success/failure timestamps and learned intervals across runs so the TUI stays conservative even after restart.

Note: TUI mode uses Python `curses` (available on macOS/Linux; Windows users may need WSL or an environment with `curses` support).

## Backlog

See `BACKLOG.md` for future work ideas.
