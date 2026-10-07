# Getting houses before they hit the market

The listed market is an auction you join late. By the time a house is on the
MLS the seller has an agent, a price and a dozen investors calling. The deals
are the owners who would sell to a fair cash offer but have not yet told
anyone - and public records point straight at them.

FlipComp finds those owners. This is how to work them.

---

## 1. Build the list (stack the signals)

One signal is noise; three is a seller. The app scores every lead by adding
signals together, so the top of each list is already stacked.

| Signal | Where it comes from | In the app |
|---|---|---|
| 3 years unpaid property tax | County treasurer roll | Leads tab, automatic |
| City liens (mowing, boarding, clean-up) | Treasurer special assessments | Leads tab, automatic |
| Foreclosure suit filed | OSCN court records | Leads tab, paste a saved search |
| Probate opened | OSCN court records | Leads tab, paste a saved search |
| Expired listing | Realtor.com | Leads tab, automatic |
| Out-of-state owner | Assessor roll | Import the roll (step 2) |
| Owned 15-25+ years | Assessor roll | Import the roll |
| No homestead exemption | Assessor roll | Import the roll |
| Estate, heirs or trust on title | Assessor roll | Import the roll |
| Senior valuation freeze | Assessor roll | Import the roll |
| Boarded, overgrown, vacant | Driving the neighbourhood | Property record tab |
| In Owasso or Collinsville schools | Census district boundaries | Added to every score |

Owasso and Collinsville are scored up because school district is what retail
buyers pay for; the boundaries come from the Census Bureau, not ZIP codes,
which do not follow district lines.

---

## 2. Get the full parcel roll (once a year)

The tax roll only shows owners who are behind. The assessor's roll shows
everyone - which is where out-of-state, long-held and inherited houses are.
Oklahoma's Open Records Act (51 O.S. § 24A.1 et seq.) makes it a public
record; counties usually charge a modest copying or search fee, and some
charge more for commercial use, so ask for a quote first.

Send one request to each assessor. Owasso and Collinsville schools straddle
two counties, so you need both.

> **Tulsa County Assessor** - open records requests via the Resources page at
> assessor.tulsacounty.org
> **Rogers County Assessor** - Claremore; use the open records contact on the
> Rogers County website

**Request text (copy, fill in your name, send):**

> To the Records Custodian, [County] County Assessor's Office:
>
> Under the Oklahoma Open Records Act, 51 O.S. § 24A.1 et seq., I request an
> electronic copy (CSV or Excel) of the current real property appraisal roll
> for all residential parcels within Owasso Public Schools (I-11) and
> Collinsville Public Schools (I-6), with the following fields where kept:
>
> - parcel or account number
> - owner name(s)
> - owner mailing address, city, state and ZIP
> - property (situs) address and city
> - homestead exemption status and senior valuation freeze status
> - most recent deed or sale date and sale price
> - year built and living area
> - land use or property class
> - school district
> - market and assessed value
>
> If the full roll is easier to produce than a district extract, the full
> county roll is fine. Please let me know any fee before processing.
>
> Thank you,
> [Your name] - [phone] - [email]

When the file arrives:

```bash
python hunt.py --import-roll tulsa_roll.csv --county tulsa
python hunt.py --import-roll rogers_roll.xlsx --county rogers
```

or use **Import assessor roll** on the Leads tab. The importer reads whatever
column names the county uses. Re-import each year; the app keeps history, so
it tells you which owners are new to the list.

A matching request to each **county treasurer** for "real property parcels
with delinquent ad valorem taxes, with owner and mailing address" fills the
gap where the treasurer's website is not on the shared system (Tulsa County).

---

## 3. Reach the owner

**Mail first.** A plain letter to the mailing address on the roll is the
highest-converting, lowest-friction first touch for absentee and inherited
houses. The app writes them: set your name and phone on the Pipeline tab, then

```bash
python hunt.py --letters 100 --focus-only --open
```

The letter is short, local and never mentions taxes, liens or court cases -
people who are behind know it, and a stranger pointing it out reads as a
threat.

**Follow up on a rhythm.** Most owners answer on the third to sixth touch,
not the first. Every lead you mail goes into the Pipeline at "Contacted"; the
app sets the next follow-up and the daily report puts it at the top when it
is due. A sensible cadence is a letter every 30-45 days, with a postcard or
second letter in between for the highest scores.

**Phone and text carefully.** Phone numbers come from skip-trace sites (the
Property record tab pre-fills the searches). Before calling or texting a
number you found that way, check it against the National Do Not Call
Registry, and do not send automated or bulk texts without consent - the
federal rules on this carry real per-message penalties.

**Probate.** The personal representative named on the OSCN case is the
person with authority to sell, and the attorney of record is often the
fastest route to them. A short, respectful letter to the representative
after the case has been open a few weeks works better than contact at the
funeral stage.

**Pre-foreclosure.** The OSCN filing date starts a clock: in Oklahoma a
judicial foreclosure usually runs several months to a sheriff's sale. Early
contact gives the owner time to sell for more than the debt and walk away
with equity. Buying from an owner in foreclosure can carry extra legal
requirements; have a real estate attorney review the contract on any such
deal.

**Drive for dollars.** Boarded windows, tall grass, piled mail and tarped
roofs in Owasso and Collinsville neighbourhoods are leads no list has yet.
Put the address into the Property record tab from your phone and add it to
the Pipeline on the spot.

---

## 4. Make the offer

When an owner calls, run the address on the Analyze tab. Start from the
suggested opening offer, never above the maximum, and know your other exits:
if the flip number is thin, the wholesale and rental figures on the same
page tell you whether to assign it or keep it. Every offer and every answer
goes in the Pipeline log, so the next conversation starts where the last one
ended.

---

## 5. Keep the machine running

```bash
python hunt.py --daily --open    # every morning (or schedule daily.bat)
```

The daily report leads with follow-ups due, then new leads in Owasso and
Collinsville schools, then everything else that changed. Weekly: paste a
fresh OSCN search for Tulsa and Rogers counties. Yearly: re-request the
assessor rolls.
