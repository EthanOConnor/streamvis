#!/usr/bin/env python3

from __future__ import annotations

"""
Browser/Pyodide entrypoint for streamvis.

This keeps the core CLI intact and simply invokes main() with a fixed
argument list suitable for running the TUI in a web "terminal".
"""

from typing import List

from streamvis import main


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
    ]
    return main(argv)


def run_with_args(arg_list: List[str]) -> int:
    """
    Allow JS to pass through custom CLI-style arguments if desired.
    """
    return main(arg_list)


if __name__ == "__main__":
    raise SystemExit(run_default())
