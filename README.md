# FlipComp

Plug in an address. Get what the house should resell for, what the renovation
will cost, and the most you can pay and still make money.

Built for screening flip candidates: it pulls real recent sales near the
property, adjusts them the way an appraiser would, and solves backwards from a
target profit to a number you can put on a contract.

---

## Running it

```bash
pip install -r requirements.txt
```

Web app:

```bash
python server.py
```

Opens at `http://127.0.0.1:8777`. Type an address, optionally an asking price,
hit Analyze.

Command line, one property:

```bash
python cli.py "123 Main St, Columbus, OH" --asking 200000
```

Screening a list (one address per line):

```bash
python cli.py --batch addresses.txt --tier light --csv results.csv
```

Scanning a whole region for candidates:

```bash
python find.py --verify 10 --csv deals.csv
```

Hunting for off-market and pre-foreclosure leads:

```bash
python hunt.py
```

Defaults to Washington County plus its neighbours (Nowata, Osage, Rogers).
`--county rogers` moves the search, `--metro` adds Tulsa County, `--only` drops
the neighbours, `--list-counties` shows what is available. The web app has the
same thing under **Find deals**.

---

## What it actually does

**1. Resolves the property.** Looks the address up and pulls beds, baths,
square footage, year built, lot size, coordinates, tax bill and days on market.

For Oklahoma counties on the treasurer's system it also reads the county tax
roll: the actual tax bill replaces the listing's figure or the state estimate,
and the report says whether the owner is paying it, how far behind they are,
and whether the city has liens on the property.

The returned record is **address-verified** before anything else happens: a
fuzzy Realtor.com search can return hundreds of nearby homes, so the street
number must match and the street name must overlap. If nothing matches, it
refuses rather than analysing the wrong house.

**2. Builds an ARV from real comps.** Pulls sold listings around the property
and filters to genuinely comparable ones — same property class, within a radius,
similar size and bedroom count, sold recently. Tolerances start tight (0.5 mi,
±20% sqft, 6 months) and widen only as far as needed to find enough sales.

**3. Adjusts each comp to the subject.** Every comp is corrected for how it
differs — living area, bedrooms, bathrooms, age, lot size, garage — plus a time
adjustment for market movement since it sold. The marginal value of square
footage is derived from the local market rather than assumed, and the market
trend comes from regressing local price-per-sqft against sale date.

**4. Reconciles into one number.** Adjusted values are weighted by distance
(half-mile falloff), recency (12-month half-life) and similarity (heavily
adjusted comps count for less). You get an ARV, a 25th–75th percentile range,
and a confidence score out of 100.

Adjustments are kept deliberately conservative, because they compound in one
direction. Bedroom and bathroom counts correlate with living area, so adjusting
freely for all three double-counts the same difference and inflates every comp
at once. No single comp can be moved more than 15% from its actual sale price,
and missing listing fields are skipped rather than treated as zero. The report
flags when the ARV drifts more than 12% from what the comps actually sold for
per square foot — if the adjustments are outvoting the sales, you should know.

**5. Estimates the renovation.** Scope tiers from cosmetic to gut rehab, priced
per square foot at **investor-grade cost** — stock cabinets, LVP, volume
pricing, investor-friendly crews — not homeowner remodel rates, which overstate
a flip budget by 40-50%.

Tiers are calibrated so a standard rehab (full kitchen, baths, flooring, paint)
lands at **$30-40/sqft all-in** at a typical market, matching real operator
numbers rather than published averages. Base rates run $13/sqft cosmetic to
$65/sqft gut, before market factor and contingency.

Only the **labour share** (50%) is scaled to the local market, because drywall,
toilets and flooring cost about the same everywhere. Scaling the whole cost
double-discounts cheap markets into impossible budgets and inflates expensive
ones past reality.

Add known big-ticket items (roof, HVAC, rewire, foundation) and a contingency.
Or just enter a contractor's bid, which overrides the tier maths entirely.

**6. Solves for the offer.** Maximum Allowable Offer is found numerically, not
with a rule of thumb, because closing costs, down payment and loan interest all
scale with the purchase price itself. The model accounts for:

- purchase and buy-side closing costs, including transfer tax where the buyer
  customarily pays it
- renovation plus contingency
- holding costs (taxes, insurance, utilities, HOA) over the hold period
- hard-money financing — points, and interest with rehab draws funding
  progressively
- selling costs — commission, concessions, transfer tax, closing

The familiar 70% rule is reported alongside it as a sanity check.

**7. Tells you what could go wrong.** Stress tests the deal against a low ARV,
a 25% renovation overrun, and both at once. Then flags the things that quietly
kill flips: comps that disagree, thin comp sets, pre-1950 construction, and
above all an asking price far below the neighbourhood — which is almost never
generosity.

---

## Finding leads before they are deals

`python hunt.py` (or the **Leads** tab) looks for owners under pressure before it
shows up as a price. Everything below is keyed to Oklahoma public records.

| Source | How early | How it works |
|---|---|---|
| **Court filings** | Earliest | Oklahoma foreclosures are lawsuits, filed on OSCN months before a sheriff's sale. Probate and divorce are in the same index. OSCN has a human check, so the app builds the search link, you open it and save the page, then paste it back. The app classifies every case (lender suits, estates, divorces) and matches defendants to parcels on the tax roll. |
| **Tax delinquency** | Early | The county treasurer's roll, county-wide, unpaid-only. Three years behind is eligible for the June resale; two years is one bill from it. Resolved to street address, owner, mailing address and assessed value. Automatic. |
| **City liens** | Early | Special assessments on the same roll: mowing, clean-up, demolition. Code enforcement has already been to the property. Automatic. |
| **Expired listings** | Mid | Tried to sell, could not, no agent now. Filtered to sale-priced records so rentals stay out. Automatic. |
| **Listing language** | Mid | as-is, investor, estate, short sale, cash only, damage, motivated, vacant, life event. Patterns are written to dodge "real estate", "fireplace" and "new roof". Automatic. |
| **Price cuts** | Mid | Every scan snapshots list prices; reductions show up from the second scan on. Automatic, accumulates. |
| **Stale / underpriced** | Mid | 90+ and 180+ days on market; listed below the last sale price; listed under 75% of the Realtor estimate. Automatic. |
| **Sheriff's sales** | Late | The county sheriff's page, parsed when a list is posted. Also docketed on OSCN. |

Every lead carries its signals and a 0-100 score. A listed property that is also
on the delinquent roll gets both merged and floats to the top -- that intersection
is the shortlist.

```bash
python hunt.py --tax-details 500 --csv leads.csv
python hunt.py --oscn saved_results.html          # classify a saved OSCN page
python hunt.py --taxcheck "1552 S Maple Ave, Bartlesville, OK"
```

Every county in the region table is on the treasurer's system, so tax
delinquency is pulled region-wide; the home county gets the largest address-
resolution budget. The assessed-to-market ratio used for implied values is
calibrated per county from current listings (it came out near 8%, not the
statutory 11%), and cached for a month.

---

## Looking up a house you drove past

`python hunt.py --dossier "717 SW Hickory Ave, Bartlesville, OK"` (or the
**Property record** tab) answers the questions a boarded-up house raises:

- **Who owns it** and **where their mail goes**, from the treasurer's roll. The
  mailing address is the legally reliable way to reach an owner, and when it
  differs from the property the owner is absentee.
- **Are they paying** - unpaid years, amount owed, city liens, and the annual
  bill.
- **How title has moved** - the owner name per tax year, with changes called
  out. A fractional interest ("1/2 INT") that consolidates usually means a
  co-owner died or was bought out.
- **Listing history** on Realtor.com, including the agent's contact details if
  it is currently listed.
- **What it means and how to approach**, read the way an experienced buyer
  would: taxes current plus absentee plus boarded is a different conversation
  from three years delinquent.
- **One-click links** to the county clerk's deed and mortgage index (search the
  surname as grantee/grantor), court records under the surname (probate,
  foreclosure, divorce), the assessor's record card, Street View, and
  skip-trace sites pre-filled with each name on title.

Phone numbers and emails are not public record, so the app does not scrape
them; it hands off to the skip-trace links with the name and city filled in.
Always confirm title at the county clerk before offering.

---

## Places you do not want to see

Some towns are not worth the drive. Put them on the skip list and they vanish
from deal scans, lead hunts, the daily report and the history store:

```bash
python hunt.py --exclude "Chelsea, Nowata"
```

or use the **Places to skip** box on the Find deals and Leads tabs. A name that
is also a county (Nowata) drops the whole county from the region, which makes
scans faster too; a town name (Chelsea) is filtered out of its county. Sales in
skipped towns still count as comps - they are evidence, just not targets. The
list lives in `prefs.json`.

---

## The daily report

Every lead hunt and deal scan is recorded in a history store keyed by property,
so the app knows what it has seen before. That gives you a diff instead of a
list: what is **new** since last time, what **changed** (went from two years
behind to three, picked up a city lien, cut its price, verdict moved), and what
**dropped off** (sold, paid up, delisted).

```bash
python hunt.py --daily --open      # hunt leads + scan deals, diff, write reports/, open it
python hunt.py --since 7           # what changed in the last 7 days, no scan
```

Reports land in `reports/YYYY-MM-DD.html` with a copy at `reports/latest.html`.
The web app shows the same diff at the top of every Leads and Find-deals run,
and the **What's new** button on the Leads tab reads the history without
scanning (1, 3, 7 or 30 days).

New deal-scan candidates are comp-verified on the daily run; candidates seen
before are not re-verified, which keeps the run to a few minutes.

To run it every morning, register `daily.bat` once with Task Scheduler (from
this folder, in a normal command prompt):

```bash
schtasks /create /tn "FlipComp daily" /tr "\"%CD%\daily.bat\"" /sc daily /st 06:30 /f
```

The first run only establishes the baseline. Differences appear from the
second run on.

---

## Finding deals across a region

Running the full comp engine on every listing in a region would take hours, so
the search is a funnel:

**Stage 1 — screen.** Pulls every active listing and 12 months of sales for
each county, then for each listing finds nearby same-size sales and takes their
median price per square foot as a quick ARV. Applies the renovation and offer
models and ranks by how far the maximum offer sits above the asking price.
Around 575 listings against 2,173 sales takes under a minute.

**Stage 2 — verify.** Runs the real single-property analysis on the strongest
candidates only: proper comp selection, adjustment, weighting, confidence and
risk flags.

Stage 1 is deliberately approximate and errs optimistic, so it keeps borderline
deals rather than discarding them. It is usually within a few percent but can be
badly wrong on unusual rural properties — one 3,900 sqft acreage screened at
$688k and came back at $366k once properly comped. **Only the verified numbers
should inform an offer**; screened-only rows are a shortlist, not an answer.

---

## Reading the output

| Field | What it means |
|---|---|
| **ARV** | What it resells for renovated. The range matters more than the point estimate. |
| **Confidence** | Comp count, agreement, distance and recency, out of 100. Under 55, verify by hand. |
| **Max offer** | Pay more than this and you miss your profit target. |
| **Suggested opening** | ~8% under max, so there's room to be negotiated up. |
| **Cash required** | Out of pocket — down payment, the unfinanced rehab, closing and carry. |

Verdicts: **PURSUE** (clears target at asking) · **NEGOTIABLE** (within ~12%) ·
**THIN** (needs a real concession) · **PASS** (spread isn't there) ·
**NOT VIABLE** (fails at any price, even free).

Any verdict that loses money at the low end of the comp range is downgraded to
**PURSUE WITH CAUTION** or **NEGOTIABLE - FRAGILE**. A healthy headline spread
with a negative downside is not a clean buy, and the tool will not present it
as one.

---

## State cost handling

Transaction costs are set automatically from the property's state. This matters
more than it sounds: on a $300,000 ARV deal the max offer swings about $8,700
between Dallas and Philadelphia purely on transfer tax and property tax.

Two details the model gets right that rules of thumb miss:

- **Transfer tax has a customary payer**, and it differs by state. In Tennessee
  and Vermont the buyer pays; in about ten states it is split. A flipper is both
  a buyer and a seller, so in those states the tax is charged **twice per flip** -
  once on acquisition, once on resale.
- **Local add-ons dwarf state rates** in a few places. Philadelphia is 4.28%
  against Pennsylvania's 1% state rate; NYC, Chicago, Seattle, Oakland,
  San Francisco, Miami-Dade and Detroit are handled too. States with wide county
  variation are flagged so you know to verify.

Property taxes use the **actual annual tax figure from the listing** whenever
it is available. The state effective rate is only a fallback, and the report
tells you which one was used.

The table lives in `flipcomp/states.py`. Rates are 2026 state-level figures and
should be confirmed against a local title company before you write an offer -
county and municipal add-ons are common.

---

## Tuning it

Everything is overridable in the web UI under *Assumptions & renovation scope*,
or by flag on the CLI. The defaults worth revisiting for your market:

- `transfer_tax_pct` — set automatically from the state; override for your county
- `agent_commission_pct` — 5% default
- `interest_rate_pct` / `points_pct` — hard money, 10.5% and 2 points
- `target_profit_pct` / `min_profit_flat` — 15% of ARV, floor $25,000

Defaults live in `flipcomp/offer.py`. Renovation tier rates and line-item
pricing live in `flipcomp/rehab.py`. Comp selection tolerances and adjustment
factors live in `flipcomp/compengine.py` — all at the top of each file.

---

## Layout

```
server.py              local web server (stdlib only, no framework)
cli.py                 command line / batch screening
static/index.html      the interface
hunt.py                lead hunter (tax roll, court filings, expired, language)
daily.bat              scheduled entry point for the daily report
reports/               daily HTML reports
flipcomp/
  history.py           run history and diffing
  report.py            daily report rendering
  prefs.py             places to skip, stored in prefs.json
  dossier.py           one-address public-record lookup and contact plan
  leads.py             lead sources, scoring, cross-referencing
  taxroll.py           county treasurer tax-roll client
  oscn_leads.py        court-record link builder, parser, classifier
  data.py              property lookup and sold-pool fetch, disk cache
  compengine.py        comp selection, adjustment, reconciliation -> ARV
  rehab.py             renovation cost tiers and line items
  offer.py             deal P&L and max-offer solver
  states.py            per-state transfer/property tax defaults
  flags.py             risk warnings
  analyze.py           orchestration
  geo.py               distance
```

Sold-sales pulls are cached in `.cache/` for 6 hours, so re-running a property
or working several deals in one market is fast after the first call.

---

## Limits worth knowing

**US only.** Sales data comes from Realtor.com MLS listings, and the cost model
assumes US transaction structure.

**Non-disclosure states.** Texas, Missouri, Kansas, Utah, Idaho, Louisiana,
Mississippi, Montana, New Mexico, North Dakota, Wyoming and Alaska do not make
sale prices public, so the MLS feed carries no sold price. Those markets fall
back to the price each home was listed at when it sold — usable, but flagged
and confidence-penalised. **Oklahoma is not affected**: sold prices are fully
available in every Oklahoma market tested (OKC, Tulsa, Norman, Broken Arrow,
Edmond, Lawton), all with deep comp pools.

**This is an estimate, not an appraisal.** It's a screening tool to decide which
properties deserve a walkthrough — not a substitute for one. The renovation tier
is the single biggest lever on the answer and it's a guess until you've been
inside the house.

**Data gaps happen.** Square footage and year built are occasionally missing or
wrong in listing data; both are overridable. Off-market and pre-foreclosure
properties generally won't resolve at all.

**Scraped data.** Realtor.com can change its structure or rate-limit, which is a
dependency on `homeharvest` continuing to work. The data layer in
`flipcomp/data.py` is isolated behind `lookup_subject()` and `fetch_sold_pool()`,
so swapping in a paid API (RentCast, ATTOM) means rewriting those two functions
and nothing else.
