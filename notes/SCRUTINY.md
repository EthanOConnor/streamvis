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

## 2025-12-09 – Central scrutinizer deep pass

**High-level take**

- This is a very well-thought-out single-file tool: clear domain focus, polite-by-design polling, and a surprisingly rich TUI given the constraints.
- The main technical risks are (a) a couple of real bugs, (b) a mismatch between the “polite polling” design story and the actual scheduler behavior for slow gauges, and (c) lack of automated checks around the adaptive control logic.

**Architecture & data flow**

- Core flows:
  - Fetch latest USGS readings (`fetch_gauge_data`, `fetch_gauge_history` in `streamvis.py:144` and `streamvis.py:260`).
  - Maintain per-gauge state, cadence, and latency statistics (`update_state_with_readings` in `streamvis.py:730`, `_cleanup_state`, `backfill_state_with_history`).
  - Predict next observation/API time and schedule next poll (`predict_gauge_next`, `schedule_next_poll` in `streamvis.py:891`).
  - Optional forecast overlay (`fetch_forecast_series`, `update_forecast_state` around `streamvis.py:489`–`streamvis.py:575`).
  - Presentation: simple table for once/adaptive modes, and a curses TUI with detail panes and sparklines (`tui_loop` / `draw_screen` in `streamvis.py:1082`).
- State model:
  - JSON file at `STATE_FILE_DEFAULT` tracks, per gauge: last timestamp/value, EWMA cadence, rolling history, latency windows and robust stats; plus a `meta` block and optional `forecast` block.
  - State cleanup/backfill logic is robust and conservative: de-duplicates, clamps intervals, caps history (`_cleanup_state` in `streamvis.py:216`, `backfill_state_with_history` in `streamvis.py:317`).

**USGS integration & correctness**

- Shape handling:
  - IV integration assumes the standard NWIS JSON layout and uses clear, defensive access to `timeSeries`, `values`, and `dateTime` (`streamvis.py:180`–`streamvis.py:207`).
  - Backfill uses `startDT`/`endDT` in UTC with `Z` suffix and merges 00060/00065 by timestamp into gauge-level points (`streamvis.py:260`–`streamvis.py:315`).
- Strengths:
  - Soft failure behavior is exactly as desired: network/JSON errors return `{}` or `{}`-like structures without throwing (`streamvis.py:169`–`streamvis.py:175`, `streamvis.py:287`–`streamvis.py:292`).
  - State only increments history on new observation timestamps; update detection is strictly `observed_at > last_timestamp` (`streamvis.py:754`–`streamvis.py:781`).
  - Sub-60s deltas are ignored when learning cadence, aligning with the “ignore sub-60-second noise” design (`MIN_UPDATE_GAP_SEC` at `streamvis.py:17`, used in backfill & EWMA update).
- Scrutiny points:
  - There is no USGS API base URL wiring to `config.toml`; the code uses `USGS_IV_URL` (`streamvis.py:38`), while `config.toml:6`–`11` anticipates a configurable base. That’s fine for now, but there’s obvious duplication and drift potential.
  - `fetch_gauge_data` always seeds result entries for all gauges, then returns `{}` on error. That matches README “exit on failure” for once mode, but in adaptive/TUI modes an empty dict means “no rows” with backoff, which is correct but worth keeping in mind for UX (possible “is it dead?” confusion).

**State management & backfill**

- Positives:
  - `_cleanup_state` and `backfill_state_with_history` are very careful about de-duplication, timestamp alignment, and clamping cadences (`streamvis.py:216`–`streamvis.py:255`, `streamvis.py:317`–`streamvis.py:362`).
  - State fields are all small bounded lists (`HISTORY_LIMIT`), preventing unbounded growth over long runtimes.
  - Backfill only runs when `hours_back` increases, tracked via `meta.backfill_hours` (`streamvis.py:369`–`streamvis.py:381`), so you don’t keep re-querying the same window.
- Risks / observations:
  - There’s no locking on the state file; running multiple `streamvis` instances (TUI + adaptive, etc.) against the same `--state-file` could interleave writes unpredictably.
  - `meta` is used for multiple unrelated concerns (`backfill_hours`, last fetch/success/failure, forecast refresh) with no schema versioning – fine for now, but brittle if you add more complexity.

**Cadence learning & scheduler**

Update detection and learning (`update_state_with_readings`, `predict_gauge_next`):

- Only strictly newer observation timestamps count as updates (`observed_at > prev_ts`), otherwise the cadence and history remain unchanged (`streamvis.py:754`–`streamvis.py:781`).
- Learned cadence is EWMA on clamped deltas in `[MIN_UPDATE_GAP_SEC, MAX_LEARNABLE_INTERVAL_SEC]` (`streamvis.py:784`–`streamvis.py:808`).
- Latency windows are derived by bracketing the new observation between the last poll where it was invisible and the current poll where it appears; midpoints feed robust median/MAD stats (`streamvis.py:814`–`streamvis.py:854`).
- `predict_gauge_next` takes the last observation time, walks forward in increments of `mean_interval_sec` until it passes `now`, and then adds the median latency (`streamvis.py:866`–`streamvis.py:887`).

Scheduler (`schedule_next_poll`):

- For each gauge with state:
  - Clamp its mean interval (`streamvis.py:922`).
  - Ask `predict_gauge_next` for the next API-visibility time (`streamvis.py:923`).
  - Decide between fine and coarse regimes based on `latency_mad_sec` and `mean_interval <= 3600` (`streamvis.py:927`–`streamvis.py:933`).
- Fine regime:
  - Inside a “latency window” ±lat_width around `next_api`, poll with step between 5 and 30 seconds (`FINE_STEP_MIN_SEC`, `FINE_STEP_MAX_SEC`) (`streamvis.py:935`–`streamvis.py:947`).
- Coarse regime:
  - Poll at max of `min_retry_seconds` and a fraction of the mean interval (no hard cap beyond cadence clamping) (`streamvis.py:950`–`streamvis.py:965`).
- Multi-gauge coordination picks the earliest candidate time across gauges (`streamvis.py:979`–`streamvis.py:983`).

Design vs implementation:

- **Politeness vs `max_retry_seconds`**
  - `README.md:68`–`70` documents `--max-retry-seconds` as “ceiling when backing off on errors.”
  - In `schedule_next_poll`, `max_retry_seconds` is used as a *global max normal poll horizon* (`streamvis.py:979`–`983`).
  - Result: even if a gauge’s learned cadence is 2–6 hours, the scheduler never waits longer than `max_retry_seconds` (default 300s), so you will still poll at least every 5 minutes during normal operation.
  - This contradicts the “cadence mimics natural interval” and “~1 call per update” story in `README.md:51`–`55` and `notes/MEMORY.md`.
  - This is the single biggest conceptual mismatch: for very slow gauges, you’ll be significantly *more* aggressive than their true cadence, unless the user manually raises `--max-retry-seconds` to align with the data interval. The docs do not currently explain this interaction.

- **Fine window call volume**
  - Inside a fine window, you can poll as frequently as every 5 seconds (`FINE_STEP_MIN_SEC`), which is far more aggressive than the 1/min guardrail mentioned in `notes/SCRUTINY.md:5`–`10`.
  - In practice, fine windows are narrow and only engage for gauges with stable latency and cadence ≤ 1 hour, so the *average* rate might still be acceptable. But the worst-case per-window rate is significantly above “1/min”.
  - Given the design intent, it would be good either to:
    - Document this “short, intense bursts are allowed” philosophy explicitly, or
    - Raise `FINE_STEP_MIN_SEC` (e.g., 15–30s) so fine windows are still gentle.

- **Multi-gauge fairness and HEADSTART**
  - The `HEADSTART_SEC` of 30 seconds is applied per gauge when walking towards `next_api`; combined with coarse steps of up to 300s, you generally arrive slightly before the predicted update, which is good.
  - Using the earliest candidate across gauges ensures a single multi-gauge request serves everyone, but there’s a subtle bias: a single “fast” gauge can drag the global cadence down, effectively oversampling slower gauges sharing the same call. Given the shared-call optimization, this is probably acceptable, but it means “~1 call per update” is defined per *system*, not per-station.

**Forecast integration**

- Implementation:
  - URL templates with `{gauge_id}` and `{site_no}` placeholders (`_resolve_forecast_url` in `streamvis.py:384`).
  - `fetch_forecast_series` uses shape-agnostic JSON parsing with explicit “shape assumptions” (validTime/time/ts, stage_ft/stage/value, flow_cfs/flow) and returns a sorted list of points (`streamvis.py:489`–`523`).
  - `summarize_forecast_points` computes 3h, 24h, and full-horizon maxima, ignoring past timestamps (`streamvis.py:526`–`563`).
  - `update_forecast_state` de-duplicates forecast points, stores summaries, computes amplitude bias vs the nearest forecast point, and estimates phase shift vs observed peak (`streamvis.py:566`–`620`).
  - `maybe_refresh_forecasts` globally rate-limits forecast fetches to once per `FORECAST_REFRESH_MIN` minutes (`streamvis.py:623`–`651`).
- This matches the narrative in `README.md:72`–`90` and `notes/MEMORY.md` around treating forecasts as an overlay and clearly marking assumptions.
- Scrutiny:
  - The forecast horizon is passed as `horizon_hours` query parameter (`streamvis.py:505`–`510`); since this is a guess at the NWPS API, you’ll almost certainly need to make this mapping configurable once you integrate real endpoints.
  - There’s no handling of forecast series that contain multiple variables for different lead times or ensemble members – the code assumes a flat list. This is fine for a first pass but worth calling out as a limitation.
  - Forecast state is not pruned; only `points` and `summary` are stored, but there’s no explicit cap; if the upstream API returns a very long horizon repeatedly, `points` might grow. In practice most forecast APIs bound horizons, but a simple `HISTORY_LIMIT`-like trim on forecast points would remove this risk.

**TUI & UX**

- Overall:
  - The TUI is thoughtfully structured: clear header, main table, optional detail pane with two modes (compact sparkline vs expanded table + trends + forecast/latency) (`draw_screen` in `streamvis.py:1082`–`1260`).
  - It respects terminal size reasonably: stops rendering rows when `row >= max_y - 5` and checks bounds before adding detail/footers.
  - Keyboard model (`↑/↓`, `j/k`, `Enter`, `c`, `r`, `q`) is well chosen and clearly advertised in the footer (`streamvis.py:1269`–`1277`).

- **Bug – uninitialized `dh` in trend calculation**
  - In the detail view, trend computation does:
    - Build `times`, `stages`, and `flows` (`streamvis.py:1208`–`1217`).
    - If `times and stages`, compute `dh` and `stage_trend` (`streamvis.py:1218`–`1221`).
    - Else, set `stage_trend = 0.0` without setting `dh` (`streamvis.py:1221`–`1222`).
    - Then, if `flows`, compute `flow_trend` using `dh` (`streamvis.py:1223`–`1224`).
  - If you ever have history points with `flow` present but `stage` missing (e.g., a gauge without stage data), `times`/`stages` remain empty while `flows` is non-empty. That produces an `UnboundLocalError` on `dh`.
  - Right now, `update_state_with_readings` always stores both `stage` and `flow` from `fetch_gauge_data`, so *given current USGS usage* this may not surface. But as soon as you support gauges that only expose one metric, this breaks. A robust fix would initialize `dh` to a default (e.g., 1.0) before the `if times and stages` branch, or derive a time span from the history regardless of stage availability.

- **Minor UX inconsistency – “Next ETA” in TUI vs CLI**
  - In the CLI table, if `next_eta` is `None` or in the past, you display `"now"` (`render_table` in `streamvis.py:1022`–`1030`).
  - In TUI, `next_str = _fmt_rel(now, next_eta)` is used directly (`streamvis.py:1114`–`1119`), so `next_eta` in the past renders as `"ago 3m"`, which doesn’t make sense for a “Next ETA”.
  - This is small but noticeable; harmonizing the logic would make the TUI feel more polished.

- Other observations:
  - The main table and detail views are careful about truncation with `[:max_x - 1]`, so wide lines won’t explode small terminals.
  - The sparkline generator is compact and elegant (`_render_sparkline` in `streamvis.py:1000`–`1019`); it naturally degrades when there’s little variation.

**CLI modes & process behavior**

- Modes:
  - `once`: do a single fetch, update state, optionally refresh forecasts, render table, exit with 0/1 (`main` in `streamvis.py:1493`–`1520`).
  - `adaptive`: infinite loop with sleep-based scheduling, updating state & optionally printing table (only on new updates or first run), with exponential backoff on errors (`adaptive_loop` in `streamvis.py:1328`–`1380`).
  - `tui`: curses wrapper, non-blocking input, UI tick at 150 ms, sharing the same scheduler logic (`tui_loop` in `streamvis.py:1040`–`1326`).
- Good things:
  - Exit codes match README promises for once mode (`README.md:36`–`39` and `main`).
  - Backoff on error uses doubling up to `args.max_retry_seconds`, and adaptive/TUI both update state with `last_failure_at` / `next_poll_at` metadata (`streamvis.py:1339`–`1350`, `streamvis.py:1292`–`1304`).
- Scrutiny:
  - There’s no user-facing way to cap total runtime or maximum number of polls; that’s fine for an interactive tool but worth noting if you ever embed it into automated environments.
  - `predict_next_poll` is retained as a “legacy helper” but unused (`streamvis.py:860`–`864`). Not harmful, but a candidate for removal or for tests to assert compatibility.

**Config & extensibility**

- `config.toml:1`–`40` contains authoritative station metadata, USGS base URLs, and placeholders for NOAA IDs and endpoints.
  - A minimal TOML loader in `streamvis.py` now reads this file when present.
  - Station bindings (`gauge_id` → `usgs_site_no`) and the primary USGS IV base URL are taken from `config.toml` when available, falling back to the hard-coded Snoqualmie defaults otherwise.
  - Forecast configuration in `config.toml` (per-station `forecast_endpoint` and `[global.noaa_nwps].default_forecast_template`) is honored when non-empty, while CLI `--forecast-base` remains the highest-precedence override.
- This wiring reduces duplication and keeps station additions/changes in config, but `FLOOD_THRESHOLDS` are still code-only for now; moving thresholds into config is a possible future refinement.

**Style, types, and maintainability**

- Style:
  - Consistent use of ALL_CAPS constants and small single-purpose helpers is good; most functions are well under 100 lines.
  - Naming is descriptive and aligned with domain concepts (“latency_window”, “backfill”, “forecast_series”).
- Typing:
  - The core public helpers use type hints (including `|` unions) and `from __future__ import annotations`, which is consistent with the project guidelines.
  - Some internal structures still rely on `Dict[str, Any]` and `List[Dict[str, Any]]`, which is fine for a single-file CLI but could be refined by introducing small `TypedDict`-like aliases if you ever add tests or mypy.
- Tests:
  - There are no tests, and this code *begs* for a thin synthetic test harness for:
    - Schedule behavior across different cadences and latencies.
    - State cleanup/backfill merge cases.
    - Forecast summarization logic.
  - Given how subtle the scheduler is, tests would be the single most leverageful investment in long-term confidence.

**Key risks and concrete issues**

1. **Scheduler over-aggressiveness for slow gauges**
   - (Original) `schedule_next_poll` never scheduled beyond `now + max_retry_seconds` (`streamvis.py:979`–`983`), even when the learned cadence is much longer, which could produce much more frequent polling than the true cadence for slow-updating stations, contradicting the “~1 call per update” objective in `README.md:51`–`55`.
   - Resolution: normal scheduling now ignores `max_retry_seconds` and is driven solely by `mean_interval_sec` (clamped) and latency stats; `--max-retry-seconds` is reserved for error backoff. In addition, once at least three update intervals have been observed for a gauge, if the learned mean interval remains significantly shorter than the empirical average of those intervals, we snap the mean upward toward that average so slow gauges (e.g., hourly) converge quickly to their true cadence even when starting from an 8‑minute prior.

2. **Fine-window polling rate vs documented guardrail**
   - Fine windows can poll every 5 seconds (`FINE_STEP_MIN_SEC` in `streamvis.py:15` and logic at `streamvis.py:935`–`947`).
   - `notes/SCRUTINY.md:5`–`10` expresses a desire to avoid “rapid-fire polling (e.g., 1/min) unless explicitly requested”.
   - Even if the *average* call rate is low, the per-window rate substantially exceeds that figure.

3. **TUI trend calculation bug**
   - `dh` may be used without being defined when flows are present but stages are not (`streamvis.py:1218`–`1224`).
   - This is a real runtime bug for any gauge that only has flow data. Even if USGS currently provides both metrics for your stations, it’s a brittle assumption.

4. **Minor TUI UX inconsistency**
   - “Next ETA” in TUI can show “ago X” when predictions are in the past (`_fmt_rel` at `streamvis.py:55`–`71` vs usage in `render_table` vs `draw_screen`).
   - This is cosmetic but worth cleaning up.

5. **Forecast point list unbounded**
   - `update_forecast_state` doesn’t explicitly cap `points` length (`streamvis.py:566`–`596`).
   - If the upstream forecast API returns very long horizons or dense time steps, this could grow large over long execution.

6. **Potential state races**
   - No locking around the state file means concurrent instances can step on each other’s writes.
   - For a single-user CLI this is acceptable but worth noting explicitly if you ever suggest running multiple modes in parallel.

**High-impact follow-ups recommended**

- Adjust the scheduler so `max_retry_seconds` governs *error backoff only*, and normal polling horizon is driven by `mean_interval_sec` (clamped by `MAX_LEARNABLE_INTERVAL_SEC`), possibly with a separate “max normal interval” knob.
- Soften fine windows slightly or document them clearly as “short, targeted bursts” that intentionally exceed the 1/min guardrail when latency is highly predictable.
- Fix the TUI trend bug by always defining `dh` even when no stage data is present (`streamvis.py:1218`–`1224`).
- Unify “Next ETA” semantics between CLI and TUI so “ago” never appears in a field labelled “next” (`render_table` vs `draw_screen`).
- Add a simple trimming step for forecast points similar to `HISTORY_LIMIT` to bound memory (`update_forecast_state`).
- Longer term: finish wiring `config.toml` into any remaining code-only knobs (e.g., flood thresholds) and introduce a small test harness to exercise `update_state_with_readings` + `schedule_next_poll` under synthetic cadences and latencies.

**Browser/Pages deployment considerations (Pyodide build)**

- The Pyodide + `web_curses` path keeps the core TUI logic in `streamvis.py` but swaps:
  - HTTP through `http_client` (requests vs `pyodide.http.open_url`), and
  - The terminal backend through a thin curses shim that draws into the DOM.
- Risks / checks:
  - Verify that USGS/NWRFC/NWPS endpoints required for the browser build are consistently CORS-enabled for direct browser access.
  - Confirm that the fixed canvas size in `web_curses.initscr` (rows/cols) is sufficient for typical mobile/desktop viewports and that overflow is at worst clipped, not mis-rendered.
  - Ensure localStorage state syncing (`streamvis_state.json` ↔ `streamvis_state_json`) is robust to first-run (no file) and very large state files; consider trimming or compressing if the browser state grows.
- Validation ideas:
  - Smoke-test the GitHub Pages build in Chrome/Firefox/Safari, including mobile, to ensure keyboard input, redraw, and state persistence behave as expected.
- When adding future features, keep the curses surface area small so that the `web_curses` shim stays easy to maintain and reason about.

## 2025-12-18 – Pyodide package loader vs modularization

- **Issue**: `web/main.js` previously wrote fetched Python files into the Pyodide FS by filename only (dropping directories), so `streamvis/tui.py` became `tui.py`. After modularization, this broke `import streamvis.*` in the browser build and encouraged “make tui.py standalone again” regressions.
- **Resolution**: the browser loader now preserves relative paths (creating directories) and installs all `streamvis/*.py` files before importing `streamvis` via `web_entrypoint`.
- **Residual risk**: the JS `streamvisFiles` list must be updated when new modules are added under `streamvis/`; mitigate by adding a quick Pages smoke-check whenever touching package layout.

## 2025-12-11 – Web responsive overflow follow-up

- **Issue**: On some iOS/Safari viewports the rightmost column could still be clipped even after font‑first adaptation, due to optimistic cols estimation (padding not subtracted, char width assumed as `font_px * 0.55`).
- **Resolution**:
  - `web_curses._measure_terminal` now derives usable text width/height by subtracting DOM padding and measures actual monospace `char_width_px`/`row_height_px` via a hidden span cached per font, making `getmaxyx()` conservative and accurate.
  - `web/main.js` now calibrates `charFactor` from the same real DOM measurement and targets the full 59‑column wide header in portrait (62 in landscape) before allowing column drops.
- **Residual risk**: If a user applies custom fonts or zoom levels that break monospace assumptions, measured factor should still track it, but we may want a small “fit safety margin” knob if we see edge cases; tracked in backlog if it resurfaces.

## 2025-12-11 – Cadence model re-prioritized

- **Change**: Adaptive polling now assumes observation cadences are multiples of 15 minutes (typ. 15/30/60) and snaps learned intervals to the best‑supported multiple (`cadence_mult`) once ≥3 deltas/backfill points agree.
- **Why it matters**: The old 8‑minute prior + free‑form EWMA could take many hours to converge on hourly gauges, and missed updates would bias the mean upward in ways that degraded low‑latency windowing. The grid prior yields faster convergence and more stable ETA predictions.
- **Correctness checks**:
  - Snapping only occurs when deltas land within ±3 minutes of a 15‑minute multiple and when the divisible‑multiple fit exceeds 0.6.
  - If a station deviates from grid behavior, fit drops and we clear `cadence_mult`, reverting to EWMA of raw deltas.
- **Residual risks / follow‑ups**:
  - If a gauge has a stable cadence that is not a 15‑minute multiple (rare), it should remain in EWMA mode, but a sustained near‑grid cadence could still “snap” incorrectly if jitter stays within tolerance; tune thresholds if this appears.
  - We do not yet estimate a separate phase offset; predictions are anchored to the last observed timestamp. If we see systematic phase drift (e.g., timestamps jittering around a boundary), consider adding a robust per‑gauge phase estimator. Logged in backlog if needed.

## 2025-12-11 – Nearby feature risks

- **Geolocation UX/privacy**: Nearby prompts for browser location only when toggled on, but we do persist the last lat/lon in browser localStorage via state; acceptable for now but note if users want a “don’t store location” option.
- **Layout pressure**: Nearby consumes up to 4 lines above the footer; on very small screens or in deep detail mode it may be clipped. This is intentional graceful degradation.

## 2025-12-11 – Dynamic Nearby discovery risks

- **API shape stability**: The NWIS Site Service uses legacy RDB; parser assumes standard column names (`site_no`, `station_nm`, `dec_lat_va`, `dec_long_va`). If USGS changes headers, Nearby discovery will silently fail soft.
- **Dynamic ID collisions**: Dynamic gauges are assigned short `Uxxxxx` ids from site number suffix; collisions are unlikely but handled by numeric fallback. If users enable Nearby in dense areas, main table may grow beyond the original Snoqualmie focus.
- **Rate/politeness**: Discovery is gated to at most once per 24h per state file, and only on Nearby enable or first location availability.

## 2025-12-12 – `modifiedSince` optimization scrutiny

- **Semantics**: `modifiedSince` on IV is a *duration*, not an absolute timestamp, and filters out stations with no changes in that window. We gate usage to fast‑cadence‑only sessions to avoid missing slow‑gauge updates. citeturn0search0turn0search1
- **UX risk**: When `modifiedSince` suppresses a station, IV omits its time series entirely. We now backfill display values from state and still count a no‑update poll by setting `observed_at` to the last stored timestamp.

## 2025-12-12 – Phase/biweight latency scrutiny

- **Phase estimator**: Uses biweight location on unwrapped modulo offsets; should handle wrap-around, but if a gauge switches cadence multiples mid‑run we may briefly predict a wrong phase until new history accumulates. Cadence snap logic clears `cadence_mult` when fit drops, which implicitly disables phase.
- **Latency prior bias**: The 600s±100s prior is strong early; if a gauge has materially different latency, fine windows will wait for a few samples before tightening. We can retune priors if field data suggests otherwise.
- **Biweight robustness**: Biweight downweights large outliers but still assumes a roughly unimodal core; if latency becomes bimodal (e.g., periodic batch publishes), scale may be underestimated. Watch `control_summary` during storms and adjust if needed.

## 2025-12-10 – Meta scrutinizer refinement

- **Critical – TUI trend crash when stages are absent** (`streamvis.py:1219`–`1225`): `dh` is only set when stage data exists, yet the flow trend divides by `dh` regardless. A flow-only gauge would raise `UnboundLocalError`. Seed `dh` from the time span of the flow samples (or default to `1.0`) before either trend calculation.
  - Resolution: trend computation now derives a shared time span from all recent timestamps, initializes a default span when necessary, and only computes stage/flow trends when there are at least two valid samples, avoiding undefined `dh`.
- **High – Normal poll horizon tied to error backoff** (`streamvis.py:979`–`983`): `max_retry_seconds` (documented as an error backoff ceiling) also capped normal scheduling. Slow gauges (hours-long cadence) got polled every `max_retry_seconds` (default 5 minutes), violating the “~1 call per update” promise and the 8-minute baseline story. Resolution: `schedule_next_poll` now ignores `max_retry_seconds` and bases normal cadence solely on learned intervals and latency stats; error backoff still uses `max_retry_seconds`.
- **High – Fine-window burstiness exceeds “polite” narrative** (`streamvis.py:935`–`947`): fine windows poll every 5–30s when latency is stable. For a 15-minute cadence, that is up to ~12 extra calls per update cycle. Resolution: floor raised to 15 seconds between fine-window polls; still consider adding instrumentation to quantify duty cycle.
- **Medium – “Next ETA” in TUI can point to the past** (`streamvis.py:1123`–`1160`): `_fmt_rel` yields “ago …” in a column labeled “Next ETA,” unlike the CLI which snaps past/None to “now.” Align the semantics (or label as “stale”) and consider rescheduling immediately when the predicted next time is behind `now`.
  - Resolution: both CLI and TUI now render “Next ETA” as `now` when the predicted time is in the past; `predict_gauge_next` was also updated to avoid “skipping” the immediate next cadence interval unless we are clearly more than two full intervals beyond the last observation, preventing confusing 2×-cadence ETAs when we are only slightly late.
- **Medium – Forecast state grows unbounded and can hold stale horizons** (`streamvis.py:636`–`680`): `g_forecast["points"]` was never trimmed. Dense feeds or long horizons could grow the state file and bias summaries against old data. Resolution: points are now deduplicated and trimmed to a window around “now” based on the configured horizon before computing summaries/bias.
- **Medium – Config drift risk**: USGS URLs, `SITE_MAP`, and thresholds live in code while `config.toml` mirrors them. Until config loading exists, document the “code is authoritative” stance; afterwards, centralize the single source to avoid silent divergence.
- **Low – State write robustness/locking**: concurrent runs or crashes mid-write can corrupt `~/.streamvis_state.json` (writes were non-atomic). Resolution: state writes now go via a temporary file + atomic rename; locking for multi-writer scenarios remains future work.
- **Validation plan**: add synthetic schedule tests (fast vs slow cadence; high vs low latency MAD; mixed gauges to observe shared-call bias) and a metric that logs “calls per station per real update” to verify the politeness envelope and fine-window duty cycle. Tracked in `notes/BACKLOG.md`.

## 2025-12-11 – Comprehensive repo review and P0 remediation

**High-level**

- The repo is compact and well‑reasoned: core logic lives in `streamvis.py`, with tiny shims for HTTP (`http_client.py`) and browser TUI compatibility (`web_curses.py`, `web_entrypoint.py`). Notes system is strong and keeps intent vs reality clear.
- Adaptive cadence + latency‑window scheduling is the main intellectual asset; TUI and optional forecast/NW RFC overlays are integrated without heavy dependencies.

**Strengths**

- Soft‑fail network/API handling across all fetchers; callers degrade gracefully without busy‑looping (`fetch_gauge_data`, `fetch_gauge_history`, `fetch_forecast_series`, `maybe_refresh_nwrfc`).
- State integrity is robust: de‑dup by timestamp, clamp cadence, cap history, atomic writes (`_cleanup_state`, `save_state`).
- Scheduler design is thoughtful: EWMA cadence learning + latency median/MAD + coarse→fine regimes (`update_state_with_readings`, `predict_gauge_next`, `schedule_next_poll`).
- TUI UX is clear and efficient: fast tick, bounded rendering, detail panes only when requested, forecast/NW RFC shown only when available.
- Dependency footprint stays minimal (requests only; TOML parsing done in‑house for the small subset needed).

**P0 – Must‑fix issues**

1. **Packaging broke installed CLI** (now resolved).
   - Symptom: `pip install .` produced a console script that imported `streamvis`, but `streamvis` imports `http_client`; setuptools only packaged `streamvis.py`, so installed runs failed with `ModuleNotFoundError: http_client`.
   - Fix: include `http_client`, `web_curses`, and `web_entrypoint` in `pyproject.toml` `py-modules` so installed CLI and optional browser entrypoint work out of the box.
   - Resolution: patched on 2025‑12‑11.

2. **Browser TUI busy‑loop / UI starvation risk** (now mitigated).
   - Symptom: `tui_loop` relies on `curses.timeout()` to throttle `getch()`, but the Pyodide shim returned immediately when no key, causing a tight loop and pegged CPU in the browser.
   - Fix: implement basic timeout semantics in `web_curses._Window.getch()` by sleeping for the configured timeout when input is empty and nodelay is off.
   - Trade‑off: this is a synchronous sleep, so keypresses are only processed between ticks; acceptable for sub‑second UI while the longer‑term async driver remains on the backlog.
   - Resolution: patched on 2025‑12‑11.

**P1 – High‑impact follow‑ups**

- **Coarse polling oversampling slow gauges** (resolved 2025‑12‑11): removed the 5‑minute hard cap so coarse steps scale with learned cadence; added per‑gauge “calls/update” instrumentation (last + EWMA) surfaced in expanded TUI detail.
- **Web hosting path clarity** (resolved 2025‑12‑11): browser loader now tries both `./module.py` and `../module.py`; README documents the two supported GitHub Pages layouts.
- **Scheduler regression tests** (resolved 2025‑12‑11): added stdlib unit coverage for coarse scaling, fine‑window stepping, and cadence snap‑up in `tests/test_scheduler.py`.

**P2 – Medium priority**

- **Config/comment drift** (partially resolved 2025‑12‑11): updated `config.toml` header to reflect live wiring; several fields remain advisory/future‑facing.
- **State multi‑writer risk** (resolved 2025‑12‑11): added a best‑effort single‑writer lock via `fcntl` and documented the behavior in README.
- **Defaults mismatch** (resolved 2025‑12‑11): added `EDGW1` to built‑in defaults so `PRIMARY_GAUGES` is consistent out of the box.
- **Minor robustness** (resolved 2025‑12‑11): preserve last non‑None stage/flow on partial reads and coerce numeric forecast strings.

**P3 – Nice‑to‑have**

- Hoist some nested TUI helpers to top level (resolved 2025‑12‑11; shared `draw_screen`/`color_for_status`).
- Optional debug/control summary logging for cadence/latency tuning (resolved 2025‑12‑11; `--debug` + `control_summary`).
- Add a simple state schema version for forward compatibility (resolved 2025‑12‑11; `meta.state_version`).

## 2025-12-11 – Pyodide/iOS responsiveness risk

- **Risk – synchronous TUI loop blocks Safari**: running the native `tui_loop` inside Pyodide on the main thread can starve the JS event loop, leading to black screens and tab hangs on iOS.
  - Resolution: added `web_tui_main()` using `asyncio` to yield every UI tick, and switched the browser entrypoint to use it.
  - Residual check: if future features add long synchronous work per tick, re‑audit that the async loop still yields frequently.

## 2025-12-12 – Community priors + localStorage flush risks

- **Medium – Browser localStorage quota / perf**: syncing state to `localStorage` on every `save_state()` increases write frequency vs “only on clean exit.” If the state grows (e.g., more dynamic sites or long forecast histories), Safari may hit a storage quota and throw. Resolution: writes are wrapped in try/except so runtime is unaffected; if this becomes real, consider trimming dynamic‑site history or compressing JSON before storage.

- **Medium – Remote priors trust / staleness**: community summaries could be stale, biased, or malicious. Resolution: client only adopts priors when local confidence is low (<3 latency samples or weak cadence snap), so local learning quickly dominates; still, UI could optionally label “seeded from community” vs “learned locally” later.

- **Low – Publishing failure visibility**: native clients ignore POST failures by design; operators might not realize publishing is ineffective. Resolution: acceptable for now; a future `--debug` log line could emit last publish success/failure timestamps without changing default UX.

## 2025-12-14 – Web publishing and quota fallback follow-ups

- **Medium – localStorage quota now mitigated, but not eliminated**: browser persistence now falls back to a slim state if a full write fails, which should preserve cadence/latency learning. Residual risk: even the slim state could exceed quota in extreme cases (many dynamic sites). Mitigation: we silently keep running; future work could evict least-recently-used dynamic gauges from the persisted state.

- **Low – Web publish queue behavior under offline/slow networks**: web publishing is queued and drained asynchronously to keep iOS responsive, but if the network is down the queue could drop samples. Mitigation: queue is capped (drops oldest) and publishing is “best effort” by design; add optional debug timestamps if we need operator visibility.

- **Low – Persisted publish opt-in**: the browser caches `publish=1` in localStorage for convenience. Risk: a user could forget it’s enabled. Mitigation: publishing remains opt-in; consider a small UI indicator later if this matters.
