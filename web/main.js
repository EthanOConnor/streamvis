import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v0.29.0/full/pyodide.mjs";

const term = document.getElementById("terminal");
const loadingEl = document.getElementById("loading");
const loadingTextEl = document.getElementById("loading-text");
const loadingProgressEl = document.getElementById("loading-progress");

// Global key queue consumed by web_curses.getch().
window.streamvisKeyQueue = [];

function initCommunityConfig() {
  const params = new URLSearchParams(window.location.search);
  const baseParam = params.get("community") || params.get("community_base");
  const publishParam =
    params.get("publish") || params.get("community_publish") || params.get("publish_samples");

  let base = "";
  if (typeof baseParam === "string" && baseParam) {
    base = baseParam;
    try {
      window.localStorage.setItem("streamvis_community_base", base);
    } catch (_err) {
      // Ignore storage failures.
    }
  } else {
    try {
      base = window.localStorage.getItem("streamvis_community_base") || "";
    } catch (_err) {
      base = "";
    }
  }

  let publish = false;
  if (publishParam !== null) {
    publish = publishParam === "1" || publishParam === "true" || publishParam === "yes";
    try {
      window.localStorage.setItem("streamvis_community_publish", publish ? "1" : "0");
    } catch (_err) {
      // Ignore storage failures.
    }
  } else {
    try {
      publish = window.localStorage.getItem("streamvis_community_publish") === "1";
    } catch (_err) {
      publish = false;
    }
  }

  window.streamvisCommunityBase = base;
  window.streamvisCommunityPublish = publish;
}

initCommunityConfig();

// User location bridge for the Nearby feature.
window.streamvisUserLocation = null;
window.streamvisLocationError = null;
window.streamvisRequestLocation = function requestStreamvisLocation() {
  if (!navigator.geolocation) {
    window.streamvisLocationError = "geolocation unavailable";
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      window.streamvisUserLocation = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        accuracy: pos.coords.accuracy,
        ts: Date.now(),
      };
      window.streamvisLocationError = null;
    },
    (err) => {
      window.streamvisLocationError = err && err.message ? err.message : String(err);
    },
    {
      enableHighAccuracy: false,
      maximumAge: 10 * 60 * 1000,
      timeout: 5000,
    }
  );
};

let measureEl = null;
function measureCharFactor(sampleFontPx = 14) {
  if (!measureEl) {
    measureEl = document.createElement("span");
    measureEl.id = "streamvis-measure-js";
    measureEl.style.position = "absolute";
    measureEl.style.visibility = "hidden";
    measureEl.style.whiteSpace = "pre";
    measureEl.style.pointerEvents = "none";
    measureEl.style.left = "-10000px";
    measureEl.style.top = "-10000px";
    document.body.appendChild(measureEl);
  }
  const style = getComputedStyle(term);
  measureEl.style.fontFamily = style.fontFamily;
  measureEl.style.fontSize = `${sampleFontPx}px`;
  measureEl.textContent = "M".repeat(100);
  const rect = measureEl.getBoundingClientRect();
  const charWidth = rect.width > 0 ? rect.width / 100 : sampleFontPx * 0.6;
  return charWidth / sampleFontPx;
}

function adaptTerminalFont() {
  const rect = term.getBoundingClientRect();
  const rawWidth = rect.width || window.innerWidth;
  const rawHeight = rect.height || window.innerHeight;
  const style = getComputedStyle(term);
  const padLeft = parseFloat(style.paddingLeft || "0") || 0;
  const padRight = parseFloat(style.paddingRight || "0") || 0;
  const width = Math.max(rawWidth - padLeft - padRight, 0);
  const height = rawHeight;

  // Aim to fit the full table on mobile by shrinking font first.
  // Wide header requires 59 columns; keep a small landscape cushion.
  const desiredCols = width > height ? 62 : 59; // landscape vs portrait
  const charFactor = measureCharFactor(14);
  const minFont = 10.5;
  const maxFont = 16.0;

  let fontPx = width / (desiredCols * charFactor);
  fontPx = Math.max(minFont, Math.min(maxFont, fontPx));
  term.style.setProperty("--term-font-size", `${fontPx.toFixed(1)}px`);
}

let resizePending = false;
function scheduleFontAdapt() {
  if (resizePending) return;
  resizePending = true;
  requestAnimationFrame(() => {
    resizePending = false;
    adaptTerminalFont();
  });
}

window.addEventListener("resize", scheduleFontAdapt);
window.addEventListener("orientationchange", scheduleFontAdapt);
adaptTerminalFont();

function setLoading(text, value) {
  if (loadingTextEl) loadingTextEl.textContent = text;
  if (loadingProgressEl) {
    if (typeof value === "number") {
      loadingProgressEl.value = value;
    } else {
      loadingProgressEl.removeAttribute("value");
    }
  }
}

function mapKey(ev) {
  if (ev.key.length === 1) {
    return ev.key.charCodeAt(0);
  }
  switch (ev.key) {
    case "ArrowUp":
      return 259; // KEY_UP
    case "ArrowDown":
      return 258; // KEY_DOWN
    case "Enter":
      return 10; // matches checks for (KEY_ENTER, 10, 13)
    case "Escape":
      // Convenience: treat ESC as 'q' to quit.
      return "q".charCodeAt(0);
    default:
      return null;
  }
}

document.addEventListener("keydown", (ev) => {
  const code = mapKey(ev);
  if (code !== null) {
    ev.preventDefault();
    window.streamvisKeyQueue.push(code);
  }
});

function handleRowClick(ev) {
  const rect = term.getBoundingClientRect();
  const style = getComputedStyle(term);
  const fontSizePx = parseFloat(style.fontSize || "13") || 13;
  const lineHeightRaw = style.lineHeight;
  let lineHeightPx;
  if (lineHeightRaw.endsWith("px")) {
    lineHeightPx = parseFloat(lineHeightRaw.replace("px", "")) || fontSizePx * 1.2;
  } else if (lineHeightRaw && lineHeightRaw !== "normal") {
    lineHeightPx = parseFloat(lineHeightRaw) * fontSizePx;
  } else {
    lineHeightPx = fontSizePx * 1.2;
  }

  const paddingTopPx = parseFloat(style.paddingTop || "0") || 0;
  const y = ev.clientY - rect.top + term.scrollTop - paddingTopPx;
  if (y < 0) return;

  const row = Math.floor(y / lineHeightPx);
  const code = 3000 + row;
  window.streamvisKeyQueue.push(code);
}

let lastPointerTs = 0;
term.addEventListener("pointerup", (ev) => {
  lastPointerTs = performance.now();
  handleRowClick(ev);
});
term.addEventListener("click", (ev) => {
  if (performance.now() - lastPointerTs < 500) return;
  handleRowClick(ev);
});

async function loadPythonModule(pyodide, path) {
  const candidates = [path];
  if (path.startsWith("../")) {
    candidates.push(path.slice(3));
  } else {
    candidates.push("../" + path);
  }

  let src = null;
  let usedPath = null;
  for (const candidate of candidates) {
    try {
      const resp = await fetch(candidate);
      if (resp.ok) {
        src = await resp.text();
        usedPath = candidate;
        break;
      }
    } catch (_err) {
      // Try the next candidate.
    }
  }

  if (src === null || usedPath === null) {
    throw new Error(`Failed to load ${path} (tried ${candidates.join(", ")})`);
  }

  // Derive module name from the filename (e.g., "streamvis.py" → "streamvis").
  const parts = usedPath.split("/");
  const filename = parts[parts.length - 1];
  const moduleName = filename.endsWith(".py") ? filename.slice(0, -3) : filename;

  // Install the module into Pyodide's virtual filesystem so that normal
  // `import moduleName` works, matching how streamvis imports http_client
  // and web_entrypoint imports streamvis.
  pyodide.FS.writeFile(filename, src, { encoding: "utf8" });
  await pyodide.runPythonAsync(`import ${moduleName}`);
}

async function syncStateFromLocalStorage(pyodide) {
  const stored = window.localStorage.getItem("streamvis_state_json");
  if (stored) {
    try {
      pyodide.FS.writeFile("streamvis_state.json", stored, { encoding: "utf8" });
    } catch (err) {
      console.warn("Failed to write initial state file:", err);
    }
  }
}

async function syncStateToLocalStorage(pyodide) {
  try {
    const data = pyodide.FS.readFile("streamvis_state.json", { encoding: "utf8" });
    window.localStorage.setItem("streamvis_state_json", data);
  } catch (err) {
    // No state file yet or other FS issue; ignore.
  }
}

async function main() {
  term.textContent = "Loading Pyodide…";
  setLoading("Loading Pyodide runtime…", 0);

  // Simple fake progress while the large runtime downloads/initializes.
  let fake = 0;
  const timer = setInterval(() => {
    fake = Math.min(fake + 1, 80);
    if (loadingProgressEl) loadingProgressEl.value = fake;
  }, 200);

  const pyodide = await loadPyodide({
    indexURL: "https://cdn.jsdelivr.net/pyodide/v0.29.0/full/",
  });

  clearInterval(timer);
  setLoading("Initializing filesystem…", 85);
  await syncStateFromLocalStorage(pyodide);

  // Load Python modules needed for the browser build.
  setLoading("Loading streamvis modules…", 90);
  await loadPythonModule(pyodide, "../http_client.py");
  await loadPythonModule(pyodide, "../web_curses.py");
  await loadPythonModule(pyodide, "../streamvis.py");
  await loadPythonModule(pyodide, "../web_entrypoint.py");

  // Patch curses to point at the web_curses shim.
  setLoading("Starting TUI…", 97);
  await pyodide.runPythonAsync(`
import sys, web_curses
sys.modules["curses"] = web_curses
  `);

  term.textContent = "Starting streamvis… (press q to quit)";
  if (loadingEl) loadingEl.classList.add("hidden");

  try {
    await pyodide.runPythonAsync(`
from web_entrypoint import run_default_async
await run_default_async()
    `);
  } catch (err) {
    console.error(err);
    term.textContent = "Error running streamvis:\\n" + err;
    setLoading("Error starting streamvis (see console).", 100);
    return;
  }

  await syncStateToLocalStorage(pyodide);
}

main().catch((err) => {
  console.error(err);
  term.textContent = "Error initialising streamvis:\\n" + err;
  setLoading("Error initializing Pyodide (see console).", 100);
});
