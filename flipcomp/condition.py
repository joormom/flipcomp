"""Condition from listing language -- renovated, distressed, or neither.

An ARV is the price a house fetches *after* it is fixed, so it should be built
from sales of fixed houses. A neighbourhood's sales mix both: the flipped
ranch that sold for $190k and the estate sale next door that went for $85k.
Averaging them answers neither question. Splitting them answers both: the
renovated sales give the ARV, the distressed ones give the as-is value, and
the gap between them is what the market actually pays for a renovation.

Patterns are written for listing copy, which is formulaic. Anything that
reads as both ('fully updated, sold as-is') is 'mixed' and treated as neutral.
"""
from __future__ import annotations

import re

RENOVATED = re.compile(
    r"\b(fully|completely|totally|newly|beautifully|recently|tastefully|extensively) "
    r"(renovated|remodel(l)?ed|updated|restored|rehabbed)"
    r"|\b(renovated|remodel(l)?ed) (kitchen|bath|bathroom|home|house)"
    r"|\bnew (kitchen|roof|hvac|ac unit|furnace|flooring|floors|cabinets|countertops|"
    r"interior paint|windows|plumbing|electrical|water heater|appliances)"
    r"|\bmove[- ]in ready|\bturn[- ]?key|\bdown to the studs|\bupdated throughout"
    r"|\bquartz|\bgranite|\bluxury vinyl|\blvp\b|\blvt\b|\bshiplap|\bsubway tile"
    r"|\bnothing to do but move|\bcompletely redone",
    re.I,
)

DISTRESSED = re.compile(
    r"\binvestor|\bhandyman|\bfixer|\btlc\b|\bas[- ]is\b|\bsold as\b|\bwhere[- ]is\b"
    r"|\bneeds (some |a little |a lot of )?(work|repairs?|updating|love|tlc|renovation)"
    r"|\bcash (only|buyers?|offers? only)|\bestate sale|\bforeclos|\bbank[- ]owned"
    r"|\bshort sale|\bsweat equity|\bbring your (tools|vision|contractor|imagination)"
    r"|\bdiamond in the rough|\bfire[- ]damage|\bwater damage|\bfoundation (issue|problem|repair)"
    r"|\bwill not (finance|qualify)|\bnot (fha|va) (eligible|approvable)|\bcondemned",
    re.I,
)

LABELS = ("renovated", "neutral", "mixed", "distressed")


def classify(text) -> str:
    t = text if isinstance(text, str) else ""
    if not t:
        return "neutral"
    r, d = bool(RENOVATED.search(t)), bool(DISTRESSED.search(t))
    if r and d:
        return "mixed"
    if r:
        return "renovated"
    if d:
        return "distressed"
    return "neutral"


def evidence(text, label: str) -> str | None:
    """The phrase that decided it, for display."""
    t = text if isinstance(text, str) else ""
    pat = RENOVATED if label == "renovated" else DISTRESSED if label == "distressed" else None
    if not pat:
        return None
    m = pat.search(t)
    return m.group(0) if m else None


# How much each condition counts toward each estimate. A renovated sale is the
# best evidence of what the subject will fetch once fixed, and nearly useless
# for what it is worth today; a distressed sale is the reverse.
# Renovated weight tuned by time-ordered backtest (Oct 2026, 1,040 sales): 8.0
# beat 2, 3 and 5 on the training half and held up on the newer half.
ARV_WEIGHT = {"renovated": 8.0, "mixed": 1.2, "neutral": 1.0, "distressed": 0.10}
ASIS_WEIGHT = {"renovated": 0.05, "mixed": 0.5, "neutral": 0.30, "distressed": 3.0}

# An as-is value built mostly from ordinary listings is just market value. It
# takes real distressed sales to say what an unfixed house fetches.
MIN_DISTRESSED_FOR_ASIS = 3
