"""Regional deal search -- find flip candidates across a set of counties.

Running the full comp engine on every active listing in a region would take
hours, so this is a funnel:

  Stage 1  A fast vectorised screen over every active listing, using one sold
           pool per county. Estimates ARV from nearby same-size sales, applies
           the renovation and offer models, and ranks by the spread between
           the maximum allowable offer and the asking price.

  Stage 2  The full single-property analysis, run only on the survivors.

Stage 1 is deliberately approximate. Its job is to throw away the 95% of
listings that obviously do not work, cheaply enough to scan a whole region.
"""
from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

from . import offer, rehab, regions, states
from .data import _cached_scrape
from .geo import EARTH_RADIUS_MI

warnings.filterwarnings("ignore")

SOLD_LOOKBACK_DAYS = 365

# Stage-1 comp search, progressively relaxed.
SCREEN_TIERS = [(1.5, 0.30), (3.0, 0.40), (6.0, 0.50)]
SCREEN_MIN_COMPS = 4

DEFAULTS = {
    "min_price": 30_000,
    "max_price": 400_000,
    "min_sqft": 700,
    "max_sqft": 4_000,
    "min_spread": 0,        # MAO must beat asking by at least this much
    "max_results": 40,
}

RESIDENTIAL = {"SINGLE_FAMILY", "MULTI_FAMILY", "DUPLEX", "TRIPLEX",
               "TOWNHOMES", "CONDOS", "CONDO"}


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def fetch_region(counties: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pull active listings and the 12-month sold pool for each county."""
    act_frames, sold_frames = [], []
    for c in counties:
        q = c["query"]
        try:
            a = _cached_scrape(f"search_active|{q.lower()}",
                               location=q, listing_type="for_sale")
            if a is not None and not a.empty:
                a = a.copy()
                a["search_county"] = c["name"]
                act_frames.append(a)
        except Exception:
            pass
        try:
            s = _cached_scrape(f"search_sold|{q.lower()}|{SOLD_LOOKBACK_DAYS}",
                               location=q, listing_type="sold",
                               past_days=SOLD_LOOKBACK_DAYS)
            if s is not None and not s.empty:
                sold_frames.append(s)
        except Exception:
            pass

    if not act_frames:
        raise RuntimeError("No active listings returned for this region.")
    if not sold_frames:
        raise RuntimeError("No recent sales returned for this region.")

    active = pd.concat(act_frames, ignore_index=True)
    sold = pd.concat(sold_frames, ignore_index=True)
    for df in (active, sold):
        if "property_id" in df.columns:
            df.drop_duplicates(subset=["property_id"], inplace=True)
    return active.reset_index(drop=True), sold.reset_index(drop=True)


def _clean_sold(sold: pd.DataFrame) -> pd.DataFrame:
    df = sold.copy()
    price = _num(df, "sold_price")
    # Non-disclosure fallback, same rule the comp engine uses.
    price = price.fillna(_num(df, "last_sold_price")).fillna(_num(df, "list_price"))
    df["sale_price"] = price
    df["sqft"] = _num(df, "sqft")
    df["lat"] = _num(df, "latitude")
    df["lon"] = _num(df, "longitude")
    df["sold_date"] = pd.to_datetime(df.get("last_sold_date"), errors="coerce")
    if getattr(df["sold_date"].dtype, "tz", None) is not None:
        df["sold_date"] = df["sold_date"].dt.tz_localize(None)

    df = df[df["sale_price"].between(10_000, 5_000_000)]
    df = df[df["sqft"].between(400, 10_000)]
    df = df[df["lat"].notna() & df["lon"].notna()]
    if "style" in df.columns:
        df = df[df["style"].astype(str).str.upper().isin(RESIDENTIAL)]

    df["psf"] = df["sale_price"] / df["sqft"]
    lo, hi = df["psf"].quantile([0.05, 0.95])
    if pd.notna(lo) and pd.notna(hi) and hi > lo:
        df = df[df["psf"].between(lo, hi)]
    return df.reset_index(drop=True)


def _clean_active(active: pd.DataFrame, f: dict) -> pd.DataFrame:
    df = active.copy()
    df["price"] = _num(df, "list_price")
    df["sqft"] = _num(df, "sqft")
    df["lat"] = _num(df, "latitude")
    df["lon"] = _num(df, "longitude")
    df["year_built"] = _num(df, "year_built")
    df["days_on_mls"] = _num(df, "days_on_mls")

    df = df[df["price"].between(f["min_price"], f["max_price"])]
    df = df[df["sqft"].between(f["min_sqft"], f["max_sqft"])]
    df = df[df["lat"].notna() & df["lon"].notna()]
    if "style" in df.columns:
        df = df[df["style"].astype(str).str.upper().isin(RESIDENTIAL)]
    from . import prefs as _prefs
    p = _prefs.load()
    if p["exclude_cities"] and "city" in df.columns:
        df = df[~df["city"].astype(str).str.strip().str.lower().isin(p["exclude_cities"])]
    return df.reset_index(drop=True)


def _estimate_arv(lat: float, lon: float, sqft: float,
                  s_lat: np.ndarray, s_lon: np.ndarray,
                  s_sqft: np.ndarray, s_psf: np.ndarray) -> tuple[float, int, float]:
    """Median $/sqft of nearby same-size sales -> (arv, comp_count, radius)."""
    # Equirectangular approximation: accurate to well under a percent at these
    # distances and far cheaper than haversine across a whole county.
    latr = np.radians(lat)
    dx = (np.radians(s_lon - lon)) * np.cos(latr)
    dy = np.radians(s_lat - lat)
    dist = EARTH_RADIUS_MI * np.sqrt(dx * dx + dy * dy)

    for radius, tol in SCREEN_TIERS:
        m = (dist <= radius) & (np.abs(s_sqft - sqft) <= sqft * tol)
        n = int(m.sum())
        if n >= SCREEN_MIN_COMPS:
            return float(np.median(s_psf[m])) * sqft, n, radius
    return 0.0, 0, 0.0


def screen(counties: list[dict], filters: dict | None = None,
           progress=None) -> dict[str, Any]:
    """Stage 1: rank every active listing in the region by flip spread."""
    f = {**DEFAULTS, **(filters or {})}

    if progress:
        progress(f"Pulling listings for {len(counties)} count"
                 f"{'y' if len(counties) == 1 else 'ies'}...")
    active_raw, sold_raw = fetch_region(counties)

    sold = _clean_sold(sold_raw)
    active = _clean_active(active_raw, f)
    if progress:
        progress(f"{len(active):,} active listings screened against "
                 f"{len(sold):,} recent sales")

    s_lat = sold["lat"].to_numpy(float)
    s_lon = sold["lon"].to_numpy(float)
    s_sqft = sold["sqft"].to_numpy(float)
    s_psf = sold["psf"].to_numpy(float)

    market_psf = float(sold["psf"].median())
    rows: list[dict] = []

    for _, r in active.iterrows():
        sqft = float(r["sqft"])
        price = float(r["price"])
        arv, n, radius = _estimate_arv(float(r["lat"]), float(r["lon"]), sqft,
                                       s_lat, s_lon, s_sqft, s_psf)
        if n < SCREEN_MIN_COMPS or arv <= 0:
            continue

        local_psf = arv / sqft
        tier = rehab.suggest_tier(r.get("year_built"), price / sqft, local_psf)
        reno = rehab.estimate(sqft=sqft, tier=tier, market_psf=local_psf, arv=arv)

        loc = states.resolve(r.get("state"), r.get("city"), r.get("county"))
        params = {
            "transfer_tax_pct": loc["sell_transfer_pct"],
            "buy_transfer_tax_pct": loc["buy_transfer_pct"],
            "annual_taxes": float(r["tax"]) if pd.notna(r.get("tax"))
            else arv * loc["property_tax_pct"] / 100.0,
        }
        mao = offer.max_allowable_offer(arv, float(reno["total"]), params)
        deal = offer.evaluate(price, arv, float(reno["total"]), params)

        spread = mao - price
        rows.append({
            "address": r.get("formatted_address"),
            "url": r.get("property_url"),
            "city": r.get("city"),
            "county": r.get("county") or r.get("search_county"),
            "zip": r.get("zip_code"),
            "style": r.get("style"),
            "price": round(price),
            "sqft": round(sqft),
            "beds": r.get("beds"),
            "baths": r.get("full_baths"),
            "year_built": int(r["year_built"]) if pd.notna(r.get("year_built")) else None,
            "days_on_mls": int(r["days_on_mls"]) if pd.notna(r.get("days_on_mls")) else None,
            "est_arv": round(arv),
            "est_arv_psf": round(local_psf, 2),
            "ask_psf": round(price / sqft, 2),
            "ask_vs_market": round(price / arv * 100, 1),
            "screen_comps": n,
            "screen_radius_mi": radius,
            "rehab_tier": reno["tier_label"],
            "est_rehab": round(float(reno["total"])),
            "est_mao": round(mao),
            "spread": round(spread),
            "spread_pct": round(spread / price * 100, 1) if price else 0,
            "profit_at_asking": deal["profit"],
            "roi_at_asking": deal["roi_pct"],
        })

    rows.sort(key=lambda x: x["spread"], reverse=True)
    hits = [r for r in rows if r["spread"] >= f["min_spread"]]

    return {
        "counties": [c["name"] for c in counties],
        "active_screened": len(active),
        "sold_pool": len(sold),
        "market_psf": round(market_psf, 2),
        "candidates": hits[: f["max_results"]],
        "total_candidates": len(hits),
        "filters": f,
    }


def verify(candidates: list[dict], top: int = 10, progress=None) -> list[dict]:
    """Stage 2: run the full comp analysis on the strongest candidates."""
    from .analyze import analyze
    from .compengine import CompError
    from .data import DataError

    out = []
    for i, cand in enumerate(candidates[:top], 1):
        addr = cand.get("address")
        if not addr:
            continue
        if progress:
            progress(f"Verifying {i}/{min(top, len(candidates))}: {addr}")
        try:
            full = analyze(addr, asking_price=cand["price"])
        except (DataError, CompError) as exc:
            cand = {**cand, "verified": False, "verify_error": str(exc)}
            out.append(cand)
            continue
        except Exception as exc:  # pragma: no cover - defensive
            cand = {**cand, "verified": False, "verify_error": f"{type(exc).__name__}: {exc}"}
            out.append(cand)
            continue

        a, o = full["arv"], full["offer"]
        out.append({
            **cand,
            "verified": True,
            "arv": a["arv"],
            "arv_low": a["arv_low"],
            "arv_psf": a["arv_psf"],
            "confidence": a["confidence"],
            "confidence_label": a["confidence_label"],
            "comp_count": a["comp_count"],
            "arv_vs_comps_pct": a.get("arv_vs_comps_pct"),
            "rehab": full["rehab"]["total"],
            "rehab_tier_final": full["rehab"]["tier_label"],
            "mao": o["mao"] if o["feasible"] else 0,
            "feasible": o["feasible"],
            "verdict": full["verdict"]["call"],
            "verdict_detail": full["verdict"]["detail"],
            "profit_at_mao": o["at_mao"]["profit"],
            "downside": o["stress"]["arv_low"]["profit"],
            "flags": full["flags"],
        })
    return out


def find_deals(home: str = regions.DEFAULT_REGION,
               include_adjacent: bool = True,
               include_metro: bool = False,
               extra_counties: list[str] | None = None,
               filters: dict | None = None,
               verify_top: int = 10,
               progress=None) -> dict[str, Any]:
    """Full funnel: screen a region, then verify the best candidates."""
    counties = regions.resolve(home, include_adjacent, include_metro, extra_counties)
    result = screen(counties, filters, progress=progress)
    if verify_top:
        result["verified"] = verify(result["candidates"], verify_top, progress=progress)
    return result
