"""Property data access layer.

Sources Realtor.com MLS data via the HomeHarvest scraper. Results are cached on
disk so repeated analyses in the same market don't re-hit the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import warnings
from typing import Any

import pandas as pd

warnings.filterwarnings("ignore")

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours - sold data moves slowly

# Columns we care about, normalised to plain Python types.
NUMERIC_COLS = [
    "beds", "full_baths", "half_baths", "sqft", "year_built", "lot_sqft",
    "sold_price", "list_price", "last_sold_price", "latitude", "longitude",
    "stories", "parking_garage", "tax", "assessed_value", "estimated_value",
    "days_on_mls", "hoa_fee",
]


class DataError(RuntimeError):
    """Raised when property data cannot be retrieved."""


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, f"{digest}.parquet")


def _cache_get(key: str) -> pd.DataFrame | None:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > CACHE_TTL_SECONDS:
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def _cache_put(key: str, df: pd.DataFrame) -> None:
    try:
        df.to_pickle(_cache_path(key))
    except Exception:
        pass  # cache is best-effort


def _scrape(**kwargs: Any) -> pd.DataFrame:
    from homeharvest import scrape_property
    return scrape_property(**kwargs)


def _cached_scrape(cache_key: str, **kwargs: Any) -> pd.DataFrame:
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    df = _scrape(**kwargs)
    if df is None:
        df = pd.DataFrame()
    _cache_put(cache_key, df)
    return df


def _num(val: Any) -> float | None:
    """Coerce pandas nullable/NaN scalars to float or None."""
    if val is None or val is pd.NA:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _text(val: Any) -> str | None:
    if val is None or val is pd.NA:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s or None


def row_to_dict(row: pd.Series) -> dict[str, Any]:
    """Normalise a HomeHarvest row into a plain dict."""
    out: dict[str, Any] = {}
    for col in row.index:
        if col in ("alt_photos", "tax_history", "nearby_schools", "text"):
            continue
        val = row[col]
        out[col] = _num(val) if col in NUMERIC_COLS else _text(val)
    sold = row.get("last_sold_date")
    out["sold_date"] = None
    try:
        if sold is not None and not pd.isna(sold):
            out["sold_date"] = pd.Timestamp(sold).strftime("%Y-%m-%d")
    except Exception:
        pass
    return out


_ADDR_SPLIT = re.compile(r"[^a-z0-9]+")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")

# Street types normalised to one spelling so "Avenue" matches "Ave". These are
# compared, not discarded: NW 73rd St and NW 73rd Ter are different streets.
_STREET_TYPES = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave",
    "road": "rd", "rd": "rd", "drive": "dr", "dr": "dr", "lane": "ln",
    "ln": "ln", "court": "ct", "ct": "ct", "place": "pl", "pl": "pl",
    "boulevard": "blvd", "blvd": "blvd", "terrace": "ter", "ter": "ter",
    "circle": "cir", "cir": "cir", "way": "way", "highway": "hwy",
    "hwy": "hwy", "parkway": "pkwy", "pkwy": "pkwy", "trail": "trl",
    "trl": "trl", "loop": "loop", "run": "run", "path": "path",
}
_DIRECTIONALS = {
    "n": "n", "north": "n", "s": "s", "south": "s", "e": "e", "east": "e",
    "w": "w", "west": "w", "ne": "ne", "northeast": "ne", "nw": "nw",
    "northwest": "nw", "se": "se", "southeast": "se", "sw": "sw",
    "southwest": "sw",
}
_UNIT_WORDS = {"unit", "apt", "apartment", "ste", "suite", "lot", "usa"}

# USPS abbreviations for words that appear in street NAMES rather than as the
# street type. Listing feeds use these freely ("Oak Knl" for "Oak Knoll"), and
# so do people typing an address in.
_NAME_ABBREV = {
    "knl": "knoll", "knls": "knoll", "crk": "creek", "ck": "creek",
    "hts": "heights", "ht": "heights", "vw": "view", "vws": "view",
    "mnr": "manor", "xing": "crossing", "crsg": "crossing",
    "spg": "spring", "spgs": "spring", "springs": "spring",
    "vlg": "village", "vlgs": "village", "mdw": "meadow", "mdws": "meadow",
    "meadows": "meadow", "rdg": "ridge", "rdgs": "ridge",
    "frst": "forest", "frg": "forge", "lk": "lake", "lks": "lake",
    "lakes": "lake", "pk": "park", "grn": "green", "grns": "green",
    "hl": "hill", "hls": "hill", "hills": "hill", "vly": "valley",
    "vlys": "valley", "gdn": "garden", "gdns": "garden", "gardens": "garden",
    "est": "estate", "ests": "estate", "estates": "estate",
    "clb": "club", "cyn": "canyon", "mtn": "mountain", "mt": "mountain",
    "orch": "orchard", "shr": "shore", "shrs": "shore", "shores": "shore",
    "brk": "brook", "brks": "brook", "bnd": "bend", "blf": "bluff",
    "cv": "cove", "hbr": "harbor", "isl": "island", "jct": "junction",
    "ldg": "lodge", "pnes": "pines", "plz": "plaza", "pt": "point",
    "pts": "point", "rnch": "ranch", "st.": "saint", "ste.": "saint",
    "trc": "trace", "vis": "vista", "wds": "woods", "woods": "woods",
}


def _skeleton(token: str) -> str:
    """First letter plus consonants, repeats collapsed.

    Catches squeeze-abbreviations not in the table: 'knoll' and 'knl' both
    reduce to 'knl'.
    """
    if not token:
        return token
    out = [token[0]]
    for ch in token[1:]:
        if ch not in "aeiou" and (not out or out[-1] != ch):
            out.append(ch)
    return "".join(out)


def _normalise_token(tok: str) -> str:
    """Canonicalise one address token.

    Ordinal suffixes are stripped so a numbered rural road written '4100th Rd'
    matches the same road recorded as '4100 Rd' -- a very common difference in
    rural Oklahoma, where roads are numbered rather than named.
    """
    m = _ORDINAL.match(tok)
    if m:
        return m.group(1)
    if tok in _STREET_TYPES:
        return _STREET_TYPES[tok]
    if tok in _DIRECTIONALS:
        return _DIRECTIONALS[tok]
    return _NAME_ABBREV.get(tok, tok)


def _street_part(value: str | None) -> str:
    """The street line only -- everything before the first comma.

    City and state tokens must be excluded from the comparison: they match for
    every property in the same town, which would let '73rd St' pass as '74th St'.
    """
    text = (value or "").strip()
    return text.split(",")[0] if "," in text else text


def _addr_tokens(value: str | None) -> list[str]:
    toks = [t for t in _ADDR_SPLIT.split((value or "").lower()) if t]
    return [_normalise_token(t) for t in toks if t not in _UNIT_WORDS]


def address_matches(query: str, candidate: str | None) -> bool:
    """Does `candidate` actually refer to the address the user asked for?

    A fuzzy Realtor.com search happily returns hundreds of nearby homes, so a
    record is only accepted when it genuinely names the same place.

    The candidate's street line is authoritative and short, so the test is that
    every meaningful part of it appears in what the user typed. That tolerates
    the user adding city, state and ZIP (or omitting a directional) while still
    rejecting a different street or a different house number.
    """
    # The user may type the whole address without commas, so keep every token
    # from the query; extra city/state tokens are harmless on this side.
    qt = _addr_tokens(query)
    ct = _addr_tokens(_street_part(candidate))
    if not qt or not ct:
        return False

    # First token of a street line is the house number.
    if not ct[0].isdigit() or not qt[0].isdigit():
        return False
    if qt[0] != ct[0]:
        return False

    q_rest, c_rest = set(qt[1:]), set(ct[1:])
    dirs, types = set(_DIRECTIONALS.values()), set(_STREET_TYPES.values())

    # Directionals and street types: an omitted one is forgivable, because
    # people leave them off constantly. A contradictory one is not -- NW 73rd St
    # and NW 73rd Ter are different streets.
    for group in (dirs, types):
        q_has, c_has = q_rest & group, c_rest & group
        if q_has and c_has and q_has != c_has:
            return False

    # Every distinctive word the candidate uses must be present in the query.
    required = c_rest - dirs - types
    if not required:
        return True
    return all(_token_present(tok, q_rest) for tok in required)


def _token_present(tok: str, q_rest: set[str]) -> bool:
    """Is `tok` named in the query, allowing for abbreviation?"""
    if tok in q_rest:
        return True
    # A numbered street must match exactly -- 4100 Rd is not 4200 Rd.
    if tok.isdigit():
        return False
    for q in q_rest:
        if q.isdigit():
            continue
        # Either side may be the abbreviated one: listing feeds shorten names
        # just as often as people do.
        short, long = (q, tok) if len(q) <= len(tok) else (tok, q)
        # Squeeze-abbreviation: 'knl' for 'knoll'.
        if len(long) >= 4 and 3 <= len(short) < len(long) \
                and _skeleton(long) == _skeleton(short):
            return True
        # Truncation: 'wash' for 'washington'. Long enough to stay safe.
        if len(long) >= 5 and len(short) >= 4 and long.startswith(short):
            return True
    return False


def lookup_subject(address: str) -> dict[str, Any]:
    """Resolve a single street address to its property record.

    Tries each listing status; Realtor.com returns the property regardless of
    whether it is currently listed, sold, or pending. Every candidate is
    address-verified before being accepted.
    """
    address = address.strip()
    if not address:
        raise DataError("No address supplied.")

    best: pd.Series | None = None
    saw_results = False

    for listing_type in ("for_sale", "sold", "pending", "for_rent"):
        try:
            df = _cached_scrape(
                f"subject|{address.lower()}|{listing_type}",
                location=address, listing_type=listing_type,
            )
        except Exception:
            continue
        if df is None or df.empty:
            continue
        saw_results = True

        # Scan every row rather than trusting the first: a broad result set
        # often still contains the right house further down.
        for _, cand in df.iterrows():
            if not address_matches(address, cand.get("formatted_address")):
                continue
            if best is None or (_num(cand.get("sqft")) or 0) > (_num(best.get("sqft")) or 0):
                best = cand
        if best is not None and _num(best.get("sqft")):
            break

    if best is None:
        hint = (" The search returned other properties but none at that street "
                "number, so it was not analysed rather than risk reporting on "
                "the wrong house." if saw_results else "")
        raise DataError(
            f"Could not find '{address}' on Realtor.com.{hint} Check the "
            "spelling and include city and state, e.g. "
            "'123 Main St, Oklahoma City, OK'."
        )
    return row_to_dict(best)


def fetch_sold_pool(subject: dict[str, Any], past_days: int = 365,
                    radius_mi: float = 3.0) -> pd.DataFrame:
    """Fetch the pool of recently sold properties around the subject."""
    addr = subject.get("formatted_address") or ""
    city, state = subject.get("city"), subject.get("state")

    frames: list[pd.DataFrame] = []

    if addr:
        try:
            df = _cached_scrape(
                f"pool_radius|{addr.lower()}|{past_days}|{radius_mi}",
                location=addr, listing_type="sold",
                past_days=past_days, radius=radius_mi,
            )
            if df is not None and not df.empty:
                frames.append(df)
        except Exception:
            pass

    # City-wide pull as a safety net so thin radius results still get comps.
    if city and state and (not frames or len(frames[0]) < 40):
        try:
            df = _cached_scrape(
                f"pool_city|{city.lower()}|{state}|{past_days}",
                location=f"{city}, {state}", listing_type="sold",
                past_days=past_days,
            )
            if df is not None and not df.empty:
                frames.append(df)
        except Exception:
            pass

    if not frames:
        raise DataError("No recent sales data returned for this market.")

    pool = pd.concat(frames, ignore_index=True)
    if "property_id" in pool.columns:
        pool = pool.drop_duplicates(subset=["property_id"])
    return pool.reset_index(drop=True)


# --- geocoding for addresses Realtor.com has never listed -------------------

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def geocode(address: str) -> dict[str, Any] | None:
    """US Census geocoder: free, keyless, and good enough to place a parcel.

    Returns latitude/longitude plus the normalised city, state and ZIP, or
    None when the address does not resolve.
    """
    import requests

    key = f"geocode|{address.strip().lower()}"
    hit = _cache_get(key)
    if hit is not None and not hit.empty:
        return hit.iloc[0].to_dict()
    try:
        r = requests.get(GEOCODER_URL, params={
            "address": address, "benchmark": "Public_AR_Current", "format": "json",
        }, timeout=30)
        r.raise_for_status()
        matches = (r.json().get("result") or {}).get("addressMatches") or []
    except Exception:
        return None
    if not matches:
        return None
    m = matches[0]
    comp = m.get("addressComponents") or {}
    out = {
        "formatted_address": m.get("matchedAddress"),
        "latitude": float(m["coordinates"]["y"]),
        "longitude": float(m["coordinates"]["x"]),
        "city": (comp.get("city") or "").title() or None,
        "state": comp.get("state"),
        "zip_code": comp.get("zip"),
        "street": " ".join(x for x in (comp.get("fromAddress") or comp.get("toAddress") or "",
                                       comp.get("preDirection"), comp.get("streetName"),
                                       comp.get("suffixType")) if x).strip() or None,
    }
    try:
        _cache_put(key, pd.DataFrame([out]))
    except Exception:
        pass
    return out
