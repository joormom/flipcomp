"""Daily report -- what is new, what moved, what fell off.

Renders the diff from `history.record()` (or `history.since()`) as a
self-contained HTML page and as plain text for the console. Written to
reports/YYYY-MM-DD.html with a copy at reports/latest.html.
"""
from __future__ import annotations

import datetime as dt
import html
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "reports")

TAG_LABEL = {
    "foreclosure": "foreclosure", "short_sale": "short sale", "auction": "auction",
    "estate": "estate", "investor": "needs work", "as_is": "as-is", "cash_only": "cash only",
    "damage": "damage", "motivated": "motivated", "price_cut_text": "reduced",
    "vacant": "vacant", "life_event": "life event", "tenant": "tenant", "stale": "stale",
    "below_last_sale": "below last sale", "price_cut": "price cut", "under_avm": "under estimate",
    "expired": "expired", "tax_3yr": "3yr tax", "tax_2yr": "2yr tax", "tax_1yr": "1yr tax",
    "city_lien": "city lien", "absentee": "absentee",
}
KIND_LABEL = {"listed": "Listed", "expired": "Expired", "tax": "Tax roll", "deal": "Deal scan"}


def _money(n) -> str:
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return "--"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _links(row: dict) -> str:
    bits = []
    if row.get("url"):
        bits.append(f'<a href="{_e(row["url"])}" target="_blank">listing</a>')
    if row.get("detail_url"):
        bits.append(f'<a href="{_e(row["detail_url"])}" target="_blank">tax roll</a>')
    if row.get("address"):
        q = html.escape((row["address"] or "") + (", OK" if "," not in (row["address"] or "") else ""))
        bits.append(f'<a href="https://www.google.com/maps/search/{q}" target="_blank">map</a>')
    return " &middot; ".join(bits)


def _row_html(row: dict, notes: list[str] | None = None) -> str:
    kind = KIND_LABEL.get(row.get("kind"), row.get("kind") or "")
    tags = " ".join(f'<span class="t">{_e(TAG_LABEL.get(t, t))}</span>' for t in (row.get("tags") or []))
    facts = []
    if row.get("price"):
        facts.append(f"asking {_money(row['price'])}")
    if row.get("est_arv"):
        facts.append(f"est ARV {_money(row['est_arv'])}")
    if row.get("est_mao"):
        facts.append(f"max offer {_money(row['est_mao'])}")
    if row.get("spread") is not None and row.get("kind") == "deal":
        facts.append(f"spread {_money(row['spread'])}")
    if row.get("verdict"):
        facts.append(_e(row["verdict"]))
    if row.get("years_behind"):
        facts.append(f"{row['years_behind']} yr behind, {_money(row.get('tax_owed'))} owed")
    if row.get("liens_owed"):
        facts.append(f"liens {_money(row['liens_owed'])}")
    if row.get("implied_value"):
        facts.append(f"~{_money(row['implied_value'])} implied")
    if row.get("owner"):
        facts.append(f"owner {_e(row['owner'])}")
    note_html = ""
    if notes:
        note_html = '<div class="n">' + "; ".join(_e(n) for n in notes) + "</div>"
    score = row.get("score")
    score_html = f'<span class="s">{score}</span>' if score is not None else ""
    return (f'<div class="r"><div class="h">{score_html}<b>{_e(row.get("address") or "(no address)")}</b>'
            f' <span class="k">{_e(kind)}{" &middot; " + _e(row["city"]) if row.get("city") else ""}</span></div>'
            f'<div class="f">{" &middot; ".join(facts)}</div>{note_html}'
            f'<div class="g">{tags}</div><div class="l">{_links(row)}</div></div>')


def _section(title: str, rows: list[dict], note_key: str | None = None, limit: int = 60,
             empty: str = "Nothing.") -> str:
    body = "".join(_row_html(r, r.get(note_key) if note_key else None) for r in rows[:limit]) \
        or f'<div class="m">{_e(empty)}</div>'
    more = f'<div class="m">and {len(rows) - limit} more</div>' if len(rows) > limit else ""
    return f'<section><h2>{_e(title)} <span class="c">{len(rows)}</span></h2>{body}{more}</section>'


def build_html(leads_diff: dict | None, deals_diff: dict | None, title: str | None = None,
               since_days: int | None = None) -> str:
    today = dt.date.today().strftime("%A %d %B %Y")
    title = title or f"FlipComp daily report - {today}"
    parts = []

    def block(diff: dict, label: str):
        if not diff:
            return
        if diff.get("first_run"):
            parts.append(f'<section><h2>{_e(label)}</h2><div class="m">First run - baseline '
                         f'established with {diff.get("tracked", 0):,} tracked. Changes start '
                         f'showing from the next run.</div></section>')
            return
        hdr = (f"since {since_days} days" if since_days else
               f"since last run{' on ' + diff['last_run'] if diff.get('last_run') else ''}")
        parts.append(f'<h1 class="src">{_e(label)} <span class="c">{_e(hdr)}</span></h1>')
        # The intersection first: anything new that is both on the market and delinquent.
        both = [r for r in diff.get("new", []) if r.get("kind") in ("listed", "expired")
                and (r.get("years_behind") or r.get("liens_owed"))]
        if both:
            parts.append(_section("New: listed AND behind on taxes", both))
        new = [r for r in diff.get("new", []) if r not in both]
        parts.append(_section("New since last time", new, empty="No new properties."))
        parts.append(_section("Changed", diff.get("changed", []), note_key="notes",
                              empty="No material changes."))
        if diff.get("returned"):
            parts.append(_section("Back on the list", diff["returned"], note_key="notes"))
        parts.append(_section("Dropped off", diff.get("dropped", []), limit=30,
                              empty="Nothing dropped off."))

    block(leads_diff, "Leads")
    block(deals_diff, "Deal scan")

    css = """
    body{font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e8eaed;margin:0;padding:24px}
    .w{max-width:960px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}h1.src{margin-top:28px;font-size:17px;color:#9aa3b2}
    h2{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:#9aa3b2;margin:22px 0 8px}
    .c{font-weight:400;color:#6b7483;font-size:12px;margin-left:6px}.sub{color:#9aa3b2;font-size:13px}
    .r{background:#171a21;border:1px solid #2a2f3a;border-radius:10px;padding:11px 14px;margin-bottom:8px}
    .h{font-size:14.5px}.k{color:#6b7483;font-size:12px;margin-left:6px}.f{color:#9aa3b2;font-size:12.5px;margin-top:3px}
    .n{color:#ffb648;font-size:12.5px;margin-top:4px}.g{margin-top:5px}.l{margin-top:6px;font-size:12px}
    .t{display:inline-block;font-size:11px;padding:1px 8px;border-radius:999px;margin:2px 4px 0 0;background:#1e222b;color:#9aa3b2;border:1px solid #2a2f3a}
    .s{display:inline-block;min-width:26px;text-align:center;font-size:11px;font-weight:700;padding:2px 6px;border-radius:999px;background:rgba(255,182,72,.16);color:#ffb648;margin-right:8px}
    .m{color:#6b7483;font-size:13px;padding:6px 2px}a{color:#4ea1ff;text-decoration:none}
    """
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title>"
            f"<style>{css}</style></head><body><div class='w'><h1>{_e(title)}</h1>"
            f"<div class='sub'>Generated {dt.datetime.now().strftime('%H:%M')}. Scores are 0-100; "
            "the intersection of listed and delinquent is the shortlist.</div>"
            + "".join(parts) + "</div></body></html>")


def build_text(leads_diff: dict | None, deals_diff: dict | None) -> str:
    out = []

    def line(r, notes=None):
        bits = [f"{(r.get('score') if r.get('score') is not None else ''):>3}",
                (r.get("address") or "(no address)")[:46].ljust(46)]
        if r.get("price"):
            bits.append(f"ask {_money(r['price']):>9}")
        if r.get("est_mao"):
            bits.append(f"max {_money(r['est_mao']):>9}")
        if r.get("years_behind"):
            bits.append(f"{r['years_behind']}yr {_money(r.get('tax_owed'))}")
        if r.get("liens_owed"):
            bits.append(f"liens {_money(r['liens_owed'])}")
        if r.get("tags"):
            bits.append(",".join(TAG_LABEL.get(t, t) for t in r["tags"]))
        s = "  " + "  ".join(bits)
        if notes:
            s += "\n        -> " + "; ".join(notes)
        return s

    for diff, label in ((leads_diff, "LEADS"), (deals_diff, "DEAL SCAN")):
        if not diff:
            continue
        out.append("=" * 96)
        if diff.get("first_run"):
            out.append(f"  {label}: first run, baseline of {diff.get('tracked', 0):,} tracked.")
            continue
        span = (f"last {diff['days']} days" if diff.get("days")
                else f"since last run{' on ' + diff['last_run'] if diff.get('last_run') else ''}")
        out.append(f"  {label} - {span}: "
                   f"{len(diff.get('new', []))} new, {len(diff.get('changed', []))} changed, "
                   f"{len(diff.get('dropped', []))} dropped, {diff.get('tracked', 0):,} tracked")
        out.append("=" * 96)
        for name, key, nk in (("NEW", "new", None), ("CHANGED", "changed", "notes"),
                              ("BACK", "returned", "notes"), ("DROPPED", "dropped", None)):
            rows = diff.get(key) or []
            if not rows:
                continue
            out.append(f"\n  {name} ({len(rows)})")
            for r in rows[:40]:
                out.append(line(r, r.get(nk) if nk else None))
            if len(rows) > 40:
                out.append(f"  ... and {len(rows) - 40} more")
    return "\n".join(out)


def write(html_text: str, date: dt.date | None = None) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    date = date or dt.date.today()
    path = os.path.join(REPORT_DIR, f"{date.isoformat()}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    with open(os.path.join(REPORT_DIR, "latest.html"), "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path
