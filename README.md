# spendtracker

Track **every** outflow on your bank account, work out where the money actually
went, and get concrete suggestions for what to cut — without double counting a
single cent.

Fed by two things:

- **Bank statement CSVs** — the authoritative record of money leaving the account.
- **Photographs of till slips** — evidence about *what* that money bought.

It runs entirely on your own machine. Nothing is uploaded anywhere unless you
turn on AI slip reading, and even then only the slip images are sent.

---

## Quick start

The command line tool has **no dependencies at all** — Python 3.10+ and nothing
else. Clone it and run it:

```bash
git clone <this repo> && cd probable-succotash

# Generate fictional sample statements so you can see it work first
python3 samples/generate_samples.py

python3 -m spendtracker.cli inspect samples/statement_march.csv   # dry run first
python3 -m spendtracker.cli import-statement 'samples/*.csv'
python3 -m spendtracker.cli report --period 2026-03
python3 -m spendtracker.cli advice --period 2026-Q1
```

Then point it at your own statements:

```bash
python3 -m spendtracker.cli inspect ~/Downloads/statement.csv
python3 -m spendtracker.cli import-statement ~/Downloads/statement*.csv
```

Install it so `spendtracker` is on your path:

```bash
pip install -e .
```

### Optional extras

Everything below is opt-in. Each one degrades to a clear message rather than an
import error, so a bare install is a complete, working tool.

```bash
pip install -e '.[web]'    # the web UI:  spendtracker serve
pip install -e '.[ai]'     # AI till-slip reading (sends slip images to Anthropic)
pip install -e '.[dates]'  # more forgiving date parsing on unusual exports
pip install -e '.[all]'    # all of the above
```

```bash
spendtracker serve         # web UI on http://127.0.0.1:5000, needs [web]
```

Why bother: bank statements are the last thing you want to hand to a machine
you do not control, and "no dependencies" means this runs on a locked-down
laptop with no package index. `tests/test_no_dependencies.py` enforces it.

---

## The core idea, and why it matters

Most spending trackers double count. You import a statement, re-import an
overlapping range next month, photograph a till slip for a purchase that is
*already* on the statement, and the total quietly inflates. Once you stop
trusting the total, the whole exercise is pointless.

This app is built around one rule:

> **`transactions` is the only ledger of money moving, and only bank statements
> may write to it.**

Everything else is evidence *about* those rows. A till slip is never an outflow.
It is one of three things:

| Situation | What happens | Effect on your total |
|---|---|---|
| Card purchase already on the statement | The slip is **linked** to that bank row and enriches it (real merchant name, line items, better category) | none |
| Paid in cash | The slip is **allocated against a cash withdrawal** the bank already counted, revealing what the cash bought | none |
| Nothing matches it | It sits in a **review queue**, reported separately and never silently added | none |

The third case is the honest one. If a slip cannot be explained, adding it to
the total *might* be right — or might double up on money the bank already showed.
So it is excluded and shown to you, with the amount, so you can decide.

### Cash is handled properly

Withdrawing R2 000 and then spending R450 of it on wine is **one** outflow, not
two. The withdrawal is what left the account. So:

- the R2 000 withdrawal is counted as spend the moment the bank shows it;
- a cash slip for R450 moves R450 **out of** "Cash Withdrawals" and **into**
  "Alcohol & Tobacco";
- the total does not change — only the breakdown gets sharper;
- the R1 550 you never photographed stays visible as *unexplained cash*.

That last number is the one most tools hide. A tidy-looking pie chart that
silently swallows R1 550 of cash is worse than an untidy one that admits it.

---

## What it tells you

### 1. Every outflow, accounted for

The category and merchant breakdowns must sum to the bank's own total, to the
cent. The app asserts this and reports the residual if it ever fails, rather
than presenting a breakdown that does not reconcile.

It also tells you when the report is **incomplete**, which is different from
being wrong:

- **Coverage gaps** — days in the period that no imported statement covers, so
  spend on those days is missing entirely.
- **Balance continuity** — whether the imported rows reproduce the statement's
  own closing balance. If not, rows were dropped.
- **Unexplained cash** — withdrawn, counted, but with no slip to break it down.
- **Held-out duplicates** — the amount currently excluded pending your review, so
  you know spending could be understated by that much.
- **Uncategorised spend** — counted, but not assignable to a category.

### 2. Summaries by type and merchant

Spend by category, by merchant, by how the money left (card, debit order, EFT,
ATM, fees), a daily series, and month-on-month trend with the essential versus
discretionary split.

### 3. Recurring commitments

Subscriptions and debit orders detected from the cadence between charges, with a
"looks cancelled" marker when the last charge is overdue. Annualised off the
canonical cycle (12 monthly, 52 weekly), not the raw gap between charges — three
payments 29 days apart imply 12.6 a year arithmetically, which would overstate a
bond repayment by thousands.

A weekly grocery shop repeats as reliably as Netflix does, so a stable amount is
required before something is offered up as a *cancellable* subscription.

### 4. Frivolous spend and what to cut

Ranked, concrete opportunities — each with the **assumption** behind it, the
evidence, a difficulty rating and a confidence level:

- fines and penalty interest (avoidable in full)
- bank fees (pure leakage — you get no goods for them)
- overlapping services (three video streaming subscriptions doing one job)
- the delivery premium on food orders
- habit spend — small, frequent purchases at one merchant, which is the biggest
  reducible line most people have and the one no single transaction reveals
- gambling
- small subscriptions that renew without anyone deciding to renew them
- price creep on debit orders
- categories spiking above their own recent average

**Savings are never double counted.** A claim ledger caps each category at the
fraction that could plausibly be cut, and findings draw against that ceiling in
order. Without it, "your takeaway spend is high" and "you pay a delivery fee on
every order" would both bank the same rand and the headline number would be
fiction. The report self-checks that the total cannot exceed monthly spending.

Every figure is an **estimate from a stated assumption**, not a measurement. The
app can see that R2 100 went on takeaways; it cannot see whether that was three
celebrations or thirty lazy Tuesdays.

---

## Before and after an import

Two commands exist because importing is the step where a silent mistake becomes
expensive. A wrong guess about sign convention inverts every number in every
report, and you will not notice from the report itself.

### `inspect` — look without importing

```bash
spendtracker inspect ~/Downloads/statement.csv
```

Parses the file and prints exactly what the importer would conclude: which
column is the date, which is the amount, whether outflows are negative, whether
dates are day-first, the totals it would produce, the first few rows as they
would be stored, and anything skipped. It touches no database and writes no
rows. If something is wrong, fix it with `--profile` before importing rather
than after.

It also flags what is worth a second look — a layout it is unsure about, a file
where everything parsed as money coming *in*, a missing balance column, an
unusual number of skipped lines. `--json` gives the same thing machine-readably.

### `undo-import` — take one back

```bash
spendtracker statements       # list imports with ids
spendtracker undo-import 3
```

Every transaction records the import that created it, so an import can be
reversed exactly: its transactions go, its statement record goes, and the
fingerprints clear so the file can be re-imported cleanly afterwards.

It shows you what will be removed and asks before doing it. If till slips have
since been linked to those transactions it refuses, because those links are
work you did by hand; `--force` proceeds and returns the slips to the review
queue rather than deleting them. A slip is evidence, and it outlives any
particular import of the statement it belongs to.

---

## Bank statement CSVs

There is no standard format, so the importer works the layout out:

- skips preamble rows before the real header;
- matches header wording against a synonym list (`Narrative`, `Details`,
  `Transaction Description`, `Money Out`, `Running Balance`, …);
- handles a single signed amount column **or** separate debit/credit columns;
- detects day-first versus month-first dates from the data itself;
- parses `1 234,56`, `1,234.56`, `(45.00)`, `45.00-`, `R1 234.56`;
- falls back to inferring columns by content when there is no header at all
  (and says so, loudly).

**It then checks its own conclusion against the running balance.** If the amount
signs disagree with the balance movements, they are inverted and the correction
is reported. This is the difference between catching a wrong sign convention and
silently reporting a month's spending as income.

Where the data cannot settle it — one amount column, no negatives, no balance —
you get an explicit warning rather than a confident guess.

### If auto-detection gets it wrong

Add a named layout to `bank_profiles.json` (0-based column indexes) and use it:

```bash
spendtracker import-statement statement.csv --profile my_bank
```

The shipped profiles are **illustrative shapes, not verified copies** of any
bank's current export. Check one against a real export before trusting it.

---

## Till slips

```bash
spendtracker add-receipt ~/Pictures/slips/*.jpg
```

Three readers, set with `SPENDTRACKER_OCR_PROVIDER`:

| Provider | Needs | Notes |
|---|---|---|
| `claude` (default) | `ANTHROPIC_API_KEY` | Much the most reliable on real crumpled, angled, faded thermal slips. Sends the image to Anthropic. |
| `tesseract` | the `tesseract` binary | Local and free. Good on flat, well-lit slips; struggles with the rest. |
| `manual` | nothing | Stores the slip; you type the details in. |

Extraction failure is never fatal — the slip is stored with the reason recorded,
and you can fill in the details in the web UI or with
`spendtracker receipts --receipt 3 --set-total 289.92`.

The parser deliberately refuses decoy amounts. `CASH TENDERED`, `CHANGE DUE`,
`ROUNDING` and the pre-VAT subtotal are all excluded when locating the total,
because a wrong total does not merely look untidy — it corrupts reconciliation.
With no `TOTAL` line at all it falls back to the largest amount and drops its
confidence to 25% with an explicit warning.

Re-uploading the same photograph does nothing (the image bytes are hashed).
Photographing the same slip twice is detected and flagged.

---

## Duplicate handling

Re-exporting an overlapping date range is the normal way people use bank
portals, so it has to be safe.

Each row gets a fingerprint that is **stable** across re-exports but
**distinguishes** two genuinely separate purchases of the same thing on the same
day:

- where the export has a running balance, the balance is part of the fingerprint
  — two identical purchases leave different balances behind, so this is
  effectively exact;
- without a balance, the fingerprint includes an occurrence index counted per
  (date, amount, merchant). Re-importing the same range reproduces the same
  indices, so it deduplicates; two real coffees on the same day get index 0 and 1
  and both survive.

A unique database index enforces it, so a crash mid-import cannot leave a partial
double.

**False positives were the bigger risk**, so suspicion is scoped: a repeated
(date, amount, merchant) is only ever questioned inside a date range two
statements both cover. Outside prior coverage it is simply a repeat purchase and
is left alone. Rows from the same file are never compared — the bank listed them
separately. A differing running balance is treated as proof of distinctness.

Anything still unprovable goes to the review queue and is held out of totals by
default, erring toward not double counting. Nothing is deleted, nothing is
hidden, and every decision is reversible.

---

## Commands

```
spendtracker inspect <files...>            dry run: how a CSV would be read
spendtracker import-statement <files...>   import bank statement CSVs
spendtracker add-receipt <images...>       add till slip photographs
spendtracker report      [--period P]     breakdown + reconciliation
spendtracker report --json [FILE]         the same report as JSON
spendtracker advice      [--period P]     frivolous spend and what to cut
spendtracker merchants   [--period P]     spend by merchant
spendtracker recurring   [--all]          subscriptions and repeating charges
spendtracker review                       resolve suspected duplicates
spendtracker categorise  [--reclassify]   review and fix categories
spendtracker receipts    [--status S]     list and correct stored slips
spendtracker statements                   every import so far
spendtracker undo-import <id>             remove one import entirely
spendtracker status                       what has been imported so far
spendtracker serve                        web interface (needs [web])
```

Periods accept `2026-03`, `2026-Q1`, `2026`, `this-month`, `last-month`,
`last-90-days`, `ytd`, or `2026-01-01:2026-03-31`.

---

## Categories

A South African category taxonomy with a merchant rule pack covering common
retailers, fuel, telecoms, insurers, medical schemes, municipalities, streaming
services and betting operators.

Unmatched merchants land in **Uncategorised** — visible, counted, and listed for
you to assign. Assigning one creates a rule, so every future import picks it up.
Your own choices are never overwritten by the shipped rules.

Two category decisions worth knowing about:

- **Cash withdrawals count as spend.** Money that left your control is spend, so
  excluding it would understate everything. Slips then reclassify it.
- **Credit card repayments count as spend by default.** Until you also import the
  card's own statement, the repayment is the *only* visible trace of that month's
  card purchases. Set `credit_card_statements_imported: true` in
  `config.local.json` once you do import them, and it becomes a transfer instead
  so the same money is not counted twice.

---

## Configuration

Environment variables, or a gitignored `config.local.json` in the project root:

```json
{
  "currency_symbol": "R",
  "currency_code": "ZAR",
  "ocr_provider": "claude",
  "credit_card_statements_imported": false,
  "match_days_window": 4,
  "match_amount_tolerance_cents": 100
}
```

| Variable | Default |
|---|---|
| `SPENDTRACKER_DATA_DIR` | `./data` |
| `SPENDTRACKER_OCR_PROVIDER` | `claude` |
| `SPENDTRACKER_OCR_MODEL` | `claude-opus-5` |
| `ANTHROPIC_API_KEY` | unset |
| `SPENDTRACKER_CURRENCY_SYMBOL` | `R` |
| `SPENDTRACKER_HOST` / `_PORT` | `127.0.0.1` / `5000` |

---

## Privacy and safety

- Everything is a local SQLite file in `data/`, which is gitignored along with
  uploaded statements and slips.
- The web UI binds to `127.0.0.1` and has **no authentication**. It is a
  single-user local tool. If you expose it beyond localhost, put something that
  authenticates in front of it.
- With `ocr_provider: claude`, slip **images** are sent to Anthropic for reading.
  Statement CSVs are never sent anywhere. Use `tesseract` or `manual` if you
  would rather nothing left the machine.
- Back up `data/spending.db`. Rebuilding it means re-importing everything.

---

## Tests

```bash
.venv/bin/python -m pytest
```

163 tests covering amount and date parsing across formats, layout detection, the
sign-inference safety net, fingerprinting and duplicate scoring, receipt
matching and cash allocation, reconciliation invariants, the savings claim
ceiling, and every web route.

The invariants worth knowing are asserted directly:

- the category breakdown sums exactly to the bank's outflow total;
- storing a receipt never changes any total;
- cash allocation can never exceed the cash actually withdrawn;
- suggested savings can never exceed the reducible headroom;
- two identical same-day purchases both survive an overlapping re-import.

---

## Limitations

Worth knowing before you rely on it:

- **Bank profiles are heuristic.** Auto-detection is checked against the balance
  column where one exists, but always eyeball the first import.
- **Savings estimates are assumptions, not predictions.** Each states its own.
- **Reducible fractions are judgement calls** baked into `taxonomy.py`. Edit them
  if they do not match your life.
- **Multi-currency is not handled.** One currency per database.
- **Cash allocation is nearest-withdrawal-first**, not a solver. If you withdraw
  cash weekly and spend it slowly, allocations may attach to the wrong
  withdrawal. Totals are unaffected; only the timing of the breakdown is.
- **Slip reading is imperfect.** Check the total on anything that matters; the
  confidence score and line-item cross-check are there to point you at the
  doubtful ones.
- **Transfers between your own accounts** are detected from narration wording. If
  yours is unusual, add a rule.

## Project layout

```
spendtracker/
  money.py         integer-cent arithmetic and amount parsing
  db.py            SQLite schema (transactions = the only ledger)
  taxonomy.py      categories + South African merchant rule pack
  categorise.py    narration cleaning, merchant naming, rule engine
  dedupe.py        fingerprints, duplicate scoring, receipt matching, cash
  analytics.py     period breakdowns, reconciliation, recurring detection
  advice.py        frivolity scoring and reduction findings (claim ledger)
  periods.py       period shorthand parsing
  inspect.py       import preview (dry run) and import undo
  cli.py           command line interface
  ingest/
    csvimport.py   statement layout detection and parsing
    loader.py      parse -> classify -> dedupe -> insert
    receipts.py    till slip extraction and storage
  web/             Flask app, templates, CSS (optional extra)
samples/           sample statement generator
tests/             pytest suite
```
