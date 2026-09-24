"""Per-state transaction cost defaults.

Transfer tax and property tax vary by roughly an order of magnitude across the
US, which moves a maximum offer by five figures. These are state-level starting
points, not quotes -- county and municipal add-ons are common, and the worst
offenders are listed in LOCALITIES.

Two things matter more than the table itself:

  * Property taxes. When the listing carries an actual annual tax figure, the
    analyser uses that and ignores the rate here entirely. The rate is only a
    fallback.
  * Who pays. Transfer tax has a customary payer that differs by state. A
    flipper is both a buyer and a seller, so in "buyer" and "split" states the
    tax is paid twice per flip -- once on acquisition, once on resale.

Rates are percentages of sale price. Sources: state revenue department
schedules and 2026 industry closing-cost surveys; verify against a local title
company before relying on them for an offer.
"""
from __future__ import annotations

SELLER, BUYER, SPLIT, NONE = "seller", "buyer", "split", "none"

# state -> (transfer_tax_pct, customary_payer, effective_property_tax_pct)
STATES: dict[str, tuple[float, str, float]] = {
    "AL": (0.10, SELLER, 0.38),
    "AK": (0.00, NONE,   1.04),
    "AZ": (0.00, NONE,   0.48),
    "AR": (0.33, SELLER, 0.55),
    "CA": (0.11, SELLER, 0.71),
    "CO": (0.01, BUYER,  0.48),
    "CT": (0.75, SELLER, 1.81),
    "DE": (4.00, SPLIT,  0.50),
    "DC": (2.20, SPLIT,  0.55),
    "FL": (0.70, SELLER, 0.79),
    "GA": (0.10, SELLER, 0.81),
    "HI": (0.20, SELLER, 0.27),
    "ID": (0.00, NONE,   0.49),
    "IL": (0.15, SELLER, 2.01),
    "IN": (0.00, NONE,   0.80),
    "IA": (0.16, SELLER, 1.39),
    "KS": (0.00, NONE,   1.26),
    "KY": (0.10, SELLER, 0.80),
    "LA": (0.00, NONE,   0.55),
    "ME": (0.44, SPLIT,  1.09),
    "MD": (1.50, SPLIT,  0.99),
    "MA": (0.46, SELLER, 1.04),
    "MI": (0.86, SELLER, 1.29),
    "MN": (0.33, SELLER, 1.05),
    "MS": (0.00, NONE,   0.75),
    "MO": (0.00, NONE,   0.88),
    "MT": (0.00, NONE,   0.72),
    "NE": (0.23, SELLER, 1.49),
    "NV": (0.13, SELLER, 0.47),
    "NH": (1.50, SPLIT,  1.66),
    "NJ": (1.00, SELLER, 2.11),
    "NM": (0.00, NONE,   0.73),
    "NY": (0.40, SELLER, 1.55),
    "NC": (0.20, SELLER, 0.72),
    "ND": (0.00, NONE,   0.95),
    "OH": (0.20, SELLER, 1.31),
    "OK": (0.15, SELLER, 0.83),
    "OR": (0.00, NONE,   0.86),
    "PA": (2.00, SPLIT,  1.36),
    "RI": (0.46, SELLER, 1.30),
    "SC": (0.37, SELLER, 0.48),
    "SD": (0.10, SELLER, 1.14),
    "TN": (0.37, BUYER,  0.50),
    "TX": (0.00, NONE,   1.49),
    "UT": (0.00, NONE,   0.52),
    "VT": (1.25, BUYER,  1.59),
    "VA": (0.25, SPLIT,  0.79),
    "WA": (1.28, SELLER, 0.84),
    "WV": (0.22, SELLER, 0.53),
    "WI": (0.30, SELLER, 1.42),
    "WY": (0.00, NONE,   0.57),
}

# Jurisdictions where the local add-on dwarfs the state rate. Keyed by
# (state, lowercase city) or (state, lowercase county). Value overrides the
# state transfer rate and carries a warning.
LOCALITIES: dict[tuple[str, str], tuple[float, str, str]] = {
    ("PA", "philadelphia"): (4.28, SPLIT,
        "Philadelphia adds 3.278% to the 1% state transfer tax."),
    ("PA", "pittsburgh"): (4.00, SPLIT,
        "Pittsburgh's combined transfer tax exceeds 4%."),
    ("IL", "chicago"): (1.05, SPLIT,
        "Chicago adds a 0.75% municipal transfer tax (buyer) to the state and "
        "county rates."),
    ("NY", "new york"): (1.83, SELLER,
        "NYC adds 1.0-1.425% RPTT on top of the 0.4% state tax. Sales over $1M "
        "also carry a buyer-paid mansion tax starting at 1%."),
    ("NY", "brooklyn"): (1.83, SELLER, "NYC transfer tax applies."),
    ("NY", "bronx"): (1.83, SELLER, "NYC transfer tax applies."),
    ("NY", "queens"): (1.83, SELLER, "NYC transfer tax applies."),
    ("NY", "staten island"): (1.83, SELLER, "NYC transfer tax applies."),
    ("CA", "san francisco"): (0.75, SELLER,
        "San Francisco's transfer tax is graduated and reaches 6% on high-value "
        "sales. Confirm the bracket for your price point."),
    ("CA", "los angeles"): (0.56, SELLER,
        "Los Angeles adds a 0.45% city tax; Measure ULA adds 4-5.5% above $5M."),
    ("CA", "oakland"): (1.61, SELLER, "Oakland's graduated city transfer tax starts at 1.5%."),
    ("FL", "miami-dade"): (1.05, SELLER,
        "Miami-Dade charges 0.6% plus surtax on non-single-family transfers."),
    ("MI", "detroit"): (1.11, SELLER, "Detroit adds a city transfer tax."),
    ("WA", "seattle"): (1.78, SELLER,
        "Washington REET is graduated (1.1% to 3.0%) and King County adds 0.5%."),
}

# States where county-level variation is wide enough to be worth verifying.
HIGH_VARIANCE = {"NY", "PA", "CA", "MD", "WA", "IL", "DE", "NV", "CO", "OH", "VA"}

DEFAULT_PROPERTY_TAX_PCT = 1.10  # national average fallback


def buy_pct_preview(rate: float, payer: str) -> str:
    """The share of the transfer tax paid on each side, for display."""
    pct = rate if payer == BUYER else rate / 2.0
    return f"{pct:g}"


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def resolve(state: str | None, city: str | None = None,
            county: str | None = None) -> dict:
    """Return cost defaults for a location.

    Falls back to national averages when the state is unknown.
    """
    code = (state or "").strip().upper()
    entry = STATES.get(code)

    if not entry:
        return {
            "state": code or None,
            "known": False,
            "transfer_tax_pct": 0.50,
            "transfer_payer": SELLER,
            "sell_transfer_pct": 0.50,
            "buy_transfer_pct": 0.00,
            "property_tax_pct": DEFAULT_PROPERTY_TAX_PCT,
            "notes": ["State could not be identified; using national-average "
                      "closing cost assumptions. Verify locally."],
        }

    rate, payer, prop_tax = entry
    notes: list[str] = []

    local = (LOCALITIES.get((code, _norm(city)))
             or LOCALITIES.get((code, _norm(county))))
    if local:
        rate, payer, note = local
        notes.append(note)
    elif code in HIGH_VARIANCE:
        notes.append(
            f"{code} transfer tax varies significantly by county and city. The "
            f"{rate:.2f}% default is a state-level figure - confirm the local rate."
        )

    if payer == SELLER:
        sell_pct, buy_pct = rate, 0.0
    elif payer == BUYER:
        sell_pct, buy_pct = 0.0, rate
    elif payer == SPLIT:
        sell_pct = buy_pct = rate / 2.0
    else:
        sell_pct = buy_pct = 0.0

    if payer in (BUYER, SPLIT) and rate > 0:
        who = ("the buyer customarily pays the transfer tax" if payer == BUYER
               else "the transfer tax is customarily split between buyer and seller")
        notes.append(
            f"In {code} {who}, so a flip pays it twice - {buy_pct_preview(rate, payer)}% "
            "on the way in and again on the way out."
        )

    return {
        "state": code,
        "known": True,
        "transfer_tax_pct": rate,
        "transfer_payer": payer,
        "sell_transfer_pct": sell_pct,
        "buy_transfer_pct": buy_pct,
        "property_tax_pct": prop_tax,
        "notes": notes,
    }
