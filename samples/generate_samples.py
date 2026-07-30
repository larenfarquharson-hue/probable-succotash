"""Generate realistic-but-fictional sample bank statements and till slips.

Run:  python samples/generate_samples.py

Nothing here is real data. The three CSVs deliberately use three *different*
layouts so you can see the auto-detection working, and statement 2 overlaps
statement 1 by two weeks so you can see duplicate handling working.

The layouts are representative shapes of South African bank exports (signed
amount + running balance; separate debit/credit columns with a preamble block;
month-first dates with no balance column). They are NOT verified copies of any
particular bank's current export format - check a real export before trusting
a named profile.
"""

from __future__ import annotations

import csv
import random
import zlib
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent
random.seed(20260730)

# description, amount (negative = out), rough monthly frequency
RECURRING = [
    ("BOND REPAYMENT ABSA HOMELOAN 4067", -1285000, 1),
    ("DEBIT ORDER OUTSURANCE 8821", -142300, 1),
    ("DEBIT ORDER DISCOVERY HEALTH MEDICAL SCHEME", -389000, 1),
    ("DEBIT ORDER VODACOM SP PTY LTD", -89900, 1),
    ("DEBIT ORDER AFRIHOST FIBRE", -79900, 1),
    ("DEBIT ORDER NETFLIX.COM AMSTERDAM NL", -19900, 1),
    ("DEBIT ORDER SHOWMAX RANDBURG ZA", -9900, 1),
    ("DEBIT ORDER SPOTIFY P29B4CE1F STOCKHOLM", -6699, 1),
    ("DEBIT ORDER MULTICHOICE DSTV PREMIUM", -94500, 1),
    ("DEBIT ORDER VIRGIN ACTIVE SA PTY", -85500, 1),
    ("DEBIT ORDER 10X INVEST RETIREMENT ANNUITY", -250000, 1),
    ("DEBIT ORDER APPLE.COM/BILL ICLOUD 2TB", -14999, 1),
    ("DEBIT ORDER MICROSOFT 365 FAMILY", -12999, 1),
    ("DEBIT ORDER ADOBE CREATIVE CLOUD", -39999, 1),
    ("MONTHLY ACCOUNT FEE", -12500, 1),
    ("SMS NOTIFICATION FEE", -600, 1),
    ("CARD FEE", -3000, 1),
    ("CITY OF JOHANNESBURG RATES AND TAXES", -178000, 1),
    ("PREPAID ELECTRICITY PURCHASE", -120000, 2),
]

VARIABLE = [
    ("CARD PURCHASE 4067********1234 CHECKERS HYPER FOURWAYS ZA", -35000, -180000, 4),
    ("CARD PURCHASE 4067********1234 WOOLWORTHS FOOD SANDTON ZA", -18000, -95000, 3),
    ("CARD PURCHASE 4067********1234 PICK N PAY FAMILY BRYANSTON", -12000, -70000, 2),
    ("CARD PURCHASE 4067********1234 ENGEN FOURWAYS", -60000, -110000, 3),
    ("CARD PURCHASE 4067********1234 SHELL RIVONIA", -55000, -105000, 2),
    ("CARD PURCHASE 4067********1234 VIDA E CAFFE NICOLWAY", -3900, -8900, 9),
    ("CARD PURCHASE 4067********1234 STARBUCKS ROSEBANK", -4500, -9500, 3),
    ("CARD PURCHASE 4067********1234 UBER EATS ZA", -14000, -42000, 6),
    ("CARD PURCHASE 4067********1234 MR D FOOD", -12000, -38000, 3),
    ("CARD PURCHASE 4067********1234 NANDOS FOURWAYS", -11000, -29000, 2),
    ("CARD PURCHASE 4067********1234 KFC WITKOPPEN", -8000, -19000, 2),
    ("CARD PURCHASE 4067********1234 DEBONAIRS PIZZA", -9900, -24000, 2),
    ("CARD PURCHASE 4067********1234 TOPS AT SPAR LONEHILL", -25000, -85000, 3),
    ("CARD PURCHASE 4067********1234 UBER TRIP HELP.UBER.COM", -6500, -21000, 4),
    ("CARD PURCHASE 4067********1234 BOLT REQUEST TALLINN EE", -5500, -18000, 2),
    ("CARD PURCHASE 4067********1234 TAKEALOT.COM CAPE TOWN", -15000, -180000, 3),
    ("CARD PURCHASE 4067********1234 CLICKS FOURWAYS MALL", -8000, -46000, 2),
    ("CARD PURCHASE 4067********1234 DIS-CHEM PHARMACY", -12000, -52000, 1),
    ("CARD PURCHASE 4067********1234 STER-KINEKOR MONTECASINO", -12000, -32000, 1),
    ("CARD PURCHASE 4067********1234 HOLLYWOODBETS ONLINE", -20000, -100000, 5),
    ("CARD PURCHASE 4067********1234 BETWAY SA", -15000, -60000, 3),
    ("CARD PURCHASE 4067********1234 STEAM GAMES", -9900, -74900, 1),
    ("CARD PURCHASE 4067********1234 MR PRICE FOURWAYS", -19900, -89900, 1),
    ("CARD PURCHASE 4067********1234 SPORTSCENE SANDTON CITY", -49900, -159900, 1),
    ("CARD PURCHASE 4067********1234 BUILDERS WAREHOUSE STRIJDOM", -25000, -190000, 1),
    ("CARD PURCHASE 4067********1234 ABSOLUTE PETS LONEHILL", -35000, -95000, 1),
    ("CARD PURCHASE 4067********1234 SANRAL E-TOLL", -12000, -38000, 1),
    ("PARKING FOURWAYS MALL", -1500, -4500, 3),
    ("ATM CASH WITHDRAWAL SANDTON", -50000, -200000, 2),
]

INFLOWS = [("SALARY ACB CREDIT IGNITION GROUP", 6850000, 1)]


def month_rows(year: int, month: int) -> list[tuple[date, str, int]]:
    rows: list[tuple[date, str, int]] = []
    first = date(year, month, 1)
    last_day = (date(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)).day

    for desc, amount, _ in INFLOWS:
        rows.append((date(year, month, min(25, last_day)), desc, amount))

    for desc, amount, times in RECURRING:
        for i in range(times):
            # crc32, not hash(): the built-in hash is salted per process, so using
            # it here made the "samples" differ on every run and the overlap count
            # in the output message change with it.
            day = min(last_day, 1 + (zlib.crc32(desc.encode()) % 26) + i * 12)
            rows.append((date(year, month, day), desc, amount))

    for desc, lo, hi, times in VARIABLE:
        for _ in range(times):
            day = random.randint(1, last_day)
            amount = random.randint(hi, lo)  # hi is the more negative bound
            amount = int(round(amount / 100) * 100) if random.random() < 0.4 else amount
            rows.append((date(year, month, day), desc, amount))

    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def with_balance(rows: list[tuple[date, str, int]], opening: int) -> list[tuple[date, str, int, int]]:
    out = []
    balance = opening
    for d, desc, amount in rows:
        balance += amount
        out.append((d, desc, amount, balance))
    return out


def write_style_a(path: Path, rows: list[tuple[date, str, int, int]]) -> None:
    """Signed amount + running balance, day-first dates, small preamble."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Statement", "Cheque Account 62xxxxxx891"])
        w.writerow(["Generated", "sample data - not a real account"])
        w.writerow([])
        w.writerow(["Date", "Description", "Amount", "Balance"])
        for d, desc, amount, balance in rows:
            w.writerow([d.strftime("%d/%m/%Y"), desc, f"{amount / 100:.2f}", f"{balance / 100:.2f}"])
        w.writerow([])
        w.writerow(["", "Closing Balance", "", f"{rows[-1][3] / 100:.2f}"])


def write_style_b(path: Path, rows: list[tuple[date, str, int, int]]) -> None:
    """Separate debit/credit columns, space thousands separators, preamble."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["ACCOUNT STATEMENT"])
        w.writerow(["Account Number", "1234567890"])
        w.writerow(["Period", f"{rows[0][0]:%d %b %Y} to {rows[-1][0]:%d %b %Y}"])
        w.writerow([])
        w.writerow(["Transaction Date", "Narrative", "Debit Amount", "Credit Amount", "Running Balance"])
        for d, desc, amount, balance in rows:
            debit = f"{abs(amount) / 100:,.2f}".replace(",", " ") if amount < 0 else ""
            credit = f"{amount / 100:,.2f}".replace(",", " ") if amount > 0 else ""
            w.writerow([
                d.strftime("%Y-%m-%d"),
                desc,
                debit,
                credit,
                f"{balance / 100:,.2f}".replace(",", " "),
            ])


def write_style_c(path: Path, rows: list[tuple[date, str, int, int]]) -> None:
    """Month-first dates, no balance column, split description columns,
    amounts in parentheses for outflows."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Posting Date", "Reference", "Details", "Transaction Amount"])
        for d, desc, amount, _balance in rows:
            head, _, tail = desc.partition(" ")
            value = f"({abs(amount) / 100:.2f})" if amount < 0 else f"{amount / 100:.2f}"
            w.writerow([d.strftime("%m/%d/%Y"), head, tail or desc, value])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    jan = month_rows(2026, 1)
    feb = month_rows(2026, 2)
    mar = month_rows(2026, 3)

    # Statement 1: January, style A.
    s1 = with_balance(jan, opening=4_250_000)
    write_style_a(OUT / "statement_january.csv", s1)

    # Statement 2: overlaps the second half of January and adds February.
    # Same rows in the overlap -> exercises duplicate detection.
    overlap_start = date(2026, 1, 16)
    jan_tail = [r for r in jan if r[0] >= overlap_start]
    s2_rows = jan_tail + feb
    opening_s2 = 4_250_000 + sum(a for d, _desc, a in jan if d < overlap_start)
    s2 = with_balance(s2_rows, opening=opening_s2)
    write_style_b(OUT / "statement_mid_jan_to_feb.csv", s2)

    # Statement 3: March, style C, no balance column.
    s3 = with_balance(mar, opening=s2[-1][3])
    write_style_c(OUT / "statement_march.csv", s3)

    print(f"Wrote 3 sample statements to {OUT}")
    for name, rows in [
        ("statement_january.csv", s1),
        ("statement_mid_jan_to_feb.csv", s2),
        ("statement_march.csv", s3),
    ]:
        out = sum(-a for _d, _desc, a, _b in rows if a < 0)
        print(f"  {name}: {len(rows)} rows, outflows R{out / 100:,.2f}")
    print(f"  overlap: {len(jan_tail)} January rows appear in both statement 1 and 2")


if __name__ == "__main__":
    main()
