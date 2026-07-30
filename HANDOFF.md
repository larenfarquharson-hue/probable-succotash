# HANDOFF — spendtracker

Written so a fresh session, or another person, can pick this up with no prior
context. Last updated at the end of the build described below.

## Status: complete and ready to use

All nine planned pieces are built, tested and committed on
`claude/bank-spending-tracker-d5w9dd`. 163 tests pass. There is no half-finished
work in the tree and nothing is stubbed.

```bash
./run.sh demo      # sets up, loads fictional samples, opens the web UI
./run.sh test      # 163 tests
```

## What was asked for, and where each part lives

| Requirement | Where | State |
|---|---|---|
| Track all spending on a bank account | `spendtracker/db.py`, `ingest/loader.py` | done |
| Fed by CSV bank statements | `ingest/csvimport.py` (layout auto-detection) | done |
| Fed by pictures of till slips | `ingest/receipts.py` (Claude vision / tesseract / manual) | done |
| Must avoid double counting | `dedupe.py` — the correctness core | done |
| Account for all outflows per period | `analytics.py` — `reconcile()` proves it sums | done |
| Summarise by type of spend | `analytics.py` `by_category` + `taxonomy.py` | done |
| Summarise by merchant | `analytics.py` `by_merchant` + `categorise.py` | done |
| Suggest frivolous / reducible spend | `advice.py` | done |
| Ready for use | CLI + web UI + README + `run.sh` | done |

## The design decisions that matter

Read these before changing anything, because several are load-bearing and look
like arbitrary choices from the outside.

1. **`transactions` is the only ledger, and only bank statements write to it.**
   Receipts are evidence about those rows. This single rule is what makes the
   totals trustworthy. If you ever find yourself adding an outflow from a
   receipt, stop — that is the bug this whole design exists to prevent.

2. **A till slip resolves to one of three states**, never a fourth:
   `matched` (linked to a bank row), `cash_allocated` (drawn from a withdrawal
   already counted), or `unmatched` (excluded from totals and surfaced). Look at
   `dedupe.match_receipt`.

3. **Cash withdrawals count as spend; slips reclassify them.** Withdrawing R2 000
   and spending R450 of it is one outflow. `analytics._cash_slip_reclass` moves
   the allocated amount out of "Cash Withdrawals" into the real category, leaving
   the unexplained remainder visible. The total never moves.

4. **Duplicate suspicion is scoped to overlapping statement coverage.** False
   positives were the bigger risk than false negatives — flagging a genuine
   repeat purchase is worse than asking about an overlap. See
   `dedupe.find_duplicate_candidates`, which returns early when the date is not
   inside a range a *previous* statement covered.

5. **Unprovable duplicates are held out of totals, not counted.** Erring toward
   understating avoids the failure mode the user explicitly cared about. The
   amounts are shown so the understatement is never hidden.

6. **Savings use a claim ledger in per-month units** (`advice._ClaimLedger`).
   Each category is capped at its reducible fraction and findings draw against it
   in order of defensibility. Without this, overlapping findings bank the same
   rand. `AdviceReport.validate()` asserts the total cannot exceed monthly spend.
   **The units are per-month throughout** — mixing in a period total was a real
   bug that produced a "saving" larger than the category's entire spend.

7. **Charts encode magnitude, so they use one hue.** Category and merchant bars
   vary in number, not in kind, so the label carries identity and every bar
   shares the accent colour. The only two-hue encoding is essential versus
   discretionary, and both palettes were validated for contrast and
   colour-vision deficiency in light and dark.

## Bugs found and fixed during the build

Kept here because each represents a trap that could be reintroduced:

- **Advice claim ledger unit mismatch.** Ceilings were period totals while
  findings claimed monthly amounts, producing a R7 988/month "saving" on a
  category that only spent R4 297/month. Now monthly throughout.
- **Cash slip attached to a card row.** `match_receipt` tried card matching
  before consulting tender type, so a cash slip could link to an unrelated
  same-amount card purchase on the same day. Tender type now decides order.
- **Slip matched a completely different merchant.** An exact amount on the exact
  day scores 0.85 alone, above the 0.62 threshold, so merchant was effectively
  advisory. Now a hard gate when both sides name a merchant.
- **`parse_amount("0.005")` returned 500.** A "3 decimals means European
  thousands" heuristic misfired. A single dot is now always a decimal point; only
  multiple dot groups are thousands separators.
- **Recurring charges annualised off the raw median gap**, overstating a monthly
  bond by ~R5 000/year. Now uses canonical cycles.
- **Variable spend offered as cancellable subscriptions** (weekly groceries).
  `Recurring.is_subscription` now requires a stable amount.
- **Inflows categorised as spending categories.** `Classifier.classify` now takes
  the amount and routes money in to `Income`.
- **`.months .plot` never matched** (the element carries both classes, needing the
  compound `.months.plot`), so the trend chart rendered as flat lines; plus
  `align-items: flex-end` collapsed each column so percentage heights resolved
  against zero.
- **Hover tooltips gave the page a horizontal scrollbar at 390px** even at
  opacity 0, because absolutely positioned elements still extend scroll width.
- **Dashboard defaulted to "last 3 months"** and looked empty for anyone
  importing older statements.
- **Transactions page rendered a 36-option select on all 500 rows** (278KB).

## Verified behaviour

On the generated samples (three deliberately different layouts, two overlapping
by a fortnight):

- all three layouts auto-detected, 100% sign agreement against balances;
- 39 overlapping rows deduplicated, zero false positives, re-import a no-op;
- reconciliation residual exactly 0; statement balances agree;
- a coverage gap (28 Feb, which the sample statement does not reach) correctly
  reported rather than silently shown as zero spend;
- a card slip links to its bank row; a cash slip moves R450 from Cash Withdrawals
  to Alcohol & Tobacco with the period total byte-identical before and after;
- suggested savings of 18.4% of spend, inside the reducible ceiling;
- every web route returns 200/404 as expected; no horizontal scroll at 390px or
  1280px in either colour scheme.

## Things deliberately not done

Not oversights — judgement calls, listed so nobody re-litigates them silently:

- **No multi-currency.** One currency per database.
- **No bank API / screen-scraping integration.** CSV in, by design.
- **No authentication on the web UI.** It binds to localhost and is a
  single-user local tool. Adding half-authentication would be worse than none.
- **Cash allocation is nearest-withdrawal-first, not a solver.** Totals are
  unaffected; only the timing of the breakdown could be off.
- **Bank profiles in `bank_profiles.json` are illustrative shapes**, not verified
  copies of any bank's export. Deliberately labelled as such rather than
  presented as authoritative, since I could not verify them.

## If you are extending it

Good next steps, roughly by value:

1. **Budgets.** The `budgets` table exists in the schema and is unused. Wire it
   into `analytics.period_summary` and show progress bars on the dashboard.
2. **Credit card statement import.** The `credit_card_statements_imported` flag
   and the switching logic already exist and are tested; what is missing is a
   second account type in the UI and guidance on setting the flag.
3. **CSV / Excel export of a period report.** All the data is in
   `PeriodSummary`; this is presentation only.
4. **Bulk re-categorisation from the web UI.** The CLI has
   `categorise --reclassify`; the web UI only sets one transaction at a time.
5. **Real bank profiles.** Verify against actual exports and replace the
   illustrative entries.

Before changing `dedupe.py`, `analytics.reconcile` or `advice._ClaimLedger`, run
the suite first — those three modules carry the invariants everything else
depends on, and the tests state each invariant explicitly.

## Token budget note

The task asked for token use to be tracked, with a pause and handoff before any
window limit was hit. The build completed inside a single window, so no pause was
needed and no work was interrupted. This document exists anyway, because it is
the artifact that would have made a pause safe — and it is worth having
regardless.

Actual usage was measured from the session transcript rather than estimated:

```bash
python3 - <<'EOF'
import json, glob, os
for f in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')):
    i=o=cc=cr=0
    for line in open(f):
        try: u=(json.loads(line).get('message') or {}).get('usage')
        except Exception: continue
        if u:
            i+=u.get('input_tokens',0); o+=u.get('output_tokens',0)
            cc+=u.get('cache_creation_input_tokens',0)
            cr+=u.get('cache_read_input_tokens',0)
    print(os.path.basename(f), f"in={i} out={o} cache_write={cc} cache_read={cr}")
EOF
```
