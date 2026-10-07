"""Rental exit -- does the deal work as a BRRRR when it does not work as a flip?

Buy, Rehab, Rent, Refinance, Repeat. The refinance is the point: a lender
values the finished house at ARV and lends a share of that, so a property
bought far enough below value hands most or all of the cash back while you
keep the house and its rent. A deal too thin to flip is often a fine rental,
and a flipper who can only see one exit walks away from half their deals.

Rent comes from local rental listings when there are enough nearby, and
otherwise from HUD Fair Market Rents -- the 40th-percentile gross rent for the
county, which is the conservative figure a lender will underwrite against.
"""
from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
import pandas as pd

from .data import _cached_scrape
from .geo import haversine_mi

warnings.filterwarnings("ignore")

# HUD Fair Market Rents by bedroom count (0-4). FY2026 runs Oct 2025 - Sep 2026.
# Osage, Rogers, Tulsa, Wagoner and Creek all sit in the Tulsa HUD Metro FMR
# Area and share its rents. Others are FY2025 non-metro figures, marked as such.
FMR = {
    "washington": {"year": 2026, "area": "Washington County, OK",
                   "rents": [677, 789, 891, 1254, 1346]},
    "tulsa_metro": {"year": 2026, "area": "Tulsa, OK HUD Metro FMR Area",
                    "rents": [940, 1000, 1230, 1620, 1880]},
    "kay": {"year": 2025, "area": "Kay County, OK", "rents": [624, 694, 910, 1202, 1206]},
    "mayes": {"year": 2025, "area": "Mayes County, OK", "rents": [691, 696, 910, 1175, 1366]},
    "craig": {"year": 2025, "area": "Craig County, OK", "rents": [709, 726, 952, 1176, 1262]},
    "pawnee": {"year": 2025, "area": "Pawnee County, OK", "rents": [643, 748, 937, 1231, 1573]},
    "noble": {"year": 2025, "area": "Noble County, OK", "rents": [678, 822, 910, 1220, 1528]},
    "nowata": {"year": 2025, "area": "Nowata County, OK", "rents": [678, 790, 910, 1275, 1347]},
}
FMR_ALIAS = {"osage": "tulsa_metro", "rogers": "tulsa_metro", "tulsa": "tulsa_metro",
             "wagoner": "tulsa_metro", "creek": "tulsa_metro"}

DEFAULTS = {
    "vacancy_pct": 8.0,
    "maintenance_pct": 8.0,
    "capex_pct": 5.0,
    "management_pct": 10.0,      # set 0 if self-managing
    "refi_ltv_pct": 75.0,        # DSCR / cash-out lenders: 70-75% of appraised ARV
    "refi_rate_pct": 7.5,
    "refi_term_years": 30,
    "refi_closing_pct": 2.5,     # origination, appraisal, title on the new loan
    "rehab_months_before_rent": None,  # filled from the rehab tier
    "min_dscr": 1.20,
    "target_cash_flow": 150,     # $/month the refinance must leave after all costs
}

REHAB_MONTHS = {"cosmetic": 1.0, "light": 1.5, "moderate": 2.5, "heavy": 4.0, "gut": 6.0}


def _county_key(county) -> str:
    return (str(county or "").lower().replace(" county", "").replace(", ok", "").strip())


def fmr_rent(county, beds) -> dict | None:
    key = _county_key(county)
    rec = FMR.get(FMR_ALIAS.get(key, key))
    if not rec:
        return None
    b = int(max(0, min(4, round(beds if beds is not None else 3))))
    return {"rent": rec["rents"][b], "source": f"HUD FMR FY{rec['year']}", "area": rec["area"], "beds": b}


def local_rent(subject: dict) -> dict | None:
    """Median asking rent of comparable rentals nearby, size-adjusted."""
    county = subject.get("county")
    state = subject.get("state") or "OK"
    if not county or subject.get("latitude") is None:
        return None
    q = f"{_county_key(county).title()} County, {state}"
    try:
        df = _cached_scrape(f"rent_pool|{q.lower()}|365", location=q, listing_type="for_rent", past_days=365)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df["rent"] = pd.to_numeric(df["list_price"], errors="coerce")
    df["sqft"] = pd.to_numeric(df["sqft"], errors="coerce")
    df["beds"] = pd.to_numeric(df["beds"], errors="coerce")
    df = df[df["rent"].between(400, 6000) & df["latitude"].notna()]
    df = df[df["style"].astype(str).str.upper().isin(
        {"SINGLE_FAMILY", "MOBILE", "TOWNHOMES", "DUPLEX_TRIPLEX", "MULTI_FAMILY"})]
    if df.empty:
        return None
    df["dist"] = df.apply(lambda r: haversine_mi(subject["latitude"], subject["longitude"],
                                                 float(r["latitude"]), float(r["longitude"])), axis=1)
    beds = subject.get("beds")
    sel = df[df["dist"] <= 5]
    if beds is not None:
        sel = sel[sel["beds"].isna() | (sel["beds"] - beds).abs().le(1)]
    if len(sel) < 4:
        return {"rent": None, "count": int(len(sel)), "source": "local listings",
                "note": f"only {len(sel)} comparable rental(s) within 5 mi"}
    rent = float(sel["rent"].median())
    s_sqft = subject.get("sqft")
    with_sqft = sel[sel["sqft"].between(400, 5000)]
    if s_sqft and len(with_sqft) >= 4:
        # Rent scales with size much more weakly than price does.
        psf = float((with_sqft["rent"] / with_sqft["sqft"]).median())
        rent = 0.5 * rent + 0.5 * psf * s_sqft
    return {"rent": round(rent), "count": int(len(sel)), "source": "local listings",
            "radius_mi": 5}


def estimate_rent(subject: dict) -> dict:
    fmr = fmr_rent(subject.get("county"), subject.get("beds"))
    loc = local_rent(subject)
    if loc and loc.get("rent"):
        rent = loc["rent"]
        # A renovated single-family house rents above the 40th percentile, but
        # an asking rent is not an achieved rent. Do not let listings run more
        # than 25% past FMR without a human looking.
        if fmr:
            rent = min(rent, fmr["rent"] * 1.25)
        return {"rent": round(rent), "source": f"{loc['count']} local rentals",
                "fmr": fmr, "local": loc}
    if fmr:
        return {"rent": fmr["rent"], "source": f"{fmr['source']} ({fmr['area']}, {fmr['beds']}BR)",
                "fmr": fmr, "local": loc}
    return {"rent": None, "source": "no rent data", "fmr": None, "local": loc}


def _loan_for_payment(payment: float, rate_pct: float, years: int) -> float:
    r = rate_pct / 100 / 12
    n = years * 12
    if payment <= 0:
        return 0.0
    if r == 0:
        return payment * n
    return payment * (1 - (1 + r) ** -n) / r


def _pmt(principal: float, rate_pct: float, years: int) -> float:
    r = rate_pct / 100 / 12
    n = years * 12
    if principal <= 0:
        return 0.0
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def brrrr(purchase: float, rehab: float, arv: float, rent: float, annual_taxes: float,
          annual_insurance: float, buy_closing: float, hold_cost_per_month: float,
          rehab_months: float, p: dict | None = None) -> dict[str, Any]:
    """The full buy-rehab-rent-refinance picture at one purchase price."""
    p = {**DEFAULTS, **(p or {})}
    all_in = purchase + rehab + buy_closing + hold_cost_per_month * (rehab_months + 1)
    max_loan = arv * p["refi_ltv_pct"] / 100

    # At today's rates a full 75% refinance often will not cash-flow. Real
    # investors size the loan to the rent: borrow only what keeps DSCR at the
    # lender's floor and leaves positive monthly cash flow, and accept that
    # some cash stays in the deal.
    taxes_m0 = annual_taxes / 12
    ins_m0 = annual_insurance / 12
    opex_pct = (p["vacancy_pct"] + p["maintenance_pct"] + p["capex_pct"] + p["management_pct"]) / 100
    noi0 = rent * (1 - opex_pct) - taxes_m0 - ins_m0
    pi_dscr = rent / p["min_dscr"] - taxes_m0 - ins_m0
    pi_cf = noi0 - p.get("target_cash_flow", 150)
    sustainable = min(max_loan, _loan_for_payment(min(pi_dscr, pi_cf), p["refi_rate_pct"],
                                                  int(p["refi_term_years"])))
    loan = max_loan if p.get("refi_mode") == "max" else max(0.0, sustainable)
    refi_costs = loan * p["refi_closing_pct"] / 100
    cash_back = loan - refi_costs
    cash_left = all_in - cash_back

    pi = _pmt(loan, p["refi_rate_pct"], int(p["refi_term_years"]))
    taxes_m = annual_taxes / 12
    ins_m = annual_insurance / 12
    vac = rent * p["vacancy_pct"] / 100
    maint = rent * p["maintenance_pct"] / 100
    capex = rent * p["capex_pct"] / 100
    mgmt = rent * p["management_pct"] / 100
    opex = taxes_m + ins_m + vac + maint + capex + mgmt
    noi_m = rent - opex
    cash_flow = noi_m - pi
    pitia = pi + taxes_m + ins_m
    dscr = rent / pitia if pitia else 0.0

    annual_cf = cash_flow * 12
    if cash_left <= 0:
        coc = math.inf
    else:
        coc = annual_cf / cash_left
    return {
        "purchase": round(purchase), "all_in": round(all_in),
        "refi_loan": round(loan), "refi_ltv_pct": round(loan / arv * 100, 1) if arv else None,
        "max_refi_loan": round(max_loan), "refi_costs": round(refi_costs),
        "cash_back_at_refi": round(cash_back), "cash_left_in_deal": round(cash_left),
        "rent": round(rent),
        "monthly": {"principal_interest": round(pi), "taxes": round(taxes_m),
                    "insurance": round(ins_m), "vacancy": round(vac),
                    "maintenance": round(maint), "capex": round(capex),
                    "management": round(mgmt)},
        "noi_monthly": round(noi_m), "cash_flow_monthly": round(cash_flow),
        "cash_flow_annual": round(annual_cf),
        "dscr": round(dscr, 2),
        "cap_rate_pct": round(noi_m * 12 / arv * 100, 2) if arv else None,
        "cash_on_cash_pct": None if coc == math.inf else round(coc * 100, 1),
        "infinite_return": coc == math.inf and annual_cf > 0,
        "one_pct_rule": round(rent / all_in * 100, 2) if all_in else None,
        "refi_qualifies": dscr >= p["min_dscr"],
    }


def max_brrrr_price(rehab: float, arv: float, rent: float, annual_taxes: float,
                    annual_insurance: float, closing_pct: float, closing_flat: float,
                    hold_cost_per_month: float, rehab_months: float,
                    p: dict | None = None) -> float:
    """Highest price at which the refinance returns every dollar you put in.

    The refinance loan does not depend on the purchase price, so this has a
    closed form: all-in cost equals cash back from the refi.
    """
    p = {**DEFAULTS, **(p or {})}
    probe = brrrr(0.0, rehab, arv, rent, annual_taxes, annual_insurance, 0.0,
                  hold_cost_per_month, rehab_months, p)
    loan = probe["refi_loan"]
    cash_back = loan * (1 - p["refi_closing_pct"] / 100)
    fixed = rehab + closing_flat + hold_cost_per_month * (rehab_months + 1)
    price = (cash_back - fixed) / (1 + closing_pct / 100)
    return max(0.0, price)


def analyse(subject: dict, arv: float, rehab_total: float, rehab_tier: str | None,
            purchase: float | None, asking: float | None, params: dict,
            arv_annual_taxes: float) -> dict[str, Any]:
    """Rental exit for the analysis page."""
    rent_info = estimate_rent(subject)
    if params.get("rent_override"):
        rent_info = {**rent_info, "rent": float(params["rent_override"]), "source": "as entered"}
    rent = rent_info.get("rent")
    rp = {k: float(params[k]) for k in ("management_pct", "refi_rate_pct", "refi_ltv_pct",
                                         "vacancy_pct") if params.get(k) not in (None, "")}
    if not rent:
        return {"available": False, "reason": "No rent data for this county.", "rent": rent_info}

    months = REHAB_MONTHS.get(rehab_tier or "moderate", 2.5)
    ins = float(params.get("annual_insurance") or 1800)
    closing_pct = float(params.get("buy_closing_pct") or 1.5) + float(params.get("buy_transfer_tax_pct") or 0)
    closing_flat = float(params.get("buy_closing_flat") or 1000) + float(params.get("inspection_flat") or 750)
    # Carrying a vacant house: taxes, insurance, utilities.
    hold_m = arv_annual_taxes / 12 + ins / 12 + float(params.get("monthly_utilities") or 250)

    max_price = max_brrrr_price(rehab_total, arv, rent, arv_annual_taxes, ins, closing_pct,
                                closing_flat, hold_m, months, rp)

    def at(price):
        if not price:
            return None
        bc = price * closing_pct / 100 + closing_flat
        return brrrr(price, rehab_total, arv, rent, arv_annual_taxes, ins, bc, hold_m, months, rp)

    at_max = at(max_price) if max_price > 0 else None
    at_ask = at(asking)
    at_offer = at(purchase) if purchase and purchase != asking else None

    # A rental is worth chasing when it cash-flows after a refinance the
    # lender will actually do.
    ref = at_ask or at_max
    if ref is None or ref["refi_loan"] <= 0:
        call, why = "NO", (f"Rent of ${rent:,.0f}/mo cannot carry any meaningful refinance once "
                           "taxes, insurance and reserves are paid.")
    else:
        left = ref["cash_left_in_deal"]
        coc = ref["cash_on_cash_pct"]
        ltv = ref["refi_ltv_pct"]
        if left <= 0:
            call = "STRONG"
            why = (f"Refinance at {ltv:.0f}% of ARV returns all your cash and still cash-flows "
                   f"${ref['cash_flow_monthly']:,.0f}/mo. Infinite return.")
        elif coc is not None and coc >= 12:
            call = "WORKS"
            why = (f"${left:,.0f} stays in the deal after a {ltv:.0f}% refinance, earning "
                   f"{coc:.0f}% a year in cash flow (${ref['cash_flow_monthly']:,.0f}/mo).")
        elif coc is not None and coc >= 6:
            call = "THIN"
            why = (f"${left:,.0f} stays in the deal for {coc:.0f}% a year. Better than a savings "
                   "account, worse than your next flip.")
        else:
            call = "NO"
            why = (f"${left:,.0f} would stay trapped for {coc or 0:.0f}% a year. The rent does not "
                   "support the price.")

    return {"available": True, "rent": rent_info, "rehab_months": months,
            "max_price_all_cash_out": round(max_price), "at_max": at_max,
            "at_asking": at_ask, "at_offer": at_offer, "call": call, "why": why,
            "assumptions": {**DEFAULTS, **rp}}
