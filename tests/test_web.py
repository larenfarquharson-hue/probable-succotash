"""Web interface smoke tests: every route renders, and nothing leaks."""

from __future__ import annotations

import io
from datetime import date

import pytest

pytest.importorskip("flask", reason="the web UI is an optional extra")

from spendtracker.ingest import loader  # noqa: E402
from spendtracker.ingest.receipts import ReceiptData, store_receipt  # noqa: E402
from spendtracker.web.app import create_app  # noqa: E402

from .conftest import write_csv  # noqa: E402

ROWS = [
    ("02/03/2026", "BOND REPAYMENT ABSA HOMELOAN", -12_850.00),
    ("03/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", -1_200.00),
    ("05/03/2026", "SALARY ACB CREDIT", 40_000.00),
    ("07/03/2026", "CARD PURCHASE HOLLYWOODBETS ONLINE", -800.00),
    ("10/03/2026", "ATM CASH WITHDRAWAL SANDTON", -2_000.00),
    ("11/03/2026", "DEBIT ORDER NETFLIX.COM", -199.00),
    ("12/03/2026", "SOME UNKNOWN PLACE 12345", -333.00),
]


@pytest.fixture
def app(conn, cfg, tmp_path, receipt_image):
    balance, out = 50_000.0, []
    for day, desc, amount in ROWS:
        balance += amount
        out.append([day, desc, f"{amount:.2f}", f"{balance:.2f}"])
    path = write_csv(tmp_path / "w.csv", ["Date", "Description", "Amount", "Balance"], out)
    loader.import_statement(conn, path, cfg=cfg)
    store_receipt(
        conn, receipt_image(), cfg=cfg, account_id=1,
        data=ReceiptData(
            merchant_raw="CHECKERS HYPER", merchant_norm="Checkers",
            receipt_date=date(2026, 3, 3), total_cents=120_000,
            tender_type="card", category="Groceries", extractor="test", confidence=0.9,
        ),
    )
    conn.commit()

    application = create_app(cfg)
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/?period=2026-03",
        "/?period=all",
        "/?period=not-a-period",
        "/transactions",
        "/transactions?category=Groceries",
        "/transactions?q=checkers&direction=out",
        "/transactions?status=duplicate",
        "/merchant/Checkers",
        "/merchant/Nobody",
        "/advice",
        "/advice?period=2026-03",
        "/recurring",
        "/recurring?all=1",
        "/review",
        "/receipts",
        "/receipts?status=unmatched",
        "/receipt/1",
        "/receipt/1/image",
        "/categorise",
        "/upload",
    ],
)
def test_route_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, path


def test_missing_receipt_is_404(client):
    assert client.get("/receipt/9999").status_code == 404


def test_dashboard_shows_the_reconciliation_verdict(client):
    body = client.get("/?period=2026-03").get_data(as_text=True)
    assert "adds up to the bank" in body
    assert "Days covered by a statement" in body


def test_dashboard_falls_back_when_the_default_period_is_empty(client):
    """Data is from March 2026; the "last 3 months" default will not contain it
    unless today happens to fall inside. The dashboard must still show data."""
    body = client.get("/").get_data(as_text=True)
    assert "Nothing imported yet" not in body
    assert "Total out of the account" in body


def test_unexplained_cash_is_disclosed_on_the_dashboard(client):
    body = client.get("/?period=2026-03").get_data(as_text=True)
    assert "Cash with no till slip" in body


def test_advice_states_assumptions(client):
    body = client.get("/advice?period=2026-03").get_data(as_text=True)
    assert "Assumes" in body
    assert "estimates" in body


def test_uncategorised_merchant_is_listed(client):
    body = client.get("/categorise").get_data(as_text=True)
    assert "Some Unknown Place" in body


def test_setting_a_category_persists_and_learns(client, conn):
    txn_id = conn.execute(
        "SELECT id FROM transactions WHERE description_raw LIKE '%UNKNOWN PLACE%'"
    ).fetchone()["id"]
    response = client.post(
        f"/transaction/{txn_id}/category",
        data={"category": "Home & Garden", "learn": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    row = conn.execute(
        "SELECT category, category_source FROM transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row["category"] == "Home & Garden"
    assert row["category_source"] == "user"
    rule = conn.execute(
        "SELECT category FROM rules WHERE source='user' AND category='Home & Garden'"
    ).fetchone()
    assert rule is not None, "a correction should teach the rule engine"


def test_correcting_a_receipt_total_rematches(client, conn):
    response = client.post(
        "/receipt/1/update",
        data={"merchant": "Checkers", "date": "2026-03-03", "total": "1200.00",
              "tender": "card", "category": "Groceries"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    row = conn.execute("SELECT link_status FROM receipts WHERE id=1").fetchone()
    assert row["link_status"] == "matched"


def test_dismissing_a_receipt_excludes_it(client, conn):
    client.post("/receipt/1/update", data={"action": "ignore"}, follow_redirects=True)
    row = conn.execute("SELECT link_status FROM receipts WHERE id=1").fetchone()
    assert row["link_status"] == "ignored"


def test_statement_upload_through_the_form(client, tmp_path, conn):
    rows = [["01/04/2026", "CARD PURCHASE WOOLWORTHS FOOD", "-450.00", "49550.00"]]
    path = write_csv(tmp_path / "april.csv", ["Date", "Description", "Amount", "Balance"], rows)
    response = client.post(
        "/upload",
        data={"kind": "statement", "account": "Main Account",
              "files": (io.BytesIO(path.read_bytes()), "april.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"1 row(s) imported" in response.data
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE txn_date='2026-04-01'"
    ).fetchone()["c"] == 1


def test_reuploading_the_same_statement_is_reported_not_duplicated(client, tmp_path, conn):
    rows = [["01/05/2026", "CARD PURCHASE SPAR", "-120.00", "49430.00"]]
    path = write_csv(tmp_path / "may.csv", ["Date", "Description", "Amount", "Balance"], rows)
    payload = path.read_bytes()
    for _ in range(2):
        client.post(
            "/upload",
            data={"kind": "statement", "files": (io.BytesIO(payload), "may.csv")},
            content_type="multipart/form-data",
        )
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE txn_date='2026-05-01'"
    ).fetchone()["c"] == 1


def test_broken_csv_upload_reports_the_error_rather_than_500(client, tmp_path):
    response = client.post(
        "/upload",
        data={"kind": "statement",
              "files": (io.BytesIO(b"this is not a statement at all\n"), "junk.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"junk.csv" in response.data


def test_receipt_image_route_refuses_paths_outside_uploads(client, conn, tmp_path):
    """A tampered stored_path must not turn into an arbitrary file read."""
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    conn.execute("UPDATE receipts SET stored_path=? WHERE id=1", (str(secret),))
    conn.commit()
    assert client.get("/receipt/1/image").status_code == 404
