"""
Native geolocation layer for CLI/TUI clients.

Provides platform-adaptive location services for the Nearby stations feature.
Supports:
- macOS: CoreLocation via pyobjc (if installed)
- Linux: GeoClue D-Bus service
- Fallback: IP-based geolocation or manual coordinates

All location methods are best-effort and fail gracefully.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class Location:
    """Geographic location with optional accuracy."""
    lat: float
    lon: float
    accuracy_meters: float | None = None
    source: str = "unknown"


def get_location_macos() -> Location | None:
    """
    Get location on macOS using CoreLocation via a helper script.
    
    Requires Location Services enabled in System Preferences.
    Returns None if location unavailable.
    """
    # Use a small Swift snippet via osascript for CoreLocation
    # This avoids pyobjc dependency while still getting accurate location
    script = '''
    use framework "Foundation"
    use framework "CoreLocation"
    use scripting additions

    property locationManager : missing value
    property currentLocation : missing value

    on run
        set locationManager to current application's CLLocationManager's alloc()'s init()
        locationManager's requestWhenInUseAuthorization()
        locationManager's startUpdatingLocation()
        
        -- Wait briefly for location
        delay 1
        
        set loc to locationManager's location()
        if loc is missing value then
            return "error:no_location"
        end if
        
        set coord to loc's coordinate()
        set lat to item 1 of coord
        set lon to item 2 of coord
        set acc to loc's horizontalAccuracy()
        
        locationManager's stopUpdatingLocation()
        return (lat as text) & "," & (lon as text) & "," & (acc as text)
    end run
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        output = result.stdout.strip()
        if output.startswith("error:"):
            return None
        parts = output.split(",")
        if len(parts) >= 2:
            lat = float(parts[0])
            lon = float(parts[1])
            acc = float(parts[2]) if len(parts) > 2 else None
            return Location(lat=lat, lon=lon, accuracy_meters=acc, source="macos_corelocation")
    except Exception:
        pass
    
    # Fallback: try the simpler 'whereami' approach if available
    try:
        result = subprocess.run(
            ["defaults", "read", "/var/db/locationd/Library/Caches/locationd/consolidated.db", "LocationServices"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        # This usually requires special permissions; fall through if it fails
    except Exception:
        pass
    
    return None


def get_location_linux() -> Location | None:
    """
    Get location on Linux using GeoClue D-Bus service.
    
    Falls back to IP-based geolocation if GeoClue unavailable.
    """
    # Try GeoClue via D-Bus
    try:
        import dbus  # type: ignore[import]
        bus = dbus.SystemBus()
        geoclue = bus.get_object(
            "org.freedesktop.GeoClue2",
            "/org/freedesktop/GeoClue2/Manager"
        )
        manager = dbus.Interface(geoclue, "org.freedesktop.GeoClue2.Manager")
        client_path = manager.GetClient()
        client = bus.get_object("org.freedesktop.GeoClue2", client_path)
        client_iface = dbus.Interface(client, "org.freedesktop.GeoClue2.Client")
        client_iface.Start()
        
        # Get location
        loc_path = client.Get("org.freedesktop.GeoClue2.Client", "Location")
        location = bus.get_object("org.freedesktop.GeoClue2", loc_path)
        lat = location.Get("org.freedesktop.GeoClue2.Location", "Latitude")
        lon = location.Get("org.freedesktop.GeoClue2.Location", "Longitude")
        acc = location.Get("org.freedesktop.GeoClue2.Location", "Accuracy")
        client_iface.Stop()
        return Location(lat=float(lat), lon=float(lon), accuracy_meters=float(acc), source="linux_geoclue")
    except Exception:
        pass
    
    return None


def get_location_ip_fallback() -> Location | None:
    """
    Get approximate location from IP address using ipinfo.io.
    
    Returns city-level accuracy (~5-10km).
    """
    try:
        import http.client
        import json
        
        conn = http.client.HTTPSConnection("ipinfo.io", timeout=3.0)
        conn.request("GET", "/json")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode())
        loc_str = data.get("loc", "")
        if "," not in loc_str:
            return None
        lat, lon = loc_str.split(",")
        return Location(
            lat=float(lat),
            lon=float(lon),
            accuracy_meters=10000.0,  # ~10km for IP geolocation
            source="ip_geolocation",
        )
    except Exception:
        return None


def get_location() -> Location | None:
    """
    Get current location using the best available method.
    
    Tries in order:
    1. Platform-specific (CoreLocation on macOS, GeoClue on Linux)
    2. IP-based geolocation fallback
    
    Returns None if all methods fail.
    """
    if sys.platform == "darwin":
        loc = get_location_macos()
        if loc is not None:
            return loc
    elif sys.platform.startswith("linux"):
        loc = get_location_linux()
        if loc is not None:
            return loc
    
    # Fallback to IP geolocation
    return get_location_ip_fallback()


def get_location_async(callback: Callable[[Location | None], None]) -> None:
    """
    Get location asynchronously (non-blocking).
    
    Useful for UI applications that shouldn't block on location.
    """
    import threading
    
    def _worker():
        loc = get_location()
        callback(loc)
    
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
