"""Focus school districts -- Owasso and Collinsville first.

School district is what retail buyers pay for, so it is what a flip resells
on. District lines do not follow ZIP codes or counties (an Owasso-schools
house can sit in Rogers County with a Collinsville mailing ZIP), so matching
by city or ZIP misses houses and includes wrong ones.

This uses the official boundaries from the Census Bureau's TIGERweb service:
fetched once, cached for a year, and then every property with coordinates is
tested locally -- no per-address lookups. Listings inside the districts are
pulled by a radius search around each district so nothing is missed at the
edges, then kept only if they fall inside the boundary.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any

import pandas as pd
import requests

from .data import CACHE_DIR, _cached_scrape

TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/School/MapServer/0/query"
TTL = 365 * 24 * 3600

# Anchor addresses near each district's middle, used as radius-search centres.
DISTRICTS: dict[str, dict] = {
    "owasso": {"name": "Owasso Public Schools", "short": "Owasso SD", "geoid": "4023280",
               "anchor": "200 S Main St, Owasso, OK 74055"},
    "collinsville": {"name": "Collinsville Public Schools", "short": "Collinsville SD",
                     "geoid": "4008370", "anchor": "106 N 12th St, Collinsville, OK 74021"},
}
DEFAULT_FOCUS = ["owasso", "collinsville"]
FOCUS_BONUS = 15  # lead-score points for being inside a focus district


def _cache_path(geoid: str) -> str:
    d = os.path.join(CACHE_DIR, "focus")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{geoid}.json")


def boundary(key: str) -> dict:
    """GeoJSON geometry for a district, cached."""
    d = DISTRICTS[key]
    path = _cache_path(d["geoid"])
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < TTL:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    r = requests.get(TIGER, params={"where": f"GEOID='{d['geoid']}'", "outFields": "NAME,GEOID",
                                    "returnGeometry": "true", "outSR": "4326", "f": "geojson"},
                     timeout=60)
    r.raise_for_status()
    feats = r.json().get("features") or []
    if not feats:
        raise RuntimeError(f"No boundary returned for {d['name']}")
    geom = feats[0]["geometry"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(geom, fh)
    return geom


def _polygons(geom: dict) -> list[list[list[list[float]]]]:
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def _in_ring(x: float, y: float, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


_shapes: dict[str, tuple] = {}


def _shape(key: str):
    if key not in _shapes:
        polys = _polygons(boundary(key))
        xs = [c[0] for p in polys for c in p[0]]
        ys = [c[1] for p in polys for c in p[0]]
        _shapes[key] = (polys, (min(xs), min(ys), max(xs), max(ys)))
    return _shapes[key]


def district_of(lat, lon, keys: list[str] | None = None) -> str | None:
    """Which focus district a point is in, or None."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if math.isnan(lat) or math.isnan(lon):
        return None
    for key in keys or DEFAULT_FOCUS:
        try:
            polys, (x0, y0, x1, y1) = _shape(key)
        except Exception:
            continue
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        for poly in polys:
            outer, holes = poly[0], poly[1:]
            if _in_ring(lon, lat, outer) and not any(_in_ring(lon, lat, h) for h in holes):
                return key
    return None


def tag(df: pd.DataFrame, keys: list[str] | None = None) -> pd.DataFrame:
    """Add a 'school_district' column (focus key or None)."""
    if df is None or df.empty:
        return df
    df = df.copy()
    df["school_district"] = [district_of(a, b, keys) for a, b in
                             zip(pd.to_numeric(df.get("latitude"), errors="coerce"),
                                 pd.to_numeric(df.get("longitude"), errors="coerce"))]
    return df


def _radius_for(key: str) -> float:
    """Miles from the anchor to the far corner of the district, plus a margin."""
    from .data import geocode
    polys, (x0, y0, x1, y1) = _shape(key)
    g = geocode(DISTRICTS[key]["anchor"]) or {}
    lat, lon = g.get("latitude", (y0 + y1) / 2), g.get("longitude", (x0 + x1) / 2)
    from .geo import haversine_mi
    far = max(haversine_mi(lat, lon, y, x) for x in (x0, x1) for y in (y0, y1))
    return round(far + 0.5, 1)


def listings(listing_type: str, keys: list[str] | None = None, past_days: int | None = None) -> pd.DataFrame:
    """Every listing of a type inside the focus districts."""
    frames = []
    for key in keys or DEFAULT_FOCUS:
        d = DISTRICTS[key]
        radius = _radius_for(key)
        kw: dict[str, Any] = {"location": d["anchor"], "listing_type": listing_type, "radius": radius}
        if past_days:
            kw["past_days"] = past_days
        try:
            df = _cached_scrape(f"focus|{key}|{listing_type}|{past_days}|{radius}", **kw)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        df = tag(df, [key])
        frames.append(df[df["school_district"] == key])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "property_id" in out.columns:
        out = out.drop_duplicates(subset=["property_id"])
    return out


def label(key: str | None) -> str | None:
    return DISTRICTS[key]["short"] if key in DISTRICTS else None
