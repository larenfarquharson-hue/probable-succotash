"""Load parsed statement rows into the ledger.

Import is idempotent at three levels, so re-running it is always safe:
  1. the same file (identical bytes) is recognised and skipped;
  2. the same row inside a different file is caught by its fingerprint;
  3. anything the data cannot prove either way lands in the review queue.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import db as dbmod
from ..categorise import Classifier, canonical_key
from ..config import Config
from ..dedupe import (
    assign_occurrences,
    covered_ranges,
    find_duplicate_candidates,
    record_candidate,
    rematch_all_receipts,
    row_fingerprint,
)
from . import csvimport
from .csvimport import ParseResult


@dataclass
class ImportReport:
    statement_id: int | None
    account_id: int
    filename: str
    rows_total: int = 0
    rows_imported: int = 0
    rows_duplicate_exact: int = 0
    rows_flagged_duplicate: int = 0
    rows_flagged_review: int = 0
    rows_skipped: int = 0
    already_imported: bool = False
    period_start: date | None = None
    period_end: date | None = None
    outflow_cents: int = 0
    inflow_cents: int = 0
    warnings: list[str] = field(default_factory=list)
    detection_notes: list[str] = field(default_factory=list)
    skipped_detail: list[tuple[int, str, str]] = field(default_factory=list)
    receipt_rematch: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        if self.already_imported:
            return f"{self.filename}: already imported, nothing to do."
        parts = [
            f"{self.filename}: {self.rows_imported} row(s) imported",
        ]
        if self.rows_duplicate_exact:
            parts.append(f"{self.rows_duplicate_exact} exact duplicate(s) skipped")
        if self.rows_flagged_duplicate:
            parts.append(
                f"{self.rows_flagged_duplicate} probable duplicate(s) held out of totals"
            )
        if self.rows_flagged_review:
            parts.append(f"{self.rows_flagged_review} possible duplicate(s) flagged")
        if self.rows_skipped:
            parts.append(f"{self.rows_skipped} unparseable row(s) skipped")
        return ", ".join(parts) + "."


def import_statement(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    cfg: Config,
    account_name: str = "Main Account",
    bank: str | None = None,
    force: bool = False,
    column_map: csvimport.ColumnMap | None = None,
    profile_name: str | None = None,
    stored_path: str | None = None,
) -> ImportReport:
    """Parse and load one bank statement CSV."""
    path = Path(path)
    account_id = dbmod.get_or_create_account(
        conn, account_name, bank=bank, currency=cfg.currency_code
    )

    file_sha = csvimport.sha256_file(path)
    existing = conn.execute(
        "SELECT id, filename FROM statements WHERE account_id = ? AND file_sha256 = ?",
        (account_id, file_sha),
    ).fetchone()
    if existing and not force:
        dbmod.log_ingest(
            conn, "statement", path.name, "skipped", f"identical to statement {existing['id']}"
        )
        conn.commit()
        return ImportReport(
            statement_id=int(existing["id"]),
            account_id=account_id,
            filename=path.name,
            already_imported=True,
        )

    profile = None
    if profile_name:
        profiles = csvimport.load_profiles()
        profile = profiles.get(profile_name)
        if profile is None:
            raise KeyError(f"unknown bank profile {profile_name!r}")

    parsed: ParseResult = csvimport.parse_statement(
        path, column_map=column_map, profile=profile
    )

    # Coverage from statements imported *before* this one - this is what makes
    # duplicate suspicion apply only inside genuinely overlapping ranges.
    coverage = covered_ranges(conn, account_id)

    cur = conn.execute(
        "INSERT INTO statements(account_id, filename, file_sha256, stored_path, profile, "
        "period_start, period_end, row_count, opening_balance_cents, closing_balance_cents, notes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            account_id,
            path.name,
            file_sha,
            stored_path,
            profile_name or "auto",
            parsed.period_start.isoformat() if parsed.period_start else None,
            parsed.period_end.isoformat() if parsed.period_end else None,
            len(parsed.rows),
            parsed.opening_balance_cents,
            parsed.closing_balance_cents,
            "; ".join(parsed.column_map.detection_notes) or None,
        ),
    )
    statement_id = int(cur.lastrowid)

    classifier = Classifier.from_db(conn)
    occurrences = assign_occurrences(parsed.rows)

    report = ImportReport(
        statement_id=statement_id,
        account_id=account_id,
        filename=path.name,
        rows_total=len(parsed.rows),
        rows_skipped=len(parsed.skipped),
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        warnings=list(parsed.warnings),
        detection_notes=list(parsed.column_map.detection_notes),
        skipped_detail=list(parsed.skipped),
    )

    for row, occurrence in zip(parsed.rows, occurrences):
        fingerprint = row_fingerprint(
            row.txn_date,
            row.amount_cents,
            row.description,
            balance_cents=row.balance_cents,
            occurrence=occurrence,
        )

        cls = classifier.classify(row.description, amount_cents=row.amount_cents)
        merchant_id = dbmod.get_or_create_merchant(
            conn,
            canonical_key(cls.merchant_name) or "unknown",
            cls.merchant_name,
            default_category=cls.category if cls.category_source == "rule" else None,
        )

        is_cash = 1 if (cls.txn_type == "atm" or cls.category == "Cash Withdrawals") else 0
        is_transfer = 1 if cls.category in ("Transfers", "Credit Card Repayment") else 0

        try:
            ins = conn.execute(
                "INSERT INTO transactions(account_id, statement_id, txn_date, posted_date, "
                "description_raw, merchant_id, merchant_norm, amount_cents, balance_cents, "
                "category, category_source, txn_type, is_cash_withdrawal, "
                "is_internal_transfer, fingerprint, row_ordinal) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    statement_id,
                    row.txn_date.isoformat(),
                    row.posted_date.isoformat() if row.posted_date else None,
                    row.description,
                    merchant_id,
                    cls.merchant_name,
                    row.amount_cents,
                    row.balance_cents,
                    cls.category,
                    cls.category_source,
                    cls.txn_type,
                    is_cash,
                    is_transfer,
                    fingerprint,
                    row.row_ordinal,
                ),
            )
        except sqlite3.IntegrityError:
            # Unique (account_id, fingerprint): this exact row is already in.
            report.rows_duplicate_exact += 1
            continue

        txn_id = int(ins.lastrowid)
        report.rows_imported += 1
        if row.amount_cents < 0:
            report.outflow_cents += -row.amount_cents
        else:
            report.inflow_cents += row.amount_cents

        # Suspected (but unproven) duplicates against earlier statements.
        verdicts = find_duplicate_candidates(
            conn,
            account_id=account_id,
            txn_date=row.txn_date,
            amount_cents=row.amount_cents,
            description=row.description,
            balance_cents=row.balance_cents,
            statement_id=statement_id,
            coverage=coverage,
        )
        if verdicts:
            top = verdicts[0]
            for verdict in verdicts:
                record_candidate(conn, txn_id, verdict)
            if top.treat_as_duplicate:
                conn.execute(
                    "UPDATE transactions SET status='duplicate', duplicate_of=? WHERE id=?",
                    (top.existing_id, txn_id),
                )
                report.rows_flagged_duplicate += 1
                report.rows_imported -= 1
                if row.amount_cents < 0:
                    report.outflow_cents -= -row.amount_cents
                else:
                    report.inflow_cents -= row.amount_cents
            else:
                report.rows_flagged_review += 1

    conn.execute(
        "UPDATE statements SET rows_imported=?, rows_duplicate=?, rows_skipped=? WHERE id=?",
        (
            report.rows_imported,
            report.rows_duplicate_exact + report.rows_flagged_duplicate,
            report.rows_skipped,
            statement_id,
        ),
    )
    dbmod.log_ingest(conn, "statement", path.name, "imported", report.summary())
    conn.commit()

    # A new statement can explain receipts that were previously unmatched.
    report.receipt_rematch = rematch_all_receipts(
        conn,
        amount_tolerance_cents=cfg.match_amount_tolerance_cents,
        days_window=cfg.match_days_window,
    )
    conn.commit()
    return report


def reclassify_all(conn: sqlite3.Connection, *, only_unset: bool = True) -> int:
    """Re-run categorisation over stored transactions.

    Never overwrites a category you set yourself (``category_source='user'``).
    With ``only_unset`` false it also refreshes rows categorised by an older
    version of the rule pack.
    """
    classifier = Classifier.from_db(conn)
    where = "category_source IN ('unset','default')" if only_unset else "category_source != 'user'"
    rows = conn.execute(
        f"SELECT id, description_raw, amount_cents FROM transactions WHERE {where}"
    ).fetchall()
    changed = 0
    for row in rows:
        cls = classifier.classify(
            row["description_raw"], amount_cents=int(row["amount_cents"])
        )
        conn.execute(
            "UPDATE transactions SET category=?, category_source=?, merchant_norm=?, "
            "txn_type=COALESCE(?, txn_type), "
            "is_cash_withdrawal=?, is_internal_transfer=? WHERE id=?",
            (
                cls.category,
                cls.category_source,
                cls.merchant_name,
                cls.txn_type,
                1 if (cls.txn_type == "atm" or cls.category == "Cash Withdrawals") else 0,
                1 if cls.category in ("Transfers", "Credit Card Repayment") else 0,
                row["id"],
            ),
        )
        changed += 1
    conn.commit()
    return changed


def set_category(
    conn: sqlite3.Connection,
    txn_id: int,
    category: str,
    *,
    create_rule: bool = True,
) -> None:
    """Record a user's category choice and, optionally, learn from it.

    Creating a user rule from the correction is what stops you re-categorising
    the same merchant every month.
    """
    row = conn.execute(
        "SELECT merchant_norm, description_raw FROM transactions WHERE id=?", (txn_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no transaction {txn_id}")

    conn.execute(
        "UPDATE transactions SET category=?, category_source='user' WHERE id=?",
        (category, txn_id),
    )

    if create_rule and row["merchant_norm"] and row["merchant_norm"] != "Unknown":
        pattern = row["merchant_norm"].lower()
        conn.execute(
            "INSERT INTO rules(priority, field, match_type, pattern, category, "
            "merchant_name, source) VALUES(5, 'merchant', 'contains', ?, ?, ?, 'user') "
            "ON CONFLICT(field, match_type, pattern, source) DO UPDATE SET category=excluded.category",
            (pattern, category, row["merchant_norm"]),
        )
        conn.execute(
            "UPDATE merchants SET default_category=? WHERE canonical=?",
            (category, canonical_key(row["merchant_norm"])),
        )
    conn.commit()
