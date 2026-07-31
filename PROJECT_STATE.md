# SpendTrack — Project State

Living status file, updated at every commit so this project can be picked up by a
fresh session or a different person with no loss of context.

**Last updated:** 2026-07-31
**Branch:** `claude/bank-spending-tracker-3fjs1x`
**Status:** Complete and usable. v1.0.0, 144 tests passing.

---

## 1. What was built

A local-first personal spending tracker for a South African bank account.

Inputs:
1. **CSV bank statements** — the authoritative record of every outflow.
2. **Photos of till slips** — detail and evidence, never an extra outflow.

Outputs:
- A reconciled accounting of all outflows for a period, with nothing unexplained.
- Summaries by category, group and merchant.
- Ranked, actionable reduction suggestions with rand values and confidence.
- Terminal report, self-contained HTML report, and JSON export.

Run it: `python3 -m spendtrack --help`. Full usage is in `README.md`.

## 2. Non-negotiable design rules (all enforced by tests)

**R1 — One source of truth for totals.** The outflow total for a period comes
from bank statement lines and only from bank statement lines. Slips enrich or
reallocate; they may never increase a total.

**R2 — Two distinct double-counting risks, handled separately.**
- *Statement re-import*: a deterministic fingerprint (account, date, amount,
  normalised description, occurrence number) with a UNIQUE index. Repeated rows
  are counted as skipped. Genuine same-day repeats both survive.
- *Slip vs statement*: slips are matched to transactions on amount, date and
  merchant similarity. A match adds detail and zero rand.

**R3 — Cash is a reallocation, not an addition.** A cash slip breaks down the ATM
withdrawal that funded it. The withdrawal total never changes; explained value
moves into real categories and the residual is reported as unexplained.

**R4 — Local only.** No network calls anywhere in the tool.

**R5 — Stdlib only.** Python 3.11 standard library. tesseract is optional and
degrades gracefully.

**R6 — Suggestions must be addable.** Each transaction feeds at most one
suggestion, and annual projections are scaled by observed frequency rather than
multiplying one month by twelve.

## 3. Milestones — all complete

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Repo scaffolding, state doc, package skeleton | done |
| 2 | SQLite schema, config, models | done |
| 3 | CSV statement importer (auto-detect + profiles) | done |
| 4 | Categorisation rules engine (SA rule set) | done |
| 5 | Slip ingestion (JSON / OCR / interactive) | done |
| 6 | Slip↔transaction matching + cash allocation | done |
| 7 | Period analysis, recurrence detection, insights | done |
| 8 | Terminal + HTML reports, full CLI | done |
| 9 | Tests, sample data, README, end-to-end verification | done |

## 4. Verifying it still works

```bash
git checkout claude/bank-spending-tracker-3fjs1x
python3 -m unittest discover -s tests -q          # 144 tests

export SPENDTRACK_HOME=/tmp/spendtrack-demo
rm -rf "$SPENDTRACK_HOME"
python3 -m spendtrack import samples/statement_signed.csv
python3 -m spendtrack import samples/statement_split_overlap.csv   # 7 dupes expected
python3 -m spendtrack import samples/statement_signed.csv          # 0 new expected
python3 -m spendtrack slip add samples/slips/slips_june.json
python3 -m spendtrack report 2026-06 --html /tmp/june.html
```

Expected on the sample data: June outflows R42,595.39, reconciliation balances,
R1,100 of the R2,000 cash withdrawal explained by slips with R900 unexplained,
one slip unmatched and excluded from all totals.

## 5. Where things are

```
spendtrack/
  cli.py            command line interface
  csvimport.py      statement CSV detection and parsing
  parsing.py        tolerant date and amount parsing
  normalise.py      description normalisation, fingerprints, fuzzy comparison
  ingest.py         statements into deduplicated transactions
  slips.py          slip ingestion: JSON, OCR, interactive
  matching.py       slip-to-statement matching and cash allocation
  categorise.py     rule engine and merchant labelling
  rules_default.py  built-in SA merchant rules
  taxonomy.py       categories, groups and discretion weights
  analysis.py       reconciliation, summaries, recurrence, suggestions
  report_text.py    terminal rendering
  report_html.py    self-contained HTML rendering
  db.py, config.py  storage and settings
tests/              144 unittest tests, no dependencies
samples/            sample statements and slips, with a walkthrough
```

## 6. Assumptions made

- South African context (ZAR, SA merchant names), inferred from the user's
  domain. Currency and the whole rule set are configurable.
- Statements may come from more than one account; each transaction records its
  account, so periods can be reported per-account or combined.
- Discretion weights (how reducible each category is) are documented opinions,
  visible in `taxonomy.py` and editable in `~/.spendtrack/rules.json`.

## 7. Known limitations

Documented in full in README.md under "Known limitations". The short list:
per-file occurrence numbering for same-day identical repeats, OCR quality on
thermal receipts, rough annual projections from a single month, no multi-currency
handling, and no learned categorisation beyond remembered exact descriptions.

## 8. If picking this up to extend it

Sensible next steps, in rough order of value:
1. A budget/target per category, with month-to-date tracking against it.
2. Recurring-charge alerts: a fixed charge that increased, or stopped appearing.
3. Multi-account transfer detection by matching equal-and-opposite pairs across
   accounts, rather than relying on description rules.
4. A `slip verify` command to walk OCR'd slips whose items do not sum to total.
5. Import of PDF statements via the pdf skill, normalised into the same rows.
