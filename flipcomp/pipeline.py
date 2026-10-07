"""Deal pipeline -- what you are doing about each property.

Finding a deal is the cheap part. Most acquisitions close on the fourth or
fifth touch, months after the first letter, so the money is in not losing
track. Each property moves through stages, carries its numbers and a dated
log, and has a next follow-up date that the daily report surfaces when due.

Stored in pipeline.json at the project root. It holds owners' names and any
phone numbers you add, so it is git-ignored.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "pipeline.json")

STAGES = [
    ("lead", "Lead"),
    ("contacted", "Contacted"),
    ("talking", "Talking"),
    ("offer_sent", "Offer sent"),
    ("negotiating", "Negotiating"),
    ("under_contract", "Under contract"),
    ("closed", "Closed"),
    ("dead", "Dead"),
]
STAGE_KEYS = [k for k, _ in STAGES]
OPEN_STAGES = set(STAGE_KEYS) - {"closed", "dead"}

# Default gap to the next touch after moving into a stage.
FOLLOW_UP_DAYS = {"lead": 3, "contacted": 10, "talking": 4, "offer_sent": 3,
                  "negotiating": 2, "under_contract": 7}

# Partners share one pipeline over the network; one writer at a time.
_lock = threading.RLock()

EDITABLE = {"address", "stage", "owner", "mailing_address", "phone", "email", "asking",
            "offer", "mao", "arv", "rehab", "next_follow_up", "source", "tags", "url",
            "detail_url"}


def _today() -> str:
    return dt.date.today().isoformat()


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    data.setdefault("deals", {})
    return data


def _save(data: dict) -> None:
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, PATH)


def _key(address: str) -> str:
    a = re.sub(r"[^a-z0-9]+", " ", (address or "").lower()).strip()
    return " ".join(a.split()[:4])  # house number + street is enough to dedupe


def list_deals(include_closed: bool = True) -> list[dict]:
    deals = list(_load()["deals"].values())
    if not include_closed:
        deals = [d for d in deals if d["stage"] in OPEN_STAGES]
    order = {k: i for i, k in enumerate(STAGE_KEYS)}
    deals.sort(key=lambda d: (order.get(d["stage"], 99), d.get("next_follow_up") or "9999"))
    return deals


def upsert(fields: dict, by: str | None = None) -> dict:
    """Add a deal or update one. Matches on id, then on address.

    `by` is who made the change; it is written into the log when the app is
    shared with partners."""
    with _lock:
        return _upsert(fields, by)


def _upsert(fields: dict, by: str | None) -> dict:
    who = f" ({by})" if by else ""
    data = _load()
    deals = data["deals"]
    did = fields.get("id")
    if not did and fields.get("address"):
        k = _key(fields["address"])
        did = next((i for i, d in deals.items() if _key(d["address"]) == k), None)
    now = _today()
    if did and did in deals:
        deal = deals[did]
    else:
        did = did or uuid.uuid4().hex[:10]
        deal = {"id": did, "created": now, "stage": "lead", "log": [], "address": ""}
        deals[did] = deal
        deal["log"].append([now, "Added to pipeline" + who])

    old_stage = deal.get("stage")
    for k, v in fields.items():
        if k in EDITABLE and v not in (None, ""):
            deal[k] = v
    if deal.get("stage") not in STAGE_KEYS:
        deal["stage"] = "lead"

    if deal["stage"] != old_stage and old_stage is not None:
        deal["log"].append([now, f"Stage: {dict(STAGES).get(old_stage, old_stage)} -> "
                                 f"{dict(STAGES)[deal['stage']]}{who}"])
        # A stage change resets the clock unless a date was given.
        if "next_follow_up" not in fields and deal["stage"] in FOLLOW_UP_DAYS:
            deal["next_follow_up"] = (dt.date.today()
                                      + dt.timedelta(days=FOLLOW_UP_DAYS[deal["stage"]])).isoformat()
    if not deal.get("next_follow_up") and deal["stage"] in FOLLOW_UP_DAYS:
        deal["next_follow_up"] = (dt.date.today()
                                  + dt.timedelta(days=FOLLOW_UP_DAYS[deal["stage"]])).isoformat()
    if deal["stage"] not in OPEN_STAGES:
        deal["next_follow_up"] = None

    note = (fields.get("note") or "").strip()
    if note:
        deal["log"].append([now, f"{by}: {note}" if by else note])
    deal["updated"] = now
    _save(data)
    return deal


def delete(deal_id: str) -> bool:
    with _lock:
        data = _load()
        if deal_id in data["deals"]:
            del data["deals"][deal_id]
            _save(data)
            return True
        return False


def due(on: dt.date | None = None) -> dict[str, list[dict]]:
    """Open deals whose follow-up is today or already past."""
    on = (on or dt.date.today()).isoformat()
    open_deals = [d for d in list_deals(False) if d.get("next_follow_up")]
    return {
        "overdue": [d for d in open_deals if d["next_follow_up"] < on],
        "today": [d for d in open_deals if d["next_follow_up"] == on],
    }


def summary() -> dict[str, Any]:
    deals = list_deals()
    by_stage = {k: 0 for k in STAGE_KEYS}
    for d in deals:
        by_stage[d["stage"]] = by_stage.get(d["stage"], 0) + 1
    dd = due()
    return {"stages": STAGES, "counts": by_stage, "total": len(deals),
            "overdue": len(dd["overdue"]), "due_today": len(dd["today"])}
