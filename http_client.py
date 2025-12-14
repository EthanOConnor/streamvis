#!/usr/bin/env python3

from __future__ import annotations

"""
Thin HTTP abstraction so streamvis can run under:
- Native CPython (requests-based)
- Pyodide in the browser (pyodide.http.open_url)

Public API:
    get_json(url, params=None, timeout=10.0) -> Any
    get_text(url, params=None, timeout=10.0) -> str
    post_json(url, data=None, timeout=10.0) -> Any
    post_json_async(url, data=None, timeout=10.0) -> Any
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


def post_json(
    url: str,
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> Any:
    """
    POST a JSON payload and return parsed JSON (or text) response.

    In CPython:
        - Uses requests.post(..., json=data, timeout=timeout)
        - Raises on non-2xx status

    In Pyodide:
        - Not currently supported (no synchronous POST path); callers should
          treat exceptions as soft failures.
    """
    if _USE_PYODIDE:
        raise RuntimeError("post_json is not supported under Pyodide")
    if requests is None:  # pragma: no cover
        raise RuntimeError(
            "requests is required for native HTTP; install streamvis with pip to pull it in."
        ) from _REQUESTS_IMPORT_ERROR
    resp = requests.post(url, json=data or {}, timeout=timeout)  # type: ignore[name-defined]
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return resp.text


async def post_json_async(
    url: str,
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> Any:
    """
    Async POST for Pyodide/browser builds.

    In CPython:
        - Delegates to post_json() (blocking).

    In Pyodide:
        - Uses browser fetch via js.fetch and an AbortController timeout.
        - Raises on non-2xx status, matching requests.raise_for_status().
    """
    if not _USE_PYODIDE:
        return post_json(url, data=data, timeout=timeout)

    try:
        import js  # type: ignore[import]
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Pyodide js module is required for async POST") from exc

    payload = json.dumps(data or {}, separators=(",", ":"), sort_keys=True)
    controller = js.AbortController.new()
    options = js.Object.new()
    options.method = "POST"
    options.body = payload
    options.signal = controller.signal
    options.keepalive = True

    timeout_ms = int(max(0.0, float(timeout)) * 1000.0)
    timer = None
    if timeout_ms > 0:
        try:
            timer = js.setTimeout(controller.abort, timeout_ms)
        except Exception:
            timer = None

    try:
        resp = await js.fetch(url, options)
        ok = bool(getattr(resp, "ok", False))
        if not ok:
            status = getattr(resp, "status", None)
            raise RuntimeError(f"HTTP POST {status}")
        text = await resp.text()
    finally:
        if timer is not None:
            try:
                js.clearTimeout(timer)
            except Exception:
                pass

    try:
        return json.loads(text)
    except Exception:
        return text
