# SpendTrack — Project State

Living status file. Updated at every commit so this project can be resumed by a
fresh session (or a different person) with zero loss of context.

**Last updated:** 2026-07-31
**Branch:** `claude/bank-spending-tracker-3fjs1x`
**Status:** Milestone 1 — scaffolding

---

## 1. What is being built

A local-first personal spending tracker for a South African bank account.

Inputs:
1. **CSV bank statements** — the authoritative record of every outflow.
2. **Photos of till slips** — *detail and evidence*, never an extra outflow.

Outputs:
- Full accounting of all outflows for a period (nothing unexplained).
- Summaries by category and by merchant.
- Ranked, actionable "frivolous / reducible spend" recommendations with rand values.
- Terminal report + a self-contained HTML report.

## 2. Non-negotiable design rules

**R1 — One source of truth for totals.** The sum of outflows for a period comes
from bank statement lines and *only* from bank statement lines. Till slips can
enrich or reallocate a transaction, but may never increase the period total.

**R2 — Two distinct double-counting risks, handled separately.**
- *Statement re-import*: the same transaction arriving twice (re-downloaded CSV,
  overlapping date ranges). Handled by a deterministic per-transaction
  fingerprint with a UNIQUE index; re-imports are counted as skipped.
- *Slip vs statement*: a card slip is the same money as its statement line.
  Handled by matching slips → transactions. Matched slips add line-item detail
  and improve the category; they add zero rand.

**R3 — Cash is a reallocation, not an addition.** An ATM withdrawal is already an
outflow on the statement. A cash till slip therefore *breaks down* that
withdrawal into real categories. It never adds to the total. Cash not explained
by slips is reported as "Cash — unaccounted" so the period still balances.

**R4 — Local only.** Bank data stays on disk. No network calls in the core tool.
(Personal financial data; POPIA-sensitive.)

**R5 — Stdlib only for the core.** Python 3.11 standard library. OCR and any
other extras are optional and degrade gracefully.

## 3. Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Repo scaffolding, state doc, package skeleton | in progress |
| 2 | SQLite schema + config + models | not started |
| 3 | Robust CSV statement importer (auto-detect + bank profiles) | not started |
| 4 | Categorisation rules engine (SA default rule set) | not started |
| 5 | Slip ingestion (JSON / OCR / interactive) | not started |
| 6 | Slip↔transaction matching + cash allocation | not started |
| 7 | Period analysis, recurring/subscription detection, insights | not started |
| 8 | Terminal report + self-contained HTML report | not started |
| 9 | Tests, sample data, README, end-to-end verification | not started |

## 4. How to resume

```bash
git checkout claude/bank-spending-tracker-3fjs1x
cat PROJECT_STATE.md          # this file — read section 3 for the next milestone
python3 -m unittest discover -s tests -q
python3 -m spendtrack --help
```

## 5. Open decisions / assumptions

- Assumed South African context (ZAR, SA merchant names) from the user's domain.
  Currency symbol and the merchant rule set are configurable.
- Assumed statements may come from more than one account; every transaction
  records its account so periods can be reported per-account or combined.
