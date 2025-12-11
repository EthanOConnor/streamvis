# BACKLOG.md — notes

## Near-term

- Wire in configurable forecast integration using NOAA / NWPS river forecast APIs via a `--forecast-base` URL template.
- For each station, compute 3-hour, 24-hour, and full-horizon forecast maxima for stage and flow, and surface them in the TUI detail view.
- Compare observed vs forecast curves to estimate:
  - Amplitude bias (actual vs forecast).
  - Phase shift / peak timing differences.
  - Simple adjusted “expected peak” using a lightweight online regression model.
- Web UI polish:
  - Tune browser color palette for readability (esp. iOS OLED contrast).
  - Add an optional light/dark theme toggle without new deps.
- Upstream API optimization:
  - Explore using USGS IV `modifiedSince` to skip parsing/no‑update payloads during early polls.
  - Track USGS OGC API‑Features rollout and plan a migration path if WaterServices IV is deprecated.
  - Evaluate NWPS HEFS probabilistic endpoints for optional future forecast overlays.

## Medium-term

- Add a configuration file (e.g., `config.toml`) for station metadata and forecast URL templates instead of wiring them only via CLI.
- Make the TUI forecast details collapsible and clearly annotated so power users get depth without overwhelming casual viewers.
- Capture basic health metrics (API error rates, average cadence per gauge) into `notes/SCRUTINY.md` for later tuning.
- Refine the latency-aware scheduler:
  - Persist per-update latency windows (lower/upper bounds) and their evolution.
  - Auto-tune fine/coarse polling windows based on latency MAD and window width.
  - Expose a concise “control summary” per station (e.g., typical cadence, latency median/MAD, fine-window duty cycle).
 - Add a small synthetic test harness for the scheduler and cadence learner (e.g., scripted cadences/latencies) and track “calls per real update” to validate the polite-polling envelope.
 - Introduce lightweight state-file locking (or documented single-writer guarantees) so multiple `streamvis` instances do not silently interleave writes.
- Add an async-friendly web TUI driver for the Pyodide build:
  - Factor the nested `draw_screen` and related helpers in `tui_loop` into reusable top-level functions.
  - Introduce a separate `web_tui_main()` that runs under `asyncio` in Pyodide, driving redraws and key handling in a cooperative loop (`await asyncio.sleep(UI_TICK_SEC)` between ticks) instead of the blocking `while True` used by the native CLI.
  - Keep the existing `tui_loop` unchanged for terminal use; the browser harness (JS) should call the new async entrypoint so the page remains responsive while the TUI runs.

## Longer-term

- Port the core logic to an Apptron-based desktop app and GitHub Pages site (see top-level `BACKLOG.md`).
- Experiment with richer, but still lightweight, learning on observed vs forecast discrepancies (e.g., online linear / quadratic regression) while keeping the implementation dependency-free.
