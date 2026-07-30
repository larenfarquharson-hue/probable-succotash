"""Till slip ingestion: photograph -> structured receipt.

Three extractors, tried in the order you configure:

``claude``     AI vision. By far the most reliable on crumpled, angled, faded
               thermal slips, which is what a real photo of a till slip is.
               Needs ANTHROPIC_API_KEY and sends the image to Anthropic.
``tesseract``  Local OCR, no network, no cost. Needs the ``tesseract`` binary.
               Works on flat, well-lit slips and struggles with the rest.
``manual``     No extraction. The slip is stored and you type the details in.
               Always available, and always the fallback.

Whatever the extractor, the result is *evidence*, not a transaction. Storing a
receipt never adds to your spending total - see :mod:`spendtracker.dedupe`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path

from ..categorise import canonical_key, normalise_merchant
from ..config import Config
from ..money import parse_amount, to_cents

SUPPORTED_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Claude Opus 5 accepts up to 2576px on the long edge. Phone photos are much
# larger, and the extra pixels cost tokens without helping legibility.
MAX_IMAGE_EDGE = 2000


class ReceiptExtractionError(RuntimeError):
    """Raised when a slip could not be read by the chosen extractor."""


@dataclass
class ReceiptItem:
    description: str
    quantity: float | None = None
    unit_price_cents: int | None = None
    line_total_cents: int | None = None
    category: str | None = None


@dataclass
class ReceiptData:
    merchant_raw: str | None = None
    merchant_norm: str | None = None
    receipt_date: date | None = None
    receipt_time: str | None = None
    total_cents: int | None = None
    vat_cents: int | None = None
    tender_type: str = "unknown"       # card|cash|eft|voucher|unknown
    card_last4: str | None = None
    category: str | None = None
    items: list[ReceiptItem] = field(default_factory=list)
    raw_text: str | None = None
    extractor: str = "manual"
    confidence: float | None = None
    notes: str | None = None

    def to_json(self) -> str:
        payload = asdict(self)
        payload["receipt_date"] = (
            self.receipt_date.isoformat() if self.receipt_date else None
        )
        return json.dumps(payload, ensure_ascii=False)

    @property
    def line_items_total_cents(self) -> int:
        return sum(i.line_total_cents or 0 for i in self.items)

    def consistency_warning(self, tolerance_cents: int = 200) -> str | None:
        """Do the line items add up to the stated total?

        A mismatch usually means a line was missed, which matters because the
        line items are what drive the category breakdown of cash spend.
        """
        if self.total_cents is None or not self.items:
            return None
        diff = self.total_cents - self.line_items_total_cents
        if abs(diff) <= tolerance_cents:
            return None
        return (
            f"line items add up to {self.line_items_total_cents / 100:.2f} but the slip "
            f"total reads {self.total_cents / 100:.2f} (difference "
            f"{abs(diff) / 100:.2f}) - a line may have been missed"
        )


# ---------------------------------------------------------------------------
# Schema handed to the model
# ---------------------------------------------------------------------------

RECEIPT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "merchant": {
            "type": "string",
            "description": "Shop or business name exactly as printed on the slip.",
        },
        "date": {
            "type": "string",
            "description": (
                "Purchase date as YYYY-MM-DD. Slips usually print day first "
                "(DD/MM/YYYY). Empty string if not legible."
            ),
        },
        "time": {
            "type": "string",
            "description": "Time as HH:MM if printed, else an empty string.",
        },
        "total": {
            "type": "string",
            "description": (
                "The final amount actually paid, as a plain decimal number with no "
                "currency symbol. Use the TOTAL / AMOUNT DUE line, not the subtotal, "
                "and not the CASH TENDERED or CHANGE lines."
            ),
        },
        "vat": {
            "type": "string",
            "description": "VAT/tax amount as a plain decimal, or an empty string.",
        },
        "tender_type": {
            "type": "string",
            "enum": ["card", "cash", "eft", "voucher", "unknown"],
            "description": (
                "How it was paid. 'card' for any card or tap payment, 'cash' when the "
                "slip shows cash tendered and/or change given, 'unknown' if unclear."
            ),
        },
        "card_last4": {
            "type": "string",
            "description": "Last four digits of the card if shown, else empty string.",
        },
        "items": {
            "type": "array",
            "description": "Purchased line items, in the order printed.",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "string", "description": "Plain number or empty string."},
                    "unit_price": {"type": "string", "description": "Plain decimal or empty string."},
                    "line_total": {"type": "string", "description": "Plain decimal or empty string."},
                },
                "required": ["description", "quantity", "unit_price", "line_total"],
                "additionalProperties": False,
            },
        },
        "legible": {
            "type": "boolean",
            "description": "False if the image is too poor to read reliably.",
        },
        "confidence": {
            "type": "string",
            "description": "Your confidence in the total, one of: high, medium, low.",
        },
        "notes": {
            "type": "string",
            "description": "Anything ambiguous or unreadable. Empty string if none.",
        },
    },
    "required": [
        "merchant", "date", "time", "total", "vat", "tender_type",
        "card_last4", "items", "legible", "confidence", "notes",
    ],
    "additionalProperties": False,
}

EXTRACTION_PROMPT = """\
This is a photograph of a retail till slip (receipt). Read it and return the \
structured data described by the schema.

Rules that matter for correctness:
- The total is the amount actually paid. On South African slips this is usually \
labelled TOTAL, AMOUNT DUE or BALANCE DUE. Never use CASH TENDERED, CHANGE, \
ROUNDING, or the pre-VAT subtotal as the total.
- Dates are almost always day-first (DD/MM/YYYY or DD-MM-YY). Convert to \
YYYY-MM-DD. If the year has two digits, assume the 2000s.
- If the slip shows cash tendered and change given, tender_type is "cash" even \
if a card logo appears elsewhere on the slip.
- Return numbers as plain decimals: "123.45", not "R123,45".
- Do not guess. If a value is not legible, return an empty string for it and say \
so in notes. An empty field is far better than an invented one, because these \
numbers are used to reconcile against a bank statement.
"""


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def media_type_for(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_IMAGE_TYPES:
        raise ReceiptExtractionError(
            f"{Path(path).name}: unsupported image type {suffix!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED_IMAGE_TYPES))}"
        )
    return SUPPORTED_IMAGE_TYPES[suffix]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _prepare_image(path: Path) -> tuple[bytes, str]:
    """Return (image bytes, media type), downscaled if oversized.

    Downscaling is a cost control: a 4000px phone photo costs several times the
    image tokens of a 2000px one and reads no better. Silently skipped if
    Pillow is not installed.
    """
    media_type = media_type_for(path)
    raw = path.read_bytes()

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return raw, media_type

    try:
        import io

        with Image.open(io.BytesIO(raw)) as img:
            if max(img.size) <= MAX_IMAGE_EDGE:
                return raw, media_type
            scale = MAX_IMAGE_EDGE / max(img.size)
            resized = img.convert("RGB").resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.LANCZOS,
            )
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=88)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        # A resize failure must never block ingestion - send the original.
        return raw, media_type


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def extract_with_claude(path: str | Path, cfg: Config) -> ReceiptData:
    """Read a till slip using Claude's vision capability."""
    try:
        import anthropic
    except ImportError as exc:
        raise ReceiptExtractionError(
            "the 'anthropic' package is not installed. Run "
            "'pip install anthropic', or set SPENDTRACKER_OCR_PROVIDER=manual."
        ) from exc

    api_key = cfg.anthropic_api_key
    if not api_key:
        raise ReceiptExtractionError(
            "no Anthropic API key found. Set ANTHROPIC_API_KEY, or set "
            "SPENDTRACKER_OCR_PROVIDER=tesseract (local OCR) or =manual (type it in)."
        )

    path = Path(path)
    image_bytes, media_type = _prepare_image(path)
    encoded = base64.standard_b64encode(image_bytes).decode("ascii")

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=cfg.ocr_model,
            max_tokens=8000,
            output_config={"format": {"type": "json_schema", "schema": RECEIPT_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIStatusError as exc:
        raise ReceiptExtractionError(
            f"Anthropic API error {exc.status_code} reading {path.name}: {exc.message}"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise ReceiptExtractionError(
            f"could not reach the Anthropic API to read {path.name}: {exc}"
        ) from exc

    # Check stop_reason before touching content - a refusal returns HTTP 200
    # with empty or partial content.
    if response.stop_reason == "refusal":
        raise ReceiptExtractionError(
            f"the model declined to process {path.name}. Enter the details manually."
        )
    if response.stop_reason == "max_tokens":
        raise ReceiptExtractionError(
            f"the response for {path.name} was cut short (very long slip). "
            "Photograph it in two halves, or enter it manually."
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ReceiptExtractionError(f"empty response reading {path.name}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReceiptExtractionError(f"unparseable response for {path.name}: {exc}") from exc

    data = _receipt_from_payload(payload)
    data.extractor = f"claude:{cfg.ocr_model}"
    if not payload.get("legible", True):
        data.notes = " ".join(
            filter(None, [data.notes, "the model judged this image hard to read"])
        )
    return data


_CONFIDENCE_MAP = {"high": 0.9, "medium": 0.65, "low": 0.35}


def _receipt_from_payload(payload: dict) -> ReceiptData:
    """Convert the model's JSON into a ReceiptData, tolerating blank fields."""
    merchant_raw = (payload.get("merchant") or "").strip() or None

    receipt_date = None
    raw_date = (payload.get("date") or "").strip()
    if raw_date:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                receipt_date = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue

    items: list[ReceiptItem] = []
    for entry in payload.get("items") or []:
        desc = (entry.get("description") or "").strip()
        if not desc:
            continue
        qty_raw = (entry.get("quantity") or "").strip()
        try:
            quantity = float(qty_raw) if qty_raw else None
        except ValueError:
            quantity = None
        items.append(
            ReceiptItem(
                description=desc,
                quantity=quantity,
                unit_price_cents=to_cents(parse_amount(entry.get("unit_price"))),
                line_total_cents=to_cents(parse_amount(entry.get("line_total"))),
            )
        )

    last4 = (payload.get("card_last4") or "").strip()
    last4 = last4[-4:] if len(last4) >= 4 and last4[-4:].isdigit() else None

    tender = (payload.get("tender_type") or "unknown").strip().lower()
    if tender not in ("card", "cash", "eft", "voucher", "unknown"):
        tender = "unknown"

    return ReceiptData(
        merchant_raw=merchant_raw,
        merchant_norm=normalise_merchant(merchant_raw) if merchant_raw else None,
        receipt_date=receipt_date,
        receipt_time=(payload.get("time") or "").strip() or None,
        total_cents=to_cents(parse_amount(payload.get("total"))),
        vat_cents=to_cents(parse_amount(payload.get("vat"))),
        tender_type=tender,
        card_last4=last4,
        items=items,
        confidence=_CONFIDENCE_MAP.get(
            (payload.get("confidence") or "").strip().lower(), 0.5
        ),
        notes=(payload.get("notes") or "").strip() or None,
    )


def extract_with_tesseract(path: str | Path, cfg: Config) -> ReceiptData:
    """Read a till slip with local OCR, then parse the text heuristically."""
    if shutil.which("tesseract") is None:
        raise ReceiptExtractionError(
            "the 'tesseract' binary was not found. Install it "
            "(Debian/Ubuntu: 'sudo apt install tesseract-ocr'; macOS: "
            "'brew install tesseract'), or use a different OCR provider."
        )
    path = Path(path)
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReceiptExtractionError(f"tesseract timed out on {path.name}") from exc
    except subprocess.CalledProcessError as exc:
        raise ReceiptExtractionError(
            f"tesseract failed on {path.name}: {(exc.stderr or '').strip()[:300]}"
        ) from exc

    data = parse_receipt_text(result.stdout)
    data.extractor = "tesseract"
    data.raw_text = result.stdout
    return data


def extract_manual(path: str | Path, cfg: Config) -> ReceiptData:
    """Store the slip with nothing filled in; you supply the details."""
    return ReceiptData(
        extractor="manual",
        confidence=None,
        notes="stored without extraction - enter the details yourself",
    )


EXTRACTORS = {
    "claude": extract_with_claude,
    "tesseract": extract_with_tesseract,
    "manual": extract_manual,
}


def extract(path: str | Path, cfg: Config, *, provider: str | None = None) -> ReceiptData:
    """Extract a receipt using the configured provider, falling back to manual.

    A failure here is never fatal: the slip is still stored so you can type the
    details in, and the reason is recorded in the notes.
    """
    name = (provider or cfg.ocr_provider or "manual").lower()
    fn = EXTRACTORS.get(name)
    if fn is None:
        raise ReceiptExtractionError(
            f"unknown OCR provider {name!r}. Choose one of: {', '.join(EXTRACTORS)}"
        )
    try:
        return fn(path, cfg)
    except ReceiptExtractionError as exc:
        if name == "manual":
            raise
        fallback = extract_manual(path, cfg)
        fallback.notes = f"{name} extraction failed: {exc}"
        return fallback


# ---------------------------------------------------------------------------
# Heuristic text parser (used for OCR output, and reusable on pasted text)
# ---------------------------------------------------------------------------

_TOTAL_LABELS = [
    "amount due", "balance due", "total due", "grand total", "total incl",
    "total inc", "total amount", "total", "te betaal", "verskuldig",
]
# Labels that look like totals but are not what was spent.
_NOT_TOTAL = [
    "subtotal", "sub total", "sub-total", "cash tendered", "tendered", "cash",
    "change", "change due", "rounding", "vat", "tax", "discount", "saving",
    "balance", "loyalty", "points",
]
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})\b"), "dmy2"),
    (re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"), "ymd"),
]
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b")
_AMOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ ,]\d{3})*(?:[.,]\d{2})|\d+[.,]\d{2})(?![\d])")
_LAST4_RE = re.compile(r"(?:\*{2,}|x{2,}|X{2,}|#{2,})\s*(\d{4})\b")


def parse_receipt_text(text: str) -> ReceiptData:
    """Best-effort structured data from raw receipt text.

    Deliberately conservative: it would rather leave the total blank than pick
    up the change amount, because a wrong total corrupts reconciliation.
    """
    data = ReceiptData(extractor="text", raw_text=text)
    if not text or not text.strip():
        data.notes = "no text could be read"
        return data

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lower = [ln.lower() for ln in lines]

    # Merchant: the first substantial line that is not an amount or a date.
    for ln in lines[:8]:
        if len(ln) < 3 or _AMOUNT_RE.search(ln):
            continue
        if any(p.search(ln) for p, _ in _DATE_PATTERNS):
            continue
        letters = sum(c.isalpha() for c in ln)
        if letters >= max(3, len(ln) * 0.5):
            data.merchant_raw = ln
            data.merchant_norm = normalise_merchant(ln)
            break

    # Date
    for ln in lines:
        for pattern, kind in _DATE_PATTERNS:
            m = pattern.search(ln)
            if not m:
                continue
            try:
                if kind == "ymd":
                    data.receipt_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                else:
                    day, month = int(m.group(1)), int(m.group(2))
                    year = int(m.group(3))
                    if kind == "dmy2":
                        year += 2000
                    if month > 12 and day <= 12:  # printed month-first after all
                        day, month = month, day
                    data.receipt_date = date(year, month, day)
            except ValueError:
                continue
            break
        if data.receipt_date:
            break

    m = _TIME_RE.search(text)
    if m:
        data.receipt_time = f"{int(m.group(1)):02d}:{m.group(2)}"

    # Total: prefer an explicitly labelled total line, skipping decoys.
    best: int | None = None
    for label in _TOTAL_LABELS:
        for ln, low in zip(lines, lower):
            if label not in low:
                continue
            if any(bad in low for bad in _NOT_TOTAL if bad not in label):
                continue
            amounts = [to_cents(parse_amount(a)) for a in _AMOUNT_RE.findall(ln)]
            amounts = [a for a in amounts if a]
            if amounts:
                best = max(amounts)
                break
        if best is not None:
            break

    if best is None:
        # No labelled total. Fall back to the largest amount on the slip, but
        # only when nothing decoy-like is present, and flag low confidence.
        candidates = [to_cents(parse_amount(a)) for a in _AMOUNT_RE.findall(text)]
        candidates = [c for c in candidates if c]
        if candidates:
            best = max(candidates)
            data.confidence = 0.25
            data.notes = (
                "no TOTAL line was found; used the largest amount on the slip - "
                "please check this"
            )
    else:
        data.confidence = 0.6

    data.total_cents = best

    for ln, low in zip(lines, lower):
        if "vat" in low or "tax" in low:
            amounts = [to_cents(parse_amount(a)) for a in _AMOUNT_RE.findall(ln)]
            amounts = [a for a in amounts if a]
            if amounts:
                data.vat_cents = max(amounts)
                break

    blob = " ".join(lower)
    if any(k in blob for k in ("cash tendered", "change due", "tendered")):
        data.tender_type = "cash"
    elif any(k in blob for k in ("credit card", "debit card", "card no", "visa", "mastercard", "contactless", "tap")):
        data.tender_type = "card"
    elif "eft" in blob or "snapscan" in blob or "zapper" in blob:
        data.tender_type = "eft"

    m = _LAST4_RE.search(text)
    if m:
        data.card_last4 = m.group(1)

    return data


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@dataclass
class ReceiptStoreResult:
    receipt_id: int | None
    data: ReceiptData
    duplicate_of: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


def store_receipt(
    conn: sqlite3.Connection,
    image_path: str | Path,
    *,
    cfg: Config,
    account_id: int | None = None,
    data: ReceiptData | None = None,
    provider: str | None = None,
    copy_into_uploads: bool = True,
) -> ReceiptStoreResult:
    """Extract, store and match one till slip.

    The same image file is never stored twice - the SHA-256 of the bytes is a
    unique key, so re-uploading the same photo is a no-op rather than a second
    receipt.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    image_sha = sha256_file(image_path)
    existing = conn.execute(
        "SELECT id FROM receipts WHERE image_sha256 = ?", (image_sha,)
    ).fetchone()
    if existing:
        return ReceiptStoreResult(
            receipt_id=int(existing["id"]),
            data=data or ReceiptData(notes="already stored"),
            duplicate_of=int(existing["id"]),
            warnings=["this exact image has already been uploaded"],
        )

    if data is None:
        data = extract(image_path, cfg, provider=provider)

    stored_path: str | None = None
    if copy_into_uploads:
        cfg.ensure_dirs()
        target_dir = Path(cfg.uploads_dir) / "receipts"
        target = target_dir / f"{image_sha[:16]}{image_path.suffix.lower()}"
        if not target.exists():
            shutil.copy2(image_path, target)
        stored_path = str(target)

    merchant_id = None
    if data.merchant_norm:
        from .. import db as dbmod

        merchant_id = dbmod.get_or_create_merchant(
            conn, canonical_key(data.merchant_norm) or "unknown", data.merchant_norm
        )

    category = data.category
    if not category and data.merchant_norm:
        row = conn.execute(
            "SELECT default_category FROM merchants WHERE canonical = ?",
            (canonical_key(data.merchant_norm),),
        ).fetchone()
        category = row["default_category"] if row else None

    cur = conn.execute(
        "INSERT INTO receipts(account_id, original_filename, stored_path, image_sha256, "
        "receipt_date, receipt_time, merchant_raw, merchant_norm, merchant_id, total_cents, "
        "vat_cents, tender_type, card_last4, category, extractor, confidence, raw_text, "
        "raw_json, link_status, counts_as_outflow) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unmatched',0)",
        (
            account_id,
            image_path.name,
            stored_path,
            image_sha,
            data.receipt_date.isoformat() if data.receipt_date else None,
            data.receipt_time,
            data.merchant_raw,
            data.merchant_norm,
            merchant_id,
            data.total_cents,
            data.vat_cents,
            data.tender_type,
            data.card_last4,
            category,
            data.extractor,
            data.confidence,
            data.raw_text,
            data.to_json(),
        ),
    )
    receipt_id = int(cur.lastrowid)

    for index, item in enumerate(data.items):
        conn.execute(
            "INSERT INTO receipt_items(receipt_id, line_no, description, quantity, "
            "unit_price_cents, line_total_cents, category) VALUES(?,?,?,?,?,?,?)",
            (
                receipt_id,
                index,
                item.description,
                item.quantity,
                item.unit_price_cents,
                item.line_total_cents,
                item.category,
            ),
        )

    warnings: list[str] = []
    if data.total_cents is None:
        warnings.append(
            "no total could be read from this slip, so it cannot be matched to a bank "
            "row until you enter one"
        )
    consistency = data.consistency_warning()
    if consistency:
        warnings.append(consistency)
    if data.confidence is not None and data.confidence < 0.5:
        warnings.append("low confidence in this reading - worth checking the total")
    if data.notes:
        warnings.append(data.notes)

    conn.commit()

    # Match against the ledger. Import here to avoid a circular import.
    from ..dedupe import find_duplicate_receipts, match_receipt

    similar = find_duplicate_receipts(conn, receipt_id)
    if similar:
        warnings.append(
            f"this looks like the same purchase as receipt(s) {', '.join(map(str, similar))} - "
            "check you have not photographed one slip twice"
        )

    match = match_receipt(
        conn,
        receipt_id,
        amount_tolerance_cents=cfg.match_amount_tolerance_cents,
        days_window=cfg.match_days_window,
    )
    if match.link_status == "unmatched" and data.total_cents is not None:
        warnings.append(match.reason)

    from .. import db as dbmod

    dbmod.log_ingest(
        conn, "receipt", image_path.name, match.link_status, match.reason or ""
    )
    conn.commit()

    return ReceiptStoreResult(receipt_id=receipt_id, data=data, warnings=warnings)


def update_receipt(
    conn: sqlite3.Connection,
    receipt_id: int,
    *,
    cfg: Config,
    merchant: str | None = None,
    receipt_date: date | str | None = None,
    total_cents: int | None = None,
    tender_type: str | None = None,
    category: str | None = None,
    note: str | None = None,
    rematch: bool = True,
) -> None:
    """Apply manual corrections to a stored slip, then re-run matching."""
    fields: list[str] = []
    params: list = []

    if merchant is not None:
        fields += ["merchant_raw = ?", "merchant_norm = ?"]
        params += [merchant, normalise_merchant(merchant)]
    if receipt_date is not None:
        iso = receipt_date.isoformat() if isinstance(receipt_date, date) else str(receipt_date)
        fields.append("receipt_date = ?")
        params.append(iso)
    if total_cents is not None:
        fields.append("total_cents = ?")
        params.append(int(total_cents))
    if tender_type is not None:
        fields.append("tender_type = ?")
        params.append(tender_type)
    if category is not None:
        fields.append("category = ?")
        params.append(category)
    if note is not None:
        fields.append("user_note = ?")
        params.append(note)

    if fields:
        # A manual correction is authoritative, so record full confidence.
        fields.append("confidence = 1.0")
        params.append(receipt_id)
        conn.execute(f"UPDATE receipts SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()

    if rematch:
        from ..dedupe import match_receipt

        match_receipt(
            conn,
            receipt_id,
            amount_tolerance_cents=cfg.match_amount_tolerance_cents,
            days_window=cfg.match_days_window,
        )


def ignore_receipt(conn: sqlite3.Connection, receipt_id: int) -> None:
    """Dismiss a slip: keeps the record, excludes it from every report."""
    conn.execute("DELETE FROM cash_allocations WHERE receipt_id = ?", (receipt_id,))
    conn.execute(
        "UPDATE receipts SET link_status='ignored', transaction_id=NULL, "
        "counts_as_outflow=0 WHERE id=?",
        (receipt_id,),
    )
    conn.commit()
