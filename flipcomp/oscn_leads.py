"""Court-record leads from OSCN (Oklahoma State Courts Network).

Oklahoma is a judicial-foreclosure state: every foreclosure is a civil lawsuit,
filed publicly on OSCN months before a sheriff's sale and long before anything
hits the MLS. Probate and divorce filings are the other two classic sources of
motivated sellers, and they live in the same index.

OSCN sits behind a human-verification gate, so this module does not fetch from
it. Instead it builds the exact search links, and parses the pages the user
brings back (saved HTML or a copy of the page source). Names found there are
then cross-referenced against the county tax roll, which has no gate, to turn a
case caption into a street address.
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any

from . import taxroll

RESULTS_URL = "https://www.oscn.net/dockets/Results.aspx"
CASE_URL = "https://www.oscn.net/dockets/GetCaseInformation.aspx"

# Case-number prefixes used by Oklahoma district courts.
CASE_TYPES = {
    "CJ": "civil",            # foreclosures live here
    "CS": "small claims",
    "CV": "civil (misc)",
    "PB": "probate",
    "FD": "family / divorce",
    "SC": "small claims",
    "TX": "tax",
}

# Plaintiffs whose civil suits are, in practice, always mortgage foreclosures.
LENDER_PATTERNS = [
    r"\bbank\b", r"\bmortgage\b", r"\bloan\b", r"\blending\b", r"\bservic(ing|er)\b",
    r"\bcredit union\b", r"\bfederal (national|home)\b", r"\bfannie mae\b",
    r"\bfreddie mac\b", r"\bfnma\b", r"\bfhlmc\b", r"\bhud\b", r"\bsecretary of housing\b",
    r"\bveterans affairs\b", r"\btrust(ee)?\b.*\bseries\b", r"\bfinanc(e|ial)\b",
    r"\bsavings\b", r"\bfunding\b", r"\bcapital\b", r"\bnationstar\b", r"\bmr\.? cooper\b",
    r"\bwells fargo\b", r"\bpnc\b", r"\bbokf\b", r"\bbank of oklahoma\b", r"\barvest\b",
    r"\bfreedom mortgage\b", r"\brocket\b", r"\bpennymac\b", r"\bcarrington\b",
    r"\bselect portfolio\b", r"\bocwen\b", r"\bphh\b", r"\blakeview\b", r"\bnewrez\b",
    r"\bshellpoint\b", r"\bus bank\b", r"\bu\.s\. bank\b", r"\bdeutsche\b", r"\bhsbc\b",
    r"\bcitibank\b", r"\bcitimortgage\b", r"\bjpmorgan\b", r"\bchase\b", r"\bflagstar\b",
    r"\bmidfirst\b", r"\btinker federal\b", r"\btruity\b", r"\bregent\b", r"\brcb\b",
    r"\bcommunity bank\b", r"\bfirst (national|bank|united)\b", r"\bhome point\b",
    r"\bcaliber\b", r"\bloandepot\b", r"\bguild\b", r"\bfairway\b",
]
_LENDER = re.compile("|".join(LENDER_PATTERNS), re.I)

# Civil suits that can also produce a distressed property.
OTHER_CIVIL = [
    (re.compile(r"\bhomeowners?'? assoc|\bhoa\b", re.I), "hoa lien"),
    (re.compile(r"\bcounty treasurer\b|\btax\b", re.I), "tax suit"),
    (re.compile(r"\bquiet title\b", re.I), "quiet title"),
    (re.compile(r"\bpartition\b", re.I), "partition"),
    (re.compile(r"\bcity of\b|\btown of\b", re.I), "municipal (code / lien)"),
    (re.compile(r"\bmechanic|\blien\b", re.I), "lien"),
]

_ENTITY_HINTS = re.compile(
    r"\b(llc|l\.l\.c|inc|corp|company|co\.|bank|trust|association|assn|mortgage|"
    r"partners|properties|holdings|group|services|city of|county|state of|"
    r"united states|federal|n\.a\.|na\b|fsb|ltd|lp\b)\b", re.I)


def _fmt(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def search_url(county: str, filed_after: dt.date, filed_before: dt.date | None = None,
               last_name: str = "") -> str:
    """Docket search for every case filed in a county over a date range."""
    q = {
        "db": county.strip().lower(), "number": "", "lname": last_name, "fname": "",
        "mname": "", "DoBMin": "", "DoBMax": "", "partytype": "", "apct": "",
        "dcct": "", "FiledDateL": _fmt(filed_after),
        "FiledDateH": _fmt(filed_before or dt.date.today()),
        "ClosedDateL": "", "ClosedDateH": "", "iLC": "", "iLCType": "",
        "iYear": "", "iNumber": "", "citation": "",
    }
    return RESULTS_URL + "?" + urllib.parse.urlencode(q)


def case_url(county: str, number: str) -> str:
    return CASE_URL + "?" + urllib.parse.urlencode({"db": county.lower(), "number": number})


def suggested_searches(county: str, days: int = 60) -> list[dict]:
    """The handful of searches worth running every week or two."""
    today = dt.date.today()
    since = today - dt.timedelta(days=days)
    return [
        {"key": "all_recent", "label": f"All cases filed in the last {days} days",
         "why": "One page covers foreclosures (CJ), probate (PB) and divorce (FD) "
                "at once. Paste it back and the app sorts them out.",
         "url": search_url(county, since, today)},
        {"key": "lender_suits", "label": "Suits by lenders (search 'bank' as a party)",
         "why": "Catches foreclosures older than the window above.",
         "url": search_url(county, today - dt.timedelta(days=365), today, last_name="bank")},
        {"key": "mortgage_suits", "label": "Suits by mortgage companies",
         "why": "Same idea for servicers that do not have 'bank' in the name.",
         "url": search_url(county, today - dt.timedelta(days=365), today, last_name="mortgage")},
    ]


# --- parsing ---------------------------------------------------------------

def _text(s: str) -> str:
    import html as h
    return re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_results(html: str) -> list[dict[str, Any]]:
    """Cases from a saved OSCN Results.aspx page.

    Tolerant of both the plain-HTML layout (table.caseCourtTable with
    tr.resultTableRow) and a copy of the rendered page where only the case
    links survive.
    """
    cases: dict[str, dict] = {}

    # Primary: rows in the results table.
    for row in re.findall(r"<tr[^>]*class=\"[^\"]*resultTableRow[^\"]*\"[^>]*>(.*?)</tr>",
                          html, flags=re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
        link = re.search(r"href=\"([^\"]*GetCaseInformation[^\"]*)\"", row, flags=re.I)
        number = None
        db = None
        if link:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(
                urllib.parse.unquote(link.group(1).replace("&amp;", "&"))).query)
            number = (q.get("number") or [None])[0]
            db = (q.get("db") or [None])[0]
        if not number:
            m = re.search(r"\b([A-Z]{2})-(\d{4})-(\d+)\b", " ".join(cells))
            if m:
                number = m.group(0)
        if not number:
            continue
        rec = cases.setdefault(number, {"number": number, "county": db, "cells": cells})
        rec["cells"] = cells

    # Fallback: any case links at all.
    if not cases:
        for href in re.findall(r"GetCaseInformation\.aspx\?([^\"'\s<>]+)", html, flags=re.I):
            q = urllib.parse.parse_qs(urllib.parse.unquote(href.replace("&amp;", "&")))
            number = (q.get("number") or [None])[0]
            if number:
                cases.setdefault(number, {"number": number, "county": (q.get("db") or [None])[0],
                                          "cells": []})

    out = []
    for number, rec in cases.items():
        cells = rec["cells"]
        # Typical row: [case number, date filed, case name, found party]
        filed = next((c for c in cells if re.fullmatch(r"\d{2}/\d{2}/\d{4}", c)), None)
        style = ""
        for c in cells:
            if " v. " in c.lower() or " vs " in c.lower() or "in re" in c.lower() \
                    or "in the matter" in c.lower():
                style = c
                break
        if not style and len(cells) >= 3:
            style = cells[2]
        out.append(classify(number, style, filed, rec.get("county")))
    return out


_IN_RE = re.compile(
    r"^(?:in\s+re:?|in\s+the\s+matter\s+of:?)?\s*(?:the\s+)?(?:estate|guardianship|"
    r"conservatorship|trust)\s+of:?\s+(.+)$", re.I)


def _split_style(style: str) -> tuple[str, list[str]]:
    """'BANK OF X v. DOE, JOHN, DOE, JANE' -> ('BANK OF X', ['DOE, JOHN', ...])

    Probate captions have no 'v.' -- 'IN RE: THE ESTATE OF DOE, JOHN' -- so the
    decedent is pulled out as the person of interest instead.
    """
    m = _IN_RE.match(style.strip())
    if m:
        name = re.sub(r",?\s*(deceased|a minor|an incapacitated person)\.?$", "",
                      m.group(1), flags=re.I).strip(" ,")
        return style.strip(), [name] if name else []
    parts = re.split(r"\s+v(?:s)?\.?\s+", style, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return style.strip(), []
    plaintiff, rest = parts[0].strip(), parts[1].strip()
    # Defendants are comma-separated 'LAST, FIRST' pairs; re-pair them.
    toks = [t.strip() for t in rest.split(",") if t.strip()]
    defendants, i = [], 0
    while i < len(toks):
        if i + 1 < len(toks) and not _ENTITY_HINTS.search(toks[i]) \
                and not _ENTITY_HINTS.search(toks[i + 1]) and toks[i].isupper():
            defendants.append(f"{toks[i]}, {toks[i + 1]}")
            i += 2
        else:
            defendants.append(toks[i])
            i += 1
    return plaintiff, defendants


def classify(number: str, style: str, filed: str | None, county: str | None) -> dict:
    prefix = number.split("-")[0].upper()
    kind = CASE_TYPES.get(prefix, "other")
    plaintiff, defendants = _split_style(style or "")

    category, why, score = "other", "", 0
    if prefix == "PB":
        category, why, score = "probate", "Estate being settled; heirs often sell.", 70
    elif prefix == "FD":
        category, why, score = "divorce", "Marital home commonly sold on a deadline.", 45
    elif prefix in ("CJ", "CV"):
        if _LENDER.search(plaintiff):
            category, why, score = "foreclosure", \
                f"Lender suit by {plaintiff}. Sheriff's sale typically 4-9 months after filing.", 90
        else:
            for pat, label in OTHER_CIVIL:
                if pat.search(plaintiff) or pat.search(style or ""):
                    category, why, score = label, f"{label.title()} action.", 40
                    break
    elif prefix == "TX":
        category, why, score = "tax suit", "County action over unpaid taxes.", 60

    people = [d for d in defendants if not _ENTITY_HINTS.search(d)]
    return {
        "number": number, "county": county, "filed": filed, "type": kind,
        "style": style, "plaintiff": plaintiff, "defendants": defendants,
        "people": people, "category": category, "why": why, "score": score,
        "case_url": case_url(county, number) if county else None,
    }


def parse_case(html: str) -> dict[str, Any]:
    """Parties, issues and docket from a saved case page, via the oscn parsers."""
    out: dict[str, Any] = {}
    try:
        from oscn import parse as P
        out["style"] = P.style(html)
        out["parties"] = P.parties(html)
        out["issues"] = P.issues(html)
        out["docket"] = P.docket(html)
    except Exception as exc:  # parser drift should not kill the request
        out["parse_error"] = f"{type(exc).__name__}: {exc}"

    text = _text(html)
    out["is_foreclosure"] = bool(re.search(r"\bforeclos", text, re.I))
    out["sheriff_sale"] = bool(re.search(r"sheriff'?s? sale|notice of sale", text, re.I))
    out["judgment"] = bool(re.search(r"journal entry of judgment|judgment", text, re.I))
    m = re.search(r"\b(\d{2,6}\s+(?:[NSEW]{1,2}\.?\s+)?[A-Z0-9][A-Za-z0-9 .'-]{2,40}?\s+"
                  r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Pl|Place|"
                  r"Blvd|Ter|Cir|Way|Hwy|Trl|Loop)\b\.?)", text)
    out["address_hint"] = m.group(1) if m else None
    return out


# --- cross-reference to the tax roll ---------------------------------------

def _name_parts(name: str) -> tuple[str, str]:
    """'DOE, JOHN A' -> ('DOE', 'JOHN')."""
    name = re.sub(r"\s+", " ", name).strip()
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
    else:
        toks = name.split()
        last, first = (toks[-1], " ".join(toks[:-1])) if len(toks) > 1 else (name, "")
    first = first.split()[0] if first else ""
    return last, first


def cross_reference(cases: list[dict], county: str, max_lookups: int = 60) -> list[dict]:
    """Attach tax-roll parcels to each case's individual defendants.

    Only categories worth chasing are looked up, highest score first, and the
    number of tax-roll calls is capped so a big paste stays responsive.
    """
    county = county.strip().lower()
    order = sorted(range(len(cases)), key=lambda i: -cases[i]["score"])
    lookups = 0
    for i in order:
        c = cases[i]
        c["parcels"] = []
        if c["score"] < 40 or not c["people"]:
            continue
        seen = set()
        for person in c["people"][:3]:
            last, first = _name_parts(person)
            if not last or (last, first) in seen or lookups >= max_lookups:
                continue
            seen.add((last, first))
            lookups += 1
            try:
                recs = taxroll.lookup_owner(county, last, first)
            except taxroll.TaxRollError:
                continue
            real = [r for r in recs if r["type"] == "Real Estate" and r["property_id"]]
            by_pid: dict[str, list] = {}
            for r in real:
                by_pid.setdefault(r["property_id"], []).append(r)
            for pid, rows in list(by_pid.items())[:4]:
                latest = max(rows, key=lambda r: r["year"] or 0)
                parcel = {
                    "property_id": pid, "owner": latest["owner"],
                    "unpaid_years": sorted({r["year"] for r in rows if not r["paid"]}),
                    "tax_owed": round(sum((r["total_due"] or 0) for r in rows if not r["paid"]), 2),
                    "detail_url": latest["detail_url"],
                    "matched_person": person,
                }
                try:
                    parcel.update(taxroll.parcel_detail(latest["detail_url"]))
                except taxroll.TaxRollError:
                    pass
                c["parcels"].append(parcel)
    return cases
