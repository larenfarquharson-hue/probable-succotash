"""Statement ingestion: parsed CSV rows into deduplicated transactions."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import categorise, csvimport, db, normalise, taxonomy
from .csvimport import ParseResult

# Marks transactions the importer created from a per-line fee column.
FEE_PREFIX = "Bank fee — "


@dataclass
class ImportSummary:
    path: str
    account: str
    rows_seen: int = 0
    inserted: int = 0
    duplicates: int = 0
    fee_rows: int = 0
    import_id: int | None = None
    already_imported: str | None = None    # ISO timestamp of the earlier import
    parse: ParseResult | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def skipped_rows(self) -> int:
        return len(self.parse.skipped) if self.parse else 0


@dataclass
class _Record:
    txn_date: str
    description: str
    amount: float
    balance: float | None
    desc_key: str
    ordinal: int = 0
    # Set for rows the importer synthesised itself, where the rules must not
    # get a say — a fee line carries the merchant's name but is not their spend.
    forced_category: str | None = None


def import_statement(conn: sqlite3.Connection, path: Path, account: str = "main",
                     profile_name: str | None = None, positive_is: str | None = None,
                     dry_run: bool = False,
                     categoriser: categorise.Categoriser | None = None) -> ImportSummary:
    """Import one statement CSV. Re-importing the same data is always safe."""
    profile = csvimport.load_profile(profile_name) if profile_name else None
    parse = csvimport.parse_file(path, profile=profile, positive_is=positive_is)
    summary = ImportSummary(path=str(path), account=account, parse=parse,
                            rows_seen=len(parse.rows))

    file_sha = normalise.file_hash(str(path))
    prior = conn.execute(
        "SELECT imported_at FROM imports WHERE file_sha256 = ? AND kind = 'statement' "
        "ORDER BY id LIMIT 1",
        (file_sha,),
    ).fetchone()
    if prior:
        summary.already_imported = prior["imported_at"]

    records = _expand(parse)
    summary.fee_rows = sum(1 for r in records if r.description.startswith(FEE_PREFIX))
    _assign_ordinals(records)

    if dry_run:
        seen: set[str] = set()
        for rec in records:
            fp = normalise.fingerprint(account, rec.txn_date, rec.amount,
                                       rec.desc_key, rec.ordinal)
            row = conn.execute(
                "SELECT 1 FROM transactions WHERE fingerprint = ?", (fp,)).fetchone()
            if row or fp in seen:
                summary.duplicates += 1
            else:
                summary.inserted += 1
            seen.add(fp)
        return summary

    cat = categoriser or categorise.build()
    acct_id = db.account_id(conn, account)
    cur = conn.execute(
        "INSERT INTO imports(kind, path, file_sha256, profile, imported_at, rows_seen) "
        "VALUES('statement', ?, ?, ?, ?, ?)",
        (str(path), file_sha, profile_name, datetime.now().isoformat(timespec="seconds"),
         len(parse.rows)),
    )
    import_id = int(cur.lastrowid)
    summary.import_id = import_id

    overrides = {
        row["description_key"]: row
        for row in conn.execute("SELECT * FROM overrides")
    }

    for rec in records:
        fingerprint = normalise.fingerprint(account, rec.txn_date, rec.amount,
                                           rec.desc_key, rec.ordinal)
        assignment = cat.classify(rec.description, rec.amount)
        category = assignment.category
        subcategory = assignment.subcategory
        merchant = assignment.merchant
        source = assignment.source
        is_internal = 1 if assignment.is_internal else 0
        excluded = 0

        if rec.forced_category:
            category = rec.forced_category
            subcategory = None
            merchant = "Bank charges"
            source = "rule"

        override = overrides.get(rec.desc_key)
        if override is not None:
            category = override["category"] or category
            subcategory = override["subcategory"] or subcategory
            merchant = override["merchant"] or merchant
            is_internal = int(override["is_internal"])
            excluded = int(override["excluded"])
            source = "manual"

        if taxonomy.get(category).kind == "transfer":
            is_internal = 1

        inserted = conn.execute(
            "INSERT OR IGNORE INTO transactions("
            " account_id, txn_date, description, description_key, amount, balance,"
            " fingerprint, import_id, category, subcategory, merchant, rule_id,"
            " category_source, is_internal, excluded)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (acct_id, rec.txn_date, rec.description, rec.desc_key, rec.amount,
             rec.balance, fingerprint, import_id, category, subcategory, merchant,
             assignment.rule_id, source, is_internal, excluded),
        ).rowcount
        if inserted:
            summary.inserted += 1
        else:
            summary.duplicates += 1

    conn.execute(
        "UPDATE imports SET rows_new = ?, rows_dupe = ? WHERE id = ?",
        (summary.inserted, summary.duplicates, import_id),
    )
    conn.commit()

    if summary.already_imported and summary.inserted == 0:
        summary.warnings.append(
            f"This exact file was already imported on {summary.already_imported}; "
            "nothing new was added."
        )
    elif summary.duplicates:
        summary.warnings.append(
            f"{summary.duplicates} row(s) already existed and were not counted again."
        )
    return summary


def _expand(parse: ParseResult) -> list[_Record]:
    """Turn parsed rows into records, splitting per-transaction fees out.

    Several banks put a fee in its own column on the same line as the purchase.
    That fee is a separate outflow and has to be counted, or the period will not
    reconcile against the closing balance.
    """
    records: list[_Record] = []
    for row in parse.rows:
        iso = row.txn_date.isoformat()
        records.append(_Record(
            txn_date=iso,
            description=row.description,
            amount=row.amount,
            balance=row.balance,
            desc_key=normalise.description_key(row.description),
        ))
        if row.fee is not None:
            label = f"{FEE_PREFIX}{normalise.clean_description(row.description)}"[:180]
            records.append(_Record(
                txn_date=iso,
                description=label,
                amount=row.fee,
                balance=None,
                desc_key=normalise.description_key(label),
                forced_category="Bank Charges & Fees",
            ))
    return records


def _assign_ordinals(records: list[_Record]) -> None:
    """Number identical same-day transactions so genuine repeats both survive.

    Two identical R25 coffees on the same day are two transactions, not one, so
    the fingerprint needs a tiebreaker. Numbering happens within the file being
    imported, which is stable across exports because a statement covers whole
    days: any export containing that day contains both coffees, in the same
    order. The consequence is intentional — re-importing the same day always
    produces the same fingerprints and therefore never double counts.
    """
    counts: dict[tuple[str, float, str], int] = defaultdict(int)
    for rec in records:
        key = (rec.txn_date, round(rec.amount, 2), rec.desc_key)
        rec.ordinal = counts[key]
        counts[key] += 1


def recategorise(conn: sqlite3.Connection,
                 categoriser: categorise.Categoriser | None = None,
                 only_uncategorised: bool = False) -> int:
    """Re-run rules over stored transactions. Manual overrides are preserved."""
    cat = categoriser or categorise.build()
    overrides = {
        row["description_key"]: row
        for row in conn.execute("SELECT * FROM overrides")
    }
    query = "SELECT id, description, description_key, amount, category_source FROM transactions"
    if only_uncategorised:
        query += f" WHERE category = '{taxonomy.UNCATEGORISED}'"
    changed = 0
    for row in conn.execute(query).fetchall():
        override = overrides.get(row["description_key"])
        if override is not None:
            conn.execute(
                "UPDATE transactions SET category = COALESCE(?, category),"
                " subcategory = ?, merchant = COALESCE(?, merchant),"
                " is_internal = ?, excluded = ?, category_source = 'manual'"
                " WHERE id = ?",
                (override["category"], override["subcategory"], override["merchant"],
                 int(override["is_internal"]), int(override["excluded"]), row["id"]),
            )
            changed += 1
            continue
        if row["category_source"] == "slip":
            continue    # a slip is better evidence than a rule
        if row["description"].startswith(FEE_PREFIX):
            conn.execute(
                "UPDATE transactions SET category = 'Bank Charges & Fees',"
                " subcategory = NULL, merchant = 'Bank charges', rule_id = 'bank-fee',"
                " category_source = 'rule' WHERE id = ?", (row["id"],))
            changed += 1
            continue
        assignment = cat.classify(row["description"], row["amount"])
        conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ?, merchant = ?,"
            " rule_id = ?, category_source = ?, is_internal = ? WHERE id = ?",
            (assignment.category, assignment.subcategory, assignment.merchant,
             assignment.rule_id, assignment.source,
             1 if assignment.is_internal else 0, row["id"]),
        )
        changed += 1
    conn.commit()
    return changed


def set_override(conn: sqlite3.Connection, description_key: str,
                 category: str | None = None, merchant: str | None = None,
                 subcategory: str | None = None, internal: bool = False,
                 excluded: bool = False) -> int:
    """Record a manual decision and apply it to every matching transaction."""
    conn.execute(
        "INSERT INTO overrides(description_key, category, subcategory, merchant,"
        " is_internal, excluded) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(description_key) DO UPDATE SET"
        " category = excluded.category, subcategory = excluded.subcategory,"
        " merchant = excluded.merchant, is_internal = excluded.is_internal,"
        " excluded = excluded.excluded",
        (description_key, category, subcategory, merchant,
         1 if internal else 0, 1 if excluded else 0),
    )
    cur = conn.execute(
        "UPDATE transactions SET category = COALESCE(?, category),"
        " subcategory = COALESCE(?, subcategory), merchant = COALESCE(?, merchant),"
        " is_internal = ?, excluded = ?, category_source = 'manual'"
        " WHERE description_key = ?",
        (category, subcategory, merchant, 1 if internal else 0,
         1 if excluded else 0, description_key),
    )
    conn.commit()
    return cur.rowcount


def undo_import(conn: sqlite3.Connection, import_id: int) -> int:
    """Remove everything a single import added. Useful after a bad mapping."""
    conn.execute(
        "UPDATE slips SET status = 'unmatched', matched_txn_id = NULL,"
        " match_score = NULL, match_reason = NULL"
        " WHERE matched_txn_id IN (SELECT id FROM transactions WHERE import_id = ?)",
        (import_id,),
    )
    cur = conn.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,))
    conn.execute("DELETE FROM slip_items WHERE slip_id IN "
                 "(SELECT id FROM slips WHERE import_id = ?)", (import_id,))
    conn.execute("DELETE FROM slips WHERE import_id = ?", (import_id,))
    conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    conn.commit()
    return cur.rowcount
