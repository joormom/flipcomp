"""Comparable selection and adjustment -> ARV (After Repair Value).

Follows the sales-comparison approach an appraiser uses: pick similar recent
sales, adjust each one for how it differs from the subject, then reconcile the
adjusted values into a single figure using distance/recency/similarity weights.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np
import pandas as pd

from . import condition as cond
from .data import row_to_dict
from .geo import haversine_mi

# --- Tunable adjustment parameters -----------------------------------------
# Marginal value of extra square footage, as a fraction of the market's median
# price-per-sqft. A 2,000 sqft house is not worth twice a 1,000 sqft house on
# the same street, so the marginal rate sits well below the average rate.
SQFT_MARGINAL_FACTOR = 0.30
# Bedroom and bathroom counts correlate strongly with living area, so adjusting
# generously for all three double-counts the same difference and pushes every
# adjusted value in the same direction. These rates are deliberately modest;
# square footage carries most of the size signal.
BED_VALUE_PCT = 0.015       # per bedroom, share of median comp price
FULL_BATH_PCT = 0.025       # per full bath
HALF_BATH_PCT = 0.012       # per half bath
AGE_RATE_PER_YEAR = 0.0025  # effective-age depreciation per year of difference
AGE_ADJ_CAP = 0.15          # cap age adjustment at +/-15% of comp price
# Lot size is the least reliable field in listing data and the marginal value
# of extra land on an ordinary residential parcel is small -- a rowhome with a
# slightly deeper yard is not worth meaningfully more. Kept deliberately weak.
LOT_MARGINAL_FACTOR = 0.03  # land value as a share of median $/sqft
LOT_ADJ_CAP = 0.06
GARAGE_VALUE_PCT = 0.015    # per garage bay
MAX_BED_DELTA = 2
MAX_BATH_DELTA = 2

# Gross adjustment above this fraction means the comp is a stretch.
GROSS_ADJ_WARN = 0.25

# Appraisal practice treats a comp needing more than ~15% net adjustment as
# weak evidence. Capping the net keeps a single over-adjusted comp from
# dragging the whole reconciliation upward.
NET_ADJ_CAP = 0.15

# Selection tolerances, progressively relaxed until enough comps are found.
# (radius_mi, sqft_tolerance, bed_delta, max_months_old)
SEARCH_TIERS = [
    (0.50, 0.20, 1, 6),
    (1.00, 0.25, 1, 9),
    (1.50, 0.30, 1, 12),
    (2.50, 0.35, 2, 12),
    (4.00, 0.45, 2, 18),
]
TARGET_COMPS = 8

# Weighting and reconciliation. Set by backtest; see backtest.py.
DIST_FALLOFF_MI = 0.5
HALF_LIFE_MONTHS = 12.0
ADJ_PENALTY = 3.0
MAX_USED = 12
ARV_QUANTILE = 0.50
TREND_METHOD = "r2"     # see estimate_market_trend

# What a confidence score means in practice: share of renovated sales whose
# ARV landed within 10% / 20% of the real price, from a time-ordered backtest
# of 1,658 sales across Washington, Rogers, Osage and the Owasso and
# Collinsville school districts (Oct 2026). (low, high, within10, within20)
CONFIDENCE_CALIBRATION = [
    (0, 50, 31.8, 45.5),
    (50, 60, 40.3, 70.1),
    (60, 70, 44.4, 75.5),
    (70, 80, 57.8, 86.6),
    (80, 90, 80.6, 96.5),
    (90, 101, 97.1, 100.0),
]

# Month-of-sale effect on renovated sales, % vs the annual average. Empty =
# off. Measured from three years of Washington, Rogers and Osage sales:
# spring and early summer run ~4% above winter. See backtest.py.
SEASONAL: dict[int, float] = {}
MIN_COMPS = 3

RESIDENTIAL_STYLES = {
    "SINGLE_FAMILY", "MULTI_FAMILY", "CONDOS", "CONDO", "TOWNHOMES",
    "TOWNHOUSE", "DUPLEX", "TRIPLEX", "APARTMENT", "COOP", "MOBILE",
}
STYLE_GROUPS = {
    "SINGLE_FAMILY": {"SINGLE_FAMILY"},
    "MULTI_FAMILY": {"MULTI_FAMILY", "DUPLEX", "TRIPLEX", "APARTMENT"},
    "CONDOS": {"CONDOS", "CONDO", "COOP"},
    "TOWNHOMES": {"TOWNHOMES", "TOWNHOUSE", "CONDOS", "CONDO"},
}


class CompError(RuntimeError):
    """Raised when a defensible ARV cannot be produced."""


def _style_group(style):
    if not style:
        return set()
    return STYLE_GROUPS.get(style.upper(), {style.upper()})


def _months_between(sold, today) -> float:
    return max(0.0, (today - sold).days / 30.44)


def drop_bulk_sales(df: pd.DataFrame, price_col: str = "sale_price",
                    date_col: str = "sold_date") -> pd.DataFrame:
    """Remove portfolio sales: many houses recorded at one total price.

    When an investor buys a bundle, the whole package price is often stamped
    on every parcel, so a $40k house shows a $790,000 'sale'. Around 10% of
    recorded sales in older Bartlesville neighbourhoods are these. Three or
    more different properties at the identical price on the identical day is
    a bundle; a pair is treated as one only when the price is far above the
    local norm, since two genuine sales can coincide.
    """
    if df.empty or price_col not in df.columns or date_col not in df.columns:
        return df
    day = pd.to_datetime(df[date_col], errors="coerce").dt.date
    key = df[price_col].astype(float).round(0).astype(str) + "|" + day.astype(str)
    size = key.map(key.value_counts())
    median_price = float(df[price_col].median()) if len(df) else 0.0
    bulk = (size >= 3) | ((size == 2) & (df[price_col] >= 2.0 * median_price))
    return df[~bulk.fillna(False)]


def prepare_pool(pool: pd.DataFrame, subject: dict, as_of=None) -> pd.DataFrame:
    """Clean the raw sold pool down to usable, comparable residential sales."""
    df = pool.copy()

    for col in ("sold_price", "sqft", "beds", "full_baths", "half_baths",
                "year_built", "lot_sqft", "latitude", "longitude", "parking_garage"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "list_price" not in df.columns:
        df["list_price"] = np.nan
    df["list_price"] = pd.to_numeric(df["list_price"], errors="coerce")

    df["sold_date"] = pd.to_datetime(df.get("last_sold_date"), errors="coerce")
    if getattr(df["sold_date"].dtype, "tz", None) is not None:
        df["sold_date"] = df["sold_date"].dt.tz_localize(None)

    # Roughly a dozen states are non-disclosure -- Texas, Missouri, Kansas,
    # Utah, Idaho, Louisiana, Mississippi, Montana, New Mexico, North Dakota,
    # Wyoming, Alaska -- meaning sale prices are not public record and the MLS
    # feed carries no sold price at all. Falling back to the price the property
    # was listed at when it sold keeps those markets usable; homes generally
    # transact within a few percent of final list. It is a proxy, so it is
    # tracked and surfaced rather than quietly substituted.
    df["sale_price"] = df["sold_price"]
    for alt in ("last_sold_price", "list_price"):
        if alt in df.columns:
            gap = df["sale_price"].isna()
            df.loc[gap, "sale_price"] = pd.to_numeric(df.loc[gap, alt], errors="coerce")
    df["price_is_proxy"] = df["sold_price"].isna() & df["sale_price"].notna()

    # Hard validity filters.
    df = df[df["sale_price"].between(10_000, 20_000_000)]
    df = df[df["sqft"].between(300, 20_000)]
    df = df[df["latitude"].notna() & df["longitude"].notna()]
    df = df[df["sold_date"].notna()]

    # Residential only - drop land, commercial, farms.
    if "style" in df.columns:
        df = df[df["style"].astype(str).str.upper().isin(RESIDENTIAL_STYLES)]

    # Match the subject's property class where we know it.
    group = _style_group(subject.get("style"))
    if group and "style" in df.columns:
        df = df[df["style"].astype(str).str.upper().isin(group)]

    # Drop the subject itself if it appears in its own comp pool.
    subj_id = subject.get("property_id")
    if subj_id and "property_id" in df.columns:
        df = df[df["property_id"].astype(str) != str(subj_id)]

    if df.empty:
        raise CompError("No valid residential sales in this market after cleaning.")

    df = drop_bulk_sales(df)
    if df.empty:
        raise CompError("No valid residential sales in this market after cleaning.")

    df["psf"] = df["sale_price"] / df["sqft"]
    if "condition" not in df.columns:  # a caller may have classified the pool already
        df["condition"] = (df["text"] if "text" in df.columns else pd.Series("", index=df.index))             .apply(cond.classify)

    # Trim price-per-sqft outliers (teardowns, estate sales, intra-family deeds).
    lo, hi = df["psf"].quantile([0.05, 0.95])
    if pd.notna(lo) and pd.notna(hi) and hi > lo:
        df = df[df["psf"].between(lo, hi)]

    # as_of lets a backtest value a house on a past date using only the sales
    # that had happened by then -- anything later would be seeing the future.
    today = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(dt.date.today())
    df = df[df["sold_date"] < today]
    df["months_ago"] = df["sold_date"].apply(lambda d: _months_between(d, today))
    df = df[df["months_ago"] <= 24]
    if df.empty:
        raise CompError("No sales before the valuation date.")

    # Vectorised haversine: identical result, two orders of magnitude faster
    # than a row-wise apply, which matters once a backtest runs it thousands of times.
    lat1, lon1 = np.radians(float(subject["latitude"])), np.radians(float(subject["longitude"]))
    lat2, lon2 = np.radians(df["latitude"].to_numpy(float)), np.radians(df["longitude"].to_numpy(float))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    df["distance_mi"] = 2 * 3958.7613 * np.arcsin(np.sqrt(a))
    return df.reset_index(drop=True)


def estimate_market_trend(df: pd.DataFrame) -> float:
    """Monthly appreciation rate implied by the local pool.

    Regresses log(price per sqft) on months-ago. Returns a monthly rate,
    clamped so a noisy pool cannot produce absurd time adjustments.
    """
    sub = df[(df["months_ago"] <= 24) & (df["psf"] > 0)]
    if len(sub) < 25:
        return 0.0
    x = sub["months_ago"].to_numpy(dtype=float)
    y = np.log(sub["psf"].to_numpy(dtype=float))
    if TREND_METHOD == "zip_tstat" and "zip_code" in sub.columns:
        # Measure each sale against its own ZIP's norm first. Otherwise a month
        # with more sales in cheap areas reads as a falling market.
        z = sub["zip_code"].astype(str).to_numpy()
        ys = pd.Series(y)
        y = (ys - ys.groupby(z).transform("median")).to_numpy()
    if np.ptp(x) < 3:
        return 0.0
    try:
        slope, intercept = np.polyfit(x, y, 1)
    except Exception:
        return 0.0
    # slope runs backwards in time, so flip the sign to get appreciation.
    monthly = float(-slope)
    if not math.isfinite(monthly):
        return 0.0

    # Scatter in individual home prices dwarfs the time signal, so a raw slope
    # can imply double-digit appreciation that is really noise. Two ways to
    # shrink it:
    #   'r2'    by the share of variance the fit explains. Safe, but in a
    #           cross-section R^2 is tiny even when the trend is real, so it
    #           all but switches the time adjustment off.
    #   'tstat' by how sure we are the slope is not zero: t^2 / (t^2 + 4),
    #           about half weight at t=2 and nearly full at t=5+. Real trends
    #           in big pools survive; noise in small ones does not.
    resid = y - (slope * x + intercept)
    ss_res = float(np.sum(resid ** 2))
    if TREND_METHOD in ("tstat", "zip_tstat"):
        n = len(x)
        sxx = float(np.sum((x - x.mean()) ** 2))
        se = math.sqrt(ss_res / max(n - 2, 1) / sxx) if sxx > 0 else math.inf
        t = abs(slope) / se if se and math.isfinite(se) else 0.0
        monthly *= t * t / (t * t + 4.0)
    else:
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        monthly *= max(0.0, min(1.0, r2))

    return max(-0.008, min(0.008, monthly))  # +/-0.8%/mo (~10%/yr) ceiling


def select_comps(df: pd.DataFrame, subject: dict):
    """Progressively relax tolerances until we have enough comps."""
    s_sqft = subject.get("sqft") or 0
    s_beds = subject.get("beds")

    for radius, sqft_tol, bed_delta, max_months in SEARCH_TIERS:
        sel = df[(df["distance_mi"] <= radius) & (df["months_ago"] <= max_months)]
        if s_sqft:
            sel = sel[sel["sqft"].between(s_sqft * (1 - sqft_tol), s_sqft * (1 + sqft_tol))]
        if s_beds is not None:
            sel = sel[sel["beds"].isna() | (sel["beds"] - s_beds).abs().le(bed_delta)]
        if len(sel) >= TARGET_COMPS:
            criteria = {
                "radius_mi": radius,
                "sqft_tolerance_pct": round(sqft_tol * 100),
                "bed_delta": bed_delta,
                "max_months_old": max_months,
                "relaxed": radius > SEARCH_TIERS[0][0],
            }
            ti = SEARCH_TIERS.index((radius, sqft_tol, bed_delta, max_months))
            sel = _supplement(df, sel, subject, ti, criteria, "renovated", MIN_RENOVATED)
            sel = _supplement(df, sel, subject, ti, criteria, "distressed",
                              cond.MIN_DISTRESSED_FOR_ASIS)
            return sel.copy(), criteria

    # Nothing hit the target; fall back to the widest tier we have.
    radius, sqft_tol, bed_delta, max_months = SEARCH_TIERS[-1]
    sel = df[(df["distance_mi"] <= radius) & (df["months_ago"] <= max_months)]
    if s_sqft:
        sel = sel[sel["sqft"].between(s_sqft * (1 - sqft_tol), s_sqft * (1 + sqft_tol))]
    if len(sel) < MIN_COMPS:
        sel = df[df["distance_mi"] <= radius].nsmallest(TARGET_COMPS, "distance_mi")
    if len(sel) < MIN_COMPS:
        raise CompError(
            "Only %d usable comparable sale(s) found near this property. "
            "The area is too thin to produce a defensible ARV." % len(sel)
        )
    return sel.copy(), {
        "radius_mi": radius,
        "sqft_tolerance_pct": round(sqft_tol * 100),
        "bed_delta": bed_delta,
        "max_months_old": max_months,
        "relaxed": True,
        "below_target": True,
    }


MIN_RENOVATED = 3


def _supplement(df: pd.DataFrame, sel: pd.DataFrame, subject: dict, tier_idx: int,
                criteria: dict, label: str, minimum: int) -> pd.DataFrame:
    """Reach up to two tiers further for comps in a given condition.

    An appraiser does the same: travel a little further for a better-matched
    sale rather than settle for a closer one in the wrong condition.
    """
    have = int((sel["condition"] == label).sum()) if "condition" in sel.columns else 0
    if have >= minimum:
        return sel
    s_sqft = subject.get("sqft") or 0
    for radius, sqft_tol, _bd, max_months in SEARCH_TIERS[tier_idx + 1: tier_idx + 3]:
        wide = df[(df["distance_mi"] <= radius) & (df["months_ago"] <= max_months)
                  & (df["condition"] == label)]
        if s_sqft:
            wide = wide[wide["sqft"].between(s_sqft * (1 - sqft_tol), s_sqft * (1 + sqft_tol))]
        if len(wide) >= minimum:
            extra = wide[~wide.index.isin(sel.index)]
            criteria[f"{label}_radius_mi"] = radius
            return pd.concat([sel, extra])
    return sel


def _local_outliers(sel: pd.DataFrame) -> pd.DataFrame:
    """Drop comps wildly out of line with their own neighbours.

    The pool-wide trim keeps the top and bottom 5% county-wide, which a
    cheap neighbourhood can sit entirely inside. Judged against the comp set
    itself, a sale at 3x or a third of the local price per sqft is a data
    error, a teardown or a land deal -- not a comparable.
    """
    if len(sel) < 5:
        return sel
    med = float(sel["psf"].median())
    keep = sel[sel["psf"].between(med * 0.33, med * 3.0)]
    return keep if len(keep) >= MIN_COMPS else sel


def season_factor(month: int) -> float:
    return 1.0 + SEASONAL.get(int(month), 0.0) / 100.0


def adjust_comps(sel: pd.DataFrame, subject: dict, monthly_trend: float, as_of=None) -> list:
    """Apply paired-sales style adjustments to bring each comp to the subject."""
    sel = _local_outliers(sel)
    median_price = float(sel["sale_price"].median())
    median_psf = float(sel["psf"].median())
    marginal_psf = median_psf * SQFT_MARGINAL_FACTOR
    land_psf = median_psf * LOT_MARGINAL_FACTOR

    value_month = (pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(dt.date.today())).month
    s_sqft = subject.get("sqft")
    s_beds = subject.get("beds")
    s_fb = subject.get("full_baths")
    s_hb = subject.get("half_baths") or 0
    s_year = subject.get("year_built")
    s_lot = subject.get("lot_sqft")
    s_gar = subject.get("parking_garage")

    out = []
    for _, r in sel.iterrows():
        price = float(r["sale_price"])
        adj = {}

        # Time / market movement: bring an older sale forward to today.
        if monthly_trend and r["months_ago"] > 0.5:
            adj["time"] = price * ((1 + monthly_trend) ** r["months_ago"] - 1)

        # Season: a January sale understates what the same house fetches in May.
        if SEASONAL and pd.notna(r.get("sold_date")):
            f = season_factor(value_month) / season_factor(r["sold_date"].month)
            if abs(f - 1) > 1e-6:
                adj["season"] = price * (f - 1)

        # Living area.
        if s_sqft and pd.notna(r["sqft"]):
            adj["sqft"] = (s_sqft - float(r["sqft"])) * marginal_psf

        # Bedrooms.
        if s_beds is not None and pd.notna(r["beds"]):
            d = max(-MAX_BED_DELTA, min(MAX_BED_DELTA, s_beds - float(r["beds"])))
            if d:
                adj["beds"] = d * median_price * BED_VALUE_PCT

        # Bathrooms.
        if s_fb is not None and pd.notna(r["full_baths"]):
            d = max(-MAX_BATH_DELTA, min(MAX_BATH_DELTA, s_fb - float(r["full_baths"])))
            if d:
                adj["full_baths"] = d * median_price * FULL_BATH_PCT
        # Missing is not the same as zero. Listing data omits half baths and
        # garage counts constantly; imputing zero would credit the subject for
        # a difference that may not exist, and every such error pushes the
        # adjusted value up.
        if s_hb is not None and pd.notna(r["half_baths"]):
            d = s_hb - float(r["half_baths"])
            if d:
                adj["half_baths"] = d * median_price * HALF_BATH_PCT

        # Effective age.
        if s_year and pd.notna(r["year_built"]):
            raw = (s_year - float(r["year_built"])) * AGE_RATE_PER_YEAR * price
            adj["age"] = max(-AGE_ADJ_CAP * price, min(AGE_ADJ_CAP * price, raw))

        # Lot size.
        if s_lot and pd.notna(r["lot_sqft"]):
            raw = (s_lot - float(r["lot_sqft"])) * land_psf
            adj["lot"] = max(-LOT_ADJ_CAP * price, min(LOT_ADJ_CAP * price, raw))

        # Garage bays.
        if s_gar is not None and pd.notna(r["parking_garage"]):
            d = s_gar - float(r["parking_garage"])
            if d:
                adj["garage"] = d * median_price * GARAGE_VALUE_PCT

        net = sum(adj.values())
        gross = sum(abs(v) for v in adj.values())

        # Cap the net so no single comp can be adjusted implausibly far from
        # what it actually sold for.
        cap = NET_ADJ_CAP * price
        net_capped = max(-cap, min(cap, net))
        capped = abs(net_capped - net) > 1
        net = net_capped

        adjusted = price + net

        rec = row_to_dict(r)
        rec.update({
            "sold_price": price,
            "price_is_proxy": bool(r.get("price_is_proxy", False)),
            "distance_mi": round(float(r["distance_mi"]), 2),
            "months_ago": round(float(r["months_ago"]), 1),
            "psf": round(float(r["psf"]), 2),
            "adjustments": {k: round(v) for k, v in adj.items()},
            "net_adjustment": round(net),
            "net_capped": capped,
            "gross_adjustment_pct": round(gross / price * 100, 1),
            "adjusted_value": round(adjusted),
            "adjusted_psf": round(adjusted / float(r["sqft"]), 2) if r["sqft"] else None,
            "over_adjusted": (gross / price) > GROSS_ADJ_WARN,
            "condition": r.get("condition") if "condition" in r else "neutral",
        })
        out.append(rec)

    return out


def _weight(comp: dict) -> float:
    d = comp["distance_mi"]
    m = comp["months_ago"]
    g = comp["gross_adjustment_pct"] / 100.0
    w_dist = 1.0 / (1.0 + (d / DIST_FALLOFF_MI) ** 2)   # falls off past the falloff distance
    w_time = 0.5 ** (m / HALF_LIFE_MONTHS)               # recency half-life
    w_sim = 1.0 / (1.0 + g * ADJ_PENALTY)                # heavy adjustment -> less trust
    return max(w_dist * w_time * w_sim, 1e-6)


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cw = np.cumsum(w) - 0.5 * w
    cw /= np.sum(w)
    return float(np.interp(q, cw, v))


def _condition_estimate(comps: list, table: dict) -> dict | None:
    """Weighted median and IQR of adjusted values under a condition weighting."""
    vals, wts = [], []
    for c in comps:
        w = c["base_weight"] * table.get(c.get("condition") or "neutral", 1.0)
        vals.append(c["adjusted_value"])
        wts.append(max(w, 1e-9))
    if len(vals) < 2:
        return None
    v, w = np.array(vals, float), np.array(wts, float)
    return {"mid": _weighted_quantile(v, w, 0.5), "low": _weighted_quantile(v, w, 0.25),
            "high": _weighted_quantile(v, w, 0.75)}


def reconcile(comps: list, subject: dict) -> dict:
    """Blend adjusted comp values into a single ARV with a range and confidence.

    The ARV leans on renovated sales and the as-is value on distressed ones;
    see condition.py for why mixing them answers neither question.
    """
    for c in comps:
        c["base_weight"] = _weight(c)
        c["weight"] = c["base_weight"] * cond.ARV_WEIGHT.get(c.get("condition") or "neutral", 1.0)

    counts = {k: sum(1 for c in comps if c.get("condition") == k) for k in cond.LABELS}
    asis = _condition_estimate(comps, cond.ASIS_WEIGHT)         if counts["distressed"] >= cond.MIN_DISTRESSED_FOR_ASIS else None

    comps.sort(key=lambda c: c["weight"], reverse=True)
    used = comps[:MAX_USED]  # keep the analysis readable and the tail from dominating
    tot_w = sum(c["weight"] for c in used)
    for c in used:
        c["weight_pct"] = round(c["weight"] / tot_w * 100, 1)

    values = np.array([c["adjusted_value"] for c in used], dtype=float)
    weights = np.array([c["weight"] for c in used], dtype=float)

    arv = _weighted_quantile(values, weights, ARV_QUANTILE)
    low = _weighted_quantile(values, weights, 0.25)
    high = _weighted_quantile(values, weights, 0.75)

    mean = float(np.average(values, weights=weights))
    cv = float(np.sqrt(np.average((values - mean) ** 2, weights=weights)) / mean) if mean else 1.0

    # Confidence: more comps, tighter spread, closer and fresher scores higher.
    n_score = min(len(used) / TARGET_COMPS, 1.0)
    cv_score = max(0.0, 1.0 - cv / 0.30)
    dist_score = max(0.0, 1.0 - float(np.median([c["distance_mi"] for c in used])) / 2.0)
    time_score = max(0.0, 1.0 - float(np.median([c["months_ago"] for c in used])) / 18.0)
    adj_score = max(0.0, 1.0 - float(np.median([c["gross_adjustment_pct"] for c in used])) / 35.0)
    confidence = round(100 * (0.30 * n_score + 0.25 * cv_score + 0.18 * dist_score
                              + 0.14 * time_score + 0.13 * adj_score))

    # Comps priced from final list rather than a recorded sale are weaker
    # evidence, so the confidence score must reflect that.
    proxy_n = sum(1 for c in used if c.get("price_is_proxy"))
    proxy_share = proxy_n / len(used) if used else 0.0
    confidence = round(confidence * (1.0 - 0.20 * proxy_share))

    if confidence >= 75:
        label = "High"
    elif confidence >= 55:
        label = "Moderate"
    elif confidence >= 35:
        label = "Low"
    else:
        label = "Very low"

    # Sanity anchor: the selected comps are already size- and type-matched, so
    # the ARV per square foot should sit close to what they actually sold for.
    # A large gap means the adjustments are doing too much work, which is how
    # an ARV drifts above the market without anyone noticing.
    reno_psf = [c["psf"] for c in used if c.get("psf") and c.get("condition") == "renovated"]
    comp_psf = float(np.median(reno_psf if len(reno_psf) >= 3
                               else [c["psf"] for c in used if c.get("psf")]))
    raw_median = float(np.median([c["sold_price"] for c in used]))

    band = next(((w10, w20) for lo, hi, w10, w20 in CONFIDENCE_CALIBRATION
                 if lo <= confidence < hi), (None, None))

    s_sqft = subject.get("sqft")
    arv_psf = (arv / s_sqft) if s_sqft else None
    drift = ((arv_psf / comp_psf - 1) * 100) if (arv_psf and comp_psf) else 0.0

    # Without renovated evidence the ARV is really a neighbourhood average,
    # which understates what a finished flip sells for. Say so.
    reno_used = sum(1 for c in used if c.get("condition") == "renovated")
    if reno_used == 0:
        confidence = round(confidence * 0.85)
        label = ("High" if confidence >= 75 else "Moderate" if confidence >= 55
                 else "Low" if confidence >= 35 else "Very low")

    return {
        "arv": round(arv),
        "arv_low": round(low),
        "arv_high": round(high),
        "as_is_value": round(asis["mid"]) if asis else None,
        "as_is_low": round(asis["low"]) if asis else None,
        "as_is_high": round(asis["high"]) if asis else None,
        "renovation_premium": round(arv - asis["mid"]) if asis else None,
        "condition_counts": counts,
        "renovated_comps_used": reno_used,
        "arv_psf": round(arv_psf, 2) if arv_psf else None,
        "comp_median_psf": round(comp_psf, 2),
        "price_proxy_share": round(proxy_share * 100),
        "comp_median_price": round(raw_median),
        "arv_vs_comps_pct": round(drift, 1),
        "confidence": confidence,
        "confidence_label": label,
        "confidence_within10_pct": band[0],
        "confidence_within20_pct": band[1],
        "dispersion_pct": round(cv * 100, 1),
        "comp_count": len(used),
        "comps": used,
    }


def run_comps(pool: pd.DataFrame, subject: dict, as_of=None) -> dict:
    """Full comp pipeline: clean -> select -> adjust -> reconcile."""
    if not subject.get("sqft"):
        raise CompError(
            "This listing has no square footage on record, so it cannot be comped "
            "by the sales-comparison approach. Enter the sqft manually to proceed."
        )
    if subject.get("latitude") is None or subject.get("longitude") is None:
        raise CompError("This property has no coordinates on record; cannot locate comps.")

    clean = prepare_pool(pool, subject, as_of=as_of)
    trend = estimate_market_trend(clean)
    sel, criteria = select_comps(clean, subject)
    comps = adjust_comps(sel, subject, trend, as_of=as_of)
    result = reconcile(comps, subject)
    result["criteria"] = criteria
    result["market_trend_monthly_pct"] = round(trend * 100, 3)
    result["market_trend_annual_pct"] = round(((1 + trend) ** 12 - 1) * 100, 1)
    result["pool_size"] = len(clean)
    return result
