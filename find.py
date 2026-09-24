"""Regional deal finder -- scan counties for flip candidates.

  python find.py                          # Washington County + neighbours
  python find.py --county rogers          # somewhere else
  python find.py --metro                  # include Tulsa County
  python find.py --max-price 200000 --verify 15 --csv deals.csv
"""
from __future__ import annotations

import argparse
import csv
import sys

from flipcomp import regions, search


def money(n):
    return "--" if n is None else f"${n:,.0f}"


def main():
    p = argparse.ArgumentParser(description="Find flip candidates across a region.")
    p.add_argument("--county", default=regions.DEFAULT_REGION,
                   help=f"home county (default {regions.DEFAULT_REGION})")
    p.add_argument("--only", action="store_true", help="home county only, no neighbours")
    p.add_argument("--metro", action="store_true",
                   help="include the adjacent large metro county (Tulsa)")
    p.add_argument("--add", nargs="*", default=[], help="extra counties to include")
    p.add_argument("--min-price", type=float, default=30_000)
    p.add_argument("--max-price", type=float, default=400_000)
    p.add_argument("--min-sqft", type=float, default=700)
    p.add_argument("--max-sqft", type=float, default=4_000)
    p.add_argument("--min-spread", type=float, default=0,
                   help="minimum $ the max offer must beat asking by")
    p.add_argument("--results", type=int, default=40, help="candidates to keep")
    p.add_argument("--verify", type=int, default=10,
                   help="how many to run the full comp analysis on (0 to skip)")
    p.add_argument("--csv", help="write results to this CSV")
    p.add_argument("--list-counties", action="store_true")
    args = p.parse_args()

    if args.list_counties:
        for k, v in sorted(regions.COUNTIES.items()):
            print(f"  {k:<12} {v['name']:<24} seat: {v['seat']}")
        return

    filters = {
        "min_price": args.min_price, "max_price": args.max_price,
        "min_sqft": args.min_sqft, "max_sqft": args.max_sqft,
        "min_spread": args.min_spread, "max_results": args.results,
    }

    try:
        res = search.find_deals(
            home=args.county,
            include_adjacent=not args.only,
            include_metro=args.metro,
            extra_counties=args.add,
            filters=filters,
            verify_top=args.verify,
            progress=lambda m: print(f"  .. {m}", file=sys.stderr),
        )
    except KeyError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    w = 112
    print("=" * w)
    print("  " + ", ".join(res["counties"]))
    print(f"  {res['active_screened']:,} active listings screened against "
          f"{res['sold_pool']:,} sales from the last 12 months")
    print(f"  Region median ${res['market_psf']:,.0f}/sqft  |  "
          f"{res['total_candidates']} candidates cleared the screen")
    print("=" * w)

    verified = res.get("verified") or []
    if verified:
        print("\n  VERIFIED - full comp analysis run on these\n")
        print("  %-38s %-9s %-9s %-9s %-9s %-4s %-5s %s" % (
            "ADDRESS", "ASKING", "ARV", "REHAB", "MAX OFFER", "CONF", "COMPS", "VERDICT"))
        print("  " + "-" * (w - 2))
        for v in verified:
            if not v.get("verified"):
                print("  %-38s %-9s  skipped: %s" % (
                    (v["address"] or "")[:38], money(v["price"]),
                    (v.get("verify_error") or "")[:44]))
                continue
            print("  %-38s %-9s %-9s %-9s %-9s %-4s %-5s %s" % (
                (v["address"] or "")[:38], money(v["price"]), money(v["arv"]),
                money(v["rehab"]), money(v["mao"]) if v["feasible"] else "n/a",
                v["confidence"], v["comp_count"], v["verdict"]))

        print("\n  DETAIL\n")
        for v in verified:
            if not v.get("verified") or not v["feasible"]:
                continue
            print(f"  {v['address']}")
            print(f"    {v.get('beds') or '?'} bd / {v.get('baths') or '?'} ba / "
                  f"{v['sqft']:,} sqft / built {v.get('year_built') or '?'}"
                  + (f" / {v['days_on_mls']} days on market" if v.get("days_on_mls") else ""))
            print(f"    Asking {money(v['price'])} -> offer up to {money(v['mao'])} "
                  f"({v['rehab_tier_final']} rehab {money(v['rehab'])})")
            print(f"    ARV {money(v['arv'])} (low {money(v['arv_low'])}), "
                  f"confidence {v['confidence']}/100 on {v['comp_count']} comps")
            print(f"    Profit at max offer {money(v['profit_at_mao'])}, "
                  f"downside {money(v['downside'])}")
            top = [f for f in (v.get("flags") or []) if f["level"] in ("high", "medium")][:2]
            for fl in top:
                print(f"    ! {fl['title']}")
            if v.get("url"):
                print(f"    {v['url']}")
            print()

    rest = res["candidates"][len(verified):]
    if rest:
        print(f"  SCREENED ONLY - not yet comp-verified ({len(rest)} more)\n")
        print("  %-40s %-9s %-10s %-9s %s" % ("ADDRESS", "ASKING", "EST ARV", "EST MAO", "SPREAD"))
        for c in rest:
            print("  %-40s %-9s %-10s %-9s %s" % (
                (c["address"] or "")[:40], money(c["price"]), money(c["est_arv"]),
                money(c["est_mao"]), money(c["spread"])))
        print("\n  Estimated ARV on these is a fast screen, not a comp analysis -"
              "\n  re-run with --verify to check them properly.")

    if args.csv:
        rows = verified + rest
        if rows:
            keys = sorted({k for r in rows for k in r if k != "flags"})
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                wr = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
                wr.writeheader()
                wr.writerows(rows)
            print(f"\n  Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
