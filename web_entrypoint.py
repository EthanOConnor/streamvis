#!/usr/bin/env python3

from __future__ import annotations

"""
Browser/Pyodide entrypoint for streamvis.

This keeps the core CLI intact and simply invokes main() with a fixed
argument list suitable for running the TUI in a web "terminal".
"""

from typing import List

from streamvis.tui import main, web_tui_main


def _append_community_args(argv: List[str]) -> None:
    try:
        import js  # type: ignore[import]
    except Exception:
        return

    base = ""
    publish = False
    try:
        base_raw = js.window.streamvisCommunityBase
        if base_raw is not None:
            base = str(base_raw)
    except Exception:
        base = ""
    try:
        publish = bool(js.window.streamvisCommunityPublish)
    except Exception:
        publish = False

    if not base or base == "undefined":
        return

    argv.extend(["--community-base", base])
    if publish:
        argv.append("--community-publish")


def run_default() -> int:
    """
    Run streamvis in TUI mode using a local state file that is easy to
    bridge to browser localStorage (web_main.js handles the mapping).
    """
    argv: List[str] = [
        "--mode",
        "tui",
        "--state-file",
        "streamvis_state.json",
        "--backfill-hours",
        "12",
        "--ui-tick-sec",
        "0.25",
    ]
    _append_community_args(argv)
    return main(argv)


async def run_default_async() -> int:
    """
    Async browser entrypoint that yields to the JS event loop.
    """
    argv: List[str] = [
        "--mode",
        "tui",
        "--state-file",
        "streamvis_state.json",
        "--backfill-hours",
        "12",
        "--ui-tick-sec",
        "0.25",
    ]
    _append_community_args(argv)
    return await web_tui_main(argv)


def run_with_args(arg_list: List[str]) -> int:
    """
    Allow JS to pass through custom CLI-style arguments if desired.
    """
    return main(arg_list)


if __name__ == "__main__":
    raise SystemExit(run_default())
