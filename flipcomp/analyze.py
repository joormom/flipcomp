"""Top-level orchestration: address in, full flip analysis out."""
from __future__ import annotations

from . import flags as riskflags
from . import accuracy, offer, rehab, rental, states
from .compengine import run_comps
from .data import DataError, fetch_sold_pool, geocode, lookup_subject


def analyze(address: str,
            asking_price: float | None = None,
            overrides: dict | None = None) -> dict:
    """Run the complete pipeline for one property.

    overrides may contain:
      subject_sqft, subject_beds, subject_full_baths, subject_year_built
      rehab_tier, rehab_line_items, rehab_contingency_pct, rehab_override_total
      plus any key from offer.DEFAULTS
    """
    o = dict(overrides or {})

    # --- 1. Resolve the subject property --------------------------------
    off_market = False
    try:
        subject = lookup_subject(address)
    except DataError as not_listed:
        # Realtor.com only knows homes that have been listed. Tax-roll and
        # court-record leads usually have not been, so place the address by
        # geocoder and comp it from what the user tells us about the house.
        subject = _offmarket_subject(address, o)
        if subject is None:
            raise not_listed
        off_market = True

    for field, key in (("sqft", "subject_sqft"), ("beds", "subject_beds"),
                       ("full_baths", "subject_full_baths"),
                       ("year_built", "subject_year_built"),
                       ("lot_sqft", "subject_lot_sqft")):
        if o.get(key):
            subject[field] = float(o[key])

    if asking_price is None:
        asking_price = subject.get("list_price")

    # --- 2. Comps -> ARV -------------------------------------------------
    pool = fetch_sold_pool(subject)
    comp_result = run_comps(pool, subject)
    arv = float(comp_result["arv"])
    _ck = (subject.get("county") or "").lower().replace(" county", "").strip()
    from . import focus as _focus
    _sd = _focus.district_of(subject.get("latitude"), subject.get("longitude"))
    subject["school_district"], subject["school"] = _sd, _focus.label(_sd)
    # A focus district's own backtest beats the county average when we have it.
    comp_result["accuracy"] = (accuracy.cached(_sd) if _sd else None) or accuracy.cached(_ck)
    comp_result["accuracy_text"] = accuracy.describe(comp_result["accuracy"])

    # --- 3. Renovation budget -------------------------------------------
    sqft = float(subject.get("sqft") or 0)
    list_psf = (asking_price / sqft) if (asking_price and sqft) else None
    suggested = rehab.suggest_tier(subject.get("year_built"), list_psf,
                                   comp_result.get("arv_psf"))
    tier = o.get("rehab_tier") or suggested

    contingency = o.get("rehab_contingency_pct")
    contingency = (float(contingency) / 100.0) if contingency is not None \
        else rehab.DEFAULT_CONTINGENCY

    reno = rehab.estimate(
        sqft=sqft,
        tier=tier,
        line_items=o.get("rehab_line_items") or [],
        contingency=contingency,
        override_total=o.get("rehab_override_total"),
        market_psf=comp_result.get("arv_psf"),
        arv=arv,
    )
    reno["suggested_tier"] = suggested
    reno["suggested_reason"] = _tier_reason(subject.get("year_built"), list_psf,
                                            comp_result.get("arv_psf"), suggested)

    # --- 4. Deal math ----------------------------------------------------
    params = {k: v for k, v in o.items() if k in offer.DEFAULTS}

    # State-level transaction costs, unless the user has set them explicitly.
    loc = states.resolve(subject.get("state"), subject.get("city"),
                         subject.get("county"))
    if "transfer_tax_pct" not in params:
        params["transfer_tax_pct"] = loc["sell_transfer_pct"]
    if "buy_transfer_tax_pct" not in params:
        params["buy_transfer_tax_pct"] = loc["buy_transfer_pct"]

    # The county treasurer's roll, when this county is on it: the real tax
    # bill, plus whether the owner is actually paying it.
    tax_roll = _tax_roll(subject)

    # An actual tax bill beats a modelled rate: user entry, then the county
    # roll, then the listing, then the state's effective rate.
    tax_source = "listing"
    if params.get("annual_taxes") in (None, ""):
        if tax_roll and tax_roll.get("annual_tax"):
            params["annual_taxes"] = tax_roll["annual_tax"]
            tax_source = "county roll"
        elif subject.get("tax"):
            params["annual_taxes"] = subject["tax"]
        else:
            params["annual_taxes"] = arv * loc["property_tax_pct"] / 100.0
            tax_source = "state rate"
    else:
        tax_source = "manual"

    # Hold period from the work and the local market, not a flat guess: how
    # long the renovation takes, how long renovated homes here sit before they
    # go under contract, and a month to close.
    hold = _hold_period(reno.get("tier"), comp_result)
    if "hold_months" not in params:
        params["hold_months"] = hold["months"]
    else:
        hold["source"] = "manual"

    rehab_total = float(reno["total"])
    mao = offer.max_allowable_offer(arv, rehab_total, params)
    mao_deal = offer.evaluate(mao, arv, rehab_total, params)
    rule70 = offer.seventy_percent_rule(arv, rehab_total)

    at_ask = offer.evaluate(float(asking_price), arv, rehab_total, params) \
        if asking_price else None

    # Sensitivity: what the deal looks like if ARV lands low or rehab runs over.
    stress = {
        "arv_low": offer.evaluate(mao, float(comp_result["arv_low"]), rehab_total, params),
        "rehab_overrun_25": offer.evaluate(mao, arv, rehab_total * 1.25, params),
        "both": offer.evaluate(mao, float(comp_result["arv_low"]), rehab_total * 1.25, params),
    }

    feasible = mao > 0
    # When no price clears the target, the best case is acquiring for nothing.
    max_profit = mao_deal["profit"] if not feasible else None

    call = offer.verdict(
        at_ask["profit"] if at_ask else 0.0,
        mao, asking_price, arv, comp_result["confidence"],
        max_profit=max_profit,
        downside_profit=stress["arv_low"]["profit"] if feasible else None,
        arv_low=comp_result["arv_low"],
    )

    # At an auction the asking price is only where bidding opens, so "clears
    # your target at asking" is meaningless. Turn the max offer into a max bid.
    if riskflags.is_auction_listing(subject) and feasible:
        bid = mao / 1.10
        call = {"call": "AUCTION - BID UP TO " + f"${bid:,.0f}", "tone": "warn",
                "detail": (f"This is an auction; the listed ${asking_price:,.0f} is the opening bid. "
                           f"Your maximum offer of ${mao:,.0f} allows for a 10% buyer's premium "
                           f"at a hammer price of about ${bid:,.0f} - stop bidding there. "
                           "Check the actual premium, deposit and closing terms first.")
                if asking_price else f"Auction. Stop bidding at about ${bid:,.0f}.",
                "gap_to_asking": None, "gap_pct": None}

    # A little negotiating room under the max, floored so it stays sane.
    opening = max(0.0, min(mao * 0.92, asking_price * 0.97)) if asking_price \
        else mao * 0.92

    # --- 5. Other exits ---------------------------------------------------
    # Wholesale: put it under contract and assign it to a flipper. They will
    # pay roughly the 70%-rule price; the fee is the gap above your contract.
    wholesale_fee_target = max(5_000.0, min(15_000.0, arv * 0.05))
    wholesale = {
        "investor_price": round(rule70),
        "fee_target": round(wholesale_fee_target),
        "max_contract_price": round(max(0.0, rule70 - wholesale_fee_target)),
        "works_at_asking": bool(asking_price and rule70 - wholesale_fee_target >= asking_price),
    }

    rent_taxes = arv * loc["property_tax_pct"] / 100.0  # sale resets the assessment
    try:
        rental_exit = rental.analyse(subject, arv, rehab_total, reno.get("tier"),
                                     mao if feasible else None, asking_price,
                                     {**offer.DEFAULTS, **params,
                                      **{k: o[k] for k in ("rent_override", "management_pct",
                                                           "refi_rate_pct", "refi_ltv_pct")
                                         if o.get(k) not in (None, "")}}, rent_taxes)
    except Exception as exc:  # never let the rental view break the analysis
        rental_exit = {"available": False, "reason": f"Rental analysis failed: {exc}"}

    result = {
        "subject": subject,
        "off_market": off_market,
        "hold": hold,
        "wholesale": wholesale,
        "rental": rental_exit,
        "asking_price": round(asking_price) if asking_price else None,
        "arv": comp_result,
        "rehab": reno,
        "offer": {
            "mao": round(mao),
            "feasible": feasible,
            "max_profit_if_free": round(max_profit) if max_profit is not None else None,
            "suggested_opening": round(opening) if feasible else None,
            "rule_70": round(rule70),
            "target_profit": round(offer.target_profit(arv, params)),
            "at_mao": mao_deal,
            "at_asking": at_ask,
            "stress": stress,
        },
        "verdict": call,
        "location": {**loc, "property_tax_source": tax_source,
                     "annual_taxes_used": round(float(params["annual_taxes"]))},
        "tax_roll": tax_roll,
        "assumptions": {**offer.DEFAULTS, **params},
    }
    result["flags"] = riskflags.build(result)
    return result


def _hold_period(tier: str | None, comp_result: dict) -> dict:
    rehab_m = rental.REHAB_MONTHS.get(tier or "moderate", 2.5)
    doms = [c.get("days_on_mls") for c in comp_result.get("comps", [])
            if c.get("condition") == "renovated" and c.get("days_on_mls")]
    if len(doms) < 3:
        doms = [c.get("days_on_mls") for c in comp_result.get("comps", []) if c.get("days_on_mls")]
    dom = float(sorted(doms)[len(doms) // 2]) if doms else 60.0
    sell_m = dom / 30.4
    months = round(rehab_m + sell_m + 1.0, 1)
    return {"months": months, "rehab_months": rehab_m, "market_days": round(dom),
            "closing_months": 1.0, "source": "computed",
            "detail": f"{rehab_m:g} mo renovation + {dom:.0f} days on market + 1 mo to close"}


def _offmarket_subject(address: str, o: dict) -> dict | None:
    """Build a subject record for an address with no listing history.

    Needs the living area from the user -- there is no public, keyless source
    for it -- but everything else (location, owner, tax bill) is recoverable.
    """
    geo = geocode(address)
    if not geo:
        return None
    sqft = o.get("subject_sqft")
    if not sqft:
        raise DataError(
            f"'{geo.get('formatted_address') or address}' has never been listed, so "
            "there is no record of its size. Enter the square footage under "
            "'Subject sqft override' (county assessor or a drive-by estimate: a "
            "typical 3/2 ranch is 1,100-1,500 sqft) and run it again. Location, "
            "owner and tax bill are pulled automatically."
        )
    subject = {
        "formatted_address": geo.get("formatted_address") or address,
        "street": geo.get("street"),
        "city": geo.get("city"), "state": geo.get("state"), "zip_code": geo.get("zip_code"),
        "county": None,
        "latitude": geo["latitude"], "longitude": geo["longitude"],
        "sqft": float(sqft),
        "beds": float(o["subject_beds"]) if o.get("subject_beds") else None,
        "full_baths": float(o["subject_full_baths"]) if o.get("subject_full_baths") else None,
        "half_baths": None,
        "year_built": float(o["subject_year_built"]) if o.get("subject_year_built") else None,
        "lot_sqft": float(o["subject_lot_sqft"]) if o.get("subject_lot_sqft") else None,
        "style": "SINGLE_FAMILY",
        "list_price": None, "tax": None, "days_on_mls": None, "status": "OFF_MARKET",
        "property_id": None, "property_url": None, "parking_garage": None,
    }
    # County comes from the region table when the city is known there.
    from . import regions
    city = (subject.get("city") or "").lower()
    for key, rec in regions.COUNTIES.items():
        if city and city == rec["seat"].lower():
            subject["county"] = rec["name"].replace(", OK", "")
            break
    return subject


def _tax_roll(subject: dict) -> dict | None:
    """Best-effort treasurer lookup. Never allowed to fail the analysis."""
    if (subject.get("state") or "").upper() != "OK":
        return None
    county = (subject.get("county") or "").lower().replace(" county", "").strip()
    address = subject.get("formatted_address") or ""
    if not county or not address:
        return None
    try:
        from . import taxroll
        d = taxroll.delinquency_for_address(county, address)
    except Exception:
        return None
    if not d.get("found"):
        return None
    d["county"] = county
    return d


def _tier_reason(year_built, list_psf, market_psf, tier) -> str:
    bits = []
    if year_built:
        bits.append(f"built {year_built:.0f}")
    if list_psf and market_psf:
        ratio = list_psf / market_psf
        bits.append(f"asking ${list_psf:,.0f}/sqft vs ${market_psf:,.0f}/sqft market "
                    f"({ratio * 100:.0f}% of market)")
    if not bits:
        return "Default scope - no age or pricing signal available."
    return ("Suggested from " + "; ".join(bits)
            + ". Override this once you have walked the property.")
