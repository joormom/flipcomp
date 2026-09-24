"""User preferences -- places not worth the drive.

Stored in prefs.json at the project root so it survives cache clears and is
easy to edit by hand. Exclusions apply everywhere: deal scans, lead hunts,
the daily report and the history store.
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFS_PATH = os.path.join(ROOT, "prefs.json")

DEFAULTS = {"exclude_cities": [], "exclude_counties": []}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load() -> dict:
    try:
        with open(PREFS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    out = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    out["exclude_cities"] = sorted({_norm(c) for c in out["exclude_cities"] if _norm(c)})
    out["exclude_counties"] = sorted({_norm(c).replace(" county", "") for c in out["exclude_counties"]
                                      if _norm(c)})
    return out


def save(prefs: dict) -> dict:
    clean = {
        "exclude_cities": sorted({_norm(c) for c in prefs.get("exclude_cities", []) if _norm(c)}),
        "exclude_counties": sorted({_norm(c).replace(" county", "")
                                    for c in prefs.get("exclude_counties", []) if _norm(c)}),
    }
    with open(PREFS_PATH, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    return clean


def set_excluded_places(places: list[str], county_keys: set[str]) -> dict:
    """Split a free-text list into counties (when the name is a known county)
    and cities. A name that is both -- Nowata -- is excluded as both."""
    cities, counties = [], []
    for p in places:
        n = _norm(p).replace(" county", "")
        if not n:
            continue
        cities.append(n)
        if n in county_keys:
            counties.append(n)
    return save({"exclude_cities": cities, "exclude_counties": counties})


def city_excluded(city, prefs: dict | None = None) -> bool:
    prefs = prefs or load()
    return _norm(str(city or "")) in set(prefs["exclude_cities"])


def county_excluded(county_key, prefs: dict | None = None) -> bool:
    prefs = prefs or load()
    return _norm(str(county_key or "")).replace(" county", "") in set(prefs["exclude_counties"])


def address_excluded(address, prefs: dict | None = None) -> bool:
    """True when a free-form address names an excluded city.

    Handles both 'Street, City, OK, ZIP' and the treasurer's 'STREET CITY'."""
    prefs = prefs or load()
    text = _norm(str(address or ""))
    if not text:
        return False
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 2 and parts[1] in set(prefs["exclude_cities"]):
        return True
    return any(text.endswith(" " + c) for c in prefs["exclude_cities"])
