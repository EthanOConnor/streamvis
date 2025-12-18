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

function normalizePyodidePath(path) {
  let out = path;
  while (out.startsWith("../")) out = out.slice(3);
  if (out.startsWith("./")) out = out.slice(2);
  out = out.replace(/^\/+/, "");
  return out;
}

function moduleNameFromFsPath(fsPath) {
  const normalized = fsPath.replace(/\\/g, "/");
  const noExt = normalized.endsWith(".py") ? normalized.slice(0, -3) : normalized;
  const parts = noExt.split("/").filter((p) => p);
  if (parts.length === 0) return noExt;
  if (parts[parts.length - 1] === "__init__") {
    parts.pop();
  }
  return parts.join(".");
}

async function loadPythonModule(pyodide, path, options = {}) {
  const { importModule = true } = options;
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

  const fsPath = normalizePyodidePath(usedPath);
  const moduleName = moduleNameFromFsPath(fsPath);

  // Install into Pyodide's virtual filesystem with the same relative path
  // so that normal `import pkg.module` works for package modules.
  const lastSlash = fsPath.lastIndexOf("/");
  if (lastSlash !== -1) {
    pyodide.FS.mkdirTree(fsPath.slice(0, lastSlash));
  }
  pyodide.FS.writeFile(fsPath, src, { encoding: "utf8" });
  if (importModule) {
    await pyodide.runPythonAsync(`import ${moduleName}`);
  }
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

  // Install the streamvis package files without importing yet.
  // (Importing streamvis executes streamvis/__init__.py which imports many
  // submodules, so they must exist in the filesystem first.)
  const streamvisFiles = [
    "../streamvis/__init__.py",
    "../streamvis/constants.py",
    "../streamvis/config.py",
    "../streamvis/gauges.py",
    "../streamvis/location.py",
    "../streamvis/scheduler.py",
    "../streamvis/state.py",
    "../streamvis/types.py",
    "../streamvis/utils.py",
    "../streamvis/tui.py",
    "../streamvis/usgs/__init__.py",
    "../streamvis/usgs/adapter.py",
    "../streamvis/usgs/ogcapi.py",
    "../streamvis/usgs/waterservices.py",
    "../streamvis/__main__.py",
  ];
  for (const file of streamvisFiles) {
    const optional = file.endsWith("/__init__.py") || file.endsWith("/__main__.py");
    try {
      await loadPythonModule(pyodide, file, { importModule: false });
    } catch (err) {
      if (!optional) throw err;
      console.warn("Optional python file missing:", file, err);
    }
  }

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
  let msg = "Error initialising streamvis:\\n" + err;

  try {
    const errStr = String(err || "");
    const isFile = window.location && window.location.protocol === "file:";
    const looksLikeMissingPySource =
      errStr.includes("Failed to load") && (errStr.includes("streamvis/") || errStr.includes("streamvis\\"));
    const mentionsInit = errStr.includes("__init__.py");

    if (isFile) {
      msg +=
        "\\n\\nTip: open this page via a local web server (http://), not file://.\\n" +
        "For example, run `python -m http.server` from the repo root and open /web/.";
    } else if (looksLikeMissingPySource) {
      msg +=
        "\\n\\nTip: the browser couldn't fetch the streamvis Python sources.\\n" +
        "- Ensure the published site includes the `streamvis/` package directory and the helper modules.\\n" +
        "- If using GitHub Pages, you likely need a `.nojekyll` file so `__init__.py` is published.";
      if (mentionsInit) {
        msg += "\\n  (This error often means `streamvis/__init__.py` was not published.)";
      }
    }
  } catch (_hintErr) {
    // Ignore hint failures.
  }

  term.textContent = msg;
  setLoading("Error initializing Pyodide (see console).", 100);
});
