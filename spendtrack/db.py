"""SQLite storage: schema, migrations and connection handling."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    bank     TEXT,
    masked   TEXT
);

-- One row per file ingested, so an import can be traced or undone.
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- 'statement' | 'slip'
    path        TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    profile     TEXT,
    imported_at TEXT NOT NULL,
    rows_seen   INTEGER NOT NULL DEFAULT 0,
    rows_new    INTEGER NOT NULL DEFAULT 0,
    rows_dupe   INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

-- The authoritative record of money movement. Nothing else contributes to a
-- period total. amount < 0 is an outflow, amount > 0 an inflow.
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    txn_date        TEXT NOT NULL,      -- ISO yyyy-mm-dd
    description     TEXT NOT NULL,      -- as printed by the bank
    description_key TEXT NOT NULL,      -- normalised, for matching
    amount          REAL NOT NULL,
    balance         REAL,
    fingerprint     TEXT NOT NULL UNIQUE,
    import_id       INTEGER REFERENCES imports(id),
    category        TEXT,
    subcategory     TEXT,
    merchant        TEXT,
    rule_id         TEXT,               -- which rule assigned the category
    category_source TEXT,               -- 'rule' | 'slip' | 'manual' | 'fallback'
    is_internal     INTEGER NOT NULL DEFAULT 0,  -- transfer between own accounts
    excluded        INTEGER NOT NULL DEFAULT 0,  -- user chose to ignore
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_txn_cat  ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_txn_key  ON transactions(description_key);

-- A till slip is evidence about a transaction, not a transaction.
CREATE TABLE IF NOT EXISTS slips (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_date      TEXT,
    slip_time      TEXT,
    merchant       TEXT,
    merchant_key   TEXT,
    total          REAL,
    tax            REAL,
    payment_method TEXT,                -- 'card' | 'cash' | 'eft' | 'unknown'
    card_last4     TEXT,
    image_path     TEXT,
    content_sha256 TEXT NOT NULL UNIQUE,
    raw_text       TEXT,
    source         TEXT,                -- 'json' | 'ocr' | 'manual'
    -- matched: same money as a statement line. cash_allocation: breaks down an
    -- ATM withdrawal. unmatched: no statement line found yet.
    status         TEXT NOT NULL DEFAULT 'unmatched',
    matched_txn_id INTEGER REFERENCES transactions(id),
    match_score    REAL,
    match_reason   TEXT,
    category       TEXT,
    import_id      INTEGER REFERENCES imports(id),
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_slip_date ON slips(slip_date);

CREATE TABLE IF NOT EXISTS slip_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_id     INTEGER NOT NULL REFERENCES slips(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    qty         REAL,
    unit_price  REAL,
    line_total  REAL,
    category    TEXT
);

CREATE INDEX IF NOT EXISTS idx_item_slip ON slip_items(slip_id);

-- Manual category overrides keyed on the normalised description, so a decision
-- made once survives future imports.
CREATE TABLE IF NOT EXISTS overrides (
    description_key TEXT PRIMARY KEY,
    category        TEXT,
    subcategory     TEXT,
    merchant        TEXT,
    is_internal     INTEGER NOT NULL DEFAULT 0,
    excluded        INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating and migrating it if needed."""
    target = path or config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return
    found = int(row["value"])
    if found > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{found} is newer than this build (v{SCHEMA_VERSION}). "
            "Upgrade SpendTrack."
        )
    # No backward migrations needed yet; future versions add them here.


def account_id(conn: sqlite3.Connection, name: str, bank: str | None = None,
               masked: str | None = None) -> int:
    """Fetch or create an account by name."""
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        if bank or masked:
            conn.execute(
                "UPDATE accounts SET bank = COALESCE(?, bank), "
                "masked = COALESCE(?, masked) WHERE id = ?",
                (bank, masked, row["id"]),
            )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO accounts(name, bank, masked) VALUES(?, ?, ?)",
        (name, bank, masked),
    )
    return int(cur.lastrowid)
