"""Assessor parcel roll -> pre-market leads.

The best off-market list is not scraped; it is requested. Under the Oklahoma
Open Records Act a county assessor will provide the parcel roll -- every
property with owner, mailing address, exemptions and last sale -- usually as
a spreadsheet for a small copying fee. That one file holds the owners most
likely to sell before they ever call an agent:

  out-of-state owner     inherited or moved away; the house is a chore
  absentee owner         landlord or empty house; mail goes elsewhere
  long ownership         15-25+ years means large equity and room to deal
  no homestead           not owner-occupied: a rental, or vacant
  estate / heirs / trust title held by someone who never lived there
  senior exemption       aging owner; downsizing and estate planning

Each is weak alone. Stacked, they find the house whose owner says yes to a
fair letter. This reads whatever column names the county uses, scores every
residential parcel, and returns the strongest -- ready for letters.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from typing import Any

import pandas as pd

from . import focus

# Header aliases, matched case-insensitively after stripping non-letters.
ALIASES = {
    "owner": ["owner", "ownername", "owner1", "name1", "taxpayer", "ownernm", "owners", "name"],
    "mail1": ["mailaddress", "mailingaddress", "mailaddr", "mailadd1", "mailaddress1", "mailingaddress1",
              "addr1", "address1", "mailstreet", "ownaddress", "owneraddress"],
    "mail_city": ["mailcity", "mailingcity", "ownercity", "city1"],
    "mail_state": ["mailstate", "mailingstate", "ownerstate", "state1", "mailst"],
    "mail_zip": ["mailzip", "mailingzip", "ownerzip", "zip1", "mailzipcode"],
    "situs": ["situs", "situsaddress", "siteaddress", "siteaddr", "propertyaddress", "propaddress",
              "location", "locationaddress", "physicaladdress", "address", "situsaddr"],
    "situs_city": ["situscity", "sitecity", "propertycity", "loccity", "city"],
    "situs_zip": ["situszip", "sitezip", "propertyzip", "zip", "zipcode"],
    "homestead": ["homestead", "homesteadexemption", "hs", "hsex", "homesteadflag", "exemption",
                  "exemptions", "exemptcode", "exemptioncode"],
    "senior": ["senior", "seniorfreeze", "valuefreeze", "additionalhomestead", "seniorexemption",
               "freeze"],
    "sale_date": ["saledate", "lastsaledate", "deeddate", "salesdate", "transferdate", "recorddate"],
    "sale_price": ["saleprice", "lastsaleprice", "salesprice", "considerationamount", "consideration"],
    "year_built": ["yearbuilt", "yrbuilt", "yearblt", "actualyearbuilt"],
    "sqft": ["sqft", "livingarea", "bldgsqft", "heatedarea", "squarefeet", "totalsqft", "sfla"],
    "value": ["marketvalue", "fairmarketvalue", "faircashvalue", "totalvalue", "appraisedvalue",
              "totalmarketvalue", "assessedvalue", "netassessed"],
    "improvements": ["improvementvalue", "bldgvalue", "buildingvalue", "improvements"],
    "school": ["school", "schooldistrict", "schooldist", "schdist", "district"],
    "use": ["landuse", "propertyclass", "class", "usecode", "propertytype", "propclass"],
    "parcel": ["parcel", "parcelid", "parcelnumber", "account", "accountno", "pin", "propertyid"],
}

ESTATE = re.compile(r"\b(estate of|est of|\best\b|heirs?|deceased|dec'?d|unknown heirs|"
                    r"successor|personal representative|executor|administrat)", re.I)
TRUST = re.compile(r"\b(trust|trustee|ttee|revocable|living trust|rev tr)\b", re.I)
ENTITY = re.compile(r"\b(llc|l\.l\.c|inc|corp|lp|ltd|properties|holdings|investments|rentals?|"
                    r"homes|realty|capital|partners)\b", re.I)
PUBLIC = re.compile(r"\b(city of|county|state of|school|church|housing authority|united states|"
                    r"board of|public|cemetery)\b", re.I)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def map_columns(columns: list[str]) -> dict[str, str]:
    """Best guess at which source column holds each field."""
    norm = {_norm_header(c): c for c in columns}
    out: dict[str, str] = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in norm and norm[n] not in out.values():
                out[field] = norm[n]
                break
        else:
            # Prefix match catches 'MAIL_ADDRESS_LINE_1' style headers.
            for n in names:
                hit = next((orig for k, orig in norm.items() if k.startswith(n) and orig not in out.values()), None)
                if hit:
                    out[field] = hit
                    break
    return out


def read(data: bytes | str, filename: str = "") -> pd.DataFrame:
    if isinstance(data, bytes) and (filename.lower().endswith((".xlsx", ".xls"))):
        return pd.read_excel(io.BytesIO(data), dtype=str)
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)


def _num(v) -> float | None:
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v))) if str(v).strip() else None
    except ValueError:
        return None


def _date(v) -> dt.date | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return pd.to_datetime(s, errors="raise").date()
    except Exception:
        return None


def _street_key(a: str) -> str:
    toks = re.sub(r"[^a-z0-9 ]", " ", (a or "").lower()).split()
    return " ".join(toks[:3])


def _yes(v) -> bool:
    s = str(v or "").strip().lower()
    return s not in ("", "0", "n", "no", "none", "false", "nan") and not s.startswith("no ")


def score_rows(df: pd.DataFrame, cols: dict[str, str], county_key: str | None = None) -> list[dict]:
    today = dt.date.today()
    get = lambda r, f: r.get(cols[f], "") if f in cols else ""
    out = []
    for _, r in df.iterrows():
        owner = str(get(r, "owner")).strip()
        situs = str(get(r, "situs")).strip()
        if not owner or not situs or not re.match(r"\s*\d", situs):
            continue  # no house number: land, or nothing to mail about
        if PUBLIC.search(owner):
            continue
        impr = _num(get(r, "improvements"))
        if impr is not None and impr <= 0:
            continue  # vacant land
        use = str(get(r, "use")).lower()
        if use and re.search(r"commerc|industr|agri|exempt|vacant|land only|utility", use):
            continue

        signals = []
        mail1 = str(get(r, "mail1")).strip()
        mstate = str(get(r, "mail_state")).strip().upper()
        if not mstate:
            m = re.search(r"\b([A-Z]{2})\s+\d{5}", str(get(r, "mail1")).upper())
            mstate = m.group(1) if m else ""
        if mstate and mstate != "OK":
            signals.append(("out_of_state", 30, f"Owner mails from {mstate}"))
        elif mail1 and _street_key(mail1) != _street_key(situs):
            signals.append(("absentee", 15, "Owner's mail goes to another address"))

        sd = _date(get(r, "sale_date"))
        if sd:
            yrs = (today - sd).days / 365.25
            if yrs >= 25:
                signals.append(("long_tenure", 30, f"Owned {yrs:.0f} years - likely free and clear"))
            elif yrs >= 15:
                signals.append(("long_tenure", 20, f"Owned {yrs:.0f} years - large equity"))

        if "homestead" in cols and not _yes(get(r, "homestead")):
            signals.append(("no_homestead", 10, "No homestead exemption - not owner-occupied"))
        if "senior" in cols and _yes(get(r, "senior")):
            signals.append(("senior", 10, "Senior value freeze - aging owner"))
        if ESTATE.search(owner):
            signals.append(("estate", 30, "Held by an estate or heirs"))
        elif TRUST.search(owner):
            signals.append(("trust", 10, "Held in trust - often the next step after a death"))
        elif ENTITY.search(owner):
            signals.append(("entity", 5, "Company-owned - possibly a tired landlord"))

        yb = _num(get(r, "year_built"))
        if yb and yb < 1975 and any(s[0] in ("long_tenure", "estate", "senior") for s in signals):
            signals.append(("dated", 10, f"Built {yb:.0f} and long-held - likely original condition"))

        school = str(get(r, "school")).upper()
        fkey = next((k for k in focus.DEFAULT_FOCUS if k.upper() in school), None)
        if not fkey:
            city = str(get(r, "situs_city")).upper()
            fkey = next((k for k in focus.DEFAULT_FOCUS if k.upper() == city), None)
        if fkey and signals:
            signals.append(("focus_school", focus.FOCUS_BONUS, f"In {focus.DISTRICTS[fkey]['name']}"))

        if not signals or sum(w for _, w, _ in signals) < 25:
            continue  # one weak signal is not a lead
        mail_full = " ".join(x for x in [mail1, str(get(r, "mail_city")).strip(), mstate,
                                         str(get(r, "mail_zip")).strip()] if x)
        city = str(get(r, "situs_city")).strip()
        score = sum(w for _, w, _ in signals)
        out.append({
            "kind": "roll", "county_key": county_key, "property_id": str(get(r, "parcel")) or None,
            "address": situs.upper(), "location_city": city.upper() or None,
            "owner": owner, "mailing_address": f"{owner} {mail_full}".strip(),
            "sale_date": sd.isoformat() if sd else None, "sale_price": _num(get(r, "sale_price")),
            "year_built": yb, "sqft": _num(get(r, "sqft")), "value": _num(get(r, "value")),
            "school_district": fkey, "school": focus.label(fkey),
            "signals": [{"tag": t, "weight": w, "label": l, "evidence": ""} for t, w, l in signals],
            "tags": sorted({t for t, _, _ in signals}),
            "score": min(100, score), "raw_score": score,
        })
    out.sort(key=lambda x: (-x["raw_score"], x["address"]))
    return out


def import_roll(data: bytes | str, filename: str = "", county_key: str | None = None) -> dict[str, Any]:
    df = read(data, filename)
    cols = map_columns(list(df.columns))
    missing = [f for f in ("owner", "situs", "mail1") if f not in cols]
    if missing:
        return {"error": f"Could not find columns for: {', '.join(missing)}. "
                         f"Headers seen: {', '.join(list(df.columns)[:30])}",
                "columns": cols, "rows": len(df)}
    leads = score_rows(df, cols, county_key)
    counts: dict[str, int] = {}
    for l in leads:
        for t in l["tags"]:
            counts[t] = counts.get(t, 0) + 1
    return {"rows": int(len(df)), "columns": cols, "leads": leads, "count": len(leads),
            "signal_counts": counts,
            "in_focus": sum(1 for l in leads if l.get("school_district"))}
