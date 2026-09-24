"""Off-market and pre-market lead discovery.

The deal finder ranks what is already listed. This module hunts for what is
about to be -- properties whose owners are under pressure that has not yet
turned into an MLS listing, or whose listing is quietly saying more than the
price does.

Sources, roughly in order of how early they see trouble:

  court filings      foreclosure, probate, divorce   (assisted; see oscn_leads)
  tax delinquency    county treasurer roll            (automated, no gate)
  city liens         special assessments on the roll  (automated)
  expired listings   tried to sell, failed            (automated)
  listing language   as-is, investor, estate, TLC...  (automated)
  price cuts         detected between scans           (automated, accumulates)
  stale listings     long days on market              (automated)
  below last sale    listed under what they paid      (automated)
  sheriff's sales    end of the foreclosure line      (automated when posted)

Every lead carries the signals that produced it and a 0-100 score, and a
listed property that also shows up on the tax roll gets both sets merged --
the intersection is where the real motivation lives.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import warnings
from collections import defaultdict
from typing import Any, Callable

import pandas as pd
import requests

from . import oscn_leads, prefs, regions, taxroll
from .data import CACHE_DIR, _cached_scrape

warnings.filterwarnings("ignore")

Progress = Callable[[str], None] | None

RESIDENTIAL = {"SINGLE_FAMILY", "MULTI_FAMILY", "DUPLEX", "DUPLEX_TRIPLEX", "TRIPLEX",
               "TOWNHOMES", "CONDOS", "CONDO", "MOBILE"}

# --- listing-language signals ------------------------------------------------
# Each pattern is written to avoid the obvious false positives: 'real estate'
# is not an estate sale, 'fireplace' is not fire damage, 'new roof' is a
# feature not a defect.
KEYWORD_SIGNALS: list[tuple[str, re.Pattern, int, str]] = [
    ("foreclosure", re.compile(r"\bforeclos|\bbank[- ]owned|\breo\b|\bpre-?foreclos|"
                               r"\bhud[- ]owned|\bhud home|\bfannie mae|\bfreddie mac|"
                               r"\bhomepath|\bhomesteps", re.I), 45,
     "Listing says bank/agency owned or in foreclosure"),
    ("short_sale", re.compile(r"\bshort sale|\bsubject to (bank|lender|third.party) approval", re.I), 40,
     "Short sale - lender will take less than owed"),
    ("auction", re.compile(r"\bauction\b|\bsheriff'?s sale|\btrustee'?s sale", re.I), 35,
     "Going to auction"),
    ("estate", re.compile(r"\bestate sale\b|\bestate of\b|\bprobate\b|\bheirs?\b|"
                          r"\binherit|\bexecutor|\bdeceased|\bpassed away", re.I), 30,
     "Estate / inherited - heirs usually want out"),
    ("investor", re.compile(r"\binvestor|\bhandyman|\bfixer|\btlc\b|\bsweat equity|"
                            r"\bbring your (tools|vision|contractor)|\bneeds (some |a little |a lot of )?"
                            r"(work|repairs?|updating|renovation|love)|\bflip\b|"
                            r"\b(needs|ready for|complete|full|total) rehab\b|"
                            r"\bdiamond in the rough|\bfull of potential|\bunfinished|"
                            r"\bpartially (renovated|remodeled|complete)", re.I), 25,
     "Pitched at investors / needs work"),
    ("as_is", re.compile(r"\bas[- ]is\b|\bsold as\b|\bwhere[- ]is\b|\bno repairs", re.I), 20,
     "Sold as-is"),
    ("cash_only", re.compile(r"\bcash (only|buyers?|offers? only)\b|\bno (owner )?financing|"
                             r"\bwill not (finance|qualify)|\bnot (fha|va|conventional)", re.I), 25,
     "Cash only - will not pass a lender inspection"),
    ("damage", re.compile(r"\b(water|storm|fire|smoke|flood|hail|wind|tornado) damage|"
                          r"\bfire[- ]damaged|\bmold\b|\bcondemned|\bfoundation (issue|problem|"
                          r"repair|crack|work)|\broof (leak|needs|is bad|damage)|"
                          r"\bneeds (a |a new )?roof", re.I), 25,
     "Describes damage or a failed system"),
    ("motivated", re.compile(r"\bmotivated\b|\bmust sell\b|\bbring (all |your )?offers?|"
                             r"\bpriced to (sell|move)|\bquick (sale|close)|\bseller (says|wants)|"
                             r"\bmake (an |us an )?offer|\bwon'?t last", re.I), 20,
     "Seller signalling urgency"),
    ("price_cut_text", re.compile(r"\bprice (reduced|reduction|drop|improvement)|\breduced\b|"
                                  r"\bnew price", re.I), 10,
     "Mentions a price reduction"),
    ("vacant", re.compile(r"\bvacant\b|\bunoccupied\b|\bsits empty\b|\bempty (house|home)\b", re.I), 15,
     "Vacant - owner is carrying it"),
    ("life_event", re.compile(r"\brelocat|\bjob transfer|\bdivorce|\bdownsiz|\bmoving out of state|"
                              r"\bhealth reasons|\bassisted living|\bnursing home", re.I), 15,
     "Life event driving the sale"),
    ("tenant", re.compile(r"\btenant[- ]occupied|\bcurrently (rented|leased)|\blong[- ]term tenant|"
                          r"\bmonth[- ]to[- ]month", re.I), 5,
     "Tenant in place (harder to show, fewer retail buyers)"),
]

SCORE_CAP = 100


_prefs_cache: dict = {}


def _PREFS() -> dict:
    """Preferences, read once per process tick rather than per row."""
    import time
    now = time.time()
    if not _prefs_cache or now - _prefs_cache.get("_at", 0) > 5:
        _prefs_cache.clear()
        _prefs_cache.update(prefs.load())
        _prefs_cache["_at"] = now
    return _prefs_cache


def _num(x) -> float | None:
    try:
        v = float(x)
        return None if v != v else v  # NaN check
    except (TypeError, ValueError):
        return None


def _s(x) -> str:
    """String or '' -- pandas' NA type raises on truthiness, so never `x or ''`."""
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    return str(x)


def _addr_key(address) -> str | None:
    parts = taxroll.split_street(_s(address))
    return f"{parts[0]}|{parts[1]}" if parts else None


# --- snapshots: price history between scans ----------------------------------

def _snap_path(county_key: str) -> str:
    d = os.path.join(CACHE_DIR, "snapshots")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{county_key}.json")


def _update_snapshots(county_key: str, active: pd.DataFrame) -> dict[str, dict]:
    """Record today's list prices; return per-property price history."""
    path = _snap_path(county_key)
    try:
        with open(path, encoding="utf-8") as fh:
            store = json.load(fh)
    except Exception:
        store = {}
    today = dt.date.today().isoformat()
    for _, r in active.iterrows():
        pid = _s(r.get("property_id"))
        price = _num(r.get("list_price"))
        if not pid or not price:
            continue
        rec = store.setdefault(pid, {"history": [], "first_seen": today})
        hist = rec["history"]
        if not hist or hist[-1][1] != price:
            hist.append([today, price])
        rec["last_seen"] = today
        rec["address"] = _s(r.get("formatted_address"))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
    except Exception:
        pass
    return store


# --- listed-property signals --------------------------------------------------

def listed_signals(counties: list[dict], progress: Progress = None) -> tuple[list[dict], pd.DataFrame]:
    """Score every active listing on what its own record gives away."""
    frames = []
    for c in counties:
        try:
            df = _cached_scrape(f"search_active|{c['query'].lower()}",
                                location=c["query"], listing_type="for_sale")
            if df is not None and not df.empty:
                df = df.copy()
                df["search_county"] = c["key"]
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return [], pd.DataFrame()
    active = pd.concat(frames, ignore_index=True)
    if "property_id" in active.columns:
        active = active.drop_duplicates(subset=["property_id"])

    snapshots: dict[str, dict] = {}
    for c in counties:
        sub = active[active["search_county"] == c["key"]]
        snapshots.update(_update_snapshots(c["key"], sub))

    leads: list[dict] = []
    for _, r in active.iterrows():
        style = _s(r.get("style")).upper()
        if style and style not in RESIDENTIAL:
            continue
        price = _num(r.get("list_price"))
        if not price or price < 15_000:
            continue
        # A lead you cannot drive past is not a lead: require a street number.
        if not re.match(r"\s*\d", _s(r.get("formatted_address"))):
            continue
        if prefs.city_excluded(r.get("city"), _PREFS()):
            continue

        signals: list[dict] = []
        text = _s(r.get("text"))
        for tag, pat, weight, label in KEYWORD_SIGNALS:
            m = pat.search(text)
            if m:
                start = max(0, m.start() - 60)
                snippet = re.sub(r"\s+", " ", text[start:m.end() + 60]).strip()
                signals.append({"tag": tag, "weight": weight, "label": label,
                                "evidence": f"...{snippet}..."})

        dom = _num(r.get("days_on_mls"))
        if dom and dom >= 180:
            signals.append({"tag": "stale", "weight": 20, "label": f"{dom:.0f} days on market",
                            "evidence": "Seller has been waiting a long time; leverage."})
        elif dom and dom >= 90:
            signals.append({"tag": "stale", "weight": 10, "label": f"{dom:.0f} days on market",
                            "evidence": "Past the point where retail buyers have passed."})

        last_price = _num(r.get("last_sold_price"))
        if last_price and last_price > 20_000 and price < last_price * 0.97:
            pct = (1 - price / last_price) * 100
            signals.append({"tag": "below_last_sale", "weight": 25,
                            "label": f"Listed {pct:.0f}% below last sale (${last_price:,.0f})",
                            "evidence": "Owner is taking a loss - equity or condition problem."})

        pid = _s(r.get("property_id"))
        hist = (snapshots.get(pid) or {}).get("history") or []
        if len(hist) >= 2:
            peak = max(p for _, p in hist)
            if price < peak:
                pct = (1 - price / peak) * 100
                w = 20 if pct >= 10 else 10 if pct >= 5 else 5
                signals.append({"tag": "price_cut", "weight": w,
                                "label": f"Cut {pct:.0f}% since first seen (${peak:,.0f})",
                                "evidence": f"{len(hist) - 1} reduction(s) observed by this app."})

        est = _num(r.get("estimated_value"))
        if est and est > 0 and price < est * 0.75:
            signals.append({"tag": "under_avm", "weight": 15,
                            "label": f"Asking {price / est * 100:.0f}% of Realtor estimate (${est:,.0f})",
                            "evidence": "Priced well under the automated valuation."})

        if not signals:
            continue
        raw = sum(s["weight"] for s in signals)
        score = min(SCORE_CAP, raw)
        leads.append({
            "kind": "listed", "raw_score": raw,
            "address": _s(r.get("formatted_address")) or None,
            "addr_key": _addr_key(r.get("formatted_address")),
            "city": _s(r.get("city")) or None, "zip": _s(r.get("zip_code")) or None,
            "county": _s(r.get("county")) or _s(r.get("search_county")),
            "county_key": _s(r.get("search_county")),
            "url": _s(r.get("property_url")) or None,
            "price": round(price), "sqft": _num(r.get("sqft")),
            "beds": _num(r.get("beds")), "baths": _num(r.get("full_baths")),
            "year_built": _num(r.get("year_built")), "days_on_mls": dom,
            "style": style, "status": _s(r.get("status")) or None,
            "signals": signals, "score": score,
            "tags": sorted({s["tag"] for s in signals}),
        })
    leads.sort(key=lambda x: -x["raw_score"])
    return leads, active


def expired_listings(counties: list[dict], active: pd.DataFrame,
                     progress: Progress = None) -> list[dict]:
    """Listings that expired or were withdrawn without selling.

    The owner wanted out, the market said no at that price, and there is no
    agent in the way any more. A classic direct-mail target, here for free.
    """
    active_ids = set(active["property_id"].astype(str)) if "property_id" in active.columns else set()
    out: list[dict] = []
    for c in counties:
        try:
            df = _cached_scrape(f"search_offmarket|{c['query'].lower()}",
                                location=c["query"], listing_type="off_market")
        except Exception:
            continue
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            pid = _s(r.get("property_id"))
            if pid in active_ids:
                continue  # re-listed; it will show up in listed signals instead
            if prefs.city_excluded(r.get("city"), _PREFS()):
                continue
            style = _s(r.get("style")).upper()
            if style and style not in RESIDENTIAL:
                continue
            price = _num(r.get("list_price"))
            if not price or price < 20_000:
                continue  # rentals share this feed; drop anything priced like rent
            changed = r.get("last_status_change_date")
            try:
                changed_dt = pd.Timestamp(changed)
                days_ago = (pd.Timestamp.today() - changed_dt.tz_localize(None)
                            if changed_dt.tzinfo else pd.Timestamp.today() - changed_dt).days
            except Exception:
                days_ago = None
            signals = [{"tag": "expired", "weight": 35,
                        "label": f"Expired at ${price:,.0f}"
                                 + (f", {days_ago} days ago" if days_ago is not None else ""),
                        "evidence": "Tried to sell and could not; no agent now."}]
            last_price = _num(r.get("last_sold_price"))
            if last_price and last_price > 20_000 and price < last_price * 0.97:
                signals.append({"tag": "below_last_sale", "weight": 15,
                                "label": f"Was asking below last sale (${last_price:,.0f})",
                                "evidence": ""})
            out.append({
                "kind": "expired",
                "address": _s(r.get("formatted_address")) or None,
                "addr_key": _addr_key(r.get("formatted_address")),
                "city": _s(r.get("city")) or None, "zip": _s(r.get("zip_code")) or None,
                "county": _s(r.get("county")) or c["name"], "county_key": c["key"],
                "url": _s(r.get("property_url")) or None,
                "price": round(price), "sqft": _num(r.get("sqft")),
                "beds": _num(r.get("beds")), "baths": _num(r.get("full_baths")),
                "year_built": _num(r.get("year_built")),
                "expired_days_ago": days_ago, "style": style,
                "signals": signals, "score": min(SCORE_CAP, sum(s["weight"] for s in signals)),
                "raw_score": sum(s["weight"] for s in signals),
                "tags": sorted({s["tag"] for s in signals}),
            })
    out.sort(key=lambda x: -x["raw_score"])
    return out


# --- tax delinquency ----------------------------------------------------------

def tax_delinquent(county_key: str, max_details: int = 250,
                   progress: Progress = None) -> list[dict]:
    """Parcels behind on property tax, worst first, with addresses resolved."""
    try:
        recs = taxroll.county_unpaid(county_key)
    except taxroll.TaxRollError as exc:
        if progress:
            progress(f"Tax roll unavailable for {county_key}: {exc}")
        return []

    by_pid: dict[str, dict] = {}
    for r in recs:
        pid = r.get("property_id")
        if not pid:
            continue
        g = by_pid.setdefault(pid, {"property_id": pid, "owner": r["owner"],
                                    "detail_url": r["detail_url"], "real_years": set(),
                                    "tax_owed": 0.0, "liens_owed": 0.0, "lien_count": 0,
                                    "kinds": set()})
        g["kinds"].add(r["type"])
        if r["type"] == "Real Estate":
            if r["year"]:
                g["real_years"].add(r["year"])
            g["tax_owed"] += r["total_due"] or 0
            # prefer the real-estate record's detail page
            g["detail_url"] = r["detail_url"] or g["detail_url"]
        elif r["type"] == "Special Assessment":
            g["liens_owed"] += r["total_due"] or 0
            g["lien_count"] += 1

    leads: list[dict] = []
    for g in by_pid.values():
        if "Real Estate" not in g["kinds"] and "Special Assessment" not in g["kinds"]:
            continue  # personal property, business, utility
        years = sorted(g["real_years"])
        n = len(years)
        signals = []
        if n >= 3:
            signals.append({"tag": "tax_3yr", "weight": 60,
                            "label": f"{n} years of unpaid tax ({years[0]}-{years[-1]})",
                            "evidence": "Eligible for the treasurer's June resale. Owner loses "
                                        "the property outright if it is not redeemed."})
        elif n == 2:
            signals.append({"tag": "tax_2yr", "weight": 40,
                            "label": f"2 years unpaid ({years[0]}, {years[1]})",
                            "evidence": "One more year and it goes to resale."})
        elif n == 1:
            signals.append({"tag": "tax_1yr", "weight": 15,
                            "label": f"{years[0]} tax unpaid",
                            "evidence": "Early sign; worth watching."})
        if g["lien_count"]:
            signals.append({"tag": "city_lien", "weight": 25,
                            "label": f"{g['lien_count']} special assessment(s), ${g['liens_owed']:,.0f}",
                            "evidence": "City liens for mowing, clean-up or demolition. "
                                        "Code enforcement has already been out."})
        if not signals:
            continue
        leads.append({
            "kind": "tax", "property_id": g["property_id"], "owner": g["owner"],
            "detail_url": g["detail_url"], "unpaid_years": years, "years_behind": n,
            "tax_owed": round(g["tax_owed"], 2), "liens_owed": round(g["liens_owed"], 2),
            "county_key": county_key, "signals": signals,
            "score": min(SCORE_CAP, sum(s["weight"] for s in signals)),
            "raw_score": sum(s["weight"] for s in signals),
            "tags": sorted({s["tag"] for s in signals}),
            "address": None, "addr_key": None,
        })

    leads.sort(key=lambda x: (-x["score"], -(x["tax_owed"] + x["liens_owed"])))

    # Resolve addresses for the strongest, within a request budget.
    resolved = 0
    for lead in leads:
        if resolved >= max_details:
            break
        if not lead["detail_url"]:
            continue
        try:
            d = taxroll.parcel_detail(lead["detail_url"])
        except taxroll.TaxRollError:
            continue
        resolved += 1
        lead.update({
            "address": d.get("location"), "addr_key": _addr_key(d.get("location")),
            "location_city": d.get("location_city"),
            "legal": d.get("legal"), "mailing_address": d.get("mailing_address"),
            "net_assessed": d.get("net_assessed"),
            "assessed_improvements": d.get("assessed_improvements"),
            "implied_value": d.get("implied_market_value"),
            "land_only": (d.get("assessed_improvements") or 0) == 0,
        })
        if d.get("owner_occupied_guess") is False:
            lead["signals"].append({"tag": "absentee", "weight": 10,
                                    "label": "Absentee owner (tax bill goes elsewhere)",
                                    "evidence": d.get("mailing_address") or ""})
            lead["score"] = min(SCORE_CAP, lead["score"] + 10)
            lead["tags"] = sorted(set(lead["tags"]) | {"absentee"})
        if progress and resolved % 50 == 0:
            progress(f"Resolved {resolved} delinquent parcels...")

    # Places the user has ruled out. Only knowable once the parcel is resolved.
    leads = [l for l in leads
             if not prefs.city_excluded(l.get("location_city"), _PREFS())
             and not prefs.address_excluded(l.get("address"), _PREFS())]

    # Land-only parcels are not flips; push them down without hiding them.
    leads.sort(key=lambda x: (x.get("land_only", False), -x["score"],
                              -(x["tax_owed"] + x["liens_owed"])))
    return leads


# --- sheriff's sales -----------------------------------------------------------

SHERIFF_PAGES = {
    "washington": "https://www.washingtoncosheriff.com/sheriff-sales",
}


def sheriff_sales(county_key: str) -> dict[str, Any]:
    url = SHERIFF_PAGES.get(county_key)
    if not url:
        return {"url": None, "items": [], "note": "No sheriff's sale page configured for this county."}
    try:
        r = requests.get(url, headers={"User-Agent": taxroll.UA}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        return {"url": url, "items": [], "note": f"Could not load: {exc}"}
    html = r.text
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    if re.search(r"no sheriff sales posted", text, re.I):
        return {"url": url, "items": [],
                "note": "Nothing posted right now. The list appears when sales are scheduled; "
                        "each is also docketed on OSCN as a Notice of Sheriff's Sale."}
    items = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
        if len(cells) >= 2 and any(re.search(r"\b[A-Z]{2}-\d{4}-\d+\b|\$\s?[\d,]+", c) for c in cells):
            items.append({"cells": cells})
    if not items:
        for m in re.finditer(r"([A-Z]{2}-\d{4}-\d+)[^.]{0,200}", text):
            items.append({"cells": [m.group(0).strip()]})
    return {"url": url, "items": items,
            "note": f"{len(items)} sale(s) posted." if items else
                    "Page loaded but no sale rows were recognised; open it directly."}


# --- assemble -------------------------------------------------------------------

def _merge_listed_with_tax(listed: list[dict], expired: list[dict],
                           tax: list[dict], county_key: str,
                           live_checks: int = 40, progress: Progress = None) -> None:
    """Cross-reference: a listed/expired address that is also delinquent."""
    tax_by_key = {t["addr_key"]: t for t in tax if t.get("addr_key")}
    checked = 0
    for lead in listed + expired:
        key = lead.get("addr_key")
        hit = tax_by_key.get(key) if key else None
        if hit is None and (lead.get("county_key") == county_key) and checked < live_checks:
            checked += 1
            try:
                d = taxroll.delinquency_for_address(county_key, lead["address"])
            except Exception:
                d = {"found": False}
            if d.get("found") and (d["years_behind"] or d["special_assessments_owed"]):
                hit = {
                    "owner": d["owner"], "unpaid_years": d["unpaid_years"],
                    "years_behind": d["years_behind"], "tax_owed": d["tax_owed"],
                    "liens_owed": d["special_assessments_owed"], "detail_url": d["detail_url"],
                    "signals": [], "tags": [],
                }
                n = d["years_behind"]
                if n >= 3:
                    hit["signals"].append({"tag": "tax_3yr", "weight": 60,
                                           "label": f"{n} years unpaid tax", "evidence": ""})
                elif n == 2:
                    hit["signals"].append({"tag": "tax_2yr", "weight": 40,
                                           "label": "2 years unpaid tax", "evidence": ""})
                elif n == 1:
                    hit["signals"].append({"tag": "tax_1yr", "weight": 15,
                                           "label": "1 year unpaid tax", "evidence": ""})
                if d["special_assessments_owed"]:
                    hit["signals"].append({"tag": "city_lien", "weight": 25,
                                           "label": f"City liens ${d['special_assessments_owed']:,.0f}",
                                           "evidence": ""})
            elif d.get("found"):
                lead["tax_status"] = "current"
        if hit:
            lead["owner"] = hit.get("owner")
            lead["tax_owed"] = hit.get("tax_owed")
            lead["liens_owed"] = hit.get("liens_owed")
            lead["unpaid_years"] = hit.get("unpaid_years")
            lead["years_behind"] = len(hit.get("unpaid_years") or [])
            lead["detail_url"] = hit.get("detail_url")
            lead["tax_status"] = "delinquent"
            existing = {s["tag"] for s in lead["signals"]}
            for s in hit["signals"]:
                if s["tag"] not in existing:
                    lead["signals"].append(s)
            lead["raw_score"] = sum(s["weight"] for s in lead["signals"])
            lead["score"] = min(SCORE_CAP, lead["raw_score"])
            lead["tags"] = sorted({s["tag"] for s in lead["signals"]})
            hit["also_listed"] = lead["address"]
            hit["listing_url"] = lead.get("url")
            hit["listing_price"] = lead.get("price")


def find_leads(home: str = regions.DEFAULT_REGION, include_adjacent: bool = True,
               include_metro: bool = False, tax_details: int = 250,
               progress: Progress = None) -> dict[str, Any]:
    counties = regions.resolve(home, include_adjacent, include_metro)
    home = home.strip().lower()

    if progress:
        progress("Reading active listings for language, staleness and price cuts...")
    listed, active = listed_signals(counties, progress)

    if progress:
        progress("Reading expired listings...")
    expired = expired_listings(counties, active, progress)

    tax: list[dict] = []
    for i, c in enumerate(counties):
        if progress:
            progress(f"Pulling the {c['name']} tax roll...")
        # Calibrate assessed->market for this county from its own listings.
        if "property_id" in active.columns and "search_county" in active.columns:
            sub = active[active["search_county"] == c["key"]]
            pairs = [(_s(r.get("formatted_address")), r.get("list_price"))
                     for _, r in sub.iterrows()]
            if pairs:
                try:
                    taxroll.calibrate_assessment_ratio(c["key"], pairs)
                except Exception:
                    pass
        # Home county gets the full budget; neighbours share what is left.
        budget = tax_details if i == 0 else max(40, tax_details // 3)
        tax += tax_delinquent(c["key"], max_details=budget, progress=progress)
    tax.sort(key=lambda x: (x.get("land_only", False), -x["score"],
                            -(x["tax_owed"] + x["liens_owed"])))

    if progress:
        progress("Cross-referencing listings against the tax rolls...")
    for c in counties:
        _merge_listed_with_tax([l for l in listed if l.get("county_key") == c["key"]],
                               [l for l in expired if l.get("county_key") == c["key"]],
                               [t for t in tax if t.get("county_key") == c["key"]],
                               c["key"], live_checks=40 if c["key"] == home else 12,
                               progress=progress)
    listed.sort(key=lambda x: -x.get("raw_score", x["score"]))
    expired.sort(key=lambda x: -x.get("raw_score", x["score"]))

    sheriff = sheriff_sales(home)

    return {
        "counties": [c["name"] for c in counties],
        "home_county": regions.COUNTIES[home]["name"],
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "stats": {
            "active_scanned": int(len(active)),
            "listed_with_signals": len(listed),
            "expired": len(expired),
            "tax_parcels": len(tax),
            "tax_counties": sorted({t["county_key"] for t in tax}),
            "tax_3yr": sum(1 for t in tax if t["years_behind"] >= 3),
            "tax_2yr": sum(1 for t in tax if t["years_behind"] == 2),
            "city_liens": sum(1 for t in tax if t["liens_owed"]),
            "listed_and_delinquent": sum(1 for l in listed + expired
                                         if l.get("tax_status") == "delinquent"),
        },
        "listed": listed,
        "expired": expired,
        "tax": tax,
        "sheriff": sheriff,
        "oscn": {
            "searches": oscn_leads.suggested_searches(home),
            "note": "Court records need a one-time human check in your browser. Open a "
                    "search, save the page (Ctrl+S, 'Webpage, HTML only') or copy its "
                    "source, and paste it below. The app classifies every case and "
                    "matches defendants to parcels on the tax roll.",
        },
    }


def oscn_from_html(county_key: str, html: str) -> dict[str, Any]:
    cases = oscn_leads.parse_results(html)
    if not cases:
        return {"cases": [], "note": "No case rows found. Make sure you saved the results "
                                     "page itself (it lists case numbers like CJ-2026-123)."}
    oscn_leads.cross_reference(cases, county_key)
    cases.sort(key=lambda c: (-c["score"], c["number"]))
    counts = defaultdict(int)
    for c in cases:
        counts[c["category"]] += 1
    return {"cases": cases, "counts": dict(counts),
            "with_parcels": sum(1 for c in cases if c.get("parcels"))}
