"""Category taxonomy and the default merchant rule pack.

``kind`` drives whether spend is treated as essential, discretionary, or
excluded from spending totals altogether (transfers between your own accounts,
savings contributions, credit card repayments).

``frivolity`` is a 0-3 baseline used by the advisor:
    0 = non-negotiable
    1 = necessary but the amount is controllable
    2 = discretionary
    3 = pure want / commonly regretted

``reducible`` is the fraction of the category that a determined person could
realistically cut without changing their life materially. It is a heuristic,
deliberately conservative, and is used only to size the opportunity - never
presented as a guarantee.

The rule pack below covers common South African merchants and generic bank
narration patterns. It is a starting point, not a complete list: unmatched
merchants land in "Uncategorised" and you assign them once in the UI, which
creates a user rule that then applies to all future imports.
"""

from __future__ import annotations

import sqlite3

# name, kind, frivolity, reducible, colour, sort_order
CATEGORIES: list[tuple[str, str, int, float, str, int]] = [
    # --- Excluded from spending totals -------------------------------------
    # Only money that moves between your own pockets belongs here. Anything
    # that genuinely leaves your control must stay in the totals.
    ("Income", "income", 0, 0.0, "#15803d", 890),
    ("Transfers", "excluded", 0, 0.0, "#94a3b8", 900),
    ("Savings & Investment", "excluded", 0, 0.0, "#0ea5e9", 910),
    # Cash leaving the account IS spend. The withdrawal is what the bank saw,
    # so it is counted here and till slips paid in cash are then allocated
    # against it (see cash_allocations) to reveal *what* the cash bought
    # without adding a second outflow. Kind "cash" is included in totals but
    # reported separately so the unexplained remainder stays visible.
    ("Cash Withdrawals", "cash", 1, 0.15, "#a1a1aa", 930),
    # Paying the credit card is only a transfer if you also import the card's
    # own statement - otherwise it is the only trace of that month's card
    # spend and excluding it would understate everything. Controlled by
    # Config.credit_card_statements_imported.
    ("Credit Card Repayment", "essential", 0, 0.0, "#64748b", 920),
    # --- Essential ---------------------------------------------------------
    ("Housing", "essential", 0, 0.02, "#1d4ed8", 10),
    ("Utilities", "essential", 0, 0.10, "#2563eb", 20),
    ("Rates & Municipal", "essential", 0, 0.02, "#3b82f6", 25),
    ("Groceries", "essential", 1, 0.15, "#16a34a", 30),
    ("Transport & Fuel", "essential", 1, 0.12, "#059669", 40),
    ("Parking & Tolls", "essential", 1, 0.10, "#10b981", 45),
    ("Insurance", "essential", 0, 0.12, "#7c3aed", 50),
    ("Medical & Health", "essential", 0, 0.05, "#db2777", 60),
    ("Debt Repayment", "essential", 0, 0.0, "#b91c1c", 70),
    ("Education", "essential", 0, 0.03, "#0d9488", 80),
    ("Childcare & Kids", "essential", 0, 0.05, "#14b8a6", 85),
    ("Tax", "essential", 0, 0.0, "#525252", 90),
    ("Communications", "essential", 1, 0.25, "#0891b2", 100),
    # --- Reducible / discretionary ----------------------------------------
    ("Bank Fees", "essential", 2, 0.45, "#f59e0b", 110),
    ("Interest & Penalties", "essential", 2, 0.60, "#d97706", 115),
    ("Subscriptions & Streaming", "discretionary", 2, 0.55, "#8b5cf6", 200),
    ("Eating Out & Takeaways", "discretionary", 3, 0.55, "#ef4444", 210),
    ("Delivery Fees & Convenience", "discretionary", 3, 0.80, "#f43f5e", 215),
    ("Coffee & Snacks", "discretionary", 3, 0.70, "#c2410c", 220),
    ("Alcohol & Tobacco", "discretionary", 3, 0.55, "#9f1239", 230),
    ("Gambling & Betting", "discretionary", 3, 0.95, "#7f1d1d", 240),
    ("Entertainment & Leisure", "discretionary", 2, 0.50, "#a855f7", 250),
    ("Shopping & Clothing", "discretionary", 2, 0.45, "#e879f9", 260),
    ("Electronics & Gadgets", "discretionary", 2, 0.55, "#6366f1", 270),
    ("Home & Garden", "discretionary", 1, 0.30, "#65a30d", 280),
    ("Personal Care & Beauty", "discretionary", 2, 0.40, "#ec4899", 290),
    ("Fitness & Sport", "discretionary", 1, 0.35, "#22c55e", 300),
    ("Travel & Accommodation", "discretionary", 2, 0.40, "#0284c7", 310),
    ("Ride-hailing", "discretionary", 2, 0.40, "#047857", 320),
    ("Gifts & Donations", "discretionary", 1, 0.20, "#f472b6", 330),
    ("Pets", "discretionary", 1, 0.20, "#84cc16", 340),
    ("Professional Services", "discretionary", 1, 0.20, "#475569", 350),
    ("Fines & Penalties", "discretionary", 3, 0.90, "#991b1b", 360),
    ("Apps & Digital", "discretionary", 2, 0.55, "#818cf8", 370),
    ("Uncategorised", "discretionary", 1, 0.25, "#9ca3af", 990),
]

ESSENTIAL_KINDS = {"essential"}
EXCLUDED_KINDS = {"excluded", "income"}
CASH_KINDS = {"cash"}
# Kinds that count toward "total money spent in the period".
SPENDING_KINDS = {"essential", "discretionary", "cash"}

# Narration patterns that identify money coming in. Applied to inflow rows only.
INCOME_PATTERNS = [
    "salary", "salaris", "wages", "payroll", "bonus", "commission",
    "interest received", "credit interest", "dividend", "refund", "reversal",
    "cash back", "cashback", "rebate", "sars refund", "tax refund",
    "unemployment", "grant", "pension", "annuity payout", "rental income",
    "invoice payment", "settlement",
]

# priority, field, match_type, pattern, category, merchant_name, txn_type, is_frivolous
# Lower priority number wins. Specific merchants sit at 10-40; generic bank
# narration patterns sit at 60+ so a merchant match always beats them.
Rule = tuple[int, str, str, str, str | None, str | None, str | None, int | None]

DEFAULT_RULES: list[Rule] = []


def _r(
    priority: int,
    pattern: str,
    category: str | None,
    merchant: str | None = None,
    *,
    match_type: str = "contains",
    field: str = "description",
    txn_type: str | None = None,
    frivolous: int | None = None,
) -> None:
    DEFAULT_RULES.append(
        (priority, field, match_type, pattern, category, merchant, txn_type, frivolous)
    )


# --- Groceries -------------------------------------------------------------
for pat, name in [
    ("checkers sixty60", "Checkers Sixty60"),
    ("sixty60", "Checkers Sixty60"),
    ("checkers", "Checkers"),
    ("shoprite", "Shoprite"),
    ("pick n pay", "Pick n Pay"),
    ("pick 'n pay", "Pick n Pay"),
    ("picknpay", "Pick n Pay"),
    ("pnp ", "Pick n Pay"),
    ("woolworths", "Woolworths"),
    ("woolies", "Woolworths"),
    ("spar", "SPAR"),
    ("food lover", "Food Lover's Market"),
    ("makro", "Makro"),
    ("boxer super", "Boxer"),
    ("usave", "Usave"),
    ("fruit & veg", "Fruit & Veg City"),
    ("ok foods", "OK Foods"),
    ("cambridge food", "Cambridge Food"),
]:
    _r(20, pat, "Groceries", name)

# --- Eating out / takeaways ------------------------------------------------
for pat, name in [
    ("uber eats", "Uber Eats"),
    ("ubereats", "Uber Eats"),
    ("mr d food", "Mr D Food"),
    ("mrd food", "Mr D Food"),
    ("bolt food", "Bolt Food"),
    ("kfc", "KFC"),
    ("mcdonald", "McDonald's"),
    ("nando", "Nando's"),
    ("steers", "Steers"),
    ("debonairs", "Debonairs Pizza"),
    ("roman's pizza", "Roman's Pizza"),
    ("romans pizza", "Roman's Pizza"),
    ("wimpy", "Wimpy"),
    ("spur ", "Spur"),
    ("ocean basket", "Ocean Basket"),
    ("panarottis", "Panarottis"),
    ("john dory", "John Dory's"),
    ("burger king", "Burger King"),
    ("chicken licken", "Chicken Licken"),
    ("fishaways", "Fishaways"),
    ("simply asia", "Simply Asia"),
    ("kauai", "Kauai"),
    ("rocomamas", "RocoMamas"),
    ("mugg & bean", "Mugg & Bean"),
    ("mugg and bean", "Mugg & Bean"),
    ("col'cacchio", "Col'Cacchio"),
    ("nikos", "Nikos"),
    ("news cafe", "News Cafe"),
    ("doppio zero", "Doppio Zero"),
    ("tashas", "Tashas"),
    ("life grand", "Life Grand Cafe"),
]:
    _r(20, pat, "Eating Out & Takeaways", name, frivolous=1)

# --- Coffee & snacks -------------------------------------------------------
for pat, name in [
    ("vida e caffe", "Vida e Caffè"),
    ("vida e caff", "Vida e Caffè"),
    ("starbucks", "Starbucks"),
    ("seattle coffee", "Seattle Coffee Co"),
    ("bootlegger", "Bootlegger Coffee"),
    ("krispy kreme", "Krispy Kreme"),
    ("dunkin", "Dunkin'"),
]:
    _r(20, pat, "Coffee & Snacks", name, frivolous=1)

# --- Alcohol & tobacco -----------------------------------------------------
for pat, name in [
    ("tops at spar", "TOPS at SPAR"),
    ("tops ", "TOPS at SPAR"),
    ("liquor city", "Liquor City"),
    ("ultra liquor", "Ultra Liquors"),
    ("makro liquor", "Makro Liquor"),
    ("norman goodfellow", "Norman Goodfellows"),
    ("bottle store", "Bottle Store"),
    ("checkers liquor", "Checkers LiquorShop"),
    ("pnp liquor", "Pick n Pay Liquor"),
]:
    _r(15, pat, "Alcohol & Tobacco", name, frivolous=1)

# --- Gambling --------------------------------------------------------------
for pat, name in [
    ("hollywoodbets", "Hollywoodbets"),
    ("betway", "Betway"),
    ("sportingbet", "Sportingbet"),
    ("supabets", "Supabets"),
    ("world sports betting", "World Sports Betting"),
    ("easybet", "Easybet"),
    ("lottostar", "LottoStar"),
    ("ithuba", "Ithuba National Lottery"),
    ("sunbet", "SunBet"),
    ("tab ", "TAB"),
]:
    _r(10, pat, "Gambling & Betting", name, frivolous=1)

# --- Fuel & transport ------------------------------------------------------
for pat, name in [
    ("engen", "Engen"),
    ("shell ", "Shell"),
    ("bp ", "BP"),
    ("sasol", "Sasol"),
    ("total ", "TotalEnergies"),
    ("caltex", "Caltex"),
    ("astron", "Astron Energy"),
    ("puma energy", "Puma Energy"),
    ("filling station", "Filling Station"),
    ("garage", "Fuel Station"),
]:
    _r(25, pat, "Transport & Fuel", name)

for pat, name in [
    ("uber trip", "Uber"),
    ("uber bv", "Uber"),
    ("uber sa", "Uber"),
    ("bolt.eu", "Bolt"),
    ("bolt request", "Bolt"),
    ("indrive", "inDrive"),
    ("didi", "DiDi"),
]:
    _r(15, pat, "Ride-hailing", name)

for pat, name in [
    ("gautrain", "Gautrain"),
    ("sanral", "SANRAL e-toll"),
    ("e-toll", "SANRAL e-toll"),
    ("etoll", "SANRAL e-toll"),
    ("parking", "Parking"),
    ("parkade", "Parking"),
    ("bakwena", "Bakwena Toll"),
    ("toll plaza", "Toll"),
    ("n3tc", "N3TC Toll"),
]:
    _r(20, pat, "Parking & Tolls", name)

# --- Communications --------------------------------------------------------
for pat, name in [
    ("vodacom", "Vodacom"),
    ("mtn ", "MTN"),
    ("telkom", "Telkom"),
    ("cell c", "Cell C"),
    ("cellc", "Cell C"),
    ("rain ", "Rain"),
    ("afrihost", "Afrihost"),
    ("webafrica", "Webafrica"),
    ("vumatel", "Vumatel"),
    ("openserve", "Openserve"),
    ("mweb", "MWEB"),
    ("supersonic", "Supersonic"),
    ("cool ideas", "Cool Ideas"),
    ("airtime", "Airtime"),
]:
    _r(20, pat, "Communications", name)

# --- Subscriptions & digital ----------------------------------------------
for pat, name in [
    ("netflix", "Netflix"),
    ("showmax", "Showmax"),
    ("dstv", "DStv"),
    ("multichoice", "MultiChoice"),
    ("spotify", "Spotify"),
    ("apple music", "Apple Music"),
    ("apple.com/bill", "Apple"),
    ("itunes", "Apple"),
    ("google play", "Google Play"),
    ("youtube premium", "YouTube Premium"),
    ("amazon prime", "Amazon Prime"),
    ("disney", "Disney+"),
    ("audible", "Audible"),
    ("microsoft 365", "Microsoft 365"),
    ("adobe", "Adobe"),
    ("dropbox", "Dropbox"),
    ("icloud", "Apple iCloud"),
    ("canva", "Canva"),
    ("linkedin premium", "LinkedIn Premium"),
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("chatgpt", "OpenAI"),
]:
    _r(15, pat, "Subscriptions & Streaming", name, frivolous=None)

for pat, name in [
    ("steam games", "Steam"),
    ("steampowered", "Steam"),
    ("playstation", "PlayStation Store"),
    ("xbox", "Xbox"),
    ("nintendo", "Nintendo"),
    ("epic games", "Epic Games"),
    ("app store", "Apple App Store"),
]:
    _r(15, pat, "Apps & Digital", name, frivolous=1)

# --- Retail / shopping -----------------------------------------------------
for pat, name in [
    ("takealot", "Takealot"),
    ("superbalist", "Superbalist"),
    ("mr price", "Mr Price"),
    ("mrp ", "Mr Price"),
    ("truworths", "Truworths"),
    ("foschini", "Foschini"),
    ("tfg ", "TFG"),
    ("edgars", "Edgars"),
    ("ackermans", "Ackermans"),
    ("pep ", "PEP"),
    ("h&m", "H&M"),
    ("cotton on", "Cotton On"),
    ("zara", "Zara"),
    ("sportscene", "Sportscene"),
    ("totalsports", "Totalsports"),
    ("cape union", "Cape Union Mart"),
    ("shein", "Shein"),
    ("temu", "Temu"),
]:
    _r(20, pat, "Shopping & Clothing", name)

for pat, name in [
    ("incredible connection", "Incredible Connection"),
    ("game store", "Game"),
    ("hifi corp", "HiFi Corp"),
    ("hi-fi corp", "HiFi Corp"),
    ("istore", "iStore"),
    ("dion wired", "Dion Wired"),
    ("evetech", "Evetech"),
    ("wootware", "Wootware"),
]:
    _r(20, pat, "Electronics & Gadgets", name)

for pat, name in [
    ("builders warehouse", "Builders Warehouse"),
    ("builders express", "Builders Express"),
    ("leroy merlin", "Leroy Merlin"),
    ("mica", "Mica"),
    ("cashbuild", "Cashbuild"),
    ("@home", "@home"),
    ("mr price home", "Mr Price Home"),
    ("sheet street", "Sheet Street"),
]:
    _r(20, pat, "Home & Garden", name)

# --- Health & personal care -----------------------------------------------
for pat, name in [
    ("clicks", "Clicks"),
    ("dis-chem", "Dis-Chem"),
    ("dischem", "Dis-Chem"),
    ("alpha pharm", "Alpha Pharm"),
    ("pharmacy", "Pharmacy"),
    ("netcare", "Netcare"),
    ("mediclinic", "Mediclinic"),
    ("life healthcare", "Life Healthcare"),
    ("pathcare", "PathCare"),
    ("lancet", "Lancet Laboratories"),
    ("ampath", "Ampath"),
    ("dr ", "Medical Practitioner"),
    ("dentist", "Dentist"),
    ("optometrist", "Optometrist"),
    ("spec-savers", "Spec-Savers"),
]:
    _r(25, pat, "Medical & Health", name)

for pat, name in [
    ("sorbet", "Sorbet"),
    ("barber", "Barber"),
    ("hair salon", "Hair Salon"),
    ("nail bar", "Nail Bar"),
    ("spa ", "Spa"),
]:
    _r(22, pat, "Personal Care & Beauty", name, frivolous=1)

# --- Fitness ---------------------------------------------------------------
for pat, name in [
    ("virgin active", "Virgin Active"),
    ("planet fitness", "Planet Fitness"),
    ("crossfit", "CrossFit"),
    ("run/walk for life", "Run/Walk for Life"),
    ("strava", "Strava"),
    ("discovery vitality", "Discovery Vitality"),
]:
    _r(20, pat, "Fitness & Sport", name)

# --- Insurance & financial services ---------------------------------------
for pat, name in [
    ("outsurance", "OUTsurance"),
    ("miway", "MiWay"),
    ("king price", "King Price Insurance"),
    ("santam", "Santam"),
    ("old mutual", "Old Mutual"),
    ("sanlam", "Sanlam"),
    ("momentum", "Momentum"),
    ("liberty", "Liberty"),
    ("hollard", "Hollard"),
    ("1life", "1Life"),
    ("clientele", "Clientèle"),
    ("avbob", "AVBOB"),
    ("budget insurance", "Budget Insurance"),
    ("dial direct", "Dialdirect"),
    ("naked insurance", "Naked Insurance"),
    ("pineapple", "Pineapple Insurance"),
]:
    _r(15, pat, "Insurance", name)

for pat, name in [
    ("discovery health", "Discovery Health"),
    ("bonitas", "Bonitas"),
    ("momentum health", "Momentum Health"),
    ("bestmed", "Bestmed"),
    ("fedhealth", "Fedhealth"),
    ("gems ", "GEMS"),
    ("medihelp", "Medihelp"),
]:
    _r(14, pat, "Medical & Health", name)

for pat, name in [
    ("netstar", "Netstar"),
    ("tracker connect", "Tracker"),
    ("cartrack", "Cartrack"),
]:
    _r(20, pat, "Insurance", name)

# --- Utilities / municipal -------------------------------------------------
for pat, name in [
    ("eskom", "Eskom"),
    ("prepaid electricity", "Prepaid Electricity"),
    ("electricity", "Electricity"),
    ("city of johannesburg", "City of Johannesburg"),
    ("city of cape town", "City of Cape Town"),
    ("city of tshwane", "City of Tshwane"),
    ("ethekwini", "eThekwini Municipality"),
    ("ekurhuleni", "Ekurhuleni Municipality"),
    ("nelson mandela bay", "Nelson Mandela Bay Municipality"),
    ("municipality", "Municipality"),
    ("rand water", "Rand Water"),
]:
    _r(20, pat, "Utilities", name)

_r(18, "rates and taxes", "Rates & Municipal", "Municipal Rates")
_r(18, "refuse removal", "Rates & Municipal", "Municipal Refuse")

# --- Housing ---------------------------------------------------------------
for pat, name in [
    ("bond repayment", "Home Loan"),
    ("home loan", "Home Loan"),
    ("homeloan", "Home Loan"),
    ("rent ", "Rent"),
    ("levy", "Body Corporate Levy"),
    ("levies", "Body Corporate Levy"),
    ("body corporate", "Body Corporate Levy"),
]:
    _r(18, pat, "Housing", name)

# --- Tax, education, childcare --------------------------------------------
_r(12, "sars", "Tax", "SARS")
_r(12, "south african revenue", "Tax", "SARS")
for pat, name in [
    ("school fees", "School Fees"),
    ("tuition", "Tuition"),
    ("university of", "University"),
    ("unisa", "UNISA"),
    ("crawford", "Crawford International"),
    ("curro", "Curro"),
    ("stadio", "STADIO"),
]:
    _r(18, pat, "Education", name)
for pat, name in [
    ("creche", "Creche"),
    ("aftercare", "Aftercare"),
    ("day care", "Day Care"),
]:
    _r(18, pat, "Childcare & Kids", name)

# --- Pets ------------------------------------------------------------------
for pat, name in [
    ("vet ", "Veterinarian"),
    ("veterinary", "Veterinarian"),
    ("absolute pets", "Absolute Pets"),
    ("pet zone", "Pet Zone"),
    ("petshop", "Pet Shop"),
]:
    _r(22, pat, "Pets", name)

# --- Travel ----------------------------------------------------------------
for pat, name in [
    ("booking.com", "Booking.com"),
    ("airbnb", "Airbnb"),
    ("flysafair", "FlySafair"),
    ("kulula", "kulula"),
    ("british airways", "British Airways"),
    ("emirates", "Emirates"),
    ("lift airline", "LIFT"),
    ("travelstart", "Travelstart"),
    ("cheapflights", "Cheapflights"),
    ("avis", "Avis"),
    ("hertz", "Hertz"),
    ("europcar", "Europcar"),
]:
    _r(18, pat, "Travel & Accommodation", name)

# --- Entertainment ---------------------------------------------------------
for pat, name in [
    ("ster-kinekor", "Ster-Kinekor"),
    ("nu metro", "Nu Metro"),
    ("computicket", "Computicket"),
    ("quicket", "Quicket"),
    ("sun international", "Sun International"),
    ("gold reef city", "Gold Reef City"),
    ("ushaka", "uShaka Marine World"),
]:
    _r(20, pat, "Entertainment & Leisure", name, frivolous=1)

# --- Donations -------------------------------------------------------------
for pat, name in [
    ("gift of the givers", "Gift of the Givers"),
    ("donation", "Donation"),
    ("tithe", "Tithe"),
    ("church", "Church"),
    ("unicef", "UNICEF"),
]:
    _r(22, pat, "Gifts & Donations", name)

# --- Fines -----------------------------------------------------------------
for pat, name in [
    ("traffic fine", "Traffic Fine"),
    ("aarto", "AARTO Fine"),
    ("speeding fine", "Traffic Fine"),
    ("municipal fine", "Municipal Fine"),
]:
    _r(15, pat, "Fines & Penalties", name, frivolous=1)

# --- Generic bank narration (lower priority) -------------------------------
_r(60, "atm cash withdrawal", "Cash Withdrawals", "ATM Withdrawal", txn_type="atm")
_r(60, "cash withdrawal", "Cash Withdrawals", "Cash Withdrawal", txn_type="atm")
_r(60, "atm withdrawal", "Cash Withdrawals", "ATM Withdrawal", txn_type="atm")
_r(61, "autobank cash", "Cash Withdrawals", "ATM Withdrawal", txn_type="atm")
_r(61, "cash send", "Cash Withdrawals", "Cash Send", txn_type="atm")
_r(61, "ewallet", "Cash Withdrawals", "eWallet Send", txn_type="atm")
_r(61, "e-wallet", "Cash Withdrawals", "eWallet Send", txn_type="atm")
_r(62, "withdrawal atm", "Cash Withdrawals", "ATM Withdrawal", txn_type="atm")

for pat in [
    "service fee",
    "monthly account fee",
    "monthly fee",
    "admin fee",
    "bank charge",
    "card fee",
    "cash handling fee",
    "declined transaction fee",
    "unpaid fee",
    "honouring fee",
    "immediate payment fee",
    "payment notification fee",
    "sms notification fee",
    "atm fee",
    "overdraft fee",
    "management fee",
    "ledger fee",
]:
    _r(65, pat, "Bank Fees", "Bank Charges", txn_type="fee")

_r(66, "interest charged", "Interest & Penalties", "Interest Charged", txn_type="interest")
_r(66, "debit interest", "Interest & Penalties", "Interest Charged", txn_type="interest")
_r(66, "overdraft interest", "Interest & Penalties", "Interest Charged", txn_type="interest")

for pat in ["transfer to", "transfer from", "internal transfer", "own account", "inter-account"]:
    _r(70, pat, "Transfers", "Internal Transfer", txn_type="transfer")

for pat, name in [
    ("credit card payment", "Credit Card Repayment"),
    ("cc repayment", "Credit Card Repayment"),
    ("card repayment", "Credit Card Repayment"),
]:
    _r(68, pat, "Credit Card Repayment", name, txn_type="transfer")

for pat, name in [
    ("unit trust", "Unit Trust"),
    ("tax free savings", "Tax Free Savings"),
    ("tfsa", "Tax Free Savings"),
    ("easyequities", "EasyEquities"),
    ("retirement annuity", "Retirement Annuity"),
    ("provident fund", "Provident Fund"),
    ("stokvel", "Stokvel"),
    ("10x invest", "10X Investments"),
    ("allan gray", "Allan Gray"),
    ("coronation fund", "Coronation"),
    ("satrix", "Satrix"),
]:
    _r(30, pat, "Savings & Investment", name)

for pat, name in [
    ("personal loan", "Personal Loan"),
    ("vehicle finance", "Vehicle Finance"),
    ("instalment sale", "Vehicle Finance"),
    ("wesbank", "WesBank"),
    ("mfc ", "MFC Vehicle Finance"),
    ("student loan", "Student Loan"),
    ("loan repayment", "Loan Repayment"),
]:
    _r(30, pat, "Debt Repayment", name)

# Transaction-type hints that do not imply a category.
for pat, ttype in [
    ("debit order", "debit_order"),
    ("do ref", "debit_order"),
    ("stop order", "debit_order"),
    ("card purchase", "card"),
    ("pos purchase", "card"),
    ("point of sale", "card"),
    ("purchase ", "card"),
    ("magtape", "eft"),
    ("acb ", "eft"),
    ("eft ", "eft"),
    ("payment to", "eft"),
    ("internet pmt", "eft"),
    ("ib payment", "eft"),
    ("immediate payment", "eft"),
    ("real time clearing", "eft"),
]:
    _r(95, pat, None, None, txn_type=ttype)


def seed(conn: sqlite3.Connection) -> None:
    """Insert categories and default rules. Idempotent; never overwrites
    user rules or user-edited category settings."""
    for name, kind, frivolity, reducible, colour, order in CATEGORIES:
        conn.execute(
            "INSERT INTO categories(name, kind, frivolity, colour, sort_order) "
            "VALUES(?,?,?,?,?) ON CONFLICT(name) DO NOTHING",
            (name, kind, frivolity, colour, order),
        )
    for priority, field, match_type, pattern, category, merchant, ttype, friv in DEFAULT_RULES:
        conn.execute(
            "INSERT INTO rules(priority, field, match_type, pattern, category, "
            "merchant_name, txn_type, is_frivolous, source) "
            "VALUES(?,?,?,?,?,?,?,?,'default') "
            "ON CONFLICT(field, match_type, pattern, source) DO NOTHING",
            (priority, field, match_type, pattern.lower(), category, merchant, ttype, friv),
        )
    conn.commit()


_REDUCIBLE = {name: reducible for name, _k, _f, reducible, _c, _o in CATEGORIES}
_KIND = {name: kind for name, kind, _f, _r2, _c, _o in CATEGORIES}
_FRIVOLITY = {name: friv for name, _k, friv, _r2, _c, _o in CATEGORIES}
_COLOUR = {name: colour for name, _k, _f, _r2, colour, _o in CATEGORIES}


def reducible_fraction(category: str | None) -> float:
    return _REDUCIBLE.get(category or "Uncategorised", 0.25)


def category_kind(category: str | None) -> str:
    return _KIND.get(category or "Uncategorised", "discretionary")


def category_frivolity(category: str | None) -> int:
    return _FRIVOLITY.get(category or "Uncategorised", 1)


def category_colour(category: str | None) -> str:
    return _COLOUR.get(category or "Uncategorised", "#9ca3af")


def is_excluded(category: str | None, *, credit_cards_imported: bool = False) -> bool:
    """True for movements of your own money that must not count as spending.

    ``credit_cards_imported`` flips credit card repayments from spend (the only
    visible trace of that card's purchases) to a transfer (because the card's
    own statement now supplies the detail, and counting both would double).
    """
    if credit_cards_imported and category == "Credit Card Repayment":
        return True
    return category_kind(category) == "excluded"


def counts_as_spend(category: str | None, *, credit_cards_imported: bool = False) -> bool:
    """True if this category belongs in the period spending total."""
    return not is_excluded(category, credit_cards_imported=credit_cards_imported)
