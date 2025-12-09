# AGENTS: streamvis

This repository hosts `streamvis`, a small but opinionated Snoqualmie River watcher:

- Pulls live gauge data from USGS NWIS Instantaneous Values (IV).
- Learns per-station update cadence to stay polite (near 1 HTTP call per new update).
- Presents an engaging TUI for informed users with minimal overhead and latency.
- Optionally backfills recent history and, in future, overlays official forecasts.

The goal is to keep the core tight, transparent, and easy to reason about, while being unusually smart about polling and presentation.

## Coding guidelines

- Python 3.10+ only; prefer `from __future__ import annotations` and type hints.
- Keep dependencies minimal. Currently:
  - Runtime: `requests`
  - Standard library only for everything else.
- Match the existing style:
  - Top-level constants in ALL_CAPS.
  - Small, single-purpose functions; keep functions under ~100 lines where practical.
  - Use descriptive names; avoid 1-letter identifiers except for obvious indices.
- Error handling:
  - Network/API errors should fail soft: return `{}` / `[]` and let the caller degrade gracefully.
  - Never busy-loop on network failures; always respect the cadence / backoff rules.
- TUI:
  - Curses-based, no external UI deps.
  - Always keep a fast-but-light UI tick (sub-second) and a slower data cadence.
  - Avoid rendering more than fits in a typical 80x24 terminal; degrade gracefully on smaller screens.

## Project structure

- `streamvis.py` – main module, CLI, adaptive scheduler, TUI, and data/forecast wiring.
- `pyproject.toml` – packaging and dependency declaration (`requests` only).
- `README.md` – user-facing usage and behavior documentation.
- `.gitignore` – ignores Python cruft, virtualenvs, local state, and build artifacts.
- `BACKLOG.md` – top-level product backlog (high-level features).
- `notes/` – inter-session, inter-agent notes (see below).

## Notes system (`notes/`)

`notes/` is for human + agent memory and coordination across sessions. Files:

- `notes/MEMORY.md`
  - Long-lived architectural memory, design decisions, and “why” behind choices.
  - Think ADR-lite: short entries, timestamped, with rationale and trade-offs.

- `notes/WORKLOG.md`
  - Chronological log of work done.
  - Use a simple table or bullet format with date, actor, and brief summary.

- `notes/BACKLOG.md`
  - Working backlog / roadmap.
  - Use it for more detailed or technical backlog items than the top-level `BACKLOG.md`.

- `notes/CHAT.md`
  - Scratchpad for ideas, hypotheses, sketches—things that might become real work later.
  - Can be informal, but keep it readable and dated.

- `notes/SCRUTINY.md`
  - Critical review and risk tracking.
  - Capture concerns about correctness, performance, API contracts, and UX, plus how we’ll validate or mitigate them.

When editing code in this repo:

- Update `notes/WORKLOG.md` with a short entry for any meaningful change.
- Update `notes/MEMORY.md` when you make or rely on a design decision (e.g., cadence rules, forecast model choice).
- Prefer adding items to `notes/BACKLOG.md` instead of TODO comments in code.
- When you spot a risk or a subtle behavior, add an entry in `notes/SCRUTINY.md`.

## APIs / sources of truth

This project deliberately leans on a small set of authoritative, low-latency sources:

- **USGS NWIS Instantaneous Values (IV)** – `https://waterservices.usgs.gov/nwis/iv/`
  - Source of truth for *observed* river stage and flow.
  - Advantages:
    - Official USGS hydrologic data with clear provenance.
    - Near-real-time, typically 15-minute resolution for these gauges.
    - Supports multi-station, multi-parameter JSON responses in a single request (good for batching).

- **NOAA / National Water Prediction Service (NWPS)** – `water.noaa.gov` APIs
  - Source of truth for *official* river forecasts (heights and flows).
  - We treat NWPS as the authoritative forecast source and integrate via an **operator-configured forecast URL template** (see `README.md`).
  - Rationale:
    - Forecasts are produced by the River Forecast Centers and routed through NWPS.
    - Designed for machine access and low latency once products are issued.
    - Lets us compare our live observations against the same curves seen by forecasters.

Because this repository is developed offline, we do **not** hard-code exact NWPS endpoints or payload shapes. Instead:

- CLI exposes a `--forecast-base` template that operators can point at the appropriate NWPS endpoint and adjust parsing as needed.
- Code clearly labels any forecast-parsing logic as “shape assumptions” so they can be aligned with the actual API using NOAA’s current documentation.

## Expectations for future agents

- Preserve the adaptive, polite-polling behavior as a first-class design constraint.
- Keep UX focused:
  - Primary goal: communicate current + near-future river behavior to informed users, with low latency and minimal noise.
  - Extra features (forecast overlays, deeper analytics) must not compromise the responsiveness or clarity of the core TUI.
- When you extend the forecast integration:
  - Prefer small, testable helpers for bias estimation, peak detection, and interpolation.
  - Always document new APIs and contracts in `README.md` and `notes/MEMORY.md`.
  - Avoid introducing heavy ML frameworks; if you need “learning,” prefer compact online methods that can live in this single-file tool.

