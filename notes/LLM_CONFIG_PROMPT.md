# LLM_CONFIG_PROMPT.md — streamvis

Use this prompt (plus the current `config.toml`) with a search-enabled LLM to auto-populate station-level forecast metadata from NOAA / NWPS while preserving USGS IV as the ground-truth observed-data source.

You can paste this prompt verbatim and then append the current contents of `config.toml` where indicated.

---

I have a small river-gauge watcher called `streamvis` that:

- Uses **USGS NWIS Instantaneous Values (IV)** as the authoritative, lowest-latency source for observed stage and flow.
- Wants to layer in **official river forecasts** from NOAA’s **National Water Prediction Service (NWPS)** as a separate forecast-only source.
- Tracks update cadence and latency per station and is designed to be very “polite”: roughly one HTTP call per real update, with fast convergence on low latency.

I will paste a `config.toml` below that already contains:

- Global USGS config:
  - `iv_base_url = "https://waterservices.usgs.gov/nwis/iv/"`
  - `iv_api_base_url = "https://api.waterdata.usgs.gov/nwis/iv/"`
- Global NWPS config:
  - `base_url = "https://water.noaa.gov/"`
  - `default_forecast_template = ""` (to be filled in)
- Per-station blocks for four Snoqualmie-system gauges:
  - `TANW1`, `GARW1`, `SQUW1`, `CRNW1`
  - Each has:
    - `gauge_id` (e.g., `"TANW1"`)
    - `display_name`
    - `usgs_site_no` (e.g., `"12141300"`)
    - Empty fields to be populated:
      - `nws_lid`            # NWS/NWPS location identifier
      - `nwps_station_id`    # NWPS station ID, if distinct
      - `forecast_endpoint`  # fully-resolved forecast endpoint URL
      - `fast_changes`       # boolean: does this station tend to update faster than standard 15‑minute cadence?

**Your tasks**

Using up-to-date USGS, NOAA NWPS, and related official documentation (e.g., `https://waterservices.usgs.gov/docs/instantaneous-values/instantaneous-values-details/`, `https://water.noaa.gov/about/api`, NWPS product docs, Drought.gov NWPS pages), please:

1. **Confirm USGS observed-data source-of-truth**
   - Verify that using USGS NWIS Instantaneous Values (`waterservices.usgs.gov/nwis/iv/` and the modernized `api.waterdata.usgs.gov/nwis/iv/`) is indeed the lowest-latency, authoritative public source for **observed** stage and flow at these gauges, compared to NWPS.
   - If there is a more appropriate “modernized” IV endpoint that should be preferred for these sites (e.g., path, version), mention it and ensure it is compatible with the existing query structure (`sites=...&parameterCd=00060,00065&format=json`).

2. **Populate NWPS / forecast metadata per station**
   - For each `[stations.<gauge_id>]` block:
     - Fill `nws_lid` with the official NWS/NWPS location identifier for that gauge (e.g., the LID used in hydrographs and NWPS products).
     - Fill `nwps_station_id` if NWPS uses a distinct station ID separate from `nws_lid` or the USGS site number. If not distinct, you can mirror the appropriate identifier.
     - Fill `forecast_endpoint` with the **best low-latency NWPS API endpoint** for river stage/flow forecasts at that gauge. Requirements:
       - It should be an official `water.noaa.gov` or related NWPS API URL.
       - It should return forecast time series (stage/flow vs time) suitable for machine consumption.
       - Prefer documented, stable API paths over scraping HTML or graphics.
     - Set `fast_changes` to `true` if the station is known to:
       - Report observations with a typical cadence significantly faster than 15 minutes (e.g., 1–5 minutes), OR
       - Have telemetry / API updates that frequently push new values more often than 15 minutes, especially in flood conditions.
       Otherwise, set `fast_changes = false`.

3. **Propose a default forecast template**
   - Under `[global.noaa_nwps]`, propose a `default_forecast_template` that:
     - Works as a URL template for the majority of these Snoqualmie gauges.
     - May contain `{gauge_id}`, `{site_no}`, and/or `{nws_lid}` placeholders.
     - Reflects the actual pattern you see in the station-specific `forecast_endpoint` URLs.
   - This template will be used by the code as a fallback; per-station `forecast_endpoint` fields can override it.

4. **Respect and preserve existing fields**
   - Do **not** change:
     - `gauge_id`
     - `display_name`
     - `usgs_site_no`
     - `global.usgs` URLs
   - Only fill or refine:
     - `global.noaa_nwps.default_forecast_template`
     - Each station’s `nws_lid`, `nwps_station_id`, `forecast_endpoint`, and `fast_changes`.

5. **Be explicit about assumptions**
   - For each station, briefly note:
     - Where you found the NWPS mapping (link to the relevant NOAA docs or station page).
     - Any assumptions you made about the forecast endpoint structure or field naming.
   - If you cannot find a reliable NWPS forecast endpoint for a specific station, leave its `forecast_endpoint` empty and explain why.

**Output format**

- Return a fully updated `config.toml` with all the fields you were able to populate.
- After the TOML, include a short bullet list summarizing for each station:
  - `nws_lid`
  - `nwps_station_id`
  - Whether you set `fast_changes = true`
  - The forecast endpoint you chose and, if applicable, the template pattern it fits.

Here is the current `config.toml` for you to update:

```toml
[PASTE CURRENT config.toml HERE]
```

---

