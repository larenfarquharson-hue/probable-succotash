"""Rule-based categorisation of transactions and slip line items.

Every pattern from every rule goes into one flat index sorted by specificity, so
the most specific pattern wins regardless of which rule it came from — that is
how "checkers sixty60" beats "checkers" without hand-ordering the rule table.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config, normalise, rules_default, taxonomy

# Patterns that describe a *kind* of business rather than naming one. When one of
# these matches, the merchant name is taken from the description instead.
GENERIC_PATTERNS = {
    "restaurant", "bistro", "eatery", "grill", "sushi", "trattoria", "steakhouse",
    "brewery", "taproom", "gastropub", "cafe ", "caffe", "coffee", "pharmacy",
    "apteek", "gym ", "salon", "spa ", "day spa", "barber", "hair salon",
    "nail bar", "hotel", "guest house", "guesthouse", "lodge", "backpackers",
    "resort", "clinic", "hospital", "medical centre", "day hospital", "dentist",
    "dental", "doctor", "dr ", "vet ", "veterinary", "school fees", "tuition",
    "creche", "daycare", "day care", "aftercare", "after care", "tutor",
    "butchery", "bakery", "garage", "filling station", "forecourt", "fuel",
    "petrol", "diesel", "parking", "parkade", "toll", "liquor", "bottle store",
    "wine", "winery", "cellar", "tobacco", "vape", "cigarette", "take away",
    "takeaway", "drive thru", "food court", "canteen", "cafeteria", "boutique",
    "clothing", "apparel", "jewellery", "electronics", "gadget", "furniture",
    "stationery", "printing", "courier", "florist", "donation", "donate",
    "charity", "church", "mosque", "temple", "internet", "fibre", "airtime",
    "data bundle", "recharge", "electricity", "municipal", "municipality",
    "rent ", "levies", "levy", "insurance premium", "life cover",
    "medical aid", "personal loan", "home loan", "car hire", "gift ",
    "supplement", "nutrition", "pet food", "kennels", "cattery", "casino",
    "slots", "poker", "sportsbook", "cinema", "theatre", "conference",
    "seminar", "membership fee", "physio", "radiology", "psychologist",
    "vehicle finance", "vehicle instalment", "car instalment", "vehicle loan",
    "loan repayment", "loan instalment", "bond repayment", "bond instalment",
    "traffic fine", "speeding fine", "fine payment", "traffic department",
    "hospital plan", "gap cover", "funeral cover", "household insurance",
    "car insurance", "short term insurance", "monthly rent", "rental payment",
    "lease payment", "landlord", "body corporate", "estate levy",
    "school uniform", "extra lessons", "textbook", "prepaid airtime",
    "top up voucher", "whatsapp bundle", "unit trust", "retirement annuity",
    "annuity contribution", "provident fund", "fixed deposit", "notice deposit",
    "money market", "tax free savings", "savings pocket", "save to", "stokvel",
    "delivery fee", "gift card", "giftcard", "travel insurance", "visa application",
    "professional body", "annual subscription fee", "credit card payment",
    "student loan", "revolving credit", "consolidation loan",
}

# Display names for patterns that are deliberately written as loose stems.
MERCHANT_ALIASES = {
    "nando": "Nando's",
    "mcdonald": "McDonald's",
    "mcd": "McDonald's",
    "roman s pizza": "Roman's Pizza",
    "romans pizza": "Roman's Pizza",
    "domino s pizza": "Domino's Pizza",
    "dominos": "Domino's Pizza",
    "col cacchio": "Col'Cacchio",
    "tasha s": "Tashas",
    "tiger s milk": "Tiger's Milk",
    "tigers milk": "Tiger's Milk",
    "gali s": "Galito's",
    "galitos": "Galito's",
    "mugg and bean": "Mugg & Bean",
    "mugg bean": "Mugg & Bean",
    "food lover s market": "Food Lover's Market",
    "food lovers": "Food Lover's Market",
    "sportsman s warehouse": "Sportsmans Warehouse",
    "dis chem": "Dis-Chem",
    "dischem": "Dis-Chem",
    "dis-chem": "Dis-Chem",
    "apple com bill": "Apple",
    "apple services": "Apple",
    "itunes": "Apple",
    "google play": "Google Play",
    "amazon co za": "Amazon",
    "amazon com": "Amazon",
    "amzn": "Amazon",
    "levi s": "Levi's",
    "levis": "Levi's",
    "hirsch s": "Hirsch's",
    "hirschs": "Hirsch's",
    "vida e caffe": "Vida e Caffè",
    "vida e": "Vida e Caffè",
    "h m": "H&M",
    "hm za": "H&M",
    "pick n pay": "Pick n Pay",
    "picknpay": "Pick n Pay",
    "pnp": "Pick n Pay",
    "mtn za": "MTN",
    "mtn sp": "MTN",
    "cell c": "Cell C",
    "cellc": "Cell C",
    "multichoice": "DStv (MultiChoice)",
    "dstv": "DStv",
    "uber trip": "Uber",
    "uber bv": "Uber",
    "uber za": "Uber",
    "bolt request": "Bolt",
    "bolt eu": "Bolt",
    "mr d food": "Mr D Food",
    "mr d": "Mr D Food",
    "mrd food": "Mr D Food",
    "hollywood bets": "Hollywoodbets",
    "world sports betting": "World Sports Betting",
    "wsb": "World Sports Betting",
    "easy equities": "EasyEquities",
    "easyequities": "EasyEquities",
    "auto and general": "Auto & General",
    "auto general": "Auto & General",
    "e toll": "e-toll",
    "etoll": "e-toll",
    "sanral": "SANRAL",
    "spec savers": "Spec-Savers",
    "specsavers": "Spec-Savers",
    "checkers sixty60": "Checkers Sixty60",
    "checkers sixty 60": "Checkers Sixty60",
    "sixty60": "Checkers Sixty60",
    "flysafair": "FlySafair",
    "fly safair": "FlySafair",
    "booking com": "Booking.com",
    "bookingcom": "Booking.com",
    "takealot": "Takealot",
    "superbalist": "Superbalist",
    "outsurance": "OUTsurance",
    "miway": "MiWay",
    "1life": "1Life",
    "one life": "1Life",
    "avbob": "AVBOB",
    "gems": "GEMS",
    "sars": "SARS",
    "saa": "SAA",
    "unisa": "UNISA",
    "nordvpn": "NordVPN",
    "expressvpn": "ExpressVPN",
    "1password": "1Password",
    "lastpass": "LastPass",
    "openai": "OpenAI",
    "chatgpt": "OpenAI (ChatGPT)",
    "claude ai": "Anthropic (Claude)",
    "anthropic": "Anthropic",
    "github": "GitHub",
    "jetbrains": "JetBrains",
    "aws": "AWS",
    "amazon web services": "AWS",
    "digitalocean": "DigitalOcean",
    "linkedin premium": "LinkedIn Premium",
    "youtube premium": "YouTube Premium",
    "youtubepremium": "YouTube Premium",
    "disney plus": "Disney+",
    "disneyplus": "Disney+",
    "paramount plus": "Paramount+",
    "hbo max": "HBO Max",
    "apple tv": "Apple TV+",
    "apple music": "Apple Music",
    "amazon prime video": "Prime Video",
    "prime video": "Prime Video",
    "ster kinekor": "Ster-Kinekor",
    "sterkinekor": "Ster-Kinekor",
    "nu metro": "Nu Metro",
    "numetro": "Nu Metro",
    "incredible connection": "Incredible Connection",
    "hifi corp": "HiFi Corp",
    "hi fi corporation": "HiFi Corp",
    "istore": "iStore",
    "i store": "iStore",
    "mr price": "Mr Price",
    "mrp": "Mr Price",
    "tekkie town": "Tekkie Town",
    "cape union mart": "Cape Union Mart",
    "virgin active": "Virgin Active",
    "planet fitness": "Planet Fitness",
    "planetfitness": "Planet Fitness",
    "city power": "City Power",
    "eskom": "Eskom",
    "ithuba": "Ithuba (National Lottery)",
    "national lottery": "National Lottery",
    "wesbank": "WesBank",
    "petshop science": "Petshop Science",
    "absolute pets": "Absolute Pets",
    "the courier guy": "The Courier Guy",
    "postnet": "PostNet",
    "van schaik": "Van Schaik",
}

_LOCATION_NOISE = {
    "sandton", "rosebank", "randburg", "midrand", "fourways", "bryanston",
    "centurion", "pretoria", "menlyn", "woodmead", "melrose", "hyde", "park",
    "durban", "umhlanga", "ballito", "pinetown", "westville", "gateway",
    "capetown", "claremont", "kenilworth", "canalwalk", "canal", "walk",
    "tygervalley", "tyger", "valley", "bellville", "stellenbosch", "somerset",
    "west", "east", "north", "south", "city", "mall", "centre", "center",
    "plaza", "square", "crossing", "village", "junction", "gardens", "hyper",
    "express", "store", "branch", "shop", "kiosk", "za", "sa", "gp", "kzn", "wc",
    "johannesburg", "soweto", "boksburg", "benoni", "germiston", "kempton",
    "alberton", "roodepoort", "krugersdorp", "vereeniging", "polokwane",
    "nelspruit", "mbombela", "bloemfontein", "kimberley", "gqeberha",
    "port", "elizabeth", "london", "george", "knysna", "mosselbay", "paarl",
}


# Longest first, and only aliases long enough to be unambiguous as whole words.
_ALIASES_BY_LENGTH = sorted(
    (a for a in MERCHANT_ALIASES if len(a) >= 5), key=len, reverse=True
)


@dataclass
class Assignment:
    category: str = taxonomy.UNCATEGORISED
    subcategory: str | None = None
    merchant: str | None = None
    rule_id: str | None = None
    flags: list[str] = field(default_factory=list)
    source: str = "fallback"

    @property
    def is_internal(self) -> bool:
        return "internal" in self.flags


@dataclass
class _Pattern:
    text: str
    regex: re.Pattern | None
    rule_id: str
    category: str
    subcategory: str | None
    merchant: str | None
    flags: list[str]
    user: bool
    generic: bool


class Categoriser:
    """Applies user rules, then built-in rules, then a fallback."""

    def __init__(self, user_rules: list[dict] | None = None,
                 category_overrides: dict | None = None):
        if category_overrides:
            taxonomy.apply_overrides(category_overrides)
        self._patterns: list[_Pattern] = []
        self._load(user_rules or [], user=True)
        self._load(rules_default.flatten(), user=False)
        # Specificity order: user rules first, then longer patterns, then
        # regexes (which are assumed deliberate) ahead of loose substrings.
        self._patterns.sort(
            key=lambda p: (p.user, len(p.text), p.regex is not None), reverse=True
        )

    def _load(self, rules: list[dict], user: bool) -> None:
        for rule in rules:
            patterns = rule.get("patterns") or rule.get("match") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            for raw in patterns:
                text = str(raw)
                compiled = None
                if text.startswith("re:"):
                    body = text[3:]
                    try:
                        compiled = re.compile(body, re.I)
                    except re.error:
                        continue
                    text = body
                else:
                    text = text.lower()
                self._patterns.append(_Pattern(
                    text=text,
                    regex=compiled,
                    rule_id=str(rule.get("id") or rule.get("category") or "rule"),
                    category=rule.get("category") or taxonomy.UNCATEGORISED,
                    subcategory=rule.get("subcategory"),
                    merchant=rule.get("merchant"),
                    flags=list(rule.get("flags") or []),
                    user=user,
                    generic=text in GENERIC_PATTERNS,
                ))

    def classify(self, description: str, amount: float | None = None) -> Assignment:
        """Categorise one description. `amount` refines sign-dependent cases."""
        haystack = normalise.match_text(description)
        if not haystack:
            return Assignment(merchant=None)

        for pat in self._patterns:
            if pat.regex is not None:
                if not pat.regex.search(haystack):
                    continue
            elif pat.text not in haystack:
                continue

            merchant = pat.merchant
            if not merchant:
                merchant = (None if pat.generic else _titleise(pat.text))
            if not merchant:
                merchant = derive_merchant(description)
            return Assignment(
                category=pat.category,
                subcategory=pat.subcategory,
                merchant=merchant,
                rule_id=pat.rule_id,
                flags=list(pat.flags),
                source="rule",
            )

        # Inflows with no matching rule are income, not uncategorised spend.
        if amount is not None and amount > 0:
            return Assignment(category="Uncategorised", merchant=derive_merchant(description),
                              rule_id=None, source="fallback")
        return Assignment(category=taxonomy.UNCATEGORISED,
                          merchant=derive_merchant(description), source="fallback")


def _titleise(pattern: str) -> str:
    """Turn a matched brand pattern into a display label."""
    stem = " ".join(pattern.strip().lower().split())
    if stem in MERCHANT_ALIASES:
        return MERCHANT_ALIASES[stem]
    words = stem.split()
    out = []
    for word in words:
        if word in {"za", "sa", "bv", "com", "co", "uk", "eu", "kfc", "mtn",
                    "dstv", "bp", "psn", "hm", "tfg", "mrp", "saa", "sars",
                    "vat", "paye", "wsb", "hoa", "tfsa", "usn", "ecsa", "hpcsa",
                    "jmpd", "tmpd", "uif", "atm", "pnp", "gems", "aws", "vas",
                    "eft", "lte", "adsl", "vpn", "suv", "ra", "cc"}:
            out.append(word.upper())
        else:
            out.append(word.capitalize())
    return " ".join(out)


def derive_merchant(description: str) -> str | None:
    """Best-effort merchant name from a raw bank description.

    Keeps the leading words that are not obvious location noise, so
    "CHECKERS HYPER SANDTON CITY" collapses towards "Checkers".
    """
    text = normalise.match_text(description)
    if not text:
        return None
    # A known brand anywhere in the description beats positional guessing.
    padded = f" {text} "
    for alias in _ALIASES_BY_LENGTH:
        if f" {alias} " in padded:
            return MERCHANT_ALIASES[alias]
    tokens = [t for t in text.split() if not t.isdigit()]
    kept: list[str] = []
    for token in tokens:
        if token in _LOCATION_NOISE and kept:
            break
        kept.append(token)
        if len(kept) >= 3:
            break
    if not kept:
        kept = tokens[:2]
    label = " ".join(kept)[:32].strip()
    return _titleise(label) if label else None


# --------------------------------------------------------------------------
# User rule file
# --------------------------------------------------------------------------

def load_user_rules(path: Path | None = None) -> tuple[list[dict], dict]:
    """Read ~/.spendtrack/rules.json. Returns (rules, category_overrides)."""
    target = path or config.rules_path()
    if not target.exists():
        return [], {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{target} is not valid JSON: {exc}") from exc
    if isinstance(data, list):
        return data, {}
    return data.get("rules", []), data.get("categories", {})


def build(path: Path | None = None) -> Categoriser:
    rules, cats = load_user_rules(path)
    return Categoriser(rules, cats)


RULES_TEMPLATE = {
    "_comment": [
        "Your rules are tried before the built-in ones, so anything here wins.",
        "Patterns match a lowercase, punctuation-free version of the bank",
        "description with card numbers and channel words removed.",
        "Prefix a pattern with 're:' to use a regular expression.",
        "'categories' lets you retune how reducible a category is:",
        "discretion 0 = untouchable, 1 = entirely optional.",
    ],
    "rules": [
        {
            "id": "my-landlord",
            "category": "Housing & Rent",
            "merchant": "Landlord",
            "patterns": ["jones properties", "re:^rent \\w+"],
            "flags": ["subscription"],
        },
        {
            "id": "my-own-savings",
            "category": "Transfers",
            "merchant": "Own savings account",
            "patterns": ["transfer to 1234567890"],
            "flags": ["internal"],
        },
    ],
    "categories": {
        "Coffee & Snacks": {"discretion": 0.9},
    },
}


def write_template(path: Path | None = None) -> Path:
    target = path or config.rules_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(RULES_TEMPLATE, indent=2), encoding="utf-8")
    return target
