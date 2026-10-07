"""Backtesting -- how the engine would have done, using only what was known then.

Two tests:

ARV backtest
    Take every renovated single-family sale in the last year. For each, wind
    the clock back to the day before it sold, hide it, and ask the comp engine
    for a value using only sales that had already closed. Compare with what it
    actually sold for. Splitting targets by date (older half to tune on, newer
    half to judge on) keeps tuning honest.

Flip backtest
    Find houses real investors flipped locally: listings whose previous sale
    was 2-20 months earlier at well under the new asking price. On the day the
    investor bought, what would this app have said the house was worth after
    repair, and what would it have let you pay? If real flippers routinely pay
    more than the app's maximum and still list at a profit, the app is too
    timid and you will lose deals to them.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import math
import random
import warnings
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from . import compengine as ce
from . import condition as cond
from . import offer, rehab, states
from .data import _cached_scrape, row_to_dict

warnings.filterwarnings("ignore")

Progress = Callable[[str], None] | None


# --- data -------------------------------------------------------------------

def sold_pool(county_key: str, days: int = 1095, state: str = "OK") -> pd.DataFrame:
    q = f"{county_key.title()} County, {state}"
    df = _cached_scrape(f"search_sold|{q.lower()}|{days}", location=q, listing_type="sold",
                        past_days=days)
    df = df.copy()
    df["_cond"] = (df["text"] if "text" in df.columns else pd.Series("", index=df.index)) \
        .apply(cond.classify)
    df["condition"] = df["_cond"]  # so the engine does not re-classify per valuation
    keep = [c for c in ("property_id", "formatted_address", "city", "state", "county", "zip_code", "style",
                        "sold_price", "list_price", "last_sold_price", "last_sold_date", "sqft",
                        "beds", "full_baths", "half_baths", "year_built", "lot_sqft", "latitude",
                        "longitude", "parking_garage", "days_on_mls", "condition", "_cond", "text")
            if c in df.columns]
    df = df[keep].copy()  # the engine copies its pool per valuation; keep it narrow
    df["_date"] = pd.to_datetime(df["last_sold_date"], errors="coerce")
    if getattr(df["_date"].dtype, "tz", None) is not None:
        df["_date"] = df["_date"].dt.tz_localize(None)
    return df


def arv_targets(pool: pd.DataFrame, days: int = 365) -> pd.DataFrame:
    price = pd.to_numeric(pool["sold_price"], errors="coerce")
    sqft = pd.to_numeric(pool["sqft"], errors="coerce")
    cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=days))
    t = pool[(pool["_cond"] == "renovated") & price.between(40_000, 600_000)
             & sqft.between(700, 4_000) & (pool["style"].astype(str) == "SINGLE_FAMILY")
             & (pool["_date"] >= cutoff)]
    # Portfolio-sale rows are not real prices; never score against them. Bundles
    # have to be found across the whole pool, not just within the targets.
    keep = ce.drop_bulk_sales(pool.assign(sale_price=price), "sale_price", "_date").index
    t = t[t.index.isin(keep)]
    return t.sort_values("_date")


# --- parameter overrides ----------------------------------------------------

TUNABLE = {
    "SQFT_MARGINAL_FACTOR": ce, "BED_VALUE_PCT": ce, "FULL_BATH_PCT": ce,
    "AGE_RATE_PER_YEAR": ce, "NET_ADJ_CAP": ce, "TARGET_COMPS": ce,
    "DIST_FALLOFF_MI": ce, "HALF_LIFE_MONTHS": ce, "ADJ_PENALTY": ce,
    "MAX_USED": ce, "ARV_QUANTILE": ce, "MIN_RENOVATED": ce, "TREND_METHOD": ce, "SEASONAL": ce,
}


@contextlib.contextmanager
def overrides(params: dict | None):
    """Temporarily change engine settings; always restored."""
    params = params or {}
    saved, saved_w = {}, dict(cond.ARV_WEIGHT)
    try:
        for k, v in params.items():
            if k.startswith("ARV_WEIGHT."):
                cond.ARV_WEIGHT[k.split(".", 1)[1]] = v
            elif k in TUNABLE:
                saved[k] = getattr(TUNABLE[k], k)
                setattr(TUNABLE[k], k, v)
            else:
                raise KeyError(f"Unknown setting {k}")
        yield
    finally:
        for k, v in saved.items():
            setattr(TUNABLE[k], k, v)
        cond.ARV_WEIGHT.clear()
        cond.ARV_WEIGHT.update(saved_w)


# --- ARV backtest -----------------------------------------------------------

def run_arv(pool: pd.DataFrame, targets: pd.DataFrame, params: dict | None = None,
            progress: Progress = None) -> pd.DataFrame:
    rows = []
    with overrides(params):
        for n, (i, r) in enumerate(targets.iterrows(), 1):
            actual = float(r["sold_price"])
            subject = row_to_dict(r)
            as_of = r["_date"] - pd.Timedelta(days=1)
            try:
                res = ce.run_comps(pool, subject, as_of=as_of)
            except Exception:
                continue
            rows.append({
                "address": subject.get("formatted_address"), "city": subject.get("city"),
                "date": r["_date"].date(), "actual": actual, "arv": res["arv"],
                "low": res["arv_low"], "high": res["arv_high"], "conf": res["confidence"],
                "comps": res["comp_count"], "reno_used": res.get("renovated_comps_used"),
                "dispersion": res.get("dispersion_pct"),
                "radius": res.get("criteria", {}).get("radius_mi"),
                "err": (res["arv"] - actual) / actual,
            })
            if progress and n % 50 == 0:
                progress(f"{n}/{len(targets)} valued")
    return pd.DataFrame(rows)


def metrics(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"n": 0}
    e = df["err"]
    return {
        "n": int(len(df)),
        "median_abs_err_pct": round(float(e.abs().median()) * 100, 1),
        "mean_abs_err_pct": round(float(e.abs().mean()) * 100, 1),
        "bias_pct": round(float(e.median()) * 100, 1),
        "within_10_pct": round(float((e.abs() <= 0.10).mean()) * 100, 1),
        "within_20_pct": round(float((e.abs() <= 0.20).mean()) * 100, 1),
        "range_coverage_pct": round(float(((df["low"] <= df["actual"])
                                           & (df["actual"] <= df["high"])).mean()) * 100, 1),
    }


def score(m: dict) -> float:
    """One number to tune on: typical error, with a nudge toward fewer big misses."""
    if not m.get("n"):
        return math.inf
    return m["median_abs_err_pct"] + 0.25 * (100 - m["within_20_pct"])


def tune(pool_targets: list[tuple[pd.DataFrame, pd.DataFrame]], grid: dict[str, list],
         base: dict | None = None, progress: Progress = None) -> dict[str, Any]:
    """Coordinate descent on the older half of targets; report the newer half.

    pool_targets: (pool, targets) per county. Targets are split by date so the
    settings are chosen on sales that happened before the ones they are judged on.
    """
    train, test = [], []
    for pool, t in pool_targets:
        half = len(t) // 2
        train.append((pool, t.iloc[:half]))
        test.append((pool, t.iloc[half:]))

    def evaluate(params, sets):
        frames = [run_arv(p, t, params) for p, t in sets]
        return metrics(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame())

    best = dict(base or {})
    best_m = evaluate(best, train)
    history = [{"params": dict(best), "train": best_m, "score": score(best_m)}]
    if progress:
        progress(f"baseline train score {score(best_m):.2f} ({best_m['median_abs_err_pct']}% typical)")
    for key, values in grid.items():
        for v in values:
            if best.get(key, _current(key)) == v:
                continue
            trial = {**best, key: v}
            m = evaluate(trial, train)
            history.append({"params": trial, "train": m, "score": score(m)})
            if score(m) < score(best_m) - 0.15:  # require a real gain, not noise
                best, best_m = trial, m
                if progress:
                    progress(f"  {key}={v}: train score {score(m):.2f} - kept")
            elif progress:
                progress(f"  {key}={v}: train score {score(m):.2f}")
    return {
        "best": best,
        "train_before": history[0]["train"], "train_after": best_m,
        "test_before": evaluate(base or {}, test), "test_after": evaluate(best, test),
        "history": history,
    }


def _current(key: str):
    if key.startswith("ARV_WEIGHT."):
        return cond.ARV_WEIGHT[key.split(".", 1)[1]]
    return getattr(TUNABLE[key], key)


def breakdown(df: pd.DataFrame, by: str, bins: Iterable | None = None) -> list[dict]:
    if df.empty:
        return []
    col = pd.cut(df[by], bins) if bins is not None else df[by]
    out = []
    for k, g in df.groupby(col, observed=True):
        out.append({"group": str(k), **metrics(g)})
    return out


# --- flip backtest ----------------------------------------------------------

def find_flips(county_key: str, state: str = "OK") -> pd.DataFrame:
    """Listings whose previous sale was 2-20 months ago at well under today's price."""
    q = f"{county_key.title()} County, {state}"
    frames = []
    for lt in ("for_sale", "pending", "off_market"):
        try:
            df = _cached_scrape(f"flipprobe|{q.lower()}|{lt}", location=q, listing_type=lt)
        except Exception:
            continue
        if df is not None and not df.empty:
            frames.append(df.assign(_lt=lt))
    if not frames:
        return pd.DataFrame()
    L = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["property_id"])
    L["_buy"] = pd.to_numeric(L["last_sold_price"], errors="coerce")
    L["_ask"] = pd.to_numeric(L["list_price"], errors="coerce")
    bd = pd.to_datetime(L["last_sold_date"], errors="coerce")
    ld = pd.to_datetime(L["list_date"], errors="coerce")
    L["_buy_date"] = bd.dt.tz_localize(None) if getattr(bd.dtype, "tz", None) is not None else bd
    L["_list_date"] = ld.dt.tz_localize(None) if getattr(ld.dtype, "tz", None) is not None else ld
    L["_gap"] = (L["_list_date"] - L["_buy_date"]).dt.days
    L["_cond"] = L["text"].apply(cond.classify)
    f = L[L["_gap"].between(60, 600) & (L["_buy"] >= 15_000) & (L["_ask"] >= L["_buy"] * 1.25)
          & (L["style"].astype(str) == "SINGLE_FAMILY")
          & pd.to_numeric(L["sqft"], errors="coerce").between(600, 4_000)]
    # A real flip is renovated before relisting; 'distressed' relists are wholesalers.
    f = f[f["_cond"].isin(["renovated", "neutral", "mixed"])]
    return f.assign(_county=county_key)


def run_flips(flips: pd.DataFrame, progress: Progress = None) -> pd.DataFrame:
    rows = []
    loc = states.resolve("OK", None, None)
    for n, (_, r) in enumerate(flips.iterrows(), 1):
        subject = row_to_dict(r)
        addr = subject.get("formatted_address")
        try:
            pool = _cached_scrape(f"flippool|{addr.lower()}|1095|2.0", location=addr,
                                  listing_type="sold", past_days=1095, radius=2.0)
            as_of = r["_buy_date"] - pd.Timedelta(days=1)
            res = ce.run_comps(pool, subject, as_of=as_of)
        except Exception:
            continue
        arv = float(res["arv"])
        buy, ask = float(r["_buy"]), float(r["_ask"])
        sqft = float(subject.get("sqft") or 0)
        tier = rehab.suggest_tier(subject.get("year_built"), buy / sqft if sqft else None,
                                  res.get("arv_psf"))
        reno = rehab.estimate(sqft=sqft, tier=tier, market_psf=res.get("arv_psf"), arv=arv)
        months = {"cosmetic": 1.0, "light": 1.5, "moderate": 2.5, "heavy": 4.0,
                  "gut": 6.0}.get(tier, 2.5) + 2.0 + 1.0
        params = {"transfer_tax_pct": loc["sell_transfer_pct"],
                  "buy_transfer_tax_pct": loc["buy_transfer_pct"],
                  "annual_taxes": arv * loc["property_tax_pct"] / 100, "hold_months": months}
        mao = offer.max_allowable_offer(arv, float(reno["total"]), params)
        at_actual = offer.evaluate(buy, ask, float(reno["total"]), params)
        rows.append({
            "property_id": str(r.get("property_id") or ""),
            "address": addr, "county": r["_county"], "bought": r["_buy_date"].date(),
            "buy": buy, "ask": ask, "gap_days": int(r["_gap"]), "status": r["_lt"],
            "arv_then": round(arv), "arv_vs_ask_pct": round((arv / ask - 1) * 100, 1),
            "conf": res["confidence"], "tier": tier, "rehab": reno["total"], "mao": round(mao),
            "mao_vs_buy_pct": round((mao / buy - 1) * 100, 1) if buy else None,
            "app_would_buy": mao >= buy,
            "profit_if_sells_at_ask": at_actual["profit"],
        })
        if progress and n % 10 == 0:
            progress(f"{n}/{len(flips)} flips valued")
    return pd.DataFrame(rows)


def district_pool(key: str, days: int = 1095) -> pd.DataFrame:
    """Three years of sales inside a focus school district, prepared like sold_pool."""
    from . import focus
    df = focus.listings("sold", [key], past_days=days)
    if df.empty:
        return df
    df = df.copy()
    df["_cond"] = (df["text"] if "text" in df.columns else pd.Series("", index=df.index)) \
        .apply(cond.classify)
    df["condition"] = df["_cond"]
    df["_date"] = pd.to_datetime(df["last_sold_date"], errors="coerce")
    if getattr(df["_date"].dtype, "tz", None) is not None:
        df["_date"] = df["_date"].dt.tz_localize(None)
    return df


# --- flip tracker: score the app against flips once they actually sell -------

def _flip_store() -> str:
    import os
    from .data import CACHE_DIR
    d = os.path.join(CACHE_DIR, "backtest")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "flips.json")


def save_flips(df: pd.DataFrame) -> int:
    """Remember what the app said about each flip on its purchase day."""
    import json
    path = _flip_store()
    try:
        store = json.load(open(path, encoding="utf-8"))
    except Exception:
        store = {}
    added = 0
    for _, r in df.iterrows():
        key = r.get("property_id") or r["address"]
        if key in store:
            continue
        store[key] = {k: (str(v) if k == "bought" else (float(v) if isinstance(v, (int, float, np.floating, np.integer)) and not isinstance(v, bool) else v))
                      for k, v in r.items() if k in ("property_id", "address", "county", "bought", "buy", "ask",
                                                     "arv_then", "mao", "conf", "rehab", "tier")}
        store[key]["recorded"] = dt.date.today().isoformat()
        added += 1
    json.dump(store, open(path, "w", encoding="utf-8"), indent=1, default=str)
    return added


def check_flips(progress: Progress = None) -> pd.DataFrame:
    """Which remembered flips have sold since, and how close the app was."""
    import json
    try:
        store = json.load(open(_flip_store(), encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    pools = []
    for c in sorted({v["county"] for v in store.values()}):
        try:
            p = _cached_scrape(f"search_sold|{c} county, ok|365", location=f"{c.title()} County, OK",
                               listing_type="sold", past_days=365)
            pools.append(p)
        except Exception:
            continue
    try:
        from . import focus
        pools.append(focus.listings("sold", past_days=365))
    except Exception:
        pass
    if not pools:
        return pd.DataFrame()
    from .leads import _addr_key
    sold = pd.concat(pools, ignore_index=True)
    sold["_pid"] = sold["property_id"].astype(str)
    sold["_key"] = sold["formatted_address"].apply(_addr_key)
    d = pd.to_datetime(sold["last_sold_date"], errors="coerce")
    sold["_d"] = d.dt.tz_localize(None) if getattr(d.dtype, "tz", None) is not None else d
    rows = []
    for key, f in store.items():
        pid = str(f.get("property_id") or "")
        hit = sold[sold["_pid"] == pid] if pid else sold.iloc[0:0]
        if hit.empty:
            hit = sold[sold["_key"] == _addr_key(f.get("address"))]
        if hit.empty:
            continue
        h = hit.sort_values("_d").iloc[-1]
        bought = pd.Timestamp(f["bought"])
        # The resale must come after the purchase it is being compared with.
        if pd.isna(h["_d"]) or h["_d"] <= bought + pd.Timedelta(days=30):
            continue
        price = float(pd.to_numeric(h["sold_price"], errors="coerce") or 0)
        if price <= 0:
            continue
        rows.append({**f, "sold": price, "sold_date": str(h["_d"])[:10],
                     "arv_vs_sold_pct": round((f["arv_then"] / price - 1) * 100, 1),
                     "ask_vs_sold_pct": round((f["ask"] / price - 1) * 100, 1)})
    return pd.DataFrame(rows)
