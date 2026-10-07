"""Measured accuracy -- how close the ARV lands, county by county.

A confidence score is the model's opinion of itself. This is the market's
opinion of the model: take recent renovated sales, hide each one's price, ask
the comp engine what it would have said, and compare. The result is cached
for a month and quoted on every analysis in that county, so "ARV $175,000"
always arrives with "typically within 9% here".
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import warnings

import pandas as pd

from .compengine import run_comps
from .condition import classify
from .data import CACHE_DIR, _cached_scrape, row_to_dict

warnings.filterwarnings("ignore")

TTL_DAYS = 30
SAMPLE = 40


def _path(county_key: str) -> str:
    d = os.path.join(CACHE_DIR, "accuracy")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{county_key}.json")


def cached(county_key: str | None) -> dict | None:
    if not county_key:
        return None
    try:
        with open(_path(county_key), encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        return None
    age = (dt.date.today() - dt.date.fromisoformat(rec["date"])).days
    return rec if age <= TTL_DAYS else None


def measure(county_key: str, state: str = "OK", sample: int = SAMPLE, seed: int = 7,
            progress=None) -> dict:
    """Leave-one-out backtest of the ARV against renovated sales."""
    q = f"{county_key.title()} County, {state}"
    pool = _cached_scrape(f"search_sold|{q.lower()}|365", location=q, listing_type="sold",
                          past_days=365)
    if pool is None or pool.empty:
        raise RuntimeError(f"No sales for {q}")
    pool = pool.copy()
    pool["_cond"] = (pool["text"] if "text" in pool.columns else pd.Series("", index=pool.index)) \
        .apply(classify)
    price = pd.to_numeric(pool["sold_price"], errors="coerce")
    sqft = pd.to_numeric(pool["sqft"], errors="coerce")
    cand = pool[(pool["_cond"] == "renovated") & price.between(40_000, 600_000)
                & sqft.between(700, 4_000) & (pool["style"].astype(str) == "SINGLE_FAMILY")]
    idx = list(cand.index)
    random.Random(seed).shuffle(idx)
    errs = []
    for i in idx[: sample * 2]:  # some will fail to comp; aim for `sample` results
        if len(errs) >= sample:
            break
        r = cand.loc[i]
        try:
            res = run_comps(pool, row_to_dict(r))
        except Exception:
            continue
        actual = float(r["sold_price"])
        errs.append((res["arv"] - actual) / actual)
        if progress and len(errs) % 10 == 0:
            progress(f"{county_key}: {len(errs)} backtested")
    if len(errs) < 10:
        raise RuntimeError(f"Only {len(errs)} renovated sales could be backtested in {q}")
    s = pd.Series(errs)
    rec = {
        "county": county_key, "date": dt.date.today().isoformat(), "n": int(len(s)),
        "median_error_pct": round(float(s.median()) * 100, 1),
        "median_abs_error_pct": round(float(s.abs().median()) * 100, 1),
        "mean_abs_error_pct": round(float(s.abs().mean()) * 100, 1),
        "within_10_pct": round(float((s.abs() <= 0.10).mean()) * 100),
        "within_20_pct": round(float((s.abs() <= 0.20).mean()) * 100),
    }
    with open(_path(county_key), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return rec


def describe(rec: dict | None) -> str | None:
    if not rec:
        return None
    bias = rec["median_error_pct"]
    lean = ("runs slightly low" if bias < -2 else "runs slightly high" if bias > 2 else "is unbiased")
    return (f"Backtested on {rec['n']} renovated sales here: typically within "
            f"{rec['median_abs_error_pct']:.0f}%, {rec['within_10_pct']}% land within 10%, "
            f"and the ARV {lean} ({bias:+.1f}%).")


def measure_area(key: str, pool, targets, progress=None) -> dict:
    """Same backtest for an arbitrary area (e.g. a school district), time-ordered."""
    from . import backtest as B
    df = B.run_arv(pool, targets, progress=progress)
    if len(df) < 10:
        raise RuntimeError(f"Only {len(df)} renovated sales could be backtested in {key}")
    e = df["err"]
    rec = {"county": key, "date": dt.date.today().isoformat(), "n": int(len(df)),
           "median_error_pct": round(float(e.median()) * 100, 1),
           "median_abs_error_pct": round(float(e.abs().median()) * 100, 1),
           "mean_abs_error_pct": round(float(e.abs().mean()) * 100, 1),
           "within_10_pct": round(float((e.abs() <= 0.10).mean()) * 100),
           "within_20_pct": round(float((e.abs() <= 0.20).mean()) * 100),
           "method": "time-ordered"}
    with open(_path(key), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return rec
