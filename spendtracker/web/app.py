"""Flask application.

A local, single-user tool. It binds to 127.0.0.1 by default, where the only
person who can reach it is the one at the keyboard, and in that configuration
there is no login — a password guarding a port nobody else can open is theatre.

Bind it anywhere else and a passphrase becomes mandatory: `serve` refuses to
start without one, and every route redirects to /login until you authenticate.
See spendtracker/auth.py for the reasoning, and for what this does not protect
against — chiefly that there is no TLS, so use it on a home network you trust
and never expose it to the internet.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .. import advice as advice_mod
from .. import auth as auth_mod
from .. import analytics, db as dbmod, taxonomy
from ..config import Config, load_config
from ..dedupe import rematch_all_receipts, resolve_candidate
from ..ingest import loader
from ..ingest.csvimport import CsvFormatError
from ..ingest.receipts import (
    SUPPORTED_IMAGE_TYPES,
    ReceiptExtractionError,
    ignore_receipt,
    store_receipt,
    update_receipt,
)
from ..money import fmt, from_cents, to_cents
from ..periods import Period, parse_period

MAX_UPLOAD_BYTES = 64 * 1024 * 1024


SESSION_LIFETIME = timedelta(days=14)


def create_app(
    cfg: Config | None = None,
    *,
    require_login: bool | None = None,
    secure_cookies: bool = False,
) -> Flask:
    """Build the app.

    ``require_login`` forces the login requirement on or off. Left as None it
    follows the stored credentials: a login is required exactly when a
    passphrase has been set. `serve` refuses to bind off-loopback without one
    (see auth.check_exposure), so this default is safe.
    """
    cfg = cfg or load_config()
    cfg.ensure_dirs()

    auth_state = auth_mod.load_auth(cfg.data_dir)
    login_required = (
        auth_state.has_passphrase if require_login is None else require_login
    )
    throttle = auth_mod.LoginThrottle()

    app = Flask(__name__)
    # Prefer the generated key over the config default: the default value is
    # public in the repository, and a known signing key means forgeable
    # session cookies.
    app.config["SECRET_KEY"] = auth_state.secret_key or cfg.secret_key
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["SPENDTRACKER_CFG"] = cfg
    app.config["SPENDTRACKER_LOGIN_REQUIRED"] = login_required
    app.config["PERMANENT_SESSION_LIFETIME"] = SESSION_LIFETIME
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Secure is set only when actually serving over TLS. Setting it on a plain
    # HTTP server would stop the cookie being sent at all, so it tracks the
    # transport rather than being hardcoded either way.
    app.config["SESSION_COOKIE_SECURE"] = secure_cookies
    app.config["SPENDTRACKER_TLS"] = secure_cookies

    # ---------------------------------------------------------------- db ---
    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            conn = dbmod.connect(cfg.db_path)
            dbmod.init_db(conn)
            taxonomy.seed(conn)
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):  # noqa: ANN001
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------ filters --
    @app.template_filter("money")
    def money_filter(cents) -> str:  # noqa: ANN001
        return fmt(int(cents or 0), cfg.currency_symbol)

    @app.template_filter("pct")
    def pct_filter(value) -> str:  # noqa: ANN001
        return f"{(value or 0) * 100:.1f}%"

    @app.template_filter("pct0")
    def pct0_filter(value) -> str:  # noqa: ANN001
        return f"{(value or 0) * 100:.0f}%"

    # ------------------------------------------------------------ context --
    def data_extent(conn: sqlite3.Connection) -> Period | None:
        row = conn.execute(
            "SELECT MIN(txn_date) lo, MAX(txn_date) hi FROM transactions WHERE status='active'"
        ).fetchone()
        if row is None or row["lo"] is None:
            return None
        lo, hi = date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"])
        return Period(lo, hi, "all data")

    def current_period(default: str = "last-3-months") -> Period:
        """The period to report on.

        When the user has not asked for one, a relative default like "last 3
        months" is only useful if the imported data actually overlaps it. Someone
        importing last year's statements would otherwise land on an empty
        dashboard and conclude the import failed, so an unrequested default that
        contains no transactions falls back to everything imported.
        """
        conn = get_db()
        requested = request.args.get("period")
        raw = requested or default
        try:
            period = parse_period(raw)
        except ValueError:
            flash(f"Could not understand the period {raw!r}; showing all data instead.", "warn")
            period = None

        if period is None:
            return data_extent(conn) or Period(date.today(), date.today(), "no data yet")

        if requested:
            return period

        start, end = period.as_iso()
        has_rows = conn.execute(
            "SELECT 1 FROM transactions WHERE status='active' AND txn_date BETWEEN ? AND ? LIMIT 1",
            (start, end),
        ).fetchone()
        if has_rows:
            return period
        return data_extent(conn) or period

    @app.context_processor
    def inject_globals() -> dict:
        # An unauthenticated request renders only the login page, which shows
        # none of these. Skip the queries rather than touching the database on
        # behalf of someone who has not signed in.
        if app.config["SPENDTRACKER_LOGIN_REQUIRED"] and not session.get("authenticated"):
            return {
                "cfg": cfg,
                "symbol": cfg.currency_symbol,
                "login_required": True,
                "authenticated": False,
            }
        conn = get_db()
        pending = conn.execute(
            "SELECT COUNT(*) c FROM duplicate_candidates WHERE resolution='pending'"
        ).fetchone()["c"]
        unmatched = conn.execute(
            "SELECT COUNT(*) c FROM receipts WHERE link_status='unmatched'"
        ).fetchone()["c"]
        uncategorised = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE status='active' AND amount_cents<0 "
            "AND COALESCE(category,'Uncategorised')='Uncategorised'"
        ).fetchone()["c"]
        return {
            "cfg": cfg,
            "symbol": cfg.currency_symbol,
            "login_required": app.config["SPENDTRACKER_LOGIN_REQUIRED"],
            "authenticated": bool(session.get("authenticated")),
            "pending_reviews": pending,
            "unmatched_receipts": unmatched,
            "uncategorised_count": uncategorised,
            "period_arg": request.args.get("period", ""),
            "all_categories": [c[0] for c in taxonomy.CATEGORIES],
            "period_presets": [
                ("this-month", "This month"),
                ("last-month", "Last month"),
                ("last-3-months", "Last 3 months"),
                ("last-6-months", "Last 6 months"),
                ("ytd", "Year to date"),
                ("all", "Everything"),
            ],
        }

    # ---------------------------------------------------------------- auth --
    PUBLIC_ENDPOINTS = {"login", "static"}

    @app.before_request
    def require_authentication():  # noqa: ANN201
        """Gate everything except the login page itself.

        Fails closed: an unknown endpoint is treated as protected.
        """
        if not app.config["SPENDTRACKER_LOGIN_REQUIRED"]:
            return None
        if session.get("authenticated") is True:
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        # Remember where they were headed, but only a path on this site —
        # taking a full URL here would make an open redirect.
        # full_path appends a bare "?" when there is no query string.
        nxt = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=nxt) if nxt else url_for("login"))

    def safe_next(target: str | None) -> str:
        """Only ever redirect to a path on this site."""
        if not target:
            return url_for("dashboard")
        if not target.startswith("/") or target.startswith("//"):
            return url_for("dashboard")
        return target

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not app.config["SPENDTRACKER_LOGIN_REQUIRED"]:
            return redirect(url_for("dashboard"))
        if session.get("authenticated") is True:
            return redirect(url_for("dashboard"))

        who = request.remote_addr or "unknown"
        wait = throttle.retry_after(who)
        if request.method == "POST" and wait > 0:
            flash(
                f"Too many failed attempts. Try again in {int(wait) + 1} seconds.",
                "warn",
            )
            return render_template("login.html", page="login", locked=True), 429

        if request.method == "POST":
            attempt = request.form.get("passphrase", "")
            if auth_mod.verify_passphrase(auth_state, attempt):
                throttle.record_success(who)
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                return redirect(safe_next(request.form.get("next")))

            delay = throttle.record_failure(who)
            # One message for a wrong passphrase whether or not it triggered a
            # lockout, so the response does not report on the attacker's progress.
            flash("Incorrect passphrase.", "warn")
            if delay:
                flash(f"Further attempts are paused for {int(delay) + 1} seconds.", "warn")
            return render_template("login.html", page="login", locked=False), 401

        return render_template(
            "login.html", page="login", locked=False, next=request.args.get("next")
        )

    @app.post("/logout")
    def logout():
        session.clear()
        flash("Signed out.", "ok")
        return redirect(url_for("login"))

    # -------------------------------------------------------------- routes --
    @app.route("/")
    def dashboard():
        conn = get_db()
        period = current_period()
        summary = analytics.period_summary(conn, period, cfg=cfg)
        trend = analytics.monthly_trend(conn, cfg=cfg, months=12)
        has_data = summary.transaction_count > 0
        peak_day = max((c for _d, c in summary.daily_cents), default=1) or 1
        peak_month = max((p.spend_cents for p in trend), default=1) or 1
        return render_template(
            "dashboard.html",
            period=period,
            s=summary,
            trend=trend,
            peak_day=peak_day,
            peak_month=peak_month,
            has_data=has_data,
        )

    @app.route("/transactions")
    def transactions():
        conn = get_db()
        period = current_period("all")
        start, end = period.as_iso()
        clauses = ["t.txn_date BETWEEN ? AND ?"]
        params: list = [start, end]

        status = request.args.get("status", "active")
        if status in ("active", "duplicate", "ignored"):
            clauses.append("t.status = ?")
            params.append(status)

        category = request.args.get("category")
        if category:
            clauses.append("COALESCE(t.category,'Uncategorised') = ?")
            params.append(category)

        merchant = request.args.get("merchant")
        if merchant:
            clauses.append("t.merchant_norm = ?")
            params.append(merchant)

        query = (request.args.get("q") or "").strip()
        if query:
            clauses.append("(t.description_raw LIKE ? OR t.merchant_norm LIKE ?)")
            params += [f"%{query}%", f"%{query}%"]

        direction = request.args.get("direction", "out")
        if direction == "out":
            clauses.append("t.amount_cents < 0")
        elif direction == "in":
            clauses.append("t.amount_cents > 0")

        row_limit = 250
        rows = conn.execute(
            f"""SELECT t.*,
                       (SELECT COUNT(*) FROM receipts r WHERE r.transaction_id = t.id) receipts
                FROM transactions t
                WHERE {' AND '.join(clauses)}
                ORDER BY t.txn_date DESC, t.id DESC
                LIMIT ?""",
            params + [row_limit],
        ).fetchall()
        total = sum(-int(r["amount_cents"]) for r in rows if r["amount_cents"] < 0)
        return render_template(
            "transactions.html",
            rows=rows,
            period=period,
            total=total,
            row_limit=row_limit,
            filters={
                "status": status,
                "category": category or "",
                "merchant": merchant or "",
                "q": query,
                "direction": direction,
            },
        )

    @app.route("/merchant/<path:name>")
    def merchant(name: str):
        conn = get_db()
        period = current_period("all")
        rows = analytics.merchant_detail(conn, name, period=period)
        total = sum(-int(r["amount_cents"]) for r in rows)
        recurring = [r for r in analytics.find_recurring(conn) if r.merchant == name]
        return render_template(
            "merchant.html",
            name=name,
            rows=rows,
            total=total,
            period=period,
            recurring=recurring[0] if recurring else None,
        )

    @app.route("/advice")
    def advice_page():
        conn = get_db()
        period = current_period()
        summary = analytics.period_summary(conn, period, cfg=cfg)
        report = advice_mod.build_advice(conn, period, cfg=cfg, summary=summary)
        monthly_spend = int(summary.spend_cents / period.months) or 1
        return render_template(
            "advice.html",
            period=period,
            report=report,
            s=summary,
            monthly_spend=monthly_spend,
        )

    @app.route("/recurring")
    def recurring_page():
        conn = get_db()
        show_all = request.args.get("all") == "1"
        items = analytics.find_recurring(conn, subscriptions_only=not show_all)
        monthly = sum(i.monthly_equivalent_cents for i in items if i.still_active)
        return render_template(
            "recurring.html", items=items, monthly=monthly, show_all=show_all
        )

    @app.route("/review")
    def review():
        conn = get_db()
        rows = conn.execute(
            """SELECT dc.id, dc.score, dc.reason,
                      n.id n_id, n.txn_date n_date, n.description_raw n_desc,
                      n.amount_cents n_amt, n.status n_status, n.balance_cents n_bal,
                      e.id e_id, e.txn_date e_date, e.description_raw e_desc,
                      e.balance_cents e_bal,
                      ns.filename n_file, es.filename e_file
               FROM duplicate_candidates dc
               JOIN transactions n ON n.id = dc.txn_id
               JOIN transactions e ON e.id = dc.existing_id
               LEFT JOIN statements ns ON ns.id = n.statement_id
               LEFT JOIN statements es ON es.id = e.statement_id
               WHERE dc.resolution = 'pending'
               ORDER BY dc.score DESC, n.txn_date"""
        ).fetchall()
        held = sum(-int(r["n_amt"]) for r in rows if r["n_status"] == "duplicate")
        return render_template("review.html", rows=rows, held=held)

    @app.post("/review/<int:candidate_id>")
    def review_resolve(candidate_id: int):
        conn = get_db()
        decision = request.form.get("decision")
        if decision not in ("duplicate", "distinct"):
            abort(400)
        try:
            resolve_candidate(conn, candidate_id, decision)
        except LookupError:
            abort(404)
        flash(
            "Marked as a duplicate and held out of your totals."
            if decision == "duplicate"
            else "Kept as two separate transactions.",
            "ok",
        )
        return redirect(url_for("review"))

    @app.route("/receipts")
    def receipts_page():
        conn = get_db()
        status = request.args.get("status") or ""
        where = "WHERE link_status = ?" if status else ""
        params = [status] if status else []
        rows = conn.execute(
            f"""SELECT r.*, t.txn_date t_date, t.description_raw t_desc,
                       t.amount_cents t_amt
                FROM receipts r
                LEFT JOIN transactions t ON t.id = r.transaction_id
                {where}
                ORDER BY r.receipt_date DESC, r.id DESC""",
            params,
        ).fetchall()
        counts = {
            row["link_status"]: row["c"]
            for row in conn.execute(
                "SELECT link_status, COUNT(*) c FROM receipts GROUP BY link_status"
            )
        }
        return render_template(
            "receipts.html", rows=rows, status=status, counts=counts
        )

    @app.route("/receipt/<int:receipt_id>")
    def receipt_detail(receipt_id: int):
        conn = get_db()
        row = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            abort(404)
        items = conn.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY line_no", (receipt_id,)
        ).fetchall()
        txn = None
        if row["transaction_id"]:
            txn = conn.execute(
                "SELECT * FROM transactions WHERE id=?", (row["transaction_id"],)
            ).fetchone()
        allocations = conn.execute(
            """SELECT a.amount_cents, t.id, t.txn_date, t.description_raw
               FROM cash_allocations a JOIN transactions t ON t.id = a.withdrawal_id
               WHERE a.receipt_id = ?""",
            (receipt_id,),
        ).fetchall()
        return render_template(
            "receipt_detail.html",
            r=row,
            items=items,
            txn=txn,
            allocations=allocations,
        )

    @app.route("/receipt/<int:receipt_id>/image")
    def receipt_image(receipt_id: int):
        conn = get_db()
        row = conn.execute(
            "SELECT stored_path FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        if row is None or not row["stored_path"]:
            abort(404)
        path = Path(row["stored_path"]).resolve()
        # Confine reads to the uploads directory so a tampered DB row cannot
        # turn this into an arbitrary file read.
        uploads = Path(cfg.uploads_dir).resolve()
        if not path.is_file() or not path.is_relative_to(uploads):
            abort(404)
        return send_file(path)

    @app.post("/receipt/<int:receipt_id>/update")
    def receipt_update(receipt_id: int):
        conn = get_db()
        if request.form.get("action") == "ignore":
            ignore_receipt(conn, receipt_id)
            flash("Receipt dismissed; it is excluded from every report.", "ok")
            return redirect(url_for("receipts_page"))

        total_raw = (request.form.get("total") or "").strip()
        try:
            total_cents = to_cents(total_raw) if total_raw else None
        except ValueError:
            flash(f"Could not read {total_raw!r} as an amount.", "warn")
            return redirect(url_for("receipt_detail", receipt_id=receipt_id))

        update_receipt(
            conn,
            receipt_id,
            cfg=cfg,
            merchant=(request.form.get("merchant") or "").strip() or None,
            receipt_date=(request.form.get("date") or "").strip() or None,
            total_cents=total_cents,
            tender_type=(request.form.get("tender") or "").strip() or None,
            category=(request.form.get("category") or "").strip() or None,
        )
        row = conn.execute(
            "SELECT link_status, match_reason FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        flash(f"Saved. This slip is now {row['link_status']}: {row['match_reason']}", "ok")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    @app.post("/transaction/<int:txn_id>/category")
    def set_txn_category(txn_id: int):
        conn = get_db()
        category = (request.form.get("category") or "").strip()
        if not category:
            abort(400)
        learn = request.form.get("learn") == "1"
        try:
            loader.set_category(conn, txn_id, category, create_rule=learn)
        except LookupError:
            abort(404)
        flash(
            f"Set to {category}."
            + (" Future imports for this merchant will use it too." if learn else ""),
            "ok",
        )
        return redirect(request.referrer or url_for("transactions"))

    @app.route("/upload", methods=["GET", "POST"])
    def upload():
        conn = get_db()
        results: list[dict] = []

        if request.method == "POST":
            kind = request.form.get("kind")
            files = [f for f in request.files.getlist("files") if f and f.filename]
            if not files:
                flash("No files were selected.", "warn")
                return redirect(url_for("upload"))

            account_name = (request.form.get("account") or "Main Account").strip()

            with tempfile.TemporaryDirectory() as tmpdir:
                for upload_file in files:
                    safe_name = Path(upload_file.filename).name
                    tmp_path = Path(tmpdir) / safe_name
                    upload_file.save(tmp_path)

                    if kind == "statement":
                        results.append(
                            _handle_statement_upload(
                                conn, tmp_path, cfg, account_name, safe_name
                            )
                        )
                    else:
                        results.append(
                            _handle_receipt_upload(
                                conn, tmp_path, cfg, account_name, safe_name
                            )
                        )

            if kind == "statement":
                rematch_all_receipts(
                    conn,
                    amount_tolerance_cents=cfg.match_amount_tolerance_cents,
                    days_window=cfg.match_days_window,
                )

        accounts = conn.execute("SELECT name FROM accounts ORDER BY name").fetchall()
        statements = conn.execute(
            "SELECT s.*, a.name account FROM statements s "
            "JOIN accounts a ON a.id = s.account_id "
            "ORDER BY s.imported_at DESC LIMIT 25"
        ).fetchall()
        return render_template(
            "upload.html",
            results=results,
            accounts=[a["name"] for a in accounts] or ["Main Account"],
            statements=statements,
            image_types=sorted(SUPPORTED_IMAGE_TYPES),
        )

    @app.route("/categorise")
    def categorise_page():
        conn = get_db()
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(merchant_norm,''),'Unknown') m, COUNT(*) n,
                      SUM(-amount_cents) total, MIN(id) example,
                      MIN(description_raw) sample
               FROM transactions
               WHERE status='active' AND amount_cents<0
                 AND COALESCE(category,'Uncategorised')='Uncategorised'
               GROUP BY 1 ORDER BY total DESC"""
        ).fetchall()
        return render_template("categorise.html", rows=rows)

    @app.errorhandler(413)
    def too_large(_exc):  # noqa: ANN001
        flash(
            f"That upload is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit.",
            "warn",
        )
        return redirect(url_for("upload")), 302

    return app


def _handle_statement_upload(
    conn: sqlite3.Connection, tmp_path: Path, cfg: Config, account: str, name: str
) -> dict:
    stored = Path(cfg.uploads_dir) / "statements" / name
    try:
        stored.write_bytes(tmp_path.read_bytes())
    except OSError:
        stored = None  # keep going; the copy is a convenience, not a requirement

    try:
        report = loader.import_statement(
            conn,
            tmp_path,
            cfg=cfg,
            account_name=account,
            stored_path=str(stored) if stored else None,
        )
    except (CsvFormatError, KeyError) as exc:
        return {"name": name, "ok": False, "message": str(exc), "notes": [], "warnings": []}

    return {
        "name": name,
        "ok": True,
        "message": report.summary(),
        "notes": report.detection_notes,
        "warnings": report.warnings,
        "period": (
            f"{report.period_start} to {report.period_end}"
            if report.period_start
            else None
        ),
        "outflow": report.outflow_cents,
        "flagged": report.rows_flagged_duplicate + report.rows_flagged_review,
    }


def _handle_receipt_upload(
    conn: sqlite3.Connection, tmp_path: Path, cfg: Config, account: str, name: str
) -> dict:
    account_id = dbmod.get_or_create_account(conn, account, currency=cfg.currency_code)
    try:
        result = store_receipt(conn, tmp_path, cfg=cfg, account_id=account_id)
    except (ReceiptExtractionError, OSError) as exc:
        return {"name": name, "ok": False, "message": str(exc), "notes": [], "warnings": []}

    if result.is_duplicate:
        return {
            "name": name,
            "ok": True,
            "message": f"already uploaded as receipt #{result.duplicate_of}",
            "notes": [],
            "warnings": [],
            "receipt_id": result.duplicate_of,
        }

    row = conn.execute(
        "SELECT link_status, match_reason FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    data = result.data
    return {
        "name": name,
        "ok": True,
        "message": (
            f"{data.merchant_norm or 'unknown merchant'} · "
            f"{data.receipt_date or 'no date'} · "
            f"{fmt(data.total_cents, cfg.currency_symbol) if data.total_cents else 'no total'} "
            f"[{data.tender_type}] — {row['link_status']}"
        ),
        "notes": [f"read by {data.extractor}", row["match_reason"] or ""],
        "warnings": result.warnings,
        "receipt_id": result.receipt_id,
    }
