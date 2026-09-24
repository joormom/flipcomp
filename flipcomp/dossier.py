"""Property dossier -- everything public about one address, and how to reach
whoever owns it.

Built for the drive-by: a boarded house, no sign, no listing. What is it, who
holds it, are they paying, where does their mail go, and what has the county
recorded against it. Every fact carries its source so it can be checked.

Contact details come from public records only: the treasurer's mailing
address is the legally reliable way to reach an owner. Phone numbers and
emails are not public record; the dossier hands off to skip-trace services
with the name and city pre-filled rather than scraping them.
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import warnings
from typing import Any

from . import history, oscn_leads, regions, taxroll
from .data import _cached_scrape, geocode, row_to_dict

warnings.filterwarnings("ignore")

_ENTITY = re.compile(r"\b(llc|inc|corp|trust|bank|properties|holdings|company|co\b|"
                     r"partners|group|church|city of|county|estate of|ltd|lp\b)", re.I)


def _norm_owner(name: str) -> str:
    n = re.sub(r"\b\d+/\d+\s*INT\b", "", name or "", flags=re.I)  # '1/2 INT'
    n = re.sub(r"[:;]", " & ", n)
    return re.sub(r"\s+", " ", n).strip(" ,&")


def _split_people(owner: str) -> list[dict]:
    """'ASKINS, BILL C & JEANNETTE' -> two people sharing a surname."""
    owner = _norm_owner(owner)
    if not owner or _ENTITY.search(owner):
        return [{"name": owner, "entity": True}] if owner else []
    if "," not in owner:
        return [{"name": owner, "entity": False, "last": owner.split()[-1] if owner.split() else owner,
                 "first": " ".join(owner.split()[:-1])}]
    last, rest = [p.strip() for p in owner.split(",", 1)]
    people = []
    for chunk in re.split(r"\s*&\s*|\s+and\s+", rest, flags=re.I):
        chunk = chunk.strip()
        if not chunk:
            continue
        # A chunk with its own comma is a different surname: 'DOE, JOHN & SMITH, JANE'.
        if "," in chunk:
            l2, f2 = [p.strip() for p in chunk.split(",", 1)]
            people.append({"name": f"{f2} {l2}".strip(), "first": f2.split()[0] if f2 else "",
                           "last": l2, "entity": False})
        else:
            people.append({"name": f"{chunk} {last}".strip(), "first": chunk.split()[0],
                           "last": last, "entity": False})
    return people


def _ownership_timeline(rows: list[dict]) -> list[dict]:
    """Owner name per tax year, collapsed into runs; changes are the story."""
    real = sorted((r for r in rows if r["type"] == "Real Estate" and r["year"]),
                  key=lambda r: r["year"])
    runs: list[dict] = []
    for r in real:
        name = r["owner"]
        if runs and runs[-1]["owner"] == name:
            runs[-1]["to"] = r["year"]
        else:
            runs.append({"owner": name, "from": r["year"], "to": r["year"]})
    return runs


def _links(address: str, city: str | None, county_key: str, people: list[dict],
           owner: str | None, legal: str | None) -> list[dict]:
    q = urllib.parse.quote_plus
    full = f"{address}, {city or ''} OK".strip()
    out = [
        {"label": "Street View / map", "url": f"https://www.google.com/maps/search/{q(full)}",
         "why": "Confirm the condition and the neighbours."},
        {"label": "County assessor record",
         "url": f"https://app.datacrosspoint.com/properties/{county_key}",
         "why": "Square footage, year built, sale history, photos of the record card."},
        {"label": "County clerk deeds, mortgages, liens",
         "url": f"https://okcountyrecords.com/search/{county_key}",
         "why": "Search the owner's last name as grantee to see how they took title, and "
                "as grantor/mortgagor for any mortgage or lien still open. Free to browse."},
    ]
    if people:
        p = people[0]
        if not p.get("entity"):
            out.append({"label": f"Court records for {p['last'].title()} (foreclosure, probate, divorce)",
                        "url": oscn_leads.search_url(county_key, dt.date.today() - dt.timedelta(days=5 * 365),
                                                     last_name=p["last"]),
                        "why": "A probate under this surname would explain a boarded house; a "
                               "foreclosure means a lender is the real counterparty."})
    for p in people[:3]:
        if p.get("entity"):
            out.append({"label": f"Oklahoma SOS lookup: {p['name']}",
                        "url": "https://www.sos.ok.gov/corp/corpInquiryFind.aspx",
                        "why": "Registered agent and officers of the entity that holds title."})
            continue
        nm = q(p["name"])
        loc = q(f"{city or 'Bartlesville'}, OK")
        out.append({"label": f"Skip-trace {p['name'].title()}",
                    "url": f"https://www.truepeoplesearch.com/results?name={nm}&citystatezip={loc}",
                    "why": "Phone numbers and relatives are not public record; this is the "
                           "usual free source. Cross-check with a second site before relying on it.",
                    "alt": f"https://www.fastpeoplesearch.com/name/{q(p['name'].lower().replace(' ', '-'))}_{q((city or 'bartlesville').lower())}-ok"})
    return out


def _situation(tax: dict, parcel: dict, listing: dict | None, timeline: list[dict],
               people: list[dict]) -> dict:
    """Read the facts the way an experienced buyer would."""
    notes, approach = [], []
    behind = tax.get("years_behind") or 0
    liens = tax.get("special_assessments_owed") or 0
    absentee = parcel.get("owner_occupied_guess") is False
    entity = any(p.get("entity") for p in people)

    if behind >= 3:
        notes.append(f"{behind} years of unpaid tax: the county can sell it at the June resale. "
                     "The owner is close to losing it for nothing.")
        approach.append("Lead with the tax problem: an offer that clears the county and leaves "
                        "them something beats the resale, where they get nothing.")
    elif behind:
        notes.append(f"{behind} year(s) of tax unpaid (${tax.get('tax_owed', 0):,.0f}).")
        approach.append("Mention you can close fast enough to stop the penalties.")
    else:
        notes.append("Taxes are current. Someone is deliberately keeping this parcel; it is "
                     "not drifting toward the county.")
    if liens:
        notes.append(f"${liens:,.0f} in city special assessments: code enforcement has already "
                     "cited it (mowing, clean-up or boarding). More citations are coming.")
        approach.append("The city is a pressure point - each citation costs the owner money "
                        "and time, and they may not know a buyer would take it as-is.")
    if absentee:
        notes.append("Absentee: the tax bill goes to a different address, so the owner does not "
                     "live there and may rarely see it.")
        approach.append("Write to the mailing address, then knock on it if it is local. A "
                        "handwritten note beats a printed mailer for this profile.")
    if len(timeline) >= 2:
        last = timeline[-1]
        prev = timeline[-2]
        notes.append(f"Ownership changed on the roll in {last['from']} (was '{prev['owner']}', "
                     f"now '{last['owner']}').")
        if "INT" in (prev["owner"] or "").upper() or "INT" in (last["owner"] or "").upper():
            notes.append("A fractional interest ('1/2 INT') was consolidated. That usually means "
                         "a co-owner died or was bought out - check probate under the surname.")
            approach.append("If it went through probate, the heir who ended up with it often "
                            "wants out and has no attachment to the house.")
    if entity:
        notes.append("Title is held by an entity. Find the registered agent via the Secretary "
                     "of State; the agent forwards mail to the principal.")
    if listing:
        st = (listing.get("status") or "").upper()
        if st in ("FOR_SALE", "PENDING", "CONTINGENT"):
            notes.append(f"Currently listed ({st.lower()}) at ${listing.get('list_price') or 0:,.0f}. "
                         "Go through the agent.")
            approach = ["Contact the listing agent; the owner is represented."]
        elif listing.get("last_sold_date"):
            notes.append(f"Last sale on record {listing['last_sold_date'][:10]}"
                         + (f" for ${listing['last_sold_price']:,.0f}" if listing.get("last_sold_price") else "")
                         + ".")
    else:
        notes.append("No MLS history at all. It has not been offered publicly in the data's memory.")
    if not approach:
        approach.append("Send a short letter to the mailing address stating you are a local "
                        "buyer, will take it as-is, and can close in weeks. Follow up in 10 days.")
    approach.append("Before offering: pull the deed from the county clerk to confirm who "
                    "actually holds title and whether a mortgage or lien is still open.")
    return {"notes": notes, "approach": approach}


def build(address: str, county_key: str | None = None) -> dict[str, Any]:
    """Assemble the dossier for one address."""
    address = address.strip()
    geo = geocode(address)
    city = (geo or {}).get("city")
    zip_code = (geo or {}).get("zip_code")
    if not county_key:
        county_key = regions.DEFAULT_REGION
        for key, rec in regions.COUNTIES.items():
            if city and city.lower() == rec["seat"].lower():
                county_key = key
                break

    out: dict[str, Any] = {
        "address": address,
        "normalised": (geo or {}).get("formatted_address"),
        "city": city, "zip": zip_code, "county_key": county_key,
        "county": regions.COUNTIES.get(county_key, {}).get("name"),
        "latitude": (geo or {}).get("latitude"), "longitude": (geo or {}).get("longitude"),
        "sources": [],
    }

    # --- treasurer: owner, mailing address, delinquency, ownership timeline ---
    tax = {"found": False}
    parcel: dict = {}
    rows: list[dict] = []
    parts = taxroll.split_street(address)
    if parts:
        try:
            rows = taxroll.lookup_address(county_key, *parts)
            tax = taxroll.delinquency_for_address(county_key, address)
            if tax.get("found") and tax.get("detail_url"):
                parcel = taxroll.parcel_detail(tax["detail_url"])
            out["sources"].append({"name": "County treasurer tax roll", "ok": bool(rows)})
        except taxroll.TaxRollError as exc:
            out["sources"].append({"name": "County treasurer tax roll", "ok": False, "error": str(exc)})
    timeline = _ownership_timeline(rows)
    owner = tax.get("owner")
    people = _split_people(owner) if owner else []

    # --- Realtor.com: listing history and physical details -----------------
    listing = None
    try:
        df = _cached_scrape(f"dossier|{address.lower()}", location=address, listing_type=None)
        if df is not None and not df.empty:
            from .data import address_matches
            for _, cand in df.iterrows():
                if address_matches(address, cand.get("formatted_address")):
                    listing = row_to_dict(cand)
                    break
        out["sources"].append({"name": "Realtor.com history", "ok": listing is not None})
    except Exception as exc:
        out["sources"].append({"name": "Realtor.com history", "ok": False, "error": str(exc)})

    # --- our own history: has this app seen it? ----------------------------
    seen = []
    try:
        store = history.load()
        from .leads import _addr_key
        key = _addr_key(address)
        for lid, rec in store["items"].items():
            snap = rec["snap"]
            if key and (snap.get("address") and _addr_key(snap["address"]) == key):
                seen.append({"source": lid.split("|")[0], "kind": snap.get("kind"),
                             "first_seen": rec["first_seen"], "last_seen": rec["last_seen"],
                             "score": snap.get("score"), "tags": snap.get("tags")})
    except Exception:
        pass

    # The Census geocoder drops compass suffixes ('SW' -> 'S'); the treasurer's
    # spelling is the legal one, so prefer it for display when we have it.
    if tax.get("found") and tax.get("address_on_roll"):
        out["normalised"] = f"{tax['address_on_roll']}, {(city or '').upper()}, OK {zip_code or ''}".strip()

    mailing = parcel.get("mailing_address")
    mailing_clean = None
    if mailing and owner:
        # The roll prefixes the mailing line with the owner name; strip it.
        mailing_clean = mailing.replace(owner, "", 1).strip(" ,") or mailing

    out.update({
        "owner": owner,
        "people": people,
        "mailing_address": mailing_clean or mailing,
        "owner_occupied": parcel.get("owner_occupied_guess"),
        "tax": {k: v for k, v in tax.items() if k != "detail_url"},
        "tax_roll_url": tax.get("detail_url"),
        "parcel": {
            "legal": parcel.get("legal"),
            "assessed_land": parcel.get("assessed_land"),
            "assessed_improvements": parcel.get("assessed_improvements"),
            "net_assessed": parcel.get("net_assessed"),
            "implied_market_value": parcel.get("implied_market_value"),
            "assessment_ratio": parcel.get("assessment_ratio"),
            "land_only": (parcel.get("assessed_improvements") or 0) == 0 if parcel else None,
        },
        "ownership_timeline": timeline,
        "listing": listing and {k: listing.get(k) for k in (
            "formatted_address", "status", "mls_status", "list_price", "list_date",
            "last_status_change_date", "sold_price", "last_sold_date", "last_sold_price",
            "sqft", "beds", "full_baths", "half_baths", "year_built", "lot_sqft",
            "estimated_value", "tax", "agent_name", "agent_phones", "agent_email",
            "office_name", "office_phones", "broker_name", "property_url", "days_on_mls")},
        "seen_by_app": seen,
        "links": _links(address, city, county_key, people, owner, parcel.get("legal")),
        "situation": _situation(tax, parcel, listing, timeline, people),
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
    })
    return out
