"""Risk flags -- the things that make a good-looking spread evaporate.

The deal maths is only as honest as its inputs. A property priced far under its
neighbours is not a free lunch; it is a question. These flags turn the quiet
assumptions in the model into explicit items to verify before offering.
"""
from __future__ import annotations

# Asking price this far below the comp-implied value per sqft is a condition signal.
DEEP_DISCOUNT_RATIO = 0.60
SEVERE_DISCOUNT_RATIO = 0.45


def build(result: dict) -> list[dict]:
    """Return a list of {level, title, detail} warnings for an analysis."""
    flags: list[dict] = []
    subj = result["subject"]
    arv = result["arv"]
    reno = result["rehab"]
    asking = result.get("asking_price")
    sqft = subj.get("sqft") or 0

    def add(level, title, detail):
        flags.append({"level": level, "title": title, "detail": detail})

    # --- Pricing anomaly: the single most important flag --------------------
    if asking and sqft and arv.get("arv_psf"):
        ask_psf = asking / sqft
        ratio = ask_psf / arv["arv_psf"]
        if ratio < SEVERE_DISCOUNT_RATIO:
            add("high", "Asking price is far below the neighbourhood",
                f"At ${ask_psf:,.0f}/sqft the asking price is only "
                f"{ratio * 100:.0f}% of the ${arv['arv_psf']:,.0f}/sqft the comps "
                "support. A discount this deep is rarely a bargain - it usually "
                "means fire, flood, structural or foundation damage, a hoarder "
                "clean-out, an occupied property, or a title problem. Verify why "
                "before trusting any renovation tier below 'heavy'.")
        elif ratio < DEEP_DISCOUNT_RATIO:
            add("medium", "Asking price is well below the neighbourhood",
                f"${ask_psf:,.0f}/sqft against a ${arv['arv_psf']:,.0f}/sqft market "
                f"({ratio * 100:.0f}%). Expect real condition issues; confirm the "
                "renovation scope with a walkthrough before offering.")

    # --- Sale prices that are not actually sale prices ---------------------
    proxy = arv.get("price_proxy_share", 0)
    if proxy >= 50:
        add("high", "Comps priced from list, not recorded sales",
            f"{proxy}% of these comps have no recorded sale price. This is a "
            "non-disclosure state - sale prices are not public record, so the "
            "figures shown are the prices these homes were LISTED at when they "
            "sold. Homes usually transact within a few percent of final list, "
            "but in a soft market they close below it, which would make this ARV "
            "optimistic. Verify with an agent who has MLS sold access.")
    elif proxy > 0:
        add("medium", "Some comps priced from list, not recorded sales",
            f"{proxy}% of these comps use the final list price because no sale "
            "price is on record. Treat the ARV as slightly softer than the "
            "confidence score suggests.")

    # --- Confidence in the ARV itself --------------------------------------
    if arv["confidence"] < 45:
        add("high", "Low confidence in the ARV",
            f"Confidence {arv['confidence']}/100 from {arv['comp_count']} comps. "
            "Treat this as a screening number only, not a basis for an offer.")
    elif arv["confidence"] < 60:
        add("medium", "Moderate confidence in the ARV",
            f"Confidence {arv['confidence']}/100. Worth pulling the comps manually "
            "before committing.")

    # The adjustments should refine the comps, not overrule them.
    drift = arv.get("arv_vs_comps_pct", 0)
    if abs(drift) > 12:
        direction = "above" if drift > 0 else "below"
        add("medium" if abs(drift) > 20 else "low",
            f"ARV sits {abs(drift):.0f}% {direction} what the comps actually sold for",
            f"The comps sold at a median ${arv.get('comp_median_psf', 0):,.0f}/sqft but "
            f"this ARV works out to ${arv.get('arv_psf', 0):,.0f}/sqft. The adjustments "
            "are doing a lot of the work, which happens when the subject differs from "
            "everything around it. Sanity-check the ARV against raw sale prices before "
            "relying on it.")

    if arv["dispersion_pct"] > 22:
        add("medium", "Comps disagree with each other",
            f"Adjusted comp values vary by {arv['dispersion_pct']:.0f}%. The "
            f"${arv['arv_low']:,} to ${arv['arv_high']:,} range is wide - the "
            "neighbourhood may be mixed, so lean on the low end.")

    if arv["comp_count"] < 5:
        add("medium", "Thin comp set",
            f"Only {arv['comp_count']} usable comparable sales. Fewer comps means "
            "a single unusual sale can move the ARV materially.")

    crit = arv.get("criteria", {})
    if crit.get("radius_mi", 0) >= 2.0:
        add("medium", "Comps pulled from far away",
            f"Had to search {crit['radius_mi']} miles to find enough sales. "
            "Values can change street by street; confirm these comps are in the "
            "same school district and neighbourhood.")

    over = [c for c in arv["comps"] if c.get("over_adjusted")]
    if len(over) >= max(3, arv["comp_count"] // 2):
        add("medium", "Comps required heavy adjustment",
            f"{len(over)} of {arv['comp_count']} comps needed more than 25% gross "
            "adjustment, meaning they differ materially from the subject.")

    # --- Renovation --------------------------------------------------------
    if reno.get("warning"):
        add("high", "Renovation scope may be uneconomic", reno["warning"])

    # --- Property age ------------------------------------------------------
    yb = subj.get("year_built")
    if yb:
        if yb < 1950:
            add("medium", "Pre-1950 construction",
                f"Built {yb:.0f}. Budget for knob-and-tube wiring, galvanised or "
                "lead supply lines, asbestos in flooring and pipe wrap, plaster "
                "walls, and a possible undersized electrical service.")
        elif yb < 1978:
            add("low", "Pre-1978 construction",
                f"Built {yb:.0f}. Federal lead-paint rules apply - disclosure is "
                "mandatory and disturbing painted surfaces requires an "
                "RRP-certified contractor.")

    # --- Data quality ------------------------------------------------------
    if not subj.get("sqft"):
        add("high", "No square footage on record",
            "Living area drives both the ARV and the renovation budget. Enter it "
            "manually from the listing or county records.")
    if not subj.get("year_built"):
        add("low", "No year built on record",
            "Age adjustments were skipped, which slightly widens the ARV range.")

    # --- Market direction --------------------------------------------------
    trend = arv.get("market_trend_annual_pct", 0)
    if trend < -3:
        add("medium", "Prices are falling in this market",
            f"Local sales imply {trend:.1f}% per year. Your resale will land into "
            "a weaker market than today's comps, so hold the low ARV.")

    # --- Jurisdiction cost notes -------------------------------------------
    loc = result.get("location") or {}
    for note in loc.get("notes", []):
        add("info", f"Transaction costs in {loc.get('state') or 'this area'}", note)
    if loc.get("property_tax_source") == "state rate":
        add("low", "Property tax is estimated",
            f"No tax figure on the listing, so holding costs assume the "
            f"{loc.get('property_tax_pct')}% state effective rate "
            f"(${loc.get('annual_taxes_used', 0):,}/yr). Check the county record.")

    # --- Listing dynamics (leverage, not risk) -----------------------------
    dom = subj.get("days_on_mls")
    if dom and dom > 90:
        add("info", "Listing has been sitting",
            f"{dom:.0f} days on market. Stale listings carry negotiating leverage - "
            "a low offer is more likely to be entertained.")

    # --- County tax roll: is the owner actually paying? -------------------
    tr = result.get("tax_roll") or {}
    if tr.get("found"):
        n = tr.get("years_behind") or 0
        liens = tr.get("special_assessments_owed") or 0
        if n >= 2 or liens:
            bits = []
            if n:
                bits.append(f"{n} year(s) of property tax unpaid "
                            f"(${tr.get('tax_owed', 0):,.0f} owed)")
            if liens:
                bits.append(f"${liens:,.0f} in city special assessments - code "
                            "enforcement has already been out")
            add("info", "Owner is behind with the county",
                "; ".join(bits) + f". Owner of record: {tr.get('owner')}. "
                + ("Three years unpaid makes the parcel eligible for the June "
                   "resale, which is real leverage. " if n >= 3 else "")
                + "Expect every dollar owed to come off the top at closing.")
        elif n == 1:
            add("info", "Last year's property tax is unpaid",
                f"${tr.get('tax_owed', 0):,.0f} owed. Mild pressure; the owner may "
                "simply be late, but it is worth knowing before you negotiate.")

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    flags.sort(key=lambda f: order.get(f["level"], 9))
    return flags
