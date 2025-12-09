# SCRUTINY.md — streamvis

## 2025-12-09 – Initial concerns and checks

- **Polling correctness**
  - We must guarantee that adaptive scheduling never degenerates into rapid-fire polling (e.g., 1/min) unless explicitly requested by the user.
  - Check that:
    - Learned intervals are always clamped into [8 minutes, 6 hours].
    - Next-poll timestamps are always in the future (or at least padded by the minimum retry interval).
    - History only increments when a new observation timestamp appears.

- **State integrity**
  - The local state file is our long-term memory; corruption or duplication there can quietly degrade behavior.
  - On load, we:
    - De-duplicate history per gauge by timestamp.
    - Realign `last_timestamp`, `last_stage`, and `last_flow` to match the most recent history entry.
    - Clamp any odd `mean_interval_sec` values into a safe range.

- **Forecast integration**
  - We intentionally do *not* hard-code NOAA NWPS endpoint details while working offline.
  - Any forecast parsing logic must be clearly marked as shape assumptions so operators can align it with current NWPS API docs before enabling `--forecast-base`.

## 2025-12-09 – Latency-aware scheduler review

- The scheduler uses per-gauge cadence and latency (median + MAD) to choose between coarse and fine polling regimes.
- Risks / checks:
  - Ensure “fine” windows only activate when latency MAD is small (≤ ~60 s) and cadence is reasonably short (≤ 1 hour).
  - Verify that the fine poll step and window half-width do not drive total calls per station above a reasonable budget, even during floods.
  - Confirm that latency windows (lower/upper bounds) remain well-behaved when the API or telemetry experiences occasional bursts or gaps.
- Validation ideas:
  - Record brief control summaries (cadence, latency median/MAD, fine-window hit rate) in logs for offline analysis.
  - Run synthetic tests with controlled cadence/latency patterns to ensure the scheduler converges as expected.
