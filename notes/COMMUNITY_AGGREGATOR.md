# Community cadence/latency aggregator

This is an optional, low/zero‑config “community” service that multiple
`streamvis` clients can use to share **priors** about:

- Typical observation cadence (snapped to 15‑minute multiples).
- Cadence phase offset on that grid.
- Observation → USGS IV visibility latency (robust location/scale).

Native and web clients can **publish** per‑update samples, and both native + web
clients can **read** shared summaries to seed cold starts.

The service is intentionally simple and privacy‑conservative. Clients only
share per‑station timing; no user identifiers or locations.

## Endpoints

### `GET /summary.json`

Returns current shared priors.

Shape:

```json
{
  "version": 1,
  "generated_at": "2025-12-12T00:00:00Z",
  "stations": {
    "12141300": {
      "cadence_mult": 1,
      "cadence_fit": 0.85,
      "phase_offset_sec": 240.0,
      "latency_loc_sec": 610.0,
      "latency_scale_sec": 95.0,
      "samples": 42,
      "updated_at": "2025-12-11T23:45:00Z"
    }
  }
}
```

Notes:
- `stations` is keyed by USGS `site_no`.
- `cadence_mult` is a multiple of `CADENCE_BASE_SEC` (900s).
- `phase_offset_sec` is in `[0, cadence_mult * 900)`.
- `latency_loc_sec` / `latency_scale_sec` should match the robust Tukey biweight
  stats used by `streamvis` (or median/MAD if the service is simpler).

### `POST /sample`

Accepts a new latency sample for a station.

Payload:

```json
{
  "version": 1,
  "site_no": "12141300",
  "gauge_id": "TANW1",
  "obs_ts": "2025-12-11T23:30:00Z",
  "poll_ts": "2025-12-11T23:40:12Z",
  "lower_sec": 540.0,
  "upper_sec": 612.0,
  "latency_sec": 600.0
}
```

Semantics:
- `obs_ts` is the USGS observation timestamp that advanced.
- `poll_ts` is when the client first saw it in IV.
- `lower_sec` / `upper_sec` bound the true latency window.
- `latency_sec` is the client’s best estimate within that window.

Response: any JSON confirming acceptance (or an error). Clients ignore failures.

## Client behavior

### Reading (`--community-base`)
- If set, clients fetch priors from `{base}/summary.json` (or `base` directly if
  it already ends in `.json`) at most once per 24h.
- A remote prior is adopted **only** if local confidence is low:
  - No `cadence_mult` or `cadence_fit < CADENCE_FIT_THRESHOLD`.
  - No `phase_offset_sec`.
  - Fewer than 3 local latency samples.

### Publishing (`--community-base` + `--community-publish`)
- Native clients POST once per **real update** (advanced observation timestamp).
- Web/Pyodide clients publish using an async fetch path, queued so it does not
  block the UI tick on iOS/Safari. Failures are ignored.

## Privacy / safety

- Payloads contain only per‑station timing.
- No location, device IDs, or user identifiers.
- Services should avoid logging raw IPs if possible, but basic HTTP logs are OK.
- Rate‑limit by IP and/or station to protect infrastructure; expected traffic is
  very low (≈ 1 POST per update per user).

## Deployment notes

Any static host + tiny serverless function works. Suggested setups:

- Cloudflare Worker + KV/D1
- AWS Lambda + DynamoDB
- Fly.io / small VPS with a tiny Flask/FastAPI app

See `serverless/community_worker.js` for a minimal Worker example.
