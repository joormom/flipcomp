"""Deal math: what the flip nets, and the most you can pay and still hit it.

The Maximum Allowable Offer is solved numerically rather than with the usual
back-of-napkin rule, because several costs (closing, down payment, loan
interest) scale with the purchase price itself. The 70% rule is still reported
alongside it as a familiar sanity check.
"""
from __future__ import annotations

DEFAULTS = {
    # Sale side
    "agent_commission_pct": 5.0,     # total, both sides
    "seller_concessions_pct": 1.0,   # buyer credits at closing
    "transfer_tax_pct": 0.5,         # varies hugely by state/county
    "sell_closing_flat": 1_500,      # title, escrow, attorney

    # Buy side
    "buy_transfer_tax_pct": 0.0,   # set from the state table; buyer/split states only
    "buy_closing_pct": 1.5,
    "buy_closing_flat": 1_000,
    "inspection_flat": 750,

    # Holding
    "hold_months": 6,
    "annual_taxes": None,            # pulled from the listing when available
    "annual_insurance": 1_800,
    "monthly_utilities": 250,
    "monthly_hoa": 0,

    # Financing (hard money is the norm for flips)
    "cash_purchase": False,
    "loan_to_purchase_pct": 85.0,
    "rehab_financed_pct": 100.0,
    "interest_rate_pct": 10.5,
    "points_pct": 2.0,

    # Target
    "target_profit_pct": 15.0,       # of ARV
    "min_profit_flat": 25_000,
}


def _pct(p: dict, key: str) -> float:
    return float(p.get(key, DEFAULTS[key]) or 0) / 100.0


def _val(p: dict, key: str) -> float:
    v = p.get(key, DEFAULTS[key])
    return float(v or 0)


def selling_costs(arv: float, p: dict) -> dict:
    commission = arv * _pct(p, "agent_commission_pct")
    concessions = arv * _pct(p, "seller_concessions_pct")
    transfer = arv * _pct(p, "transfer_tax_pct")
    flat = _val(p, "sell_closing_flat")
    total = commission + concessions + transfer + flat
    return {
        "agent_commission": round(commission),
        "seller_concessions": round(concessions),
        "transfer_tax": round(transfer),
        "closing_flat": round(flat),
        "total": round(total),
        "_total": total,
    }


def holding_costs(p: dict) -> dict:
    months = _val(p, "hold_months")
    taxes = _val(p, "annual_taxes") / 12.0 * months
    ins = _val(p, "annual_insurance") / 12.0 * months
    util = _val(p, "monthly_utilities") * months
    hoa = _val(p, "monthly_hoa") * months
    total = taxes + ins + util + hoa
    return {
        "months": months,
        "property_taxes": round(taxes),
        "insurance": round(ins),
        "utilities": round(util),
        "hoa": round(hoa),
        "total": round(total),
        "_total": total,
    }


def financing_costs(purchase: float, rehab: float, p: dict) -> dict:
    if p.get("cash_purchase", DEFAULTS["cash_purchase"]):
        return {"cash": True, "loan_amount": 0, "points": 0, "interest": 0,
                "down_payment": round(purchase), "total": 0, "_total": 0.0,
                "_cash_in_deal": purchase + rehab}

    months = _val(p, "hold_months")
    ltp = _pct(p, "loan_to_purchase_pct")
    rehab_fin = _pct(p, "rehab_financed_pct")

    loan_purchase = purchase * ltp
    loan_rehab = rehab * rehab_fin
    loan = loan_purchase + loan_rehab

    points = loan * _pct(p, "points_pct")
    # Rehab draws fund progressively, so the rehab tranche carries roughly half
    # a full term of interest on average.
    monthly_rate = _pct(p, "interest_rate_pct") / 12.0
    interest = (loan_purchase * monthly_rate * months
                + loan_rehab * monthly_rate * months * 0.5)

    down = purchase - loan_purchase
    total = points + interest
    return {
        "cash": False,
        "loan_amount": round(loan),
        "down_payment": round(down),
        "points": round(points),
        "interest": round(interest),
        "total": round(total),
        "_total": total,
        "_cash_in_deal": down + (rehab - loan_rehab),
    }


def evaluate(purchase: float, arv: float, rehab: float, p: dict) -> dict:
    """Full P&L for buying at `purchase` and reselling at `arv`."""
    sell = selling_costs(arv, p)
    hold = holding_costs(p)
    fin = financing_costs(purchase, rehab, p)

    buy_closing = (purchase * _pct(p, "buy_closing_pct")
                   + purchase * _pct(p, "buy_transfer_tax_pct")
                   + _val(p, "buy_closing_flat")
                   + _val(p, "inspection_flat"))

    total_cost = purchase + buy_closing + rehab + hold["_total"] + fin["_total"]
    net_proceeds = arv - sell["_total"]
    profit = net_proceeds - total_cost

    cash_in = fin["_cash_in_deal"] + buy_closing + hold["_total"] + fin["_total"]
    roi = profit / cash_in if cash_in > 0 else 0.0
    months = max(_val(p, "hold_months"), 1)
    annualized = roi * (12.0 / months)

    return {
        "purchase_price": round(purchase),
        "buy_closing": round(buy_closing),
        "rehab": round(rehab),
        "holding": hold,
        "financing": fin,
        "selling": sell,
        "total_project_cost": round(total_cost),
        "net_sale_proceeds": round(net_proceeds),
        "profit": round(profit),
        "profit_margin_pct": round(profit / arv * 100, 1) if arv else 0,
        "cash_required": round(cash_in),
        "roi_pct": round(roi * 100, 1),
        "annualized_roi_pct": round(annualized * 100, 1),
    }


def target_profit(arv: float, p: dict) -> float:
    return max(arv * _pct(p, "target_profit_pct"), _val(p, "min_profit_flat"))


def max_allowable_offer(arv: float, rehab: float, p: dict) -> float:
    """Solve for the purchase price where profit exactly equals the target.

    Profit falls monotonically as purchase price rises, so a bisection search
    converges reliably regardless of how the cost structure is configured.
    """
    goal = target_profit(arv, p)
    lo, hi = 0.0, max(arv, 1.0)

    if evaluate(lo, arv, rehab, p)["profit"] < goal:
        return 0.0  # deal fails even if the house were free

    for _ in range(80):
        mid = (lo + hi) / 2.0
        if evaluate(mid, arv, rehab, p)["profit"] >= goal:
            lo = mid
        else:
            hi = mid
    return lo


def seventy_percent_rule(arv: float, rehab: float, pct: float = 0.70) -> float:
    return max(0.0, arv * pct - rehab)


def verdict(profit_at_ask: float, mao: float, asking: float | None,
            arv: float, confidence: int, max_profit: float | None = None,
            downside_profit: float | None = None,
            arv_low: float | None = None) -> dict:
    """Plain-language call on whether the deal is worth pursuing.

    `downside_profit` is the profit at the max offer if the ARV lands at the
    low end of the comp range. A deal that loses money there is not a clean
    buy however good the headline spread looks, so it is never reported as an
    unqualified PURSUE.
    """
    if mao <= 0:
        detail = ("This property cannot hit your profit target at any purchase "
                  "price - the renovation and carrying costs alone consume the "
                  "resale value.")
        if max_profit is not None:
            detail += (f" Even acquired for nothing it would clear only "
                       f"${max_profit:,.0f}.")
        detail += (" Reduce the renovation scope, or treat this as a rental "
                   "rather than a flip.")
        return {"call": "NOT VIABLE", "detail": detail, "tone": "bad",
                "gap_to_asking": None, "gap_pct": None}

    if asking is None or asking <= 0:
        return {"call": "NO ASKING PRICE",
                "detail": "Enter the asking price to see how it compares to your max offer.",
                "tone": "neutral"}

    gap = mao - asking
    gap_pct = gap / asking * 100 if asking else 0
    margin = profit_at_ask / arv * 100 if arv else 0

    if gap >= 0:
        call, tone = "PURSUE", "good"
        detail = (f"Your max offer is ${gap:,.0f} above asking. The deal clears your "
                  f"profit target at list price. Offer at or below ${mao:,.0f}.")
    elif gap_pct > -12:
        call, tone = "NEGOTIABLE", "warn"
        detail = (f"Asking is ${abs(gap):,.0f} ({abs(gap_pct):.0f}%) over your max offer. "
                  f"Within normal negotiating range - open below ${mao:,.0f} and hold firm.")
    elif gap_pct > -30:
        call, tone = "THIN", "warn"
        detail = (f"Asking is ${abs(gap):,.0f} ({abs(gap_pct):.0f}%) over your max. "
                  "You would need a major price concession or a cheaper rehab scope.")
    else:
        call, tone = "PASS", "bad"
        detail = (f"Asking is ${abs(gap):,.0f} ({abs(gap_pct):.0f}%) over your max offer. "
                  "The spread is not there at any realistic negotiation.")

    if confidence < 45:
        detail += (" Note: ARV confidence is low, so treat these numbers as a screen, "
                   "not a decision.")
    if margin < 0 and call != "PASS":
        detail += f" At full asking price this loses ${abs(profit_at_ask):,.0f}."

    # The headline spread can look healthy while the downside is a loss. Never
    # let that read as a clean buy.
    if downside_profit is not None and downside_profit < 0 and call in ("PURSUE", "NEGOTIABLE"):
        call = "PURSUE WITH CAUTION" if call == "PURSUE" else "NEGOTIABLE - FRAGILE"
        tone = "warn"
        low_txt = f" (${arv_low:,.0f})" if arv_low else ""
        detail += (f" But the margin is fragile: if the resale lands at the low end "
                   f"of the comp range{low_txt} this loses "
                   f"${abs(downside_profit):,.0f}. Underwrite to the low ARV, not "
                   "the midpoint.")

    return {"call": call, "detail": detail, "tone": tone,
            "gap_to_asking": round(gap), "gap_pct": round(gap_pct, 1),
            "downside_negative": bool(downside_profit is not None and downside_profit < 0)}
