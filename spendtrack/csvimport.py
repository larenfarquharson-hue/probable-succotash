"""CSV bank statement import.

Bank CSV exports differ in almost every respect: delimiter, preamble junk above
the header, column names, whether debits are a separate column or a negative
amount, and even whether "positive" means money in or money out. This module
detects all of that, reports what it decided, and lets the user override any
part of it.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import normalise, parsing
from .parsing import ParseError

ENCODINGS = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
DELIMITERS = [",", ";", "\t", "|"]
MAX_HEADER_SCAN = 40

# Header aliases, lowercase and stripped of punctuation. Longest match wins so
# "transaction date" beats a bare "date" when both appear.
ALIASES: dict[str, list[str]] = {
    "date": [
        "transaction date", "txn date", "trans date", "posting date",
        "posted date", "date posted", "effective date", "value date",
        "process date", "action date", "datum", "date",
    ],
    "description": [
        "transaction description", "transaction details", "transaction",
        "description", "narrative", "details", "detail", "particulars",
        "memo", "payee", "merchant", "beneficiary", "narration",
        "reference", "ref", "type",
    ],
    "amount": [
        "transaction amount", "amount in zar", "amount zar", "amount",
        "value", "signed amount", "net amount",
    ],
    "debit": [
        "debit amount", "debits", "debit", "money out", "paid out",
        "withdrawal", "withdrawals", "withdrawal amount", "out", "dr",
    ],
    "credit": [
        "credit amount", "credits", "credit", "money in", "paid in",
        "deposit", "deposits", "deposit amount", "in", "cr",
    ],
    "balance": [
        "running balance", "closing balance", "available balance",
        "balance after", "balance", "bal",
    ],
    "fee": [
        "service fee", "transaction fee", "bank fee", "fees", "fee",
    ],
    "indicator": [
        "debit credit indicator", "dr cr", "dr/cr", "debit or credit",
        "transaction type", "type indicator", "sign",
    ],
    "card": ["card number", "card", "card last 4", "account number", "account"],
}


@dataclass
class ColumnMap:
    date: int | None = None
    description: list[int] = field(default_factory=list)
    amount: int | None = None
    debit: int | None = None
    credit: int | None = None
    balance: int | None = None
    fee: int | None = None
    indicator: int | None = None

    def is_usable(self) -> bool:
        has_money = self.amount is not None or self.debit is not None or self.credit is not None
        return self.date is not None and has_money


@dataclass
class ParsedRow:
    txn_date: date
    description: str
    amount: float
    balance: float | None = None
    fee: float | None = None
    line_no: int = 0


@dataclass
class ParseResult:
    """What the importer made of a file, including how it decided."""
    rows: list[ParsedRow] = field(default_factory=list)
    encoding: str = ""
    delimiter: str = ""
    header_row: int = -1
    headers: list[str] = field(default_factory=list)
    column_map: ColumnMap = field(default_factory=ColumnMap)
    sign_convention: str = "signed"   # signed | positive_is_outflow | split_columns
    date_format: str | None = None
    skipped: list[tuple[int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    profile: str | None = None

    @property
    def outflow_total(self) -> float:
        return sum(r.amount for r in self.rows if r.amount < 0)

    @property
    def inflow_total(self) -> float:
        return sum(r.amount for r in self.rows if r.amount > 0)


# --------------------------------------------------------------------------
# Low-level file reading
# --------------------------------------------------------------------------

def read_text(path: Path) -> tuple[str, str]:
    """Read a file, returning (text, encoding-used)."""
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1 (with replacements)"


def detect_delimiter(text: str) -> str:
    """Pick the delimiter that yields the most consistent column count."""
    sample = "\n".join(text.splitlines()[:MAX_HEADER_SCAN + 20])
    best, best_score = ",", -1.0
    for delim in DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delim))
        except csv.Error:
            continue
        widths = [len(r) for r in rows if any(c.strip() for c in r)]
        if not widths:
            continue
        modal = max(set(widths), key=widths.count)
        if modal < 2:
            continue
        # Reward many columns and consistency across rows.
        consistency = widths.count(modal) / len(widths)
        score = modal * consistency
        if score > best_score:
            best, best_score = delim, score
    return best


def _norm_header(value: str) -> str:
    """Lowercase a header cell and reduce punctuation to single spaces."""
    text = (value or "").strip().strip('"').lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def match_header(cell: str) -> str | None:
    """Map one header cell to a logical field, preferring the longest alias."""
    text = _norm_header(cell)
    if not text:
        return None
    best: tuple[int, str] | None = None
    for field_name, aliases in ALIASES.items():
        for alias in aliases:
            if text == alias:
                score = 100 + len(alias)
            elif text.startswith(alias + " ") or text.endswith(" " + alias):
                score = 50 + len(alias)
            elif alias in text and len(alias) >= 4:
                score = 10 + len(alias)
            else:
                continue
            if best is None or score > best[0]:
                best = (score, field_name)
    return best[1] if best else None


def find_header_row(rows: list[list[str]]) -> int:
    """Locate the header row, skipping any bank preamble above it.

    Scored on how many cells map to known fields; a row that looks like data
    (parses as a date plus an amount) is never treated as a header.
    """
    best_idx, best_score = -1, 0
    for idx, row in enumerate(rows[:MAX_HEADER_SCAN]):
        if not any(cell.strip() for cell in row):
            continue
        fields = {match_header(cell) for cell in row}
        fields.discard(None)
        score = len(fields)
        has_date_field = "date" in fields
        has_money = bool(fields & {"amount", "debit", "credit"})
        if not (has_date_field and has_money):
            continue
        # Reject rows whose cells are actual values rather than labels.
        value_like = sum(
            1 for cell in row
            if cell.strip() and (parsing.looks_like_date(cell) or parsing.looks_like_amount(cell))
        )
        if value_like >= 2:
            continue
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx


def build_column_map(headers: list[str]) -> ColumnMap:
    cmap = ColumnMap()
    claimed: dict[str, tuple[int, int]] = {}   # field -> (index, score)
    desc_candidates: list[tuple[int, int]] = []

    for idx, cell in enumerate(headers):
        text = _norm_header(cell)
        if not text:
            continue
        field_name = match_header(cell)
        if field_name is None:
            continue
        # Score again so the best column wins when two map to the same field.
        score = max(
            (100 + len(a)) if text == a else (50 + len(a)) if
            (text.startswith(a + " ") or text.endswith(" " + a)) else
            (10 + len(a)) if (a in text and len(a) >= 4) else 0
            for a in ALIASES[field_name]
        )
        if field_name == "description":
            desc_candidates.append((idx, score))
            continue
        prev = claimed.get(field_name)
        if prev is None or score > prev[1]:
            claimed[field_name] = (idx, score)

    for field_name, (idx, _score) in claimed.items():
        if hasattr(cmap, field_name):
            setattr(cmap, field_name, idx)

    # Keep every description-ish column; several banks split the narrative over
    # two or three ("Description 1/2/3", or "Type" plus "Reference").
    cmap.description = [idx for idx, _ in sorted(desc_candidates, key=lambda p: p[0])]
    return cmap


# --------------------------------------------------------------------------
# Sign convention
# --------------------------------------------------------------------------

def detect_sign_convention(rows: list[list[str]], cmap: ColumnMap) -> tuple[str, list[str]]:
    """Work out whether a positive amount means money in or money out.

    Uses the running balance where available: if the balance falls on rows with
    a positive amount, positive means an outflow. Falls back to the shape of the
    data (an all-positive amount column on a spending account is outflows).
    """
    notes: list[str] = []
    if cmap.amount is None:
        return "split_columns", notes

    pairs: list[tuple[float, float]] = []
    for row in rows:
        amount = _cell_amount(row, cmap.amount)
        balance = _cell_amount(row, cmap.balance) if cmap.balance is not None else None
        if amount is None or balance is None:
            continue
        pairs.append((amount, balance))

    if len(pairs) >= 3:
        agree_signed = agree_flipped = 0
        for (amt, bal), (_prev_amt, prev_bal) in zip(pairs[1:], pairs[:-1]):
            delta = round(bal - prev_bal, 2)
            if abs(delta) < 0.005 or abs(amt) < 0.005:
                continue
            if abs(delta - amt) < 0.02:
                agree_signed += 1
            elif abs(delta + amt) < 0.02:
                agree_flipped += 1
        if agree_signed or agree_flipped:
            if agree_flipped > agree_signed:
                notes.append(
                    "Running balance falls when the amount is positive, so this "
                    "export uses positive = money out. Signs were flipped."
                )
                return "positive_is_outflow", notes
            notes.append("Signs confirmed against the running balance column.")
            return "signed", notes

    amounts = [a for a in (_cell_amount(r, cmap.amount) for r in rows) if a is not None]
    if amounts and all(a >= 0 for a in amounts):
        if cmap.indicator is not None:
            notes.append("All amounts positive; using the debit/credit indicator column.")
            return "indicator", notes
        notes.append(
            "All amounts are positive and there is no balance or indicator column, "
            "so they are treated as outflows. Override with --positive-is inflow "
            "if that is wrong."
        )
        return "positive_is_outflow", notes
    return "signed", notes


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _cell_amount(row: list[str], idx: int | None) -> float | None:
    text = _cell(row, idx)
    return parsing.try_amount(text) if text else None


_DEBIT_WORDS = {"d", "dr", "db", "debit", "debits", "out", "outflow", "withdrawal", "payment"}
_CREDIT_WORDS = {"c", "cr", "credit", "credits", "in", "inflow", "deposit"}


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def parse_file(path: Path, profile: dict | None = None,
               positive_is: str | None = None) -> ParseResult:
    """Parse a statement CSV into ParsedRow objects plus diagnostics.

    `profile` may pin any of: delimiter, encoding, header_row, columns,
    date_format, positive_is. `positive_is` ('inflow'|'outflow') overrides the
    detected sign convention.
    """
    profile = profile or {}
    result = ParseResult(profile=profile.get("name"))

    if profile.get("encoding"):
        text = path.read_text(encoding=profile["encoding"])
        result.encoding = profile["encoding"]
    else:
        text, result.encoding = read_text(path)

    result.delimiter = profile.get("delimiter") or detect_delimiter(text)
    all_rows = [r for r in csv.reader(io.StringIO(text), delimiter=result.delimiter)]
    # Track original line numbers before dropping blank rows.
    numbered = [(i + 1, r) for i, r in enumerate(all_rows) if any(c.strip() for c in r)]
    if not numbered:
        raise ParseError(f"{path.name} contains no data rows")

    stripped = [r for _, r in numbered]

    if "header_row" in profile:
        header_idx = int(profile["header_row"])
    else:
        header_idx = find_header_row(stripped)

    if header_idx >= 0:
        result.header_row = numbered[header_idx][0]
        result.headers = [c.strip() for c in stripped[header_idx]]
        data_rows = numbered[header_idx + 1:]
        cmap = build_column_map(result.headers)
    else:
        result.notes.append(
            "No header row recognised; columns were inferred from the data itself."
        )
        data_rows = numbered
        cmap = _infer_headerless(stripped)

    if profile.get("columns"):
        cmap = _apply_column_overrides(cmap, profile["columns"], result.headers)

    if not cmap.is_usable():
        raise ParseError(
            f"Could not identify a date column and an amount column in {path.name}. "
            f"Headers seen: {result.headers or '(none)'}. "
            "Supply a profile with explicit column indexes."
        )
    result.column_map = cmap

    # Keep only rows that actually carry a parseable date, so trailing totals
    # and footers fall away rather than corrupting the import.
    candidates: list[tuple[int, list[str]]] = []
    for line_no, row in data_rows:
        date_text = _cell(row, cmap.date)
        if not date_text:
            result.skipped.append((line_no, "no date value"))
            continue
        if not parsing.looks_like_date(date_text):
            result.skipped.append((line_no, f"unparseable date {date_text!r}"))
            continue
        candidates.append((line_no, row))

    if not candidates:
        raise ParseError(f"No rows with a valid date found in {path.name}")

    convention = profile.get("positive_is")
    if positive_is:
        convention = positive_is
    if convention == "outflow":
        result.sign_convention = "positive_is_outflow"
    elif convention == "inflow":
        result.sign_convention = "signed"
    else:
        result.sign_convention, notes = detect_sign_convention(
            [r for _, r in candidates], cmap)
        result.notes.extend(notes)

    date_texts = [_cell(r, cmap.date) for _, r in candidates]
    result.date_format = profile.get("date_format") or parsing.detect_date_format(date_texts)
    if result.date_format is None:
        result.notes.append(
            "Dates use more than one format; each was parsed individually. "
            "Check a few rows for day/month swaps."
        )

    for line_no, row in candidates:
        try:
            parsed = _row_to_parsed(row, cmap, result, line_no)
        except ParseError as exc:
            result.skipped.append((line_no, str(exc)))
            continue
        if parsed is not None:
            result.rows.append(parsed)

    result.rows.sort(key=lambda r: (r.txn_date, r.line_no))
    return result


def _row_to_parsed(row: list[str], cmap: ColumnMap, result: ParseResult,
                   line_no: int) -> ParsedRow | None:
    date_text = _cell(row, cmap.date)
    if result.date_format:
        txn_date = datetime.strptime(date_text, result.date_format).date()
    else:
        txn_date = parsing.parse_date(date_text)

    parts: list[str] = []
    for idx in cmap.description:
        value = _cell(row, idx)
        if value and value not in parts:
            parts.append(value)
    description = " ".join(parts).strip()
    if not description:
        description = "(no description)"

    amount = _resolve_amount(row, cmap, result)
    if amount is None:
        raise ParseError("no amount value")

    balance = _cell_amount(row, cmap.balance) if cmap.balance is not None else None
    fee = None
    if cmap.fee is not None:
        fee_value = _cell_amount(row, cmap.fee)
        if fee_value is not None and abs(fee_value) > 0.004:
            fee = -abs(fee_value)

    return ParsedRow(
        txn_date=txn_date,
        description=description,
        amount=round(amount, 2),
        balance=balance,
        fee=fee,
        line_no=line_no,
    )


def _resolve_amount(row: list[str], cmap: ColumnMap, result: ParseResult) -> float | None:
    """Combine amount / debit / credit / indicator columns into a signed value."""
    debit = _cell_amount(row, cmap.debit) if cmap.debit is not None else None
    credit = _cell_amount(row, cmap.credit) if cmap.credit is not None else None

    if debit is not None and abs(debit) > 0.004:
        return -abs(debit)
    if credit is not None and abs(credit) > 0.004:
        return abs(credit)

    if cmap.amount is None:
        # Both split columns were zero or blank: a genuinely zero-value row.
        if debit is not None or credit is not None:
            return 0.0
        return None

    amount = _cell_amount(row, cmap.amount)
    if amount is None:
        return None

    if result.sign_convention == "positive_is_outflow":
        return -amount
    if result.sign_convention == "indicator" and cmap.indicator is not None:
        marker = _cell(row, cmap.indicator).lower().strip(". ")
        if marker in _DEBIT_WORDS or "debit" in marker or marker.startswith("d"):
            return -abs(amount)
        if marker in _CREDIT_WORDS or "credit" in marker or marker.startswith("c"):
            return abs(amount)
        return -abs(amount)
    return amount


def _infer_headerless(rows: list[list[str]]) -> ColumnMap:
    """Guess columns for a file with no header, from the shape of the values."""
    width = max(len(r) for r in rows)
    date_col = amount_col = balance_col = None
    date_hits = [0] * width
    amount_hits = [0] * width
    text_hits = [0] * width

    for row in rows[:200]:
        for idx in range(min(width, len(row))):
            cell = (row[idx] or "").strip()
            if not cell:
                continue
            if parsing.looks_like_date(cell):
                date_hits[idx] += 1
            elif parsing.looks_like_amount(cell):
                amount_hits[idx] += 1
            else:
                text_hits[idx] += 1

    if any(date_hits):
        date_col = date_hits.index(max(date_hits))
    money_cols = [i for i, n in enumerate(amount_hits) if n > 0]
    money_cols.sort(key=lambda i: amount_hits[i], reverse=True)
    if money_cols:
        # Where two money columns exist, the last is usually the balance.
        ordered = sorted(money_cols[:2])
        amount_col = ordered[0]
        if len(ordered) > 1:
            balance_col = ordered[1]
    desc_cols = [i for i, n in enumerate(text_hits) if n > 0 and i != date_col]

    return ColumnMap(date=date_col, description=desc_cols, amount=amount_col,
                     balance=balance_col)


def _apply_column_overrides(cmap: ColumnMap, overrides: dict,
                            headers: list[str]) -> ColumnMap:
    """Apply explicit column overrides given as an index or a header name."""
    def resolve(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        text = _norm_header(str(value))
        for idx, cell in enumerate(headers):
            if _norm_header(cell) == text:
                return idx
        if str(value).isdigit():
            return int(value)
        raise ParseError(f"Profile references unknown column {value!r}")

    for key, value in overrides.items():
        if key == "description":
            items = value if isinstance(value, list) else [value]
            cmap.description = [i for i in (resolve(v) for v in items) if i is not None]
        elif hasattr(cmap, key):
            setattr(cmap, key, resolve(value))
    return cmap


# --------------------------------------------------------------------------
# Bank profiles
# --------------------------------------------------------------------------

def profiles_dir() -> Path:
    return Path(__file__).parent / "profiles"


def load_profile(name: str) -> dict:
    from . import config
    for base in (config.home_dir() / "profiles", profiles_dir()):
        path = base / f"{name}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("name", name)
            return data
    raise ParseError(f"No such profile: {name}. Use `spendtrack profiles` to list them.")


def list_profiles() -> list[tuple[str, str]]:
    from . import config
    seen: dict[str, str] = {}
    for base in (profiles_dir(), config.home_dir() / "profiles"):
        if not base.exists():
            continue
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            seen[path.stem] = data.get("description", "")
    return sorted(seen.items())


def describe(result: ParseResult) -> str:
    """Human-readable account of how a file was interpreted."""
    cmap = result.column_map
    lines = [
        f"  encoding        : {result.encoding}",
        f"  delimiter       : {result.delimiter!r}",
        f"  header row      : {result.header_row if result.header_row > 0 else 'none found'}",
        f"  date column     : {_col_label(cmap.date, result.headers)}",
        f"  description     : {', '.join(_col_label(i, result.headers) for i in cmap.description) or '-'}",
        f"  amount column   : {_col_label(cmap.amount, result.headers)}",
        f"  debit column    : {_col_label(cmap.debit, result.headers)}",
        f"  credit column   : {_col_label(cmap.credit, result.headers)}",
        f"  balance column  : {_col_label(cmap.balance, result.headers)}",
        f"  fee column      : {_col_label(cmap.fee, result.headers)}",
        f"  sign convention : {result.sign_convention}",
        f"  date format     : {result.date_format or 'mixed (per-row)'}",
        f"  rows parsed     : {len(result.rows)}",
        f"  rows skipped    : {len(result.skipped)}",
    ]
    if result.rows:
        lines.append(
            f"  date range      : {result.rows[0].txn_date} to {result.rows[-1].txn_date}"
        )
    return "\n".join(lines)


def _col_label(idx: int | None, headers: list[str]) -> str:
    if idx is None:
        return "-"
    if 0 <= idx < len(headers) and headers[idx]:
        return f"[{idx}] {headers[idx]}"
    return f"[{idx}]"
