// Minimal Cloudflare Worker example for streamvis community priors.
// This version stores data in-memory (ephemeral). For real use, back this with
// KV/D1/Durable Objects.

const CADENCE_BASE_SEC = 900; // 15 minutes
const HISTORY_LIMIT = 120;

const SITES = new Map(); // site_no -> { latencies: [], deltas: [], obsTimes: [], lastObs: number|null }

function median(arr) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function mad(arr, med) {
  const dev = arr.map((x) => Math.abs(x - med));
  return median(dev);
}

function tukeyBiweightLocationScale(samples, initialLoc, initialScale, cLoc = 6.0, cScale = 9.0, maxIters = 5) {
  let loc = initialLoc;
  let scale = initialScale > 0 ? initialScale : 1.0;
  if (!samples.length) return { loc: 0, scale: 1 };

  for (let iter = 0; iter < maxIters; iter++) {
    let num = 0;
    let den = 0;
    for (const x of samples) {
      const u = (x - loc) / (cLoc * scale);
      if (Math.abs(u) < 1) {
        const w = (1 - u * u) ** 2;
        num += x * w;
        den += w;
      }
    }
    if (den === 0) break;
    const newLoc = num / den;
    if (Math.abs(newLoc - loc) < 1e-6) {
      loc = newLoc;
      break;
    }
    loc = newLoc;

    // Scale update (biweight midvariance).
    let numS = 0;
    let denS = 0;
    for (const x of samples) {
      const v = (x - loc) / (cScale * scale);
      if (Math.abs(v) < 1) {
        const t = 1 - v * v;
        numS += (x - loc) ** 2 * t ** 4;
        denS += t * (1 - 5 * v * v);
      }
    }
    if (denS > 0) {
      const newScale = Math.sqrt(samples.length * numS) / Math.abs(denS);
      if (Number.isFinite(newScale) && newScale > 0) scale = newScale;
    }
  }
  return { loc, scale };
}

function estimateCadence(deltas) {
  if (deltas.length < 3) return null;
  const multiples = deltas
    .map((d) => Math.round(d / CADENCE_BASE_SEC))
    .filter((m) => m >= 1 && m <= 24);
  if (!multiples.length) return null;
  const counts = {};
  for (const m of multiples) counts[m] = (counts[m] || 0) + 1;
  let best = null;
  let bestCount = 0;
  for (const [k, v] of Object.entries(counts)) {
    if (v > bestCount) {
      bestCount = v;
      best = parseInt(k, 10);
    }
  }
  return { mult: best, fit: bestCount / multiples.length };
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

async function handlePostSample(req) {
  const body = await req.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return new Response(JSON.stringify({ ok: false, error: "invalid json" }), { status: 400, headers: corsHeaders() });
  }
  const siteNo = String(body.site_no || "");
  const obsTs = String(body.obs_ts || "");
  const latency = Number(body.latency_sec);
  if (!siteNo || !obsTs || !Number.isFinite(latency)) {
    return new Response(JSON.stringify({ ok: false, error: "missing fields" }), { status: 400, headers: corsHeaders() });
  }

  const obsMs = Date.parse(obsTs);
  if (!Number.isFinite(obsMs)) {
    return new Response(JSON.stringify({ ok: false, error: "bad obs_ts" }), { status: 400, headers: corsHeaders() });
  }
  const obsSec = obsMs / 1000;

  const entry = SITES.get(siteNo) || { latencies: [], deltas: [], obsTimes: [], lastObs: null, updatedAt: null };
  entry.latencies.push(latency);
  if (entry.latencies.length > HISTORY_LIMIT) entry.latencies.splice(0, entry.latencies.length - HISTORY_LIMIT);

  if (entry.lastObs != null) {
    const delta = obsSec - entry.lastObs;
    if (delta > 60) {
      entry.deltas.push(delta);
      if (entry.deltas.length > HISTORY_LIMIT) entry.deltas.splice(0, entry.deltas.length - HISTORY_LIMIT);
    }
  }
  entry.lastObs = obsSec;
  entry.obsTimes.push(obsSec);
  if (entry.obsTimes.length > HISTORY_LIMIT) entry.obsTimes.splice(0, entry.obsTimes.length - HISTORY_LIMIT);
  entry.updatedAt = new Date().toISOString();

  SITES.set(siteNo, entry);

  return new Response(JSON.stringify({ ok: true }), { status: 200, headers: corsHeaders() });
}

function handleGetSummary() {
  const stations = {};
  for (const [siteNo, entry] of SITES.entries()) {
    const latencies = entry.latencies || [];
    if (!latencies.length) continue;
    const med = median(latencies);
    const s = mad(latencies, med) || 1.0;
    const { loc, scale } = tukeyBiweightLocationScale(latencies, med, s);

    const cadence = estimateCadence(entry.deltas || []);
    let phaseOffsetSec = null;
    if (cadence && entry.obsTimes && entry.obsTimes.length) {
      const cadenceSec = cadence.mult * CADENCE_BASE_SEC;
      const phases = entry.obsTimes.map((t) => ((t % cadenceSec) + cadenceSec) % cadenceSec);
      phaseOffsetSec = median(phases);
    }

    stations[siteNo] = {
      cadence_mult: cadence ? cadence.mult : null,
      cadence_fit: cadence ? cadence.fit : null,
      phase_offset_sec: phaseOffsetSec,
      latency_loc_sec: loc,
      latency_scale_sec: scale,
      samples: latencies.length,
      updated_at: entry.updatedAt,
    };
  }

  const payload = {
    version: 1,
    generated_at: new Date().toISOString(),
    stations,
  };
  return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders() } });
}

export default {
  async fetch(req) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") {
      return new Response("", { status: 204, headers: corsHeaders() });
    }
    if (req.method === "GET" && url.pathname.endsWith("/summary.json")) {
      return handleGetSummary();
    }
    if (req.method === "POST" && url.pathname.endsWith("/sample")) {
      return handlePostSample(req);
    }
    return new Response("Not found", { status: 404, headers: corsHeaders() });
  },
};

