from __future__ import annotations

import csv
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from spendtracker import db as dbmod, taxonomy
from spendtracker.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(data_dir=tmp_path / "data", ocr_provider="manual")
    config.ensure_dirs()
    return config


@pytest.fixture
def conn(cfg: Config) -> sqlite3.Connection:
    connection = dbmod.connect(cfg.db_path)
    dbmod.init_db(connection)
    taxonomy.seed(connection)
    yield connection
    connection.close()


def write_csv(path: Path, header: list[str], rows: list[list], *, delimiter: str = ",",
              preamble: list[list] | None = None) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        for line in preamble or []:
            writer.writerow(line)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    return path


@pytest.fixture
def signed_statement(tmp_path: Path):
    """Signed amount column plus a running balance, day-first dates, preamble."""

    def build(name: str = "signed.csv", *, opening: float = 10_000.0, rows=None) -> Path:
        rows = rows or [
            ("01/03/2026", "CARD PURCHASE 4067****1234 CHECKERS FOURWAYS", -350.00),
            ("02/03/2026", "DEBIT ORDER NETFLIX.COM", -199.00),
            ("03/03/2026", "CARD PURCHASE 4067****1234 VIDA E CAFFE", -42.50),
            ("03/03/2026", "CARD PURCHASE 4067****1234 VIDA E CAFFE", -42.50),
            ("05/03/2026", "SALARY ACB CREDIT", 25_000.00),
            ("07/03/2026", "ATM CASH WITHDRAWAL SANDTON", -1_000.00),
            ("09/03/2026", "MONTHLY ACCOUNT FEE", -125.00),
        ]
        balance = opening
        out = []
        for day, desc, amount in rows:
            balance += amount
            out.append([day, desc, f"{amount:.2f}", f"{balance:.2f}"])
        return write_csv(
            tmp_path / name,
            ["Date", "Description", "Amount", "Balance"],
            out,
            preamble=[["Statement", "Cheque Account"], []],
        )

    return build


@pytest.fixture
def debit_credit_statement(tmp_path: Path):
    """Separate debit/credit columns, semicolons, space thousands separators."""

    def build(name: str = "dc.csv", *, opening: float = 10_000.0) -> Path:
        rows = [
            ("2026-03-01", "CARD PURCHASE CHECKERS FOURWAYS", -350.00),
            ("2026-03-04", "PAYMENT TO TAKEALOT.COM", -1_299.00),
            ("2026-03-05", "SALARY ACB CREDIT", 25_000.00),
        ]
        balance = opening
        out = []
        for day, desc, amount in rows:
            balance += amount
            debit = f"{abs(amount):,.2f}".replace(",", " ") if amount < 0 else ""
            credit = f"{amount:,.2f}".replace(",", " ") if amount > 0 else ""
            out.append([day, desc, debit, credit, f"{balance:,.2f}".replace(",", " ")])
        return write_csv(
            tmp_path / name,
            ["Transaction Date", "Narrative", "Debit Amount", "Credit Amount", "Running Balance"],
            out,
            delimiter=";",
            preamble=[["ACCOUNT STATEMENT"], ["Account", "123456"], []],
        )

    return build


@pytest.fixture
def receipt_image(tmp_path: Path):
    """A tiny real image file.

    Content must differ per name: receipts are deduplicated on the SHA-256 of the
    image bytes, so byte-identical fixtures make every "second upload" look like a
    re-upload of the first and silently neuter the test.
    """

    def build(name: str = "slip.png") -> Path:
        path = tmp_path / name
        seed = sum(name.encode()) % 200
        try:
            from PIL import Image

            Image.new("RGB", (24, 48), (255 - seed, 250, 245)).save(path)
        except ImportError:
            # Smallest valid 1x1 PNG, so tests run without Pillow.
            path.write_bytes(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d4948445200000001000000010806"
                    "0000001f15c4890000000a49444154789c6300010000050001"
                    "0d0a2db40000000049454e44ae426082"
                )
            )
        return path

    return build


def days(base: date, offset: int) -> str:
    return (base + timedelta(days=offset)).isoformat()
