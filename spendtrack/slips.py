"""Till slip ingestion.

A slip is *evidence about* an outflow, never an outflow in itself. Whatever route
it arrives by — hand-typed JSON, OCR, or the interactive prompt — it lands in the
same shape and is then matched against the statement by matching.py.

Three routes in, deliberately:
  json   — a JSON file per slip (or a list of them). Always available, and the
           target format for anything that reads a photo for you.
  ocr    — tesseract, if it happens to be installed. Optional by design; slip
           photos vary enormously and OCR output still needs checking.
  manual — interactive prompts. Slow, but always works and is never wrong about
           the total, which is the number that actually matters.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import categorise, normalise, parsing

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}

PAYMENT_CARD = "card"
PAYMENT_CASH = "cash"
PAYMENT_EFT = "eft"
PAYMENT_UNKNOWN = "unknown"


@dataclass
class SlipItem:
    description: str
    qty: float | None = None
    unit_price: float | None = None
    line_total: float | None = None
    category: str | None = None


@dataclass
class Slip:
    merchant: str | None = None
    slip_date: date | None = None
    slip_time: str | None = None
    total: float | None = None
    tax: float | None = None
    payment_method: str = PAYMENT_UNKNOWN
    card_last4: str | None = None
    image_path: str | None = None
    raw_text: str | None = None
    source: str = "json"
    notes: str | None = None
    items: list[SlipItem] = field(default_factory=list)

    @property
    def merchant_key(self) -> str:
        return normalise.merchant_key(self.merchant or "")

    def content_hash(self) -> str:
        """Identity of the *purchase*, so two photos of one slip collide.

        Time is included because it is what separates two same-value purchases
        at the same shop on the same day. Without a time on the slip, such a
        pair does collide — `slip add --force` is the escape hatch.
        """
        item_sig = ";".join(
            f"{i.description}:{i.line_total}" for i in self.items[:40]
        )
        return normalise.content_hash(
            self.merchant_key,
            self.slip_date.isoformat() if self.slip_date else "",
            f"{self.total:.2f}" if self.total is not None else "",
            self.slip_time or "",
            item_sig,
        )

    def problems(self) -> list[str]:
        """What is missing before this slip can be trusted or matched."""
        issues = []
        if self.total is None:
            issues.append("no total")
        if self.slip_date is None:
            issues.append("no date")
        if not self.merchant:
            issues.append("no merchant")
        if self.total is not None and self.items:
            summed = sum(i.line_total or 0 for i in self.items)
            if summed and abs(summed - self.total) > max(1.0, self.total * 0.02):
                issues.append(
                    f"line items sum to {summed:.2f} but the total says {self.total:.2f}"
                )
        return issues


# --------------------------------------------------------------------------
# JSON route
# --------------------------------------------------------------------------

SLIP_JSON_TEMPLATE = {
    "merchant": "Checkers Hyper Sandton",
    "date": "2026-06-02",
    "time": "17:42",
    "total": 1842.66,
    "tax": 240.35,
    "payment_method": "card",
    "card_last4": "8891",
    "image": "IMG_0231.jpg",
    "notes": "",
    "items": [
        {"description": "Full cream milk 2L", "qty": 2, "unit_price": 32.99, "total": 65.98},
        {"description": "Bread white loaf", "qty": 1, "unit_price": 21.99, "total": 21.99},
    ],
}


def from_dict(data: dict, default_image: str | None = None) -> Slip:
    """Build a Slip from a JSON object, accepting several key spellings."""
    def pick(*names):
        for name in names:
            if name in data and data[name] not in (None, ""):
                return data[name]
        return None

    raw_date = pick("date", "slip_date", "purchase_date", "transaction_date")
    slip_date = None
    if raw_date:
        slip_date = parsing.parse_date(str(raw_date))

    raw_total = pick("total", "amount", "grand_total", "total_amount", "total_due")
    total = parsing.parse_amount(str(raw_total)) if raw_total is not None else None
    if total is not None:
        total = abs(total)

    raw_tax = pick("tax", "vat", "vat_amount", "tax_amount")
    tax = abs(parsing.parse_amount(str(raw_tax))) if raw_tax is not None else None

    items: list[SlipItem] = []
    for entry in (pick("items", "line_items", "lines") or []):
        if isinstance(entry, str):
            items.append(SlipItem(description=entry))
            continue
        line_total = entry.get("total", entry.get("line_total", entry.get("amount")))
        unit = entry.get("unit_price", entry.get("price", entry.get("unit")))
        qty = entry.get("qty", entry.get("quantity"))
        items.append(SlipItem(
            description=str(entry.get("description") or entry.get("name") or "item"),
            qty=float(qty) if qty not in (None, "") else None,
            unit_price=abs(parsing.parse_amount(str(unit))) if unit not in (None, "") else None,
            line_total=abs(parsing.parse_amount(str(line_total)))
            if line_total not in (None, "") else None,
        ))

    method = str(pick("payment_method", "payment", "paid_with", "tender") or "").lower()
    method = normalise_payment_method(method)

    last4 = pick("card_last4", "last4", "card")
    if last4 is not None:
        digits = re.sub(r"\D", "", str(last4))
        last4 = digits[-4:] if len(digits) >= 4 else None

    return Slip(
        merchant=str(pick("merchant", "store", "shop", "vendor", "name") or "") or None,
        slip_date=slip_date,
        slip_time=str(pick("time", "slip_time") or "") or None,
        total=total,
        tax=tax,
        payment_method=method,
        card_last4=last4,
        image_path=str(pick("image", "image_path", "photo", "file") or default_image or "")
        or None,
        raw_text=pick("raw_text", "text"),
        source=str(pick("source") or "json"),
        notes=str(pick("notes", "note", "comment") or "") or None,
        items=items,
    )


def load_json_file(path: Path) -> list[Slip]:
    """Read one JSON file containing a slip, a list of slips, or {"slips": [...]}"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "slips" in data:
        data = data["slips"]
    entries = data if isinstance(data, list) else [data]
    slips = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slip = from_dict(entry)
        slip.source = slip.source or "json"
        slips.append(slip)
    return slips


def normalise_payment_method(value: str) -> str:
    text = (value or "").lower()
    if not text:
        return PAYMENT_UNKNOWN
    if any(w in text for w in ("cash", "kontant", "notes", "coin")):
        return PAYMENT_CASH
    if any(w in text for w in ("card", "visa", "master", "amex", "debit", "credit",
                               "contactless", "tap", "chip", "snapscan", "zapper",
                               "apple pay", "samsung pay", "garmin pay")):
        return PAYMENT_CARD
    if any(w in text for w in ("eft", "transfer", "instant", "payshap", "capitec pay")):
        return PAYMENT_EFT
    return PAYMENT_UNKNOWN


# --------------------------------------------------------------------------
# OCR route
# --------------------------------------------------------------------------

def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(path: Path, lang: str = "eng") -> str:
    """Run tesseract over an image and return its text."""
    if not tesseract_available():
        raise RuntimeError(
            "tesseract is not installed, so photos cannot be read automatically.\n"
            "Three ways forward:\n"
            "  1. Install it   — Debian/Ubuntu: sudo apt install tesseract-ocr\n"
            "                    macOS: brew install tesseract\n"
            "  2. Write a slip JSON per photo and run `spendtrack slip add <file.json>`\n"
            "     (`spendtrack slip template` prints the format)\n"
            "  3. Type it in   — `spendtrack slip enter` asks for merchant, date and total"
        )
    proc = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", lang, "--psm", "6"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract failed on {path.name}: {proc.stderr.strip()}")
    return proc.stdout


# Lines that carry an amount but are not a purchased item.
_NOT_AN_ITEM = re.compile(
    r"(?i)\b(sub\s*-?\s*total|total|vat|tax|change|tendered|tender|cash|card|visa|"
    r"master|debit|credit|rounding|round|balance|due|discount|saving|savings|"
    r"promotion|promo|loyalty|points|voucher|refund|invoice|receipt|till|cashier|"
    r"operator|terminal|auth|batch|trace|ref|reg no|vat reg|tel|www|http|thank|"
    r"queries|returns|exchange|slip|copy|merchant|aid |tvr|tsi|arqc|approved)\b"
)
_TOTAL_LINE = re.compile(
    r"(?i)\b(grand\s*total|total\s*due|amount\s*due|balance\s*due|to\s*pay|total)\b"
)
_EXCLUDE_TOTAL = re.compile(
    r"(?i)\b(sub\s*-?\s*total|vat|tax|change|tendered|tender|discount|saving|"
    r"rounding|loyalty|points|items?\s*total|qty)\b"
)
_AMOUNT_IN_LINE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ ,]\d{3})*|\d+)[.,](\d{2})(?![\d])")
_QTY_LINE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*[xX@*]\s*(\d+(?:[.,]\d{2}))")
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)(?::([0-5]\d))?\b")
_LAST4_RE = re.compile(r"(?:[\*x#]{2,}|\bxxxx\b)\s*(\d{4})\b", re.I)
_VAT_RE = re.compile(r"(?i)\b(?:vat|tax)\b")


def parse_slip_text(text: str, image_path: str | None = None) -> Slip:
    """Turn OCR text into a Slip. Best-effort by nature — always review it."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]

    slip = Slip(source="ocr", raw_text=text, image_path=image_path)
    slip.merchant = _find_merchant(lines)
    slip.slip_date = _find_date(lines)
    slip.slip_time = _find_time(lines)
    slip.total = _find_total(lines)
    slip.tax = _find_vat(lines)
    slip.payment_method = _find_payment_method(text)
    match = _LAST4_RE.search(text or "")
    if match:
        slip.card_last4 = match.group(1)
    slip.items = _find_items(lines)
    return slip


def _amounts_in(line: str) -> list[float]:
    out = []
    for whole, cents in _AMOUNT_IN_LINE.findall(line):
        value = parsing.try_amount(f"{whole}.{cents}")
        if value is not None:
            out.append(abs(value))
    return out


def _find_merchant(lines: list[str]) -> str | None:
    """The shop name is normally the first line or two of a slip."""
    for line in lines[:6]:
        letters = sum(ch.isalpha() for ch in line)
        if letters < 3:
            continue
        if re.search(r"(?i)\b(vat\s*reg|reg\s*no|tel|fax|www|http|invoice|tax\s*invoice|"
                     r"receipt|slip)\b", line):
            continue
        if _amounts_in(line):
            continue
        cleaned = re.sub(r"[^\w&'\- ]+", " ", line)
        cleaned = " ".join(cleaned.split())
        if len(cleaned) >= 3:
            return cleaned[:60]
    return None


def _find_date(lines: list[str]) -> date | None:
    pattern = re.compile(
        r"(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4})|"
        r"(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\s+\d{2,4})",
        re.I,
    )
    for line in lines:
        for match in pattern.finditer(line):
            token = (match.group(1) or match.group(2) or "").replace(".", "/")
            try:
                found = parsing.parse_date(token)
            except parsing.ParseError:
                continue
            if 2000 <= found.year <= 2100:
                return found
    return None


def _find_time(lines: list[str]) -> str | None:
    for line in lines:
        match = _TIME_RE.search(line)
        if match:
            return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def _find_total(lines: list[str]) -> float | None:
    """Prefer an explicit total line; fall back to the largest amount seen."""
    candidates: list[float] = []
    for idx, line in enumerate(lines):
        if not _TOTAL_LINE.search(line) or _EXCLUDE_TOTAL.search(line):
            continue
        amounts = _amounts_in(line)
        if not amounts:
            # "TOTAL" alone on its line, value printed underneath.
            for follow in lines[idx + 1: idx + 3]:
                amounts = _amounts_in(follow)
                if amounts:
                    break
        if amounts:
            candidates.append(amounts[-1])
    if candidates:
        # The last total printed is the one actually paid.
        return candidates[-1]
    every = [a for line in lines for a in _amounts_in(line)]
    return max(every) if every else None


def _find_vat(lines: list[str]) -> float | None:
    """The VAT amount is the last money value on the VAT line.

    Line-based rather than a single regex over the whole text, because slips
    print the rate before the amount ("VAT @ 15%   240.35") and a rate is not a
    money value.
    """
    for line in lines:
        if not _VAT_RE.search(line) or re.search(r"(?i)\breg\b", line):
            continue
        amounts = _amounts_in(line)
        if amounts:
            return amounts[-1]
    return None


def _find_payment_method(text: str) -> str:
    lowered = (text or "").lower()
    # An explicit change line is strong evidence of cash, even on a slip that
    # also prints the word "card" somewhere in its footer.
    if re.search(r"(?i)\bchange\b[^\n]*\d", lowered) and "cash" in lowered:
        return PAYMENT_CASH
    for word, method in (
        ("cash", PAYMENT_CASH), ("kontant", PAYMENT_CASH),
        ("visa", PAYMENT_CARD), ("mastercard", PAYMENT_CARD),
        ("master card", PAYMENT_CARD), ("debit card", PAYMENT_CARD),
        ("credit card", PAYMENT_CARD), ("card", PAYMENT_CARD),
        ("snapscan", PAYMENT_CARD), ("zapper", PAYMENT_CARD),
        ("payshap", PAYMENT_EFT), ("eft", PAYMENT_EFT),
    ):
        if word in lowered:
            return method
    return PAYMENT_UNKNOWN


def _find_items(lines: list[str]) -> list[SlipItem]:
    items: list[SlipItem] = []
    for idx, line in enumerate(lines):
        if _NOT_AN_ITEM.search(line):
            continue
        amounts = _amounts_in(line)
        if not amounts:
            continue
        # Strip the trailing amount (and any single-letter tax code) to get the
        # description.
        desc = _AMOUNT_IN_LINE.sub(" ", line)
        desc = re.sub(r"(?i)\b[a-z]\s*$", "", desc)
        desc = re.sub(r"[^\w&'%\-. ]+", " ", desc)
        desc = " ".join(desc.split())
        if len(desc) < 2 or not any(ch.isalpha() for ch in desc):
            continue

        qty = unit = None
        qty_match = _QTY_LINE.match(line)
        if qty_match:
            qty = parsing.try_amount(qty_match.group(1))
            unit = parsing.try_amount(qty_match.group(2))
        elif idx + 1 < len(lines):
            follow = _QTY_LINE.match(lines[idx + 1])
            if follow:
                qty = parsing.try_amount(follow.group(1))
                unit = parsing.try_amount(follow.group(2))
        items.append(SlipItem(description=desc[:120], qty=qty, unit_price=unit,
                              line_total=amounts[-1]))
    return items


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

@dataclass
class SlipSaveResult:
    slip_id: int | None
    duplicate: bool
    slip: Slip
    problems: list[str] = field(default_factory=list)


def save_slip(conn: sqlite3.Connection, slip: Slip, import_id: int | None = None,
              force: bool = False,
              categoriser: categorise.Categoriser | None = None) -> SlipSaveResult:
    """Store a slip, refusing a second copy of the same purchase."""
    digest = slip.content_hash()
    if force:
        # A genuine second purchase that hashes the same needs a distinct key.
        existing = conn.execute(
            "SELECT COUNT(*) n FROM slips WHERE content_sha256 LIKE ?",
            (digest + "%",)).fetchone()["n"]
        digest = f"{digest}#{existing}" if existing else digest

    prior = conn.execute(
        "SELECT id FROM slips WHERE content_sha256 = ?", (digest,)).fetchone()
    if prior:
        return SlipSaveResult(slip_id=int(prior["id"]), duplicate=True, slip=slip,
                              problems=slip.problems())

    cat = categoriser or categorise.build()
    assignment = cat.classify(slip.merchant or "", -abs(slip.total or 0))

    cur = conn.execute(
        "INSERT INTO slips(slip_date, slip_time, merchant, merchant_key, total, tax,"
        " payment_method, card_last4, image_path, content_sha256, raw_text, source,"
        " status, category, import_id, notes)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'unmatched',?,?,?)",
        (slip.slip_date.isoformat() if slip.slip_date else None, slip.slip_time,
         slip.merchant, slip.merchant_key, slip.total, slip.tax, slip.payment_method,
         slip.card_last4, slip.image_path, digest, slip.raw_text, slip.source,
         assignment.category, import_id, slip.notes),
    )
    slip_id = int(cur.lastrowid)

    for item in slip.items:
        item_cat = cat.classify(item.description, -abs(item.line_total or 0))
        conn.execute(
            "INSERT INTO slip_items(slip_id, description, qty, unit_price, line_total,"
            " category) VALUES(?,?,?,?,?,?)",
            (slip_id, item.description, item.qty, item.unit_price, item.line_total,
             item_cat.category if item_cat.source == "rule" else None),
        )
    conn.commit()
    return SlipSaveResult(slip_id=slip_id, duplicate=False, slip=slip,
                          problems=slip.problems())


def ingest_path(conn: sqlite3.Connection, path: Path, force: bool = False,
                ocr_lang: str = "eng",
                categoriser: categorise.Categoriser | None = None
                ) -> list[SlipSaveResult]:
    """Ingest a slip JSON, a slip image, or a directory of either."""
    targets: list[Path] = []
    if path.is_dir():
        for child in sorted(path.iterdir()):
            if child.suffix.lower() in IMAGE_SUFFIXES or child.suffix.lower() == ".json":
                targets.append(child)
    else:
        targets.append(path)

    cat = categoriser or categorise.build()
    results: list[SlipSaveResult] = []
    for target in targets:
        import_id = _record_import(conn, target)
        if target.suffix.lower() == ".json":
            for slip in load_json_file(target):
                if not slip.image_path:
                    slip.image_path = None
                results.append(save_slip(conn, slip, import_id, force, cat))
        elif target.suffix.lower() in IMAGE_SUFFIXES:
            text = ocr_image(target, ocr_lang)
            slip = parse_slip_text(text, image_path=str(target))
            results.append(save_slip(conn, slip, import_id, force, cat))
        else:
            raise ValueError(
                f"{target.name}: expected a .json slip or an image "
                f"({', '.join(sorted(IMAGE_SUFFIXES))})"
            )
    return results


def _record_import(conn: sqlite3.Connection, path: Path) -> int:
    cur = conn.execute(
        "INSERT INTO imports(kind, path, file_sha256, imported_at, rows_seen)"
        " VALUES('slip', ?, ?, ?, 1)",
        (str(path), normalise.file_hash(str(path)),
         datetime.now().isoformat(timespec="seconds")),
    )
    return int(cur.lastrowid)
