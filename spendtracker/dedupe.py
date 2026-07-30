"""Deduplication: the part that decides what is and is not the same money.

Three distinct problems, deliberately solved differently.

1. The same statement row imported twice
   ------------------------------------
   Re-exporting an overlapping date range is the normal way people use bank
   portals, so overlapping imports must be safe. Each row gets a fingerprint
   that is *stable* across re-exports but *distinguishes* two genuinely
   separate purchases of the same thing on the same day:

     - When the export has a running balance, the balance is part of the
       fingerprint. Two identical purchases leave different balances behind,
       so this is effectively exact.
     - Without a balance, the fingerprint includes an occurrence index counted
       per (date, amount, description). Re-importing the same range reproduces
       the same indices, so it deduplicates; two real coffees on the same day
       get index 0 and 1 and both survive.

   A unique index on (account, fingerprint) makes the database itself the
   enforcement point, so a crash mid-import cannot leave a partial double.

2. Rows that *might* be the same, where the data cannot prove it
   ------------------------------------------------------------
   Only ever an issue inside a date range two statements both cover. Outside
   prior coverage, a repeated (date, amount, merchant) is simply a repeat
   purchase and is left alone - flagging those was the main false-positive risk.
   Inside overlapping coverage, an unproven match is recorded in
   ``duplicate_candidates`` and, by default, excluded from totals so the answer
   errs toward not double counting. Nothing is deleted and nothing is hidden:
   the review queue shows every one with its amount so you can reinstate it.

3. Till slips versus bank rows
   ---------------------------
   A till slip is not a second outflow. It is either the *detail* of a bank
   row (card purchase), or it is cash spent out of a withdrawal the bank
   already counted. See :func:`match_receipt`.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

from .categorise import canonical_key, normalise_merchant

# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

_FP_NOISE = re.compile(r"[^a-z0-9]")


def fingerprint_description(description: str) -> str:
    """Aggressively normalised description used only for identity comparison.

    Uses the *cleaned merchant* rather than the raw string so that the same
    purchase exported with slightly different narration still matches.
    """
    merchant = normalise_merchant(description or "")
    return _FP_NOISE.sub("", merchant.lower()) or _FP_NOISE.sub("", (description or "").lower())


def soft_key(txn_date: date | str, amount_cents: int, description: str) -> str:
    """Identity of a row ignoring anything that could legitimately differ."""
    d = txn_date.isoformat() if isinstance(txn_date, date) else str(txn_date)
    return f"{d}|{amount_cents}|{fingerprint_description(description)}"


def row_fingerprint(
    txn_date: date | str,
    amount_cents: int,
    description: str,
    *,
    balance_cents: int | None = None,
    occurrence: int = 0,
) -> str:
    """Stable identity hash for a statement row.

    ``balance_cents``, when present, makes this near-exact. ``occurrence`` is
    the 0-based index of this row among identical rows on the same date, and is
    what keeps two real same-day same-amount purchases apart when no balance is
    available.
    """
    base = soft_key(txn_date, amount_cents, description)
    if balance_cents is not None:
        payload = f"{base}|b{balance_cents}"
    else:
        payload = f"{base}|o{occurrence}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def assign_occurrences(rows: Sequence["_HasIdentity"]) -> list[int]:
    """Occurrence index per row, counted within (date, amount, description)."""
    seen: dict[str, int] = defaultdict(int)
    out: list[int] = []
    for row in rows:
        key = soft_key(row.txn_date, row.amount_cents, row.description)
        out.append(seen[key])
        seen[key] += 1
    return out


class _HasIdentity:  # pragma: no cover - typing helper only
    txn_date: date
    amount_cents: int
    description: str


# ---------------------------------------------------------------------------
# Coverage: which date ranges have already been imported
# ---------------------------------------------------------------------------


def covered_ranges(conn: sqlite3.Connection, account_id: int) -> list[tuple[date, date]]:
    """Date ranges already covered by previously imported statements."""
    ranges: list[tuple[date, date]] = []
    for row in conn.execute(
        "SELECT period_start, period_end FROM statements "
        "WHERE account_id = ? AND period_start IS NOT NULL AND period_end IS NOT NULL",
        (account_id,),
    ):
        try:
            start = date.fromisoformat(row["period_start"])
            end = date.fromisoformat(row["period_end"])
        except (TypeError, ValueError):
            continue
        ranges.append((start, end))
    return ranges


def in_covered_range(d: date, ranges: Iterable[tuple[date, date]]) -> bool:
    return any(start <= d <= end for start, end in ranges)


# ---------------------------------------------------------------------------
# Suspected-duplicate scoring
# ---------------------------------------------------------------------------

# Above this, the row is excluded from totals pending review (avoid double
# counting). Between REVIEW and DUPLICATE it stays counted but is flagged.
DUPLICATE_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.55


@dataclass
class DuplicateVerdict:
    existing_id: int
    score: float
    reason: str

    @property
    def treat_as_duplicate(self) -> bool:
        return self.score >= DUPLICATE_THRESHOLD


def score_duplicate(
    *,
    new_date: date,
    new_amount: int,
    new_desc: str,
    new_balance: int | None,
    existing_date: date,
    existing_amount: int,
    existing_desc: str,
    existing_balance: int | None,
    same_statement: bool,
) -> tuple[float, str]:
    """Likelihood that two rows are the same money, with a reason string."""
    if new_amount != existing_amount:
        return 0.0, "different amount"

    # Two rows from the *same* import file are, by construction, distinct rows
    # of the statement - the bank listed them separately.
    if same_statement:
        return 0.0, "separate lines of the same statement"

    # A differing running balance is proof they are different rows.
    if new_balance is not None and existing_balance is not None:
        if new_balance != existing_balance:
            return 0.0, "running balance differs, so these are distinct rows"

    desc_match = fingerprint_description(new_desc) == fingerprint_description(existing_desc)
    day_gap = abs((new_date - existing_date).days)

    if not desc_match:
        # Same amount, different merchant: coincidence, not a duplicate.
        return 0.0, "different merchant"

    if day_gap == 0:
        if new_balance is not None and existing_balance is not None:
            score, reason = 0.97, "same date, amount, merchant and running balance"
        else:
            score, reason = 0.93, "same date, amount and merchant in an overlapping period"
    elif day_gap == 1:
        score, reason = 0.72, "same amount and merchant one day apart in an overlapping period"
    elif day_gap <= 3:
        score, reason = 0.58, f"same amount and merchant {day_gap} days apart in an overlapping period"
    else:
        return 0.0, "too far apart in time"

    return score, reason


def find_duplicate_candidates(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    txn_date: date,
    amount_cents: int,
    description: str,
    balance_cents: int | None,
    statement_id: int | None,
    coverage: Sequence[tuple[date, date]],
    window_days: int = 3,
) -> list[DuplicateVerdict]:
    """Suspected duplicates of a row about to be inserted.

    Returns an empty list unless the row's date falls inside a range a
    *previous* statement already covered - outside prior coverage there is no
    reason to suspect a duplicate, only a repeat purchase.
    """
    if not in_covered_range(txn_date, coverage):
        return []

    lo = (txn_date - timedelta(days=window_days)).isoformat()
    hi = (txn_date + timedelta(days=window_days)).isoformat()

    verdicts: list[DuplicateVerdict] = []
    for row in conn.execute(
        "SELECT id, txn_date, amount_cents, description_raw, balance_cents, statement_id "
        "FROM transactions "
        "WHERE account_id = ? AND amount_cents = ? AND txn_date BETWEEN ? AND ? "
        "AND status != 'duplicate'",
        (account_id, amount_cents, lo, hi),
    ):
        try:
            existing_date = date.fromisoformat(row["txn_date"])
        except (TypeError, ValueError):
            continue
        score, reason = score_duplicate(
            new_date=txn_date,
            new_amount=amount_cents,
            new_desc=description,
            new_balance=balance_cents,
            existing_date=existing_date,
            existing_amount=int(row["amount_cents"]),
            existing_desc=row["description_raw"],
            existing_balance=row["balance_cents"],
            same_statement=(
                statement_id is not None and row["statement_id"] == statement_id
            ),
        )
        if score >= REVIEW_THRESHOLD:
            verdicts.append(DuplicateVerdict(int(row["id"]), score, reason))

    verdicts.sort(key=lambda v: v.score, reverse=True)
    return verdicts


def record_candidate(
    conn: sqlite3.Connection, txn_id: int, verdict: DuplicateVerdict
) -> None:
    conn.execute(
        "INSERT INTO duplicate_candidates(txn_id, existing_id, score, reason) "
        "VALUES(?,?,?,?) ON CONFLICT(txn_id, existing_id) DO NOTHING",
        (txn_id, verdict.existing_id, verdict.score, verdict.reason),
    )


def resolve_candidate(conn: sqlite3.Connection, candidate_id: int, resolution: str) -> None:
    """Apply a human decision. ``resolution`` is 'duplicate' or 'distinct'."""
    if resolution not in ("duplicate", "distinct"):
        raise ValueError("resolution must be 'duplicate' or 'distinct'")
    row = conn.execute(
        "SELECT txn_id, existing_id FROM duplicate_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no duplicate candidate {candidate_id}")

    if resolution == "duplicate":
        conn.execute(
            "UPDATE transactions SET status='duplicate', duplicate_of=? WHERE id=?",
            (row["existing_id"], row["txn_id"]),
        )
    else:
        conn.execute(
            "UPDATE transactions SET status='active', duplicate_of=NULL WHERE id=?",
            (row["txn_id"],),
        )
    conn.execute(
        "UPDATE duplicate_candidates SET resolution=?, resolved_at=datetime('now') WHERE id=?",
        (resolution, candidate_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Receipt matching
# ---------------------------------------------------------------------------


@dataclass
class ReceiptMatch:
    transaction_id: int | None
    score: float
    reason: str
    # 'matched' | 'cash_allocated' | 'unmatched'
    link_status: str = "unmatched"
    allocations: list[tuple[int, int]] | None = None  # (withdrawal_txn_id, cents)


MATCH_THRESHOLD = 0.62


def _merchant_similarity(a: str, b: str) -> float:
    """0..1 similarity between two merchant names."""
    ka, kb = canonical_key(a or ""), canonical_key(b or "")
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    if ka in kb or kb in ka:
        return 0.85

    ta = {t for t in re.split(r"[^a-z0-9]+", (a or "").lower()) if len(t) > 2}
    tb = {t for t in re.split(r"[^a-z0-9]+", (b or "").lower()) if len(t) > 2}
    if ta and tb:
        overlap = len(ta & tb) / len(ta | tb)
        if overlap:
            return min(0.8, 0.3 + overlap * 0.6)

    # Character trigram overlap catches "Woolworths" vs "Woolwrths Fd".
    def trigrams(s: str) -> set[str]:
        return {s[i : i + 3] for i in range(max(0, len(s) - 2))}

    ga, gb = trigrams(ka), trigrams(kb)
    if not ga or not gb:
        return 0.0
    return min(0.75, len(ga & gb) / len(ga | gb))


def score_receipt_match(
    *,
    receipt_total_cents: int,
    receipt_date: date | None,
    receipt_merchant: str,
    receipt_last4: str | None,
    txn_amount_cents: int,
    txn_date: date,
    txn_merchant: str,
    txn_last4: str | None,
    amount_tolerance_cents: int,
    days_window: int,
) -> tuple[float, str]:
    """Score one receipt against one candidate bank transaction."""
    outflow = -txn_amount_cents  # positive magnitude of money out
    if outflow <= 0:
        return 0.0, "transaction is not an outflow"

    diff = abs(outflow - receipt_total_cents)
    if diff > max(amount_tolerance_cents, int(receipt_total_cents * 0.05)):
        return 0.0, "amount too different"

    if diff == 0:
        amount_score, amount_reason = 0.55, "exact amount"
    elif diff <= 100:
        amount_score, amount_reason = 0.45, f"amount within {diff}c"
    else:
        amount_score, amount_reason = 0.30, f"amount within {diff / 100:.2f}"

    if receipt_date is None:
        date_score, date_reason = 0.05, "no receipt date"
    else:
        # Card purchases usually post on or after the purchase date.
        gap = (txn_date - receipt_date).days
        if gap == 0:
            date_score, date_reason = 0.30, "same day"
        elif 0 < gap <= days_window:
            date_score, date_reason = 0.24 - 0.03 * gap, f"posted {gap} day(s) later"
        elif -2 <= gap < 0:
            date_score, date_reason = 0.12, f"posted {abs(gap)} day(s) earlier"
        else:
            return 0.0, "dates too far apart"

    msim = _merchant_similarity(receipt_merchant, txn_merchant)

    # When both sides name a merchant and the names do not resemble each other,
    # refuse outright rather than letting a strong amount-and-date score carry it.
    # An exact amount on the exact day scores 0.85 on its own, which is enough to
    # attach a slip to a stranger's purchase that happened to cost the same - and
    # the resulting "detail" would be confidently wrong.
    both_named = bool(canonical_key(receipt_merchant)) and bool(canonical_key(txn_merchant))
    if both_named and msim < 0.25:
        return 0.0, "different merchant"

    merchant_score = msim * 0.30
    if msim >= 0.85:
        merchant_reason = "merchant matches"
    elif msim >= 0.4:
        merchant_reason = "merchant similar"
    else:
        merchant_reason = "merchant not legible on the slip"

    card_score = 0.0
    card_reason = ""
    if receipt_last4 and txn_last4 and receipt_last4 == txn_last4:
        card_score, card_reason = 0.15, "same card"

    total = min(1.0, amount_score + date_score + merchant_score + card_score)
    reason = ", ".join(p for p in (amount_reason, date_reason, merchant_reason, card_reason) if p)
    return total, reason


def match_receipt(
    conn: sqlite3.Connection,
    receipt_id: int,
    *,
    amount_tolerance_cents: int = 100,
    days_window: int = 4,
    cash_lookback_days: int = 21,
) -> ReceiptMatch:
    """Decide how a receipt relates to the bank ledger.

    Never creates an outflow. The outcomes are:
      matched        - linked to a bank transaction; enriches it
      cash_allocated - paid in cash out of a withdrawal already counted
      unmatched      - unexplained; reported separately for review
    """
    r = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if r is None:
        raise LookupError(f"no receipt {receipt_id}")
    if r["total_cents"] is None:
        return _apply_match(
            conn, receipt_id, ReceiptMatch(None, 0.0, "no total could be read from the slip")
        )

    total = int(r["total_cents"])
    rdate = date.fromisoformat(r["receipt_date"]) if r["receipt_date"] else None
    merchant = r["merchant_norm"] or r["merchant_raw"] or ""
    account_id = r["account_id"]
    tender = (r["tender_type"] or "unknown").lower()

    # The slip's own tender type decides which explanation to try first, and
    # that ordering is load-bearing. A slip that says "cash tendered / change
    # due" was not paid by card, so linking it to a same-amount card row would
    # attach it to somebody else's purchase - plausible-looking and wrong. Only
    # if no cash withdrawal can account for it do we fall back to card
    # matching, on the assumption the tender type was misread.
    if tender == "cash":
        cash_result = _try_cash(
            conn, r, receipt_id, total, rdate, account_id, cash_lookback_days
        )
        if cash_result is not None:
            return _apply_match(conn, receipt_id, cash_result)

    # --- Try to link to a card / EFT transaction ---------------------------
    best: tuple[float, str, int] | None = None
    if rdate is not None:
        lo = (rdate - timedelta(days=3)).isoformat()
        hi = (rdate + timedelta(days=days_window + 1)).isoformat()
        date_clause = "AND t.txn_date BETWEEN ? AND ?"
        params: list = [total + max(amount_tolerance_cents, int(total * 0.05)), lo, hi]
    else:
        date_clause = ""
        params = [total + max(amount_tolerance_cents, int(total * 0.05))]

    sql = f"""
        SELECT t.id, t.txn_date, t.amount_cents, t.merchant_norm, t.description_raw
        FROM transactions t
        WHERE t.status = 'active'
          AND t.amount_cents < 0
          AND t.is_cash_withdrawal = 0
          AND -t.amount_cents <= ?
          {date_clause}
          AND NOT EXISTS (
                SELECT 1 FROM receipts r2
                WHERE r2.transaction_id = t.id AND r2.id != {int(receipt_id)}
                  AND r2.link_status = 'matched'
          )
    """
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)

    last4 = r["card_last4"]
    for t in conn.execute(sql, params).fetchall():
        score, reason = score_receipt_match(
            receipt_total_cents=total,
            receipt_date=rdate,
            receipt_merchant=merchant,
            receipt_last4=last4,
            txn_amount_cents=int(t["amount_cents"]),
            txn_date=date.fromisoformat(t["txn_date"]),
            txn_merchant=t["merchant_norm"] or t["description_raw"] or "",
            txn_last4=None,
            amount_tolerance_cents=amount_tolerance_cents,
            days_window=days_window,
        )
        if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
            best = (score, reason, int(t["id"]))

    if best is not None:
        score, reason, txn_id = best
        if tender == "cash":
            reason = (
                f"{reason}; note the slip says cash but no cash withdrawal could account "
                "for it, so it was linked to this card row instead - check this is right"
            )
        return _apply_match(
            conn, receipt_id, ReceiptMatch(txn_id, score, reason, link_status="matched")
        )

    # --- Cash: allocate against withdrawals the bank already counted -------
    if tender in ("unknown", "voucher"):
        cash_result = _try_cash(
            conn, r, receipt_id, total, rdate, account_id, cash_lookback_days
        )
        if cash_result is not None:
            return _apply_match(conn, receipt_id, cash_result)

    # --- Genuinely unexplained --------------------------------------------
    reason = (
        "no bank transaction matches this slip. It may be on a statement you have not "
        "imported yet, paid by someone else, or paid in cash that was never withdrawn "
        "from this account."
    )
    return _apply_match(conn, receipt_id, ReceiptMatch(None, 0.0, reason))


def _try_cash(
    conn: sqlite3.Connection,
    receipt_row: sqlite3.Row,
    receipt_id: int,
    total: int,
    rdate: date | None,
    account_id: int | None,
    lookback_days: int,
) -> ReceiptMatch | None:
    """Attempt to account for a slip out of prior cash withdrawals.

    Returns None when nothing could be allocated, so the caller can try another
    explanation.
    """
    if rdate is None:
        return None
    allocations = allocate_cash(
        conn,
        receipt_id=receipt_id,
        amount_cents=total,
        on_or_before=rdate,
        lookback_days=lookback_days,
        account_id=account_id,
    )
    allocated = sum(cents for _, cents in allocations)
    if allocated <= 0:
        return None
    if allocated >= total:
        return ReceiptMatch(
            None,
            0.8,
            f"paid in cash from {len(allocations)} withdrawal(s) already in the ledger",
            link_status="cash_allocated",
            allocations=allocations,
        )
    return ReceiptMatch(
        None,
        0.5,
        (
            f"only {allocated / 100:.2f} of {total / 100:.2f} could be traced to a cash "
            "withdrawal - the rest has no source in the imported statements"
        ),
        link_status="cash_allocated",
        allocations=allocations,
    )


def _apply_match(conn: sqlite3.Connection, receipt_id: int, match: ReceiptMatch) -> ReceiptMatch:
    """Persist a match decision. ``counts_as_outflow`` stays 0 in every case -
    a receipt never adds money to the totals."""
    conn.execute(
        "UPDATE receipts SET link_status=?, transaction_id=?, match_score=?, match_reason=?, "
        "counts_as_outflow=0 WHERE id=?",
        (match.link_status, match.transaction_id, match.score, match.reason, receipt_id),
    )
    if match.link_status == "matched" and match.transaction_id is not None:
        _enrich_transaction_from_receipt(conn, match.transaction_id, receipt_id)
    conn.commit()
    return match


def _enrich_transaction_from_receipt(
    conn: sqlite3.Connection, txn_id: int, receipt_id: int
) -> None:
    """A matched slip knows the real merchant and often a better category.

    It never changes the amount - the bank is authoritative on that.
    """
    r = conn.execute(
        "SELECT merchant_norm, merchant_raw, category FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    t = conn.execute(
        "SELECT category_source, merchant_norm FROM transactions WHERE id=?", (txn_id,)
    ).fetchone()
    if r is None or t is None:
        return

    merchant = r["merchant_norm"] or r["merchant_raw"]
    # A user's own choice always wins; otherwise the slip beats a guess.
    if r["category"] and t["category_source"] in ("unset", "default"):
        conn.execute(
            "UPDATE transactions SET category=?, category_source='receipt' WHERE id=?",
            (r["category"], txn_id),
        )
    if merchant and (not t["merchant_norm"] or t["merchant_norm"] in ("Unknown", "")):
        conn.execute(
            "UPDATE transactions SET merchant_norm=? WHERE id=?", (merchant, txn_id)
        )


def cash_available(
    conn: sqlite3.Connection, withdrawal_id: int
) -> int:
    """Unallocated cents remaining on a cash withdrawal."""
    row = conn.execute(
        "SELECT -amount_cents AS magnitude FROM transactions WHERE id=?", (withdrawal_id,)
    ).fetchone()
    if row is None:
        return 0
    used = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS used FROM cash_allocations WHERE withdrawal_id=?",
        (withdrawal_id,),
    ).fetchone()["used"]
    return max(0, int(row["magnitude"]) - int(used))


def allocate_cash(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    amount_cents: int,
    on_or_before: date,
    lookback_days: int = 21,
    account_id: int | None = None,
) -> list[tuple[int, int]]:
    """Consume a cash receipt against prior withdrawals, nearest first.

    This is what makes cash spend visible by category without inventing a
    second outflow: the withdrawal already left the account, so allocating a
    slip against it re-labels part of that money rather than adding to it.
    """
    conn.execute("DELETE FROM cash_allocations WHERE receipt_id = ?", (receipt_id,))

    lo = (on_or_before - timedelta(days=lookback_days)).isoformat()
    hi = on_or_before.isoformat()
    params: list = [lo, hi]
    account_clause = ""
    if account_id is not None:
        account_clause = "AND account_id = ?"
        params.append(account_id)

    rows = conn.execute(
        f"""SELECT id, txn_date, -amount_cents AS magnitude
            FROM transactions
            WHERE is_cash_withdrawal = 1 AND status = 'active' AND amount_cents < 0
              AND txn_date BETWEEN ? AND ? {account_clause}
            ORDER BY txn_date DESC, id DESC""",
        params,
    ).fetchall()

    remaining = amount_cents
    allocations: list[tuple[int, int]] = []
    for row in rows:
        if remaining <= 0:
            break
        available = cash_available(conn, int(row["id"]))
        if available <= 0:
            continue
        take = min(available, remaining)
        conn.execute(
            "INSERT INTO cash_allocations(receipt_id, withdrawal_id, amount_cents) "
            "VALUES(?,?,?) ON CONFLICT(receipt_id, withdrawal_id) DO UPDATE SET amount_cents=?",
            (receipt_id, int(row["id"]), take, take),
        )
        allocations.append((int(row["id"]), take))
        remaining -= take

    return allocations


def rematch_all_receipts(conn: sqlite3.Connection, **kwargs) -> dict[str, int]:
    """Re-run matching for every receipt that is not manually resolved.

    Worth doing after importing a new statement: slips that were unexplained
    may now have a bank row to attach to.
    """
    counts: dict[str, int] = defaultdict(int)
    ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM receipts WHERE link_status IN ('unmatched','cash_allocated') "
            "ORDER BY receipt_date, id"
        )
    ]
    for rid in ids:
        result = match_receipt(conn, rid, **kwargs)
        counts[result.link_status] += 1
    return dict(counts)


def find_duplicate_receipts(conn: sqlite3.Connection, receipt_id: int) -> list[int]:
    """Other receipts that look like the same slip photographed twice."""
    r = conn.execute(
        "SELECT merchant_norm, receipt_date, total_cents FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    if r is None or r["total_cents"] is None or not r["receipt_date"]:
        return []
    others = conn.execute(
        "SELECT id, merchant_norm, merchant_raw FROM receipts "
        "WHERE id != ? AND receipt_date = ? AND total_cents = ? AND link_status != 'ignored'",
        (receipt_id, r["receipt_date"], r["total_cents"]),
    ).fetchall()
    mine = r["merchant_norm"] or ""
    return [
        int(x["id"])
        for x in others
        if _merchant_similarity(mine, x["merchant_norm"] or x["merchant_raw"] or "") >= 0.6
    ]
