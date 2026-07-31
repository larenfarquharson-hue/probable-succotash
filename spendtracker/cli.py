"""Command line interface.

    spendtracker import-statement statements/*.csv
    spendtracker add-receipt slips/*.jpg
    spendtracker report --period 2026-03
    spendtracker advice --period last-3-months
    spendtracker review            # resolve suspected duplicates
    spendtracker merchants
    spendtracker serve             # start the web UI
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import advice as advice_mod
from . import analytics, db as dbmod, taxonomy
from .config import Config, load_config
from .dedupe import rematch_all_receipts, resolve_candidate
from .ingest import loader
from .ingest.csvimport import CsvFormatError, load_profiles
from .ingest.receipts import ReceiptExtractionError, store_receipt, update_receipt
from . import inspect as inspect_mod
from .money import fmt, to_cents
from .periods import Period, months_between, parse_period


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

BAR_WIDTH = 28


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "·" * (width - filled)


def expand_paths(patterns: list[str]) -> list[Path]:
    """Expand shell-style globs and drop anything that does not exist.

    Windows shells do not expand globs for us, so the CLI does it itself.
    """
    out: list[Path] = []
    for pattern in patterns:
        matches = (
            sorted(Path().glob(pattern))
            if any(c in pattern for c in "*?[")
            else [Path(pattern)]
        )
        for match in matches:
            if match.exists() and match not in out:
                out.append(match)
    return out


def heading(text: str) -> str:
    return f"\n{text}\n{'─' * len(text)}"


def open_db(cfg: Config) -> sqlite3.Connection:
    cfg.ensure_dirs()
    conn = dbmod.connect(cfg.db_path)
    dbmod.init_db(conn)
    taxonomy.seed(conn)
    return conn


def resolve_period(conn: sqlite3.Connection, text: str | None) -> Period:
    """Parse a period, defaulting to everything the data covers."""
    period = parse_period(text)
    if period is not None:
        return period
    row = conn.execute(
        "SELECT MIN(txn_date) lo, MAX(txn_date) hi FROM transactions WHERE status='active'"
    ).fetchone()
    if row is None or row["lo"] is None:
        raise SystemExit(
            "No transactions yet. Import a statement first:\n"
            "  spendtracker import-statement path/to/statement.csv"
        )
    from datetime import date

    lo, hi = date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"])
    return Period(lo, hi, f"{lo.isoformat()} to {hi.isoformat()} (all data)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_import_statement(args, cfg: Config) -> int:
    conn = open_db(cfg)
    exit_code = 0
    for pattern in args.paths:
        matches = sorted(Path().glob(pattern)) if any(c in pattern for c in "*?[") else [Path(pattern)]
        if not matches:
            print(f"no files matched {pattern!r}", file=sys.stderr)
            exit_code = 1
            continue
        for path in matches:
            if not path.exists():
                print(f"{path}: not found", file=sys.stderr)
                exit_code = 1
                continue
            try:
                report = loader.import_statement(
                    conn,
                    path,
                    cfg=cfg,
                    account_name=args.account,
                    force=args.force,
                    profile_name=args.profile,
                )
            except (CsvFormatError, KeyError) as exc:
                print(f"{path.name}: {exc}", file=sys.stderr)
                exit_code = 1
                continue

            print(report.summary())
            if report.period_start and report.period_end:
                print(f"  period: {report.period_start} to {report.period_end}")
                print(
                    f"  outflows {fmt(report.outflow_cents, cfg.currency_symbol)}, "
                    f"inflows {fmt(report.inflow_cents, cfg.currency_symbol)}"
                )
            for note in report.detection_notes:
                print(f"  detected: {note}")
            for warning in report.warnings:
                print(f"  WARNING: {warning}")
            if args.verbose:
                for line_no, reason, raw in report.skipped_detail[:20]:
                    print(f"  skipped line {line_no} ({reason}): {raw[:110]}")
            if report.receipt_rematch:
                bits = ", ".join(f"{v} {k}" for k, v in sorted(report.receipt_rematch.items()))
                print(f"  receipts re-checked: {bits}")

    pending = conn.execute(
        "SELECT COUNT(*) c FROM duplicate_candidates WHERE resolution='pending'"
    ).fetchone()["c"]
    if pending:
        print(
            f"\n{pending} suspected duplicate(s) need review. Run: spendtracker review"
        )
    conn.close()
    return exit_code


def cmd_add_receipt(args, cfg: Config) -> int:
    conn = open_db(cfg)
    account_id = dbmod.get_or_create_account(conn, args.account, currency=cfg.currency_code)
    exit_code = 0
    for pattern in args.paths:
        matches = sorted(Path().glob(pattern)) if any(c in pattern for c in "*?[") else [Path(pattern)]
        if not matches:
            print(f"no files matched {pattern!r}", file=sys.stderr)
            exit_code = 1
            continue
        for path in matches:
            try:
                result = store_receipt(
                    conn, path, cfg=cfg, account_id=account_id, provider=args.provider
                )
            except (ReceiptExtractionError, FileNotFoundError, OSError) as exc:
                print(f"{path.name}: {exc}", file=sys.stderr)
                exit_code = 1
                continue

            if result.is_duplicate:
                print(f"{path.name}: already uploaded (receipt #{result.duplicate_of})")
                continue

            data = result.data
            row = conn.execute(
                "SELECT link_status, transaction_id, match_reason FROM receipts WHERE id=?",
                (result.receipt_id,),
            ).fetchone()
            print(
                f"{path.name}: receipt #{result.receipt_id} — "
                f"{data.merchant_norm or 'unknown merchant'}, "
                f"{data.receipt_date or 'unknown date'}, "
                f"{fmt(data.total_cents, cfg.currency_symbol) if data.total_cents else 'no total'} "
                f"[{data.tender_type}] via {data.extractor}"
            )
            print(f"  status: {row['link_status']} — {row['match_reason']}")
            for warning in result.warnings:
                print(f"  NOTE: {warning}")
    conn.close()
    return exit_code


def cmd_report(args, cfg: Config) -> int:
    conn = open_db(cfg)
    period = resolve_period(conn, args.period)
    summary = analytics.period_summary(conn, period, cfg=cfg)
    sym = cfg.currency_symbol
    rec = summary.reconciliation

    if args.json:
        payload = analytics.summary_to_dict(summary)
        text = json.dumps(payload, indent=2)
        if args.json is not True and str(args.json) != "-":
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"wrote {args.json}")
        else:
            print(text)
        conn.close()
        return 0

    print(heading(f"Spending report — {period.label}"))
    print(f"  Total out of the account   {fmt(summary.total_outflow_cents, sym):>14}")
    print(f"  Of that, actual spending   {fmt(summary.spend_cents, sym):>14}")
    if summary.excluded_cents:
        print(f"  Excluded (own-money moves) {fmt(summary.excluded_cents, sym):>14}")
    print(f"  Money in                   {fmt(summary.total_inflow_cents, sym):>14}")
    print(f"  Net                        {fmt(summary.net_cents, sym):>14}")
    print(f"  Transactions               {summary.transaction_count:>14}")
    if period.days > 45:
        print(f"  Spending per month         {fmt(summary.spend_per_month_cents, sym):>14}")
    print(
        f"  Essential / discretionary  "
        f"{fmt(summary.essential_cents, sym)} / {fmt(summary.discretionary_cents, sym)} "
        f"({summary.discretionary_share:.0%} discretionary)"
    )

    print(heading("By type of spend"))
    for b in summary.by_category:
        if b.total_cents <= 0:
            continue
        flag = "" if b.kind not in taxonomy.EXCLUDED_KINDS else "  (not spending)"
        extra = (
            f"  +{fmt(b.from_cash_slips_cents, sym)} from cash slips"
            if b.from_cash_slips_cents
            else ""
        )
        print(
            f"  {b.name:<28} {fmt(b.total_cents, sym):>12} {b.share:>6.1%} "
            f"{bar(b.share)} n={b.count}{flag}{extra}"
        )

    print(heading(f"By merchant (top {min(args.top, len(summary.by_merchant))})"))
    for b in summary.by_merchant[: args.top]:
        print(
            f"  {b.name:<28} {fmt(b.total_cents, sym):>12} {b.share:>6.1%} "
            f"{bar(b.share)} n={b.count}  avg {fmt(b.average_cents, sym)}"
        )

    print(heading("How the money left"))
    for b in summary.by_type:
        print(f"  {b.name:<28} {fmt(b.total_cents, sym):>12} n={b.count}")

    print(heading("Is anything missing?"))
    print(f"  Breakdown reconciles to the bank total: {'yes' if rec.residual_cents == 0 else 'NO'}")
    if rec.balances_agree is not None:
        print(f"  Statement balances line up:             {'yes' if rec.balances_agree else 'no'}")
    print(
        f"  Days covered by a statement:            {rec.days_covered}/{rec.days_in_period}"
    )
    print(f"  Cash withdrawn / explained by slips:    "
          f"{fmt(rec.cash_withdrawn_cents, sym)} / {fmt(rec.cash_explained_cents, sym)}")
    print(
        f"  Receipts matched / cash / unmatched:     "
        f"{rec.receipts_matched} / {rec.receipts_cash_allocated} / {rec.receipts_unmatched}"
    )
    print(f"  Share of outflows explained:            {rec.explained_share:.1%}")
    if rec.warnings:
        print()
        for warning in rec.warnings:
            print(f"  ! {warning}")

    if args.trend:
        points = analytics.monthly_trend(conn, cfg=cfg, months=args.trend)
        if len(points) > 1:
            print(heading("Month on month"))
            peak = max(p.spend_cents for p in points) or 1
            for point in points:
                print(
                    f"  {point.period.label:<16} {fmt(point.spend_cents, sym):>12} "
                    f"{bar(point.spend_cents / peak)}  "
                    f"disc {fmt(point.discretionary_cents, sym)}"
                )
    conn.close()
    return 0


def cmd_advice(args, cfg: Config) -> int:
    conn = open_db(cfg)
    period = resolve_period(conn, args.period)
    summary = analytics.period_summary(conn, period, cfg=cfg)
    report = advice_mod.build_advice(conn, period, cfg=cfg, summary=summary)
    sym = cfg.currency_symbol

    print(heading(f"Where you could cut back — {period.label}"))
    monthly_spend = int(summary.spend_cents / period.months)
    print(f"  You are spending about {fmt(monthly_spend, sym)} a month.")
    print(
        f"  Suggestions below add up to {fmt(report.monthly_total_cents, sym)} a month "
        f"({fmt(report.annual_total_cents, sym)} a year)"
        + (f", {report.monthly_total_cents / monthly_spend:.0%} of your spending." if monthly_spend else ".")
    )

    if not report.findings:
        print("\n  Nothing stands out as clearly reducible in this period.")
    for order, finding in enumerate(report.findings, start=1):
        print(
            f"\n  {order}. {finding.title}"
            f"\n     Save about {fmt(finding.monthly_saving_cents, sym)}/month "
            f"({fmt(finding.annual_saving_cents, sym)}/year) — "
            f"{finding.difficulty}, {finding.confidence} confidence"
        )
        for line in _wrap(finding.detail, 88, "     "):
            print(line)
        for line in _wrap(f"Assumption: {finding.assumption}", 88, "     "):
            print(line)
        if args.verbose:
            for item in finding.evidence[:6]:
                print(f"       · {item}")

    print(heading("Spend that looks frivolous"))
    print(
        f"  {fmt(report.frivolous_total_cents, sym)} across {len(report.frivolous)} "
        f"transaction(s) scored as discretionary."
    )
    for item in report.frivolous[: args.top]:
        print(
            f"  {item.txn_date}  {item.merchant:<26} {fmt(item.amount_cents, sym):>11}  "
            f"{item.category} ({item.score:.2f})"
        )

    if report.notes:
        print(heading("Caveats"))
        for note in report.notes:
            for line in _wrap(note, 88, "  "):
                print(line)
    conn.close()
    return 0


def _wrap(text: str, width: int, indent: str) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def cmd_review(args, cfg: Config) -> int:
    conn = open_db(cfg)
    sym = cfg.currency_symbol
    rows = conn.execute(
        """SELECT dc.id, dc.score, dc.reason,
                  n.txn_date n_date, n.description_raw n_desc, n.amount_cents n_amt,
                  n.status n_status, n.balance_cents n_bal,
                  e.txn_date e_date, e.description_raw e_desc, e.balance_cents e_bal,
                  ns.filename n_file, es.filename e_file
           FROM duplicate_candidates dc
           JOIN transactions n ON n.id = dc.txn_id
           JOIN transactions e ON e.id = dc.existing_id
           LEFT JOIN statements ns ON ns.id = n.statement_id
           LEFT JOIN statements es ON es.id = e.statement_id
           WHERE dc.resolution = 'pending'
           ORDER BY dc.score DESC, n.txn_date"""
    ).fetchall()

    if not rows:
        print("No suspected duplicates to review.")
        conn.close()
        return 0

    print(f"{len(rows)} suspected duplicate(s).\n")
    for row in rows:
        held = row["n_status"] == "duplicate"
        print(f"#{row['id']}  score {row['score']:.2f} — {row['reason']}")
        print(
            f"   new      {row['n_date']}  {fmt(-row['n_amt'], sym):>11}  "
            f"{(row['n_desc'] or '')[:56]}   [{row['n_file'] or '?'}]"
        )
        print(
            f"   existing {row['e_date']}  {fmt(-row['n_amt'], sym):>11}  "
            f"{(row['e_desc'] or '')[:56]}   [{row['e_file'] or '?'}]"
        )
        if row["n_bal"] is not None and row["e_bal"] is not None:
            print(
                f"   balances {fmt(row['n_bal'], sym)} vs {fmt(row['e_bal'], sym)}"
            )
        print(
            "   currently HELD OUT of totals" if held else "   currently COUNTED in totals"
        )

        if args.yes_duplicate:
            resolve_candidate(conn, int(row["id"]), "duplicate")
            print("   -> marked as duplicate\n")
            continue
        if args.yes_distinct:
            resolve_candidate(conn, int(row["id"]), "distinct")
            print("   -> marked as distinct\n")
            continue

        answer = input("   [d]uplicate / [k]eep both / [s]kip / [q]uit? ").strip().lower()
        if answer.startswith("q"):
            break
        if answer.startswith("d"):
            resolve_candidate(conn, int(row["id"]), "duplicate")
            print("   -> marked as duplicate")
        elif answer.startswith("k"):
            resolve_candidate(conn, int(row["id"]), "distinct")
            print("   -> both kept")
        print()
    conn.close()
    return 0


def cmd_merchants(args, cfg: Config) -> int:
    conn = open_db(cfg)
    period = resolve_period(conn, args.period)
    start, end = period.as_iso()
    sym = cfg.currency_symbol
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(merchant_norm,''),'Unknown') m,
                  COALESCE(category,'Uncategorised') c,
                  COUNT(*) n, SUM(-amount_cents) total
           FROM transactions
           WHERE status='active' AND amount_cents<0 AND txn_date BETWEEN ? AND ?
           GROUP BY 1 ORDER BY total DESC""",
        (start, end),
    ).fetchall()
    print(heading(f"Merchants — {period.label}"))
    for row in rows[: args.top]:
        print(
            f"  {row['m']:<32} {fmt(row['total'], sym):>12}  n={row['n']:<4} {row['c']}"
        )
    conn.close()
    return 0


def cmd_recurring(args, cfg: Config) -> int:
    conn = open_db(cfg)
    sym = cfg.currency_symbol
    items = analytics.find_recurring(
        conn, subscriptions_only=not args.all
    )
    label = "Recurring commitments" if args.all else "Subscriptions and fixed commitments"
    print(heading(label))
    if not items:
        print("  None detected. Three or more charges at a regular cadence are needed.")
    for item in items:
        status = "" if item.still_active else "   (looks cancelled)"
        print(
            f"  {item.merchant:<28} {fmt(item.typical_cents, sym):>11} {item.cadence:<12} "
            f"x{item.occurrences:<3} ≈{fmt(item.annualised_cents, sym)}/yr  "
            f"last {item.last_seen}{status}"
        )
    total = sum(i.monthly_equivalent_cents for i in items if i.still_active)
    print(f"\n  Active commitments total about {fmt(total, sym)} a month.")
    conn.close()
    return 0


def cmd_categorise(args, cfg: Config) -> int:
    conn = open_db(cfg)
    if args.txn_id and args.category:
        loader.set_category(conn, args.txn_id, args.category, create_rule=not args.no_rule)
        print(f"transaction {args.txn_id} set to {args.category!r}")
        if not args.no_rule:
            print("a rule was created so this merchant is categorised automatically in future")
    elif args.reclassify:
        changed = loader.reclassify_all(conn, only_unset=not args.force)
        print(f"re-categorised {changed} transaction(s) (your own choices were preserved)")
    else:
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(merchant_norm,''),'Unknown') m, COUNT(*) n,
                      SUM(-amount_cents) total, MIN(id) example
               FROM transactions
               WHERE status='active' AND amount_cents<0
                 AND COALESCE(category,'Uncategorised')='Uncategorised'
               GROUP BY 1 ORDER BY total DESC"""
        ).fetchall()
        if not rows:
            print("Everything is categorised.")
        else:
            print(heading("Uncategorised merchants"))
            print("  Assign one with: spendtracker categorise --txn <id> --category '<name>'\n")
            for row in rows[:40]:
                print(
                    f"  {row['m']:<32} {fmt(row['total'], cfg.currency_symbol):>12} "
                    f"n={row['n']:<4} example txn id {row['example']}"
                )
            print(heading("Available categories"))
            names = [c[0] for c in taxonomy.CATEGORIES]
            for index in range(0, len(names), 3):
                print("  " + "".join(f"{n:<30}" for n in names[index : index + 3]))
    conn.close()
    return 0


def cmd_receipts(args, cfg: Config) -> int:
    conn = open_db(cfg)
    sym = cfg.currency_symbol
    if args.set_total is not None or args.set_merchant or args.set_date or args.set_category:
        if not args.receipt_id:
            print("--receipt is required when setting a value", file=sys.stderr)
            return 1
        update_receipt(
            conn,
            args.receipt_id,
            cfg=cfg,
            merchant=args.set_merchant,
            receipt_date=args.set_date,
            total_cents=to_cents(args.set_total) if args.set_total is not None else None,
            category=args.set_category,
        )
        row = conn.execute(
            "SELECT link_status, match_reason FROM receipts WHERE id=?", (args.receipt_id,)
        ).fetchone()
        print(f"receipt #{args.receipt_id} updated — now {row['link_status']}: {row['match_reason']}")
        conn.close()
        return 0

    if args.rematch:
        counts = rematch_all_receipts(
            conn,
            amount_tolerance_cents=cfg.match_amount_tolerance_cents,
            days_window=cfg.match_days_window,
        )
        print("re-checked receipts: " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"))
        conn.close()
        return 0

    where = "WHERE link_status = ?" if args.status else ""
    params = [args.status] if args.status else []
    rows = conn.execute(
        f"""SELECT id, receipt_date, merchant_norm, total_cents, tender_type,
                   link_status, transaction_id, match_reason, extractor, confidence
            FROM receipts {where} ORDER BY receipt_date DESC, id DESC""",
        params,
    ).fetchall()
    print(heading(f"Receipts ({len(rows)})"))
    if not rows:
        print("  None yet. Add one with: spendtracker add-receipt slip.jpg")
    for row in rows[: args.top]:
        link = f"txn {row['transaction_id']}" if row["transaction_id"] else "—"
        print(
            f"  #{row['id']:<4} {row['receipt_date'] or '????-??-??'}  "
            f"{(row['merchant_norm'] or 'unknown'):<24} "
            f"{fmt(row['total_cents'], sym) if row['total_cents'] else 'no total':>11}  "
            f"{row['tender_type']:<8} {row['link_status']:<15} {link}"
        )
        if args.verbose and row["match_reason"]:
            print(f"        {row['match_reason']}")
    conn.close()
    return 0


def cmd_serve(args, cfg: Config) -> int:
    try:
        from .web.app import create_app
    except ImportError as exc:  # Flask is an optional extra
        print(
            "error: the web interface needs Flask, which is not installed.\n"
            "       pip install 'spendtracker[web]'   (or: pip install Flask)\n"
            "       Every other command works without it.\n"
            f"       ({exc})",
            file=sys.stderr,
        )
        return 3

    app = create_app(cfg)
    host = args.host or cfg.web_host
    port = args.port or cfg.web_port
    print(f"spendtracker running at http://{host}:{port}  (Ctrl+C to stop)")
    print(f"database: {cfg.db_path}")
    app.run(host=host, port=port, debug=args.debug)
    return 0


# ---------------------------------------------------------------------------
# Look before you import, and undo after you did
# ---------------------------------------------------------------------------


def cmd_inspect(args, cfg: Config) -> int:
    """Dry run. Show how a CSV would be read, write nothing."""
    profile = None
    if args.profile:
        profiles = load_profiles()
        if args.profile not in profiles:
            print(
                f"error: unknown profile {args.profile!r}. "
                f"Available: {', '.join(sorted(profiles)) or '(none)'}",
                file=sys.stderr,
            )
            return 2
        profile = profiles[args.profile]

    paths = expand_paths(args.paths)
    if not paths:
        print("error: no files matched", file=sys.stderr)
        return 2

    previews = []
    failed = 0
    for path in paths:
        try:
            previews.append(inspect_mod.preview_file(path, profile=profile))
        except CsvFormatError as exc:
            failed += 1
            print(f"{path}: cannot be parsed — {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps([p.to_dict() for p in previews], indent=2))
    else:
        for preview in previews:
            print(heading(f"Inspecting {preview.path.name}"))
            print(
                inspect_mod.format_preview(
                    preview, symbol=cfg.currency_symbol, rows=args.rows
                )
            )

    return 1 if failed else 0


def cmd_statements(args, cfg: Config) -> int:
    """Every import so far, and what each one contributed."""
    conn = open_db(cfg)
    rows = inspect_mod.list_statements(conn)
    sym = cfg.currency_symbol
    if not rows:
        print("Nothing imported yet.")
        conn.close()
        return 0

    print(heading("Statement imports"))
    print(
        f"  {'id':>4}  {'imported':<19} {'period':<25} {'rows':>6} {'dup':>5} "
        f"{'outflow':>14}  file"
    )
    for row in rows:
        period = f"{row['period_start'] or '—'} to {row['period_end'] or '—'}"
        print(
            f"  {row['id']:>4}  {row['imported_at'][:19]:<19} {period:<25} "
            f"{row['rows_imported']:>6} {row['rows_duplicate']:>5} "
            f"{fmt(row['outflow'], sym):>14}  {row['filename']}"
        )
    print("\n  Remove one with: spendtracker undo-import <id>")
    conn.close()
    return 0


def cmd_undo_import(args, cfg: Config) -> int:
    """Remove one import and everything it added."""
    conn = open_db(cfg)
    sym = cfg.currency_symbol
    try:
        plan = inspect_mod.plan_undo(conn, args.statement_id)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        conn.close()
        return 2

    print(heading(f"Undo import {plan.statement_id}"))
    print(f"  file             {plan.filename}")
    print(f"  imported         {plan.imported_at}")
    print(f"  transactions     {plan.transaction_count}")
    print(f"  outflow removed  {fmt(plan.outflow_cents, sym)}")
    print(f"  inflow removed   {fmt(plan.inflow_cents, sym)}")
    if plan.pending_candidates:
        print(f"  review queue     {plan.pending_candidates} pending item(s) will be dropped")
    if plan.linked_receipts:
        print(
            f"  linked slips     {plan.linked_receipts} — these return to the review "
            "queue, they are not deleted"
        )
    if plan.cash_allocations:
        print(
            f"  cash allocations {plan.cash_allocations} — these ARE deleted; the cash "
            "breakdown will need redoing"
        )

    if plan.blocked and not args.force:
        print(
            "\n  Refusing: this import has slips linked to it. Re-run with --force "
            "if you accept the above.",
            file=sys.stderr,
        )
        conn.close()
        return 1

    if not args.yes:
        answer = input("\n  Type 'yes' to remove it: ").strip().lower()
        if answer != "yes":
            print("  Cancelled. Nothing changed.")
            conn.close()
            return 0

    inspect_mod.undo_statement(conn, args.statement_id, force=args.force)
    print(f"\n  Removed {plan.transaction_count} transaction(s).")
    conn.close()
    return 0


def cmd_status(args, cfg: Config) -> int:
    conn = open_db(cfg)
    sym = cfg.currency_symbol
    print(heading("spendtracker status"))
    print(f"  database         {cfg.db_path}")
    print(f"  currency         {cfg.currency_code} ({sym})")
    print(f"  slip reader      {cfg.ocr_provider}"
          + (f" ({cfg.ocr_model})" if cfg.ocr_provider == "claude" else ""))
    if cfg.ocr_provider == "claude" and not cfg.anthropic_api_key:
        print("                   ! no ANTHROPIC_API_KEY set — slips will need manual entry")
    print(f"  credit cards imported: {cfg.credit_card_statements_imported}")

    stmts = conn.execute(
        "SELECT COUNT(*) n, MIN(period_start) lo, MAX(period_end) hi FROM statements"
    ).fetchone()
    txns = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active, "
        "SUM(CASE WHEN status='duplicate' THEN 1 ELSE 0 END) dup FROM transactions"
    ).fetchone()
    receipts = conn.execute("SELECT COUNT(*) n FROM receipts").fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) n FROM duplicate_candidates WHERE resolution='pending'"
    ).fetchone()

    print(f"\n  statements       {stmts['n']} covering {stmts['lo'] or '—'} to {stmts['hi'] or '—'}")
    print(f"  transactions     {txns['active'] or 0} active, {txns['dup'] or 0} held as duplicates")
    print(f"  receipts         {receipts['n']}")
    print(f"  awaiting review  {pending['n']}")
    conn.close()
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spendtracker",
        description="Track every outflow on your bank account, from statement CSVs and till slips.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--data-dir", help="override where the database lives")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-statement", help="import one or more bank statement CSVs")
    p.add_argument("paths", nargs="+", help="CSV files (globs allowed)")
    p.add_argument("--account", default="Main Account")
    p.add_argument("--profile", help="named layout from bank_profiles.json")
    p.add_argument("--force", action="store_true", help="re-import a file already seen")
    p.add_argument("-v", "--verbose", action="store_true", help="show skipped rows")
    p.set_defaults(func=cmd_import_statement)

    p = sub.add_parser("add-receipt", help="add till slip photographs")
    p.add_argument("paths", nargs="+", help="image files (globs allowed)")
    p.add_argument("--account", default="Main Account")
    p.add_argument("--provider", choices=["claude", "tesseract", "manual"])
    p.set_defaults(func=cmd_add_receipt)

    p = sub.add_parser("report", help="spending breakdown for a period")
    p.add_argument("--period", help="2026-03, 2026-Q1, last-month, last-90-days, ytd, or a:b")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--trend", type=int, default=6, help="months of trend to show (0 = none)")
    p.add_argument(
        "--json",
        nargs="?",
        const=True,
        metavar="FILE",
        help="emit the report as JSON (to FILE, or stdout if no file given)",
    )
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("advice", help="frivolous spend and reduction opportunities")
    p.add_argument("--period")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("-v", "--verbose", action="store_true", help="show the evidence")
    p.set_defaults(func=cmd_advice)

    p = sub.add_parser("review", help="resolve suspected duplicate transactions")
    p.add_argument("--yes-duplicate", action="store_true", help="accept all as duplicates")
    p.add_argument("--yes-distinct", action="store_true", help="keep all as distinct")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("merchants", help="spend by merchant")
    p.add_argument("--period")
    p.add_argument("--top", type=int, default=40)
    p.set_defaults(func=cmd_merchants)

    p = sub.add_parser("recurring", help="subscriptions and repeating charges")
    p.add_argument("--all", action="store_true", help="include variable repeating spend")
    p.set_defaults(func=cmd_recurring)

    p = sub.add_parser("categorise", help="review and fix categories")
    p.add_argument("--txn", dest="txn_id", type=int)
    p.add_argument("--category")
    p.add_argument("--no-rule", action="store_true", help="do not learn from this correction")
    p.add_argument("--reclassify", action="store_true", help="re-run rules over stored rows")
    p.add_argument("--force", action="store_true", help="with --reclassify, also refresh rule-set rows")
    p.set_defaults(func=cmd_categorise)

    p = sub.add_parser("receipts", help="list and correct stored till slips")
    p.add_argument("--status", choices=["matched", "cash_allocated", "unmatched", "ignored"])
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--receipt", dest="receipt_id", type=int)
    p.add_argument("--set-total", type=str, help="correct the total, e.g. 289.92")
    p.add_argument("--set-merchant")
    p.add_argument("--set-date", help="YYYY-MM-DD")
    p.add_argument("--set-category")
    p.add_argument("--rematch", action="store_true", help="re-run matching for every receipt")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("serve", help="start the web interface")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "inspect",
        help="dry run: show how a statement CSV would be read, import nothing",
    )
    p.add_argument("paths", nargs="+", help="CSV file(s) or a glob")
    p.add_argument("--profile", help="force a named column layout")
    p.add_argument("--rows", type=int, default=8, help="sample rows to show")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("statements", help="list past imports")
    p.set_defaults(func=cmd_statements)

    p = sub.add_parser("undo-import", help="remove one import and everything it added")
    p.add_argument("statement_id", type=int, help="id from `spendtracker statements`")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument(
        "--force",
        action="store_true",
        help="proceed even when slips are linked to these transactions",
    )
    p.set_defaults(func=cmd_undo_import)

    p = sub.add_parser("status", help="what has been imported so far")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {"data_dir": args.data_dir} if args.data_dir else None
    cfg = load_config(overrides=overrides)
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
