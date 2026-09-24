"""County treasurer tax-roll client (oktaxrolls.com).

Most Oklahoma county treasurers publish their tax roll through this one vendor,
and its search backend answers plain HTTP with no bot gate. It exposes exactly
what a flipper wants: who has not paid property tax, for how many years, and
where the parcel is.

Delinquency is the earliest public distress signal there is. A parcel three
years behind is headed for the treasurer's June resale; two years behind is a
homeowner under strain and, very often, an owner who would take a fair cash
offer before it gets there.
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import re
import time
from typing import Any

import requests

from .data import CACHE_DIR

BASE = "https://oktaxrolls.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Tax years the roll can be queried over. The vendor's form offers 2019 onward.
FIRST_YEAR = 2019
LAST_YEAR = 2025

UNPAID_CACHE_TTL = 24 * 60 * 60    # county-wide list, refreshed daily
DETAIL_CACHE_TTL = 30 * 24 * 3600  # a parcel's address does not change

# Counties known to be served by oktaxrolls. Others may work too -- the slug is
# simply the lower-case county name -- but these have been confirmed.
KNOWN_COUNTIES = {"washington"}


class TaxRollError(RuntimeError):
    """Raised when the tax roll cannot be queried."""


_session: requests.Session | None = None


def _sess(county: str) -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
    _session.headers["Referer"] = f"{BASE}/searchTaxRoll/{county}"
    return _session


def _dt_body(start: int = 0, length: int = 25, ncols: int = 8) -> dict:
    """The server-side DataTables request the vendor's page sends."""
    body = {
        "draw": 1, "start": start, "length": length,
        "search[value]": "", "search[regex]": "false",
        "order[0][column]": 0, "order[0][dir]": "desc",
    }
    for i in range(ncols):
        body.update({
            f"columns[{i}][data]": i, f"columns[{i}][name]": "",
            f"columns[{i}][searchable]": "true", f"columns[{i}][orderable]": "true",
            f"columns[{i}][search][value]": "", f"columns[{i}][search][regex]": "false",
        })
    return body


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


def _link(s: str) -> str | None:
    m = re.search(r"href='([^']+)'", s or "") or re.search(r'href="([^"]+)"', s or "")
    return m.group(1) if m else None


def _money(s: str) -> float | None:
    s = _strip(s)
    if not s or "PAID" in s.upper():
        return 0.0
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _query(county: str, mode: str, params: dict, start: int = 0,
           length: int = 25) -> list[list[str]]:
    q = {"from_years": str(FIRST_YEAR), "to_year": str(LAST_YEAR),
         "show_records": "25", "total_record": "", **params}
    try:
        r = _sess(county).post(f"{BASE}/searchResult/{county}/{mode}", params=q,
                               data=_dt_body(start, length), timeout=120)
        r.raise_for_status()
        j = r.json()
    except requests.RequestException as exc:
        raise TaxRollError(f"Tax roll request failed: {exc}") from exc
    except ValueError as exc:
        raise TaxRollError(
            f"Tax roll returned non-JSON for {county}; the county may not be on "
            "this system.") from exc
    data = j.get("data") or []
    return data if isinstance(data, list) else []


def _row(cells: list[str], mode: str) -> dict[str, Any] | None:
    """Normalise one result row. Column layout differs by search mode."""
    if len(cells) < 7:
        return None
    if mode == "street_address":
        year, tax_id, address, owner_html, kind, base, due = cells[:7]
        rec = {"address": _strip(address), "property_id": None}
    else:
        year, tax_id, owner_html, pid, kind, base, due = cells[:7]
        rec = {"address": None, "property_id": _strip(pid) or None}
    rec.update({
        "year": int(_strip(year)) if _strip(year).isdigit() else None,
        "tax_id": _strip(tax_id) or None,
        "owner": _strip(owner_html),
        "detail_url": _link(owner_html),
        "type": _strip(kind),
        "base_tax": _money(base),
        "total_due": _money(due),
        "paid": "PAID" in _strip(due).upper(),
    })
    return rec


# --- cache ------------------------------------------------------------------

def _cache_file(name: str) -> str:
    d = os.path.join(CACHE_DIR, "taxroll")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _cache_load(name: str, ttl: int):
    p = _cache_file(name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def _cache_save(name: str, obj) -> None:
    try:
        with open(_cache_file(name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    except Exception:
        pass


# --- public API -------------------------------------------------------------

def county_unpaid(county: str) -> list[dict[str, Any]]:
    """Every unpaid tax record in the county, all years, all types."""
    county = county.strip().lower()
    hit = _cache_load(f"unpaid_{county}.json", UNPAID_CACHE_TTL)
    if hit is not None:
        return hit

    params = {"first_name": "", "last_name": "", "business_owner_name": "",
              "show_unpaid_only": "1"}
    out: list[dict] = []
    start, page = 0, 2000
    while True:
        rows = _query(county, "owner_name", params, start=start, length=page)
        for cells in rows:
            rec = _row(cells, "owner_name")
            if rec:
                out.append(rec)
        if len(rows) < page:
            break
        start += page
        if start > 50_000:  # safety valve
            break
    _cache_save(f"unpaid_{county}.json", out)
    return out


def parcel_detail(detail_url: str) -> dict[str, Any]:
    """Situs address, legal description and assessed values for one record."""
    key = "detail3_" + re.sub(r"[^a-zA-Z0-9]", "", detail_url)[-60:] + ".json"
    hit = _cache_load(key, DETAIL_CACHE_TTL)
    if hit is not None:
        return hit

    try:
        r = _sess("washington").get(detail_url, timeout=60)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise TaxRollError(f"Parcel detail failed: {exc}") from exc

    text = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", text))
    # The situs line is '1624  SE MISSION RD<nbsp><nbsp> BARTLESVILLE': plain
    # spaces inside the street (two after the house number), and the only
    # street/city separator is a run of non-breaking spaces. Split on those
    # alone, before any whitespace collapsing.
    raw_loc = re.search(r"Location\s*:\s*(.*?)\s*School District", text, flags=re.I | re.S)
    location, location_city = None, None
    if raw_loc:
        chunks = [re.sub(r"\s+", " ", c).strip() for c in re.split(r" +", raw_loc.group(1))]
        chunks = [c for c in chunks if c]
        if chunks:
            location = chunks[0]
            location_city = " ".join(chunks[1:]) or None
    text = re.sub(r"\s+", " ", text)

    def grab(pattern):
        m = re.search(pattern, text, flags=re.I)
        return m.group(1).strip() if m else None

    mailing = grab(r"Mailing Address\s*(.*?)\s*Taxroll Information")
    legal = grab(r"Legal Description and Other Information:\s*(.*?)\s*(?:History|FINAL PAYMENT|Assessed)")
    land = grab(r"Land\s+([\d,]+)")
    impr = grab(r"Improvements\s+([\d,]+)")
    net = grab(r"Net Assessed\s+([\d,]+)")

    def num(s):
        try:
            return float(s.replace(",", "")) if s else None
        except ValueError:
            return None

    county = _county_from_url(detail_url)
    ratio = assessment_ratio(county)
    out = {
        "location": location or None,
        "location_city": location_city,
        "mailing_address": mailing or None,
        "legal": legal or None,
        "assessed_land": num(land),
        "assessed_improvements": num(impr),
        "net_assessed": num(net),
        "assessment_ratio": ratio,
        "implied_market_value": round(num(net) / ratio) if num(net) else None,
        "owner_occupied_guess": bool(location and mailing and
                                     location.split()[:2] == mailing.split()[:2])
        if location and mailing else None,
    }
    _cache_save(key, out)
    return out


def _county_from_url(url: str) -> str:
    m = re.search(r"/owner_details/([a-z_]+)", url or "")
    return m.group(1) if m else "washington"


# Oklahoma statute puts assessed value at 11-13.5% of market, but treasurers'
# net-assessed figures (after exemptions, and lagging the market) come out well
# under that. Measured against 38 listed Washington County homes the median was
# 8.2%. Each county is calibrated from its own listings when possible.
DEFAULT_ASSESSMENT_RATIO = 0.082
RATIO_CACHE_TTL = 30 * 24 * 3600


def assessment_ratio(county: str) -> float:
    hit = _cache_load(f"ratio_{county}.json", RATIO_CACHE_TTL)
    if hit and hit.get("ratio"):
        return float(hit["ratio"])
    return DEFAULT_ASSESSMENT_RATIO


def calibrate_assessment_ratio(county: str, listings, max_samples: int = 40) -> dict:
    """Median net-assessed / list-price over currently listed homes.

    `listings` is an iterable of (address, list_price). Results are cached for
    a month; the ratio drifts slowly with the market and the assessor's cycle.
    """
    county = county.strip().lower()
    hit = _cache_load(f"ratio_{county}.json", RATIO_CACHE_TTL)
    if hit and hit.get("ratio"):
        return hit
    ratios = []
    for address, price in listings:
        if len(ratios) >= max_samples:
            break
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price < 40_000:
            continue
        parts = split_street(address)
        if not parts:
            continue
        try:
            recs = [r for r in lookup_address(county, *parts)
                    if r["type"] == "Real Estate" and r["detail_url"]]
            if not recs:
                continue
            d = parcel_detail(recs[0]["detail_url"])
        except TaxRollError:
            continue
        na = d.get("net_assessed")
        if na and na > 500 and (d.get("assessed_improvements") or 0) > 0:
            ratios.append(na / price)
    if len(ratios) < 8:
        return {"ratio": DEFAULT_ASSESSMENT_RATIO, "samples": len(ratios), "calibrated": False}
    ratios.sort()
    mid = ratios[len(ratios) // 2]
    # Keep it inside the band the statute and the data both allow.
    mid = max(0.05, min(0.14, mid))
    out = {"ratio": round(mid, 4), "samples": len(ratios), "calibrated": True}
    _cache_save(f"ratio_{county}.json", out)
    return out


def lookup_address(county: str, street_number: str, street_name: str) -> list[dict]:
    """All tax records for a street address, paid and unpaid, across years."""
    county = county.strip().lower()
    params = {"street_number": str(street_number).strip(),
              "street_name": street_name.strip().upper(), "show_unpaid_only": "0"}
    rows = _query(county, "street_address", params, length=200)
    return [rec for rec in (_row(c, "street_address") for c in rows) if rec]


def lookup_owner(county: str, last_name: str, first_name: str = "",
                 unpaid_only: bool = False) -> list[dict]:
    county = county.strip().lower()
    params = {"first_name": first_name.strip().upper(), "last_name": last_name.strip().upper(),
              "business_owner_name": "", "show_unpaid_only": "1" if unpaid_only else "0"}
    rows = _query(county, "owner_name", params, length=500)
    return [rec for rec in (_row(c, "owner_name") for c in rows) if rec]


_STREET_CORE = re.compile(
    r"^\s*(\d+[A-Za-z]?)(?:-\d+)?\s+(?:(?:N|S|E|W|NE|NW|SE|SW|NORTH|SOUTH|EAST|WEST)\.?\s+)?(.+?)"
    r"(?:\s+(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|CT|COURT|PL|PLACE|BLVD|"
    r"TER|TERRACE|CIR|CIRCLE|WAY|HWY|PKWY|TRL|LOOP)\.?)?(?:\s+(?:N|S|E|W|NE|NW|SE|SW))?\s*$",
    re.I,
)


def split_street(line: str) -> tuple[str, str] | None:
    """'1552 S Maple Ave' -> ('1552', 'MAPLE'). The roll matches on the core name."""
    line = (line or "").split(",")[0].strip()
    m = _STREET_CORE.match(line)
    if not m:
        return None
    return m.group(1), m.group(2).strip().upper()


def delinquency_for_address(county: str, address: str) -> dict[str, Any]:
    """Summarise unpaid property tax for a listed address.

    Returns unpaid years, total owed and the owner of record. Used to cross-
    reference active listings: a listing whose owner is also behind on taxes
    is a more motivated seller than the description will admit.
    """
    parts = split_street(address)
    if not parts:
        return {"found": False, "reason": "unparseable address"}
    number, name = parts
    try:
        recs = lookup_address(county, number, name)
    except TaxRollError as exc:
        return {"found": False, "reason": str(exc)}
    if not recs:
        return {"found": False, "reason": "no tax record at that address"}

    real = [r for r in recs if r["type"] == "Real Estate"]
    special = [r for r in recs if r["type"] == "Special Assessment"]
    unpaid_years = sorted({r["year"] for r in real if not r["paid"] and r["year"]})
    owed = sum((r["total_due"] or 0) for r in real if not r["paid"])
    liens = sum((r["total_due"] or 0) for r in special if not r["paid"])
    latest = max((r for r in real if r["year"]), key=lambda r: r["year"], default=None)
    return {
        "found": True,
        "owner": (real or recs)[0]["owner"],
        "unpaid_years": unpaid_years,
        "years_behind": len(unpaid_years),
        "tax_owed": round(owed, 2),
        "special_assessments_owed": round(liens, 2),
        "annual_tax": latest["base_tax"] if latest else None,
        "annual_tax_year": latest["year"] if latest else None,
        "detail_url": (real or recs)[0]["detail_url"],
        "address_on_roll": (real or recs)[0]["address"],
    }
