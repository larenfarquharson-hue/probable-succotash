"""SQLite storage layer.

Design notes
------------
* ``transactions`` is the single authoritative ledger of money moving on the
  account. It is populated *only* from bank statements. Nothing else may add
  rows to it, which is what makes the totals trustworthy.
* ``receipts`` (till slips) are evidence *about* spend. A receipt never adds
  to the outflow total on its own. It either:
    - matches a bank transaction, and then enriches it (real merchant name,
      line items, better category); or
    - was paid in cash, and then it is allocated against an ATM/cash
      withdrawal that the bank already counted; or
    - is genuinely unexplained, and then it sits in a review queue where you
      decide - it is reported separately and never silently added.
* Amounts are integer cents. Outflows are stored negative, inflows positive.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    bank           TEXT,
    account_masked TEXT,
    currency       TEXT NOT NULL DEFAULT 'ZAR',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per imported CSV file.
CREATE TABLE IF NOT EXISTS statements (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    filename              TEXT NOT NULL,
    file_sha256           TEXT NOT NULL,
    stored_path           TEXT,
    profile               TEXT,
    period_start          TEXT,
    period_end            TEXT,
    row_count             INTEGER NOT NULL DEFAULT 0,
    rows_imported         INTEGER NOT NULL DEFAULT 0,
    rows_duplicate        INTEGER NOT NULL DEFAULT 0,
    rows_skipped          INTEGER NOT NULL DEFAULT 0,
    opening_balance_cents INTEGER,
    closing_balance_cents INTEGER,
    imported_at           TEXT NOT NULL DEFAULT (datetime('now')),
    notes                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_statements_sha ON statements(file_sha256);

CREATE TABLE IF NOT EXISTS merchants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical        TEXT NOT NULL UNIQUE,   -- lowercase matching key
    display_name     TEXT NOT NULL,
    default_category TEXT,
    is_frivolous     INTEGER,                -- NULL = inherit from category
    notes            TEXT
);

-- The authoritative ledger. Bank statements only.
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    statement_id      INTEGER REFERENCES statements(id) ON DELETE SET NULL,
    txn_date          TEXT NOT NULL,          -- ISO date, the value/effective date
    posted_date       TEXT,
    description_raw   TEXT NOT NULL,
    merchant_id       INTEGER REFERENCES merchants(id) ON DELETE SET NULL,
    merchant_norm     TEXT,                   -- cleaned merchant name
    amount_cents      INTEGER NOT NULL,       -- negative = outflow
    balance_cents     INTEGER,
    category          TEXT,
    category_source   TEXT NOT NULL DEFAULT 'unset',  -- unset|rule|merchant|user|receipt
    txn_type          TEXT,                   -- card|debit_order|eft|atm|fee|interest|transfer|other
    is_cash_withdrawal INTEGER NOT NULL DEFAULT 0,
    is_internal_transfer INTEGER NOT NULL DEFAULT 0,
    fingerprint       TEXT NOT NULL,
    row_ordinal       INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active',  -- active|duplicate|ignored
    duplicate_of      INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    user_note         TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_fingerprint
    ON transactions(account_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions(merchant_norm);
CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);

-- Suspected duplicates that need a human decision. Nothing is dropped
-- silently; a row lands here and stays visible until resolved.
CREATE TABLE IF NOT EXISTS duplicate_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id         INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    existing_id    INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    score          REAL NOT NULL,
    reason         TEXT NOT NULL,
    resolution     TEXT NOT NULL DEFAULT 'pending',  -- pending|duplicate|distinct
    resolved_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(txn_id, existing_id)
);
CREATE INDEX IF NOT EXISTS idx_dupcand_res ON duplicate_candidates(resolution);

-- Till slips.
CREATE TABLE IF NOT EXISTS receipts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    original_filename   TEXT,
    stored_path         TEXT,
    image_sha256        TEXT,
    receipt_date        TEXT,
    receipt_time        TEXT,
    merchant_raw        TEXT,
    merchant_norm       TEXT,
    merchant_id         INTEGER REFERENCES merchants(id) ON DELETE SET NULL,
    total_cents         INTEGER,
    vat_cents           INTEGER,
    tender_type         TEXT NOT NULL DEFAULT 'unknown',  -- card|cash|eft|voucher|unknown
    card_last4          TEXT,
    category            TEXT,
    extractor           TEXT,
    confidence          REAL,
    raw_text            TEXT,
    raw_json            TEXT,
    -- How this receipt relates to the ledger:
    --   matched        -> linked to a bank transaction (no extra outflow)
    --   cash_allocated -> spent from a withdrawal the bank already counted
    --   unmatched      -> no explanation found yet; excluded from totals,
    --                     surfaced in the reconciliation queue
    --   ignored        -> user dismissed it
    link_status         TEXT NOT NULL DEFAULT 'unmatched',
    transaction_id      INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    match_score         REAL,
    match_reason        TEXT,
    counts_as_outflow   INTEGER NOT NULL DEFAULT 0,
    user_note           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_sha ON receipts(image_sha256)
    WHERE image_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_receipt_link ON receipts(link_status);
CREATE INDEX IF NOT EXISTS idx_receipt_date ON receipts(receipt_date);
CREATE INDEX IF NOT EXISTS idx_receipt_txn ON receipts(transaction_id);

CREATE TABLE IF NOT EXISTS receipt_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id       INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL DEFAULT 0,
    description      TEXT NOT NULL,
    quantity         REAL,
    unit_price_cents INTEGER,
    line_total_cents INTEGER,
    category         TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_receipt ON receipt_items(receipt_id);

-- Cash receipts consume part of a withdrawal the bank already recorded.
CREATE TABLE IF NOT EXISTS cash_allocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id    INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    withdrawal_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    amount_cents  INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(receipt_id, withdrawal_id)
);

CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    priority      INTEGER NOT NULL DEFAULT 100,
    field         TEXT NOT NULL DEFAULT 'description',  -- description|merchant
    match_type    TEXT NOT NULL DEFAULT 'contains',     -- contains|regex|exact|startswith
    pattern       TEXT NOT NULL,
    category      TEXT,
    merchant_name TEXT,
    txn_type      TEXT,
    is_frivolous  INTEGER,
    enabled       INTEGER NOT NULL DEFAULT 1,
    source        TEXT NOT NULL DEFAULT 'default',      -- default|user
    hits          INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(field, match_type, pattern, source)
);
CREATE INDEX IF NOT EXISTS idx_rules_priority ON rules(priority);

CREATE TABLE IF NOT EXISTS categories (
    name         TEXT PRIMARY KEY,
    parent       TEXT,
    kind         TEXT NOT NULL DEFAULT 'discretionary',  -- essential|discretionary|excluded
    frivolity    INTEGER NOT NULL DEFAULT 0,  -- 0..3 baseline frivolity score
    colour       TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS budgets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,
    period       TEXT NOT NULL DEFAULT 'month',
    limit_cents  INTEGER NOT NULL,
    UNIQUE(category, period)
);

-- Audit trail of ingestion runs, so an interrupted session can be understood.
CREATE TABLE IF NOT EXISTS ingest_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,     -- statement|receipt
    ref        TEXT,
    outcome    TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if needed and record the version."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by an earlier version."""
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row else 0
    if current == 0 or current >= SCHEMA_VERSION:
        return
    # Additive-only: every column added since v1 is declared in SCHEMA with a
    # default, so re-running executescript above is enough for tables. Columns
    # added to pre-existing tables are handled here.
    existing = {
        table: {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}
        for table in ("transactions", "receipts", "statements")
    }
    additions = [
        ("transactions", "is_internal_transfer", "INTEGER NOT NULL DEFAULT 0"),
        ("receipts", "vat_cents", "INTEGER"),
        ("receipts", "receipt_time", "TEXT"),
        ("statements", "notes", "TEXT"),
    ]
    for table, column, decl in additions:
        if column not in existing.get(table, set()):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context manager yielding an initialised connection."""
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_account(
    conn: sqlite3.Connection,
    name: str,
    *,
    bank: str | None = None,
    account_masked: str | None = None,
    currency: str = "ZAR",
) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO accounts(name, bank, account_masked, currency) VALUES(?,?,?,?)",
        (name, bank, account_masked, currency),
    )
    return int(cur.lastrowid)


def get_or_create_merchant(
    conn: sqlite3.Connection,
    canonical: str,
    display_name: str,
    *,
    default_category: str | None = None,
) -> int:
    canonical = canonical.strip().lower()
    row = conn.execute(
        "SELECT id, default_category FROM merchants WHERE canonical = ?", (canonical,)
    ).fetchone()
    if row:
        if default_category and not row["default_category"]:
            conn.execute(
                "UPDATE merchants SET default_category = ? WHERE id = ?",
                (default_category, row["id"]),
            )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO merchants(canonical, display_name, default_category) VALUES(?,?,?)",
        (canonical, display_name, default_category),
    )
    return int(cur.lastrowid)


def log_ingest(
    conn: sqlite3.Connection, kind: str, ref: str | None, outcome: str, detail: str = ""
) -> None:
    conn.execute(
        "INSERT INTO ingest_log(kind, ref, outcome, detail) VALUES(?,?,?,?)",
        (kind, ref, outcome, detail),
    )
