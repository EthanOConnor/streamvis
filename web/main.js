import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v0.29.0/full/pyodide.mjs";

const term = document.getElementById("terminal");

// Global key queue consumed by web_curses.getch().
window.streamvisKeyQueue = [];

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

term.addEventListener("click", (ev) => {
  const rect = term.getBoundingClientRect();
  const y = ev.clientY - rect.top;
  if (y < 0) return;

  const style = getComputedStyle(term);
  const fontSizePx = parseFloat(style.fontSize || "13") || 13;
  const lineHeightRaw = style.lineHeight;
  let lineHeightPx;
  if (lineHeightRaw.endsWith("px")) {
    lineHeightPx = parseFloat(lineHeightRaw.replace("px", "")) || fontSizePx * 1.1;
  } else if (lineHeightRaw && lineHeightRaw !== "normal") {
    lineHeightPx = parseFloat(lineHeightRaw) * fontSizePx;
  } else {
    lineHeightPx = fontSizePx * 1.1;
  }

  const row = Math.floor(y / lineHeightPx);
  // Encode "click row N" as a synthetic key code; Python maps row indices
  // to gauge rows based on its layout (table_start + 1, etc.).
  const code = 3000 + row;
  window.streamvisKeyQueue.push(code);
});

async function loadPythonModule(pyodide, path) {
  const resp = await fetch(path);
  if (!resp.ok) {
    throw new Error(`Failed to load ${path}: ${resp.status} ${resp.statusText}`);
  }
  const src = await resp.text();
  await pyodide.runPythonAsync(src);
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

  const pyodide = await loadPyodide({
    indexURL: "https://cdn.jsdelivr.net/pyodide/v0.29.0/full/",
  });

  await syncStateFromLocalStorage(pyodide);

  // Load Python modules needed for the browser build.
  await loadPythonModule(pyodide, "../http_client.py");
  await loadPythonModule(pyodide, "../web_curses.py");
  await loadPythonModule(pyodide, "../streamvis.py");
  await loadPythonModule(pyodide, "../web_entrypoint.py");

  // Patch curses to point at the web_curses shim.
  await pyodide.runPythonAsync(`
import sys, web_curses
sys.modules["curses"] = web_curses
  `);

  term.textContent = "Starting streamvis… (press q to quit)";

  try {
    await pyodide.runPythonAsync(`
from web_entrypoint import run_default
run_default()
    `);
  } catch (err) {
    console.error(err);
    term.textContent = "Error running streamvis:\\n" + err;
    return;
  }

  await syncStateToLocalStorage(pyodide);
}

main().catch((err) => {
  console.error(err);
  term.textContent = "Error initialising streamvis:\\n" + err;
});
