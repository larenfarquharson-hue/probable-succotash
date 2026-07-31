# HANDOFF — spendtracker

Written so a fresh session, or another person, can pick this up with no prior
context.

## Status: complete and ready to use

`main` holds the merged version: the `claude/bank-spending-tracker-d5w9dd`
build, plus three things ported from the parallel `claude/bank-spending-tracker-3fjs1x`
prototype. 190 tests pass. There is no half-finished work in the tree and
nothing is stubbed.

```bash
python3 -m spendtracker.cli --help    # no dependencies needed
./run.sh demo                          # sets up, loads fictional samples, opens the web UI
./run.sh test                          # 190 tests
```

## The merge: what came from the other branch, and what did not

Two independent builds of the same brief existed on separate branches. `d5w9dd`
was the better base — it stores money as integer cents (`3fjs1x` used SQLite
`REAL`, i.e. floats, in an app whose entire pitch is that the totals reconcile),
and it has a web UI, a savings advisor and recurring-charge detection that the
other lacks.

Ported **in** from `3fjs1x`:

| What | Where | Why it was worth taking |
|---|---|---|
| `inspect` — dry-run CSV preview | `spendtracker/inspect.py` | Import is where a silent mistake becomes expensive. A wrong sign convention inverts every number in every report and is invisible from the report itself. |
| `undo-import` + `statements` | `spendtracker/inspect.py` | Without it the only remedy for a bad import was deleting the database. Maps cleanly onto the existing `statements` table and `transactions.statement_id`. |
| Zero-dependency install | `pyproject.toml`, `cli.py` | `3fjs1x` had no runtime dependencies at all. Bank data is the last thing you want to hand to a machine you do not control, and this now runs on a locked-down laptop with no package index. |
| `report --json` | `analytics.summary_to_dict` | Cheap, and makes the reconciliation numbers scriptable. |

Deliberately **not** ported, so nobody re-litigates it:

- **`audit-duplicates`.** It groups transactions by (date, amount, description)
  and flags repeats. `dedupe.py` deliberately does *not* do this outside
  overlapping statement coverage, because a repeated (date, amount, merchant)
  outside prior coverage is simply a repeat purchase — flagging those was the
  main false-positive risk identified during the original build. Porting the
  command would have reintroduced exactly the noise this codebase was designed
  to avoid.
- **Bank profiles.** Already present here as `bank_profiles.json`, with 0-based
  column indexes and sign verification against the running balance. The other
  branch's version was not better.
- **The float money representation**, obviously. It is the reason this branch
  was the base rather than the other way round.

### One wart left in place

`cli.py` expands shell globs in three commands. `expand_paths()` is the shared
helper, but only the new `inspect` command uses it — the two older call sites
still have their own inline copies. Consolidating them is safe but touches
working import paths for purely cosmetic gain, so it was left for someone with
the suite in front of them.

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

Added by the merge, and covered by tests:

- `inspect` predicts the same row count and the same outflow and inflow totals
  that `import-statement` then actually produces — a preview that disagreed
  with the importer would be worse than no preview;
- `inspect` writes nothing: no database file, no rows, no statement record;
- `undo-import` removes exactly one import's transactions and leaves every
  other import untouched, and clears fingerprints so the same file re-imports
  cleanly afterwards;
- `undo-import` refuses when till slips are linked, and under `--force` returns
  those slips to the review queue with `counts_as_outflow = 0` rather than
  deleting them;
- every core command runs with `flask`, `dateutil`, `anthropic` and `PIL` all
  made unimportable; `serve` alone fails, with an instruction and exit code 3
  rather than a traceback (`tests/test_no_dependencies.py`).

## Things deliberately not done

Not oversights — judgement calls, listed so nobody re-litigates them silently:

- **No multi-currency.** One currency per database.
- **No bank API / screen-scraping integration.** CSV in, by design.
- **No authentication on the web UI *when bound to localhost*.** A password
  guarding a port only this machine can open is theatre. Authentication now
  exists, but it activates on exposure rather than by default — see below.
- **Cash allocation is nearest-withdrawal-first, not a solver.** Totals are
  unaffected; only the timing of the breakdown could be off.
- **Bank profiles in `bank_profiles.json` are illustrative shapes**, not verified
  copies of any bank's export. Deliberately labelled as such rather than
  presented as authoritative, since I could not verify them.

## Web authentication, and why it works the way it does

Added after the merge, when the app needed to be reachable from a phone.

The design decision worth preserving: **authentication is tied to the bind
address, not to a config flag.** Serving on loopback needs no passphrase and
behaves exactly as before. Serving on anything else requires one, and
`cmd_serve` calls `auth.check_exposure` and exits 4 before Flask ever binds.

The alternative — a `require_auth: true` setting plus a warning in the README —
was rejected because the failure mode is silent. Someone adds `--host 0.0.0.0`
to reach it from a phone, it works, and their bank statements are on the network
with nothing in the way. Tying the check to the bind address makes the unsafe
configuration unreachable rather than merely discouraged.

Supporting choices:

- **scrypt from `hashlib`.** bcrypt and argon2 are packages, and the CLI having
  no dependencies is enforced by `tests/test_no_dependencies.py`. scrypt is
  memory-hard, unlike a bare SHA-256.
- **The signing key is generated on first run, not defaulted.** `Config.secret_key`
  still carries `"dev-only-change-me"`; the app now prefers the generated key from
  `auth.json`. A default key published in a public repo means anyone can forge a
  session cookie without knowing the passphrase, so `check_exposure` also refuses
  to serve off-loopback if the key is still the default.
- **Throttling is in-memory, per process.** One process serving one person; a
  shared store would be ceremony. Restarting clears the counters, which is fine —
  an attacker cannot restart your server.
- **The context processor short-circuits when unauthenticated**, so a signed-out
  request does not run dashboard queries on the database.
- **`next=` is path-only.** Accepting a full URL would turn the login page into
  an open redirect. `safe_next` rejects anything not starting with a single `/`.

### What was NOT done, deliberately

- **No TLS.** Certificates on a LAN mean either a self-signed cert (browser
  warnings on every device, training the user to click through) or a real
  hostname and ACME. Both are big enough to be their own change. The consequence
  is documented in three places rather than glossed: the traffic is readable by
  anyone on the network.
- **No CSRF tokens.** `SameSite=Lax` blocks the cross-site POST that CSRF needs,
  and adding tokens would touch every form template. If a route ever needs to
  accept a cross-site POST, this decision has to be revisited.
- **No user accounts, no password reset.** One user. Recovery is
  `spendtracker passphrase --force` at the machine, because access to the
  machine is already access to the data.

If you expose this beyond a trusted LAN, TLS stops being optional.

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

One further invariant is easy to break by accident: **no module reachable from
`cli.py` may import a third-party package at module scope.** The zero-dependency
promise decays silently — one convenience import and the app stops installing on
a locked-down machine, with nothing failing until someone tries it. Optional
packages go inside the function that needs them, behind a `try/except
ImportError` with a message telling the user which extra to install.
`tests/test_no_dependencies.py` enforces this; if it fails, that is what broke.

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
