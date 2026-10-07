"""Run history -- what changed since last time.

Every lead hunt and deal scan is recorded against a persistent store keyed by
property. That is what makes a daily report possible: a lead is *new* if this
is the first run that saw it, *changed* if its score, tags or price moved, and
*dropped* if it was there yesterday and is gone today (sold, paid up, delisted).

The store is a single JSON file. It is small -- a few thousand records with a
handful of fields each -- and readable by hand if something looks off.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from .data import CACHE_DIR

STORE_PATH = os.path.join(CACHE_DIR, "history", "store.json")
KEEP_DROPPED_DAYS = 45

# Signals whose appearance is worth calling out by name in the report.
NOTABLE_TAGS = {
    "tax_3yr": "now 3 years behind on tax",
    "tax_2yr": "now 2 years behind on tax",
    "city_lien": "city lien added",
    "price_cut": "price cut",
    "expired": "listing expired",
    "foreclosure": "foreclosure language",
    "short_sale": "short sale",
    "estate": "estate language",
    "below_last_sale": "now below last sale price",
}


def _today() -> str:
    return dt.date.today().isoformat()


def load() -> dict[str, Any]:
    try:
        with open(STORE_PATH, encoding="utf-8") as fh:
            store = json.load(fh)
    except Exception:
        store = {}
    store.setdefault("items", {})
    store.setdefault("runs", [])
    return store


def save(store: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh)
    os.replace(tmp, STORE_PATH)


def lead_id(lead: dict) -> str | None:
    kind = lead.get("kind") or "lead"
    if kind in ("tax", "roll") and lead.get("property_id"):
        return f"{kind}:{lead.get('county_key')}:{lead['property_id']}"
    key = lead.get("addr_key") or (lead.get("address") or "").strip().lower()
    if not key:
        return None
    return f"{kind}:{key}"


def _snapshot(lead: dict) -> dict:
    """The fields worth diffing. Everything else is re-derived on each run."""
    return {
        "kind": lead.get("kind"),
        "address": lead.get("address"),
        "city": lead.get("city") or lead.get("location_city"),
        "county_key": lead.get("county_key"),
        "score": lead.get("score"),
        "tags": sorted(lead.get("tags") or []),
        "price": lead.get("price"),
        "owner": lead.get("owner"),
        "years_behind": lead.get("years_behind"),
        "tax_owed": lead.get("tax_owed"),
        "liens_owed": lead.get("liens_owed"),
        "implied_value": lead.get("implied_value"),
        "url": lead.get("url") or lead.get("listing_url"),
        "detail_url": lead.get("detail_url"),
        "mailing_address": lead.get("mailing_address"),
        "school_district": lead.get("school_district"),
        "land_only": lead.get("land_only"),
        # for the daily email's property cards
        "photo": lead.get("photo"),
        "beds": lead.get("beds"),
        "baths": lead.get("baths"),
        "sqft": lead.get("sqft"),
        # deal-scan fields
        "est_arv": lead.get("est_arv"),
        "est_mao": lead.get("est_mao"),
        "spread": lead.get("spread"),
        "verdict": lead.get("verdict"),
        "mao": lead.get("mao"),
    }


def _describe_change(old: dict, new: dict) -> list[str]:
    notes = []
    o_tags, n_tags = set(old.get("tags") or []), set(new.get("tags") or [])
    # A price move has to be big enough to mean something: at least 1% and $1,000.
    price_moved = bool(old.get("price") and new.get("price")
                       and abs(new["price"] - old["price"]) >= max(1_000, 0.01 * old["price"]))
    for t in sorted(n_tags - o_tags):
        if t == "price_cut" and price_moved:
            continue  # the price line below says it with numbers
        notes.append(NOTABLE_TAGS.get(t, f"+{t}"))
    if price_moved:
        pct = (new["price"] - old["price"]) / old["price"] * 100
        notes.append(f"price {'cut' if pct < 0 else 'raised'} {abs(pct):.0f}% "
                     f"(${old['price']:,.0f} -> ${new['price']:,.0f})")
    if (old.get("years_behind") or 0) < (new.get("years_behind") or 0):
        notes.append(f"unpaid years {old.get('years_behind') or 0} -> {new['years_behind']}")
    if old.get("spread") is not None and new.get("spread") is not None:
        d = new["spread"] - old["spread"]
        if abs(d) >= 5_000:
            notes.append(f"spread {'up' if d > 0 else 'down'} ${abs(d):,.0f}")
    if old.get("verdict") and new.get("verdict") and old["verdict"] != new["verdict"]:
        notes.append(f"verdict {old['verdict']} -> {new['verdict']}")
    if (new.get("score") or 0) - (old.get("score") or 0) >= 15 and not notes:
        notes.append(f"score {old.get('score')} -> {new.get('score')}")
    return notes


def record(leads: list[dict], source: str, stats: dict | None = None) -> dict[str, Any]:
    """Merge one run's leads into the store; return what changed.

    `source` names the producer ('leads' or 'deals') so a property found by
    both is tracked separately for each and each can be reported on.
    """
    store = load()
    items = store["items"]
    today = _today()
    seen_ids: set[str] = set()
    new, changed, returned = [], [], []

    def group(rec):
        s = rec["snap"]
        return (s.get("kind"), s.get("county_key"))

    # How many properties each (kind, county) had going into this run, counted
    # before anything is touched, so the guard below compares like with like.
    active_before: dict = {}
    for lid, rec in items.items():
        if lid.startswith(source + "|") and not rec.get("dropped_on"):
            g = group(rec)
            active_before[g] = active_before.get(g, 0) + 1

    for lead in leads:
        lid = lead_id(lead)
        if not lid:
            continue
        lid = f"{source}|{lid}"
        if lid in seen_ids:
            # Same property twice in one run (duplicate MLS entry, or a
            # hyphenated range address that normalises to the same key). Leads
            # arrive best-first, so the first one stands and the rest are noise.
            continue
        seen_ids.add(lid)
        snap = _snapshot(lead)
        rec = items.get(lid)
        if rec is None:
            items[lid] = {"first_seen": today, "last_seen": today, "runs": 1,
                          "snap": snap, "changes": [], "dropped_on": None}
            new.append({"id": lid, **snap, "first_seen": today})
            continue
        was_dropped = bool(rec.get("dropped_on"))
        notes = _describe_change(rec["snap"], snap)
        if was_dropped:
            rec["dropped_on"] = None
            returned.append({"id": lid, **snap, "first_seen": rec["first_seen"],
                             "notes": ["back after being gone"] + notes})
        elif notes:
            rec["changes"].append([today, "; ".join(notes)])
            rec["changes"] = rec["changes"][-20:]
            changed.append({"id": lid, **snap, "first_seen": rec["first_seen"],
                            "notes": notes, "old_score": rec["snap"].get("score")})
        rec["snap"] = snap
        rec["last_seen"] = today
        rec["runs"] = rec.get("runs", 0) + 1

    # Guard: a source that suddenly returns far less than last time has almost
    # certainly failed (a site change, a cap, an outage) rather than had most of
    # its properties sell or pay up overnight. Keep that group as it was.
    seen_now: dict = {}
    for lid in seen_ids:
        g = group(items[lid])
        seen_now[g] = seen_now.get(g, 0) + 1
    protected, warnings = set(), []
    for g, before in active_before.items():
        now = seen_now.get(g, 0)
        if before >= 50 and now < before * 0.5:
            protected.add(g)
            warnings.append(f"{g[0] or 'leads'} in {g[1] or 'region'}: got {now} this run vs "
                            f"{before} last time - kept the previous list instead of dropping "
                            f"{before - now}. Check the source.")

    # Anything from this source not seen this run has dropped off.
    dropped = []
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DROPPED_DAYS)).isoformat()
    for lid, rec in list(items.items()):
        if not lid.startswith(source + "|"):
            continue
        if lid in seen_ids:
            continue
        if group(rec) in protected:
            continue
        if not rec.get("dropped_on"):
            rec["dropped_on"] = today
            dropped.append({"id": lid, **rec["snap"], "first_seen": rec["first_seen"],
                            "last_seen": rec["last_seen"]})
        elif rec["dropped_on"] < cutoff:
            del items[lid]

    prior_runs = [r for r in store["runs"] if r.get("source") == source]
    first_run = not prior_runs
    store["runs"].append({"date": today, "at": dt.datetime.now().isoformat(timespec="seconds"),
                          "source": source, "count": len(seen_ids), "new": len(new),
                          "changed": len(changed), "dropped": len(dropped),
                          "warnings": warnings,
                          "stats": stats or {}})
    store["runs"] = store["runs"][-400:]
    save(store)

    rank = lambda x: -(x.get("score") or x.get("spread") or 0)
    return {
        "source": source, "date": today, "first_run": first_run, "warnings": warnings,
        "tracked": len(seen_ids),
        "new": sorted(new, key=rank),
        "changed": sorted(changed, key=rank),
        "returned": sorted(returned, key=rank),
        "dropped": sorted(dropped, key=rank),
        "last_run": prior_runs[-1]["date"] if prior_runs else None,
    }


def since(days: int = 7, source: str | None = None) -> dict[str, Any]:
    """Everything first seen, changed or dropped within the last N days."""
    store = load()
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    new, changed, dropped = [], [], []
    for lid, rec in store["items"].items():
        src = lid.split("|", 1)[0]
        if source and src != source:
            continue
        row = {"id": lid, "source": src, **rec["snap"], "first_seen": rec["first_seen"],
               "last_seen": rec["last_seen"]}
        if rec.get("dropped_on") and rec["dropped_on"] >= cutoff:
            dropped.append({**row, "dropped_on": rec["dropped_on"]})
            continue
        if rec["first_seen"] >= cutoff:
            new.append(row)
            continue
        recent = [c for c in rec.get("changes", []) if c[0] >= cutoff]
        if recent:
            changed.append({**row, "notes": [c[1] for c in recent]})
    rank = lambda x: -(x.get("score") or x.get("spread") or 0)
    runs = [r for r in store["runs"] if r["date"] >= cutoff and (not source or r["source"] == source)]
    return {"days": days, "new": sorted(new, key=rank), "changed": sorted(changed, key=rank),
            "dropped": sorted(dropped, key=rank), "runs": runs,
            "tracked": sum(1 for lid, r in store["items"].items()
                           if not r.get("dropped_on") and (not source or lid.startswith(source + "|")))}


def annotate(source: str, leads: list[dict], fields: tuple[str, ...] = (
        "verdict", "mao", "est_arv", "est_mao", "score", "arv_low", "confidence")) -> int:
    """Patch stored snapshots with fields learned after the run was recorded.

    The daily run comp-verifies only the *new* deal candidates, which are only
    known once `record()` has run, so their verdicts arrive a step late. This
    writes them back without counting as another run.
    """
    store = load()
    n = 0
    for lead in leads:
        # Rows from a diff carry their store key; raw leads are keyed afresh.
        key = lead.get("id") if lead.get("id") in store["items"] else None
        if not key:
            lid = lead_id(lead)
            key = f"{source}|{lid}" if lid else None
        rec = store["items"].get(key) if key else None
        if not rec:
            continue
        for f in fields:
            if lead.get(f) is not None:
                rec["snap"][f] = lead[f]
                n += 1
    if n:
        save(store)
    return n


def top_tax_leads(n: int = 50, county_key: str | None = None,
                  kinds: tuple[str, ...] = ("tax",), focus_only: bool = False) -> list[dict]:
    """Strongest tracked off-market leads with a street and mailing address."""
    from . import prefs as _prefs
    p = _prefs.load()
    out = []
    for lid, rec in load()["items"].items():
        s = rec["snap"]
        if s.get("kind") not in kinds or rec.get("dropped_on") or s.get("land_only"):
            continue
        if focus_only and not s.get("school_district"):
            continue
        if not s.get("address"):
            continue
        if county_key and s.get("county_key") != county_key:
            continue
        if _prefs.address_excluded(s.get("address"), p) or _prefs.city_excluded(s.get("city"), p):
            continue
        out.append({**s, "first_seen": rec["first_seen"]})
    out.sort(key=lambda x: (-(x.get("score") or 0), -((x.get("tax_owed") or 0) + (x.get("liens_owed") or 0))))
    # Leads tracked before mailing addresses were kept: recover them from the
    # tax-roll detail cache (on disk, so this is usually free).
    from . import taxroll
    ready = []
    for row in out:
        if len(ready) >= n:
            break
        if not row.get("mailing_address") and row.get("detail_url"):
            try:
                row["mailing_address"] = taxroll.parcel_detail(row["detail_url"]).get("mailing_address")
            except Exception:
                pass
        if row.get("mailing_address"):
            ready.append(row)
    return ready


def purge_excluded() -> int:
    """Remove tracked properties in places the user has ruled out.

    Done silently rather than letting them age out as 'dropped': they did not
    sell or get paid up, the user just does not want to see them.
    """
    from . import prefs as _prefs
    p = _prefs.load()
    if not p["exclude_cities"] and not p["exclude_counties"]:
        return 0
    store = load()
    doomed = []
    for lid, rec in store["items"].items():
        snap = rec["snap"]
        if (_prefs.county_excluded(snap.get("county_key"), p)
                or _prefs.city_excluded(snap.get("city"), p)
                or _prefs.address_excluded(snap.get("address"), p)):
            doomed.append(lid)
    for lid in doomed:
        del store["items"][lid]
    if doomed:
        save(store)
    return len(doomed)
