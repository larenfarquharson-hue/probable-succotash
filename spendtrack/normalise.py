"""Text normalisation, fingerprints and fuzzy comparison.

Bank descriptions are noisy: card masks, store numbers, embedded dates, city and
country suffixes. Everything downstream (dedup, categorisation, slip matching)
depends on reducing them to a stable key.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

# Payment-channel noise that carries no information about who was paid.
_PREFIXES = [
    "card purchase", "pos purchase", "purchase at", "point of sale",
    "debit card purchase", "cheque card purchase", "card payment",
    "internet banking payment", "ib payment to", "ib payment from",
    "ib transfer to", "ib transfer from", "immediate payment",
    "magtape debit", "magtape credit", "external debit order",
    "internal debit order", "debit order", "external transfer",
    "electronic payment", "eft payment", "eft debit", "eft credit",
    "payment to", "payment from", "transfer to", "transfer from",
    "app payment to", "app transfer to", "banking app payment",
    "recurring payment", "scheduled payment", "digital payment",
    "purchase", "payment", "pos",
]
# Deliberately NOT stripped: anything naming a cash withdrawal. "ATM CASH
# WITHDRAWAL SANDTON CITY" is *about* the withdrawal — strip that and all
# that remains is a suburb.

_SUFFIXES = ["za", "zaf", "south africa", "sa"]

# Card masks: 4123********1234, 4123*****1234, ...1234
_CARD_MASK = re.compile(r"\b\d{4}[\*x]{2,}\d{2,4}\b", re.I)
_STARS = re.compile(r"[\*]{2,}")
# Embedded transaction dates: "12 JAN", "12/01", "2026-01-12", "12 JAN 26"
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_WORDS = re.compile(rf"\b\d{{1,2}}\s*({_MONTHS})\s*\d{{0,4}}\b", re.I)
_DATE_NUM = re.compile(r"\b\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")
_LONG_DIGITS = re.compile(r"\b\d{4,}\b")
_REF_NOISE = re.compile(r"\b(ref|refno|reference|trace|auth|seq|no|nr)\b\.?\s*[:#]?\s*\w*",
                        re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9&' ]+")
_SPACES = re.compile(r"\s+")

# Words that survive cleaning but never identify a merchant.
_STOPWORDS = {
    "the", "and", "pty", "ltd", "limited", "cc", "inc", "co", "store", "stores",
    "branch", "sa", "za", "online", "www", "com", "net", "http", "https",
    "purchase", "payment", "debit", "credit", "card", "pos", "trf", "trans",
    "transaction", "fee", "fees",
}


def clean_description(raw: str) -> str:
    """Human-readable description with channel noise and identifiers removed."""
    text = (raw or "").strip()
    text = _CARD_MASK.sub(" ", text)
    text = _STARS.sub(" ", text)
    text = _DATE_WORDS.sub(" ", text)
    text = _DATE_NUM.sub(" ", text)
    text = _TIME.sub(" ", text)
    text = _REF_NOISE.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)
    text = _SPACES.sub(" ", text).strip(" -,.:;#*/")

    low = text.lower()
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if low.startswith(prefix + " ") or low == prefix:
                text = text[len(prefix):].strip(" -,.:;#*/")
                low = text.lower()
                changed = True
                break
    for suffix in _SUFFIXES:
        if low.endswith(" " + suffix):
            text = text[: -(len(suffix) + 1)].strip(" -,.:;#*/")
            low = text.lower()
    return _SPACES.sub(" ", text).strip() or (raw or "").strip()


def description_key(raw: str) -> str:
    """Aggressively normalised key: lowercase alphanumerics, noise words gone.

    Two descriptions that mean "the same merchant, same channel" should collapse
    to the same key so overrides and recurrence detection work across months.
    """
    text = clean_description(raw).lower()
    text = _NON_ALNUM.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in _STOPWORDS]
    # Drop pure-numeric leftovers (store numbers, till numbers).
    tokens = [t for t in tokens if not t.isdigit()]
    return " ".join(tokens) or clean_description(raw).lower().strip()


def match_text(raw: str) -> str:
    """Lowercase, punctuation-free text used for rule matching.

    Unlike description_key this keeps short words and digits, because rules need
    to see phrases like "monthly account fee" and "apple com bill" intact.
    """
    text = clean_description(raw).lower()
    text = _NON_ALNUM.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def merchant_key(name: str) -> str:
    """Normalised key for a merchant name, used to compare slips to statements."""
    return description_key(name)


def fingerprint(account: str, date_iso: str, amount: float, desc_key: str,
                ordinal: int) -> str:
    """Stable identity for a statement line, so re-imports never double count.

    `ordinal` distinguishes genuinely repeated identical transactions on the
    same day (two identical coffees). It is the 0-based position within the
    group of identical (account, date, amount, description) lines, which is
    stable across exports as long as a statement covers whole days — the normal
    case. `spendtrack audit-duplicates` surfaces anything that slips through.
    """
    payload = f"{account}|{date_iso}|{amount:.2f}|{desc_key}|{ordinal}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def content_hash(*parts: object) -> str:
    payload = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def similarity(left: str, right: str) -> float:
    """0..1 similarity combining token overlap with character-level ratio.

    Token overlap catches "CHECKERS SANDTON" vs "CHECKERS HYPER"; the character
    ratio catches OCR damage like "CHEKERS".
    """
    a, b = (left or "").strip(), (right or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # A containment match ("woolworths" inside "woolworths food sandton") is a
    # strong signal that raw token overlap understates.
    contained = 1.0 if (a in b or b in a) else 0.0
    return max(0.6 * overlap + 0.4 * ratio, 0.85 * contained)
