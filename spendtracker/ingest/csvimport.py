"""Bank statement CSV reader.

Bank CSV exports are wildly inconsistent: preamble rows before the header,
one signed amount column or separate debit/credit columns, day-first or
month-first dates, comma or dot decimals, quoted thousands separators, trailing
total rows. This module figures the layout out and - importantly - checks its
own conclusion against the running balance column when one exists, so a wrong
sign convention is caught rather than silently inverting your whole statement.

If auto-detection is wrong you can pin the layout explicitly with a
:class:`ColumnMap`, or add a named profile to ``bank_profiles.json``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..money import parse_amount, to_cents

# ---------------------------------------------------------------------------
# Header vocabulary
# ---------------------------------------------------------------------------

# Longest / most specific phrases first within each list.
HEADER_SYNONYMS: dict[str, list[str]] = {
    "txn_date": [
        "transactiondate", "transdate", "txndate", "valuedate", "effectivedate",
        "dateprocessed", "processdate", "postingdate", "posteddate", "actiondate",
        "date", "datum", "trndate", "bookingdate",
    ],
    "posted_date": ["postingdate", "posteddate", "processdate", "captureddate"],
    "description": [
        "transactiondescription", "transactiondetails", "transactionremarks",
        "description1", "description2", "description3", "description",
        "narrative", "narration", "details", "detail", "particulars",
        "reference", "yourreference", "theirreference", "memo", "payee",
        "beneficiary", "merchant", "transaction", "remarks", "beskrywing",
    ],
    "amount": [
        "transactionamount", "amountzar", "amountinzar", "amount", "value",
        "bedrag", "trnamount", "signedamount",
    ],
    "debit": [
        "debitamount", "debitszar", "moneyout", "amountout", "withdrawal",
        "withdrawals", "paidout", "payments", "debits", "debit", "dr",
    ],
    "credit": [
        "creditamount", "creditszar", "moneyin", "amountin", "deposit",
        "deposits", "paidin", "receipts", "credits", "credit", "cr",
    ],
    "balance": [
        "runningbalance", "closingbalance", "balanceafter", "availablebalance",
        "balance", "saldo",
    ],
    "direction": ["debitcredit", "drcr", "type", "transactiontype", "indicator", "sign"],
    "card_last4": ["cardnumber", "cardlast4", "card"],
    "fees": ["fee", "servicefee", "charge", "charges"],
}

# Header cells that mark a row as *not* the header (summary blocks, etc).
_NOISE_HEADERS = {"", "none", "nan"}

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d",
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
    "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
    "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y",
    "%d %b %y", "%b %d, %Y", "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
]

_DIRECTION_OUT = {"d", "dr", "debit", "db", "withdrawal", "payment", "out", "-"}
_DIRECTION_IN = {"c", "cr", "credit", "deposit", "in", "+"}


class CsvFormatError(ValueError):
    """Raised when a file cannot be understood as a bank statement."""


def norm_header(text: str) -> str:
    """Reduce a header cell to a comparison key."""
    return re.sub(r"[^a-z0-9]", "", (text or "").strip().lower())


# ---------------------------------------------------------------------------
# Layout description
# ---------------------------------------------------------------------------


@dataclass
class ColumnMap:
    """Which column index supplies which field. -1 / empty means absent."""

    txn_date: int = -1
    posted_date: int = -1
    description: list[int] = field(default_factory=list)
    amount: int = -1
    debit: int = -1
    credit: int = -1
    balance: int = -1
    direction: int = -1
    card_last4: int = -1
    # True when a single signed amount column stores outflows as negative.
    outflow_is_negative: bool = True
    dayfirst: bool = True
    # Provenance for the audit trail / UI.
    detection_notes: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def has_amount(self) -> bool:
        return self.amount >= 0 or self.debit >= 0 or self.credit >= 0

    def to_dict(self) -> dict:
        return {
            "txn_date": self.txn_date,
            "posted_date": self.posted_date,
            "description": list(self.description),
            "amount": self.amount,
            "debit": self.debit,
            "credit": self.credit,
            "balance": self.balance,
            "direction": self.direction,
            "outflow_is_negative": self.outflow_is_negative,
            "dayfirst": self.dayfirst,
            "confidence": round(self.confidence, 3),
            "notes": list(self.detection_notes),
        }


@dataclass
class ParsedRow:
    """One statement line, normalised."""

    txn_date: date
    description: str
    amount_cents: int  # negative = outflow
    balance_cents: int | None = None
    posted_date: date | None = None
    row_ordinal: int = 0
    raw: list[str] = field(default_factory=list)
    card_last4: str | None = None


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    column_map: ColumnMap
    header: list[str]
    file_sha256: str
    delimiter: str
    encoding: str
    skipped: list[tuple[int, str, str]] = field(default_factory=list)  # line no, reason, raw
    warnings: list[str] = field(default_factory=list)
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None

    @property
    def period_start(self) -> date | None:
        return min((r.txn_date for r in self.rows), default=None)

    @property
    def period_end(self) -> date | None:
        return max((r.txn_date for r in self.rows), default=None)


# ---------------------------------------------------------------------------
# Low level file handling
# ---------------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode statement bytes, returning (text, encoding-used)."""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8/replace"


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:60])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    # Fall back to whichever candidate yields the most consistent column count.
    best, best_score = ",", -1.0
    for cand in (",", ";", "\t", "|"):
        counts = [line.count(cand) for line in sample.splitlines() if line.strip()]
        if not counts or max(counts) == 0:
            continue
        mode = max(set(counts), key=counts.count)
        score = counts.count(mode) * mode
        if score > best_score:
            best, best_score = cand, score
    return best


def read_grid(path: str | Path) -> tuple[list[list[str]], str, str, str]:
    """Read a CSV into a raw grid of strings.

    Returns (grid, delimiter, encoding, sha256).
    """
    raw = Path(path).read_bytes()
    text, encoding = decode_bytes(raw)
    delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    grid = [[(cell or "").strip() for cell in row] for row in reader]
    return grid, delimiter, encoding, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Header + column detection
# ---------------------------------------------------------------------------


def _score_header_row(cells: Sequence[str]) -> float:
    """How much does this row look like a header row?"""
    keys = [norm_header(c) for c in cells]
    non_empty = [k for k in keys if k and k not in _NOISE_HEADERS]
    if len(non_empty) < 2:
        return 0.0

    score = 0.0
    matched_fields: set[str] = set()
    for key in non_empty:
        for fieldname, synonyms in HEADER_SYNONYMS.items():
            if any(key == syn for syn in synonyms):
                score += 2.0
                matched_fields.add(fieldname)
                break
            if any(syn in key or key in syn for syn in synonyms if len(syn) > 3):
                score += 1.0
                matched_fields.add(fieldname)
                break

    # A header row should not itself parse as data.
    numeric = sum(1 for k in non_empty if re.fullmatch(r"-?[\d.,]+", k or ""))
    score -= numeric * 1.5

    # Must contain something date-like and something amount-like to be usable.
    if "txn_date" not in matched_fields:
        score -= 3.0
    if not matched_fields & {"amount", "debit", "credit"}:
        score -= 3.0
    return score


def find_header_row(grid: list[list[str]], *, search_limit: int = 30) -> int:
    """Index of the most header-like row, or -1 if none looks like one."""
    best_idx, best_score = -1, 0.5
    for idx, row in enumerate(grid[:search_limit]):
        s = _score_header_row(row)
        if s > best_score:
            best_idx, best_score = idx, s
    return best_idx


def detect_columns(header: Sequence[str]) -> ColumnMap:
    """Map header cells onto fields."""
    keys = [norm_header(c) for c in header]
    cmap = ColumnMap()
    taken: set[int] = set()

    def claim(fieldname: str, *, multi: bool = False) -> None:
        synonyms = HEADER_SYNONYMS[fieldname]
        hits: list[int] = []
        # Exact matches first, in synonym-preference order.
        for syn in synonyms:
            for i, key in enumerate(keys):
                if i in taken or not key:
                    continue
                if key == syn:
                    hits.append(i)
                    if not multi:
                        break
            if hits and not multi:
                break
        if not hits:
            for syn in synonyms:
                if len(syn) < 4:
                    continue
                for i, key in enumerate(keys):
                    if i in taken or not key:
                        continue
                    if syn in key:
                        hits.append(i)
                        if not multi:
                            break
                if hits and not multi:
                    break
        if not hits:
            return
        if multi:
            for i in hits:
                taken.add(i)
            cmap.description = sorted(hits)
        else:
            taken.add(hits[0])
            setattr(cmap, fieldname, hits[0])

    # Order matters: specific numeric columns before the generic date/desc grab.
    claim("balance")
    claim("debit")
    claim("credit")
    claim("amount")
    claim("txn_date")
    claim("posted_date")
    claim("direction")
    claim("card_last4")
    claim("description", multi=True)

    matched = sum(
        1
        for f in ("txn_date", "amount", "debit", "credit", "balance")
        if getattr(cmap, f) >= 0
    ) + (1 if cmap.description else 0)
    cmap.confidence = min(1.0, matched / 4.0)
    return cmap


def infer_columns_without_header(grid: list[list[str]]) -> ColumnMap:
    """Positional fallback for exports with no header row at all.

    Picks the first column that parses as a date everywhere, the last numeric
    column as the balance (if the file has two or more numeric columns), the
    remaining numeric column as the amount, and the widest text column as the
    description.
    """
    data = [r for r in grid if any(c.strip() for c in r)]
    if not data:
        raise CsvFormatError("file is empty")
    width = max(len(r) for r in data)
    sample = data[: min(len(data), 50)]

    date_cols, numeric_cols, text_widths = [], [], {}
    for col in range(width):
        vals = [r[col] for r in sample if col < len(r) and r[col].strip()]
        if not vals:
            continue
        dates = sum(1 for v in vals if parse_date(v) is not None)
        nums = sum(1 for v in vals if parse_amount(v) is not None)
        if dates >= max(2, int(0.8 * len(vals))):
            date_cols.append(col)
        elif nums >= max(2, int(0.8 * len(vals))):
            numeric_cols.append(col)
        else:
            text_widths[col] = sum(len(v) for v in vals) / len(vals)

    if not date_cols or not numeric_cols:
        raise CsvFormatError(
            "could not find a date column and an amount column; "
            "pass an explicit column map or add a bank profile"
        )

    cmap = ColumnMap()
    cmap.txn_date = date_cols[0]
    if len(date_cols) > 1:
        cmap.posted_date = date_cols[1]
    if len(numeric_cols) >= 2:
        cmap.balance = numeric_cols[-1]
        cmap.amount = numeric_cols[-2]
    else:
        cmap.amount = numeric_cols[0]
    if text_widths:
        cmap.description = [max(text_widths, key=lambda c: text_widths[c])]
    cmap.confidence = 0.4
    cmap.detection_notes.append("no header row found; columns inferred by content")
    return cmap


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def parse_date(value: str, *, dayfirst: bool = True) -> date | None:
    """Parse a date from a statement cell, preferring day-first order."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or len(s) < 6:
        return None
    s = s.replace(" ", " ")

    ordered = list(DATE_FORMATS)
    if not dayfirst:
        # Move month-first formats ahead of day-first ones.
        ordered.sort(key=lambda f: 0 if f.startswith(("%m", "%b", "%B", "%Y")) else 1)

    for fmt in ordered:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    try:  # last resort: dateutil handles odd separators and month names
        from dateutil import parser as dateparser

        return dateparser.parse(s, dayfirst=dayfirst, fuzzy=False).date()
    except (ImportError, ValueError, OverflowError, TypeError):
        return None


def detect_dayfirst(values: Iterable[str]) -> bool:
    """Decide day-first vs month-first from the data itself.

    A value whose first component exceeds 12 proves day-first; one whose second
    component exceeds 12 proves month-first. Ambiguous files default to
    day-first, which is the South African / European convention.
    """
    day_evidence = month_evidence = 0
    for value in values:
        m = re.match(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$", str(value or ""))
        if not m:
            continue
        first, second = int(m.group(1)), int(m.group(2))
        if first > 12 and second <= 12:
            day_evidence += 1
        elif second > 12 and first <= 12:
            month_evidence += 1
    if month_evidence > day_evidence:
        return False
    return True


def _cell(row: Sequence[str], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _direction_sign(token: str) -> int | None:
    t = re.sub(r"[^a-z+\-]", "", (token or "").strip().lower())
    if not t:
        return None
    if t in _DIRECTION_OUT:
        return -1
    if t in _DIRECTION_IN:
        return 1
    return None


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def parse_statement(
    path: str | Path,
    *,
    column_map: ColumnMap | None = None,
    profile: dict | None = None,
) -> ParseResult:
    """Parse a bank statement CSV into normalised rows."""
    grid, delimiter, encoding, sha = read_grid(path)
    if not grid:
        raise CsvFormatError(f"{path}: file is empty")

    if profile:
        column_map = _column_map_from_profile(profile)

    header: list[str] = []
    warnings: list[str] = []

    if column_map is None:
        header_idx = find_header_row(grid)
        if header_idx >= 0:
            header = grid[header_idx]
            column_map = detect_columns(header)
            data_rows = grid[header_idx + 1 :]
            if header_idx > 0:
                column_map.detection_notes.append(
                    f"skipped {header_idx} preamble row(s) before the header"
                )
        else:
            column_map = infer_columns_without_header(grid)
            data_rows = grid
            warnings.append(
                "No header row recognised - column layout was inferred from the data. "
                "Check the first few imported rows before trusting the totals."
            )
    else:
        header_idx = find_header_row(grid)
        if header_idx >= 0:
            header = grid[header_idx]
            data_rows = grid[header_idx + 1 :]
        else:
            data_rows = grid

    if not column_map.has_amount:
        raise CsvFormatError(
            f"{path}: no amount, debit or credit column found. "
            f"Header seen: {header or '(none)'}"
        )
    if column_map.txn_date < 0:
        raise CsvFormatError(f"{path}: no date column found. Header seen: {header or '(none)'}")

    # Day-first detection from the actual date column.
    date_samples = [_cell(r, column_map.txn_date) for r in data_rows[:200]]
    column_map.dayfirst = detect_dayfirst(date_samples)

    rows: list[ParsedRow] = []
    skipped: list[tuple[int, str, str]] = []
    ordinal = 0

    for offset, raw_row in enumerate(data_rows):
        line_no = (header_idx + 2 + offset) if header_idx >= 0 else (offset + 1)
        if not any((c or "").strip() for c in raw_row):
            continue

        raw_date = _cell(raw_row, column_map.txn_date)
        txn_date = parse_date(raw_date, dayfirst=column_map.dayfirst)
        if txn_date is None:
            skipped.append((line_no, "unparseable date", delimiter.join(raw_row)[:300]))
            continue

        amount = _row_amount(raw_row, column_map)
        if amount is None:
            skipped.append((line_no, "no amount value", delimiter.join(raw_row)[:300]))
            continue

        description = " ".join(
            part for part in (_cell(raw_row, i) for i in column_map.description) if part
        ).strip()
        if not description:
            description = "(no description)"
        description = re.sub(r"\s+", " ", description)

        balance = to_cents(parse_amount(_cell(raw_row, column_map.balance))) if column_map.balance >= 0 else None
        posted = (
            parse_date(_cell(raw_row, column_map.posted_date), dayfirst=column_map.dayfirst)
            if column_map.posted_date >= 0
            else None
        )
        last4 = None
        if column_map.card_last4 >= 0:
            digits = re.findall(r"\d{4}", _cell(raw_row, column_map.card_last4))
            last4 = digits[-1] if digits else None

        rows.append(
            ParsedRow(
                txn_date=txn_date,
                description=description,
                amount_cents=amount,
                balance_cents=balance,
                posted_date=posted,
                row_ordinal=ordinal,
                raw=list(raw_row),
                card_last4=last4,
            )
        )
        ordinal += 1

    if not rows:
        raise CsvFormatError(
            f"{path}: no data rows could be parsed "
            f"({len(skipped)} row(s) skipped). Header seen: {header or '(none)'}"
        )

    # Verify (and if necessary correct) the sign convention against balances.
    sign_warnings = _verify_signs(rows, column_map)
    warnings.extend(sign_warnings)

    opening, closing = _balance_bookends(rows)

    return ParseResult(
        rows=rows,
        column_map=column_map,
        header=header,
        file_sha256=sha,
        delimiter=delimiter,
        encoding=encoding,
        skipped=skipped,
        warnings=warnings,
        opening_balance_cents=opening,
        closing_balance_cents=closing,
    )


def _row_amount(row: Sequence[str], cmap: ColumnMap) -> int | None:
    """Signed cents for one row: negative = money out."""
    if cmap.debit >= 0 or cmap.credit >= 0:
        debit = parse_amount(_cell(row, cmap.debit)) if cmap.debit >= 0 else None
        credit = parse_amount(_cell(row, cmap.credit)) if cmap.credit >= 0 else None
        # Banks are inconsistent about whether the debit column is already
        # negative; magnitude plus column position is the reliable signal.
        total = 0
        seen = False
        if debit is not None and debit != 0:
            total -= abs(debit)
            seen = True
        if credit is not None and credit != 0:
            total += abs(credit)
            seen = True
        if not seen:
            # Both blank/zero: fall through to a signed amount column if any.
            if cmap.amount < 0:
                return None
        else:
            return to_cents(total)

    if cmap.amount < 0:
        return None
    value = parse_amount(_cell(row, cmap.amount))
    if value is None:
        return None

    if cmap.direction >= 0:
        sign = _direction_sign(_cell(row, cmap.direction))
        if sign is not None:
            return to_cents(abs(value) * sign)

    cents = to_cents(value)
    if cents is None:
        return None
    return cents if cmap.outflow_is_negative else -cents


def _verify_signs(rows: list[ParsedRow], cmap: ColumnMap) -> list[str]:
    """Cross-check amounts against the running balance and repair if inverted.

    Returns a list of human-readable warnings.
    """
    warnings: list[str] = []
    with_balance = [r for r in rows if r.balance_cents is not None]

    if len(with_balance) < 3:
        # No balance column to check against. The one case we can still catch
        # is a single-amount-column statement where every value is positive,
        # which means the sign convention is carried elsewhere (or the file is
        # a payments-only export).
        if cmap.amount >= 0 and cmap.debit < 0 and cmap.credit < 0:
            if all(r.amount_cents >= 0 for r in rows):
                for r in rows:
                    r.amount_cents = -abs(r.amount_cents)
                cmap.outflow_is_negative = False
                cmap.detection_notes.append(
                    "single amount column, no negatives and no balance column: "
                    "treated every row as an outflow"
                )
                warnings.append(
                    "This file has one amount column with no negative values and no "
                    "balance column, so every row was treated as money out. If it also "
                    "contains deposits, the totals will be wrong - re-export with "
                    "separate debit/credit columns or a balance column."
                )
        return warnings

    def agreement(flip: bool) -> float:
        """Fraction of consecutive pairs where balance delta matches amount."""
        ok = total = 0
        for prev, cur in zip(with_balance, with_balance[1:]):
            if cur.balance_cents is None or prev.balance_cents is None:
                continue
            delta = cur.balance_cents - prev.balance_cents
            amt = -cur.amount_cents if flip else cur.amount_cents
            total += 1
            if abs(delta - amt) <= 2:  # allow 2c for rounding in the export
                ok += 1
        return ok / total if total else 0.0

    as_is, flipped = agreement(False), agreement(True)

    if flipped > as_is and flipped >= 0.7:
        for r in rows:
            r.amount_cents = -r.amount_cents
        cmap.outflow_is_negative = not cmap.outflow_is_negative
        cmap.detection_notes.append(
            f"amount signs inverted to match the balance column "
            f"(agreement {as_is:.0%} -> {flipped:.0%})"
        )
        as_is = flipped
    elif as_is >= 0.7:
        cmap.detection_notes.append(
            f"amount signs confirmed against the balance column ({as_is:.0%} agreement)"
        )

    if max(as_is, flipped) < 0.7:
        warnings.append(
            f"Amounts only agree with the running balance for {max(as_is, flipped):.0%} of "
            "rows. The statement may be out of date order, may contain rows this parser "
            "skipped, or may use an unusual layout. Check the imported rows against the "
            "original statement before relying on the totals."
        )
    else:
        cmap.confidence = min(1.0, cmap.confidence + 0.3)

    return warnings


def _balance_bookends(rows: list[ParsedRow]) -> tuple[int | None, int | None]:
    """Opening balance (before the first row) and closing balance."""
    with_balance = [r for r in rows if r.balance_cents is not None]
    if not with_balance:
        return None, None
    first, last = with_balance[0], with_balance[-1]
    opening = None
    if first.balance_cents is not None:
        opening = first.balance_cents - first.amount_cents
    return opening, last.balance_cents


# ---------------------------------------------------------------------------
# Named profiles
# ---------------------------------------------------------------------------

PROFILES_PATH = Path(__file__).resolve().parent.parent.parent / "bank_profiles.json"


def load_profiles(path: str | Path | None = None) -> dict[str, dict]:
    p = Path(path) if path else PROFILES_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data.get("profiles", data) if isinstance(data, dict) else {}


def _column_map_from_profile(profile: dict) -> ColumnMap:
    cmap = ColumnMap()
    for key in ("txn_date", "posted_date", "amount", "debit", "credit", "balance", "direction", "card_last4"):
        if key in profile:
            setattr(cmap, key, int(profile[key]))
    desc = profile.get("description", [])
    cmap.description = [int(d) for d in (desc if isinstance(desc, list) else [desc])]
    cmap.outflow_is_negative = bool(profile.get("outflow_is_negative", True))
    cmap.dayfirst = bool(profile.get("dayfirst", True))
    cmap.confidence = 1.0
    cmap.detection_notes.append(f"explicit profile: {profile.get('name', 'custom')}")
    return cmap


def describe_result(result: ParseResult) -> dict[str, Any]:
    """Compact, loggable summary of what the parser concluded."""
    return {
        "rows": len(result.rows),
        "skipped": len(result.skipped),
        "delimiter": repr(result.delimiter),
        "encoding": result.encoding,
        "period": [
            result.period_start.isoformat() if result.period_start else None,
            result.period_end.isoformat() if result.period_end else None,
        ],
        "columns": result.column_map.to_dict(),
        "warnings": result.warnings,
    }
