"""The daily email: what is new in Find Deals and Leads, and who to call.

Built from the same diffs as the daily report, but cut down to what is worth
reading on a phone: follow-ups due, anything new in the Owasso and
Collinsville school districts, the best new deal-scan hits and the
highest-scoring new leads. Each person gets their own copy with their own
private link into the app.

Sent through any SMTP account. With Gmail, turn on 2-Step Verification and
create an app password (Google Account > Security > App passwords); the normal
Gmail password will not work. The settings live in team.json (git-ignored).

The markup is table-based with inline styles only, because that is what
Gmail, Apple Mail and Outlook all render the same way.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import access

TAG_LABEL = {
    "foreclosure": "Foreclosure", "short_sale": "Short sale", "auction": "Auction",
    "estate": "Estate", "investor": "Needs work", "as_is": "As-is", "cash_only": "Cash only",
    "damage": "Damage", "motivated": "Motivated", "price_cut_text": "Reduced",
    "vacant": "Vacant", "life_event": "Life event", "tenant": "Tenant", "stale": "Stale",
    "below_last_sale": "Below last sale", "price_cut": "Price cut", "under_avm": "Under estimate",
    "expired": "Expired", "tax_3yr": "3 yrs tax owed", "tax_2yr": "2 yrs tax owed",
    "tax_1yr": "1 yr tax owed", "city_lien": "City lien", "absentee": "Absentee owner",
}
# The district already has its own badge, so its tag would only repeat it.
HIDDEN_TAGS = {"focus_school"}
# Tags that are the reason to call; they get a warmer chip.
HOT_TAGS = {"foreclosure", "auction", "estate", "tax_3yr", "vacant", "damage", "short_sale",
            "city_lien", "motivated", "cash_only"}
KIND_LABEL = {"listed": "For sale", "expired": "Expired listing", "tax": "Tax delinquent",
              "deal": "Deal scan", "roll": "Assessor roll"}
SCHOOL_LABEL = {"owasso": "Owasso schools", "collinsville": "Collinsville schools"}

# --- palette and type ----------------------------------------------------------
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK, BODY, MUTED, FAINT = "#0f172a", "#334155", "#64748b", "#94a3b8"
LINE, PAGE, CARD = "#e2e8f0", "#eef2f7", "#ffffff"
NAVY, BLUE, GREEN, AMBER, RED = "#0b1f3a", "#2563eb", "#047857", "#b45309", "#b91c1c"

# Leads scoring below this are one weak signal ("stale") and only bury the good
# ones in an email; they are still in the app and the full report.
MIN_LEAD_SCORE = 20


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _money(n) -> str:
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return ""


def _photo(url: str | None) -> str | None:
    """Realtor.com serves .webp, which Outlook cannot show; the same image is
    available as .jpg."""
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None
    url = url.split("?", 1)[0]
    return re.sub(r"\.webp$", ".jpg", url)


# --- picking what goes in -------------------------------------------------------
def _addr(r: dict) -> str:
    return " ".join((r.get("address") or "").lower().replace(",", " ").split()[:3])


def _new(diff: dict | None) -> list[dict]:
    if not diff or diff.get("first_run"):
        return []
    return list(diff.get("new") or [])


def pick(leads_diff: dict | None, deals_diff: dict | None, per_section: int = 12) -> dict:
    leads_new = _new(leads_diff)
    deals_new = _new(deals_diff)
    focus = [r for r in leads_new + deals_new if r.get("school_district")]
    focus.sort(key=lambda r: -(r.get("score") or 0))
    seen = {id(r) for r in focus}

    deals = [r for r in deals_new if id(r) not in seen]
    # A verified verdict beats a screen estimate; then the widest spread.
    deals.sort(key=lambda r: (not r.get("verdict"), -(r.get("spread") or 0)))
    # One house, one card: a deal-scan hit is not repeated as a lead.
    shown = {_addr(r) for r in focus + deals}
    leads = [r for r in leads_new if id(r) not in seen and _addr(r) not in shown
             and (r.get("score") or 0) >= MIN_LEAD_SCORE]
    leads.sort(key=lambda r: -(r.get("score") or 0))

    # The one to look at first: best in the focus districts, else the best lead.
    top = (focus or leads or deals or [None])[0]

    try:
        from . import pipeline
        dd = pipeline.due()
        follow = dd["overdue"] + dd["today"]
    except Exception:
        follow = []

    warnings = []
    for d in (leads_diff, deals_diff):
        warnings += (d or {}).get("warnings") or []
    return {
        "top": top,
        "follow": follow,
        "focus": [r for r in focus if r is not top][:per_section], "focus_total": len(focus),
        "deals": [r for r in deals if r is not top][:per_section], "deals_total": len(deals),
        "leads": [r for r in leads if r is not top][:per_section], "leads_total": len(leads),
        "changed": len((leads_diff or {}).get("changed") or []) + len((deals_diff or {}).get("changed") or []),
        "tracked": ((leads_diff or {}).get("tracked") or 0) + ((deals_diff or {}).get("tracked") or 0),
        "warnings": warnings,
    }


def subject_line(p: dict, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    bits = []
    if p["follow"]:
        bits.append(f"{len(p['follow'])} follow-up{'s' if len(p['follow']) != 1 else ''} due")
    if p["focus_total"]:
        bits.append(f"{p['focus_total']} new in Owasso/Collinsville")
    if p["deals_total"]:
        bits.append(f"{p['deals_total']} new deal{'s' if p['deals_total'] != 1 else ''}")
    if p["leads_total"]:
        bits.append(f"{p['leads_total']} new lead{'s' if p['leads_total'] != 1 else ''}")
    return f"FlipComp {today.strftime('%a %b')} {today.day}: " + (", ".join(bits) or "no new properties")


# --- building blocks -------------------------------------------------------------
def _pill(text: str, fg: str, bg: str, bold: bool = False) -> str:
    return (f'<span style="display:inline-block;font:{700 if bold else 600} 11px/16px {FONT};color:{fg};'
            f'background:{bg};padding:2px 8px;border-radius:999px;margin:0 4px 4px 0;'
            f'white-space:nowrap">{_e(text)}</span>')


def _score_badge(score) -> str:
    if score is None:
        return ""
    fg, bg = ((GREEN, "#d1fae5") if score >= 70 else (AMBER, "#fef3c7") if score >= 40
              else (MUTED, "#f1f5f9"))
    return (f'<span style="display:inline-block;font:800 12px/16px {FONT};color:{fg};background:{bg};'
            f'padding:3px 9px;border-radius:999px;margin:0 4px 4px 0">{int(score)}</span>')


def _button(href: str, label: str, primary: bool = False) -> str:
    style = (f"background:{BLUE};color:#ffffff;border:1px solid {BLUE}" if primary
             else f"background:#ffffff;color:{BLUE};border:1px solid #bfdbfe")
    return (f'<a href="{_e(href)}" style="display:inline-block;{style};font:600 13px/18px {FONT};'
            f'text-decoration:none;padding:7px 12px;border-radius:8px;margin:0 6px 6px 0">{_e(label)}</a>')


def _maps(address: str | None) -> str | None:
    if not address:
        return None
    q = address + ("" if "," in address else ", OK")
    return "https://www.google.com/maps/search/?api=1&query=" + html.escape(q.replace(" ", "+"), quote=True)


def _facts_line(r: dict) -> str:
    bits = []
    if r.get("beds"):
        bits.append(f"{float(r['beds']):g} bd")
    if r.get("baths"):
        bits.append(f"{float(r['baths']):g} ba")
    if r.get("sqft"):
        bits.append(f"{float(r['sqft']):,.0f} sqft")
    return " &middot; ".join([_e(r.get("city"))] * bool(r.get("city")) + bits)


def _numbers(r: dict) -> str:
    """The money strip under the address: what they want, what it's worth, what to pay."""
    cells = []
    if r.get("price"):
        cells.append(("Asking", _money(r["price"]), INK))
    if r.get("est_arv"):
        cells.append(("ARV", _money(r["est_arv"]), INK))
    if r.get("est_mao"):
        cells.append(("Max offer", _money(r["est_mao"]), GREEN))
    if r.get("years_behind"):
        cells.append((f"Tax owed &middot; {int(r['years_behind'])} yr", _money(r.get("tax_owed")), RED))
    if r.get("liens_owed"):
        cells.append(("City liens", _money(r["liens_owed"]), RED))
    if not r.get("price") and r.get("implied_value"):
        cells.append(("Assessed ~", _money(r["implied_value"]), INK))
    if not cells:
        return ""
    tds = "".join(
        f'<td valign="top" style="padding:8px 10px;border-right:1px solid {LINE}">'
        f'<div style="font:600 10px/14px {FONT};letter-spacing:.6px;text-transform:uppercase;color:{FAINT}">{label}</div>'
        f'<div style="font:700 15px/20px {FONT};color:{color};white-space:nowrap">{_e(val)}</div></td>'
        for label, val, color in cells[:4])
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin-top:10px;background:#f8fafc;border:1px solid {LINE};border-radius:8px;'
            f'border-collapse:separate"><tr>{tds}</tr></table>')


def _chips(r: dict) -> str:
    hidden = HIDDEN_TAGS | ({"auction"} if str(r.get("verdict") or "").upper().startswith("AUCTION") else set())
    tags = [t for t in (r.get("tags") or []) if t not in hidden]
    tags.sort(key=lambda t: t not in HOT_TAGS)
    out = []
    for t in tags[:6]:
        hot = t in HOT_TAGS
        out.append(_pill(TAG_LABEL.get(t, t.replace("_", " ").capitalize()),
                         "#9a3412" if hot else BODY, "#ffedd5" if hot else "#f1f5f9"))
    if r.get("verdict"):
        v = str(r["verdict"]).upper()
        fg, bg = ((GREEN, "#d1fae5") if v.startswith("PURSUE") else
                  (RED, "#fee2e2") if v in ("PASS", "NOT VIABLE") else (AMBER, "#fef3c7"))
        out.insert(0, _pill(str(r["verdict"]).capitalize() if not v.startswith("AUCTION")
                            else str(r["verdict"]), fg, bg, bold=True))
    return f'<div style="margin-top:10px">{"".join(out)}</div>' if out else ""


def _links(r: dict) -> str:
    out = []
    if r.get("url"):
        out.append(_button(r["url"], "View listing"))
    if r.get("detail_url"):
        out.append(_button(r["detail_url"], "Tax record"))
    m = _maps(r.get("address"))
    if m:
        out.append(_button(m, "Map"))
    return f'<div style="margin-top:8px">{"".join(out)}</div>' if out else ""


def _badges(r: dict) -> str:
    out = _score_badge(r.get("score"))
    out += _pill(KIND_LABEL.get(r.get("kind"), r.get("kind") or ""), "#1e3a8a", "#dbeafe")
    if r.get("school_district"):
        out += _pill(SCHOOL_LABEL.get(r["school_district"], r["school_district"]), GREEN, "#d1fae5")
    return out


def _card(inner: str, accent: str | None = None) -> str:
    border = f"border-left:4px solid {accent};" if accent else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{CARD};border:1px solid {LINE};{border}border-radius:12px;'
            f'margin:0 0 12px;border-collapse:separate"><tr><td style="padding:14px">{inner}</td></tr></table>')


def _property(r: dict) -> str:
    photo = _photo(r.get("photo"))
    title = _e(r.get("address") or "(no address)")
    if r.get("url"):
        title = f'<a href="{_e(r["url"])}" style="color:{INK};text-decoration:none">{title}</a>'
    head = (f'<div>{_badges(r)}</div>'
            f'<div style="font:700 16px/22px {FONT};color:{INK};margin-top:2px">{title}</div>'
            f'<div style="font:13px/18px {FONT};color:{MUTED};margin-top:2px">{_facts_line(r)}</div>')
    if photo:
        img = (f'<a href="{_e(r.get("url") or photo)}"><img src="{_e(photo)}" width="112" height="84" alt="" '
               f'style="display:block;width:112px;height:84px;object-fit:cover;border-radius:8px;'
               f'border:0;background:{LINE}"></a>')
        head = (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
                f'<td valign="top" width="112" style="width:112px;padding-right:12px">{img}</td>'
                f'<td valign="top">{head}</td></tr></table>')
    return _card(head + _numbers(r) + _chips(r) + _links(r))


def _top_pick(r: dict) -> str:
    photo = _photo(r.get("photo"))
    img = ""
    if photo:
        img = (f'<a href="{_e(r.get("url") or photo)}"><img src="{_e(photo)}" width="560" alt="" '
               f'style="display:block;width:100%;max-width:560px;height:auto;border:0;'
               f'border-radius:12px 12px 0 0"></a>')
    title = _e(r.get("address") or "(no address)")
    if r.get("url"):
        title = f'<a href="{_e(r["url"])}" style="color:{INK};text-decoration:none">{title}</a>'
    body = (f'<div style="font:700 11px/14px {FONT};letter-spacing:1px;text-transform:uppercase;'
            f'color:{AMBER};margin-bottom:8px">&#9733; Today\'s top pick</div>'
            f'<div>{_badges(r)}</div>'
            f'<div style="font:800 20px/26px {FONT};color:{INK};margin-top:2px">{title}</div>'
            f'<div style="font:14px/20px {FONT};color:{MUTED};margin-top:2px">{_facts_line(r)}</div>'
            + _numbers(r) + _chips(r) + _links(r))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{CARD};border:1px solid {LINE};border-radius:12px;margin:0 0 12px;'
            f'border-collapse:separate">'
            + (f'<tr><td style="padding:0;line-height:0">{img}</td></tr>' if img else "")
            + f'<tr><td style="padding:16px">{body}</td></tr></table>')


def _follow(d: dict) -> str:
    late = d["next_follow_up"] < dt.date.today().isoformat()
    last = (d.get("log") or [["", ""]])[-1]
    stage = d.get("stage", "").replace("_", " ").capitalize()
    pills = (_pill("Overdue" if late else "Due today", RED if late else AMBER,
                   "#fee2e2" if late else "#fef3c7", bold=True) + _pill(stage, "#1e3a8a", "#dbeafe"))
    who = []
    if d.get("owner"):
        who.append(_e(d["owner"]))
    if d.get("phone"):
        tel = re.sub(r"[^0-9+]", "", d["phone"])
        who.append(f'<a href="tel:{_e(tel)}" style="color:{BLUE};text-decoration:none;font-weight:600">'
                   f'{_e(d["phone"])}</a>')
    money = " &middot; ".join(x for x in (f"offered {_money(d['offer'])}" if d.get("offer") else "",
                                          f"max {_money(d['mao'])}" if d.get("mao") else "") if x)
    inner = (f'<div>{pills}</div>'
             f'<div style="font:700 15px/21px {FONT};color:{INK};margin-top:2px">{_e(d.get("address"))}</div>'
             + (f'<div style="font:13px/18px {FONT};color:{BODY};margin-top:2px">{" &middot; ".join(who)}</div>' if who else "")
             + (f'<div style="font:13px/18px {FONT};color:{BODY}">{money}</div>' if money else "")
             + f'<div style="font:12px/17px {FONT};color:{MUTED};margin-top:6px">Last touch {_e(last[0])}: '
               f'{_e(last[1]) or "-"}</div>')
    return _card(inner, RED if late else "#f59e0b")


def _section(title: str, sub: str, total: int, blocks: list[str], more_link: str | None) -> str:
    if not blocks:
        return ""
    more = ""
    if total > len(blocks):
        more = (f'<div style="font:13px/18px {FONT};color:{MUTED};margin:-2px 0 6px;text-align:center">'
                f'+ {total - len(blocks)} more'
                + (f' &middot; <a href="{_e(more_link)}" style="color:{BLUE};text-decoration:none;'
                   f'font-weight:600">see them all</a>' if more_link else "") + "</div>")
    head = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:26px 0 12px"><tr><td style="border-left:4px solid {BLUE};padding:1px 0 1px 10px">'
            f'<div style="font:800 17px/22px {FONT};color:{INK}">{_e(title)} '
            f'<span style="font:600 13px {FONT};color:{FAINT}">{total}</span></div>'
            f'<div style="font:13px/18px {FONT};color:{MUTED}">{_e(sub)}</div></td></tr></table>')
    return head + "".join(blocks) + more


def _stats(p: dict) -> str:
    tiles = [(len(p["follow"]), "Follow-ups", RED if p["follow"] else INK),
             (p["focus_total"], "Target area", GREEN),
             (p["deals_total"], "New deals", BLUE),
             (p["leads_total"], "New leads", INK)]
    tds = "".join(
        f'<td align="center" width="25%" style="padding:12px 4px;{"border-left:1px solid " + LINE + ";" if i else ""}">'
        f'<div style="font:800 24px/28px {FONT};color:{color}">{n}</div>'
        f'<div style="font:600 10px/14px {FONT};letter-spacing:.5px;text-transform:uppercase;color:{MUTED}">{label}</div></td>'
        for i, (n, label, color) in enumerate(tiles))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{CARD};border:1px solid {LINE};border-radius:12px;border-collapse:separate">'
            f'<tr>{tds}</tr></table>')


def _shell(inner: str, preheader: str = "") -> str:
    hidden = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">'
              f'{_e(preheader)}{"&nbsp;&zwnj;" * 40}</div>') if preheader else ""
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
            f'</head><body style="margin:0;padding:0;background:{PAGE}">{hidden}'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAGE}">'
            f'<tr><td align="center" style="padding:20px 10px 32px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px">'
            f'<tr><td>{inner}</td></tr></table></td></tr></table></body></html>')


def _header(title: str, subtitle: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{NAVY};border-radius:14px;margin-bottom:12px;border-collapse:separate">'
            f'<tr><td style="padding:22px 22px 20px">'
            f'<div style="font:800 13px/16px {FONT};letter-spacing:2px;color:#93c5fd">FLIPCOMP</div>'
            f'<div style="font:800 24px/30px {FONT};color:#ffffff;margin-top:6px">{_e(title)}</div>'
            f'<div style="font:14px/20px {FONT};color:#cbd5e1;margin-top:2px">{subtitle}</div>'
            f'</td></tr></table>')


# --- the email -------------------------------------------------------------------
def render(p: dict, name: str = "", app_link: str | None = None,
           report_link: str | None = None) -> tuple[str, str]:
    """Return (html, plain text) for one recipient."""
    today = dt.date.today()
    first = (name or "").split(" ")[0]
    greet = "Good morning" + (f", {_e(first)}" if first and first.lower() != "owner" else "")
    parts = [_header(f"{greet}.", f"Your deal report for {today.strftime('%A, %B')} {today.day}"), _stats(p)]

    if app_link:
        parts.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                     f'style="margin:12px 0 0"><tr><td align="center" style="background:{BLUE};border-radius:10px">'
                     f'<a href="{_e(app_link)}" style="display:block;padding:13px 16px;font:700 15px/20px {FONT};'
                     f'color:#ffffff;text-decoration:none">Open FlipComp &rarr;</a></td></tr></table>'
                     f'<div style="font:11px/16px {FONT};color:{FAINT};text-align:center;margin-top:6px">'
                     f'That button is your personal sign-in link, so please don\'t forward this email.</div>')
    else:
        parts.append(f'<div style="font:12px/17px {FONT};color:{FAINT};text-align:center;margin-top:10px">'
                     f'The app is not shared online right now, so there is no sign-in link today.</div>')

    for w in p["warnings"]:
        parts.append(f'<div style="margin-top:12px">' + _card(
            f'<div style="font:13px/19px {FONT};color:{AMBER}"><b>Data warning.</b> {_e(w)}</div>', "#f59e0b")
            + "</div>")

    parts.append(_section("Follow-ups due", "Deals in your pipeline waiting on a call, text or letter.",
                          len(p["follow"]), [_follow(d) for d in p["follow"]], None))
    if p["top"]:
        parts.append('<div style="height:14px"></div>' + _top_pick(p["top"]))
    parts.append(_section("New in Owasso & Collinsville schools", "The districts you're targeting, best first.",
                          p["focus_total"], [_property(r) for r in p["focus"]], report_link))
    parts.append(_section("New from Find Deals", "Listed houses where the numbers may work as a flip.",
                          p["deals_total"], [_property(r) for r in p["deals"]], report_link))
    parts.append(_section("New leads", "Off-market and motivated-seller signals across the region.",
                          p["leads_total"], [_property(r) for r in p["leads"]], report_link))
    if not (p["follow"] or p["top"]):
        parts.append('<div style="height:12px"></div>' + _card(
            f'<div style="font:14px/20px {FONT};color:{BODY}">Nothing new today. {p["tracked"]:,} '
            f'properties are still being watched.</div>'))

    parts.append(f'<div style="font:12px/18px {FONT};color:{FAINT};text-align:center;margin-top:26px">'
                 f'{p["changed"]} watched properties changed today (price cuts, new tax years, status) '
                 f'&middot; {p["tracked"]:,} watched in all'
                 + (f'<br><a href="{_e(report_link)}" style="color:{BLUE};text-decoration:none;font-weight:600">'
                    f'Open the full report</a>' if report_link else "")
                 + '<br>Scores run 0-100. Values are automated estimates, not appraisals.</div>')

    subject = subject_line(p, today)
    doc = _shell("".join(parts), preheader=subject.split(": ", 1)[-1])

    lines = [subject, ""]
    if app_link:
        lines += [f"Open FlipComp (your personal link, don't forward): {app_link}", ""]
    if p["top"]:
        r = p["top"]
        lines += ["TODAY'S TOP PICK", f"  {r.get('address')}  " + " | ".join(_text_facts(r))]
        if r.get("url"):
            lines.append(f"  {r['url']}")
        lines.append("")
    for title, rows, total in (("FOLLOW-UPS DUE", p["follow"], len(p["follow"])),
                               ("NEW IN OWASSO / COLLINSVILLE SCHOOLS", p["focus"], p["focus_total"]),
                               ("NEW FROM FIND DEALS", p["deals"], p["deals_total"]),
                               ("NEW LEADS", p["leads"], p["leads_total"])):
        if not rows:
            continue
        lines.append(f"{title} ({total})")
        for r in rows:
            if title == "FOLLOW-UPS DUE":
                lines.append(f"  {r['next_follow_up']}  {r.get('address')}  {r.get('stage')}  {r.get('phone') or ''}")
            else:
                lines.append(f"  {r.get('score') if r.get('score') is not None else '':>3}  {r.get('address')}  "
                             + " | ".join(_text_facts(r)))
                if r.get("url"):
                    lines.append(f"       {r['url']}")
        lines.append("")
    return doc, "\n".join(lines)


def _text_facts(r: dict) -> list[str]:
    f = []
    if r.get("price"):
        f.append(f"asking {_money(r['price'])}")
    if r.get("est_arv"):
        f.append(f"ARV ~{_money(r['est_arv'])}")
    if r.get("est_mao"):
        f.append(f"max offer {_money(r['est_mao'])}")
    if r.get("verdict"):
        f.append(str(r["verdict"]))
    if r.get("years_behind"):
        f.append(f"{r['years_behind']} yr behind on tax ({_money(r.get('tax_owed'))})")
    if r.get("liens_owed"):
        f.append(f"city liens {_money(r['liens_owed'])}")
    if r.get("owner"):
        f.append(f"owner {r['owner']}")
    return f


# --- sending -------------------------------------------------------------------
class MailError(Exception):
    pass


def configured() -> bool:
    s = access.settings()
    return bool(s.get("smtp_host") and s.get("smtp_user") and s.get("smtp_password"))


def _connect():
    s = access.settings()
    if not configured():
        raise MailError("Email is not set up yet: add the sending address and app password in the Team tab.")
    port = int(s.get("smtp_port") or 587)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            conn = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=30, context=ctx)
        else:
            conn = smtplib.SMTP(s["smtp_host"], port, timeout=30)
            conn.starttls(context=ctx)
        conn.login(s["smtp_user"], s["smtp_password"])
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("The mail server refused the login. With Gmail you need an app password, "
                        "not your normal password.") from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"Could not reach the mail server: {exc}") from exc
    return conn


def _message(to: str, subject: str, html_body: str, text_body: str) -> EmailMessage:
    s = access.settings()
    msg = EmailMessage()
    msg["From"] = formataddr((s.get("mail_from_name") or "FlipComp", s["smtp_user"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _report_link(u: dict) -> str | None:
    link = access.join_link(u)
    return f"{link}?next=/report" if link else None


def send_daily(leads_diff: dict | None, deals_diff: dict | None, only: list[str] | None = None) -> dict:
    """Email everyone who has the daily email turned on. `only` limits it to user ids."""
    p = pick(leads_diff, deals_diff)
    subject = subject_line(p)
    people = [u for u in access.list_users()
              if u.get("email") and u.get("daily_email") and (not only or u["id"] in only)]
    if not people:
        return {"sent": [], "failed": [], "subject": subject,
                "note": "Nobody has the daily email turned on with an address filled in."}
    sent, failed = [], []
    conn = _connect()
    try:
        for u in people:
            html_body, text_body = render(p, u.get("name") or "", access.join_link(u), _report_link(u))
            try:
                conn.send_message(_message(u["email"], subject, html_body, text_body))
                sent.append(u["email"])
            except smtplib.SMTPException as exc:
                failed.append({"email": u["email"], "error": str(exc)})
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return {"sent": sent, "failed": failed, "subject": subject}


def invite_html(who: str, link: str) -> str:
    inner = (_header("You're invited.", f"{_e(who)} shared FlipComp with you")
             + _card(f'<div style="font:15px/23px {FONT};color:{BODY}">FlipComp finds and values flips around '
                     f'Bartlesville, Owasso and Collinsville. Comp any address, see new deals and off-market '
                     f'leads each morning, and work the shared pipeline.</div>'
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                     f'style="margin:16px 0 4px"><tr><td align="center" style="background:{BLUE};border-radius:10px">'
                     f'<a href="{_e(link)}" style="display:block;padding:13px 16px;font:700 15px/20px {FONT};'
                     f'color:#ffffff;text-decoration:none">Open FlipComp &rarr;</a></td></tr></table>'
                     f'<div style="font:12px/18px {FONT};color:{MUTED};margin-top:8px">This link is personal to you '
                     f'and signs you in, so please don\'t share it. If the address ever changes, the newest link is '
                     f'in the FlipComp daily email.</div>'))
    return _shell(inner, preheader=f"{who} added you to FlipComp")


def send_invite(u: dict) -> dict:
    """Email someone their personal link."""
    link = access.join_link(u)
    if not link:
        raise MailError("The app is not shared online yet, so there is no link to send. Start it with share.bat.")
    if not u.get("email"):
        raise MailError("Add an email address for this person first.")
    who = access.owner().get("name") or "Your partner"
    subject = f"{who} shared FlipComp with you"
    text_body = (f"{who} added you to FlipComp.\n\nOpen it here (your personal sign-in link, please don't "
                 f"share it):\n{link}\n")
    conn = _connect()
    try:
        conn.send_message(_message(u["email"], subject, invite_html(who, link), text_body))
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return {"sent": [u["email"]]}
