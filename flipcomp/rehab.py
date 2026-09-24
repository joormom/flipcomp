"""Renovation cost estimation.

Two ways in: pick a scope tier and get a $/sqft budget, or add specific big-ticket
line items on top. Everything is overridable because rehab pricing is intensely
local -- these are national-average starting points, not quotes.
"""
from __future__ import annotations

# Base scope tiers, dollars per finished square foot.
#
# These are INVESTOR-GRADE flip costs, not retail remodel costs. A flipper
# buying stock cabinets from a wholesaler, using LVP throughout and running
# investor-friendly crews on volume pricing pays a fraction of what a homeowner
# pays a general contractor for a custom kitchen. Pricing this model off
# homeowner remodel data (HomeAdvisor, Cost vs Value) overstates a flip budget
# by roughly 40-50%.
#
# Calibrated to the operator's own actuals: a standard rehab (full kitchen,
# baths, flooring, paint on a 1,500-2,000 sqft house) runs $30-40/sqft ALL-IN
# including contingency. The "moderate" tier is pinned to land in that band at
# a typical market, and the other tiers are spaced around it.
#
# Base rates below are BEFORE the market factor and BEFORE contingency.
TIERS = {
    "cosmetic": {
        "label": "Cosmetic",
        "psf": 13,
        "desc": "Paint, LVP flooring, light fixtures, hardware, landscaping, "
                "deep clean. Systems and layout untouched.",
    },
    "light": {
        "label": "Light",
        "psf": 21,
        "desc": "Cosmetic plus kitchen and bath refresh (stock cabinets, "
                "laminate or butcher-block counters, new vanities), appliances, "
                "minor drywall.",
    },
    "moderate": {
        "label": "Moderate",
        "psf": 30,
        "desc": "Full kitchen and bathroom replacement, all flooring, all paint, "
                "some window and door replacement, one major system.",
    },
    "heavy": {
        "label": "Heavy",
        "psf": 44,
        "desc": "Moderate plus roof, HVAC, electrical and plumbing updates, "
                "windows throughout, exterior work.",
    },
    "gut": {
        "label": "Gut rehab",
        "psf": 65,
        "desc": "Down to the studs. New everything including systems, possible "
                "structural and layout changes, permits throughout.",
    },
}

# Big-ticket items priced separately from the $/sqft tier, at investor pricing.
LINE_ITEMS = {
    "roof":            {"label": "Roof replacement",       "kind": "psf_roof", "rate": 5.0},
    "hvac":            {"label": "HVAC system",            "kind": "flat",     "rate": 6_500},
    "electrical":      {"label": "Full rewire",            "kind": "psf",      "rate": 5.0},
    "plumbing":        {"label": "Repipe",                 "kind": "psf",      "rate": 4.5},
    "windows":         {"label": "Windows (whole house)",  "kind": "psf",      "rate": 6.0},
    "foundation":      {"label": "Foundation repair",      "kind": "flat",     "rate": 12_000},
    "sewer_septic":    {"label": "Sewer / septic",         "kind": "flat",     "rate": 10_000},
    "siding":          {"label": "Siding",                 "kind": "psf",      "rate": 7.5},
    "water_heater":    {"label": "Water heater",           "kind": "flat",     "rate": 1_400},
    "mold_asbestos":   {"label": "Mold / asbestos abatement", "kind": "flat",  "rate": 6_000},
    "addition_permit": {"label": "Permits & architectural","kind": "flat",     "rate": 3_500},
    "landscaping":     {"label": "Landscaping / exterior", "kind": "flat",     "rate": 3_000},
}

DEFAULT_CONTINGENCY = 0.15  # flips find surprises; 15% is the standard buffer

# The tier rates above are national averages that implicitly assume a roughly
# $200/sqft finished market. Rehab cost tracks the local price point, but only
# partly: a sheet of drywall, a toilet and a box of LVP cost about the same in
# Cleveland as in San Jose. Only labour genuinely varies by market, and labour
# is roughly half of a rehab budget.
#
# So the market adjustment is applied to the labour share only. Scaling the
# whole cost -- as a naive $/sqft multiplier does -- double-discounts cheap
# markets into impossible budgets and inflates expensive ones past reality.
BASELINE_MARKET_PSF = 200.0
MARKET_FACTOR_MIN = 0.45
MARKET_FACTOR_MAX = 1.9
LABOUR_SHARE = 0.50          # industry guidance puts labour at 40-60% of a rehab
MATERIAL_SHARE = 1.0 - LABOUR_SHARE

# Renovation spend above this share of ARV is rarely recoverable on a flip.
UNECONOMIC_RATIO = 0.50


def market_factor(market_psf: float | None) -> float:
    """Local cost multiplier, applied to the labour share of a rehab budget.

    The square root keeps the adjustment directional without letting extreme
    markets produce absurd numbers; blending against a fixed material share
    then keeps the result anchored to what materials actually cost.
    """
    if not market_psf or market_psf <= 0:
        return 1.0
    raw = (market_psf / BASELINE_MARKET_PSF) ** 0.5
    labour = max(MARKET_FACTOR_MIN, min(MARKET_FACTOR_MAX, raw))
    return MATERIAL_SHARE + LABOUR_SHARE * labour


def suggest_tier(year_built: float | None, list_price_psf: float | None,
                 market_psf: float | None) -> str:
    """Guess a scope tier from age and how far below market the asking price is.

    A steep discount to market price-per-sqft often signals condition. It is a
    weak signal though -- in distressed markets everything trades below the
    renovated comps -- so it is weighted lightly here. This is a starting point
    to be overridden once the property has actually been walked.
    """
    score = 0
    # Age alone says little about scope -- a well-kept 1930s house can need
    # paint and floors -- so it contributes at most one step, and only pre-war.
    if year_built and year_built < 1940:
        score += 1

    # Discount to market is the stronger signal, but it is noisy: in low-priced
    # markets everything trades below the renovated comps as a matter of course.
    # Only a genuinely steep discount moves the scope two steps.
    if list_price_psf and market_psf and market_psf > 0:
        ratio = list_price_psf / market_psf
        if ratio < 0.35:
            score += 2
        elif ratio < 0.55:
            score += 1

    # Floor is "light" and the ceiling reachable by inference is "heavy".
    # "Cosmetic" and "gut" are deliberate calls only you can make after walking
    # the property, so the suggester never picks them.
    ladder = ["light", "light", "moderate", "heavy"]
    return ladder[min(score, len(ladder) - 1)]


def estimate(sqft: float,
             tier: str = "moderate",
             line_items: list[str] | None = None,
             contingency: float = DEFAULT_CONTINGENCY,
             override_total: float | None = None,
             roof_sqft: float | None = None,
             market_psf: float | None = None,
             arv: float | None = None) -> dict:
    """Build a renovation budget, scaled to the local market price point.

    override_total short-circuits the tier maths -- if the user has a contractor
    bid, that number wins and contingency is applied on top of it.
    """
    line_items = line_items or []
    sqft = float(sqft or 0)
    roof_sqft = float(roof_sqft or sqft * 1.35)  # footprint + pitch + overhang
    factor = market_factor(market_psf)

    breakdown = []

    if override_total is not None:
        base = float(override_total)
        breakdown.append({"label": "Contractor bid / manual estimate", "amount": round(base)})
        tier_used = None
        rate = None
    else:
        tier_used = tier if tier in TIERS else "moderate"
        t = TIERS[tier_used]
        rate = t["psf"] * factor
        base = sqft * rate
        breakdown.append({
            "label": f"{t['label']} scope ({sqft:,.0f} sqft x ${rate:,.0f}/sqft)",
            "amount": round(base),
        })

    extras = 0.0
    for key in line_items:
        item = LINE_ITEMS.get(key)
        if not item:
            continue
        if item["kind"] == "flat":
            amt = item["rate"] * factor
        elif item["kind"] == "psf_roof":
            amt = roof_sqft * item["rate"] * factor
        else:
            amt = sqft * item["rate"] * factor
        extras += amt
        breakdown.append({"label": item["label"], "amount": round(amt)})

    subtotal = base + extras
    buffer = subtotal * contingency
    breakdown.append({
        "label": f"Contingency ({contingency * 100:.0f}%)",
        "amount": round(buffer),
    })

    total = subtotal + buffer

    warning = None
    if arv and arv > 0 and total / arv > UNECONOMIC_RATIO:
        warning = (
            f"This scope costs {total / arv * 100:.0f}% of the property's ARV. "
            "Renovation that heavy rarely pays back on a flip - either the scope "
            "is wrong for this price point or the property is a rental/hold, not "
            "a flip."
        )

    return {
        "tier": tier_used,
        "tier_label": TIERS[tier_used]["label"] if tier_used else "Manual",
        "rate_psf": round(rate, 2) if rate else None,
        "market_factor": round(factor, 2),
        "subtotal": round(subtotal),
        "contingency_pct": round(contingency * 100),
        "contingency": round(buffer),
        "total": round(total),
        "psf_all_in": round(total / sqft, 2) if sqft else None,
        "pct_of_arv": round(total / arv * 100, 1) if arv else None,
        "warning": warning,
        "breakdown": breakdown,
    }
