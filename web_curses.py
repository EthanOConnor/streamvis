#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass
import html
import time
from typing import Any, Dict, List, Tuple

try:
    from js import document, window  # type: ignore[import]
except ImportError:  # pragma: no cover
    raise RuntimeError("web_curses is only meant for use under Pyodide")


# Attribute bits (kept for structural compatibility; no visual effect for now).
A_BOLD = 1 << 0
A_REVERSE = 1 << 1
A_UNDERLINE = 1 << 2

# Color constants – distinct sentinel values.
COLOR_GREEN = 2
COLOR_YELLOW = 3
COLOR_RED = 4
COLOR_CYAN = 6

# Key codes – aligned with common curses values.
KEY_UP = 259
KEY_DOWN = 258
KEY_ENTER = 343


# Color pair mapping: pair_number -> {"fg": curses color const, "bg": curses color const}
_color_pairs: Dict[int, Dict[str, int]] = {0: {"fg": COLOR_GREEN, "bg": -1}}


def _encode_pair(pair_number: int) -> int:
    # Shift into high bits so OR-ing with attribute flags is safe.
    return int(pair_number) << 8


def _decode_pair(attr: int) -> int:
    return (int(attr) >> 8) & 0xFF


def has_colors() -> bool:
    return True


def start_color() -> None:
    pass


def use_default_colors() -> None:
    pass


def init_pair(pair_number: int, fg: int, bg: int) -> None:
    _color_pairs[int(pair_number)] = {"fg": int(fg), "bg": int(bg)}


def color_pair(pair_number: int) -> int:
    if int(pair_number) not in _color_pairs:
        _color_pairs[int(pair_number)] = {"fg": COLOR_GREEN, "bg": -1}
    return _encode_pair(int(pair_number))


def curs_set(visibility: int) -> None:
    # 0 = invisible, 1 = normal, 2 = very visible; ignored in this shim.
    pass


def flash() -> int:
    # No-op visual bell for the web shim; real terminals handle this via curses.
    return 0


def beep() -> int:
    # No-op audible bell for the web shim.
    return 0


def _measure_terminal() -> Tuple[int, int]:
    """
    Estimate terminal rows/cols from the DOM size and font metrics so the
    TUI can adapt to different screen sizes (desktop vs phone, etc.).
    """
    el = document.getElementById("terminal")
    if el is None:
        # Fallback to a conservative canvas if the element is missing.
        return 40, 120

    try:
        style = window.getComputedStyle(el)
        font_size_px_raw = style.getPropertyValue("font-size") or "13px"
        font_size_px = float(font_size_px_raw.replace("px", "")) or 13.0
        line_height_raw = style.getPropertyValue("line-height")
        if line_height_raw.endswith("px"):
            line_height = float(line_height_raw.replace("px", "")) or (1.1 * font_size_px)
        elif line_height_raw and line_height_raw != "normal":
            # Numeric multiplier.
            line_height = float(line_height_raw) * font_size_px
        else:
            line_height = 1.1 * font_size_px
        row_height = max(line_height, font_size_px)
        char_width = max(font_size_px * 0.55, 6.0)

        rect = el.getBoundingClientRect()
        width = max(rect.width, 320.0)
        height = max(rect.height, 320.0)

        cols = int(width // char_width)
        rows = int(height // row_height)

        cols = max(40, min(cols, 200))
        rows = max(20, min(rows, 60))
        return rows, cols
    except Exception:
        return 40, 120


@dataclass
class _Window:
    rows: int
    cols: int
    _buffer: List[List[str]]
    _attr_buffer: List[List[int]]
    _nodelay: bool = False
    _timeout_ms: int = -1

    def __post_init__(self) -> None:
        self._term_el = document.getElementById("terminal")
        if self._term_el is None:
            raise RuntimeError("web_curses: #terminal element not found in DOM")

    def _resize_to_dom(self) -> None:
        rows, cols = _measure_terminal()
        if rows == self.rows and cols == self.cols:
            return

        # Resize rows.
        if rows > self.rows:
            for _ in range(rows - self.rows):
                self._buffer.append([" " for _ in range(self.cols)])
                self._attr_buffer.append([0 for _ in range(self.cols)])
        elif rows < self.rows:
            self._buffer = self._buffer[:rows]
            self._attr_buffer = self._attr_buffer[:rows]
        self.rows = rows

        # Resize columns.
        if cols != self.cols:
            for r in range(self.rows):
                row = self._buffer[r]
                arow = self._attr_buffer[r]
                if cols > self.cols:
                    row.extend([" "] * (cols - self.cols))
                    arow.extend([0] * (cols - self.cols))
                elif cols < self.cols:
                    self._buffer[r] = row[:cols]
                    self._attr_buffer[r] = arow[:cols]
            self.cols = cols

    def getmaxyx(self) -> Tuple[int, int]:
        # Keep the window in sync with the actual DOM size on each layout pass.
        self._resize_to_dom()
        return (self.rows, self.cols)

    def erase(self) -> None:
        for r in range(self.rows):
            for c in range(self.cols):
                self._buffer[r][c] = " "
                self._attr_buffer[r][c] = 0

    def addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        if y < 0 or y >= self.rows:
            return
        if x < 0:
            s = s[-x:]
            x = 0
        if not s:
            return
        for i, ch in enumerate(s):
            c = x + i
            if 0 <= c < self.cols:
                self._buffer[y][c] = ch
                self._attr_buffer[y][c] = int(attr)

    def refresh(self) -> None:
        def css_for_attr(attr: int) -> str:
            pair = _decode_pair(attr)
            pair_info = _color_pairs.get(pair, _color_pairs[0])
            fg_const = pair_info.get("fg", COLOR_GREEN)
            reverse = bool(attr & A_REVERSE)
            bold = bool(attr & A_BOLD)
            underline = bool(attr & A_UNDERLINE)

            color_map = {
                COLOR_GREEN: "#0f0",
                COLOR_YELLOW: "#ff0",
                COLOR_RED: "#f44",
                COLOR_CYAN: "#0ff",
            }
            fg = color_map.get(fg_const, "#0f0")
            bg = "#000"
            if reverse:
                fg, bg = "#000", fg
            styles = [f"color: {fg}", f"background-color: {bg}"]
            if bold:
                styles.append("font-weight: bold")
            if underline:
                styles.append("text-decoration: underline")
            return "; ".join(styles)

        html_lines: List[str] = []
        for row_chars, row_attrs in zip(self._buffer, self._attr_buffer):
            out_parts: List[str] = []
            current_attr = None
            segment: List[str] = []
            for ch, attr in zip(row_chars, row_attrs):
                if current_attr is None:
                    current_attr = attr
                if attr != current_attr:
                    text = html.escape("".join(segment))
                    style = css_for_attr(int(current_attr))
                    out_parts.append(f'<span style="{style}">{text}</span>')
                    segment = []
                    current_attr = attr
                segment.append(ch)

            if segment:
                text = html.escape("".join(segment))
                style = css_for_attr(int(current_attr or 0))
                out_parts.append(f'<span style="{style}">{text}</span>')

            html_lines.append("".join(out_parts).rstrip())

        self._term_el.innerHTML = "\n".join(html_lines)

    def nodelay(self, flag: bool) -> None:
        self._nodelay = flag

    def timeout(self, ms: int) -> None:
        self._timeout_ms = ms

    def getch(self) -> int:
        try:
            queue = window.streamvisKeyQueue  # type: ignore[attr-defined]
        except Exception:
            return -1
        if queue:
            key = queue.pop(0)
            try:
                return int(key)
            except Exception:
                return -1

        # Respect basic curses timing semantics to avoid a busy loop:
        # - If nodelay is True or timeout == 0, return immediately.
        # - If timeout < 0, behave like "blocking" but we can't truly block
        #   in a browser; instead sleep briefly to yield CPU.
        # - If timeout > 0, sleep up to that many milliseconds.
        if self._nodelay or self._timeout_ms == 0:
            return -1

        timeout_ms = self._timeout_ms
        if timeout_ms is None or timeout_ms < 0:
            time.sleep(0.05)
            return -1

        time.sleep(timeout_ms / 1000.0)
        return -1


def initscr() -> _Window:
    # Fixed canvas; sized generously for the existing TUI layout.
    rows, cols = 40, 120
    buf = [[" " for _ in range(cols)] for _ in range(rows)]
    abuf = [[0 for _ in range(cols)] for _ in range(rows)]
    return _Window(rows=rows, cols=cols, _buffer=buf, _attr_buffer=abuf)


def wrapper(func: Any) -> int:
    win = initscr()
    return func(win)
