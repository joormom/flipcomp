"""Off-market lead hunter -- who is under pressure before it is a listing.

  python hunt.py                              # Washington County + neighbours
  python hunt.py --county rogers --only
  python hunt.py --tax-details 500 --csv leads.csv
  python hunt.py --oscn saved_results.html    # classify a saved OSCN page
  python hunt.py --taxcheck "1552 S Maple Ave, Bartlesville, OK"
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import webbrowser

from flipcomp import history, leads, regions, report, search, taxroll


def money(n):
    return "--" if n in (None, "") else f"${float(n):,.0f}"


def main():
    p = argparse.ArgumentParser(description="Find distressed / pre-market properties.")
    p.add_argument("--county", default=regions.DEFAULT_REGION)
    p.add_argument("--only", action="store_true", help="home county only")
    p.add_argument("--metro", action="store_true", help="include Tulsa County")
    p.add_argument("--tax-details", type=int, default=250,
                   help="how many delinquent parcels to resolve to an address")
    p.add_argument("--csv", help="write every lead to this CSV")
    p.add_argument("--oscn", help="path to a saved OSCN results page to classify")
    p.add_argument("--taxcheck", help="look up delinquency for one address")
    p.add_argument("--dossier", metavar="ADDRESS",
                   help="everything public about one address, and how to reach the owner")
    p.add_argument("--exclude", metavar="PLACES",
                   help='towns/counties to skip everywhere, e.g. --exclude "Chelsea, Nowata" '
                        '(replaces the list; "" clears it)')
    p.add_argument("--buyer", nargs="+", metavar="FIELD",
                   help='your details for owner letters: --buyer "Name" "phone" ["email"] ["company"]')
    p.add_argument("--letters", type=int, metavar="N",
                   help="write owner letters + a mail-merge CSV for the top N tax leads")
    p.add_argument("--pipeline", action="store_true", help="list the deals you are working")
    p.add_argument("--import-roll", metavar="FILE",
                   help="score an assessor parcel roll (CSV/XLSX from an Open Records request)")
    p.add_argument("--roll", action="store_true", help="letters: only assessor-roll leads")
    p.add_argument("--focus-only", action="store_true",
                   help="letters: only leads inside the focus school districts")
    p.add_argument("--flip-backtest", action="store_true",
                   help="value recent local flips as of their purchase day; score past ones that have sold")
    p.add_argument("--accuracy", nargs="*", metavar="COUNTY",
                   help="backtest the ARV against real renovated sales (default: region counties)")
    p.add_argument("--daily", action="store_true",
                   help="run leads + deal scan, diff against the last run, write reports/")
    p.add_argument("--since", type=int, metavar="DAYS",
                   help="report what changed in the last N days without running anything")
    p.add_argument("--open", action="store_true", help="open the HTML report when done")
    p.add_argument("--verify-new", type=int, default=8,
                   help="daily: comp-verify this many NEW deal candidates")
    p.add_argument("--email", action="store_true",
                   help="daily: email the update to everyone with the daily email on (Team tab)")
    p.add_argument("--email-now", action="store_true",
                   help="email the last day's changes now, without running a scan")
    args = p.parse_args()

    if args.email_now:
        return send_email(history.since(1, source="leads"), history.since(1, source="deals"))

    if args.flip_backtest:
        import pandas as _pd
        from flipcomp import backtest as _bt
        done = _bt.check_flips()
        if len(done):
            print(f"  {len(done)} tracked flips have sold. App ARV on purchase day vs actual sale: "
                  f"median {done['arv_vs_sold_pct'].median():+.1f}%, within 10% "
                  f"{(done['arv_vs_sold_pct'].abs() <= 10).mean() * 100:.0f}%; flipper's ask vs sale "
                  f"{done['ask_vs_sold_pct'].median():+.1f}%")
        else:
            print("  No tracked flips have sold yet.")
        frames = [_bt.find_flips(c["key"]) for c in regions.resolve(args.county, not args.only, args.metro)]
        flips = _pd.concat(frames, ignore_index=True).drop_duplicates(subset=["property_id"]) if frames else _pd.DataFrame()
        res = _bt.run_flips(flips, progress=lambda m: print(f"  .. {m}", file=sys.stderr))
        print(f"  {len(res)} current flips valued; {_bt.save_flips(res)} newly tracked.")
        if len(res):
            print(f"  App ARV vs flipper's ask {res['arv_vs_ask_pct'].median():+.1f}% | app max offer vs price paid "
                  f"{res['mao_vs_buy_pct'].median():+.1f}% | app would have bought {res['app_would_buy'].mean() * 100:.0f}%")
        return

    if args.accuracy is not None:
        from flipcomp import accuracy as _acc
        keys = args.accuracy or [c["key"] for c in regions.resolve(args.county, not args.only, args.metro)]
        for k in keys:
            try:
                rec = _acc.measure(k, progress=lambda m: print(f"  .. {m}", file=sys.stderr))
                print(f"  {k:<12} {_acc.describe(rec)}")
            except Exception as exc:
                print(f"  {k:<12} could not backtest: {exc}")
        return

    if args.buyer:
        from flipcomp import prefs as _prefs
        keys = ["buyer_name", "buyer_phone", "buyer_email", "buyer_company"]
        saved = _prefs.save_buyer(**dict(zip(keys, args.buyer)))
        print("  Letters will be signed:", ", ".join(v for v in saved.values() if v))
        return

    if args.import_roll:
        from flipcomp import roll_import as _ri
        with open(args.import_roll, "rb") as fh:
            res = _ri.import_roll(fh.read(), args.import_roll,
                                  None if args.county == regions.DEFAULT_REGION else args.county)
        if res.get("error"):
            print("  " + res["error"])
            return
        d = history.record(res["leads"], "roll")
        print(f"  {res['rows']:,} parcels read -> {res['count']:,} pre-market leads "
              f"({res['in_focus']:,} in focus districts); {len(d['new'])} new since last import")
        print("  signals: " + ", ".join(f"{k} {v:,}" for k, v in sorted(res["signal_counts"].items(), key=lambda x: -x[1])))
        for l in res["leads"][:25]:
            print(f"  {l['score']:>3}  {l['address'][:34]:<34} {l['owner'][:30]:<30} {l.get('school') or '':<16} "
                  f"{', '.join(l['tags'])}")
        print("  Letters: python hunt.py --letters 100 --roll --open")
        return

    if args.letters:
        from flipcomp import letters as _letters
        kinds = ("roll",) if getattr(args, "roll", False) else ("tax", "roll")
        rows = history.top_tax_leads(args.letters,
                                     None if args.county == regions.DEFAULT_REGION else args.county,
                                     kinds, args.focus_only)
        if not rows:
            print("  No tracked tax leads with mailing addresses yet. Run: python hunt.py --daily")
            return
        os.makedirs(report.REPORT_DIR, exist_ok=True)
        import datetime as _dt
        stamp = _dt.date.today().isoformat()
        hp = os.path.join(report.REPORT_DIR, f"letters-{stamp}.html")
        cp = os.path.join(report.REPORT_DIR, f"mailers-{stamp}.csv")
        with open(hp, "w", encoding="utf-8") as fh:
            fh.write(_letters.letters_document(rows))
        with open(cp, "w", encoding="utf-8", newline="") as fh:
            fh.write(_letters.mail_merge_csv(rows))
        print(f"  {len(rows)} letters: {hp}")
        print(f"  Mail-merge CSV: {cp}")
        if args.open:
            webbrowser.open(hp)
        return

    if args.pipeline:
        from flipcomp import pipeline as _pl
        sm = _pl.summary()
        print("  " + "  ".join(f"{label}: {sm['counts'][k]}" for k, label in sm["stages"]))
        print(f"  {sm['overdue']} overdue, {sm['due_today']} due today")
        print()
        for d in _pl.list_deals():
            print(f"  {d['stage']:<14} {(d.get('next_follow_up') or ''):<11} "
                  f"{(d.get('address') or '')[:44]:<44} {d.get('owner') or ''}  "
                  f"offer {money(d.get('offer'))}  max {money(d.get('mao'))}")
        return

    if args.exclude is not None:
        from flipcomp import prefs as _prefs
        saved = _prefs.set_excluded_places(args.exclude.replace(";", ",").split(","), set(regions.COUNTIES))
        purged = history.purge_excluded()
        print(f"  Skipping cities:   {', '.join(saved['exclude_cities']) or '(none)'}")
        print(f"  Skipping counties: {', '.join(saved['exclude_counties']) or '(none)'}")
        print(f"  Removed {purged} tracked properties in those places.")
        return

    if args.since:
        ld = history.since(args.since, source="leads")
        dd = history.since(args.since, source="deals")
        print(report.build_text(ld, dd))
        path = report.write(report.build_html(ld, dd, since_days=args.since,
                                              title=f"FlipComp - last {args.since} days"))
        print(f"\n  Report: {path}")
        if args.open:
            webbrowser.open(path)
        return

    if args.daily:
        return daily(args)

    if args.dossier:
        from flipcomp import dossier as _dossier
        d = _dossier.build(args.dossier, None if args.county == regions.DEFAULT_REGION else args.county)
        print("=" * 90)
        print(f"  {d.get('normalised') or d['address']}   ({d.get('county')})")
        print("=" * 90)
        print(f"  Owner of record     {d.get('owner') or 'not on the roll'}")
        print(f"  Mail goes to        {d.get('mailing_address') or '?'}"
              + ("   (absentee)" if d.get("owner_occupied") is False else ""))
        t = d["tax"]
        if t.get("found"):
            print(f"  Taxes               {t['years_behind']} yr behind, {money(t['tax_owed'])} owed, "
                  f"liens {money(t['special_assessments_owed'])}, bill {money(t.get('annual_tax'))}/yr")
        pr = d["parcel"]
        if pr.get("net_assessed"):
            print(f"  Assessed            {money(pr['net_assessed'])} net -> ~{money(pr['implied_market_value'])} implied"
                  + ("   LAND ONLY" if pr.get("land_only") else ""))
        if pr.get("legal"):
            print(f"  Legal               {pr['legal']}")
        for run in d["ownership_timeline"]:
            print(f"  Roll {run['from']}-{run['to']}      {run['owner']}")
        L = d.get("listing")
        if L:
            print(f"  Realtor.com         {L.get('status')} {money(L.get('list_price'))}  "
                  f"{L.get('sqft') or '?'} sqft, built {L.get('year_built') or '?'}, last sold "
                  f"{(L.get('last_sold_date') or '')[:10]} {money(L.get('last_sold_price'))}")
            if L.get("agent_name"):
                print(f"  Listing agent       {L['agent_name']} {L.get('agent_phones') or ''} {L.get('agent_email') or ''}")
        else:
            print("  Realtor.com         no listing history")
        if d["seen_by_app"]:
            print(f"  In this app         " + "; ".join(f"{s['kind']} since {s['first_seen']} ({','.join(s.get('tags') or [])})" for s in d["seen_by_app"]))
        print("-" * 90)
        print("  WHAT IT MEANS")
        for n in d["situation"]["notes"]:
            print(f"    - {n}")
        print("  HOW TO APPROACH")
        for n in d["situation"]["approach"]:
            print(f"    - {n}")
        print("-" * 90)
        print("  LINKS")
        for l in d["links"]:
            print(f"    {l['label']}\n      {l['url']}")
        if d.get("tax_roll_url"):
            print(f"    Tax roll record\n      {d['tax_roll_url']}")
        return

    if args.taxcheck:
        d = taxroll.delinquency_for_address(args.county, args.taxcheck)
        for k, v in d.items():
            print(f"  {k:<26} {v}")
        return

    if args.oscn:
        with open(args.oscn, encoding="utf-8", errors="replace") as fh:
            html = fh.read()
        res = leads.oscn_from_html(args.county, html)
        print(f"  {len(res['cases'])} cases: {res.get('counts')}; "
              f"{res.get('with_parcels', 0)} matched to parcels\n")
        for c in res["cases"]:
            if c["category"] == "other" and not c.get("parcels"):
                continue
            print(f"  {c['number']:<14} {c['filed'] or '':<11} {c['category']:<12} "
                  f"{c['plaintiff'][:40]}")
            if c["people"]:
                print(f"      v. {'; '.join(c['people'][:3])}")
            for pr in c.get("parcels", []):
                print(f"      -> {pr.get('location') or pr.get('legal') or pr['property_id']}"
                      f"  owner {pr['owner']}  ~{money(pr.get('implied_market_value'))}"
                      + (f"  unpaid {pr['unpaid_years']}" if pr.get("unpaid_years") else ""))
        return

    res = leads.find_leads(
        home=args.county, include_adjacent=not args.only, include_metro=args.metro,
        tax_details=args.tax_details,
        progress=lambda m: print(f"  .. {m}", file=sys.stderr),
    )
    st = res["stats"]
    w = 100
    print("=" * w)
    print("  " + ", ".join(res["counties"]))
    print(f"  {st['active_scanned']:,} listings read | {st['listed_with_signals']} with signals | "
          f"{st['expired']} expired | {st['tax_parcels']:,} tax-delinquent parcels "
          f"({st['tax_3yr']} at 3+ yrs, {st['city_liens']} with city liens) | "
          f"{st['listed_and_delinquent']} listed AND delinquent")
    print("=" * w)

    both = [l for l in res["listed"] + res["expired"] if l.get("tax_status") == "delinquent"]
    if both:
        print("\n  LISTED AND BEHIND ON TAXES\n")
        for l in both:
            print(f"  {l['score']:>3}  {money(l['price']):>9}  {l['address']}")
            print(f"        owner {l.get('owner')}  tax {money(l.get('tax_owed'))}  "
                  f"liens {money(l.get('liens_owed'))}  {', '.join(l['tags'])}")
            if l.get("url"):
                print(f"        {l['url']}")

    print("\n  ACTIVE LISTINGS WITH SIGNALS\n")
    for l in [x for x in res["listed"] if x.get("tax_status") != "delinquent"][:25]:
        print(f"  {l['score']:>3}  {money(l['price']):>9}  {l['address'][:48]:<48} {', '.join(l['tags'])}")

    print("\n  EXPIRED LISTINGS\n")
    for l in [x for x in res["expired"] if x.get("tax_status") != "delinquent"][:20]:
        print(f"  {l['score']:>3}  {money(l['price']):>9}  {l['address'][:48]:<48} "
              f"{l.get('expired_days_ago') or '?'}d ago")

    print("\n  TAX DELINQUENT, OFF MARKET (worst first)\n")
    for t in [x for x in res["tax"] if x.get("address") and not x.get("land_only")][:40]:
        print(f"  {t['score']:>3}  {t['years_behind']}yr  tax {money(t['tax_owed']):>8}  "
              f"liens {money(t['liens_owed']):>7}  ~{money(t.get('implied_value')):>9}  "
              f"{t['address'][:34]:<34} {t['owner'][:28]}")

    sh = res["sheriff"]
    print(f"\n  SHERIFF'S SALES: {sh['note']}  {sh.get('url') or ''}")
    print("\n  COURT FILINGS (open in a browser, save the page, then --oscn file.html):")
    for s in res["oscn"]["searches"]:
        print(f"    {s['label']}\n      {s['url']}")

    if args.csv:
        rows = []
        for kind in ("listed", "expired", "tax"):
            for l in res[kind]:
                rows.append({
                    "kind": kind, "score": l["score"], "address": l.get("address"),
                    "owner": l.get("owner"), "price": l.get("price"),
                    "implied_value": l.get("implied_value"), "years_behind": l.get("years_behind"),
                    "tax_owed": l.get("tax_owed"), "liens_owed": l.get("liens_owed"),
                    "tags": " ".join(l.get("tags") or []), "url": l.get("url"),
                    "tax_roll": l.get("detail_url"), "land_only": l.get("land_only"),
                    "mailing_address": l.get("mailing_address"),
                })
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["kind"])
            wr.writeheader()
            wr.writerows(rows)
        print(f"\n  Wrote {len(rows)} leads to {args.csv}")



def daily(args):
    """The scheduled run: everything, diffed, written to reports/."""
    log = lambda m: print(f"  .. {m}", file=sys.stderr)

    log("Hunting leads")
    res = leads.find_leads(home=args.county, include_adjacent=not args.only,
                           include_metro=args.metro, tax_details=args.tax_details, progress=log)
    all_leads = res["listed"] + res["expired"] + res["tax"]
    leads_diff = history.record(all_leads, "leads", stats=res["stats"])

    log("Scanning deals")
    counties = regions.resolve(args.county, not args.only, args.metro)
    scan = search.screen(counties, {"max_results": 80}, progress=log)
    cands = [{**c, "kind": "deal", "addr_key": leads._addr_key(c.get("address"))}
             for c in scan["candidates"]]
    deals_diff = history.record(cands, "deals", stats={
        "active_screened": scan["active_screened"], "sold_pool": scan["sold_pool"]})

    # Comp-verify only what is new; the rest was verified when it first appeared.
    fresh = [c for c in cands if any(n["id"].endswith(history.lead_id(c) or "~") for n in deals_diff["new"])]
    if fresh and args.verify_new:
        log(f"Comp-verifying {min(len(fresh), args.verify_new)} new candidates")
        verified = search.verify(fresh, top=args.verify_new, progress=log)
        by_addr = {v["address"]: v for v in verified}
        annotated = []
        for n in deals_diff["new"]:
            v = by_addr.get(n.get("address"))
            if v and v.get("verified"):
                n.update({"verdict": v["verdict"], "mao": v["mao"], "est_arv": v["arv"],
                          "est_mao": v["mao"] if v["feasible"] else 0, "score": v["confidence"]})
                annotated.append({**n, "kind": "deal"})
        history.annotate("deals", annotated)

    # The best new listings in the target districts get a full comp too, so the
    # email and report show ARV and max offer for them, not just the asking price.
    focus_new = sorted((n for n in leads_diff.get("new", []) if n.get("school_district")
                        and n.get("kind") in ("listed", "expired") and n.get("price")),
                       key=lambda n: -(n.get("score") or 0))[:args.verify_new]
    if focus_new and args.verify_new:
        log(f"Comp-verifying {len(focus_new)} new Owasso/Collinsville leads")
        by_addr = {v["address"]: v for v in search.verify(focus_new, top=len(focus_new), progress=log)}
        done = []
        for n in focus_new:
            v = by_addr.get(n.get("address"))
            if v and v.get("verified"):
                n.update({"verdict": v["verdict"], "est_arv": v["arv"], "mao": v["mao"],
                          "est_mao": v["mao"] if v["feasible"] else 0})
                done.append(n)
        history.annotate("leads", done, fields=("verdict", "est_arv", "est_mao", "mao"))

    text = report.build_text(leads_diff, deals_diff)
    print(text)
    path = report.write(report.build_html(leads_diff, deals_diff))
    print(f"\n  Report: {path}")
    if args.email:
        send_email(leads_diff, deals_diff)
    if args.open:
        webbrowser.open(path)


def send_email(leads_diff, deals_diff):
    from flipcomp import mailer
    try:
        res = mailer.send_daily(leads_diff, deals_diff)
    except mailer.MailError as exc:
        print(f"  Email not sent: {exc}")
        return
    if res["sent"]:
        print(f"  Emailed \"{res['subject']}\" to {', '.join(res['sent'])}")
    else:
        print(f"  Email not sent: {res.get('note') or 'no recipients'}")
    for f in res["failed"]:
        print(f"  Email to {f['email']} failed: {f['error']}")


if __name__ == "__main__":
    main()
