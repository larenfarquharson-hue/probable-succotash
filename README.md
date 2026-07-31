# SpendTrack

Accounts for every outflow on a bank account, from CSV statements and photos of
till slips, and says what could be cut.

Local-first: your statements never leave the machine. No dependencies beyond the
Python 3.11 standard library. OCR is optional.

```
$ spendtrack import june.csv july.csv
$ spendtrack slip add slips/
$ spendtrack report 2026-06 --html report.html
```

---

## The problem it actually solves

Feeding a tracker from two sources — statements *and* till slips — invites double
counting, because most till slips describe money the statement already shows.
SpendTrack treats the statement as the single source of truth for how much was
spent, and slips as evidence of *what* it was spent on.

Three rules follow from that, and they are enforced in code and in tests:

**1. Statement lines are the only thing that counts as money.**
Each line gets a deterministic fingerprint from account, date, amount and a
normalised description. Re-import the same file, or two exports whose date ranges
overlap, and the repeated lines are recognised and skipped. Two genuinely
identical purchases on the same day both survive, because the fingerprint
includes an occurrence number.

**2. A card slip is the same money as its statement line.**
Slips are matched on amount, date proximity and merchant-name similarity. A match
copies the slip's better category and line items onto the transaction and adds
exactly zero rand. Slips that match nothing are listed in the report and excluded
from every total — either the statement covering them is missing, or they were
paid from an account SpendTrack does not have.

**3. A cash slip is a breakdown of an ATM withdrawal, not new spending.**
The withdrawal already left the account. So a cash slip is *allocated against*
it: R620 of a R2,000 withdrawal moves from "Cash Withdrawals" to "Groceries", the
period total does not budge, and the remaining R900 is reported as
"still unexplained" rather than quietly disappearing. Cash slips that exceed the
cash withdrawn are flagged, never counted — that would be inventing spending.

Every report ends with a reconciliation line confirming the buckets add back to
the total that left the account.

---

## Install

```bash
git clone <this repo>
cd probable-succotash
python3 -m spendtrack --help
```

Optionally put it on your path:

```bash
alias spendtrack='python3 -m spendtrack'
# or, from the repo root:
pip install -e .
```

Data lives in `~/.spendtrack` (`spendtrack.db`, `settings.json`, `rules.json`).
Override with `SPENDTRACK_HOME` or `--home`.

Requires Python 3.11 or newer. Nothing else is needed. `tesseract` unlocks
reading slip photos directly, and everything works without it.

---

## Importing statements

Start by checking how a file will be read. This writes nothing:

```bash
spendtrack inspect statements/june.csv
```

```
  encoding        : utf-8-sig
  delimiter       : ','
  header row      : 6
  date column     : [0] Transaction Date
  description     : [1] Description
  amount column   : [2] Amount
  balance column  : [3] Balance
  sign convention : signed
  date format     : %d/%m/%Y
  rows parsed     : 42
  rows skipped    : 1
```

The importer works out for itself:

- **Encoding** — UTF-8 with or without a BOM, CP1252, Latin-1.
- **Delimiter** — comma, semicolon, tab or pipe.
- **Header row** — skipping the account-details preamble banks put on top.
- **Columns** — by header name, with many aliases per field. Files with no header
  at all are inferred from the shape of the values.
- **Sign convention** — whether a positive amount means money in or money out.
  Where a running balance is present it is used to *prove* which, and the report
  says so. Separate Debit/Credit columns and Dr/Cr indicator columns both work.
- **Dates** — one format is chosen that parses every date in the file, which is
  what stops `01/02/2026` being read as 2 January in one row and 1 February in
  the next.
- **Amounts** — `R1 234,56`, `1,234.56`, `(450.00)`, `450.00-`, `450.00 Cr`.
- **Per-line fee columns** — some exports put a fee beside the purchase it
  relates to. That fee is a separate outflow, so it becomes its own transaction.
  Without this the period will not reconcile against the closing balance.

Then import. Repeating an import is always safe:

```bash
spendtrack import statements/*.csv --account cheque
```

If the detection got something wrong, write a profile. `spendtrack profiles`
shows where they live and `spendtrack/profiles/example.json` documents every
option. No bank-specific profiles ship with SpendTrack: export layouts change,
and a stale profile is worse than detection. Build yours from a real export.

```bash
spendtrack import june.csv --profile mybank
spendtrack import june.csv --positive-is outflow   # one-off override
spendtrack import june.csv --dry-run               # see the effect first
```

---

## Adding till slips

Three routes in. They all produce the same thing, so use whichever fits.

### 1. Slip JSON — always available

```bash
spendtrack slip template > slip.json     # shows the format
spendtrack slip add slip.json
spendtrack slip add slips/               # a whole folder
```

```json
{
  "merchant": "Checkers Hyper Sandton",
  "date": "2026-06-02",
  "time": "17:42",
  "total": 1842.66,
  "tax": 240.35,
  "payment_method": "card",
  "card_last4": "8891",
  "image": "IMG_0231.jpg",
  "items": [
    {"description": "Full cream milk 2L", "qty": 2, "unit_price": 32.99, "total": 65.98}
  ]
}
```

One object or a list of them per file. `items` is optional but makes the report
much more useful. `payment_method` matters: `cash` sends the slip down the
withdrawal-allocation path instead of looking for a card transaction.

This is also the format to target if you have something else read the photo for
you — an AI assistant, a receipt-scanning service, a colleague with a keyboard.
Write the JSON, add it, and the matching and validation work identically.

### 2. OCR from photos — if tesseract is installed

```bash
sudo apt install tesseract-ocr     # or: brew install tesseract
spendtrack slip add photos/IMG_0231.jpg
```

The text parser handles South African till slip layouts: it finds the shop name,
date and time, distinguishes `TOTAL` from `SUBTOTAL`, `VAT`, `CASH` tendered and
`CHANGE`, reads line items with `2 x 32.99` quantities, and picks up the card's
last four digits. Slip photos vary enormously, so every OCR result is checked
against its own line items and anything that does not add up is reported:

```
slip 7: Checkers Hyper R1,842.66 2026-06-02 [card]
  check: line items sum to 602.87 but the total says 1842.66
```

Fix it in the database, or delete and re-add from JSON. The total is the number
that matters, and OCR gets that wrong less often than it garbles item names.

### 3. Type it in

```bash
spendtrack slip enter
```

Slow, but never wrong about the total, and needs nothing installed.

### Slips are never stored twice

A slip's identity is the purchase it describes — merchant, date, time, total and
items — not the filename. Two photos of one slip are one slip. Two genuinely
separate purchases at the same shop, same day, same amount *and* no time printed
on the slip will collide; `spendtrack slip add --force` overrides that.

---

## Matching

Importing statements or adding slips runs the matcher automatically. To run it
alone:

```bash
spendtrack match
spendtrack match --rematch     # discard automatic links and redo them
spendtrack match --dry-run
```

```
3 matched to statement lines, 2 allocated against cash withdrawals, 1 unmatched
  [matched] slip 1 Checkers Hyper Sandton City R1,842.66 -> txn 4
      amount exact, same day, merchant similarity 0.85, card number matches
  [cash] slip 4 Sandton Market Butchery R620.00 -> txn 9
      paid from cash; allocated against the withdrawal on 2026-06-04
      (R900.00 of it still unexplained)
  [unmatched] slip 6 Rhapsody's Melrose Arch R1,250.00
      no statement line within the matching window — import the statement
      covering this date, or it was paid from another account
```

Every link shows its reasoning and score. Where the scorer is wrong, fix it by
hand — manual links survive `--rematch`:

```bash
spendtrack slip link 6 87      # slip 6 is the evidence for transaction 87
spendtrack slip unlink 6
```

Tuning, if the defaults do not suit your bank's posting delays:

```bash
spendtrack settings --match-window 7      # days either side of the slip date
```

---

## The report

```bash
spendtrack report 2026-06
spendtrack report last-month --html ~/reports/june.html
spendtrack report 2026-06-15:2026-07-14 --json june.json
spendtrack report all --account cheque
```

Periods: `YYYY-MM`, `YYYY`, `YYYY-MM-DD:YYYY-MM-DD`, `this-month`,
`last-month`, `ytd`, `all`.

The report has seven parts:

**Where every rand went** — the reconciliation. Total out, split into
consumption, debt repayments, savings, transfers between your own accounts, and
anything you excluded. These are non-overlapping and add back to the total.
Savings and debt capital are outflows but not consumption, and are never mixed in
with spending.

**By type of spend** — categories, with the share of each and how much came from
cash slips. Grouped as Essentials / Getting around / Household / Lifestyle /
Discretionary / Avoidable cost.

**By merchant** — total, count and average per merchant. Merchant names are
normalised, so "CHECKERS HYPER SANDTON CITY" and "CHECKERS SANDTON" are one
merchant, and a changing card number or reference does not split them.

**What the cash bought** — the withdrawal, what slips explain, what remains
unexplained.

**Monthly commitments** — split into *fixed* (same amount every month: the
subscriptions and debit orders decided once and never revisited) and *variable*
(recurs monthly, amount moves: electricity, a monthly big shop). Detection groups
on the normalised description, so a changing reference number cannot hide a
repeat charge, and requires roughly one occurrence a month — four coffees a month
is a habit, not a commitment, and is reported as one.

**What could be cut** — the point of the exercise. See below.

**Worth knowing about these numbers** — what is uncategorised, what cash is
unexplained, which slips did not match, how many months are loaded. Read this
before trusting anything above it.

The `--html` report is a single file with no external requests: inline SVG
charts, light and dark themes, print styles. It opens from disk and works
offline, which is the only sensible way to handle a file containing your bank
statement.

---

## How the reduction suggestions are calculated

This is the part most easily fudged, so here is exactly what happens.

**Each category carries a discretion weight** — the share of it that is
realistically reducible. Gambling is 1.0. Bond repayments are 0.0. Groceries are
0.2, because the lever is brand and shop choice, not eating less. Coffee and
snacks are 0.8. These are starting points, not facts about your life, and they
are all editable in `~/.spendtrack/rules.json`.

**Suggestions do not overlap.** They are generated in order of how specific and
clear-cut they are, and each one is sized only on transactions no earlier
suggestion has already claimed. The same R1,367 of streaming cannot appear in
the subscriptions suggestion, the discretionary-category suggestion and the
income benchmark. That is what makes the total at the bottom addable.

**Annual figures are scaled by observed frequency.** Multiplying one month by
twelve turns a single R750 traffic fine into a R9,000-a-year problem. Instead,
for each category SpendTrack counts in how many of the loaded months it appears
at all, and scales the projection by that share. With one month of data it cannot
tell, so it says so in the report and assumes a typical month. This gets more
accurate as you load more history — three months is where it starts being worth
trusting.

**Savings are a fraction of the discretionary portion, not all of it.** A
category rarely gives up everything it theoretically could, so the estimate
generally assumes half of the discretionary share goes. The habit suggestions
assume the frequency halves rather than stopping, because that is what people
actually manage.

**Context is separated from savings.** The income-share benchmark and the
unexplained-cash gap are reported with no rand claim attached, because neither is
money you can decide to stop spending.

What it looks for:

| Suggestion | Basis |
|---|---|
| Betting and lottery | Full amount; nothing in a budget depends on it |
| Bank charges, interest, fines | Split into recurring fees (40% recoverable) and penalties, failed debit orders and fines (90%) |
| Food delivery premium | ~25% of order value is delivery, service and small-order fees rather than food |
| Habit spend | A merchant with four or more small discretionary purchases; assumes the frequency halves |
| Parallel services | Two or more fixed commitments in one category; keeping the largest |
| Discretionary recurring charges | Fixed monthly charges in optional categories; assumes a third are cancelled |
| Whatever is left over | Remaining optional categories at half their discretion weight |

Each suggestion states its confidence (high / medium / low), the evidence behind
it, and one concrete action.

---

## Categorisation

Rule-based, with about 700 merchant patterns weighted towards South African
retailers, banks, insurers, telecoms, delivery platforms and betting operators.
Patterns are matched against a normalised description with card masks, embedded
dates, reference numbers and channel prefixes removed, so
`CARD PURCHASE 4123****8891 CHECKERS HYPER SANDTON 02 JUN` is tested as
`checkers hyper sandton`.

All patterns from all rules are ranked together by specificity, so
`checkers sixty60` beats a bare `checkers` without anyone hand-ordering the
table.

To see what happened and correct it:

```bash
spendtrack review                          # walk the uncategorised ones
spendtrack categorise 412 "Health & Fitness"
spendtrack categorise 88 Transfers --internal    # your own account
spendtrack categorise 91 Groceries --exclude     # leave out of totals
```

Corrections are stored against the normalised description, so a decision made
once applies to every past *and future* transaction that matches. Manual
decisions always outrank rules and slips.

Your own rules go in `~/.spendtrack/rules.json` and are tried before the
built-ins:

```bash
spendtrack rules init      # writes a starter file
spendtrack rules show
```

```json
{
  "rules": [
    {
      "id": "my-landlord",
      "category": "Housing & Rent",
      "merchant": "Landlord",
      "patterns": ["jones properties", "re:^rent \\w+"],
      "flags": ["subscription"]
    }
  ],
  "categories": {
    "Coffee & Snacks": {"discretion": 0.9}
  }
}
```

Patterns are plain substrings unless prefixed with `re:`, and are tried against
both the normalised description and the raw one, so a regex expecting digits
still works.

---

## Every command

| Command | What it does |
|---|---|
| `import PATH...` | Import statement CSVs. Safe to repeat. |
| `inspect PATH` | Show how a CSV would be read. Writes nothing. |
| `slip add PATH...` | Ingest slip JSON, images, or a folder |
| `slip enter` | Type a slip in by hand |
| `slip list [--status S]` | List stored slips |
| `slip show ID` | One slip in full, with items and its link |
| `slip template` | Print the slip JSON format |
| `slip link SLIP TXN` | Link by hand; survives `--rematch` |
| `slip unlink SLIP` | Undo a link |
| `slip delete ID` | Delete a slip |
| `match` | Match pending slips |
| `report [PERIOD]` | The full report; `--html`, `--json`, `--quiet` |
| `compare PERIOD...` | Category totals side by side |
| `list [PERIOD]` | List transactions; filter by category, merchant, text, amount |
| `review [PERIOD]` | Walk through uncategorised transactions |
| `categorise TXN CAT` | Set a category and remember it |
| `recategorise` | Re-run rules; keeps manual decisions |
| `categories` | Categories and their discretion weights |
| `profiles` | Available bank profiles and where they live |
| `rules [path\|init\|show]` | Manage your rules file |
| `settings` | Show or change settings |
| `imports` | Every import, with counts |
| `undo-import ID` | Remove everything one import added |
| `audit-duplicates` | Repeated transactions that might be double counted |
| `status` | What is loaded and what needs attention |

Settings worth setting:

```bash
spendtrack settings --income 42000        # enables the share-of-income context
spendtrack settings --currency R
spendtrack settings --small-threshold 150 # what counts as "small" habit spend
spendtrack settings --match-window 4      # slip matching window, in days
```

---

## Known limitations

Stated plainly, because knowing where a tool is weak is what makes it usable.

- **Two identical transactions on one day, imported from two differently
  formatted exports.** The occurrence number that separates genuine repeats is
  assigned per file. It is stable across exports as long as a statement covers
  whole days, which is the normal case. `spendtrack audit-duplicates` lists
  anything suspicious; a re-import shares the same running balance, while genuine
  repeats do not.
- **OCR quality.** Slip photos vary hugely and tesseract is not built for
  thermal receipts. Totals come out well; item names often do not. Everything is
  validated against its own arithmetic and problems are reported rather than
  hidden, but review anything OCR'd before relying on the line-item detail.
- **Annual projections from one month.** Honest but rough. Load three months or
  more.
- **Discretion weights are opinions.** They are visible, documented and editable.
  If you disagree with one, change it — the report will change with it.
- **Delivery premium is an estimate.** The real split between food and fees only
  appears on the platform's invoice, never on the bank line. Flagged medium
  confidence for that reason.
- **Multi-currency is not handled.** Foreign transactions are counted at the
  rand value the bank posted, which is correct for spend tracking, but there is
  no currency conversion or FX-fee analysis.
- **One rule engine, no learning.** It does not infer categories from your
  corrections beyond remembering the exact normalised description. That is a
  deliberate trade: predictable and inspectable over clever.

---

## Development

```bash
python3 -m unittest discover -s tests -q     # 144 tests, no dependencies
```

The tests worth reading first are in `tests/test_doublecounting.py`. They assert
the invariants the whole design rests on: re-imports change no total, overlapping
statement periods do not accumulate, a matched slip adds no money, a cash slip
only moves value sideways, two slips cannot claim one transaction, and the
reconciliation buckets always add back to the total.

```
spendtrack/
  cli.py            command line interface
  csvimport.py      statement CSV detection and parsing
  parsing.py        tolerant date and amount parsing
  normalise.py      description normalisation, fingerprints, fuzzy comparison
  ingest.py         statements into deduplicated transactions
  slips.py          slip ingestion: JSON, OCR, interactive
  matching.py       slip-to-statement matching and cash allocation
  categorise.py     the rule engine
  rules_default.py  built-in merchant rules
  taxonomy.py       categories, groups and discretion weights
  analysis.py       reconciliation, summaries, recurrence, suggestions
  report_text.py    terminal rendering
  report_html.py    self-contained HTML rendering
  db.py             SQLite schema
  config.py         paths and settings
```

## Privacy

Bank statements are among the most revealing documents a person has. Nothing here
sends data anywhere: no network calls, no telemetry, no cloud. The database is a
plain SQLite file in `~/.spendtrack` — back it up like anything else you would
not want to lose, and be aware it is not encrypted at rest. If you use full-disk
encryption you already have the protection that matters most.

The only step that can involve anything external is reading slip photos, and only
if you choose a route that does. Local tesseract does not. Handing a photo to an
online service does — that is your call, and if the data is sensitive to your
employer, check the policy that applies to you first.
