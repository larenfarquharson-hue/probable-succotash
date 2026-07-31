"""Look before you import, and undo after you did.

Two safety tools, both ported from the ``spendtrack`` prototype branch, both
answering questions the rest of the app deliberately cannot.

``preview_file``
    Parses a statement CSV and reports what the importer *would* conclude —
    columns, sign convention, date order, totals, skipped lines — without
    opening the database or writing a single row. The importer is good at
    guessing, but a wrong guess about sign convention silently inverts every
    number in a report. This makes the guess inspectable before it costs you
    anything.

``undo_statement``
    Removes everything one statement import added. The ``statements`` table
    already records each import and every transaction carries its
    ``statement_id``, so an import is a unit that can be reversed exactly.
    Without this the only remedy for a bad import is deleting the database.

Neither function ever changes the meaning of a total. Preview writes nothing;
undo removes only rows a specific import created, and refuses when a row has
since accumulated evidence that would be destroyed with it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ingest import csvimport
from .money import fmt


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@dataclass
class Preview:
    """What the importer concluded about a file, without importing it."""

    path: Path
    result: csvimport.ParseResult
    outflow_cents: int = 0
    inflow_cents: int = 0
    concerns: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.result.rows)

    def to_dict(self) -> dict[str, Any]:
        described = csvimport.describe_result(self.result)
        described.update(
            {
                "path": str(self.path),
                "outflow_cents": self.outflow_cents,
                "inflow_cents": self.inflow_cents,
                "concerns": list(self.concerns),
            }
        )
        return described


def preview_file(
    path: str | Path,
    *,
    profile: dict | None = None,
) -> Preview:
    """Parse ``path`` and summarise it. Never touches the database.

    Raises :class:`csvimport.CsvFormatError` for a file that cannot be parsed
    at all — the same error the importer would raise, surfaced earlier.
    """
    path = Path(path)
    result = csvimport.parse_statement(path, profile=profile)

    outflow = sum(-r.amount_cents for r in result.rows if r.amount_cents < 0)
    inflow = sum(r.amount_cents for r in result.rows if r.amount_cents > 0)

    preview = Preview(
        path=path,
        result=result,
        outflow_cents=outflow,
        inflow_cents=inflow,
        concerns=_concerns(result, outflow, inflow),
    )
    return preview


def _concerns(result: csvimport.ParseResult, outflow: int, inflow: int) -> list[str]:
    """Things worth a human's attention that the parser does not already say.

    The parser emits its own warnings (``result.warnings``) and the preview
    prints those verbatim, so this function deliberately adds only what those
    do not cover. Every item is a plausible misreading that would corrupt
    totals rather than merely look untidy — a noisy check here trains you to
    ignore the whole section, which is worse than having no check.
    """
    out: list[str] = []
    cmap = result.column_map

    if not result.rows:
        out.append("no rows parsed — the header row or delimiter is probably wrong")
        return out

    if cmap.confidence < 0.5:
        out.append(
            f"low confidence ({cmap.confidence:.0%}) in the column layout — "
            "check the columns above against your file, and use --profile if wrong"
        )

    if inflow and not outflow:
        out.append(
            "every row parsed as money coming IN, with nothing going out. If this "
            "is a spending statement the sign convention is inverted — use a "
            "profile with outflow_is_negative set the other way"
        )

    if cmap.balance < 0:
        out.append(
            "no running balance column found — deduplication of overlapping "
            "imports falls back to occurrence counting, which is slightly weaker. "
            "Re-export with a balance column if your bank offers one"
        )

    skipped = len(result.skipped)
    total_lines = skipped + len(result.rows)
    if skipped and skipped > total_lines * 0.1:
        out.append(
            f"{skipped} of {total_lines} lines were skipped — that is a lot, "
            "check the samples below before trusting the totals"
        )

    # Day-first vs month-first can only be inferred from the data when some
    # date has a day above 12. A file where none does is genuinely ambiguous —
    # but a short file is ambiguous by chance rather than by format, so only
    # flag it once the file is long enough that a day > 12 should have appeared.
    span_days = 0
    if result.period_start and result.period_end:
        span_days = (result.period_end - result.period_start).days
    if span_days > 20 and all(r.txn_date.day <= 12 for r in result.rows):
        out.append(
            "this file spans more than three weeks yet no date has a day above "
            "12, so day-first vs month-first could not be confirmed from the "
            "data — check the sample rows below against your statement"
        )

    return out


def format_preview(preview: Preview, *, symbol: str = "R", rows: int = 8) -> str:
    """Human-readable preview, in the same voice as the rest of the CLI."""
    r = preview.result
    cmap = r.column_map
    lines: list[str] = []

    lines.append(f"{preview.path}")
    lines.append(f"  encoding        {r.encoding}")
    lines.append(f"  delimiter       {r.delimiter!r}")
    lines.append(f"  rows parsed     {preview.row_count}")
    lines.append(f"  rows skipped    {len(r.skipped)}")
    lines.append(
        f"  period          {r.period_start or '—'} to {r.period_end or '—'}"
    )
    lines.append(f"  confidence      {cmap.confidence:.0%}")

    lines.append("")
    lines.append("  Columns understood as:")
    named = [
        ("date", cmap.txn_date),
        ("posted date", cmap.posted_date),
        ("amount", cmap.amount),
        ("debit", cmap.debit),
        ("credit", cmap.credit),
        ("balance", cmap.balance),
        ("direction", cmap.direction),
    ]
    for label, idx in named:
        if idx >= 0:
            head = r.header[idx] if idx < len(r.header) else "(no header)"
            lines.append(f"    {label:<12} column {idx}  {head}")
    if cmap.description:
        heads = ", ".join(
            r.header[i] if i < len(r.header) else "(no header)"
            for i in cmap.description
        )
        cols = "+".join(str(i) for i in cmap.description)
        lines.append(f"    {'description':<12} column {cols}  {heads}")
    lines.append(
        f"    {'signs':<12} "
        + (
            "outflows are negative"
            if cmap.outflow_is_negative
            else "outflows are positive"
        )
    )
    lines.append(
        f"    {'dates':<12} " + ("day first" if cmap.dayfirst else "month first")
    )

    for note in cmap.detection_notes:
        lines.append(f"    note: {note}")
    for warning in r.warnings:
        lines.append(f"    warning: {warning}")

    lines.append("")
    lines.append(f"  Money out       {fmt(preview.outflow_cents, symbol):>16}")
    lines.append(f"  Money in        {fmt(preview.inflow_cents, symbol):>16}")

    if r.rows:
        lines.append("")
        lines.append(f"  First {min(rows, len(r.rows))} row(s) as they would be stored:")
        for row in r.rows[:rows]:
            lines.append(
                f"    {row.txn_date}  {fmt(row.amount_cents, symbol, signed=True):>14}"
                f"  {row.description[:52]}"
            )

    if r.skipped:
        lines.append("")
        lines.append(f"  Skipped {len(r.skipped)} line(s):")
        for line_no, reason, raw in r.skipped[:6]:
            lines.append(f"    line {line_no}: {reason}  |  {raw[:48]}")

    if preview.concerns:
        lines.append("")
        lines.append("  Worth checking before you import:")
        for concern in preview.concerns:
            lines.append(f"    ! {concern}")
    else:
        lines.append("")
        lines.append("  Nothing looks wrong. Safe to import.")

    lines.append("")
    lines.append("  Nothing was written. Run import-statement when this looks right.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


@dataclass
class UndoPlan:
    """What undoing a statement would remove, worked out before removing it."""

    statement_id: int
    filename: str
    imported_at: str
    transaction_count: int
    outflow_cents: int
    inflow_cents: int
    linked_receipts: int
    cash_allocations: int
    pending_candidates: int

    @property
    def blocked(self) -> bool:
        """True when undoing would destroy evidence gathered since the import."""
        return bool(self.linked_receipts or self.cash_allocations)


def plan_undo(conn: sqlite3.Connection, statement_id: int) -> UndoPlan:
    """Work out the consequences of undoing an import. Changes nothing.

    Raises ``LookupError`` when no such statement exists.
    """
    stmt = conn.execute(
        "SELECT id, filename, imported_at FROM statements WHERE id = ?",
        (statement_id,),
    ).fetchone()
    if stmt is None:
        raise LookupError(f"no statement with id {statement_id}")

    totals = conn.execute(
        "SELECT COUNT(*) n,"
        "       COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) out,"
        "       COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) inn"
        "  FROM transactions WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()

    receipts = conn.execute(
        "SELECT COUNT(*) n FROM receipts r"
        "  JOIN transactions t ON t.id = r.transaction_id"
        " WHERE t.statement_id = ?",
        (statement_id,),
    ).fetchone()

    allocations = conn.execute(
        "SELECT COUNT(*) n FROM cash_allocations a"
        "  JOIN transactions t ON t.id = a.withdrawal_id"
        " WHERE t.statement_id = ?",
        (statement_id,),
    ).fetchone()

    candidates = conn.execute(
        "SELECT COUNT(*) n FROM duplicate_candidates c"
        "  JOIN transactions t ON t.id = c.txn_id"
        " WHERE t.statement_id = ? AND c.resolution = 'pending'",
        (statement_id,),
    ).fetchone()

    return UndoPlan(
        statement_id=statement_id,
        filename=stmt["filename"],
        imported_at=stmt["imported_at"],
        transaction_count=totals["n"],
        outflow_cents=totals["out"],
        inflow_cents=totals["inn"],
        linked_receipts=receipts["n"],
        cash_allocations=allocations["n"],
        pending_candidates=candidates["n"],
    )


def undo_statement(
    conn: sqlite3.Connection,
    statement_id: int,
    *,
    force: bool = False,
) -> UndoPlan:
    """Remove one import and everything it added. Returns what was removed.

    Refuses when till slips have since been linked to these transactions, or
    cash has been allocated against them, unless ``force`` is set. Those links
    are work you did by hand; silently discarding them would be the kind of
    quiet data loss this app exists to avoid. With ``force``, receipts are
    unlinked and returned to the review queue rather than deleted — a slip is
    evidence and outlives any particular import of the statement.
    """
    plan = plan_undo(conn, statement_id)
    if plan.blocked and not force:
        raise ValueError(
            f"statement {statement_id} has {plan.linked_receipts} linked receipt(s) "
            f"and {plan.cash_allocations} cash allocation(s). Re-run with force to "
            "unlink them and remove the import anyway."
        )

    with conn:
        # Return slips to the review queue instead of deleting them.
        conn.execute(
            "UPDATE receipts SET transaction_id = NULL, link_status = 'unmatched',"
            "       match_score = NULL, match_reason = NULL"
            " WHERE transaction_id IN"
            "   (SELECT id FROM transactions WHERE statement_id = ?)",
            (statement_id,),
        )
        conn.execute(
            "DELETE FROM cash_allocations WHERE withdrawal_id IN"
            "   (SELECT id FROM transactions WHERE statement_id = ?)",
            (statement_id,),
        )
        conn.execute(
            "DELETE FROM duplicate_candidates WHERE txn_id IN"
            "   (SELECT id FROM transactions WHERE statement_id = ?)"
            "    OR existing_id IN"
            "   (SELECT id FROM transactions WHERE statement_id = ?)",
            (statement_id, statement_id),
        )
        conn.execute("DELETE FROM transactions WHERE statement_id = ?", (statement_id,))
        conn.execute("DELETE FROM statements WHERE id = ?", (statement_id,))
        conn.execute(
            "INSERT INTO ingest_log (kind, ref, outcome, detail) VALUES (?,?,?,?)",
            (
                "statement",
                plan.filename,
                "undone",
                f"removed statement {statement_id} and {plan.transaction_count} transaction(s)",
            ),
        )

    return plan


def list_statements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every import so far, newest first, with what each contributed."""
    return conn.execute(
        "SELECT s.id, s.filename, s.imported_at, s.period_start, s.period_end,"
        "       s.rows_imported, s.rows_duplicate, s.rows_skipped,"
        "       COALESCE(SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents END), 0) outflow"
        "  FROM statements s"
        "  LEFT JOIN transactions t ON t.statement_id = s.id"
        " GROUP BY s.id"
        " ORDER BY s.imported_at DESC, s.id DESC"
    ).fetchall()
