#!/usr/bin/env python3

from __future__ import annotations

"""
Thin HTTP abstraction so streamvis can run under:
- Native CPython (requests-based)
- Pyodide in the browser (pyodide.http.open_url)

Public API:
    get_json(url, params=None, timeout=10.0) -> Any
    get_text(url, params=None, timeout=10.0) -> str
"""

import json
from typing import Any, Dict, Optional
from urllib.parse import urlencode


def _build_url(url: str, params: Optional[Dict[str, Any]]) -> str:
    if not params:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode(params)}"


try:
    # Pyodide / browser branch.
    from pyodide.http import open_url  # type: ignore[import]

    _USE_PYODIDE = True
except Exception:
    # Native CPython branch.
    _USE_PYODIDE = False
    try:
        import requests  # type: ignore[import]
    except Exception as exc:  # pragma: no cover
        requests = None  # type: ignore[assignment]
        _REQUESTS_IMPORT_ERROR = exc
    else:
        _REQUESTS_IMPORT_ERROR = None


def get_text(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> str:
    """
    Fetch a URL and return its body as text.

    In CPython:
        - Uses requests.get(..., params=params, timeout=timeout)
        - Raises on non-2xx status

    In Pyodide:
        - Uses pyodide.http.open_url(full_url)
        - Relies on browser fetch + CORS.
    """
    if not _USE_PYODIDE:
        if requests is None:  # pragma: no cover
            raise RuntimeError(
                "requests is required for native HTTP; install streamvis with pip to pull it in."
            ) from _REQUESTS_IMPORT_ERROR
        resp = requests.get(url, params=params, timeout=timeout)  # type: ignore[name-defined]
        resp.raise_for_status()
        return resp.text

    full_url = _build_url(url, params)
    fp = open_url(full_url)  # type: ignore[func-returns-value]
    return fp.read()


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> Any:
    """
    Fetch a URL and parse its body as JSON.

    Exceptions bubble up as Exception subclasses, which callers already
    catch generically.
    """
    if not _USE_PYODIDE:
        if requests is None:  # pragma: no cover
            raise RuntimeError(
                "requests is required for native HTTP; install streamvis with pip to pull it in."
            ) from _REQUESTS_IMPORT_ERROR
        resp = requests.get(url, params=params, timeout=timeout)  # type: ignore[name-defined]
        resp.raise_for_status()
        return resp.json()

    text = get_text(url, params=params, timeout=timeout)
    return json.loads(text)
