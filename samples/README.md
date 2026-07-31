# Sample data

Fabricated data for trying SpendTrack out and for demonstrating the parts that
are easy to get wrong. No real account details.

| File | What it exercises |
|---|---|
| `statement_signed.csv` | Bank preamble above the header, signed amounts, a running balance, a trailing "Closing Balance" footer, `dd/mm/yyyy` dates. June 2026. |
| `statement_split_overlap.csv` | Semicolon delimited, separate Debit/Credit columns, a per-line Fee column, `dd-mm-yyyy` dates. **Deliberately overlaps 25–30 June** with the file above, so importing both shows overlap handling. |
| `slips/slips_june.json` | Six slips: three card slips that match statement lines, two cash slips that get allocated against the 4 June ATM withdrawal, and one paid on another bank's card that matches nothing. |
| `slips/checkers_ocr.txt` | Raw OCR-style slip text, for testing the text parser without tesseract. |

## Try it

```bash
export SPENDTRACK_HOME=/tmp/spendtrack-demo
python3 -m spendtrack import samples/statement_signed.csv
python3 -m spendtrack import samples/statement_split_overlap.csv
python3 -m spendtrack slip add samples/slips/slips_june.json
python3 -m spendtrack report 2026-06 --html /tmp/june.html
```

Things to look for:

- The second import reports **7 rows already present** — the overlapping days are
  recognised, not added again.
- Importing either file twice adds nothing at all.
- Two identical R500 Betway transactions on 9 June both survive. They are real
  repeats, not duplicates.
- The R1,842.66 Checkers slip matches its statement line and the June total does
  not change.
- The two cash slips (R480 + R620) move value out of "Cash Withdrawals" into
  "Eating Out" and "Groceries". The R2,000 withdrawal stays R2,000, and R900 is
  reported as still unexplained.
- The Rhapsody's slip is listed as unmatched and is in no total, because it was
  paid on a card this account does not have.
- The report ends with a reconciliation line confirming the buckets add back to
  the R42,595.39 that left the account.

To see the OCR parser without installing tesseract:

```bash
python3 -c "
from pathlib import Path
from spendtrack import slips
slip = slips.parse_slip_text(Path('samples/slips/checkers_ocr.txt').read_text())
print(slip.merchant, slip.slip_date, slip.total, slip.payment_method, slip.card_last4)
for item in slip.items: print(' ', item.description, item.line_total)
print('problems:', slip.problems())
"
```
