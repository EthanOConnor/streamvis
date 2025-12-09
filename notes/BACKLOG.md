# BACKLOG.md — notes

## Near-term

- Wire in configurable forecast integration using NOAA / NWPS river forecast APIs via a `--forecast-base` URL template.
- For each station, compute 3-hour, 24-hour, and full-horizon forecast maxima for stage and flow, and surface them in the TUI detail view.
- Compare observed vs forecast curves to estimate:
  - Amplitude bias (actual vs forecast).
  - Phase shift / peak timing differences.
  - Simple adjusted “expected peak” using a lightweight online regression model.

## Medium-term

- Add a configuration file (e.g., `config.toml`) for station metadata and forecast URL templates instead of wiring them only via CLI.
- Make the TUI forecast details collapsible and clearly annotated so power users get depth without overwhelming casual viewers.
- Capture basic health metrics (API error rates, average cadence per gauge) into `notes/SCRUTINY.md` for later tuning.
- Refine the latency-aware scheduler:
  - Persist per-update latency windows (lower/upper bounds) and their evolution.
  - Auto-tune fine/coarse polling windows based on latency MAD and window width.
  - Expose a concise “control summary” per station (e.g., typical cadence, latency median/MAD, fine-window duty cycle).

## Longer-term

- Port the core logic to an Apptron-based desktop app and GitHub Pages site (see top-level `BACKLOG.md`).
- Experiment with richer, but still lightweight, learning on observed vs forecast discrepancies (e.g., online linear / quadratic regression) while keeping the implementation dependency-free.
