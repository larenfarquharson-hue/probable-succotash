"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (__version__, analysis, categorise, config, csvimport, db, ingest,
               matching, report_html, report_text, slips, taxonomy)
from .parsing import ParseError


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 1
    try:
        return args.handler(args) or 0
    except (ParseError, ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spendtrack",
        description="Track every outflow on a bank account from CSV statements and "
                    "till slips, without double counting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"spendtrack {__version__}")
    parser.add_argument("--home", metavar="DIR",
                        help="override the data directory (default ~/.spendtrack)")
    sub = parser.add_subparsers(dest="command")

    # ---- import ----------------------------------------------------------
    p = sub.add_parser("import", help="import one or more statement CSV files")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--account", default="main",
                   help="name for the account these statements belong to")
    p.add_argument("--profile", help="bank profile to use (see `spendtrack profiles`)")
    p.add_argument("--positive-is", choices=["inflow", "outflow"],
                   help="override how the sign of the amount column is read")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be imported without writing anything")
    p.add_argument("--no-match", action="store_true",
                   help="skip re-matching slips afterwards")
    p.set_defaults(handler=cmd_import)

    # ---- inspect ---------------------------------------------------------
    p = sub.add_parser("inspect", help="show how a CSV would be interpreted")
    p.add_argument("path", type=Path)
    p.add_argument("--profile")
    p.add_argument("--positive-is", choices=["inflow", "outflow"])
    p.add_argument("--rows", type=int, default=8, help="sample rows to display")
    p.set_defaults(handler=cmd_inspect)

    # ---- slip ------------------------------------------------------------
    p = sub.add_parser("slip", help="work with till slips")
    slip_sub = p.add_subparsers(dest="slip_command")

    sp = slip_sub.add_parser("add", help="ingest slip JSON files, images, or a folder")
    sp.add_argument("paths", nargs="+", type=Path)
    sp.add_argument("--force", action="store_true",
                    help="store even if it looks like a slip already added")
    sp.add_argument("--lang", default="eng", help="OCR language (default eng)")
    sp.add_argument("--no-match", action="store_true")
    sp.set_defaults(handler=cmd_slip_add)

    sp = slip_sub.add_parser("enter", help="type a slip in by hand")
    sp.set_defaults(handler=cmd_slip_enter)

    sp = slip_sub.add_parser("list", help="list stored slips")
    sp.add_argument("--status", help="filter by status")
    sp.set_defaults(handler=cmd_slip_list)

    sp = slip_sub.add_parser("show", help="show one slip in full")
    sp.add_argument("slip_id", type=int)
    sp.set_defaults(handler=cmd_slip_show)

    sp = slip_sub.add_parser("template", help="print the slip JSON format")
    sp.set_defaults(handler=cmd_slip_template)

    sp = slip_sub.add_parser("link", help="link a slip to a transaction by hand")
    sp.add_argument("slip_id", type=int)
    sp.add_argument("txn_id", type=int)
    sp.set_defaults(handler=cmd_slip_link)

    sp = slip_sub.add_parser("unlink", help="undo a slip's link")
    sp.add_argument("slip_id", type=int)
    sp.set_defaults(handler=cmd_slip_unlink)

    sp = slip_sub.add_parser("delete", help="delete a slip")
    sp.add_argument("slip_id", type=int)
    sp.set_defaults(handler=cmd_slip_delete)

    # ---- match -----------------------------------------------------------
    p = sub.add_parser("match", help="match pending slips to statement lines")
    p.add_argument("--rematch", action="store_true",
                   help="clear automatic links and start again")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(handler=cmd_match)

    # ---- report ----------------------------------------------------------
    p = sub.add_parser("report", help="the full spending report for a period")
    p.add_argument("period", nargs="?", default="last-month",
                   help="YYYY-MM, YYYY, YYYY-MM-DD:YYYY-MM-DD, this-month, "
                        "last-month, ytd, all")
    p.add_argument("--account")
    p.add_argument("--merchants", type=int, default=12)
    p.add_argument("--html", type=Path, metavar="FILE",
                   help="also write a self-contained HTML report")
    p.add_argument("--json", type=Path, metavar="FILE",
                   help="also write the report data as JSON")
    p.add_argument("--quiet", action="store_true", help="suppress the terminal report")
    p.set_defaults(handler=cmd_report)

    # ---- compare ---------------------------------------------------------
    p = sub.add_parser("compare", help="compare category totals across periods")
    p.add_argument("periods", nargs="+")
    p.set_defaults(handler=cmd_compare)

    # ---- list / review ---------------------------------------------------
    p = sub.add_parser("list", help="list transactions")
    p.add_argument("period", nargs="?", default="all")
    p.add_argument("--category")
    p.add_argument("--merchant")
    p.add_argument("--search")
    p.add_argument("--min", type=float, dest="min_amount")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("review", help="categorise uncategorised transactions")
    p.add_argument("period", nargs="?", default="all")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(handler=cmd_review)

    p = sub.add_parser("categorise", help="set a category by hand and remember it")
    p.add_argument("txn_id", type=int)
    p.add_argument("category")
    p.add_argument("--merchant")
    p.add_argument("--internal", action="store_true",
                   help="mark as a transfer between your own accounts")
    p.add_argument("--exclude", action="store_true",
                   help="leave out of spending totals entirely")
    p.set_defaults(handler=cmd_categorise)

    p = sub.add_parser("recategorise", help="re-run the rules over stored transactions")
    p.add_argument("--only-uncategorised", action="store_true")
    p.set_defaults(handler=cmd_recategorise)

    # ---- housekeeping ----------------------------------------------------
    p = sub.add_parser("categories", help="list categories and their discretion weights")
    p.set_defaults(handler=cmd_categories)

    p = sub.add_parser("profiles", help="list available bank profiles")
    p.set_defaults(handler=cmd_profiles)

    p = sub.add_parser("rules", help="manage the user rules file")
    p.add_argument("action", choices=["path", "init", "show"], default="path", nargs="?")
    p.set_defaults(handler=cmd_rules)

    p = sub.add_parser("settings", help="show or change settings")
    p.add_argument("--income", type=float, help="monthly take-home pay")
    p.add_argument("--currency")
    p.add_argument("--small-threshold", type=float)
    p.add_argument("--match-window", type=int, help="slip matching window in days")
    p.set_defaults(handler=cmd_settings)

    p = sub.add_parser("imports", help="list past imports")
    p.set_defaults(handler=cmd_imports)

    p = sub.add_parser("undo-import", help="remove everything one import added")
    p.add_argument("import_id", type=int)
    p.set_defaults(handler=cmd_undo_import)

    p = sub.add_parser("audit-duplicates",
                       help="look for transactions that may have been counted twice")
    p.add_argument("period", nargs="?", default="all")
    p.set_defaults(handler=cmd_audit_duplicates)

    p = sub.add_parser("status", help="what is loaded and what needs attention")
    p.set_defaults(handler=cmd_status)

    return parser


EPILOG = """\
Getting started
  spendtrack inspect june.csv           # check how the file will be read
  spendtrack import june.csv july.csv   # load statements (safe to repeat)
  spendtrack slip template > slip.json  # see the slip format
  spendtrack slip add slips/            # add slips (JSON or photos)
  spendtrack report 2026-06 --html report.html

Nothing leaves your machine. Data lives in ~/.spendtrack (override with
SPENDTRACK_HOME or --home).
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _prepare(args) -> None:
    if getattr(args, "home", None):
        import os
        os.environ[config.ENV_HOME] = str(Path(args.home).expanduser())
    config.ensure_dirs()


def _open(args):
    _prepare(args)
    return db.connect(), config.Settings.load()


def _period(args, conn) -> analysis.Period:
    return analysis.parse_period(getattr(args, "period", None), conn)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_import(args) -> int:
    conn, cfg = _open(args)
    cat = categorise.build()
    total_new = total_dupe = 0
    for path in args.paths:
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 2
        summary = ingest.import_statement(
            conn, path, account=args.account, profile_name=args.profile,
            positive_is=args.positive_is, dry_run=args.dry_run, categoriser=cat)
        total_new += summary.inserted
        total_dupe += summary.duplicates
        verb = "would import" if args.dry_run else "imported"
        print(f"{path.name}: {verb} {summary.inserted} transaction(s), "
              f"{summary.duplicates} already present"
              + (f", {summary.fee_rows} fee line(s) split out" if summary.fee_rows else ""))
        parse = summary.parse
        if parse:
            if parse.rows:
                print(f"  period {parse.rows[0].txn_date} to {parse.rows[-1].txn_date}, "
                      f"outflows {config.money(parse.outflow_total, cfg.currency)}, "
                      f"inflows {config.money(parse.inflow_total, cfg.currency)}")
            for note in parse.notes:
                print(f"  note: {note}")
            if parse.skipped:
                print(f"  skipped {len(parse.skipped)} non-transaction row(s) "
                      f"(headers, totals, footers)")
                for line_no, reason in parse.skipped[:3]:
                    print(f"    line {line_no}: {reason}")
        for warning in summary.warnings:
            print(f"  {warning}")

    if args.dry_run:
        print(f"\ndry run: {total_new} new, {total_dupe} duplicate(s). Nothing written.")
        return 0

    print(f"\n{total_new} transaction(s) added, {total_dupe} skipped as duplicates.")
    if not args.no_match:
        report = matching.match_slips(conn, cfg)
        if report.considered:
            print(f"slips: {report.summary_line()}")
    return 0


def cmd_inspect(args) -> int:
    _prepare(args)
    profile = csvimport.load_profile(args.profile) if args.profile else None
    result = csvimport.parse_file(args.path, profile=profile,
                                 positive_is=args.positive_is)
    print(f"{args.path}:")
    print(csvimport.describe(result))
    for note in result.notes:
        print(f"  note: {note}")
    if result.headers:
        print(f"  headers         : {result.headers}")
    print(f"\n  outflows        : {config.money(result.outflow_total)}")
    print(f"  inflows         : {config.money(result.inflow_total)}")
    if result.rows:
        print(f"\nFirst {args.rows} parsed row(s):")
        for row in result.rows[:args.rows]:
            fee = f"  fee {config.money(row.fee)}" if row.fee else ""
            print(f"  {row.txn_date}  {config.money(row.amount):>14}  "
                  f"{row.description[:56]}{fee}")
    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} row(s):")
        for line_no, reason in result.skipped[:10]:
            print(f"  line {line_no}: {reason}")
    print("\nIf any of the above is wrong, write a profile — "
          "`spendtrack profiles` shows where they live.")
    return 0


def cmd_slip_add(args) -> int:
    conn, cfg = _open(args)
    cat = categorise.build()
    added = dupes = 0
    for path in args.paths:
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 2
        results = slips.ingest_path(conn, path, force=args.force, ocr_lang=args.lang,
                                   categoriser=cat)
        for result in results:
            label = result.slip.merchant or "(unknown merchant)"
            total = (config.money(result.slip.total, cfg.currency)
                     if result.slip.total is not None else "no total")
            if result.duplicate:
                dupes += 1
                print(f"  already have this slip: {label} {total} "
                      f"(slip {result.slip_id}) — not counted again")
                continue
            added += 1
            print(f"  slip {result.slip_id}: {label} {total} "
                  f"{result.slip.slip_date or ''} [{result.slip.payment_method}]")
            for problem in result.problems:
                print(f"    check: {problem}")
    print(f"\n{added} slip(s) added, {dupes} already present.")
    if added and not args.no_match:
        report = matching.match_slips(conn, cfg)
        print(f"matching: {report.summary_line()}")
        for outcome in report.unmatched + report.over_cash:
            print(f"  slip {outcome.slip_id} ({outcome.merchant}, "
                  f"{config.money(outcome.total or 0, cfg.currency)}): {outcome.reason}")
    return 0


def cmd_slip_enter(args) -> int:
    conn, cfg = _open(args)
    print("Type the slip details. Press enter to skip an optional field.\n")
    merchant = input("Merchant: ").strip()
    slip_date = input("Date (YYYY-MM-DD): ").strip()
    total = input("Total: ").strip()
    method = input("Paid by [card/cash/eft]: ").strip() or "unknown"
    time_value = input("Time (HH:MM, optional): ").strip()
    note = input("Note (optional): ").strip()

    from . import parsing
    slip = slips.Slip(
        merchant=merchant or None,
        slip_date=parsing.parse_date(slip_date) if slip_date else None,
        slip_time=time_value or None,
        total=abs(parsing.parse_amount(total)) if total else None,
        payment_method=slips.normalise_payment_method(method),
        source="manual",
        notes=note or None,
    )
    print("\nLine items — description then amount. Blank description to finish.")
    while True:
        desc = input("  item: ").strip()
        if not desc:
            break
        amount = input("  amount: ").strip()
        slip.items.append(slips.SlipItem(
            description=desc,
            line_total=abs(parsing.parse_amount(amount)) if amount else None))

    result = slips.save_slip(conn, slip)
    if result.duplicate:
        print(f"\nThat slip is already stored as slip {result.slip_id}. Nothing added.")
        return 0
    print(f"\nStored as slip {result.slip_id}.")
    for problem in result.problems:
        print(f"  check: {problem}")
    report = matching.match_slips(conn, cfg)
    print(f"matching: {report.summary_line()}")
    return 0


def cmd_slip_list(args) -> int:
    conn, cfg = _open(args)
    query = "SELECT * FROM slips"
    params: list[object] = []
    if args.status:
        query += " WHERE status = ?"
        params.append(args.status)
    query += " ORDER BY slip_date DESC, id DESC"
    rows = conn.execute(query, params).fetchall()
    print(report_text.render_slip_table(rows, cfg.currency))
    return 0


def cmd_slip_show(args) -> int:
    conn, cfg = _open(args)
    slip = conn.execute("SELECT * FROM slips WHERE id = ?", (args.slip_id,)).fetchone()
    if slip is None:
        print(f"error: no slip with id {args.slip_id}", file=sys.stderr)
        return 2
    print(f"Slip {slip['id']}")
    for key in ("slip_date", "slip_time", "merchant", "total", "tax",
                "payment_method", "card_last4", "status", "match_score",
                "match_reason", "category", "image_path", "source", "notes"):
        value = slip[key]
        if value not in (None, ""):
            print(f"  {key:<15}: {value}")
    if slip["matched_txn_id"]:
        txn = conn.execute("SELECT * FROM transactions WHERE id = ?",
                           (slip["matched_txn_id"],)).fetchone()
        if txn:
            print(f"  linked to      : txn {txn['id']} {txn['txn_date']} "
                  f"{config.money(float(txn['amount']), cfg.currency)} "
                  f"{txn['description'][:50]}")
    items = conn.execute("SELECT * FROM slip_items WHERE slip_id = ?",
                         (args.slip_id,)).fetchall()
    if items:
        print("  items:")
        for item in items:
            total = (config.money(float(item["line_total"]), cfg.currency)
                     if item["line_total"] is not None else "-")
            print(f"    {item['description'][:44]:<44}{total:>12}"
                  f"  {item['category'] or ''}")
    return 0


def cmd_slip_template(args) -> int:
    print(json.dumps(slips.SLIP_JSON_TEMPLATE, indent=2))
    print()
    print("# One object, or a list of objects, per file. `items` is optional but",
          file=sys.stderr)
    print("# makes the report much more useful. `payment_method` matters: 'cash'",
          file=sys.stderr)
    print("# tells SpendTrack to allocate this against an ATM withdrawal instead",
          file=sys.stderr)
    print("# of looking for a card transaction.", file=sys.stderr)
    return 0


def cmd_slip_link(args) -> int:
    conn, _cfg = _open(args)
    matching.link_manually(conn, args.slip_id, args.txn_id)
    print(f"Slip {args.slip_id} linked to transaction {args.txn_id}. "
          f"It will not be re-matched automatically.")
    return 0


def cmd_slip_unlink(args) -> int:
    conn, _cfg = _open(args)
    matching.unlink(conn, args.slip_id)
    print(f"Slip {args.slip_id} unlinked and back in the queue.")
    return 0


def cmd_slip_delete(args) -> int:
    conn, _cfg = _open(args)
    conn.execute("DELETE FROM slip_items WHERE slip_id = ?", (args.slip_id,))
    changed = conn.execute("DELETE FROM slips WHERE id = ?", (args.slip_id,)).rowcount
    conn.commit()
    print(f"Deleted {changed} slip(s).")
    return 0


def cmd_match(args) -> int:
    conn, cfg = _open(args)
    report = matching.match_slips(conn, cfg, rematch=args.rematch, dry_run=args.dry_run)
    print(report.summary_line())
    for name, label in (("matched", "matched"), ("cash_allocated", "cash"),
                        ("unmatched", "unmatched"), ("over_cash", "over cash"),
                        ("skipped", "incomplete")):
        for outcome in getattr(report, name):
            total = config.money(outcome.total or 0, cfg.currency)
            print(f"  [{label}] slip {outcome.slip_id} {outcome.merchant} {total}"
                  + (f" -> txn {outcome.txn_id}" if outcome.txn_id else ""))
            print(f"      {outcome.reason}")
    if args.dry_run:
        print("\ndry run: nothing was changed.")
    return 0


def cmd_report(args) -> int:
    conn, cfg = _open(args)
    period = _period(args, conn)
    report = analysis.build_report(conn, period, cfg, account=args.account)
    if not args.quiet:
        print(report_text.render(report, top_merchants=args.merchants))
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(report_html.render(report), encoding="utf-8")
        print(f"HTML report written to {args.html}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(to_dict(report), indent=2), encoding="utf-8")
        print(f"JSON written to {args.json}")
    return 0


def cmd_compare(args) -> int:
    conn, cfg = _open(args)
    periods = [analysis.parse_period(spec, conn) for spec in args.periods]
    result = analysis.compare(conn, periods, cfg)
    print(report_text.render_comparison(result, cfg.currency))
    return 0


def cmd_list(args) -> int:
    conn, cfg = _open(args)
    period = _period(args, conn)
    query = ["SELECT * FROM transactions WHERE txn_date BETWEEN ? AND ?"]
    params: list[object] = [period.start, period.end]
    if args.category:
        query.append("AND category = ?")
        params.append(args.category)
    if args.merchant:
        query.append("AND merchant LIKE ?")
        params.append(f"%{args.merchant}%")
    if args.search:
        query.append("AND description LIKE ?")
        params.append(f"%{args.search}%")
    if args.min_amount:
        query.append("AND ABS(amount) >= ?")
        params.append(args.min_amount)
    query.append("ORDER BY txn_date, id")
    rows = conn.execute(" ".join(query), params).fetchall()
    print(report_text.render_transactions(rows, cfg.currency, args.limit))
    total = sum(float(r["amount"]) for r in rows if float(r["amount"]) < 0)
    print(f"\n{len(rows)} transaction(s), outflows "
          f"{config.money(total, cfg.currency)}")
    return 0


def cmd_review(args) -> int:
    conn, cfg = _open(args)
    period = _period(args, conn)
    rows = conn.execute(
        "SELECT * FROM transactions WHERE category = ? AND txn_date BETWEEN ? AND ?"
        " AND amount < 0 ORDER BY ABS(amount) DESC LIMIT ?",
        (taxonomy.UNCATEGORISED, period.start, period.end, args.limit)).fetchall()
    if not rows:
        print("Nothing uncategorised in this period.")
        return 0

    names = sorted(taxonomy.CATEGORIES)
    print(f"{len(rows)} uncategorised transaction(s). Enter a category name or its "
          f"number, 'x' to exclude, 's' to skip, 'q' to stop.")
    print("Anything you set is remembered for future imports of the same description.\n")
    for index, name in enumerate(names, start=1):
        print(f"  {index:>2}. {name}")
    print()

    for row in rows:
        print(f"{row['txn_date']}  {config.money(float(row['amount']), cfg.currency)}  "
              f"{row['description']}")
        answer = input("  category> ").strip()
        if answer.lower() in ("q", "quit"):
            break
        if answer.lower() in ("s", "skip", ""):
            continue
        if answer.lower() in ("x", "exclude"):
            ingest.set_override(conn, row["description_key"], excluded=True)
            print("  excluded from totals.")
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            chosen = names[int(answer) - 1]
        else:
            chosen = answer
        count = ingest.set_override(conn, row["description_key"], category=chosen)
        print(f"  set to {chosen} ({count} transaction(s) updated, "
              f"including future imports).")
    return 0


def cmd_categorise(args) -> int:
    conn, _cfg = _open(args)
    row = conn.execute("SELECT description_key FROM transactions WHERE id = ?",
                       (args.txn_id,)).fetchone()
    if row is None:
        print(f"error: no transaction with id {args.txn_id}", file=sys.stderr)
        return 2
    count = ingest.set_override(conn, row["description_key"], category=args.category,
                                merchant=args.merchant, internal=args.internal,
                                excluded=args.exclude)
    print(f"{count} transaction(s) set to {args.category}. Remembered for future imports.")
    return 0


def cmd_recategorise(args) -> int:
    conn, _cfg = _open(args)
    changed = ingest.recategorise(conn, only_uncategorised=args.only_uncategorised)
    print(f"Re-ran the rules over {changed} transaction(s). "
          f"Manual decisions were preserved.")
    return 0


def cmd_categories(args) -> int:
    _prepare(args)
    categorise.build()   # applies any user overrides to the taxonomy
    print(f"{'Category':<28}{'Group':<18}{'Discretion':>11}  Kind")
    print("-" * 78)
    for name in sorted(taxonomy.CATEGORIES):
        cat = taxonomy.CATEGORIES[name]
        print(f"{name:<28}{cat.group:<18}{cat.discretion:>10.0%}  {cat.kind}")
    print("\nDiscretion is the share of a category treated as reducible. Change it in")
    print(f"{config.rules_path()} under \"categories\".")
    return 0


def cmd_profiles(args) -> int:
    _prepare(args)
    found = csvimport.list_profiles()
    if found:
        for name, description in found:
            print(f"  {name:<16}{description}")
    else:
        print("  (none)")
    print(f"\nBuilt-in: {csvimport.profiles_dir()}")
    print(f"Yours   : {config.home_dir() / 'profiles'}")
    print("\nProfiles are only needed when auto-detection gets something wrong. Run")
    print("`spendtrack inspect <file.csv>` first — it usually works without one.")
    return 0


def cmd_rules(args) -> int:
    _prepare(args)
    path = config.rules_path()
    if args.action == "init":
        if path.exists():
            print(f"{path} already exists; leaving it alone.")
            return 0
        categorise.write_template(path)
        print(f"Wrote a starter rules file to {path}")
        return 0
    if args.action == "show":
        if not path.exists():
            print(f"No rules file yet. Create one with `spendtrack rules init`.")
            return 0
        print(path.read_text(encoding="utf-8"))
        return 0
    print(path)
    return 0


def cmd_settings(args) -> int:
    _prepare(args)
    cfg = config.Settings.load()
    changed = False
    if args.income is not None:
        cfg.monthly_income = args.income
        changed = True
    if args.currency:
        cfg.currency = args.currency
        changed = True
    if args.small_threshold is not None:
        cfg.small_txn_threshold = args.small_threshold
        changed = True
    if args.match_window is not None:
        cfg.slip_match_window_days = args.match_window
        changed = True
    if changed:
        print(f"Saved to {cfg.save()}")
    print(f"  data directory        : {config.home_dir()}")
    print(f"  currency              : {cfg.currency}")
    print(f"  monthly income        : "
          f"{cfg.monthly_income if cfg.monthly_income is not None else '(not set)'}")
    print(f"  small spend threshold : {cfg.small_txn_threshold}")
    print(f"  slip match window     : {cfg.slip_match_window_days} days")
    print(f"  slip amount tolerance : {cfg.slip_match_amount_tolerance}")
    return 0


def cmd_imports(args) -> int:
    conn, _cfg = _open(args)
    rows = conn.execute("SELECT * FROM imports ORDER BY id").fetchall()
    if not rows:
        print("Nothing imported yet.")
        return 0
    print(f"{'ID':>4}  {'When':<20}{'Kind':<10}{'New':>6}{'Dupe':>6}  File")
    for row in rows:
        print(f"{row['id']:>4}  {row['imported_at']:<20}{row['kind']:<10}"
              f"{row['rows_new']:>6}{row['rows_dupe']:>6}  {Path(row['path']).name}")
    return 0


def cmd_undo_import(args) -> int:
    conn, _cfg = _open(args)
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (args.import_id,)).fetchone()
    if row is None:
        print(f"error: no import with id {args.import_id}", file=sys.stderr)
        return 2
    print(f"About to remove everything added by import {args.import_id} "
          f"({Path(row['path']).name}, {row['rows_new']} row(s)).")
    if input("Type 'yes' to confirm: ").strip().lower() != "yes":
        print("Cancelled.")
        return 0
    removed = ingest.undo_import(conn, args.import_id)
    print(f"Removed {removed} transaction(s).")
    return 0


def cmd_audit_duplicates(args) -> int:
    """Surface possible double counting the fingerprint could not catch."""
    conn, cfg = _open(args)
    period = _period(args, conn)
    rows = conn.execute(
        "SELECT txn_date, amount, description_key, COUNT(*) n,"
        "       GROUP_CONCAT(id) ids, MIN(description) sample"
        "  FROM transactions WHERE txn_date BETWEEN ? AND ?"
        " GROUP BY txn_date, amount, description_key HAVING COUNT(*) > 1"
        " ORDER BY ABS(amount) DESC",
        (period.start, period.end)).fetchall()
    if not rows:
        print(f"No repeated transactions in {period.label}. Nothing looks double counted.")
        return 0
    print(f"Repeated transactions in {period.label} — identical date, amount and")
    print("description. These are usually genuine (two of the same coffee), but if a")
    print("statement was loaded twice from differently formatted exports they could be")
    print("duplicates. Check the ids and remove one with `spendtrack undo-import`.\n")
    for row in rows:
        print(f"  {row['txn_date']}  {config.money(float(row['amount']), cfg.currency):>13}"
              f"  x{row['n']}  ids {row['ids']}  {row['sample'][:44]}")
    print("\nBalances can settle this: repeated purchases have different running")
    print("balances, a re-import has the same one.")
    return 0


def cmd_status(args) -> int:
    conn, cfg = _open(args)
    txn = conn.execute(
        "SELECT COUNT(*) n, MIN(txn_date) a, MAX(txn_date) b,"
        "       COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) out"
        "  FROM transactions").fetchone()
    print(f"Data directory : {config.home_dir()}")
    print(f"Transactions   : {txn['n']}"
          + (f"  ({txn['a']} to {txn['b']})" if txn["n"] else ""))
    if txn["n"]:
        print(f"Total outflows : {config.money(float(txn['out']), cfg.currency)}")
    accounts = conn.execute(
        "SELECT a.name, COUNT(t.id) n FROM accounts a"
        " LEFT JOIN transactions t ON t.account_id = a.id GROUP BY a.id").fetchall()
    if accounts:
        print("Accounts       : " + ", ".join(f"{r['name']} ({r['n']})" for r in accounts))
    slip_rows = conn.execute(
        "SELECT status, COUNT(*) n FROM slips GROUP BY status").fetchall()
    if slip_rows:
        print("Slips          : " + ", ".join(f"{r['status']} {r['n']}" for r in slip_rows))
    else:
        print("Slips          : none yet")
    uncat = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(-amount), 0) t FROM transactions"
        " WHERE category = ? AND amount < 0", (taxonomy.UNCATEGORISED,)).fetchone()
    if uncat["n"]:
        print(f"Uncategorised  : {uncat['n']} worth "
              f"{config.money(float(uncat['t']), cfg.currency)} "
              f"— run `spendtrack review`")
    months = conn.execute(
        "SELECT COUNT(DISTINCT substr(txn_date, 1, 7)) m FROM transactions").fetchone()["m"]
    print(f"Months loaded  : {months}")
    if months and months < 3:
        print("  Annual projections firm up once three or more months are loaded.")
    print("\nOCR for slip photos: "
          + ("tesseract found" if slips.tesseract_available()
             else "tesseract not installed — use slip JSON or `spendtrack slip enter`"))
    return 0


def to_dict(report: analysis.PeriodReport) -> dict:
    """Report as plain data, for JSON export or another front end."""
    rec = report.reconciliation
    return {
        "period": {"start": report.period.start, "end": report.period.end,
                   "label": report.period.label, "days": report.period.days},
        "currency": report.currency,
        "accounts": report.accounts,
        "months_observed": report.months_observed,
        "reconciliation": {
            "total_out": rec.total_out, "consumption": rec.consumption,
            "debt": rec.debt, "savings": rec.savings, "transfers": rec.transfers,
            "excluded": rec.excluded, "income": rec.income, "refunds": rec.refunds,
            "cash_withdrawn": rec.cash_withdrawn,
            "cash_explained": rec.cash_explained,
            "cash_unexplained": rec.cash_unexplained,
            "accounted": rec.accounted, "difference": rec.difference,
            "balances": rec.balances,
        },
        "categories": [
            {"name": b.name, "total": b.total, "count": b.count, "group": b.group,
             "discretion": b.discretion, "from_cash": b.from_cash,
             "slip_backed": b.slip_backed}
            for b in report.categories
        ],
        "groups": [{"name": b.name, "total": b.total} for b in report.groups],
        "merchants": [
            {"name": b.name, "total": b.total, "count": b.count,
             "from_cash": b.from_cash}
            for b in report.merchants
        ],
        "recurring": report.recurring,
        "largest": report.largest,
        "daily": report.daily,
        "uncategorised": report.uncategorised,
        "slip_coverage": report.slip_coverage,
        "insights": [
            {"title": i.title, "detail": i.detail, "kind": i.kind,
             "confidence": i.confidence, "period_amount": i.period_amount,
             "annual_amount": i.annual_amount, "monthly_saving": i.monthly_saving,
             "annual_saving": i.annual_saving, "counts_to_total": i.counts_to_total,
             "action": i.action, "evidence": i.evidence}
            for i in report.insights
        ],
        "totals": {"monthly_reducible": report.monthly_reducible,
                   "annual_reducible": report.annual_reducible},
        "data_quality": report.data_quality,
    }


if __name__ == "__main__":
    raise SystemExit(main())
