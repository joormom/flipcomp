"""Owner letters and mail-merge lists for off-market leads.

A handwritten-style letter to the mailing address on the tax roll is still
the best-converting outreach to absentee and distressed owners. This writes
the letters ready to print (one per page) and a CSV for any mail house.

The letter never mentions the owner's tax position. People who are behind
know it; a stranger pointing it out reads as a threat, not an offer.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import re

from . import prefs

_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_STATE_ZIP = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")


def parse_mailing(owner: str | None, mailing: str | None) -> dict:
    """'SMITH, JOHN 123 MAIN ST BARTLESVILLE OK 74003-0000' -> parts.

    The roll gives one run-on line with the owner name first. City is the
    word(s) between the last street token and the state; for mail purposes a
    trailing 'CITY OK ZIP' is all the post office needs.
    """
    text = (mailing or "").strip()
    if owner and text.upper().startswith(owner.upper()):
        text = text[len(owner):].strip(" ,")
    m = _STATE_ZIP.search(text)
    if not m:
        return {"line1": text, "city": "", "state": "", "zip": "", "raw": mailing or ""}
    state, zip5 = m.group(1), m.group(2)
    before = text[:m.start()].strip()
    toks = before.split()
    # City is the last 1-2 words; street suffixes mark where the street ended.
    sfx = {"ST", "AVE", "RD", "DR", "LN", "CT", "PL", "BLVD", "TER", "CIR", "WAY", "HWY",
           "PKWY", "TRL", "LOOP", "SQUARE", "SQ", "BOX", "APT", "UNIT", "N", "S", "E", "W"}
    cut = len(toks) - 1
    for i in range(len(toks) - 1, 0, -1):
        if toks[i - 1].upper() in sfx or toks[i - 1].isdigit():
            cut = i
            break
    line1, city = " ".join(toks[:cut]), " ".join(toks[cut:])
    return {"line1": line1.title(), "city": city.title(), "state": state, "zip": zip5,
            "raw": mailing or ""}


def addressee(owner: str | None) -> str:
    """'ASKINS, BILL C & JEANNETTE' -> 'Bill C & Jeannette Askins'."""
    o = re.sub(r"\b\d+/\d+\s*INT\b", "", owner or "", flags=re.I).strip(" ,:")
    if not o:
        return "Property Owner"
    if re.search(r"\b(llc|inc|corp|trust|bank|properties|holdings|company|church|estate)\b", o, re.I):
        return _entity_case(o)
    if "," in o:
        last, rest = [p.strip() for p in o.split(",", 1)]
        names = [c.strip(" ,").title() for c in re.split(r"\s*[:&]\s*|\s+and\s+", rest, flags=re.I)
                 if c.strip(" ,")]
        if not names:
            return last.title()
        # 'LESLIE STEVENS' already carries the surname; 'JEANNETTE' does not.
        has_own = [last.lower() in n.lower().split() for n in names]
        if not any(has_own):
            return f"{' & '.join(names)} {last.title()}"      # Bill C & Jeannette Askins
        full = [n if own else f"{n} {last.title()}" for n, own in zip(names, has_own)]
        return " & ".join(full)                                 # Patti Lynn Stevens & Leslie Stevens
    return o.title()


def _entity_case(name: str) -> str:
    t = name.title()
    for w in ("Llc", "Lp", "Llp", "Na"):
        t = re.sub(rf"\b{w}\b", w.upper(), t)
    return t


def greeting(owner: str | None) -> str:
    name = addressee(owner)
    if name == "Property Owner" or re.search(
            r"\b(llc|lp|inc|corp|trust|bank|company|properties|holdings|church|estate)\b", name, re.I):
        return "Hello,"
    # Without 'LAST, FIRST' punctuation the roll's word order is ambiguous;
    # 'Dear Wick' to a Mr Wick is worse than a plain hello.
    if "," not in (owner or ""):
        return "Hello,"
    first = name.split("&")[0].split()[0]
    return f"Dear {first},"


_UPPER_TOKENS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "OK", "PO", "US", "II", "III"}


def address_case(addr: str | None) -> str:
    """'717 SW HICKORY AVE, BARTLESVILLE, OK 74003' -> '717 SW Hickory Ave, Bartlesville, OK 74003'."""
    out = []
    for tok in re.split(r"(\s+|,)", addr or ""):
        bare = tok.strip(".").upper()
        out.append(bare if bare in _UPPER_TOKENS else tok.title() if tok.strip() else tok)
    return "".join(out).strip()


def _buyer(p: dict | None = None) -> dict:
    """Who signs the letters: the person printing them, else the saved buyer."""
    p = p if p and p.get("buyer_name") else prefs.load_buyer()
    return {"name": p.get("buyer_name") or "[Your name]",
            "phone": p.get("buyer_phone") or "[Your phone]",
            "email": p.get("buyer_email") or "",
            "company": p.get("buyer_company") or ""}


def letter_html(lead: dict, b: dict | None = None) -> str:
    b = b or _buyer()
    to = addressee(lead.get("owner"))
    mail = parse_mailing(lead.get("owner"), lead.get("mailing_address"))
    prop = address_case(lead.get("address"))
    e = html.escape
    today = dt.date.today().strftime("%B %d, %Y").replace(" 0", " ")
    return f"""
<section class="letter">
  <div class="date">{e(today)}</div>
  <div class="to">{e(to)}<br>{e(address_case(mail['line1']))}<br>{e(mail['city'])}{', ' if mail['city'] else ''}{e(mail['state'])} {e(mail['zip'])}</div>
  <p>{e(greeting(lead.get('owner')))}</p>
  <p>My name is {e(b['name'])}{(' with ' + e(b['company'])) if b['company'] else ''}, and I buy houses here in the
  Bartlesville area. I'm writing about your property at <b>{e(prop)}</b>.</p>
  <p>If you have ever thought about selling it, I would like to make you a fair cash offer.
  I buy houses as they are, so there is nothing to repair or clean out, no agent commissions,
  and I pay the normal closing costs. I can close in as little as two weeks, or on whatever
  timeline suits you.</p>
  <p>There is no obligation at all. If now is not the right time, I understand, and I will not
  pester you. But if you would like to hear a number, call or text me at <b>{e(b['phone'])}</b>{
  (' or email ' + e(b['email'])) if b['email'] else ''}.</p>
  <p>Thank you for your time,</p>
  <p class="sig">{e(b['name'])}<br>{e(b['phone'])}</p>
</section>"""


def letters_document(leads: list[dict], buyer: dict | None = None) -> str:
    css = """
    body{font:15px/1.6 Georgia,'Times New Roman',serif;color:#111;background:#fff;margin:0}
    .letter{max-width:640px;margin:0 auto;padding:64px 48px;page-break-after:always;min-height:85vh}
    .date{margin-bottom:28px}.to{margin-bottom:28px}.sig{margin-top:28px}
    @media print{.letter{padding:48px 40px}.noprint{display:none}}
    .noprint{font:13px system-ui;background:#fffbe6;border-bottom:1px solid #e6d98a;padding:10px 16px}
    """
    b = _buyer(buyer)
    warn = ""
    if "[Your" in b["name"] or "[Your" in b["phone"]:
        warn = ("<div class='noprint'>Set your name and phone first: "
                "<code>python hunt.py --buyer \"Your Name\" \"918-555-0100\"</code> "
                "(or the Pipeline tab). They are blank placeholders below.</div>")
    body = "".join(letter_html(l, b) for l in leads)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Owner letters ({len(leads)})</title>"
            f"<style>{css}</style></head><body>{warn}{body}</body></html>")


def mail_merge_csv(leads: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["addressee", "greeting", "mail_line1", "mail_city", "mail_state", "mail_zip",
                "property_address", "years_behind", "tax_owed", "liens_owed", "score", "tags"])
    for l in leads:
        m = parse_mailing(l.get("owner"), l.get("mailing_address"))
        w.writerow([addressee(l.get("owner")), greeting(l.get("owner")), address_case(m["line1"]), m["city"],
                    m["state"], m["zip"], address_case(l.get("address")), l.get("years_behind"),
                    l.get("tax_owed"), l.get("liens_owed"), l.get("score"),
                    " ".join(l.get("tags") or [])])
    return out.getvalue()
