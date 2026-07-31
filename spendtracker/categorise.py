"""Merchant normalisation and rule-based categorisation.

Bank narration is noisy: masked card numbers, embedded dates, terminal IDs,
city and country suffixes, channel prefixes. Two purchases at the same shop can
look completely different as strings. Cleaning them to a stable merchant name
is what makes both the merchant summary and duplicate detection work.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# --- Noise stripped from bank narration ------------------------------------

# Masked card numbers: 1234****5678, 123456*8901, 4067********1234
_MASKED_CARD = re.compile(r"\b\d{2,6}[*x#\.]{2,}\d{2,6}\b", re.IGNORECASE)
# Bare long digit runs (terminal ids, reference numbers) - keep 4 digits or
# fewer so "SPAR 4" style store numbers survive.
_LONG_DIGITS = re.compile(r"\b\d{5,}\b")
# Embedded dates: 12/03, 12-03-2024, 2024/03/12, 12 MAR
_EMBEDDED_DATE = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}([/\-.]\d{2,4})?|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})\b"
)
_MONTH_DAY = re.compile(
    r"\b\d{1,2}\s?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.IGNORECASE
)
_TIME = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")

# Channel / instrument prefixes that describe *how* not *who*.
_PREFIXES = [
    "card purchase authorisation",
    "card purchase auth",
    "card purchase",
    "pos purchase",
    "point of sale purchase",
    "point of sale",
    "purchase at",
    "purchase",
    "payment to",
    "payment from",
    "pmt to",
    "internet banking payment to",
    "internet banking payment",
    "internet pmt to",
    "internet trf to",
    "ib payment to",
    "ib payment",
    "immediate payment to",
    "immediate payment",
    "real time clearing",
    "rtc payment",
    "external debit order",
    "internal debit order",
    "unpaid debit order",
    "debit order",
    "magtape debit",
    "magtape credit",
    "stop order",
    "acb debit",
    "acb credit",
    "eft debit",
    "eft credit",
    "eft to",
    "eft",
    "app payment to",
    "app payment",
    "app transfer to",
    "digital payment",
    "scheduled payment to",
    "recurring payment",
    "online purchase",
    "web purchase",
    "mobile payment",
    "3d secure",
    "visa purchase",
    "mastercard purchase",
    "chq card purchase",
    "cheque card purchase",
    "fnb app payment to",
    "fnb app payment from",
    "capitec pay",
    "send money",
    "prepaid purchase",
]

# Suffixes: locality and country codes that follow a merchant name.
_COUNTRY_SUFFIX = re.compile(
    r"[\s,]+(za|rsa|zaf|south africa|gb|gbr|us|usa|nl|ie|de|ae|sg|au|ca)\s*$",
    re.IGNORECASE,
)
_SA_PLACES = {
    "johannesburg", "jhb", "sandton", "randburg", "rosebank", "midrand", "roodepoort",
    "soweto", "benoni", "boksburg", "kempton park", "germiston", "alberton", "springs",
    "centurion", "pretoria", "pta", "menlyn", "hatfield", "brooklyn",
    "cape town", "cpt", "claremont", "sea point", "bellville", "durbanville",
    "stellenbosch", "somerset west", "paarl", "table view", "century city", "kenilworth",
    "durban", "dbn", "umhlanga", "ballito", "pinetown", "westville", "hillcrest",
    "pietermaritzburg", "pmb", "richards bay", "newcastle",
    "port elizabeth", "gqeberha", "east london", "bloemfontein", "kimberley",
    "polokwane", "nelspruit", "mbombela", "rustenburg", "potchefstroom", "welkom",
    "george", "knysna", "mossel bay", "worcester", "vereeniging", "vanderbijlpark",
    "gauteng", "western cape", "kwazulu natal", "kwazulu-natal", "kzn", "mpumalanga",
    "limpopo", "north west", "free state", "eastern cape", "northern cape",
}
# Store-format words that add nothing to the merchant identity.
_FORMAT_WORDS = {
    "hyper", "hypermarket", "express", "superstore", "supermarket", "liquorshop",
    "foodco", "food co", "local", "on nicol", "value", "family", "mini", "kwikspar",
    "superspar", "sixty60", "drive thru", "drivethru", "store", "branch", "ltd",
    "pty", "pty ltd", "cc", "inc", "no1", "no2",
}

_MULTISPACE = re.compile(r"\s+")
_PUNCT_EDGES = re.compile(r"^[^\w]+|[^\w]+$")
_REF_TOKEN = re.compile(r"\b(ref|reference|auth|authno|trn|txn|seq|batch|inv|id)[:#\s]*\w{2,}\b", re.IGNORECASE)


def clean_description(raw: str) -> str:
    """Strip narration noise, leaving something close to a merchant name."""
    if not raw:
        return ""
    s = raw.strip()
    s = _MASKED_CARD.sub(" ", s)
    s = _REF_TOKEN.sub(" ", s)
    s = _EMBEDDED_DATE.sub(" ", s)
    s = _MONTH_DAY.sub(" ", s)
    s = _TIME.sub(" ", s)
    s = _LONG_DIGITS.sub(" ", s)
    s = s.replace("*", " ").replace("#", " ").replace("_", " ")
    s = _MULTISPACE.sub(" ", s).strip()

    lower = s.lower()
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if lower.startswith(prefix):
                s = s[len(prefix) :].strip(" -:,")
                lower = s.lower()
                changed = True
                break

    s = _COUNTRY_SUFFIX.sub("", s).strip(" -:,")
    s = _MULTISPACE.sub(" ", s).strip()
    return s


def normalise_merchant(raw: str, *, drop_places: bool = True) -> str:
    """Produce a stable, display-ready merchant name from bank narration."""
    s = clean_description(raw)
    if not s:
        return "Unknown"

    if drop_places:
        s = _strip_trailing_places(s)

    tokens = [t for t in re.split(r"[\s/]+", s) if t]
    kept: list[str] = []
    for tok in tokens:
        bare = _PUNCT_EDGES.sub("", tok)
        if not bare:
            continue
        if bare.lower() in _FORMAT_WORDS and kept:
            continue
        kept.append(bare)
        if len(kept) >= 5:  # merchant names are short; the tail is noise
            break

    if not kept:
        return "Unknown"

    return _titlecase(" ".join(kept))


def _strip_trailing_places(s: str) -> str:
    """Remove a trailing locality, e.g. 'Woolworths Sandton City' -> keeps
    'Woolworths Sandton City' only if the place is not a bare city name."""
    lower = s.lower().strip()
    for place in sorted(_SA_PLACES, key=len, reverse=True):
        if lower.endswith(" " + place):
            candidate = s[: -(len(place) + 1)].strip(" -,")
            # Never strip the whole thing away.
            if candidate:
                return candidate
    return s


_ALL_CAPS_KEEP = {"kfc", "spar", "mtn", "dstv", "bp", "sa", "za", "tfg", "pep", "atm",
                  "uk", "usa", "vat", "sars", "pnp", "tab", "gems", "mrp", "hifi", "h&m"}


def _titlecase(s: str) -> str:
    out = []
    for word in s.split(" "):
        low = word.lower()
        if low in _ALL_CAPS_KEEP:
            out.append(low.upper())
        elif word.isupper() and len(word) <= 3:
            out.append(word)
        elif "'" in word:
            head, _, tail = word.partition("'")
            out.append(head.capitalize() + "'" + tail.lower())
        else:
            out.append(word.capitalize())
    return " ".join(out)


def canonical_key(merchant: str) -> str:
    """Matching key for a merchant name: lowercase, alphanumeric only."""
    return re.sub(r"[^a-z0-9]", "", (merchant or "").lower())


# --- Rule engine -----------------------------------------------------------


@dataclass
class Rule:
    id: int
    priority: int
    field: str
    match_type: str
    pattern: str
    category: str | None
    merchant_name: str | None
    txn_type: str | None
    is_frivolous: int | None
    source: str
    _compiled: re.Pattern | None = None

    def matches(self, description: str, merchant: str) -> bool:
        haystack = (merchant if self.field == "merchant" else description).lower()
        if not haystack:
            return False
        pat = self.pattern
        if self.match_type == "contains":
            return pat in haystack
        if self.match_type == "exact":
            return haystack.strip() == pat.strip()
        if self.match_type == "startswith":
            return haystack.startswith(pat)
        if self.match_type == "regex":
            if self._compiled is None:
                try:
                    self._compiled = re.compile(pat, re.IGNORECASE)
                except re.error:
                    self._compiled = re.compile(r"(?!)")  # never matches
            return bool(self._compiled.search(haystack))
        return False


@dataclass
class Classification:
    category: str
    category_source: str  # rule|merchant|default
    merchant_name: str
    txn_type: str | None = None
    is_frivolous: int | None = None
    rule_id: int | None = None


class Classifier:
    """Applies rules, then merchant defaults, then a fallback category.

    User rules always beat default rules at the same priority, so a correction
    you make in the UI sticks even if a shipped rule also matches.
    """

    FALLBACK_CATEGORY = "Uncategorised"

    def __init__(self, rules: list[Rule], merchant_defaults: dict[str, tuple[str | None, int | None]] | None = None):
        self.rules = sorted(rules, key=lambda r: (r.priority, 0 if r.source == "user" else 1, r.id))
        self.merchant_defaults = merchant_defaults or {}

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "Classifier":
        rules = [
            Rule(
                id=int(r["id"]),
                priority=int(r["priority"]),
                field=r["field"],
                match_type=r["match_type"],
                pattern=(r["pattern"] or "").lower(),
                category=r["category"],
                merchant_name=r["merchant_name"],
                txn_type=r["txn_type"],
                is_frivolous=r["is_frivolous"],
                source=r["source"],
            )
            for r in conn.execute("SELECT * FROM rules WHERE enabled = 1")
        ]
        defaults = {
            r["canonical"]: (r["default_category"], r["is_frivolous"])
            for r in conn.execute(
                "SELECT canonical, default_category, is_frivolous FROM merchants"
            )
        }
        return cls(rules, defaults)

    def classify(self, description_raw: str, *, amount_cents: int | None = None) -> Classification:
        """Classify one narration string.

        ``amount_cents`` is optional but improves accuracy: money coming *in*
        is income or a reversal, never a spending category, and treating it as
        one is how inflows end up polluting a spending report.
        """
        guess = normalise_merchant(description_raw)
        desc_l = (description_raw or "").lower()

        if amount_cents is not None and amount_cents > 0:
            return self._classify_inflow(desc_l, guess)

        category: str | None = None
        merchant: str | None = None
        txn_type: str | None = None
        frivolous: int | None = None
        rule_id: int | None = None

        for rule in self.rules:
            if not rule.matches(desc_l, guess):
                continue
            if category is None and rule.category:
                category = rule.category
                rule_id = rule.id
            if merchant is None and rule.merchant_name:
                merchant = rule.merchant_name
            if txn_type is None and rule.txn_type:
                txn_type = rule.txn_type
            if frivolous is None and rule.is_frivolous is not None:
                frivolous = rule.is_frivolous
            # Keep scanning: a high-priority rule may set the merchant while a
            # generic one supplies the txn_type. Stop once everything is known.
            if category and merchant and txn_type and frivolous is not None:
                break

        final_merchant = merchant or guess
        source = "rule" if category else "default"

        if category is None:
            default_cat, default_friv = self.merchant_defaults.get(
                canonical_key(final_merchant), (None, None)
            )
            if default_cat:
                category, source = default_cat, "merchant"
            if frivolous is None:
                frivolous = default_friv

        return Classification(
            category=category or self.FALLBACK_CATEGORY,
            category_source=source,
            merchant_name=final_merchant,
            txn_type=txn_type,
            is_frivolous=frivolous,
            rule_id=rule_id,
        )

    def _classify_inflow(self, desc_l: str, guess: str) -> Classification:
        """Money in: income, a refund, or a transfer from your own account."""
        from .taxonomy import INCOME_PATTERNS

        for rule in self.rules:
            # Honour explicit transfer / savings rules so an inter-account
            # movement is not reported as earnings.
            if rule.category in ("Transfers", "Savings & Investment") and rule.matches(
                desc_l, guess
            ):
                return Classification(
                    category=rule.category,
                    category_source="rule",
                    merchant_name=rule.merchant_name or guess,
                    txn_type=rule.txn_type or "transfer",
                    rule_id=rule.id,
                )

        for pattern in INCOME_PATTERNS:
            if pattern in desc_l:
                return Classification(
                    category="Income",
                    category_source="rule",
                    merchant_name=guess,
                    txn_type="credit",
                )

        return Classification(
            category="Income",
            category_source="default",
            merchant_name=guess,
            txn_type="credit",
        )
