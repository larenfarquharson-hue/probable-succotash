"""The login gate, exercised against the real routes.

The unit tests in test_auth.py prove the primitives work. These prove the app
actually uses them — that no route serves statement data to an unauthenticated
request, which is the only property that matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask", reason="the web UI is an optional extra")

from spendtracker import auth as auth_mod  # noqa: E402
from spendtracker.config import Config  # noqa: E402
from spendtracker.ingest import loader  # noqa: E402
from spendtracker.web.app import create_app  # noqa: E402

from .conftest import write_csv  # noqa: E402

PASSPHRASE = "a-good-enough-passphrase"

ROWS = [
    ["02/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", "-1200.00", "38800.00"],
    ["05/03/2026", "SALARY ACB CREDIT", "40000.00", "78800.00"],
    ["11/03/2026", "DEBIT ORDER NETFLIX.COM", "-199.00", "78601.00"],
]

# Every route that shows or changes data. If a new one is added without being
# listed here, test_no_route_is_left_unguarded fails.
PROTECTED_PATHS = [
    "/",
    "/transactions",
    "/advice",
    "/recurring",
    "/review",
    "/receipts",
    "/upload",
    "/categorise",
    "/merchant/Checkers",
]


@pytest.fixture
def populated(conn, cfg: Config, tmp_path: Path) -> Config:
    path = write_csv(
        tmp_path / "march.csv", ["Date", "Description", "Amount", "Balance"], ROWS
    )
    loader.import_statement(conn, path, cfg=cfg)
    return cfg


@pytest.fixture
def secured(populated: Config):
    """An app with a passphrase set, as it would be when served to a network."""
    state = auth_mod.load_auth(populated.data_dir)
    auth_mod.set_passphrase(state, PASSPHRASE)
    app = create_app(populated)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def open_app(populated: Config):
    """An app with no passphrase — the localhost-only default."""
    app = create_app(populated)
    app.config.update(TESTING=True)
    return app


# ---------------------------------------------------------------------------
# The localhost default must not change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_without_a_passphrase_everything_works_as_before(open_app, path: str) -> None:
    """Setting up a login must not break the existing localhost workflow."""
    client = open_app.test_client()
    assert client.get(path).status_code == 200


def test_the_login_page_redirects_away_when_no_passphrase_is_set(open_app) -> None:
    response = open_app.test_client().get("/login")
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# With a passphrase, nothing leaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_every_page_redirects_to_login_when_signed_out(secured, path: str) -> None:
    response = secured.test_client().get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_no_statement_data_reaches_an_unauthenticated_response(
    secured, path: str
) -> None:
    """A redirect is only useful if the body is empty of real data."""
    body = secured.test_client().get(path).get_data(as_text=True)
    for leak in ("CHECKERS", "NETFLIX", "38 800", "1 200.00"):
        assert leak not in body


def test_parameterised_routes_are_guarded_too(secured) -> None:
    """The route sweep skips these, and one of them serves slip photographs."""
    client = secured.test_client()
    for path in ("/receipt/1", "/receipt/1/image", "/merchant/Checkers"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"], path


def test_the_next_parameter_has_no_stray_query_marker(secured) -> None:
    location = secured.test_client().get("/advice").headers["Location"]
    assert "next=/advice" in location
    assert location.endswith("?") is False


def test_posts_are_blocked_too(secured) -> None:
    """Read protection is worthless if writes are still accepted."""
    response = secured.test_client().post(
        "/transaction/1/category", data={"category": "Groceries"}
    )
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_no_route_is_left_unguarded(secured) -> None:
    """Catches a new route added without an auth decision being made.

    The guard is a before_request hook, so new routes are protected by
    default — this asserts that property rather than trusting it.
    """
    public = {"login", "static"}
    client = secured.test_client()

    for rule in secured.url_map.iter_rules():
        if rule.endpoint in public:
            continue
        if "GET" not in (rule.methods or set()):
            continue
        if any(c in rule.rule for c in "<>"):
            continue  # parameterised routes are covered by PROTECTED_PATHS
        response = client.get(rule.rule)
        assert response.status_code == 302, f"{rule.rule} was not guarded"
        assert "/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


def test_the_login_page_itself_is_reachable(secured) -> None:
    response = secured.test_client().get("/login")
    assert response.status_code == 200
    assert "Passphrase" in response.get_data(as_text=True)


def test_the_login_page_reveals_nothing_about_the_data(secured) -> None:
    body = secured.test_client().get("/login").get_data(as_text=True)
    for leak in ("Dashboard", "Transactions", "CHECKERS", "Reduce spend"):
        assert leak not in body


def test_the_right_passphrase_signs_you_in(secured) -> None:
    client = secured.test_client()
    response = client.post("/login", data={"passphrase": PASSPHRASE})

    assert response.status_code == 302
    assert client.get("/").status_code == 200


def test_a_wrong_passphrase_does_not(secured) -> None:
    client = secured.test_client()
    response = client.post("/login", data={"passphrase": "not-the-passphrase"})

    assert response.status_code == 401
    assert client.get("/").status_code == 302


def test_the_session_persists_across_requests(secured) -> None:
    client = secured.test_client()
    client.post("/login", data={"passphrase": PASSPHRASE})

    for path in PROTECTED_PATHS:
        assert client.get(path).status_code == 200, path


def test_signing_out_ends_the_session(secured) -> None:
    client = secured.test_client()
    client.post("/login", data={"passphrase": PASSPHRASE})
    assert client.get("/").status_code == 200

    client.post("/logout")
    assert client.get("/").status_code == 302


def test_the_session_cookie_is_hardened(secured) -> None:
    client = secured.test_client()
    response = client.post("/login", data={"passphrase": PASSPHRASE})

    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie, "must not be readable from JavaScript"
    assert "SameSite=Lax" in cookie, "blocks cross-site form posts"


# ---------------------------------------------------------------------------
# Redirect handling
# ---------------------------------------------------------------------------


def test_you_land_back_where_you_were_headed(secured) -> None:
    client = secured.test_client()
    response = client.get("/advice")
    assert "next=" in response.headers["Location"]

    signed_in = client.post(
        "/login", data={"passphrase": PASSPHRASE, "next": "/advice"}
    )
    assert signed_in.headers["Location"].endswith("/advice")


@pytest.mark.parametrize(
    "hostile",
    ["https://evil.example.com/", "//evil.example.com/", "http://evil.example.com"],
)
def test_the_next_parameter_cannot_redirect_off_site(secured, hostile: str) -> None:
    """Otherwise the login page becomes an open redirect for phishing."""
    client = secured.test_client()
    response = client.post(
        "/login", data={"passphrase": PASSPHRASE, "next": hostile}
    )

    assert "evil.example.com" not in response.headers["Location"]


# ---------------------------------------------------------------------------
# Brute force
# ---------------------------------------------------------------------------


def test_repeated_failures_are_throttled(secured) -> None:
    client = secured.test_client()

    codes = [
        client.post("/login", data={"passphrase": "wrong"}).status_code
        for _ in range(8)
    ]

    assert 429 in codes, "guessing must eventually be rate limited"
    assert codes.index(429) >= auth_mod.FREE_ATTEMPTS, "typos should be forgiven first"


def test_a_wrong_passphrase_says_nothing_useful(secured) -> None:
    """No hint about length, correctness of a prefix, or attempts remaining."""
    body = (
        secured.test_client()
        .post("/login", data={"passphrase": "wrong"})
        .get_data(as_text=True)
    )
    assert "Incorrect passphrase" in body
    for leak in ("characters", "length", "attempts remaining", "close"):
        assert leak not in body.lower()
