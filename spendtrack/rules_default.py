"""Built-in categorisation rules, weighted towards South African merchants.

Each entry is (rule_id, category, merchant_label, patterns, flags).

`patterns` are matched against the *normalised* description key produced by
normalise.description_key() — lowercase, no card masks, no store numbers — so
"CARD PURCHASE 4123****1234 CHECKERS HYPER SANDTON 12 JAN" is tested as
"checkers hyper sandton".

A pattern is a plain substring unless it starts with "re:", in which case the
remainder is a regular expression. Longer, more specific patterns are tried
first, so "checkers sixty60" beats "checkers".

`flags` may contain:
  subscription — recurring by nature, so a missing month is worth noticing
  internal     — movement between the user's own accounts
  premium      — carries an avoidable convenience premium (delivery, ATM draw)

This list is deliberately editable. Add your own in ~/.spendtrack/rules.json;
user rules are tried before these.
"""

from __future__ import annotations

# fmt: off
DEFAULT_RULES: list[tuple[str, str, str, list[str], list[str]]] = [

    # ---------------- Bank charges, interest, fees ------------------------
    ("bank-fee", "Bank Charges & Fees", "Bank charges", [
        "monthly account fee", "monthly service fee", "service fee", "admin fee",
        "account maintenance fee", "monthly fee", "bank charges", "bank charge",
        "cash deposit fee", "cash withdrawal fee", "atm fee", "declined fee",
        "unpaid fee", "unsuccessful debit order fee", "honouring fee",
        "dishonoured fee", "card fee", "card replacement", "stop payment fee",
        "immediate payment fee", "payment notification fee", "notification fee",
        "sms notification", "incontact", "in contact", "statement fee",
        "branch fee", "teller fee", "pos device fee", "value added service fee",
        "vas fee", "electronic payment fee", "eft fee", "debit order fee",
        "balance enquiry", "mini statement", "cheque fee", "overdraft fee",
        "facility fee", "initiation fee", "monthly admin", "transaction fee",
        "prepaid airtime fee", "foreign exchange fee", "international transaction fee",
        "currency conversion fee", "re:^fee\\b", "re:\\bfee$",
    ], []),
    ("bank-interest", "Interest & Penalties", "Interest & penalties", [
        "interest charged", "debit interest", "overdraft interest",
        "interest on overdraft", "penalty interest", "late payment",
        "late payment fee", "arrears", "penalty fee", "penalty charge",
        "interest and fees", "finance charge", "finance charges",
    ], []),

    # ---------------- Groceries -------------------------------------------
    ("groceries", "Groceries", None, [
        "checkers hyper", "checkers", "shoprite", "usave",
        "pick n pay", "picknpay", "pnp express", "pnp crn", "pnp ",
        "woolworths food", "woolworths", "woolies",
        "spar", "superspar", "kwikspar", "savemor",
        "food lovers", "food lover s market", "fruit and veg city",
        "makro", "cambridge food", "boxer", "ok foods", "okfoods", "ok grocer",
        "choppies", "game food", "president hyper", "elite cash and carry",
        "rhino cash", "trade centre", "the fruit", "farmers market",
        "butchery", "biltong", "bakery", "greengrocer", "dischem food",
    ], []),
    ("groceries-online", "Groceries", None, [
        "checkers sixty60", "checkers sixty 60", "sixty60", "woolies dash",
        "woolworths dash", "pnp asap", "onecart", "yebo fresh",
        "faithful to nature",
    ], ["premium"]),

    # ---------------- Household / pharmacy --------------------------------
    ("pharmacy", "Medical & Healthcare", None, [
        "clicks", "dis chem", "dischem", "dis-chem", "alpha pharm", "medirite",
        "link pharmacy", "pharmacy", "apteek", "sparpharmacy", "local choice",
    ], []),
    ("household", "Household Supplies", None, [
        "west pack", "westpack", "crazy store", "pep home", "sheet street",
        "mr price home", "home etc", "hirsch s", "hirschs", "dunns home",
    ], []),
    ("home-garden", "Home & Garden", None, [
        "builders warehouse", "builders express", "builders", "cashbuild",
        "leroy merlin", "chamberlain", "brights hardware", "timbercity",
        "italtile", "tile africa", "garden centre", "stodels", "starke ayres",
        "lifestyle home garden", "coricraft", "furniture", "wetherlys",
        "bradlows", "russells", "rochester", "decofurn", "the crazy store",
    ], []),

    # ---------------- Fuel and transport ----------------------------------
    ("fuel", "Transport & Fuel", None, [
        "engen", "shell ", "shell", "bp ", "bp southern", "sasol", "total ",
        "totalenergies", "caltex", "astron", "puma energy", "viva ", "zenex",
        "petroport", "ultra city", "garage", "filling station", "forecourt",
        "fuel", "petrol", "diesel",
    ], []),
    ("ride-hailing", "Ride Hailing", None, [
        "uber trip", "uber bv", "uber za", "uber", "bolt request", "bolt eu",
        "bolt", "indrive", "in drive", "didi", "wanatu", "taxify",
        "gautrain", "metrorail", "myciti", "rea vaya", "bus fare",
        "intercape", "greyhound", "citiliner",
    ], []),
    ("tolls-parking", "Tolls & Parking", None, [
        "sanral", "e toll", "etoll", "bakwena", "n3tc", "trac n4", "toll",
        "parking", "parkade", "pay by parking", "parkupp", "safe parking",
    ], []),
    ("vehicle", "Vehicle & Maintenance", None, [
        "tiger wheel", "tyres", "tyre", "supa quick", "hi q", "hi-q", "midas",
        "goldwagen", "autozone", "auto zone", "car service", "panel beater",
        "licence disc", "natis", "vehicle licence", "roadworthy", "car wash",
        "carwash", "dents", "windscreen", "glassfit", "pg glass",
        "battery centre", "battery clinic",
    ], []),
    ("vehicle-finance", "Vehicle Finance", None, [
        "vehicle finance", "vaf ", "wesbank", "mfc a division", "mfc nedbank",
        "toyota financial", "vw financial", "bmw financial", "mercedes benz financial",
        "absa vehicle", "car instalment", "vehicle instalment", "vehicle loan",
    ], ["subscription"]),
    ("fines", "Fines & Traffic", None, [
        "traffic fine", "aarto", "traffic department", "jmpd", "tmpd",
        "metro police", "speeding fine", "fine payment", "payfine", "fines sa",
    ], []),

    # ---------------- Eating out and takeaways ----------------------------
    ("food-delivery", "Food Delivery", None, [
        "uber eats", "ubereats", "mr d food", "mr d ", "mrd food",
        "bolt food", "boltfood", "checkers sixty 60", "delivery fee",
        "orderin", "quench", "driveby",
    ], ["premium"]),
    ("fast-food", "Takeaways & Fast Food", None, [
        "kfc", "mcdonald", "mcd ", "steers", "debonairs", "roman s pizza",
        "romans pizza", "chicken licken", "nando", "burger king", "wimpy",
        "fishaways", "milky lane", "simply asia", "panarottis", "pedros",
        "galitos", "gali s", "hungry lion", "captain do", "spur",
        "dominos", "domino s pizza", "pizza hut", "scooters pizza",
        "andiccio", "col cacchio", "sausage saloon", "king pie", "pie city",
        "chesa nyama", "shisanyama", "take away", "takeaway", "drive thru",
    ], []),
    ("eating-out", "Eating Out", None, [
        "ocean basket", "mugg and bean", "mugg bean", "tashas", "tasha s",
        "rocomamas", "roco mamas", "cape town fish market", "the hussar",
        "bootleggers", "tiger s milk", "tigers milk", "hudsons", "turn n tender",
        "the grillhouse", "life grand cafe", "doppio zero", "primi", "primi piatti",
        "news cafe", "cappuccino s", "restaurant", "bistro", "eatery", "grill",
        "sushi", "trattoria", "steakhouse", "brewery", "taproom", "gastropub",
        "kauai", "salad bar", "food court", "canteen", "cafeteria",
    ], []),
    ("coffee-snacks", "Coffee & Snacks", None, [
        "vida e caffe", "vida e", "seattle coffee", "starbucks", "bootlegger coffee",
        "coffee", "cafe ", "caffe", "krispy kreme", "dunkin", "cinnabon",
        "sweet ", "candy", "ice cream", "gelato", "paul s homemade",
        "woolworths cafe", "on the run", "freshstop", "fresh stop", "quickshop",
        "quick shop", "pick n pay express", "kwikshop", "vending",
    ], []),
    ("alcohol", "Alcohol & Tobacco", None, [
        "tops at spar", "tops at", "tops ", "ultra liquors", "liquor",
        "liquorland", "drankwinkel", "checkers liquor", "pnp liquor",
        "makro liquor", "norman goodfellows", "wine", "winery", "cellar",
        "bottle store", "brewhouse", "tobacco", "vape", "twisp", "juul",
        "cigarette", "hookah", "hubbly",
    ], []),

    # ---------------- Subscriptions ---------------------------------------
    ("streaming", "Streaming & Subscriptions", None, [
        "netflix", "showmax", "disney plus", "disneyplus", "amazon prime video",
        "prime video", "apple tv", "youtube premium", "youtubepremium",
        "spotify", "apple music", "deezer", "tidal", "audible", "sirius",
        "multichoice", "dstv", "dstv stream", "supersport", "viu ", "britbox",
        "mubi", "crunchyroll", "paramount plus", "hbo max",
    ], ["subscription"]),
    ("apps-software", "Apps & Software", None, [
        "apple com bill", "apple services", "itunes", "google play",
        "google storage", "google one", "google workspace", "microsoft",
        "office 365", "microsoft 365", "adobe", "canva", "dropbox", "icloud",
        "notion", "openai", "chatgpt", "anthropic", "claude ai", "midjourney",
        "github", "jetbrains", "slack", "zoom", "grammarly", "nordvpn",
        "expressvpn", "surfshark", "lastpass", "1password", "namecheap",
        "godaddy", "afrihost hosting", "aws ", "amazon web services",
        "digitalocean", "heroku", "linkedin premium", "duolingo",
    ], ["subscription"]),
    ("gaming", "Gaming", None, [
        "steam games", "steampowered", "valve corp", "playstation", "psn ",
        "xbox", "microsoft games", "nintendo", "epic games", "riot games",
        "battle net", "blizzard", "roblox", "supercell", "garena",
        "ea sports", "ubisoft", "rockstar games",
    ], []),
    ("entertainment", "Entertainment & Events", None, [
        "ster kinekor", "sterkinekor", "nu metro", "numetro", "cinema",
        "computicket", "webtickets", "quicket", "howler", "ticketpro",
        "theatre", "monte casino", "montecasino", "gold reef", "emperors palace",
        "sun international", "grandwest", "silverstar", "time square",
        "escape room", "bowling", "acrobranch", "ratanga", "gold restaurant",
    ], []),
    ("gambling", "Gambling & Betting", None, [
        "betway", "hollywoodbets", "hollywood bets", "supabets", "sunbet",
        "world sports betting", "wsb ", "easybet", "gbets", "playabets",
        "betxchange", "sportingbet", "lottostar", "yesplay", "10bet",
        "bet com", "sportsbook", "casino", "slots", "poker",
    ], []),
    ("lottery", "Lottery", None, [
        "ithuba", "national lottery", "lotto", "powerball", "sportstake",
    ], []),

    # ---------------- Retail ----------------------------------------------
    ("online-shopping", "Online Shopping", None, [
        "takealot", "superbalist", "bash com", "zando", "amazon co za",
        "amazon com", "amzn", "aliexpress", "shein", "temu", "wish com",
        "bidorbuy", "loot co za", "loot", "yuppiechef", "netflorist",
        "onedayonly", "one day only", "wantitall", "raru", "leroy",
        "makro online", "ebay", "etsy", "asos", "next co uk",
    ], []),
    ("clothing", "Clothing & Apparel", None, [
        "mr price", "mrp ", "ackermans", "pep stores", "pep ", "truworths",
        "foschini", "tfg ", "markham", "exact", "sportscene", "totalsports",
        "sportsmans warehouse", "sportsman s warehouse", "cape union mart",
        "old khaki", "poetry", "queenspark", "jet stores", "jet ",
        "cotton on", "h m ", "hm za", "zara", "superdry", "levis", "levi s",
        "adidas", "nike", "puma store", "new balance", "shoe city", "tekkie town",
        "rage shoes", "identity", "legit", "donna claire", "edgars", "clothing",
        "boutique", "apparel", "the fix", "typo", "american swiss", "sterns",
        "galaxy co", "jewellery",
    ], []),
    ("electronics", "Electronics & Gadgets", None, [
        "incredible connection", "hifi corp", "hi fi corporation", "istore",
        "i store", "digicape", "core group", "evetech", "wootware", "rebeltech",
        "computer mania", "matrix warehouse", "game stores", "game za",
        "dion wired", "samsung", "huawei store", "cellucity", "vodacom shop",
        "electronics", "gadget",
    ], []),
    ("beauty", "Beauty & Personal Care", None, [
        "sorbet", "placecol", "dream nails", "nail bar", "hair salon", "barber",
        "the hair", "salon", "spa ", "day spa", "skin renewal", "lipco",
        "the body shop", "mac cosmetics", "inglot", "foschini beauty",
        "clicks beauty", "perfume", "cosmetics", "waxing", "aesthetics",
    ], []),
    ("fitness", "Health & Fitness", None, [
        "virgin active", "planet fitness", "planetfitness", "zone fitness",
        "gym ", "crossfit", "f45", "curves", "run walk for life", "parkrun",
        "sweat1000", "boxing", "yoga", "pilates", "dischem vitality",
        "supplement", "sportron", "usn ", "biogen", "nutrition",
    ], []),
    ("pets", "Pets", None, [
        "petshop science", "absolute pets", "pet zone", "petmania", "vet ",
        "veterinary", "animal hospital", "epol", "montego", "hills pet",
        "royal canin", "pet food", "kennels", "cattery", "dog park",
    ], []),

    # ---------------- Telecoms and utilities ------------------------------
    ("telecoms", "Telecoms & Data", None, [
        "vodacom", "mtn ", "mtn za", "mtn sp", "telkom", "cell c", "cellc",
        "rain ", "rain networks", "afrihost", "webafrica", "web africa",
        "vumatel", "openserve", "octotel", "frogfoot", "cool ideas",
        "supersonic", "mweb", "axxess", "rsaweb", "herotel", "seacom",
        "airtime", "data bundle", "prepaid airtime", "recharge", "top up voucher",
        "whatsapp bundle", "internet", "fibre", "adsl", "lte ",
    ], ["subscription"]),
    ("electricity", "Electricity & Water", None, [
        "eskom", "prepaid electricity", "prepaid elec", "city power",
        "city of johannesburg", "city of cape town", "city of tshwane",
        "ethekwini", "nelson mandela bay", "mangaung", "buffalo city",
        "municipal", "municipality", "joburg water", "rand water",
        "electricity", "water and sanitation", "refuse removal",
    ], ["subscription"]),
    ("rates-levies", "Rates & Levies", None, [
        "body corporate", "levies", "levy", "hoa ", "homeowners association",
        "home owners association", "estate levy", "municipal rates",
        "rates and taxes", "sectional title", "trafalgar", "pam golding property management",
    ], ["subscription"]),
    ("rent", "Housing & Rent", None, [
        "rent ", "rental payment", "huur", "lease payment", "landlord",
        "monthly rent", "accommodation rent",
    ], ["subscription"]),

    # ---------------- Insurance, medical, finance -------------------------
    ("medical-aid", "Medical Aid", None, [
        "discovery health", "bonitas", "momentum health", "bestmed",
        "medihelp", "fedhealth", "gems ", "profmed", "keyhealth", "medshield",
        "medical aid", "hospital plan", "gap cover",
    ], ["subscription"]),
    ("insurance", "Insurance", None, [
        "outsurance", "king price", "miway", "santam", "hollard", "budget insurance",
        "first for women", "dial direct", "auto general", "auto and general",
        "old mutual", "sanlam", "liberty life", "momentum ", "discovery life",
        "discovery insure", "1life", "one life", "clientele", "assupol",
        "avbob", "metropolitan", "stangen", "bidvest insurance", "naked insurance",
        "pineapple", "insurance premium", "life cover", "funeral cover",
        "household insurance", "car insurance", "short term insurance",
    ], ["subscription"]),
    ("medical", "Medical & Healthcare", None, [
        "netcare", "mediclinic", "life healthcare", "life hospital", "hospital",
        "dr ", "doctor", "dentist", "dental", "orthodont", "optometrist",
        "spec savers", "specsavers", "torga optical", "execuspecs",
        "physio", "biokinetic", "psychologist", "radiology", "pathcare",
        "lancet", "ampath", "clinic", "medical centre", "day hospital",
    ], []),
    ("debt", "Debt Repayment", None, [
        "personal loan", "loan repayment", "loan instalment", "credit card payment",
        "capitec loan", "african bank loan", "old mutual finance", "bayport",
        "direct axis", "directaxis", "izwe loans", "finchoice", "wonga",
        "revolving credit", "student loan", "nsfas repayment", "consolidation loan",
    ], ["subscription"]),
    ("bond", "Bond & Home Loan", None, [
        "home loan", "bond repayment", "bond instalment", "sa home loans",
        "homeloan", "ooba", "betterbond",
    ], ["subscription"]),
    ("tax", "Tax & SARS", None, [
        "sars", "south african revenue", "provisional tax", "efiling",
        "paye ", "vat payment", "income tax",
    ], []),
    ("savings", "Savings & Investments", None, [
        "easyequities", "easy equities", "satrix", "sygnia", "10x investments",
        "allan gray", "coronation", "ninety one", "psg wealth", "sanlam invest",
        "tax free savings", "tfsa", "unit trust", "retirement annuity",
        "annuity contribution", "provident fund", "stokvel", "fixed deposit",
        "notice deposit", "money market", "savings pocket", "save to",
        "luno", "valr", "binance", "coinbase", "revix", "ovex",
        "etoro", "trading 212", "interactive brokers", "ig markets",
    ], ["subscription"]),

    # ---------------- Education, work, giving -----------------------------
    ("education", "Education & Childcare", None, [
        "school fees", "skoolgeld", "curro", "advtech", "crawford", "reddam",
        "sparrow schools", "unisa", "stadio", "varsity college", "cti education",
        "boston city campus", "damelin", "rosebank college", "milpark",
        "university of", "tuition", "creche", "daycare", "day care",
        "aftercare", "after care", "nursery school", "playschool",
        "udemy", "coursera", "edx ", "skillshare", "masterclass", "getsmarter",
        "textbook", "van schaik", "school uniform", "extra lessons", "tutor",
    ], []),
    ("professional", "Professional & Work", None, [
        "saica", "saipa", "saiba", "engineering council", "ecsa", "hpcsa",
        "law society", "professional body", "membership fee", "annual subscription fee",
        "conference", "seminar", "printing", "postnet", "courier", "the courier guy",
        "aramex", "dhl", "fedex", "ram couriers", "postoffice", "post office",
        "stationery", "waltons", "cna ",
    ], []),
    ("gifts", "Gifts & Donations", None, [
        "gift ", "giftcard", "gift card", "netflorist", "florist",
        "donation", "donate", "charity", "gift of the givers", "sanccob",
        "cansa", "smile foundation", "reach for a dream", "tithe", "offering",
        "church", "mosque", "temple", "zakah", "zakat", "sadaqah",
    ], []),

    # ---------------- Travel ----------------------------------------------
    ("travel", "Travel & Accommodation", None, [
        "flysafair", "fly safair", "airlink", "cemair", "lift airline",
        "south african airways", "saa ", "emirates", "qatar airways",
        "british airways", "lufthansa", "klm ", "air france", "turkish airlines",
        "ethiopian airlines", "kenya airways", "travelstart", "kulula",
        "booking com", "bookingcom", "airbnb", "lekkeslaap", "nightsbridge",
        "hotel", "guest house", "guesthouse", "lodge", "backpackers",
        "sanparks", "kruger", "resort", "protea hotel", "city lodge",
        "southern sun", "tsogo sun", "premier hotel", "avis", "hertz",
        "europcar", "bidvest car rental", "first car rental", "car hire",
        "travel insurance", "visa application", "vfs global", "passport",
        "dha ", "home affairs",
    ], []),

    # ---------------- Money in --------------------------------------------
    ("income-salary", "Income", "Salary", [
        "salary", "salaris", "wages", "payroll", "sal ", "re:\\bsal$",
        "remuneration", "commission received", "bonus payment",
    ], []),
    ("income-other", "Income", None, [
        "sassa", "grant payment", "uif payout", "pension payment",
        "annuity payment", "dividend", "rental income", "invoice payment received",
        "tax refund", "sars refund",
    ], []),
    ("interest-received", "Interest Received", "Credit interest", [
        "credit interest", "interest received", "interest earned",
        "interest capitalised", "gross interest",
    ], []),
    ("refunds", "Refunds & Reversals", "Refund", [
        "refund", "reversal", "reversed", "chargeback", "charge back",
        "returned debit order", "unpaid reversal", "credit note",
    ], []),

    # ---------------- Cash and transfers ----------------------------------
    ("cash", "Cash Withdrawals", "Cash", [
        "atm withdrawal", "cash withdrawal", "atm cash", "autobank withdrawal",
        "cash out", "cashout", "cash send", "cashsend", "ewallet", "e wallet",
        "instant money", "mobile money", "money transfer to cell",
        "atm ", "saswitch", "cardless cash",
    ], []),
    ("transfer", "Transfers", "Internal transfer", [
        "transfer to savings", "transfer from savings", "internal transfer",
        "own account transfer", "inter account transfer", "to my ",
        "from my ", "own acc", "transfer own",
    ], ["internal"]),
]
# fmt: on


# Rules whose meaning outranks any merchant name in the same description.
# "REFUND TAKEALOT.COM" is a refund, not shopping; "TRANSFER TO SAVINGS" is a
# transfer, not a purchase at a shop called Savings.
PRIORITIES = {
    "refunds": 30,
    "interest-received": 20,
    "transfer": 10,
}


def flatten() -> list[dict]:
    """Expand the compact table into dicts the rules engine consumes."""
    out: list[dict] = []
    for rule_id, category, merchant, patterns, flags in DEFAULT_RULES:
        out.append({
            "id": rule_id,
            "category": category,
            "merchant": merchant,
            "patterns": patterns,
            "flags": list(flags),
            "priority": PRIORITIES.get(rule_id, 0),
        })
    return out
