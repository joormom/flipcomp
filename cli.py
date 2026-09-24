"""Command-line interface -- analyse one address, or screen a list of them.

  python cli.py "123 Main St, Columbus, OH" --asking 200000
  python cli.py --batch addresses.txt --tier light --csv results.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys

from flipcomp import rehab
from flipcomp.analyze import analyze
from flipcomp.compengine import CompError
from flipcomp.data import DataError


def money(n):
    return "--" if n is None else f"${n:,.0f}"


def build_overrides(args) -> dict:
    o = {}
    if args.tier:
        o["rehab_tier"] = args.tier
    if args.rehab is not None:
        o["rehab_override_total"] = args.rehab
    if args.contingency is not None:
        o["rehab_contingency_pct"] = args.contingency
    if args.hold is not None:
        o["hold_months"] = args.hold
    if args.commission is not None:
        o["agent_commission_pct"] = args.commission
    if args.target is not None:
        o["target_profit_pct"] = args.target
    if args.min_profit is not None:
        o["min_profit_flat"] = args.min_profit
    if args.cash:
        o["cash_purchase"] = True
    if args.sqft is not None:
        o["subject_sqft"] = args.sqft
    return o


def print_report(r: dict) -> None:
    s, a, rh, o, v = r["subject"], r["arv"], r["rehab"], r["offer"], r["verdict"]
    w = 74
    print("=" * w)
    print(s.get("formatted_address") or "")
    print(f"  {s.get('beds') or '?'} bd / {s.get('full_baths') or '?'} ba / "
          f"{s.get('sqft') or 0:,.0f} sqft / built {s.get('year_built') or '?':.0f}"
          if s.get("year_built") else
          f"  {s.get('beds') or '?'} bd / {s.get('full_baths') or '?'} ba / "
          f"{s.get('sqft') or 0:,.0f} sqft")
    print("=" * w)
    print(f"  {v['call']}: {v['detail']}")
    print("-" * w)
    print(f"  ARV (resale)        {money(a['arv']):>14}   "
          f"({money(a['arv_low'])} - {money(a['arv_high'])})")
    print(f"  Confidence          {a['confidence']:>13}/100   "
          f"{a['confidence_label']}, {a['comp_count']} comps")
    print(f"  Renovation          {money(rh['total']):>14}   "
          f"{rh['tier_label']}, ${rh['psf_all_in']}/sqft")
    print(f"  Asking price        {money(r['asking_price']):>14}")
    print("-" * w)
    if o["feasible"]:
        print(f"  MAXIMUM OFFER       {money(o['mao']):>14}   "
              f"nets {money(o['target_profit'])}")
        print(f"  Suggested opening   {money(o['suggested_opening']):>14}")
    else:
        print(f"  MAXIMUM OFFER       {'not viable':>14}   "
              f"best case {money(o['max_profit_if_free'])} at zero cost")
    print(f"  70% rule            {money(o['rule_70']):>14}")

    if o["at_asking"]:
        k = o["at_asking"]
        print(f"  Profit at asking    {money(k['profit']):>14}   "
              f"{k['profit_margin_pct']}% margin, {k['roi_pct']}% on cash")

    st = o["stress"]
    print("-" * w)
    print("  Downside at max offer:")
    print(f"    ARV at low end      {money(st['arv_low']['profit']):>12}")
    print(f"    Rehab +25%          {money(st['rehab_overrun_25']['profit']):>12}")
    print(f"    Both                {money(st['both']['profit']):>12}")

    L = r.get("location") or {}
    if L.get("state"):
        src = {"listing":"from listing","state rate":"est. from state rate",
               "manual":"as entered"}.get(L.get("property_tax_source"), "")
        print("-" * w)
        print(f"  {L['state']} costs: transfer tax {L['transfer_tax_pct']}% "
              f"({L['transfer_payer']}"
              + (f", {L['buy_transfer_pct']}% on the buy" if L.get("buy_transfer_pct") else "")
              + f"), property tax {money(L.get('annual_taxes_used'))}/yr {src}")

    if r["flags"]:
        print("-" * w)
        print("  CHECK BEFORE YOU OFFER:")
        for f in r["flags"]:
            print(f"    [{f['level'].upper()}] {f['title']}")
            for line in _wrap(f["detail"], w - 10):
                print(f"          {line}")

    print("-" * w)
    print("  Comparable sales:")
    for c in a["comps"][:8]:
        print(f"    {(c['formatted_address'] or '')[:38]:<38} "
              f"{money(c['sold_price']):>10} {c['months_ago']:>4}mo "
              f"{c['distance_mi']:>5}mi -> {money(c['adjusted_value']):>10}")
    print("=" * w)


def _wrap(text: str, width: int):
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def main():
    p = argparse.ArgumentParser(description="FlipComp - ARV and max-offer analysis.")
    p.add_argument("address", nargs="?", help="property address")
    p.add_argument("--asking", type=float, help="asking price (defaults to the listing)")
    p.add_argument("--batch", help="file with one address per line")
    p.add_argument("--csv", help="write batch results to this CSV")
    p.add_argument("--json", action="store_true", help="emit raw JSON")
    p.add_argument("--tier", choices=list(rehab.TIERS), help="renovation scope")
    p.add_argument("--rehab", type=float, help="total renovation cost override")
    p.add_argument("--contingency", type=float, help="contingency %% (default 15)")
    p.add_argument("--hold", type=float, help="hold months (default 6)")
    p.add_argument("--commission", type=float, help="agent commission %% (default 5)")
    p.add_argument("--target", type=float, help="target profit %% of ARV (default 15)")
    p.add_argument("--min-profit", type=float, help="minimum profit $ (default 25000)")
    p.add_argument("--cash", action="store_true", help="all-cash purchase")
    p.add_argument("--sqft", type=float, help="override subject square footage")
    args = p.parse_args()

    overrides = build_overrides(args)

    if args.batch:
        with open(args.batch, encoding="utf-8") as fh:
            addresses = [ln.strip() for ln in fh if ln.strip()
                         and not ln.startswith("#")]
        rows = []
        for addr in addresses:
            try:
                r = analyze(addr, asking_price=args.asking, overrides=overrides)
                rows.append({
                    "address": r["subject"].get("formatted_address") or addr,
                    "asking": r["asking_price"],
                    "arv": r["arv"]["arv"],
                    "arv_low": r["arv"]["arv_low"],
                    "confidence": r["arv"]["confidence"],
                    "comps": r["arv"]["comp_count"],
                    "rehab": r["rehab"]["total"],
                    "rehab_tier": r["rehab"]["tier_label"],
                    "max_offer": r["offer"]["mao"] if r["offer"]["feasible"] else 0,
                    "profit_at_asking": (r["offer"]["at_asking"] or {}).get("profit"),
                    "verdict": r["verdict"]["call"],
                    "top_flag": r["flags"][0]["title"] if r["flags"] else "",
                })
                last = rows[-1]
                print(f"{last['verdict']:<11} {last['address'][:44]:<44} "
                      f"ARV {money(last['arv']):>10}  MAO {money(last['max_offer']):>10}")
            except (DataError, CompError) as exc:
                print(f"{'SKIPPED':<11} {addr[:44]:<44} {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"{'ERROR':<11} {addr[:44]:<44} {exc}", file=sys.stderr)

        if args.csv and rows:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
                wr.writeheader()
                wr.writerows(rows)
            print(f"\nWrote {len(rows)} rows to {args.csv}")
        return

    if not args.address:
        p.error("give an address, or use --batch")

    try:
        r = analyze(args.address, asking_price=args.asking, overrides=overrides)
    except (DataError, CompError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
