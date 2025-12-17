#!/usr/bin/env python3
"""
Streamvis CLI entrypoint.

Usage:
    python -m streamvis [options]
    streamvis [options]  # if installed via pip

See --help for available options.
"""

from __future__ import annotations

import sys

from streamvis import main

if __name__ == "__main__":
    sys.exit(main())
