import calendar
import csv
import datetime
import difflib
import functools
import hmac
import io
import json
import math
import numbers
import os
import re
import secrets
import zipfile
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
import xlsxwriter

from db import DB_ERRORS, IS_POSTGRES, Database, init_schema

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Lusaka"))
PAYMENT_STATUSES = ("Paid", "Partial Payment", "Unpaid")
PERMISSIONS = ("sell", "procure", "transfer", "delete")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Statutory rules: each contribution has an employee rate (deducted from pay) and an employer rate (paid on top)
STATUTORY_RATES = ("napsa_employee_pct", "napsa_employer_pct", "nhima_employee_pct", "nhima_employer_pct")
HR_DEFAULTS = {
    "non_working_days": "saturday,sunday", "napsa_employee_pct": 5.0, "napsa_employer_pct": 5.0,
    "nhima_employee_pct": 1.0, "nhima_employer_pct": 1.0, "contribution_base": "basic",
}

if ON_VERCEL and not IS_POSTGRES:
    raise RuntimeError("DATABASE_URL is not set. Connect a Postgres database to this Vercel project.")

secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    if ON_VERCEL:
        raise RuntimeError("SECRET_KEY is not set. Add it under Settings > Environment Variables in Vercel.")
    secret_key = "local-development-only"

# Vercel serves public/** from its CDN; locally Flask serves the same folder at the same URL
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "public", "static"), static_url_path="/static")
app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL,
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
)

PUBLIC_DIR = os.path.join(BASE_DIR, "public")


# --- INSTALLABLE APP (PWA) ---
# manifest.json and sw.js live at the project root (public/), not under public/static/, so they are served
# at "/manifest.json" and "/sw.js" - a service worker's scope is the folder it is served from, and both need
# to be at the site root to cover the whole app. On Vercel these two routes are never actually reached: Vercel
# serves everything under public/** straight from its CDN, before a request would reach this Flask app. They
# exist so the same URLs work the same way in local development, where Flask serves public/ itself.
@app.get("/manifest.json")
def pwa_manifest():
    return send_from_directory(PUBLIC_DIR, "manifest.json", mimetype="application/manifest+json")


@app.get("/sw.js")
def pwa_service_worker():
    return send_from_directory(PUBLIC_DIR, "sw.js", mimetype="application/javascript")


init_schema()


class StockError(Exception):
    pass


# --- REQUEST PLUMBING ---
def get_db():
    if "db" not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_field():
    return Markup('<input type="hidden" name="csrf" value="{}">').format(csrf_token())


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    if request.endpoint == "static":
        return

    if request.method == "POST":
        expected = session.get("csrf", "")
        supplied = request.form.get("csrf", "")
        if not expected or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
            abort(400, "This form has expired. Go back, refresh the page and try again.")

    uid = session.get("uid")
    if uid:
        g.user = get_db().one(
            """SELECT u.id, u.business_id, u.username, u.role, u.can_sell, u.can_procure, u.can_transfer, u.can_delete, u.theme, u.glass,
                      b.name AS business_name, b.subtitle, b.contact, b.currency, b.low_stock_threshold,
                      b.alert_phone, b.alert_low_stock, b.alert_permission, b.backup_hour, b.last_backup_at, b.last_backup_by
               FROM users u JOIN businesses b ON b.id = u.business_id WHERE u.id=?""",
            (uid,),
        )
        if g.user is None:
            session.clear()


def can(perm):
    user = g.get("user")
    if not user:
        return False
    if user["role"] == "admin":
        return True
    return perm in PERMISSIONS and bool(user["can_" + perm])


def need(perm):
    """Requires a signed-in user with the given permission ('admin' means admins only, None means any user)."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if perm is not None and not can(perm):
                flash("Your account does not have permission to do that. Ask an admin.", "error")
                return redirect(url_for("pos"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def template_globals():
    return {
        "csrf_field": csrf_field, "user": g.get("user"), "can": can, "look": appearance_of(g.get("user")), "themes": THEMES,
        "asset": asset, "csrf_token": csrf_token, "inv_link": inv_link,
    }


THEMES = (("dark", "Dark"), ("light", "Light"), ("system", "Match my device"))
DEFAULT_GLASS = 50
# Changes with every deployment, so browsers never keep using an older stylesheet or script
ASSET_VERSION = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:10] or datetime.datetime.now().strftime("%Y%m%d%H%M%S")


def asset(filename):
    return url_for("static", filename=filename, v=ASSET_VERSION)


def appearance_of(user):
    """The theme and transparency (0-100) to draw the page with.

    A signed-in user's saved choice wins. Signed-out pages (login) reuse the look last used on this
    device, kept in a cookie. Anything unset or invalid falls back to the defaults.
    """
    theme = glass = None
    if user:
        theme, glass = user["theme"], user["glass"]
    else:
        remembered = request.cookies.get("look", "").split(":")
        if len(remembered) == 2 and remembered[1].isdigit():
            theme, glass = remembered[0], int(remembered[1])
    return {
        "theme": theme if theme in dict(THEMES) else "dark",
        "glass": glass if isinstance(glass, int) and 0 <= glass <= 100 else DEFAULT_GLASS,
    }


@app.after_request
def remember_look(response):
    user = g.get("user")
    if user:
        look = appearance_of(user)
        value = f"{look['theme']}:{look['glass']}"
        if request.cookies.get("look") != value:
            response.set_cookie("look", value, max_age=365 * 24 * 3600, samesite="Lax", secure=ON_VERCEL, httponly=True)
    return response


@app.route("/appearance", methods=["GET", "POST"])
@need(None)
def appearance():
    """Every user chooses their own look; it follows them to whichever device they sign in on."""
    if request.method == "POST":
        # The page saves in the background the moment a choice is made; the plain form post is the fallback
        background = request.headers.get("X-Requested-With") == "fetch"
        theme = text("theme")
        try:
            glass = number("glass", int)
        except ValueError:
            glass = -1
        valid = theme in dict(THEMES) and 0 <= glass <= 100
        if valid:
            db = get_db()
            db.execute("UPDATE users SET theme=?, glass=? WHERE id=?", (theme, glass, g.user["id"]))
            db.commit()
            g.user["theme"], g.user["glass"] = theme, glass
        if background:
            return ("", 204) if valid else ("Choose a theme and a transparency between 0 and 100.", 400)
        if valid:
            flash("Appearance saved.", "ok")
        else:
            flash("Choose a theme and a transparency between 0 and 100.", "error")
        back_to_settings = request.form.get("back") == "settings" and g.user["role"] == "admin"
        return redirect(url_for("settings" if back_to_settings else "appearance"))
    return render_template("appearance.html")


@app.post("/account/password")
@need(None)
def change_password():
    """Lets a signed-in user (admin or operator) change their own password, proving it is them with their current one."""
    db = get_db()
    back = redirect(url_for("appearance"))
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    current, new, confirm = request.form.get("current_password", ""), request.form.get("new_password", ""), request.form.get("confirm_password", "")
    if not me or not check_password_hash(me["password_hash"], current):
        flash("Your current password is not correct. Nothing was changed.", "error")
    elif len(new) < 8:
        flash("Choose a new password of at least 8 characters.", "error")
    elif new != confirm:
        flash("The two new passwords do not match.", "error")
    elif check_password_hash(me["password_hash"], new):
        flash("That is already your current password. Choose a different one.", "error")
    else:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
        db.commit()
        flash("Your password has been changed.", "ok")
    return back


@app.template_filter("money4")
def money_precise(value):
    """Per-seed costs are tiny (ZMW 0.0625), so they are shown to four decimals instead of two."""
    user = g.get("user")
    return f"{user['currency'] if user else ''}{float(value or 0):,.4f}"


@app.template_filter("money")
def money(value):
    user = g.get("user")
    currency = user["currency"] if user else ""
    amount = float(value or 0)
    return f"{currency}{0.0 if abs(amount) < 0.005 else amount:,.2f}"   # never "-0.00" from rounding


@app.template_filter("signed")
def signed_money(value):
    """An amount's effect on the money held: + for money in, a minus sign for money out."""
    amount = float(value or 0)
    if abs(amount) < 0.005:
        return money(0)
    return ("+ " if amount > 0 else "− ") + money(abs(amount))


def text(key):
    return request.form.get(key, "").strip()


def checked(key, default=True):
    """Reads a checkbox sent as a hidden '0' followed by the box itself sending '1' when ticked, so the box's own
    value always wins over the hidden one, and a field missing entirely (an older client, or a GET) keeps default.
    """
    ticks = request.form.getlist(key)
    return ticks[-1] != "0" if ticks else default


def number(key, kind=float, default=None):
    """Reads a numeric form field. Raises ValueError when it is missing (and no default is given) or not a number."""
    raw = text(key).replace(",", "")
    if not raw:
        if default is None:
            raise ValueError(key)
        return default
    value = kind(raw)
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def now():
    return datetime.datetime.now(TZ)


def payment_status_for(total, paid):
    if paid >= total - 0.005:
        return "Paid"
    return "Partial Payment" if paid > 0.005 else "Unpaid"


def amount_for_status(status_text, amount_text, total):
    """Works out the amount paid from a Paid / Partial Payment / Unpaid choice. Raises ValueError with the reason."""
    status = status_text.strip().lower()
    if status in ("", "paid"):
        return total
    if status == "unpaid":
        return 0.0
    if not status.startswith("partial"):
        raise ValueError(f"payment status '{status_text}' must be Paid, Partial Payment or Unpaid")
    try:
        amount = round(parse_amount(amount_text), 2)
    except ValueError:
        raise ValueError("a partial payment needs the amount paid") from None
    if not 0 < amount < total:
        raise ValueError("a partial payment must be more than zero and less than the total")
    return amount


def update_payment(db, kind, ref_id, total, old_paid):
    """Reads the admin payment form, logs the change and returns the new amount paid. Raises ValueError on bad input."""
    if request.form.get("mark_paid"):
        new_paid = total
    else:
        new_paid = round(number("amount_paid"), 2)
        if not 0 <= new_paid <= total + 0.005:
            raise ValueError("amount_paid")
        new_paid = min(new_paid, total)
    db.execute(
        "INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (g.user["business_id"], kind, ref_id, old_paid, new_paid, text("note")[:200], g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return new_paid


def payment_history(db, kind, ref_id):
    return db.all(
        "SELECT old_paid, new_paid, note, changed_by, changed_at FROM payment_log WHERE business_id=? AND kind=? AND ref_id=? ORDER BY id DESC",
        (g.user["business_id"], kind, ref_id),
    )


WALK_IN = "Walk-in Customer"


def name_key(name):
    key = " ".join(name.lower().split())
    return "" if key == WALK_IN.lower() else key


def phone_key(phone):
    """Digits only, with Zambia's +260 prefix folded to a leading 0 so both spellings find the same account."""
    digits = re.sub(r"\D", "", phone or "")
    return "0" + digits[3:] if digits.startswith("260") and len(digits) == 12 else digits


def customer_for(db, biz, name, phone, when):
    """Finds or opens the customer account for a sale. Returns None for an anonymous walk-in (no name and no phone)."""
    name, phone = (name or "").strip(), (phone or "").strip()
    nkey, pkey = name_key(name), phone_key(phone)
    if not nkey and not pkey:
        return None

    exact = db.one("SELECT id FROM customers WHERE business_id=? AND name_key=? AND phone_key=?", (biz, nkey, pkey))
    if exact:
        return exact["id"]
    merged = db.one(
        """SELECT c.id FROM customer_aliases a JOIN customers c ON c.id = a.customer_id
           WHERE a.business_id=? AND a.name_key=? AND a.phone_key=?""",
        (biz, nkey, pkey),
    )
    if merged:
        return merged["id"]
    # Only one of the two details was given: use the account it points to when there is exactly one
    if not pkey or not nkey:
        column, value = ("name_key", nkey) if not pkey else ("phone_key", pkey)
        matches = db.all(f"SELECT id FROM customers WHERE business_id=? AND {column}=? LIMIT 2", (biz, value))
        if len(matches) == 1:
            return matches[0]["id"]
    return db.insert(
        "INSERT INTO customers (business_id, name, phone, name_key, phone_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (biz, name if nkey else "Unnamed customer", phone, nkey, pkey, when),
    )


@app.template_filter("month")
def month_label(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m").strftime("%B %Y")
    except (ValueError, TypeError):
        return value


def csv_safe(value):
    # A leading = or @ (or a +/- that is not a number or phone) makes spreadsheet apps run the cell as a formula
    if value is None:
        return ""
    if isinstance(value, str) and value and (
        value[0] in "=@\t\r" or (value[0] in "+-" and not re.fullmatch(r"[+-][\d\s().,-]*", value))
    ):
        return "'" + value
    return value


def csv_response(filename, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(value) for value in row])
    # utf-8-sig adds the marker Excel needs to read accented characters correctly
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def department_names(db, biz):
    return [row["name"] for row in db.all("SELECT name FROM departments WHERE business_id=? ORDER BY name", (biz,))]


# --- CATEGORIES (the tiles inventory is organised by) ---
def norm(value):
    """Capital letters, spacing and stray whitespace do not make two names different."""
    return " ".join((value or "").split()).lower()


def clean_name(value, limit=60):
    return " ".join((value or "").split())[:limit]


def category_rows(db, biz):
    return db.all("SELECT id, name FROM categories WHERE business_id=? ORDER BY name", (biz,))


def resolve_category(db, biz, typed):
    """The managed category a typed name belongs to; blank means General. A name that is new is added to the list.

    The caller commits, so a purchase that fails afterwards leaves no stray category behind.
    """
    label = clean_name(typed) or "General"
    for row in db.all("SELECT name FROM categories WHERE business_id=?", (biz,)):
        if norm(row["name"]) == norm(label):
            return row["name"]
    db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, label))
    return label


def sync_categories():
    """Makes the category list match the items already in stock and gives every item the one canonical spelling.

    Runs at start-up. It is what turns the free-text categories of older data into tiles, and it is safe to
    repeat: once everything is canonical it changes nothing.
    """
    db = Database()
    try:
        for biz in [row["id"] for row in db.all("SELECT id FROM businesses")]:
            known = {norm(row["name"]): row["name"] for row in category_rows(db, biz)}
            if "general" not in known:
                db.execute("INSERT INTO categories (business_id, name) VALUES (?, 'General')", (biz,))
                known["general"] = "General"

            spellings = {}      # normalised name -> {spelling: items using it}
            raw_values = []     # (value stored on items, spelling it stands for)
            for row in db.all("SELECT category, COUNT(*) AS n FROM products WHERE business_id=? GROUP BY category", (biz,)):
                label = clean_name(row["category"]) or "General"
                spellings.setdefault(norm(label), {})[label] = spellings.get(norm(label), {}).get(label, 0) + row["n"]
                raw_values.append((row["category"], label))

            for key, options in spellings.items():
                if key not in known:
                    # The most used spelling wins; a capitalised one wins a tie
                    best = max(options, key=lambda s: (options[s], s[:1].isupper(), s))
                    db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, best))
                    known[key] = best

            for raw, label in raw_values:
                canonical = known[norm(label)]
                if raw == canonical:
                    continue
                if raw is None:
                    db.execute("UPDATE products SET category=? WHERE business_id=? AND category IS NULL", (canonical, biz))
                else:
                    db.execute("UPDATE products SET category=? WHERE business_id=? AND category=?", (canonical, biz, raw))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()


sync_categories()


def next_invoice_no(db, biz):
    year = now().strftime("%Y")
    # Longest then highest = the largest sequence, whatever order invoices were entered or imported in
    last = db.one(
        "SELECT invoice_no FROM invoices WHERE business_id=? AND invoice_no LIKE ? ORDER BY LENGTH(invoice_no) DESC, invoice_no DESC LIMIT 1",
        (biz, f"INV-{year}-%"),
    )
    seq = 1
    if last:
        try:
            seq = int(last["invoice_no"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            pass
    # Imported invoices can carry any number, so make sure the candidate really is free. A number whose invoice was
    # deleted stays taken, so the deletion record can never be confused with a later invoice.
    while (db.one("SELECT id FROM invoices WHERE business_id=? AND invoice_no=?", (biz, f"INV-{year}-{seq:04d}"))
           or db.one("SELECT id FROM deleted_invoices WHERE business_id=? AND invoice_no=?", (biz, f"INV-{year}-{seq:04d}"))):
        seq += 1
    return f"INV-{year}-{seq:04d}"


# --- ACCOUNTS ---
def signup_open(db):
    # Only the first account can be created freely; set ALLOW_SIGNUP=1 to let further businesses register
    if os.environ.get("ALLOW_SIGNUP") == "1":
        return True
    return db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    if not signup_open(db):
        flash("Registration is closed. Ask your admin for an operator account.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        business, username, password = text("business_name"), text("username"), request.form.get("password", "")
        if not business or not username:
            flash("Business name and username are required.", "error")
        elif len(password) < 8:
            flash("Choose a password of at least 8 characters.", "error")
        elif password != request.form.get("confirm", ""):
            flash("The two passwords do not match.", "error")
        elif db.one("SELECT id FROM users WHERE username=?", (username,)):
            flash("That username is already taken.", "error")
        else:
            try:
                biz = db.insert(
                    "INSERT INTO businesses (name, subtitle, contact, cashbook_ready) VALUES (?, ?, ?, 1)",
                    (business, text("subtitle"), text("contact")),
                )
                db.execute("INSERT INTO departments (business_id, name) VALUES (?, 'General')", (biz,))
                db.execute("INSERT INTO categories (business_id, name) VALUES (?, 'General')", (biz,))
                uid = db.insert(
                    """INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete)
                       VALUES (?, ?, ?, 'admin', 1, 1, 1, 1)""",
                    (biz, username, generate_password_hash(password)),
                )
                db.commit()
            except DB_ERRORS:
                db.rollback()
                flash("The account could not be created. Please try again.", "error")
            else:
                session.clear()
                session["uid"] = uid
                session.permanent = True
                return redirect(url_for("pos"))

    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    if g.user:
        return redirect(url_for("pos"))

    if request.method == "POST":
        found = db.one("SELECT id, password_hash FROM users WHERE username=?", (text("username"),))
        if found and check_password_hash(found["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = found["id"]
            session.permanent = True
            return redirect(url_for("pos"))
        flash("Invalid username or password.", "error")
    elif db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0:
        return redirect(url_for("setup"))

    return render_template("login.html", signup_open=signup_open(db))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- SALES & POS ---
@app.get("/")
@need(None)
def pos():
    db, biz = get_db(), g.user["business_id"]
    release_plantings(db, biz)
    products = db.all(
        "SELECT id, item_code, name, department, date_planted, quantity, price FROM pos_products WHERE business_id=? AND quantity > 0 ORDER BY name",
        (biz,),
    )
    units = db.one(
        """SELECT COALESCE(SUM(ii.qty), 0) AS n FROM invoice_items ii
           JOIN invoices i ON i.id = ii.invoice_id WHERE i.business_id=? AND i.kind='SALE' AND i.payment_status <> 'Cancelled'""",
        (biz,),
    )["n"]
    sales = db.one(
        "SELECT COALESCE(SUM(amount_paid), 0) AS received, COALESCE(SUM(balance_due), 0) AS owed FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled'",
        (biz,),
    )
    spend = db.one(
        "SELECT COALESCE(SUM(amount_paid), 0) AS paid, COALESCE(SUM(total_cost - amount_paid), 0) AS owed FROM procurements WHERE business_id=?",
        (biz,),
    )
    stats = {
        "listed": len(products),
        "low": sum(1 for p in products if p["quantity"] < g.user["low_stock_threshold"]),
        "units": units,
        "revenue": sales["received"],
        "receivable": sales["owed"],
        "expenditure": spend["paid"],
        "payable": spend["owed"],
    }
    known_customers = db.all("SELECT name, phone FROM customers WHERE business_id=? ORDER BY name LIMIT 1000", (biz,))
    return render_template(
        "pos.html", products=products, stats=stats, departments=department_names(db, biz), statuses=PAYMENT_STATUSES, methods=PAYMENT_METHODS,
        known_customers=known_customers, up_next=up_next_summary(db, biz), removal_reasons=POS_REMOVAL_REASONS,
    )


POS_REMOVAL_REASONS = (("disease", "Disease"), ("damaged", "Damaged / spoiled"), ("mortality", "Mortality"), ("other", "Other"))


def record_stock_adjustment(db, biz, kind, item_code, item_name, department, before, after, reason, note, pos_product_id=None, product_id=None):
    """Logs a manual stock change (a POS removal or an inventory quantity correction) so it stays reviewable."""
    db.execute(
        """INSERT INTO stock_adjustments (business_id, kind, item_code, item_name, department, quantity_before, quantity_after, delta, reason, note,
                                          pos_product_id, product_id, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, kind, item_code, item_name, department, before, after, after - before, reason, note,
            pos_product_id, product_id, g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


@app.post("/pos/<int:pos_id>/remove")
@need("transfer")
def remove_from_pos(pos_id):
    """Takes stock listed in the POS off sale - disease, damage, mortality or anything else - with a reason kept on record."""
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("pos"))
    p = db.one("SELECT id, item_code, name, department, quantity FROM pos_products WHERE id=? AND business_id=?", (pos_id, biz))
    if p is None:
        abort(404)

    reason_key = text("reason")
    reason_labels = dict(POS_REMOVAL_REASONS)
    if reason_key not in reason_labels:
        flash("Choose why this stock is being removed.", "error")
        return back
    try:
        qty = number("quantity", int)
        if qty < 1:
            raise ValueError
    except ValueError:
        flash("Enter how many to remove (a whole number above zero).", "error")
        return back
    if qty > p["quantity"]:
        flash(f"Only {p['quantity']:,} of '{p['name']}' are listed. Nothing was removed.", "error")
        return back
    note = text("note")[:300]
    if reason_key == "other" and not note:
        flash("Say what happened, since the reason is 'Other'.", "error")
        return back

    new_qty = p["quantity"] - qty
    try:
        db.execute("UPDATE pos_products SET quantity=? WHERE id=? AND business_id=?", (new_qty, pos_id, biz))
        record_stock_adjustment(
            db, biz, "pos_removal", p["item_code"], p["name"], p["department"], p["quantity"], new_qty,
            reason_labels[reason_key], note, pos_product_id=pos_id,
        )
        notify(
            db, "stock_removal", f"Stock removed from the POS: {p['name']}",
            f"{g.user['username']} removed {qty:,} of '{p['name']}' (code {p['item_code']}) from the POS. Reason: {reason_labels[reason_key]}."
            + (f" {note}" if note else ""),
            url_for("stock_adjustments"),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The removal could not be saved. Please try again.", "error")
        return back

    flash(f"Removed {qty:,} of '{p['name']}' from the POS ({reason_labels[reason_key]}). {new_qty:,} remain listed.", "ok")
    return back


@app.post("/checkout")
@need("sell")
def checkout():
    db, biz = get_db(), g.user["business_id"]

    # The browser only says which products and how many; names, prices and stock come from the database
    wanted = {}
    try:
        for line in json.loads(request.form.get("cart", "[]")):
            pid, qty = int(line["id"]), int(line["qty"])
            if qty <= 0:
                raise ValueError("qty")
            wanted[pid] = wanted.get(pid, 0) + qty
    except (ValueError, KeyError, TypeError):
        wanted = {}
    if not wanted:
        flash("Add at least one item to the cart before completing the invoice.", "error")
        return redirect(url_for("pos"))

    lines, total = [], 0.0
    for pid, qty in wanted.items():
        p = db.one(
            "SELECT id, item_code, name, department, batch_no, quantity, price FROM pos_products WHERE id=? AND business_id=?",
            (pid, biz),
        )
        if not p or qty > p["quantity"]:
            available = p["quantity"] if p else 0
            flash(f"Insufficient stock for {p['name'] if p else 'an item'}. Available: {available}.", "error")
            return redirect(url_for("pos"))
        line_total = round(qty * p["price"], 2)
        lines.append((p, qty, line_total))
        total += line_total
    total = round(total, 2)

    status = text("payment_status")
    if status not in PAYMENT_STATUSES:
        status = "Paid"
    if status == "Paid":
        amount_paid = total
    elif status == "Unpaid":
        amount_paid = 0.0
    else:
        try:
            amount_paid = round(number("amount_paid"), 2)
        except ValueError:
            flash("Enter the amount paid for a partial payment.", "error")
            return redirect(url_for("pos"))
        if amount_paid <= 0 or amount_paid >= total:
            flash("A partial payment must be more than zero and less than the invoice total.", "error")
            return redirect(url_for("pos"))
    # Whatever is paid now goes to Accounts with how it was paid: cash, bank or mobile money
    method, reference = NOT_RECORDED, ""
    if amount_paid > 0.005:
        method, reference, problem = read_money_method()
        if problem:
            flash(problem, "error")
            return redirect(url_for("pos"))

    try:
        sold_at = now().strftime("%Y-%m-%d %H:%M:%S")
        invoice_no, customer = next_invoice_no(db, biz), text("customer_name") or WALK_IN
        invoice_id = db.insert(
            """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id)
               VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, invoice_no, customer, text("customer_phone"),
                status, total, amount_paid, round(total - amount_paid, 2), g.user["username"], sold_at,
                customer_for(db, biz, text("customer_name"), text("customer_phone"), sold_at),
            ),
        )
        record_money(db, biz, "in", amount_paid, method, "sale", reference, customer, f"Invoice {invoice_no}", "invoice", invoice_id, sold_at)
        for p, qty, line_total in lines:
            # The quantity check in the WHERE clause stops two tills selling the same last units
            moved = db.execute(
                "UPDATE pos_products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
                (qty, p["id"], biz, qty),
            )
            if moved.rowcount != 1:
                raise StockError(p["name"])
            low_stock_alert(db, p["name"], p["item_code"], p["quantity"], p["quantity"] - qty, "listed for sale in the POS")
            db.execute(
                """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (invoice_id, biz, p["item_code"], p["name"], p["department"], p["batch_no"], qty, p["price"], line_total),
            )
        db.commit()
    except StockError as e:
        db.rollback()
        flash(f"'{e}' was just sold elsewhere and is now short. Nothing was saved; please review the cart.", "error")
        return redirect(url_for("pos"))
    except DB_ERRORS:
        db.rollback()
        flash("The sale could not be saved. Please try again.", "error")
        return redirect(url_for("pos"))

    return redirect(url_for("invoice", invoice_id=invoice_id))


# --- INVENTORY ---
ITEM_COLUMNS = "id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date, seeds_per_pack, unit"

# --- Packaging unit: what a quantity of an item is counted in (bag, box, kg, litre...). Seed items are always packs. ---
DEFAULT_UNITS = ("bag", "bottle", "box", "bundle", "carton", "kg", "litre", "pack", "piece", "roll", "sachet", "tin", "tray")
SEED_UNIT = "pack"


def canonical_unit(typed, known):
    """A unit name cleaned up, using the spelling already in use when it only differs by capitals or spacing.

    known maps normalised names to the spelling in use. A blank unit stays blank: it means none has been set.
    """
    label = clean_name(typed, 30)
    return known.get(norm(label), label) if label else ""


def used_units(db, biz):
    return {norm(r["unit"]): r["unit"] for r in db.all("SELECT DISTINCT unit FROM products WHERE business_id=? AND unit IS NOT NULL AND unit <> ''", (biz,))}


def resolve_unit(db, biz, typed):
    return canonical_unit(typed, {**{norm(u): u for u in DEFAULT_UNITS}, **used_units(db, biz)})


def unit_choices(db, biz):
    """Suggestions for the unit box: the usual ones plus whatever this business already uses."""
    return sorted({**{norm(u): u for u in DEFAULT_UNITS}, **used_units(db, biz)}.values(), key=str.lower)

# --- Greenhouse: seed packs are bought, sown, and the seedlings are what goes on sale ---
# An item is a seed item when its category contains the word "seed" or "seeds" ("Seeds", "Vegetable seeds").
# "Seedlings" is deliberately not matched. Stock of a seed item is counted in packs.
SEED_WORD = re.compile(r"\bseeds?\b", re.I)
SEEDLING_SUFFIX = "-SDL"


def is_seed_category(category):
    return bool(SEED_WORD.search(category or ""))


def seeds_per_pack_for(existing, category, typed):
    """The pack size to keep for a purchase. Returns (seeds per pack or None, problem or None).

    Non-seed items have none. For a seed item the form or file must say how many seeds a pack holds,
    unless the item is already in stock with a pack size. Stock is counted in packs, so one item code
    cannot switch pack size without muddling the count.
    """
    stored = existing["seeds_per_pack"] if existing else None
    if not is_seed_category(existing["category"] if existing else category):
        return None, None
    typed = (typed or "").strip()
    if typed:
        try:
            size = parse_whole(typed)
            if size < 1:
                raise ValueError
        except ValueError:
            return None, f"seeds per pack '{typed}' must be a whole number above zero"
        if stored and stored != size:
            return None, f"this item is already counted in packs of {stored:,} seeds; use a new item code for packs of {size:,}"
        return size, None
    if stored:
        return stored, None
    return None, "seeds per pack is needed for a seed item (how many seeds are in one pack)"


def seedling_name_for(seed_name):
    """'Tomato Seed' becomes 'Tomato seedlings'; a name without the word gets 'seedlings' added."""
    renamed, count = SEED_WORD.subn("seedlings", seed_name, count=1)
    return renamed if count else f"{seed_name} seedlings"


def round_up_to_cent(value):
    return math.ceil(round(value * 100, 6)) / 100


def seedling_plan(packs, seeds_per_pack, germination_pct, seed_cost):
    """What sowing these packs is expected to give. Seedlings are rounded down: better to under-promise stock."""
    seeds = packs * seeds_per_pack
    seedlings = math.floor(seeds * germination_pct / 100 + 1e-9)
    cost_per_seedling = seed_cost / (germination_pct / 100)
    return {
        "seeds": seeds, "seedlings": seedlings, "cost_per_seedling": cost_per_seedling,
        "break_even_price": round_up_to_cent(cost_per_seedling),
    }


def backfill_units():
    """Seed items are counted in packs, but older ones have no unit recorded. Safe to repeat."""
    db = Database()
    try:
        for biz in [row["id"] for row in db.all("SELECT id FROM businesses")]:
            for row in db.all("SELECT DISTINCT category FROM products WHERE business_id=? AND (unit IS NULL OR unit = '')", (biz,)):
                if is_seed_category(row["category"]):
                    db.execute(
                        "UPDATE products SET unit=? WHERE business_id=? AND category=? AND (unit IS NULL OR unit = '')", (SEED_UNIT, biz, row["category"])
                    )
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()


backfill_units()


def get_item(db, item_id):
    item = db.one(f"SELECT {ITEM_COLUMNS} FROM products WHERE id=? AND business_id=?", (item_id, g.user["business_id"]))
    if item is None:
        abort(404)
    return item


def chosen_department(db, biz):
    dept = text("department")
    return dept if dept in department_names(db, biz) else "General"


INV_PARAMS = ("category", "department", "view", "q", "period", "from", "to")


def inv_link(endpoint, **changes):
    """A link to an inventory page that keeps the filters in force and changes only what is asked."""
    params = {key: value for key, value in request.args.items() if key in INV_PARAMS}
    params.update(changes)
    return url_for(endpoint, **{key: value for key, value in params.items() if value not in (None, "")})


def facets(db, biz, rows, measure):
    """The category tiles and department chips for the rows on screen, plus which of each is selected.

    Each tile counts the rows left after the *other* filter, so a number on a tile is exactly what clicking
    it shows. A name in the address that matches nothing is ignored. Rows are dicts with category and department.
    """
    cats = {norm(r["name"]): r["name"] for r in category_rows(db, biz)}
    depts = {norm(name): name for name in department_names(db, biz)}
    for r in rows:
        cats.setdefault(norm(r["category"] or "General"), clean_name(r["category"]) or "General")
        depts.setdefault(norm(r["department"] or "General"), clean_name(r["department"]) or "General")

    want_cat, want_dept = norm(request.args.get("category")), norm(request.args.get("department"))
    want_cat, want_dept = (want_cat if want_cat in cats else ""), (want_dept if want_dept in depts else "")

    def in_cat(row, key):
        return not key or norm(row["category"] or "General") == key

    def in_dept(row, key):
        return not key or norm(row["department"] or "General") == key

    return {
        "tiles": [
            dict(measure([r for r in rows if in_cat(r, key) and in_dept(r, want_dept)]), name=name, active=key == want_cat, seed=is_seed_category(name))
            for key, name in sorted(cats.items(), key=lambda pair: pair[1].lower())
        ],
        "chips": [
            dict(measure([r for r in rows if in_dept(r, key) and in_cat(r, want_cat)]), name=name, active=key == want_dept)
            for key, name in sorted(depts.items(), key=lambda pair: pair[1].lower())
        ],
        "all_categories": measure([r for r in rows if in_dept(r, want_dept)]),
        "all_departments": measure([r for r in rows if in_cat(r, want_cat)]),
        "category": cats.get(want_cat, ""), "department": depts.get(want_dept, ""),
        "match": lambda r: in_cat(r, want_cat) and in_dept(r, want_dept),
    }


def stock_measure(subset):
    return {"items": len(subset), "units": sum(r["quantity"] for r in subset), "value": sum(r["quantity"] * r["unit_cost"] for r in subset)}


def cost_measure(subset):
    return {"purchases": len(subset), "cost": sum(r["total_cost"] or 0.0 for r in subset)}


@app.get("/inventory")
@need(None)
def inventory():
    db, biz = get_db(), g.user["business_id"]
    everything = db.all(f"SELECT {ITEM_COLUMNS} FROM products WHERE business_id=? ORDER BY name", (biz,))
    search = request.args.get("q", "").strip()
    found = [i for i in everything if not search or norm(search) in norm(i["name"]) or norm(search) in norm(i["item_code"])]

    # Tiles and chips narrow the list; each item is in exactly one category and one department
    facet = facets(db, biz, found, stock_measure)
    items = [i for i in found if facet["match"](i)]

    # Grouped view: "Fertiliser", "fertiliser " and "FERTILISER" are one group, under its most used spelling
    by_category = {}
    for item in items:
        label = clean_name(item["category"]) or "Uncategorised"
        group = by_category.setdefault(norm(label), {"spellings": {}, "entries": [], "units": 0, "value": 0.0, "low": 0})
        group["spellings"][label] = group["spellings"].get(label, 0) + 1
        group["name"] = max(group["spellings"], key=lambda spelling: (group["spellings"][spelling], spelling[:1].isupper(), spelling))
        group["entries"].append(item)
        group["units"] += item["quantity"]
        group["value"] += item["quantity"] * item["unit_cost"]
        group["low"] += 1 if item["quantity"] < g.user["low_stock_threshold"] else 0
    categories = [group for _, group in sorted(by_category.items())]

    return render_template(
        "inventory.html", items=items, categories=categories, view="list" if request.args.get("view") == "list" else "category",
        facet=facet, search=search, has_items=bool(everything), mode="stock",
    )


@app.get("/inventory/new")
@need("procure")
def inventory_new():
    """The purchase form opened from Inventory: the 'also an inventory item' box starts ticked."""
    return purchase_form("inventory")


@app.get("/procurements/new")
@need("procure")
def procurement_new():
    """The same form opened from Procurements: the box starts unticked, so a purchase is only an expense unless it is ticked."""
    return purchase_form("procurements")


def purchase_form(origin):
    """The guided purchase form: is it an inventory item, which category, which department, then the details."""
    db, biz = get_db(), g.user["business_id"]
    items = db.all("SELECT item_code, name, category, department, seeds_per_pack, unit FROM products WHERE business_id=? ORDER BY name LIMIT 5000", (biz,))
    per_category, per_department = {}, {}
    for i in items:
        per_category[norm(i["category"] or "General")] = per_category.get(norm(i["category"] or "General"), 0) + 1
        per_department[norm(i["department"] or "General")] = per_department.get(norm(i["department"] or "General"), 0) + 1

    # Lets the page recognise an item that is already in stock by its code
    known = {
        i["item_code"]: {
            "name": i["name"], "category": i["category"] or "General", "department": i["department"] or "General",
            "seeds": i["seeds_per_pack"] or 0, "unit": i["unit"] or "",
        }
        for i in items
    }
    categories = [{"name": r["name"], "items": per_category.get(norm(r["name"]), 0), "seed": is_seed_category(r["name"])} for r in category_rows(db, biz)]
    departments = [{"name": n, "items": per_department.get(norm(n), 0)} for n in department_names(db, biz)]

    code = request.args.get("code", "").strip()
    start = known.get(code)
    want_cat = norm(start["category"] if start else request.args.get("category"))
    want_dept = norm(start["department"] if start else request.args.get("department"))
    # Restocking a known item (?code=) is always an inventory purchase; otherwise the entry point decides, and ?inventory=1/0 can override it
    asked = request.args.get("inventory")
    as_inventory = bool(start) or (asked == "1" if asked in ("0", "1") else origin == "inventory")
    return render_template(
        "inventory_new.html", origin=origin, as_inventory=as_inventory, categories=categories, methods=PAYMENT_METHODS,
        known_suppliers=db.all("SELECT name, phone FROM suppliers WHERE business_id=? ORDER BY name LIMIT 1000", (biz,)), departments=departments, known=known, today=now().strftime("%Y-%m-%d"), units=unit_choices(db, biz),
        pre_category=next((c["name"] for c in categories if norm(c["name"]) == want_cat), ""),
        pre_department=next((d["name"] for d in departments if norm(d["name"]) == want_dept), ""),
        pre_code=code, pre_name=start["name"] if start else "",
    )


@app.get("/inventory/categories")
@need(None)
def categories_page():
    db, biz = get_db(), g.user["business_id"]
    totals = {norm(r["category"] or "General"): r for r in db.all(
        "SELECT category, COUNT(*) AS items, COALESCE(SUM(quantity), 0) AS units FROM products WHERE business_id=? GROUP BY category", (biz,))}
    tiles = [
        {"id": r["id"], "name": r["name"], "items": totals.get(norm(r["name"]), {}).get("items", 0), "units": totals.get(norm(r["name"]), {}).get("units", 0),
         "seed": is_seed_category(r["name"])}
        for r in category_rows(db, biz)
    ]
    return render_template("categories.html", tiles=tiles)


@app.post("/inventory/categories")
@need("procure")
def add_category():
    db, biz = get_db(), g.user["business_id"]
    from_wizard = text("next") == "new"

    def back(category=""):
        return redirect(url_for("inventory_new", category=category) if from_wizard else url_for("categories_page"))

    name = clean_name(text("category_name"))
    if not name:
        flash("Type a name for the new category.", "error")
        return back()
    existing = next((r["name"] for r in category_rows(db, biz) if norm(r["name"]) == norm(name)), None)
    if existing:
        flash(f"The category '{existing}' already exists." + (" It is selected for you." if from_wizard else ""), "ok" if from_wizard else "error")
        return back(existing)
    try:
        db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, name))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The category could not be added. Please try again.", "error")
        return back()
    note = " Items in it are counted in packs, so each purchase asks how many seeds a pack holds." if is_seed_category(name) else ""
    flash(f"Category '{name}' added.{note}", "ok")
    return back(name)


def retag(db, table, biz, old, new, column="category"):
    """Moves every row of a table from one name to another in a category or department column.

    The table and column come from the code, never from the request. Returns how many rows moved.
    """
    moved = 0
    for row in db.all(f"SELECT DISTINCT {column} AS value FROM {table} WHERE business_id=?", (biz,)):
        if row["value"] is not None and norm(row["value"]) == norm(old):
            moved += db.execute(f"UPDATE {table} SET {column}=? WHERE business_id=? AND {column}=?", (new, biz, row["value"])).rowcount
    return moved


def plan_merge(all_rows, ids, target, kind):
    """Works out merging several categories or departments into one name. Nothing is changed here.

    Returns (row that stays, rows that go, final name, None), or (None, None, None, what is wrong).
    The name given may be one of the ticked ones (that one stays) or a new one. It may not be an unticked
    one, because that would quietly swallow it. General is the default and can only be merged *into*.
    """
    wanted = {i for i in ids}
    chosen = [r for r in all_rows if r["id"] in wanted]
    if len(wanted) < 2:
        return None, None, None, f"Tick at least two {kind}s to merge."
    if len(chosen) != len(wanted):
        return None, None, None, f"Some of the ticked {kind}s no longer exist. Refresh the page and try again."
    target = clean_name(target)
    if not target:
        return None, None, None, f"Type the name for the merged {kind}."
    if any(r["name"] == "General" for r in chosen) and norm(target) != "general":
        return None, None, None, f"General is the default {kind} and cannot be renamed or removed. To merge into it, name the result General."
    clash = next((r for r in all_rows if r["id"] not in wanted and norm(r["name"]) == norm(target)), None)
    if clash:
        return None, None, None, f"There is already a {kind} called '{clash['name']}'. Tick it as well to merge into it."
    keep = next((r for r in chosen if norm(r["name"]) == norm(target)), chosen[0])
    final = keep["name"] if norm(keep["name"]) == norm(target) else target
    return keep, [r for r in chosen if r is not keep], final, None


def read_ids():
    return [int(value) for value in request.form.getlist("ids") if value.isdigit()]


@app.post("/inventory/categories/merge")
@need("admin")
def merge_categories():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("categories_page"))
    keep, others, final, problem = plan_merge(category_rows(db, biz), read_ids(), text("category_name"), "category")
    if problem:
        flash(problem, "error")
        return back
    # A name with the word seed is what makes a category seeds, counted in packs, so seed and other categories do not mix
    if len({is_seed_category(r["name"]) for r in [keep] + others} | {is_seed_category(final)}) > 1:
        flash("Seed categories (counted in packs) and other categories cannot be merged together, and the new name must match: use the word seed in the name only if all of them are seed categories.", "error")
        return back

    try:
        for r in others:
            for table in ("products", "procurements", "pos_products"):
                retag(db, table, biz, r["name"], final)
            db.execute("DELETE FROM categories WHERE id=? AND business_id=?", (r["id"], biz))
        if keep["name"] != final:
            db.execute("UPDATE categories SET name=? WHERE id=? AND business_id=?", (final, keep["id"], biz))
            for table in ("products", "procurements", "pos_products"):
                retag(db, table, biz, keep["name"], final)
        items = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND category=?", (biz, final))["n"]
        purchases = db.one("SELECT COUNT(*) AS n FROM procurements WHERE business_id=? AND category=?", (biz, final))["n"]
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be completed, so nothing was changed. Please try again.", "error")
        return back

    names = ", ".join(f"'{r['name']}'" for r in [keep] + others)
    flash(f"Merged {names} into '{final}'. It now holds {items} item(s) and {purchases} purchase record(s); the others were removed.", "ok")
    return redirect(url_for("categories_page"))


@app.post("/inventory/categories/<int:category_id>/rename")
@need("admin")
def rename_category(category_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, name FROM categories WHERE id=? AND business_id=?", (category_id, biz))
    if row is None:
        abort(404)
    new = clean_name(text("category_name"))
    clash = next((r for r in category_rows(db, biz) if norm(r["name"]) == norm(new) and r["id"] != category_id), None)
    if not new:
        flash("Type the new name.", "error")
    elif row["name"] == "General":
        flash("The General category cannot be renamed.", "error")
    elif clash:
        flash(f"There is already a category called '{clash['name']}'.", "error")
    elif is_seed_category(new) != is_seed_category(row["name"]):
        # A name with the word seed is what makes a category seeds, counted in packs
        flash("That rename would change whether this category counts as seeds (counted in packs), because the word seed in a name is what marks it. Keep or remove the word seed in both names.", "error")
    else:
        try:
            db.execute("UPDATE categories SET name=? WHERE id=? AND business_id=?", (new, category_id, biz))
            for table in ("products", "procurements", "pos_products"):
                retag(db, table, biz, row["name"], new)
            db.commit()
        except DB_ERRORS:
            db.rollback()
            flash("The category could not be renamed. Please try again.", "error")
        else:
            flash(f"'{row['name']}' is now '{new}'. Its items and past purchases moved with it.", "ok")
    return redirect(url_for("categories_page"))


@app.post("/inventory/categories/<int:category_id>/delete")
@need("admin")
def delete_category(category_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, name FROM categories WHERE id=? AND business_id=?", (category_id, biz))
    if row is None:
        abort(404)
    in_use = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND category=?", (biz, row["name"]))["n"]
    if row["name"] == "General":
        flash("The General category cannot be deleted.", "error")
    elif in_use:
        flash(f"'{row['name']}' still has {in_use} item(s). Move them to another category first, then delete it.", "error")
    else:
        db.execute("DELETE FROM categories WHERE id=? AND business_id=?", (category_id, biz))
        db.commit()
        flash(f"Category '{row['name']}' deleted.", "ok")
    return redirect(url_for("categories_page"))


# --- INVENTORY ANALYSIS: what was bought, and what it cost, for any category, department, item and period ---
def analysis_rows(db, biz, start, end):
    """Every purchase in the period, each tagged with the item's *current* category and department.

    Using the item's own category means a purchase logged before an item was recategorised still lands with it;
    purchases of items that have since been deleted keep the category they were logged under.
    """
    sql = """SELECT pr.id, pr.item_code, pr.name, pr.purchase_date, pr.quantity, pr.unit_cost, pr.total_cost, pr.amount_paid, pr.supplier_name, pr.invoice_no,
                    COALESCE(p.category, pr.category) AS category, COALESCE(p.department, pr.department) AS department,
                    p.seeds_per_pack AS seeds_per_pack, p.quantity AS in_stock, p.unit AS unit
             FROM procurements pr LEFT JOIN products p ON p.business_id = pr.business_id AND p.item_code = pr.item_code
             WHERE pr.business_id=? AND COALESCE(pr.in_inventory, 1) = 1"""
    params = [biz]
    if start is not None:
        sql += " AND pr.purchase_date >= ?"
        params.append(start.isoformat())
    if end is not None:
        sql += " AND pr.purchase_date < ?"
        params.append((end + datetime.timedelta(days=1)).isoformat())
    rows = db.all(sql, params)
    for r in rows:
        r["category"] = clean_name(r["category"]) or "General"
        r["department"] = clean_name(r["department"]) or "General"
        r["quantity"] = r["quantity"] or 0
        r["total_cost"] = r["total_cost"] or 0.0
        r["amount_paid"] = r["amount_paid"] if r["amount_paid"] is not None else r["total_cost"]
        r["status"] = payment_status_for(r["total_cost"], r["amount_paid"])
    return rows


def analysis_context():
    db, biz = get_db(), g.user["business_id"]
    choice, start, end = report_period("all")
    search = request.args.get("q", "").strip()
    rows = [r for r in analysis_rows(db, biz, start, end) if not search or norm(search) in norm(r["name"]) or norm(search) in norm(r["item_code"])]
    facet = facets(db, biz, rows, cost_measure)
    chosen = [r for r in rows if facet["match"](r)]

    items = {}
    for r in sorted(chosen, key=lambda r: (r["purchase_date"] or "", r["id"])):
        it = items.setdefault(r["item_code"], {
            "code": r["item_code"], "purchases": 0, "qty": 0, "cost": 0.0, "paid": 0.0, "last": "", "seeds_per_pack": r["seeds_per_pack"],
        })
        # Rows run oldest first, so the name, place and stock left over are those of the latest purchase
        it.update(name=r["name"], category=r["category"], department=r["department"], in_stock=r["in_stock"], last=r["purchase_date"] or "", unit=r["unit"] or "")
        it["purchases"] += 1
        it["qty"] += r["quantity"]
        it["cost"] += r["total_cost"]
        it["paid"] += r["amount_paid"]
    item_list = sorted(items.values(), key=lambda i: (-i["cost"], i["name"].lower()))
    for it in item_list:
        it["avg"] = it["cost"] / it["qty"] if it["qty"] else 0.0

    totals = {
        "items": len(item_list), "purchases": len(chosen), "qty": sum(i["qty"] for i in item_list),
        "cost": sum(i["cost"] for i in item_list), "paid": sum(i["paid"] for i in item_list),
    }
    totals["owed"] = totals["cost"] - totals["paid"]
    return {
        "facet": facet, "mode": "analysis", "search": search, "periods": REPORT_PERIODS, "period": choice, "start": start, "end": end,
        "items": item_list, "totals": totals, "has_items": True,
        "history": sorted(chosen, key=lambda r: (r["purchase_date"] or "", r["id"]), reverse=True)[:300], "history_total": len(chosen),
    }


@app.get("/inventory/analysis")
@need(None)
def inventory_analysis():
    return render_template("inventory_analysis.html", **analysis_context())


@app.get("/inventory/analysis.csv")
@need(None)
def inventory_analysis_csv():
    ctx = analysis_context()
    return csv_response(
        "inventory-analysis.csv",
        ["Item Code", "Item", "Category", "Department", "Unit", "Purchases", "Quantity Bought", "Total Cost", "Average Unit Cost", "Last Purchase", "In Stock Now"],
        [(i["code"], i["name"], i["category"], i["department"], i["unit"], i["purchases"], i["qty"], round(i["cost"], 2), round(i["avg"], 4), i["last"], i["in_stock"])
         for i in ctx["items"]],
    )


@app.post("/inventory/purchase")
@need("procure")
def purchase():
    db, biz = get_db(), g.user["business_id"]
    code, name = text("item_code"), text("name")
    # Every purchase is logged in Procurements. The box says whether it is also an inventory item. The form sends a 0 before the
    # tick, so the last value wins; a post with no such field at all (an older client) counts as an inventory purchase.
    ticks = request.form.getlist("in_inventory")
    inventory = not (ticks and ticks[-1] == "0")
    origin = text("origin") if text("origin") in ("inventory", "procurements") else "inventory"
    # A problem sends the user back to the form with the category and department they had picked
    again = url_for(
        "inventory_new" if origin == "inventory" else "procurement_new", category=text("category"), department=text("department"),
        inventory="1" if inventory else "0",
    )
    try:
        qty = number("quantity", int)
        unit_cost = number("unit_cost")
        price = number("price") if inventory and text("price") else None
    except ValueError:
        flash("Quantity must be a whole number; unit cost and selling price must be numbers.", "error")
        return redirect(again)
    if not name or (inventory and not code) or qty <= 0 or unit_cost < 0 or (price is not None and price < 0):
        flash(
            ("Item code and name are required" if inventory else "A name is required") + ", quantity must be above zero and prices cannot be negative.", "error"
        )
        return redirect(again)
    if not inventory and code and db.one("SELECT 1 AS x FROM products WHERE business_id=? AND item_code=?", (biz, code)):
        flash(f"'{code}' is an item in inventory. Tick 'This is also an inventory item' to add this purchase to its stock, or use a different code.", "error")
        return redirect(again)
    try:
        amount_paid = amount_for_status(text("payment_status"), text("amount_paid"), round(qty * unit_cost, 2))
    except ValueError as e:
        flash(str(e).capitalize() + ".", "error")
        return redirect(again)
    # How it was paid goes to Accounts. Paid to a named supplier it is also kept on their statement, and then it needs its
    # reference number (for cash, the receipt number); with no supplier the reference is optional.
    method, reference = NOT_RECORDED, ""
    if amount_paid > 0.005:
        method, reference, problem = read_payment_details(strict=False) if text("supplier_name") else read_money_method(required=False)
        if problem:
            flash(problem, "error")
            return redirect(again)

    known = db.one("SELECT category, department, seeds_per_pack FROM products WHERE business_id=? AND item_code=?", (biz, code)) if inventory else None
    try:
        # An item already in stock keeps its own category and department; a new one is filed where the form says.
        # A category that is new is added to the list, but only once the whole purchase is saved.
        category = known["category"] if known else resolve_category(db, biz, text("category"))
        department = known["department"] if known else chosen_department(db, biz)

        # Seed items must say how many seeds a pack holds. Something that is not an inventory item has no packs, unit or selling price.
        seeds_per_pack = None
        if inventory:
            seeds_per_pack, problem = seeds_per_pack_for(known, category, text("seeds_per_pack"))
            if problem:
                flash(problem[0].upper() + problem[1:] + ".", "error")
                return redirect(again)

        existing_name = record_purchase(db, biz, {
            "in_inventory": inventory, "amount_paid": amount_paid, "seeds_per_pack": seeds_per_pack,
            "unit": (SEED_UNIT if seeds_per_pack else resolve_unit(db, biz, text("unit"))) if inventory else "",
            "item_code": code, "name": name, "category": category, "department": department,
            "quantity": qty, "unit_cost": unit_cost, "price": price, "supplier_name": text("supplier_name"), "supplier_phone": text("supplier_phone"),
            "payment_method": method, "payment_reference": reference,
            "invoice_no": text("invoice_no"), "purchase_date": text("purchase_date") or now().strftime("%Y-%m-%d"),
            "batch_no": text("batch_no") if inventory else "", "date_planted": text("date_planted") if inventory else "", "expiry_date": text("expiry_date"),
        })
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The purchase could not be saved. Please try again.", "error")
        return redirect(again)

    seed_note = f" That is {qty * seeds_per_pack:,} seeds in {qty:,} pack(s) of {seeds_per_pack:,}." if seeds_per_pack else ""
    if not inventory:
        flash(f"Purchase logged for '{name}' in Procurements. It was not added to inventory.", "ok")
    elif existing_name:
        flash(f"Added {qty} unit(s) to the existing stock of '{existing_name}' (code {code}). It is logged in Procurements too.{seed_note}", "ok")
    else:
        flash(f"Purchase logged for '{name}', added to inventory and recorded in Procurements.{seed_note}", "ok")
    # Back to where the user started, unless that page would not show what they just logged
    return redirect(url_for("inventory" if inventory and origin == "inventory" else "procurements"))


def record_purchase(db, biz, p):
    """Logs one purchase in the procurement log and, when it is an inventory item, adds it to stock too. Shared by the purchase form and the CSV import.

    p["in_inventory"] (default True) says whether the purchase is also an inventory item; if not, only the procurement record is made.
    A blank price (None) keeps an existing item's selling price and uses the unit cost for a new item.
    Returns the existing item's name when the code was already in stock, otherwise None. The caller commits.
    """
    inventory = p.get("in_inventory", True)
    existing = db.one("SELECT id, name, price, category, department, unit FROM products WHERE business_id=? AND item_code=?", (biz, p["item_code"])) if inventory else None
    if existing:
        # A restock is filed where the item already lives, so analysis by category and department stays consistent.
        # It is counted in the item's own unit too; only an item with no unit yet takes the one given.
        p = dict(p, category=existing["category"], department=existing["department"])
        unit = existing["unit"] or p.get("unit") or ""
        price = existing["price"] if p["price"] is None else p["price"]
        db.execute(
            """UPDATE products SET quantity = quantity + ?, unit_cost=?, price=?, batch_no=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?,
                                  seeds_per_pack = COALESCE(?, seeds_per_pack), unit=?
               WHERE id=? AND business_id=?""",
            (p["quantity"], p["unit_cost"], price, p["batch_no"], p["supplier_name"], p["invoice_no"], p["purchase_date"], p["expiry_date"],
             p.get("seeds_per_pack"), unit, existing["id"], biz),
        )
    elif inventory:
        price = p["unit_cost"] if p["price"] is None else p["price"]
        db.execute(
            """INSERT INTO products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date, seeds_per_pack, unit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, p["item_code"], p["name"], p["category"], p["department"], p["batch_no"], p["date_planted"], p["quantity"],
                p["unit_cost"], price, p["supplier_name"], p["invoice_no"], p["purchase_date"], p["expiry_date"], p.get("seeds_per_pack"), p.get("unit") or "",
            ),
        )
    total_cost = round(p["quantity"] * p["unit_cost"], 2)
    paid_now = total_cost if p.get("amount_paid") is None else p["amount_paid"]
    # The supplier named on the purchase gets an account (or the existing one is used), so the purchase lands on their statement
    supplier_id = supplier_for(db, biz, p["supplier_name"], p.get("supplier_phone"), p["purchase_date"] or now().strftime("%Y-%m-%d"))
    procurement_id = db.insert(
        """INSERT INTO procurements (business_id, item_code, name, category, department, supplier_name, invoice_no, purchase_date, quantity, unit_cost, total_cost, expiry_date, amount_paid, in_inventory, supplier_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, p["item_code"], existing["name"] if existing else p["name"], p["category"], p["department"], p["supplier_name"],
            p["invoice_no"], p["purchase_date"], p["quantity"], p["unit_cost"], total_cost, p["expiry_date"],
            paid_now, 1 if inventory else 0, supplier_id,
        ),
    )
    if supplier_id and paid_now > 0.005:
        # What was paid when the purchase was logged is a payment to the supplier too, with its method and reference
        add_supplier_payment(
            db, biz, supplier_id, paid_now, p.get("payment_method") or NOT_RECORDED, p.get("payment_reference") or "", p["purchase_date"],
            "Paid when the purchase was logged", [(procurement_id, paid_now)], apply=False,
        )
    elif paid_now > 0.005:
        # Bought with no supplier account: the money still left the business
        record_money(
            db, biz, "out", paid_now, p.get("payment_method") or NOT_RECORDED, "purchase", p.get("payment_reference") or "", "",
            f"{existing['name'] if existing else p['name']} x {p['quantity']:,}", "purchase", procurement_id, stamp_for_day(p["purchase_date"]),
        )
    if inventory:
        db.execute("UPDATE pos_products SET price=? WHERE business_id=? AND item_code=?", (price, biz, p["item_code"]))
    return existing["name"] if existing else None


@app.get("/inventory/<int:item_id>")
@need(None)
def item(item_id):
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)

    # Greenhouse details for seed items: what the seedlings are listed as now, the cost of one seed, past sowings
    seed = is_seed_category(current["category"])
    listing, batches, per_seed, loose, plantings = None, [], None, 0, []
    if seed:
        loose = db.one("SELECT loose_seeds FROM products WHERE id=? AND business_id=?", (item_id, biz))["loose_seeds"] or 0
        plantings = db.all(
            "SELECT id, name, seeds_planted, planted_on, ready_on, actual_ready_on FROM plantings WHERE business_id=? AND seed_item_id=? ORDER BY id DESC LIMIT 10",
            (biz, item_id),
        )
        size = current["seeds_per_pack"]
        per_seed = current["unit_cost"] / size if size else None
        listing = db.one(
            "SELECT name, price, quantity FROM pos_products WHERE business_id=? AND item_code=?", (biz, current["item_code"] + SEEDLING_SUFFIX)
        )
        batches = db.all(
            """SELECT created_at, packs, seeds_sown, germination_pct, seed_unit_cost, seedlings, cost_per_seedling, selling_price, created_by
               FROM seedling_batches WHERE business_id=? AND seed_code=? ORDER BY id DESC LIMIT 20""",
            (biz, current["item_code"]),
        )

    # The category list for the edit form; an item's own category is always offered, even if it was never added to the list
    category_choices = [r["name"] for r in category_rows(db, biz)]
    if norm(current["category"]) not in {norm(n) for n in category_choices}:
        category_choices.append(current["category"] or "General")

    return render_template(
        "item.html", item=current, departments=department_names(db, biz), category_choices=category_choices, units=unit_choices(db, biz),
        access={action: item_access_state(db, item_id, action) for action, _ in ITEM_ACTIONS}, actions=ITEM_ACTIONS,
        seed=seed, listing=listing, batches=batches, per_seed=per_seed, loose=loose, plantings=plantings, seedling_name=(listing or {}).get("name") or seedling_name_for(current["name"]),
    )


@app.post("/inventory/<int:item_id>/update")
@need(None)
def update_item(item_id):
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)
    grant = item_grant(db, item_id, "procure")
    if grant is None:
        return refuse_item_action(item_id)
    name = text("name")
    try:
        unit_cost = number("unit_cost")
        price = number("price", default=unit_cost)
    except ValueError:
        flash("Unit cost and selling price must be numbers.", "error")
        return redirect(url_for("item", item_id=item_id))
    if not name or unit_cost < 0 or price < 0:
        flash("Item name is required and prices cannot be negative.", "error")
        return redirect(url_for("item", item_id=item_id))

    # A field that was not sent stays as it is, so an edit never clears what it was not asked to change
    category = resolve_category(db, biz, text("category")) if "category" in request.form else current["category"]
    dept = chosen_department(db, biz) if "department" in request.form else current["department"]

    # A seed item always carries its pack size; here an admin can correct a wrong one
    seeds_per_pack = None
    if is_seed_category(category):
        try:
            seeds_per_pack = parse_whole(text("seeds_per_pack")) if text("seeds_per_pack") else current["seeds_per_pack"]
            if not seeds_per_pack or seeds_per_pack < 1:
                raise ValueError
        except ValueError:
            flash("A seed item needs the number of seeds in one pack (a whole number above zero).", "error")
            return redirect(url_for("item", item_id=item_id))

    # The packaging unit. Seed items are always counted in packs; otherwise it is whatever was typed (blank = none set)
    if is_seed_category(category):
        unit = SEED_UNIT
    elif "unit" in request.form:
        unit = resolve_unit(db, biz, text("unit"))
    else:
        unit = current["unit"] or ""

    re_filed = norm(category) != norm(current["category"]) or norm(dept) != norm(current["department"])

    # Item code and quantity are not editable: stock only moves through purchases, transfers and sales
    try:
        db.execute(
            """UPDATE products SET name=?, category=?, department=?, batch_no=?, date_planted=?, unit_cost=?, price=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?,
                                  seeds_per_pack = COALESCE(?, seeds_per_pack), unit=?
               WHERE id=? AND business_id=?""",
            (
                name, category, dept, text("batch_no"), text("date_planted"), unit_cost, price, text("supplier_name"),
                text("invoice_no"), text("purchase_date"), text("expiry_date"), seeds_per_pack, unit, item_id, biz,
            ),
        )
        db.execute(
            "UPDATE pos_products SET name=?, category=?, department=?, price=? WHERE business_id=? AND item_code=?",
            (name, category, dept, price, biz, current["item_code"]),
        )
        # Re-filing an item moves everything that belongs to it: the seedlings grown from it are listed in its department,
        # and its purchase history follows it so Procurements, Reports and the analysis all agree. Past sales stay where
        # they were made, because a sale is something that happened in that department at the time.
        db.execute("UPDATE pos_products SET department=? WHERE business_id=? AND item_code=?", (dept, biz, current["item_code"] + SEEDLING_SUFFIX))
        purchases = db.execute(
            "UPDATE procurements SET category=?, department=? WHERE business_id=? AND item_code=? AND COALESCE(in_inventory, 1) = 1",
            (category, dept, biz, current["item_code"]),
        ).rowcount
        if re_filed:
            # Planting records made from this seed, and the batches already on the POS, follow it too
            db.execute("UPDATE plantings SET department=? WHERE business_id=? AND seed_item_id=?", (dept, biz, item_id))
            db.execute(
                "UPDATE pos_products SET department=? WHERE business_id=? AND item_code IN (SELECT pos_code FROM plantings WHERE business_id=? AND seed_item_id=?)",
                (dept, biz, biz, item_id),
            )
        use_grant(db, grant)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The item could not be updated. Please try again.", "error")
        return redirect(url_for("item", item_id=item_id))

    moved = f" It is now filed under {category} / {dept}, with its {purchases} purchase record(s) and any listings." if re_filed else ""
    flash(f"Details saved for '{name}'.{moved}", "ok")
    return redirect(url_for("item", item_id=item_id))   # stay on the item that was just edited


@app.post("/inventory/<int:item_id>/adjust-quantity")
@need(None)
def adjust_quantity(item_id):
    """Corrects how much of an item is in stock by hand - a stock count, a loss, anything a purchase or transfer didn't capture.

    Always needs a note saying where the difference went (reduced) or came from (increased); kept on record either way.
    """
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)
    grant = item_grant(db, item_id, "procure")
    if grant is None:
        return refuse_item_action(item_id)
    back = redirect(url_for("item", item_id=item_id))

    try:
        new_qty = number("quantity", int)
        if new_qty < 0:
            raise ValueError
    except ValueError:
        flash("Enter the new quantity as a whole number, 0 or more.", "error")
        return back
    note = " ".join(text("note").split())[:300]
    old_qty = current["quantity"]
    if new_qty == old_qty:
        flash("That is already the quantity on record. Nothing was changed.", "ok")
        return back
    if len(note) < 5:
        flash("Say, in a few words, where the difference went (if reducing) or came from (if increasing). Nothing was changed.", "error")
        return back

    try:
        db.execute("UPDATE products SET quantity=? WHERE id=? AND business_id=?", (new_qty, item_id, biz))
        record_stock_adjustment(
            db, biz, "inventory_edit", current["item_code"], current["name"], current["department"], old_qty, new_qty,
            "Increase" if new_qty > old_qty else "Decrease", note, product_id=item_id,
        )
        low_stock_alert(db, current["name"], current["item_code"], old_qty, new_qty, "in inventory")
        use_grant(db, grant)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The quantity could not be saved. Please try again.", "error")
        return back

    change = f"increased by {new_qty - old_qty:,}" if new_qty > old_qty else f"reduced by {old_qty - new_qty:,}"
    flash(f"Quantity {change}, from {old_qty:,} to {new_qty:,}. Noted: {note}", "ok")
    return back


@app.post("/inventory/<int:item_id>/transfer")
@need(None)
def transfer(item_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_item(db, item_id)
    grant = item_grant(db, item_id, "transfer")
    if grant is None:
        return refuse_item_action(item_id)
    destination = text("destination")
    try:
        qty = number("quantity", int)
    except ValueError:
        qty = 0
    if destination not in ("farm", "pos") or qty <= 0 or qty > p["quantity"]:
        flash(f"Choose a destination and a quantity between 1 and {p['quantity']}.", "error")
        return redirect(url_for("item", item_id=item_id))

    # Seed packs are not sold as they are: they are sown, and the seedlings that come up are what goes on sale
    sowing = None
    if destination == "pos" and is_seed_category(p["category"]):
        sowing, problem = read_sowing_form(p, qty)
        if problem:
            flash(problem, "error")
            return redirect(url_for("item", item_id=item_id))

    invoice_id = None
    try:
        moved = db.execute(
            "UPDATE products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
            (qty, item_id, biz, qty),
        )
        if moved.rowcount != 1:
            raise StockError(p["name"])
        low_stock_alert(db, p["name"], p["item_code"], p["quantity"], p["quantity"] - qty, "in inventory")

        if destination == "farm":
            invoice_id = db.insert(
                """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at)
                   VALUES (?, ?, 'FARM_USE', ?, ?, 'INTERNAL', 0, 0, 0, ?, ?)""",
                (biz, next_invoice_no(db, biz), text("recipient") or "Farm Internal", text("phone"), g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            db.execute(
                """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (invoice_id, biz, p["item_code"], p["name"], p["department"], p["batch_no"], qty, p["price"]),
            )
        elif sowing:
            list_seedlings(db, biz, p, qty, sowing)
        else:
            listed = db.one("SELECT id FROM pos_products WHERE business_id=? AND item_code=?", (biz, p["item_code"]))
            if listed:
                db.execute("UPDATE pos_products SET quantity = quantity + ?, price=? WHERE id=?", (qty, p["price"], listed["id"]))
            else:
                db.execute(
                    """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (biz, p["item_code"], p["name"], p["category"], p["department"], p["batch_no"], p["date_planted"], qty, p["price"]),
                )
        use_grant(db, grant)
        db.commit()
    except StockError:
        db.rollback()
        flash("The stock changed while you were working. Nothing was transferred; please try again.", "error")
        return redirect(url_for("item", item_id=item_id))
    except DB_ERRORS:
        db.rollback()
        flash("The transfer could not be saved. Please try again.", "error")
        return redirect(url_for("item", item_id=item_id))

    if invoice_id:
        return redirect(url_for("invoice", invoice_id=invoice_id))
    if sowing:
        flash(
            f"Sowed {qty:,} pack(s) = {sowing['seeds']:,} seeds. About {sowing['seedlings']:,} seedlings are now listed for sale as "
            f"'{sowing['name']}' at {money(sowing['price'])} each (cost {money_precise(sowing['cost_per_seedling'])} per seedling).",
            "ok",
        )
    else:
        flash(f"Transferred {qty} unit(s) of '{p['name']}' to Sales & POS.", "ok")
    return redirect(url_for("inventory"))


def read_sowing_form(item, packs):
    """Reads the greenhouse questions asked when seed packs are moved to the point of sale.

    Returns (details, None) or (None, what is wrong). Nothing is saved here.
    """
    size = item["seeds_per_pack"]
    if not size:
        # A seed item bought before pack sizes were recorded: ask for it now
        try:
            size = number("seeds_per_pack", int)
            if size < 1:
                raise ValueError
        except ValueError:
            return None, "Say how many seeds are in one pack (a whole number above zero)."
    try:
        germination = number("germination_pct")
        if not 0 < germination <= 100:
            raise ValueError
    except ValueError:
        return None, "Enter the estimated germination rate as a percentage above 0 and up to 100."
    try:
        seed_cost = number("seed_cost")
        if seed_cost < 0:
            raise ValueError
    except ValueError:
        return None, "Enter the cost of one seed (0 or more)."

    plan = seedling_plan(packs, size, germination, seed_cost)
    if plan["seedlings"] < 1:
        return None, "At that germination rate these seeds would give less than one seedling."
    try:
        # Left blank, the seedlings are priced at what they cost to raise, rounded up to the next cent
        price = number("seedling_price") if text("seedling_price") else plan["break_even_price"]
        if price < 0:
            raise ValueError
    except ValueError:
        return None, "The selling price per seedling must be a number, 0 or more."
    return dict(
        plan, seeds_per_pack=size, germination=germination, seed_cost=seed_cost, price=round(price, 2),
        name=text("seedling_name")[:100] or seedling_name_for(item["name"]),
    ), None


def list_seedlings(db, biz, item, packs, s):
    """Puts the seedlings expected from a sowing on sale and logs the batch. The caller commits."""
    pos_code = item["item_code"] + SEEDLING_SUFFIX
    sown_on = now().strftime("%Y-%m-%d")
    listed = db.one("SELECT id FROM pos_products WHERE business_id=? AND item_code=?", (biz, pos_code))
    if listed:
        db.execute(
            "UPDATE pos_products SET quantity = quantity + ?, price=?, name=?, department=?, batch_no=?, date_planted=? WHERE id=?",
            (s["seedlings"], s["price"], s["name"], item["department"], item["batch_no"], sown_on, listed["id"]),
        )
    else:
        db.execute(
            """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
               VALUES (?, ?, ?, 'Seedlings', ?, ?, ?, ?, ?)""",
            (biz, pos_code, s["name"], item["department"], item["batch_no"], sown_on, s["seedlings"], s["price"]),
        )
    if not item["seeds_per_pack"]:
        db.execute("UPDATE products SET seeds_per_pack=? WHERE id=? AND business_id=?", (s["seeds_per_pack"], item["id"], biz))
    db.execute(
        """INSERT INTO seedling_batches (business_id, seed_item_id, seed_code, seed_name, pos_code, seedling_name, packs, seeds_per_pack, seeds_sown,
                                         germination_pct, seed_unit_cost, seedlings, cost_per_seedling, selling_price, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, item["id"], item["item_code"], item["name"], pos_code, s["name"], packs, s["seeds_per_pack"], s["seeds"],
            s["germination"], s["seed_cost"], s["seedlings"], s["cost_per_seedling"], s["price"], g.user["username"],
            now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


@app.post("/inventory/<int:item_id>/delete")
@need(None)
def delete_item(item_id):
    db = get_db()
    p = get_item(db, item_id)
    grant = item_grant(db, item_id, "delete")
    if grant is None:
        return refuse_item_action(item_id)
    db.execute("DELETE FROM products WHERE id=? AND business_id=?", (item_id, g.user["business_id"]))
    use_grant(db, grant)
    db.commit()
    flash(f"'{p['name']}' was deleted from inventory.", "ok")
    return redirect(url_for("inventory"))


# --- SALES & INVOICES HISTORY ---
INVOICE_PERIODS = (
    ("all", "All dates"), ("today", "Today"), ("yesterday", "Yesterday"), ("last_7", "Last 7 days"),
    ("this_week", "This week"), ("last_week", "Last week"), ("this_month", "This month"), ("last_month", "Last month"),
    ("this_year", "This year"), ("last_year", "Last year"), ("custom", "Custom range"),
)
HISTORY_PARAMS = ("q", "period", "from", "to")


def parse_day(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def invoice_period(default="all"):
    """The date filter on Sales & Invoices: (choice, first day, last day) from the query string.

    Either day can be None, meaning no limit on that side (both None for all dates). Weeks run Monday to Sunday.
    """
    today = now().date()
    day = datetime.timedelta(days=1)
    choice = request.args.get("period", default)
    start = end = None
    if choice == "today":
        start = end = today
    elif choice == "yesterday":
        start = end = today - day
    elif choice == "last_7":
        start, end = today - 6 * day, today
    elif choice == "this_week":
        start = today - today.weekday() * day
        end = start + 6 * day
    elif choice == "last_week":
        end = today - (today.weekday() + 1) * day
        start = end - 6 * day
    elif choice == "this_month":
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif choice == "last_month":
        end = today.replace(day=1) - day
        start = end.replace(day=1)
    elif choice == "this_year":
        start, end = today.replace(month=1, day=1), today.replace(month=12, day=31)
    elif choice == "last_year":
        start, end = datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    elif choice == "custom":
        # One date alone is an open-ended range: everything from that day, or everything up to it
        start, end = parse_day(request.args.get("from")), parse_day(request.args.get("to"))
        if start and end and end < start:
            start, end = end, start
    else:
        choice = "all"
    return choice, start, end


def history_filter(biz, q, start, end):
    """The WHERE clause shared by the invoice list, its totals and its CSV export, so all three always agree."""
    where, params = "business_id=?", [biz]
    if q:
        like = f"%{q.lower()}%"
        where += """ AND (LOWER(invoice_no) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(customer_phone) LIKE ?
                     OR EXISTS (SELECT 1 FROM invoice_items ii WHERE ii.invoice_id = invoices.id AND LOWER(ii.item_name) LIKE ?))"""
        params += [like, like, like, like]
    # created_at is stored as 'YYYY-MM-DD HH:MM:SS' text, so whole days compare as text, the same way the reports do
    if start:
        where += " AND created_at >= ?"
        params.append(start.isoformat())
    if end:
        where += " AND created_at < ?"
        params.append((end + datetime.timedelta(days=1)).isoformat())
    return where, params


def history_rows(db, where, params, limit):
    sql = f"""SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_at
              FROM invoices WHERE {where} ORDER BY id DESC LIMIT ?"""
    return db.all(sql, params + [limit])


def history_totals(db, where, params):
    """How many invoices match, and the money on them. Cancelled invoices and farm use are not sales, so their
    amounts are left out of the money (the same rule as the reports), though they are still counted and listed."""
    counted = "COALESCE(payment_status, '') <> 'Cancelled' AND COALESCE(kind, 'SALE') <> 'FARM_USE'"
    return db.one(
        f"""SELECT COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN {counted} THEN total ELSE 0 END), 0) AS total,
                   COALESCE(SUM(CASE WHEN {counted} THEN amount_paid ELSE 0 END), 0) AS paid,
                   COALESCE(SUM(CASE WHEN {counted} THEN balance_due ELSE 0 END), 0) AS owed
            FROM invoices WHERE {where}""",
        params,
    )


@app.get("/history")
@need(None)
def history():
    db = get_db()
    q = request.args.get("q", "").strip()
    period, start, end = invoice_period()
    where, params = history_filter(g.user["business_id"], q, start, end)
    csv_args = {key: request.args.get(key) for key in HISTORY_PARAMS if request.args.get(key)}
    return render_template(
        "history.html", invoices=history_rows(db, where, params, 500), totals=history_totals(db, where, params),
        q=q, periods=INVOICE_PERIODS, period=period, start=start, end=end, csv_url=url_for("history_csv", **csv_args),
    )


@app.get("/history.csv")
@need(None)
def history_csv():
    q = request.args.get("q", "").strip()
    _, start, end = invoice_period()
    rows = history_rows(get_db(), *history_filter(g.user["business_id"], q, start, end), 50000)
    return csv_response(
        "sales-invoices.csv",
        ["Invoice No", "Date/Time", "Type", "Customer", "Phone", "Total", "Paid", "Balance", "Status"],
        [
            (r["invoice_no"], r["created_at"], "Farm use" if r["kind"] == "FARM_USE" else "Sale", r["customer_name"],
             r["customer_phone"], r["total"], r["amount_paid"], r["balance_due"], r["payment_status"])
            for r in rows
        ],
    )


@app.get("/invoices/<int:invoice_id>")
@need(None)
def invoice(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id,
                  cancelled_at, cancelled_by, cancel_reason
           FROM invoices WHERE id=? AND business_id=?""",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    lines = db.all(
        "SELECT item_code, item_name, qty, unit_price, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (invoice_id, biz),
    )
    twins = phone_twins(db, biz, inv["customer_id"]) if inv["customer_id"] and g.user["role"] == "admin" else []
    # An operator needs an admin's one-time approval; an admin only needs to confirm with their password
    asked = None if g.user["role"] == "admin" else active_request(db, invoice_id, DELETE_INVOICE)
    if asked and asked["status"] == "approved":
        asked = {**asked, "until": (asked["expires_at"] or "")[11:16]}
    return render_template(
        "invoice.html", inv=inv, lines=lines, payments=payment_history(db, "sale", invoice_id), twins=twins, asked=asked,
        stock_to=("inventory" if inv["kind"] == "FARM_USE" else "the POS"), methods=PAYMENT_METHODS,
        paid_by=money_lines(db, biz, "invoice", invoice_id),
    )


def invoice_stock_table(kind):
    """Where an invoice's items came from, and so where they go back to: farm use drew on inventory, a sale on the POS."""
    return "products" if kind == "FARM_USE" else "pos_products"


@app.post("/invoices/<int:invoice_id>/request-deletion")
@need(None)
def request_invoice_deletion(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT id, invoice_no, customer_name, total FROM invoices WHERE id=? AND business_id=?", (invoice_id, biz))
    if inv is None:
        abort(404)
    back = redirect(url_for("invoice", invoice_id=invoice_id))
    if g.user["role"] == "admin":
        flash("Admins delete invoices directly, with their password.", "ok")
        return back
    if active_request(db, invoice_id, DELETE_INVOICE):
        flash("You already have a request to delete this invoice waiting or approved.", "ok")
        return back
    reason = " ".join(text("reason").split())[:300]
    if len(reason) < 5:
        flash("Say why this invoice should be deleted (at least a few words). The admin reads it before answering.", "error")
        return back

    request_id = db.insert(
        """INSERT INTO permission_requests (business_id, user_id, username, action, item_id, item_name, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (biz, g.user["id"], g.user["username"], DELETE_INVOICE, invoice_id, inv["invoice_no"], reason, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if alert_enabled("permission"):
        notify(
            db, "permission", f"Permission request from {g.user['username']}",
            f"{g.user['username']} asks to delete invoice {inv['invoice_no']} ({inv['customer_name']}, {money(inv['total'])}). Reason: {reason}. Open the app to approve or deny.",
            url_for("permission_requests") + f"#request-{request_id}", whatsapp=True,
        )
    db.commit()
    flash("Your request has been sent to the admin. You will get a popup here when they answer.", "ok")
    return back


@app.post("/invoices/<int:invoice_id>/delete")
@need(None)
def delete_invoice(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at
           FROM invoices WHERE id=? AND business_id=?""",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    back = redirect(url_for("invoice", invoice_id=invoice_id))

    grant = None
    if g.user["role"] == "admin":
        reason = " ".join(text("reason").split())[:300]
        if len(reason) < 5:
            flash("Say why the invoice is being deleted (at least a few words). It is kept in the deletion records. Nothing was deleted.", "error")
            return back
        me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
        if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
            flash("That password is not correct. Nothing was deleted.", "error")
            return back
        approved_by = g.user["username"]
    else:
        # The operator's own words are what was approved, so they are what gets recorded
        row = active_request(db, invoice_id, DELETE_INVOICE)
        if not row or row["status"] != "approved":
            flash("An admin has to approve deleting this invoice first. Use the request button.", "error")
            return back
        grant, reason, approved_by = row, row["reason"], row["decided_by"] or ""

    lines = db.all(
        "SELECT item_code, item_name, department, batch_no, qty, unit_price, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (invoice_id, biz),
    )
    payments = db.all(
        "SELECT old_paid, new_paid, note, changed_by, changed_at FROM payment_log WHERE business_id=? AND kind='sale' AND ref_id=? ORDER BY id",
        (biz, invoice_id),
    )

    # Everything happens in one transaction: either the invoice is gone and recorded, or nothing changed
    try:
        table = invoice_stock_table(inv["kind"])
        missing = []
        if request.form.get("return_stock"):
            for line in lines:
                back_in = db.execute(
                    f"UPDATE {table} SET quantity = quantity + ? WHERE business_id=? AND item_code=?", (line["qty"], biz, line["item_code"]),
                ).rowcount
                if back_in != 1:
                    missing.append(line["item_code"])
            where = "inventory" if table == "products" else "POS stock"
            stock_note = f"Items returned to {where}." if not missing else f"Items returned to {where}, except {', '.join(missing)} (no longer listed)."
        else:
            stock_note = "Items were not returned to stock."

        db.execute("DELETE FROM invoice_items WHERE invoice_id=? AND business_id=?", (invoice_id, biz))
        db.execute("DELETE FROM payment_log WHERE business_id=? AND kind='sale' AND ref_id=?", (biz, invoice_id))
        # A deleted invoice was a mistake, so the money recorded against it goes from Accounts too (its payments stay in the deletion record)
        db.execute("DELETE FROM cashbook WHERE business_id=? AND source='invoice' AND source_id=?", (biz, invoice_id))
        db.execute("DELETE FROM invoices WHERE id=? AND business_id=?", (invoice_id, biz))
        db.insert(
            """INSERT INTO deleted_invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due,
                                             created_by, created_at, items_json, payments_json, stock_note, reason, deleted_by, approved_by, request_id, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, inv["invoice_no"], inv["kind"], inv["customer_name"], inv["customer_phone"], inv["payment_status"], inv["total"],
                inv["amount_paid"], inv["balance_due"], inv["created_by"], inv["created_at"], json.dumps(lines), json.dumps(payments),
                stock_note, reason, g.user["username"], approved_by, grant["id"] if grant else None, now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        use_grant(db, grant)
        if grant:
            notify(
                db, "deletion", f"Invoice {inv['invoice_no']} was deleted",
                f"{g.user['username']} deleted invoice {inv['invoice_no']} ({money(inv['total'])}) with your approval. Reason: {reason}.",
                url_for("deleted_invoices"),
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The invoice could not be deleted, so nothing was changed. Please try again.", "error")
        return back

    flash(f"Invoice {inv['invoice_no']} was deleted and recorded in the deletion records. {stock_note}", "ok")
    return redirect(url_for("history"))


@app.post("/invoices/<int:invoice_id>/cancel")
@need("admin")
def cancel_invoice(invoice_id):
    """Cancels a sale without erasing it: it stays visible, with its original amounts, but owes nothing and is left
    out of revenue, receivables and reports from here on. Admin only, always - no operator request/approval path.
    """
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        "SELECT id, invoice_no, kind, customer_id, customer_name, payment_status, total, amount_paid FROM invoices WHERE id=? AND business_id=? AND kind='SALE'",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    back = redirect(url_for("customer_statement", customer_id=inv["customer_id"]) if text("back") == "statement" and inv["customer_id"] else url_for("invoice", invoice_id=invoice_id))
    if inv["payment_status"] == "Cancelled":
        flash("That invoice has already been cancelled.", "ok")
        return back

    reason = " ".join(text("reason").split())[:300]
    if len(reason) < 5:
        flash("Say why the invoice is being cancelled (at least a few words). Nothing was changed.", "error")
        return back
    # Cancelling moves no money by itself. What the customer had paid is refunded only when that is ticked, and then
    # Accounts records it as money out, with how it was paid back.
    refund = round(inv["amount_paid"], 2) if request.form.get("refund") and inv["amount_paid"] > 0.005 else 0.0
    method, reference = NOT_RECORDED, ""
    if refund:
        method, reference, problem = read_money_method()
        if problem or not text("payment_method"):
            flash(("Choose how the refund was paid." if not text("payment_method") else problem) + " Nothing was changed.", "error")
            return back

    lines = db.all("SELECT item_code, qty FROM invoice_items WHERE invoice_id=? AND business_id=?", (invoice_id, biz))
    try:
        stock_note = ""
        if request.form.get("return_stock"):
            table = invoice_stock_table(inv["kind"])
            missing = []
            for line in lines:
                back_in = db.execute(f"UPDATE {table} SET quantity = quantity + ? WHERE business_id=? AND item_code=?", (line["qty"], biz, line["item_code"])).rowcount
                if back_in != 1:
                    missing.append(line["item_code"])
            stock_note = "Items returned to the POS." if not missing else f"Items returned to the POS, except {', '.join(missing)} (no longer listed)."
        db.execute(
            "UPDATE invoices SET payment_status='Cancelled', balance_due=0, cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=? AND business_id=?",
            (now().strftime("%Y-%m-%d %H:%M:%S"), g.user["username"], reason, invoice_id, biz),
        )
        record_money(
            db, biz, "out", refund, method, "refund", reference, inv["customer_name"], f"Refund on cancelled invoice {inv['invoice_no']}", "invoice", invoice_id,
        )
        notify(
            db, "cancellation", f"Invoice {inv['invoice_no']} was cancelled",
            f"{g.user['username']} cancelled invoice {inv['invoice_no']} ({inv['customer_name']}, {money(inv['total'])}). Reason: {reason}."
            + (f" {money(refund)} was refunded by {method.lower()}." if refund else ""),
            url_for("invoice", invoice_id=invoice_id),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The invoice could not be cancelled. Please try again.", "error")
        return back

    refund_note = f" {money(refund)} was refunded by {method.lower()} and recorded in Accounts." if refund else ""
    flash(f"Invoice {inv['invoice_no']} was cancelled. It stays on record but owes nothing and no longer counts toward sales.{refund_note} {stock_note}".strip(), "ok")
    return back


@app.get("/records/deleted-invoices")
@need("admin")
def deleted_invoices():
    rows = get_db().all(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at,
                  items_json, payments_json, stock_note, reason, deleted_by, approved_by, deleted_at
           FROM deleted_invoices WHERE business_id=? ORDER BY id DESC LIMIT 500""",
        (g.user["business_id"],),
    )
    for r in rows:
        r["lines"] = json.loads(r["items_json"] or "[]")
        r["payments"] = json.loads(r["payments_json"] or "[]")
    return render_template("deleted_invoices.html", rows=rows, total=sum(r["total"] for r in rows))


@app.post("/invoices/<int:invoice_id>/payment")
@need("admin")
def invoice_payment(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        "SELECT invoice_no, customer_name, total, amount_paid, payment_status FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz)
    )
    if inv is None:
        abort(404)
    if inv["payment_status"] == "Cancelled":
        flash("This invoice is cancelled, so its payment cannot be changed.", "error")
        return redirect(url_for("invoice", invoice_id=invoice_id))
    try:
        new_paid = update_payment(db, "sale", invoice_id, inv["total"], inv["amount_paid"])
    except ValueError:
        db.rollback()
        flash(f"Enter an amount between 0 and the invoice total ({money(inv['total'])}).", "error")
        return redirect(url_for("invoice", invoice_id=invoice_id))

    # More paid is money received, with how it came in; less paid is taken back out of the account it is said to come from
    change = round(new_paid - inv["amount_paid"], 2)
    method, reference = CORRECTION, ""
    if abs(change) > 0.005:
        method, reference, problem = read_money_method() if change > 0 else read_correction_method()
        if problem:
            db.rollback()
            flash(problem, "error")
            return redirect(url_for("invoice", invoice_id=invoice_id))
    db.execute(
        "UPDATE invoices SET amount_paid=?, balance_due=?, payment_status=? WHERE id=? AND business_id=?",
        (new_paid, round(inv["total"] - new_paid, 2), payment_status_for(inv["total"], new_paid), invoice_id, biz),
    )
    record_money(
        db, biz, "in", change, method, "customer_payment", reference, inv["customer_name"],
        f"Invoice {inv['invoice_no']}" + (f": {text('note')[:120]}" if text("note") else ""), "invoice", invoice_id,
    )
    db.commit()
    flash("Payment updated.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


# --- PROCUREMENTS ---
PROCUREMENT_COLUMNS = "id, purchase_date, supplier_name, invoice_no, item_code, name, category, department, quantity, unit_cost, total_cost, expiry_date, amount_paid, COALESCE(in_inventory, 1) AS in_inventory, supplier_id"
PROCUREMENT_KINDS = (("", "All purchases"), ("inventory", "Inventory items"), ("expense", "Not in inventory"))


def with_payment_state(row):
    row["balance"] = round((row["total_cost"] or 0.0) - (row["amount_paid"] or 0.0), 2)
    row["status"] = payment_status_for(row["total_cost"] or 0.0, row["amount_paid"] or 0.0)
    return row


def procurement_rows(db, biz, category, kind=""):
    sql = f"SELECT {PROCUREMENT_COLUMNS} FROM procurements WHERE business_id=?"
    params = [biz]
    if category:
        sql += " AND category=?"
        params.append(category)
    if kind == "inventory":
        sql += " AND COALESCE(in_inventory, 1) = 1"
    elif kind == "expense":
        sql += " AND COALESCE(in_inventory, 1) = 0"
    return [with_payment_state(row) for row in db.all(sql + " ORDER BY id DESC", params)]


def get_procurement(db, purchase_id):
    row = db.one(f"SELECT {PROCUREMENT_COLUMNS} FROM procurements WHERE id=? AND business_id=?", (purchase_id, g.user["business_id"]))
    if row is None:
        abort(404)
    return with_payment_state(row)


@app.get("/procurements/<int:purchase_id>")
@need("admin")
def procurement(purchase_id):
    db = get_db()
    p = get_procurement(db, purchase_id)
    return render_template(
        "purchase.html", p=p, payments=payment_history(db, "purchase", purchase_id), methods=PAYMENT_METHODS, ref_required=bool(p["supplier_id"]),
        supplier=db.one("SELECT id, name FROM suppliers WHERE id=? AND business_id=?", (p["supplier_id"], g.user["business_id"])) if p["supplier_id"] else None,
    )


@app.post("/procurements/<int:purchase_id>/payment")
@need("admin")
def procurement_payment(purchase_id):
    db = get_db()
    p = get_procurement(db, purchase_id)
    here = redirect(url_for("procurement", purchase_id=purchase_id))
    if p["supplier_id"]:
        # A purchase from a supplier on file: the change is a payment to that supplier, kept with how it was paid
        try:
            new_paid = p["total_cost"] if request.form.get("mark_paid") else round(number("amount_paid"), 2)
            if not 0 <= new_paid <= p["total_cost"] + 0.005:
                raise ValueError("amount_paid")
            new_paid = min(new_paid, p["total_cost"])
        except ValueError:
            flash(f"Enter an amount between 0 and the purchase total ({money(p['total_cost'])}).", "error")
            return here
        change = round(new_paid - p["amount_paid"], 2)
        if abs(change) < 0.005:
            flash("The amount paid is unchanged.", "ok")
            return here
        paid_on = today_str()
        method, reference, problem = read_payment_details(strict=False) if change > 0 else read_correction_method()
        if problem:
            flash(problem, "error")
            return here
        try:
            add_supplier_payment(db, g.user["business_id"], p["supplier_id"], change, method, reference, paid_on, text("note")[:150], [(purchase_id, change)])
            db.commit()
        except DB_ERRORS:
            db.rollback()
            flash("The payment could not be saved. Please try again.", "error")
            return here
        flash("Payment updated." if change > 0 else "Payment corrected.", "ok")
        return here

    try:
        new_paid = update_payment(db, "purchase", purchase_id, p["total_cost"], p["amount_paid"])
    except ValueError:
        db.rollback()
        flash(f"Enter an amount between 0 and the purchase total ({money(p['total_cost'])}).", "error")
        return redirect(url_for("procurement", purchase_id=purchase_id))

    # No supplier account: the payment goes to Accounts on its own, with how it was paid (or, lowered, the account it goes back to)
    change = round(new_paid - p["amount_paid"], 2)
    method, reference = CORRECTION, ""
    if abs(change) > 0.005:
        method, reference, problem = read_money_method() if change > 0 else read_correction_method()
        if problem:
            db.rollback()
            flash(problem, "error")
            return here
    db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, purchase_id, g.user["business_id"]))
    record_money(
        db, g.user["business_id"], "out", change, method, "purchase", reference, p["supplier_name"] or "",
        f"{p['name']} x {p['quantity']:,}" + (f": {text('note')[:120]}" if text("note") else ""), "purchase", purchase_id,
    )
    db.commit()
    flash("Payment updated.", "ok")
    return redirect(url_for("procurement", purchase_id=purchase_id))


@app.get("/procurements")
@need(None)
def procurements():
    db, biz = get_db(), g.user["business_id"]
    category = request.args.get("category", "").strip()
    kind = request.args.get("kind", "") if request.args.get("kind") in ("inventory", "expense") else ""
    rows = procurement_rows(db, biz, category, kind)
    by_category = {}
    for r in rows:
        label = r["category"] or "Uncategorised"
        by_category[label] = by_category.get(label, 0.0) + (r["total_cost"] or 0.0)
    categories = [c["category"] for c in db.all("SELECT DISTINCT category FROM procurements WHERE business_id=? ORDER BY category", (biz,))]
    return render_template(
        "procurements.html", rows=rows, category=category, categories=categories, kind=kind, kinds=PROCUREMENT_KINDS,
        by_category=sorted(by_category.items()), grand_total=sum(by_category.values()),
        total_paid=sum(r["amount_paid"] or 0.0 for r in rows), total_owed=sum(r["balance"] for r in rows),
    )


@app.get("/procurements.csv")
@need(None)
def procurements_csv():
    kind = request.args.get("kind", "") if request.args.get("kind") in ("inventory", "expense") else ""
    rows = procurement_rows(get_db(), g.user["business_id"], request.args.get("category", "").strip(), kind)
    return csv_response(
        "procurements.csv",
        ["Date", "Supplier", "Invoice No", "Item Code", "Product Name", "Category", "Dept", "Qty", "Unit Cost", "Total Cost", "Expiry", "Amount Paid", "Balance", "Payment Status", "In Inventory"],
        [
            (r["purchase_date"], r["supplier_name"], r["invoice_no"], r["item_code"], r["name"], r["category"], r["department"],
             r["quantity"], r["unit_cost"], r["total_cost"], r["expiry_date"], r["amount_paid"], r["balance"], r["status"], "Yes" if r["in_inventory"] else "No")
            for r in rows
        ],
    )


# --- HR & PAYROLL (ADMIN) ---
def hr_settings_for(db, biz):
    saved = db.one(
        f"SELECT non_working_days, {', '.join(STATUTORY_RATES)}, contribution_base FROM hr_settings WHERE business_id=?", (biz,)
    )
    rules = dict(HR_DEFAULTS)
    rules.update({key: value for key, value in (saved or {}).items() if value is not None})
    return rules


def statutory_amounts(rules, calculated_basic, bonuses_total):
    """The employee and employer share of NAPSA and NHIMA for one payslip. Every new payslip goes through here."""
    base = calculated_basic + (bonuses_total if rules["contribution_base"] == "gross" else 0.0)
    return {rate[:-4]: round(base * rules[rate] / 100.0, 2) for rate in STATUTORY_RATES}


def expected_work_days(year, month, non_working):
    days_in_month = calendar.monthrange(year, month)[1]
    return sum(1 for day in range(1, days_in_month + 1) if WEEKDAYS[datetime.date(year, month, day).weekday()] not in non_working)


def named_amounts(prefix):
    """Reads the repeated name/amount rows of the payslip form into {name: amount}."""
    result = {}
    for name, raw in zip(request.form.getlist(prefix + "_name"), request.form.getlist(prefix + "_amount")):
        name, raw = name.strip(), raw.strip().replace(",", "")
        if not name and not raw:
            continue
        amount = float(raw or 0)
        if not name or not math.isfinite(amount) or amount < 0:
            raise ValueError(prefix)
        if amount:
            result[name] = round(result.get(name, 0.0) + amount, 2)
    return result


@app.get("/hr")
@need("admin")
def hr():
    db, biz = get_db(), g.user["business_id"]
    employees = db.all(
        "SELECT id, full_name, nrc_number, employment_type, basic_salary, expected_days_week, start_date FROM employees WHERE business_id=? ORDER BY full_name",
        (biz,),
    )
    payslips = db.all(
        """SELECT id, pay_month, employee_name, basic_salary, expected_days, worked_days, napsa_amt, nhima_amt, net_payable
           FROM payroll WHERE business_id=? ORDER BY id DESC LIMIT 500""",
        (biz,),
    )
    summary = db.all(
        """SELECT pay_month, COUNT(id) AS slips, COALESCE(SUM(calculated_basic), 0) AS basic,
                  COALESCE(SUM(napsa_amt), 0) AS emp_napsa, COALESCE(SUM(nhima_amt), 0) AS emp_nhima,
                  COALESCE(SUM(employer_napsa), 0) AS empr_napsa, COALESCE(SUM(employer_nhima), 0) AS empr_nhima,
                  COALESCE(SUM(net_payable), 0) AS net
           FROM payroll WHERE business_id=? GROUP BY pay_month ORDER BY pay_month DESC""",
        (biz,),
    )
    return render_template("hr.html", employees=employees, payslips=payslips, summary=summary, today=now().strftime("%Y-%m-%d"))


@app.post("/hr/employees")
@need("admin")
def add_employee():
    db, biz = get_db(), g.user["business_id"]
    name, nrc = text("full_name"), text("nrc_number")
    try:
        salary = number("basic_salary")
        age = number("age", int, default=0) or None
        days_week = number("expected_days_week", int, default=5)
    except ValueError:
        flash("Salary, age and days per week must be numbers.", "error")
        return redirect(url_for("hr"))
    if not name or not nrc or salary < 0 or not 1 <= days_week <= 7:
        flash("Full name and NRC number are required, salary cannot be negative and days per week must be 1 to 7.", "error")
        return redirect(url_for("hr"))

    employment_type = text("employment_type")
    if employment_type not in ("Permanent", "Casual Worker"):
        employment_type = "Permanent"
    db.execute(
        """INSERT INTO employees (business_id, full_name, nrc_number, basic_salary, employment_type, address, age, start_date, expected_days_week)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (biz, name, nrc, round(salary, 2), employment_type, text("address"), age, text("start_date"), days_week),
    )
    db.commit()
    flash(f"Employee '{name}' registered.", "ok")
    return redirect(url_for("hr"))


@app.post("/hr/employees/<int:employee_id>/delete")
@need("admin")
def delete_employee(employee_id):
    db = get_db()
    # Past payslips keep the employee's name, so the payroll history stays intact
    db.execute("DELETE FROM employees WHERE id=? AND business_id=?", (employee_id, g.user["business_id"]))
    db.commit()
    flash("Employee removed. Their past payslips are kept.", "ok")
    return redirect(url_for("hr"))


@app.route("/hr/settings", methods=["GET", "POST"])
@need("admin")
def hr_settings():
    db, biz = get_db(), g.user["business_id"]
    if request.method == "POST":
        non_working = ",".join(day for day in WEEKDAYS if request.form.get("day_" + day))
        base = text("contribution_base")
        try:
            rates = [number(rate) for rate in STATUTORY_RATES]
        except ValueError:
            rates = [-1.0]
        if not all(0 <= rate <= 100 for rate in rates) or base not in ("basic", "gross"):
            flash("Every rate must be a percentage between 0 and 100. Use 0 where a side pays nothing.", "error")
        elif len(non_working.split(",")) == 7:
            flash("At least one day of the week must be a working day.", "error")
        else:
            # napsa_pct, nhima_pct and the two bearer columns belong to the old single-rate rule; they are
            # still filled in because the table requires them, but nothing reads them any more
            values = (non_working, *rates, base, rates[0], rates[2], biz)
            if db.one("SELECT business_id FROM hr_settings WHERE business_id=?", (biz,)):
                db.execute(
                    f"""UPDATE hr_settings SET non_working_days=?, {'=?, '.join(STATUTORY_RATES)}=?, contribution_base=?,
                               napsa_pct=?, nhima_pct=?, napsa_bearer='employee', nhima_bearer='employee' WHERE business_id=?""",
                    values,
                )
            else:
                db.execute(
                    f"""INSERT INTO hr_settings (non_working_days, {', '.join(STATUTORY_RATES)}, contribution_base, napsa_pct, nhima_pct, business_id, napsa_bearer, nhima_bearer)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'employee', 'employee')""",
                    values,
                )
            db.commit()
            flash("Statutory rules saved. They apply to every payslip computed or imported from now on; existing payslips are unchanged.", "ok")
            return redirect(url_for("hr"))
        return redirect(url_for("hr_settings"))

    settings_row = hr_settings_for(db, biz)
    return render_template("hr_settings.html", s=settings_row, weekdays=WEEKDAYS, non_working=settings_row["non_working_days"].split(","))


@app.route("/hr/payslips/new", methods=["GET", "POST"])
@need("admin")
def new_payslip():
    db, biz = get_db(), g.user["business_id"]
    employees = db.all("SELECT id, full_name, basic_salary FROM employees WHERE business_id=? ORDER BY full_name", (biz,))
    if not employees:
        flash("Register an employee first.", "error")
        return redirect(url_for("hr"))
    hr_set = hr_settings_for(db, biz)
    non_working = hr_set["non_working_days"].split(",")

    if request.method == "POST":
        employee = next((e for e in employees if e["id"] == request.form.get("employee_id", type=int)), None)
        try:
            period = datetime.datetime.strptime(text("pay_month"), "%Y-%m")
            expected = number("expected_days", default=float(expected_work_days(period.year, period.month, non_working)))
            worked = number("worked_days")
            bonuses, deductions = named_amounts("bonus"), named_amounts("deduction")
        except ValueError:
            flash("Check the pay month (YYYY-MM), the day counts, and that every bonus or deduction has a name and a positive amount.", "error")
            return redirect(url_for("new_payslip"))
        if employee is None or expected <= 0 or worked < 0:
            flash("Choose an employee; expected days must be above zero and days worked cannot be negative.", "error")
            return redirect(url_for("new_payslip"))
        method, _, problem = read_money_method()
        if problem:
            flash(problem.replace("the money was paid", "the pay was paid"), "error")
            return redirect(url_for("new_payslip"))

        basic = employee["basic_salary"]
        calculated_basic = round(basic / expected * worked, 2)
        # The employee share comes off their pay; the employer share is a cost on top and never touches net pay
        share = statutory_amounts(hr_set, calculated_basic, sum(bonuses.values()))
        emp_napsa, employer_napsa = share["napsa_employee"], share["napsa_employer"]
        emp_nhima, employer_nhima = share["nhima_employee"], share["nhima_employer"]
        net = round(calculated_basic + sum(bonuses.values()) - sum(deductions.values()) - emp_napsa - emp_nhima, 2)

        payslip_id = db.insert(
            """INSERT INTO payroll (business_id, employee_id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic,
                                    bonuses_json, deductions_json, napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, employee["id"], employee["full_name"], period.strftime("%Y-%m"), basic, expected, worked, calculated_basic,
                json.dumps(bonuses), json.dumps(deductions), emp_napsa, emp_nhima, employer_napsa, employer_nhima, net,
                now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        record_payslip_money(
            db, biz, payslip_id, employee["full_name"], period.strftime("%Y-%m"), net, emp_napsa + emp_nhima + employer_napsa + employer_nhima,
            method, now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        db.commit()
        return redirect(url_for("payslips", id=payslip_id))

    return render_template("payslip_new.html", employees=employees, hr=hr_set, this_month=now().strftime("%Y-%m"), methods=PAYMENT_METHODS)


@app.get("/hr/payslips")
@need("admin")
def payslips():
    db, biz = get_db(), g.user["business_id"]
    ids = request.args.getlist("id", type=int)[:200]
    if not ids:
        flash("Tick at least one payslip to print.", "error")
        return redirect(url_for("hr"))
    marks = ",".join("?" for _ in ids)
    slips = db.all(
        f"""SELECT id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic, bonuses_json, deductions_json,
                   napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable
            FROM payroll WHERE business_id=? AND id IN ({marks}) ORDER BY id""",
        [biz] + ids,
    )
    for slip in slips:
        slip["bonuses"] = json.loads(slip["bonuses_json"] or "{}")
        slip["deductions"] = json.loads(slip["deductions_json"] or "{}")
    return render_template("payslips.html", slips=slips)


@app.post("/hr/payslips/<int:payslip_id>/delete")
@need("admin")
def delete_payslip(payslip_id):
    db = get_db()
    db.execute("DELETE FROM payroll WHERE id=? AND business_id=?", (payslip_id, g.user["business_id"]))
    db.execute("DELETE FROM cashbook WHERE business_id=? AND source='payslip' AND source_id=?", (g.user["business_id"], payslip_id))
    db.commit()
    flash("Payslip deleted, with its pay in Accounts.", "ok")
    return redirect(url_for("hr"))


# --- ALERTS: IN-APP NOTIFICATIONS AND WHATSAPP ---
# WhatsApp needs an account with a sending service, set up once through environment variables (never stored
# in the database). Two services are supported:
#   WHATSAPP_PROVIDER=meta       WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_TEMPLATE (an approved template whose body
#                                is a single {{1}} placeholder), optional WHATSAPP_TEMPLATE_LANG and WHATSAPP_API_VERSION
#   WHATSAPP_PROVIDER=callmebot  WHATSAPP_CALLMEBOT_KEY (the key CallMeBot issued to the alert phone number)
# Until one is configured, alerts still appear in the app and the WhatsApp copy is recorded as skipped.
ALERT_KINDS = (("low_stock", "Low stock"), ("permission", "Requests for admin permission"))
ITEM_ACTIONS = (("procure", "edit this item's details"), ("transfer", "transfer or use this item's stock"), ("delete", "delete this item"))
GRANT_MINUTES = 60
# An operator's request to delete one invoice travels through the same request/approve/spend-once flow as the item actions
DELETE_INVOICE = "delete_invoice"
COLLECT_PREORDER = "collect_preorder"
REQUEST_LABELS = {**dict(ITEM_ACTIONS), DELETE_INVOICE: "delete this invoice", COLLECT_PREORDER: "collect a pre-order and record it as a sale"}
WHATSAPP_TIMEOUT = 6


def whatsapp_service():
    """Returns (provider, None) when WhatsApp sending is configured, otherwise (None, why not)."""
    provider = os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
    if provider == "meta":
        missing = [name for name in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_TEMPLATE") if not os.environ.get(name)]
        return (None, "missing " + ", ".join(missing)) if missing else ("meta", None)
    if provider == "callmebot":
        return ("callmebot", None) if os.environ.get("WHATSAPP_CALLMEBOT_KEY") else (None, "missing WHATSAPP_CALLMEBOT_KEY")
    return None, "no WhatsApp service has been connected yet"


def whatsapp_number(phone):
    """Digits with the country code, as WhatsApp wants them. A local Zambian 09... number gains its 260."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "260" + digits[1:]
    return digits if 9 <= len(digits) <= 15 else ""


def send_whatsapp(phone, message):
    """Sends one WhatsApp message. Returns (status, detail) with status 'sent', 'failed' or 'skipped'; never raises."""
    import urllib.error
    import urllib.parse
    import urllib.request

    number_to = whatsapp_number(phone)
    if not number_to:
        return "skipped", "no valid WhatsApp number is saved in Settings"
    provider, problem = whatsapp_service()
    if provider is None:
        return "skipped", problem

    # One line of plain text: template parameters may not contain line breaks or long runs of spaces
    message = " ".join(message.split())[:900]
    try:
        if provider == "meta":
            version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
            payload = {
                "messaging_product": "whatsapp", "recipient_type": "individual", "to": number_to, "type": "template",
                "template": {
                    "name": os.environ["WHATSAPP_TEMPLATE"], "language": {"code": os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": message}]}],
                },
            }
            call = urllib.request.Request(
                f"https://graph.facebook.com/{version}/{os.environ['WHATSAPP_PHONE_ID']}/messages",
                data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Authorization": "Bearer " + os.environ["WHATSAPP_TOKEN"], "Content-Type": "application/json"},
            )
        else:
            query = urllib.parse.urlencode({"phone": "+" + number_to, "text": message, "apikey": os.environ["WHATSAPP_CALLMEBOT_KEY"]})
            call = urllib.request.Request("https://api.callmebot.com/whatsapp.php?" + query, method="GET")
        with urllib.request.urlopen(call, timeout=WHATSAPP_TIMEOUT) as reply:
            answer = reply.read(2000).decode("utf-8", errors="replace")
        if provider == "callmebot" and re.search(r"error|invalid|not\s+(valid|allowed|activated)", answer, re.I):
            return "failed", re.sub(r"<[^>]+>", " ", answer)[:200].strip()
        return "sent", ""
    except urllib.error.HTTPError as e:
        detail = e.read(600).decode("utf-8", errors="replace")
        return "failed", f"the WhatsApp service answered {e.code}: {' '.join(detail.split())[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "failed", f"could not reach the WhatsApp service ({type(e).__name__})"


def alert_enabled(kind):
    """An alert kind is on unless an admin has switched it off in Settings."""
    return g.user["alert_" + kind] != 0


def notify(db, kind, title, body, link, for_user_id=None, whatsapp=False):
    """Records an in-app alert for one user, or for the business's admins when for_user_id is None.

    With whatsapp=True a copy is queued for the number in Settings. It is sent after the response is
    ready, and only if the alert was really saved, so a failed sale never sends a message.
    """
    notification_id = db.insert(
        "INSERT INTO notifications (business_id, for_user_id, kind, title, body, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (g.user["business_id"], for_user_id, kind, title, body, link, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if whatsapp:
        outbox = g.setdefault("whatsapp_outbox", [])
        outbox.append((notification_id, f"{g.user['business_name']}: {title}. {body}"))
    return notification_id


@app.after_request
def deliver_whatsapp(response):
    outbox = g.pop("whatsapp_outbox", None)
    if not outbox or not g.get("user"):
        return response
    db = get_db()
    try:
        for notification_id, message in outbox:
            # Skip anything whose alert was rolled back together with the action that raised it
            if not db.one("SELECT id FROM notifications WHERE id=?", (notification_id,)):
                continue
            status, detail = send_whatsapp(g.user["alert_phone"], message)
            db.execute("UPDATE notifications SET wa_status=?, wa_error=? WHERE id=?", (status, detail, notification_id))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    return response


def low_stock_alert(db, name, code, before, after, where):
    """Raises the alert once, at the moment a quantity drops below the threshold, not on every later sale."""
    threshold = g.user["low_stock_threshold"]
    if alert_enabled("low_stock") and before >= threshold > after:
        notify(
            db, "low_stock", f"Low stock: {name}",
            f"{name} (code {code}) is down to {after} {where}. The alert level is {threshold}.",
            url_for("inventory"), whatsapp=True,
        )


def my_notifications_clause():
    """SQL and parameters selecting the alerts the signed-in user should see."""
    if g.user["role"] == "admin":
        return "business_id=? AND (for_user_id=? OR for_user_id IS NULL)", [g.user["business_id"], g.user["id"]]
    return "business_id=? AND for_user_id=?", [g.user["business_id"], g.user["id"]]


@app.get("/notifications/poll")
@need(None)
def notifications_poll():
    where, params = my_notifications_clause()
    rows = get_db().all(
        f"SELECT id, kind, title, body, created_at FROM notifications WHERE {where} AND read_at IS NULL ORDER BY id DESC LIMIT 20", params
    )
    for row in rows:
        row["open"] = url_for("open_notification", notification_id=row["id"])
    return {"unread": len(rows), "items": rows}


@app.get("/notifications/<int:notification_id>/open")
@need(None)
def open_notification(notification_id):
    """Marks one alert as read and goes to what it is about."""
    db = get_db()
    where, params = my_notifications_clause()
    row = db.one(f"SELECT id, link FROM notifications WHERE {where} AND id=?", params + [notification_id])
    if row is None:
        abort(404)
    db.execute("UPDATE notifications SET read_at=? WHERE id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), notification_id))
    db.commit()
    # Links are written by this app; anything that is not a local path is ignored
    link = row["link"] or ""
    return redirect(link if link.startswith("/") and not link.startswith("//") else url_for("notifications"))


@app.route("/notifications", methods=["GET", "POST"])
@need(None)
def notifications():
    db = get_db()
    where, params = my_notifications_clause()
    if request.method == "POST":
        db.execute(f"UPDATE notifications SET read_at=? WHERE {where} AND read_at IS NULL", [now().strftime("%Y-%m-%d %H:%M:%S")] + params)
        db.commit()
        return redirect(url_for("notifications"))
    rows = db.all(
        f"SELECT id, kind, title, body, created_at, read_at, wa_status, wa_error FROM notifications WHERE {where} ORDER BY id DESC LIMIT 100", params
    )
    return render_template("notifications.html", rows=rows)


@app.post("/settings/alerts")
@need("admin")
def save_alerts():
    db = get_db()
    phone = text("alert_phone")
    if phone and not whatsapp_number(phone):
        flash("Enter the WhatsApp number with its country code, for example +260 972 047 233.", "error")
        return redirect(url_for("settings"))
    db.execute(
        "UPDATE businesses SET alert_phone=?, alert_low_stock=?, alert_permission=? WHERE id=?",
        (phone, 1 if request.form.get("alert_low_stock") else 0, 1 if request.form.get("alert_permission") else 0, g.user["business_id"]),
    )
    db.commit()
    flash("Alert settings saved.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/alerts/test")
@need("admin")
def test_alert():
    db = get_db()
    notification_id = notify(db, "test", "Test alert", "This is a test. If you can read it, alerts are reaching you.", url_for("notifications"))
    db.commit()
    status, detail = send_whatsapp(g.user["alert_phone"], f"{g.user['business_name']}: Test alert. If you can read this, WhatsApp alerts are working.")
    db.execute("UPDATE notifications SET wa_status=?, wa_error=? WHERE id=?", (status, detail, notification_id))
    db.commit()
    if status == "sent":
        flash(f"Test sent to WhatsApp number {g.user['alert_phone']}. A popup will also appear here shortly.", "ok")
    else:
        flash(f"The popup alert works, but the WhatsApp message was not sent: {detail}.", "error")
    return redirect(url_for("settings"))


# --- PERMISSION REQUESTS ---
def active_request(db, item_id, action):
    """The signed-in user's latest request for this action on this item that is still pending or approved and unexpired."""
    row = db.one(
        """SELECT id, status, expires_at, reason, decided_by FROM permission_requests
           WHERE business_id=? AND user_id=? AND item_id=? AND action=? AND status IN ('pending', 'approved') ORDER BY id DESC LIMIT 1""",
        (g.user["business_id"], g.user["id"], item_id, action),
    )
    if row and row["status"] == "approved" and (row["expires_at"] or "") < now().strftime("%Y-%m-%d %H:%M:%S"):
        return None
    return row


def item_grant(db, item_id, action):
    """'own' when the user's account allows the action, the approved request when an admin allowed it once, else None."""
    if can(action):
        return "own"
    row = active_request(db, item_id, action)
    return row if row and row["status"] == "approved" else None


def use_grant(db, grant):
    """An admin's approval covers one use: it is spent when the action goes through."""
    if isinstance(grant, dict):
        db.execute("UPDATE permission_requests SET status='used' WHERE id=?", (grant["id"],))


def item_access_state(db, item_id, action):
    """For the item page: 'own', 'approved', 'pending' or 'none', plus when an approval runs out."""
    if can(action):
        return {"state": "own", "until": ""}
    row = active_request(db, item_id, action)
    return {"state": row["status"], "until": (row["expires_at"] or "")[11:16]} if row else {"state": "none", "until": ""}


def refuse_item_action(item_id):
    flash("Your account does not have permission to do that. Use the request button to ask an admin.", "error")
    return redirect(url_for("item", item_id=item_id))


@app.post("/inventory/<int:item_id>/request-permission")
@need(None)
def request_permission(item_id):
    db, biz = get_db(), g.user["business_id"]
    target = get_item(db, item_id)
    action = text("action")
    back = redirect(url_for("item", item_id=item_id))
    if action not in dict(ITEM_ACTIONS):
        abort(400)
    if can(action):
        flash("Your account already has that permission.", "ok")
        return back
    if active_request(db, item_id, action):
        flash("You already have a request for this waiting or approved.", "ok")
        return back

    reason = text("reason")[:200]
    request_id = db.insert(
        """INSERT INTO permission_requests (business_id, user_id, username, action, item_id, item_name, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (biz, g.user["id"], g.user["username"], action, item_id, target["name"], reason, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if alert_enabled("permission"):
        notify(
            db, "permission", f"Permission request from {g.user['username']}",
            f"{g.user['username']} asks to {dict(ITEM_ACTIONS)[action]}: {target['name']} (code {target['item_code']})."
            + (f" Reason: {reason}." if reason else "") + " Open the app to approve or deny.",
            url_for("permission_requests") + f"#request-{request_id}", whatsapp=True,
        )
    db.commit()
    flash("Your request has been sent to the admin. You will get a popup here when they answer.", "ok")
    return back


@app.get("/requests")
@need("admin")
def permission_requests():
    rows = get_db().all(
        """SELECT id, username, action, item_id, item_name, reason, status, created_at, decided_by, decided_at, expires_at
           FROM permission_requests WHERE business_id=? ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC LIMIT 200""",
        (g.user["business_id"],),
    )
    return render_template("requests.html", rows=rows, actions=REQUEST_LABELS, delete_invoice=DELETE_INVOICE, minutes=GRANT_MINUTES)


@app.post("/requests/<int:request_id>/decide")
@need("admin")
def decide_request(request_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, user_id, action, item_id, item_name, status FROM permission_requests WHERE id=? AND business_id=?", (request_id, biz))
    approve = text("decision") == "approve"
    if row is None or row["status"] != "pending" or text("decision") not in ("approve", "deny"):
        flash("That request has already been answered.", "error")
        return redirect(url_for("permission_requests"))

    stamp = now()
    expires = (stamp + datetime.timedelta(minutes=GRANT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S") if approve else None
    db.execute(
        "UPDATE permission_requests SET status=?, decided_by=?, decided_at=?, expires_at=? WHERE id=?",
        ("approved" if approve else "denied", g.user["username"], stamp.strftime("%Y-%m-%d %H:%M:%S"), expires, request_id),
    )
    what = REQUEST_LABELS[row["action"]]
    if row["action"] == DELETE_INVOICE:
        target = url_for("invoice", invoice_id=row["item_id"])
    elif row["action"] == COLLECT_PREORDER:
        target = url_for("up_next")
    else:
        target = url_for("item", item_id=row["item_id"])
    if approve:
        notify(
            db, "decision", "Request approved",
            f"{g.user['username']} approved your request to {what}: {row['item_name']}. It covers one use and lasts {GRANT_MINUTES} minutes.",
            target, for_user_id=row["user_id"],
        )
    else:
        notify(
            db, "decision", "Request denied",
            f"{g.user['username']} did not approve your request to {what}: {row['item_name']}.",
            target, for_user_id=row["user_id"],
        )
    db.commit()
    flash("Request approved. The operator has been notified." if approve else "Request denied. The operator has been notified.", "ok")
    return redirect(url_for("permission_requests"))


# --- EMPLOYEE STATEMENTS (ADMIN) ---
STATEMENT_PERIODS = (
    ("this_year", "This year"), ("last_6", "Last 6 months"), ("last_12", "Last 12 months"), ("all", "All time"), ("custom", "Custom range"),
)


def shift_month(year, month, by):
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def statement_months():
    """Returns (choice, first month, last month) as 'YYYY-MM' strings; both are None for 'all time'."""
    today = now().date()
    this_month = today.strftime("%Y-%m")
    choice = request.args.get("period", "this_year")
    if choice == "custom":
        try:
            start = datetime.datetime.strptime(request.args.get("from", ""), "%Y-%m").strftime("%Y-%m")
            end = datetime.datetime.strptime(request.args.get("to", ""), "%Y-%m").strftime("%Y-%m")
            return choice, min(start, end), max(start, end)
        except ValueError:
            choice = "this_year"
    if choice == "all":
        return choice, None, None
    if choice in ("last_6", "last_12"):
        year, month = shift_month(today.year, today.month, -(5 if choice == "last_6" else 11))
        return choice, f"{year:04d}-{month:02d}", this_month
    return "this_year", f"{today.year:04d}-01", f"{today.year:04d}-12"


@app.get("/hr/statements")
@need("admin")
def employee_statements():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    sql = """SELECT e.id, e.full_name, e.nrc_number, e.employment_type, e.basic_salary,
                    COUNT(p.id) AS payslips, MIN(p.pay_month) AS first_month, MAX(p.pay_month) AS last_month
             FROM employees e LEFT JOIN payroll p ON p.employee_id = e.id AND p.business_id = e.business_id
             WHERE e.business_id=?"""
    params = [biz]
    if q:
        like = f"%{q.lower()}%"
        sql += " AND (LOWER(e.full_name) LIKE ? OR LOWER(e.nrc_number) LIKE ?)"
        params += [like, like]
    sql += " GROUP BY e.id, e.full_name, e.nrc_number, e.employment_type, e.basic_salary ORDER BY e.full_name LIMIT 100"
    return render_template("employee_statements.html", results=db.all(sql, params), q=q)


@app.get("/hr/employees/<int:employee_id>/statement")
@need("admin")
def employee_statement(employee_id):
    db, biz = get_db(), g.user["business_id"]
    employee = db.one(
        "SELECT id, full_name, nrc_number, employment_type, basic_salary, start_date, expected_days_week FROM employees WHERE id=? AND business_id=?",
        (employee_id, biz),
    )
    if employee is None:
        abort(404)

    choice, start, end = statement_months()
    slips = db.all(
        """SELECT pay_month, expected_days, worked_days, calculated_basic, bonuses_json, deductions_json,
                  napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable
           FROM payroll WHERE employee_id=? AND business_id=? AND pay_month >= ? AND pay_month <= ? ORDER BY pay_month, id""",
        (employee_id, biz, start or "0000-00", end or "9999-99"),
    )

    money_keys = ("basic", "bonuses", "deductions", "napsa", "nhima", "employer_napsa", "employer_nhima", "net")
    blank = dict.fromkeys(("payslips", "expected", "worked", "absent") + money_keys, 0)
    months, totals, named = {}, dict(blank), {"bonus": {}, "deduction": {}}
    for slip in slips:
        bonuses, deductions = json.loads(slip["bonuses_json"] or "{}"), json.loads(slip["deductions_json"] or "{}")
        for kind, items in (("bonus", bonuses), ("deduction", deductions)):
            for name, amount in items.items():
                named[kind][name] = named[kind].get(name, 0.0) + (amount or 0.0)
        figures = {
            "payslips": 1, "expected": slip["expected_days"], "worked": slip["worked_days"],
            # Absence is counted payslip by payslip: the days expected that were not worked
            "absent": max(0.0, slip["expected_days"] - slip["worked_days"]),
            "basic": slip["calculated_basic"], "bonuses": sum(bonuses.values()), "deductions": sum(deductions.values()),
            "napsa": slip["napsa_amt"], "nhima": slip["nhima_amt"], "employer_napsa": slip["employer_napsa"] or 0.0,
            "employer_nhima": slip["employer_nhima"] or 0.0, "net": slip["net_payable"],
        }
        month = months.setdefault(slip["pay_month"], dict(blank, month=slip["pay_month"]))
        for key, value in figures.items():
            month[key] += value or 0
            totals[key] += value or 0

    worked_months = len(months)
    summary = {
        "months": worked_months,
        "average_absent": totals["absent"] / worked_months if worked_months else 0.0,
        "attendance": 100.0 * totals["worked"] / totals["expected"] if totals["expected"] else None,
        "average_net": totals["net"] / worked_months if worked_months else 0.0,
    }
    return render_template(
        "employee_statement.html", employee=employee, months=list(months.values()), totals=totals, summary=summary,
        bonuses=sorted(named["bonus"].items()), deductions=sorted(named["deduction"].items()),
        periods=STATEMENT_PERIODS, period=choice, start=start, end=end, printed=now().strftime("%d %b %Y %H:%M"),
    )


# --- CUSTOMER STATEMENTS (ADMIN) ---
@app.get("/customers")
@need("admin")
def customers():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    sql = """SELECT c.id, c.name, c.phone, COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed
             FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind='SALE' AND i.payment_status <> 'Cancelled'
             WHERE c.business_id=?"""
    params = [biz]
    if q:
        sql += " AND (c.name_key LIKE ?"
        params.append(f"%{' '.join(q.lower().split())}%")
        if phone_key(q):
            sql += " OR c.phone_key LIKE ?"
            params.append(f"%{phone_key(q)}%")
        sql += ")"
    sql += " GROUP BY c.id, c.name, c.phone ORDER BY c.name LIMIT 100"
    return render_template("customers.html", results=db.all(sql, params), q=q, duplicate_count=len(find_duplicates(db, biz)))


def get_customer(db, customer_id):
    customer = db.one("SELECT id, name, phone, created_at FROM customers WHERE id=? AND business_id=?", (customer_id, g.user["business_id"]))
    if customer is None:
        abort(404)
    return customer


def statement_data(db, customer_id):
    """Everything a customer statement shows, shared by the page and the PDF."""
    biz = g.user["business_id"]
    invoices = db.all(
        """SELECT id, invoice_no, created_at, created_by, payment_status, total, amount_paid, balance_due, cancelled_at, cancelled_by, cancel_reason
           FROM invoices WHERE customer_id=? AND business_id=? AND kind='SALE' ORDER BY created_at, id""",
        (customer_id, biz),
    )
    lines = db.all(
        """SELECT ii.invoice_id, ii.item_code, ii.item_name, ii.qty, ii.unit_price, ii.line_total
           FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
           WHERE i.customer_id=? AND i.business_id=? AND i.kind='SALE' ORDER BY ii.id""",
        (customer_id, biz),
    )
    changes = db.all(
        """SELECT pl.ref_id, pl.old_paid, pl.new_paid, pl.note, pl.changed_by, pl.changed_at
           FROM payment_log pl JOIN invoices i ON i.id = pl.ref_id
           WHERE pl.kind='sale' AND pl.business_id=? AND i.customer_id=? ORDER BY pl.id""",
        (biz, customer_id),
    )

    products = {}
    for inv in invoices:
        inv["lines"] = [line for line in lines if line["invoice_id"] == inv["id"]]
        inv["changes"] = [change for change in changes if change["ref_id"] == inv["id"]]
        if inv["payment_status"] == "Cancelled":
            continue  # kept on the statement for the record, but not counted as something they bought
        for line in inv["lines"]:
            p = products.setdefault((line["item_code"], line["item_name"]), {"code": line["item_code"], "name": line["item_name"], "qty": 0, "value": 0.0, "last": ""})
            p["qty"] += line["qty"]
            p["value"] += line["line_total"]
            p["last"] = max(p["last"], inv["created_at"][:10])

    # The statement: every invoice as a charge and every payment as a credit, in date order with a running balance.
    # Whatever was paid before the first logged change was paid at the time of sale. A cancelled invoice still shows
    # as a line, clearly marked, but charges and pays nothing - it no longer affects the running balance.
    entries = []
    for inv in invoices:
        cancelled = inv["payment_status"] == "Cancelled"
        items = ", ".join(f"{line['qty']} x {line['item_name']}" for line in inv["lines"])
        what = f"Invoice {inv['invoice_no']} (cancelled)" if cancelled else f"Invoice {inv['invoice_no']}"
        entries.append({"at": inv["created_at"], "order": 0, "what": what, "detail": items, "charge": 0.0 if cancelled else inv["total"], "paid": 0.0, "invoice_id": inv["id"]})
        if cancelled:
            continue
        at_sale = inv["changes"][0]["old_paid"] if inv["changes"] else inv["amount_paid"]
        if at_sale > 0:
            entries.append({"at": inv["created_at"], "order": 1, "what": "Payment at time of sale", "detail": inv["invoice_no"], "charge": 0.0, "paid": at_sale, "invoice_id": inv["id"]})
        for change in inv["changes"]:
            amount = round(change["new_paid"] - change["old_paid"], 2)
            if amount:
                detail = inv["invoice_no"] + (f" — {change['note']}" if change["note"] else "") + f" (recorded by {change['changed_by']})"
                entries.append({
                    "at": change["changed_at"], "order": 2, "what": "Payment received" if amount > 0 else "Payment correction",
                    "detail": detail, "charge": 0.0, "paid": amount, "invoice_id": inv["id"],
                })
    entries.sort(key=lambda e: (e["at"], e["invoice_id"], e["order"]))
    balance = 0.0
    for entry in entries:
        balance = round(balance + entry["charge"] - entry["paid"], 2)
        entry["balance"] = balance

    active = [inv for inv in invoices if inv["payment_status"] != "Cancelled"]
    summary = {
        "invoices": len(active), "total": sum(inv["total"] for inv in active),
        "paid": sum(inv["amount_paid"] for inv in active), "owed": sum(inv["balance_due"] for inv in active),
    }
    return {
        "invoices": invoices, "entries": entries, "summary": summary,
        "products": sorted(products.values(), key=lambda p: -p["value"]), "printed": now().strftime("%d %b %Y %H:%M"),
    }


@app.get("/customers/<int:customer_id>")
@need("admin")
def customer_statement(customer_id):
    db = get_db()
    return render_template(
        "customer.html", customer=get_customer(db, customer_id), twins=phone_twins(db, g.user["business_id"], customer_id),
        methods=PAYMENT_METHODS, **statement_data(db, customer_id),
    )


@app.get("/customers/<int:customer_id>/statement.pdf")
@need("admin")
def customer_statement_pdf(customer_id):
    db = get_db()
    customer = get_customer(db, customer_id)
    pdf = statement_pdf(customer, statement_data(db, customer_id))
    filename = "statement-" + (re.sub(r"[^A-Za-z0-9]+", "-", customer["name"]).strip("-") or "customer") + ".pdf"
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def statement_pdf(customer, data):
    """Lays the statement out on A4 pages and returns the PDF as bytes."""
    # Imported here so the PDF library only loads when a statement is actually downloaded
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    right = ParagraphStyle("right", parent=small, alignment=2)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")

    def cell(value, style=small):
        return Paragraph(escape(str(value)), style)

    def amount(value, blank_zero=False):
        return "" if blank_zero and not value else money(value)

    def table(rows, widths, numeric_from, bold_last=False):
        """A ruled table whose first row is the header; columns from numeric_from onward are right-aligned."""
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), grey),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
        ]
        if bold_last:
            style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        return Table(rows, colWidths=[w * mm for w in widths], repeatRows=1, style=TableStyle(style))

    summary = data["summary"]
    story = [Paragraph(escape(g.user["business_name"]), title)]
    for extra in (g.user["subtitle"], g.user["contact"]):
        if extra:
            story.append(Paragraph(escape(extra), centre))
    story += [
        Spacer(1, 4), Paragraph("<b>CUSTOMER STATEMENT</b>", centre), Spacer(1, 8),
        Table(
            [
                ["Customer", cell(customer["name"]), "Invoices", str(summary["invoices"])],
                ["Phone", cell(customer["phone"] or "-"), "Total bought", money(summary["total"])],
                ["Customer since", (customer["created_at"] or "")[:10] or "-", "Total paid", money(summary["paid"])],
                ["Statement date", data["printed"], "Balance owed", money(summary["owed"])],
            ],
            colWidths=[28 * mm, 62 * mm, 30 * mm, 60 * mm],
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.grey), ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("FONTNAME", (2, 3), (3, 3), "Helvetica-Bold"),
                ("BOX", (0, 0), (-1, -1), 0.4, line),
            ]),
        ),
        Paragraph("Statement of account", heading),
    ]

    ledger = [["Date / time", "Entry", "Details", "Charged", "Paid", "Balance owed"]]
    for e in data["entries"]:
        ledger.append([e["at"], cell(e["what"]), cell(e["detail"]), amount(e["charge"], True), amount(e["paid"], True), money(e["balance"])])
    if data["entries"]:
        ledger.append(["Totals", "", "", money(summary["total"]), money(summary["paid"]), money(summary["owed"])])
        story.append(table(ledger, (30, 34, 50, 22, 22, 22), 3, bold_last=True))
    else:
        story.append(Paragraph("No purchases yet.", small))

    if data["products"]:
        story.append(Paragraph("What they have bought", heading))
        bought = [["Code", "Product", "Quantity", "Value", "Last bought"]]
        for p in data["products"]:
            bought.append([cell(p["code"] or "-"), cell(p["name"]), str(p["qty"]), money(p["value"]), p["last"]])
        story.append(table(bought, (30, 78, 20, 28, 24), 2))

    if data["invoices"]:
        story.append(Paragraph("Invoices in full", heading))
    for inv in data["invoices"]:
        rows = [["Code", "Item", "Qty", "Unit price", "Total"]]
        for item in inv["lines"]:
            rows.append([cell(item["item_code"] or "-"), cell(item["item_name"]), str(item["qty"]), money(item["unit_price"]), money(item["line_total"])])
        rows += [
            ["", "", "", "Invoice total", money(inv["total"])],
            ["", "", "", "Paid so far", money(inv["amount_paid"])],
            ["", "", "", "Still owed", money(inv["balance_due"])],
        ]
        caption = f"<b>{escape(inv['invoice_no'])}</b> &nbsp; {escape(inv['created_at'])} &nbsp; served by {escape(inv['created_by'] or '-')} &nbsp; <b>{escape(inv['payment_status'] or '')}</b>"
        story.append(KeepTogether([Paragraph(caption, small), Spacer(1, 2), table(rows, (30, 84, 14, 26, 26), 2), Spacer(1, 8)]))

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"{customer['name']} - statement printed {data['printed']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Statement - {customer['name']}", author=g.user["business_name"],
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


@app.post("/customers/<int:customer_id>/payment")
@need("admin")
def customer_payment(customer_id):
    """Spreads one lump-sum payment over the customer's unpaid invoices, oldest first. Accounts gets it as one payment."""
    db, biz = get_db(), g.user["business_id"]
    customer = get_customer(db, customer_id)
    back = redirect(url_for("customer_statement", customer_id=customer_id))
    unpaid = db.all(
        """SELECT id, total, amount_paid, balance_due FROM invoices
           WHERE customer_id=? AND business_id=? AND kind='SALE' AND balance_due > 0 ORDER BY created_at, id""",
        (customer_id, biz),
    )
    owed = round(sum(inv["balance_due"] for inv in unpaid), 2)
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    if amount <= 0 or amount > owed + 0.005:
        flash(f"Enter an amount above zero and no more than the {money(owed)} this customer owes.", "error")
        return back
    method, reference, problem = read_money_method()
    if problem:
        flash(problem, "error")
        return back

    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    note = f"Part of a {money(amount)} lump-sum payment" + (f": {text('note')[:150]}" if text("note") else "")
    remaining, settled = min(amount, owed), 0
    try:
        for inv in unpaid:
            if remaining <= 0.005:
                break
            part = min(remaining, inv["balance_due"])
            new_paid = min(inv["total"], round(inv["amount_paid"] + part, 2))
            db.execute(
                "INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at) VALUES (?, 'sale', ?, ?, ?, ?, ?, ?)",
                (biz, inv["id"], inv["amount_paid"], new_paid, note, g.user["username"], stamp),
            )
            db.execute(
                "UPDATE invoices SET amount_paid=?, balance_due=?, payment_status=? WHERE id=? AND business_id=?",
                (new_paid, round(inv["total"] - new_paid, 2), payment_status_for(inv["total"], new_paid), inv["id"], biz),
            )
            remaining = round(remaining - part, 2)
            settled += 1
        record_money(
            db, biz, "in", min(amount, owed), method, "customer_payment", reference, customer["name"],
            f"Payment on account, spread over {settled} invoice(s)" + (f": {text('note')[:120]}" if text("note") else ""), "customer", customer_id, stamp,
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be saved. Please try again.", "error")
        return back

    flash(f"Recorded {money(amount)} across {settled} invoice(s), oldest first. Balance now owed: {money(owed - min(amount, owed))}.", "ok")
    return back


@app.post("/customers/<int:customer_id>/update")
@need("admin")
def update_customer(customer_id):
    db, biz = get_db(), g.user["business_id"]
    get_customer(db, customer_id)
    name, phone = text("name"), text("phone")
    nkey, pkey = name_key(name), phone_key(phone)
    clash = db.one("SELECT id FROM customers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?", (biz, nkey, pkey, customer_id))
    if not nkey:
        flash("The customer needs a name.", "error")
    elif clash:
        flash("Another customer account already has that name and phone number.", "error")
    else:
        db.execute("UPDATE customers SET name=?, phone=?, name_key=?, phone_key=? WHERE id=? AND business_id=?", (name, phone, nkey, pkey, customer_id, biz))
        db.commit()
        flash("Customer details saved. Invoices already issued keep the name and phone printed on them.", "ok")
    return redirect(url_for("customer_statement", customer_id=customer_id))


DUPLICATE_SCAN_LIMIT = 3000
DUPLICATES_SHOWN = 100
CUSTOMER_COLUMNS = "id, name, phone, name_key, phone_key, created_at"


def names_alike(a, b):
    """True when two name keys look like the same person: reordered, a small typo, or an initial for a full name."""
    words_a, words_b = a.split(), b.split()
    if sorted(words_a) == sorted(words_b) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.86:
        return True
    if len(words_a) != len(words_b) or len(words_a) < 2:
        return False
    whole_matches = 0
    for x, y in zip(words_a, words_b):
        x, y = x.strip("."), y.strip(".")
        if x == y:
            whole_matches += 1
        elif not ((len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))):
            return False
    return whole_matches >= 1


def pair_key(a, b):
    return (min(a, b), max(a, b))


def distinct_pairs(db, biz):
    return {(r["low_id"], r["high_id"]) for r in db.all("SELECT low_id, high_id FROM customer_distinct WHERE business_id=?", (biz,))}


def find_duplicates(db, biz):
    """Pairs of customer accounts that may be the same person, strongest evidence first."""
    people = db.all(
        """SELECT c.id, c.name, c.phone, c.name_key, c.phone_key, c.created_at,
                  COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed
           FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind='SALE' AND i.payment_status <> 'Cancelled'
           WHERE c.business_id=? GROUP BY c.id, c.name, c.phone, c.name_key, c.phone_key, c.created_at ORDER BY c.id LIMIT ?""",
        (biz, DUPLICATE_SCAN_LIMIT),
    )
    return duplicate_pairs(people, distinct_pairs(db, biz))


def duplicate_pairs(people, ruled_out):
    """Pairs of accounts that may be the same party, strongest evidence first. Shared by customers and suppliers.

    people are dicts with id, name_key and phone_key; ruled_out holds pair_key pairs an admin said are different.
    """
    by_id = {p["id"]: p for p in people}
    found = {}

    def suggest(a, b, rank, reason):
        key = pair_key(a, b)
        if a != b and key not in ruled_out and (key not in found or rank < found[key][0]):
            found[key] = (rank, reason)

    same = {}
    for p in people:
        if p["phone_key"]:
            same.setdefault(("phone", p["phone_key"]), []).append(p["id"])
        if p["name_key"]:
            same.setdefault(("name", p["name_key"]), []).append(p["id"])
    for (kind, _), ids in same.items():
        ids = ids[:30]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if kind == "phone":
                    suggest(a, b, 0, "Same phone number")
                else:
                    suggest(a, b, 1, "Same name, different phone numbers")

    # Comparing every name with every other is too slow, so only compare names that share a word or their first letters
    buckets = {}
    for p in people:
        if p["name_key"]:
            for word in set(p["name_key"].split()) | {p["name_key"][:3]}:
                if len(word) >= 2:
                    buckets.setdefault(word, []).append(p["id"])
    for ids in buckets.values():
        if len(ids) > 150:
            continue
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if pair_key(a, b) not in found and names_alike(by_id[a]["name_key"], by_id[b]["name_key"]):
                    both_have_phones = by_id[a]["phone_key"] and by_id[b]["phone_key"]
                    suggest(a, b, 3 if both_have_phones else 2, "Similar name, different phone numbers" if both_have_phones else "Similar name")

    pairs = [{"a": by_id[low], "b": by_id[high], "reason": reason, "rank": rank} for (low, high), (rank, reason) in found.items()]
    pairs.sort(key=lambda pair: (pair["rank"], pair["a"]["name_key"], pair["a"]["id"]))
    return pairs


def phone_twins(db, biz, customer_id):
    """Other accounts carrying this customer's phone number, unless an admin already said they are different people."""
    me = db.one("SELECT phone_key FROM customers WHERE id=? AND business_id=?", (customer_id, biz))
    if not me or not me["phone_key"]:
        return []
    others = db.all(
        "SELECT id, name, phone FROM customers WHERE business_id=? AND phone_key=? AND id<>? ORDER BY id",
        (biz, me["phone_key"], customer_id),
    )
    ruled_out = distinct_pairs(db, biz)
    return [other for other in others if pair_key(customer_id, other["id"]) not in ruled_out]


@app.get("/customers/duplicates")
@need("admin")
def customer_duplicates():
    pairs = find_duplicates(get_db(), g.user["business_id"])
    return render_template("duplicates.html", pairs=pairs[:DUPLICATES_SHOWN], total=len(pairs))


def customer_pair_from_form(db, biz, first, second):
    a = db.one(f"SELECT {CUSTOMER_COLUMNS} FROM customers WHERE id=? AND business_id=?", (request.form.get(first, type=int), biz))
    b = db.one(f"SELECT {CUSTOMER_COLUMNS} FROM customers WHERE id=? AND business_id=?", (request.form.get(second, type=int), biz))
    return (a, b) if a and b and a["id"] != b["id"] else (None, None)


def back_from_duplicates():
    back_to = request.form.get("back_to", type=int)
    return redirect(url_for("customer_statement", customer_id=back_to) if back_to else url_for("customer_duplicates"))


@app.post("/customers/merge")
@need("admin")
def merge_customers():
    db, biz = get_db(), g.user["business_id"]
    keep, remove = customer_pair_from_form(db, biz, "keep", "remove")
    if keep is None:
        flash("Those two accounts could not be found. One may already have been merged.", "error")
        return redirect(url_for("customer_duplicates"))

    try:
        moved = db.execute("UPDATE invoices SET customer_id=? WHERE customer_id=? AND business_id=?", (keep["id"], remove["id"], biz)).rowcount
        # The removed account's spelling, and any spellings merged into it earlier, now lead to the kept account
        db.execute("UPDATE customer_aliases SET customer_id=? WHERE customer_id=? AND business_id=?", (keep["id"], remove["id"], biz))
        db.execute("DELETE FROM customer_aliases WHERE business_id=? AND name_key=? AND phone_key=?", (biz, remove["name_key"], remove["phone_key"]))
        db.execute(
            "INSERT INTO customer_aliases (business_id, name_key, phone_key, customer_id) VALUES (?, ?, ?, ?)",
            (biz, remove["name_key"], remove["phone_key"], keep["id"]),
        )
        db.execute("DELETE FROM customer_distinct WHERE business_id=? AND (low_id=? OR high_id=?)", (biz, remove["id"], remove["id"]))
        db.execute("DELETE FROM customers WHERE id=? AND business_id=?", (remove["id"], biz))

        since = min(keep["created_at"] or "9999", remove["created_at"] or "9999")
        phone, pkey = keep["phone"], keep["phone_key"]
        if not pkey and remove["phone_key"]:
            taken = db.one(
                "SELECT id FROM customers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?",
                (biz, keep["name_key"], remove["phone_key"], keep["id"]),
            )
            if not taken:
                phone, pkey = remove["phone"], remove["phone_key"]
        db.execute(
            "UPDATE customers SET phone=?, phone_key=?, created_at=? WHERE id=? AND business_id=?",
            (phone, pkey, None if since == "9999" else since, keep["id"], biz),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be saved, so nothing was changed. Please try again.", "error")
        return redirect(url_for("customer_duplicates"))

    flash(f"Merged '{remove['name']}' into '{keep['name']}': {moved} invoice(s) moved onto this statement.", "ok")
    return redirect(url_for("customer_statement", customer_id=keep["id"]))


@app.post("/customers/distinct")
@need("admin")
def mark_customers_distinct():
    db, biz = get_db(), g.user["business_id"]
    a, b = customer_pair_from_form(db, biz, "a", "b")
    if a is not None:
        low, high = pair_key(a["id"], b["id"])
        if not db.one("SELECT id FROM customer_distinct WHERE business_id=? AND low_id=? AND high_id=?", (biz, low, high)):
            db.execute("INSERT INTO customer_distinct (business_id, low_id, high_id) VALUES (?, ?, ?)", (biz, low, high))
            db.commit()
        flash(f"Noted: '{a['name']}' and '{b['name']}' are different customers. They will not be suggested again.", "ok")
    return back_from_duplicates()


def link_existing_customers():
    """Opens accounts for named customers on invoices issued before customer accounts existed."""
    db = Database()
    try:
        unlinked = db.all(
            """SELECT id, business_id, customer_name, customer_phone, created_at FROM invoices
               WHERE customer_id IS NULL AND kind='SALE' AND NOT (customer_name = ? AND (customer_phone IS NULL OR customer_phone = ''))
               ORDER BY id""",
            (WALK_IN,),
        )
        for inv in unlinked:
            customer_id = customer_for(db, inv["business_id"], inv["customer_name"], inv["customer_phone"], inv["created_at"])
            if customer_id:
                db.execute("UPDATE invoices SET customer_id=? WHERE id=?", (customer_id, inv["id"]))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()


link_existing_customers()


# --- SUPPLIER STATEMENTS (ADMIN) ---
# A supplier account is opened from the supplier named on a purchase, the way a customer account is opened from a sale. Every
# payment to a supplier is kept with how it was paid and the receipt or reference number. A payment entered on a statement is
# spread over the supplier's unpaid purchases, oldest first.
# The ways money is paid, offered wherever a payment is entered: each one lands in one of the balances kept in Accounts (cash at
# hand, the bank, the Airtel Money and MTN Money wallets), except Other.
PAYMENT_METHODS = (
    ("Cash", "Receipt number"), ("Bank transfer", "Transaction reference"), ("Airtel Money", "Transaction ID"), ("MTN Money", "Transaction ID"),
    ("Cheque", "Cheque number"), ("Card", "Card slip number"), ("Other", "Reference number"),
)
# Before the two wallets were told apart, mobile money was one method. Records that say so stay valid, but it is no longer offered.
LEGACY_MOBILE = "Mobile money"
METHOD_LABELS = dict(PAYMENT_METHODS + ((LEGACY_MOBILE, "Transaction ID"),))
METHOD_NAMES = [method for method, _ in PAYMENT_METHODS]
NOT_RECORDED = "Not recorded"
METHOD_ALIASES = {
    "bank": "Bank transfer", "transfer": "Bank transfer", "eft": "Bank transfer", "bank deposit": "Bank transfer",
    "airtel": "Airtel Money", "airtel mobile money": "Airtel Money", "mtn": "MTN Money", "momo": "MTN Money", "mtn momo": "MTN Money",
    "mtn mobile money": "MTN Money", "mobile": LEGACY_MOBILE, "check": "Cheque", "credit card": "Card", "debit card": "Card",
}


def method_from_text(raw):
    """A payment method written in a file or form, matched to one of the known ones; None when it is not recognised."""
    key = " ".join((raw or "").lower().split())
    for method in METHOD_LABELS:
        if method.lower() == key:
            return method
    return METHOD_ALIASES.get(key)


def supplier_key(name):
    return " ".join((name or "").lower().split())


def supplier_for(db, biz, name, phone, when):
    """Finds or opens the supplier account for a purchase. Returns None when no supplier is named."""
    name, phone = (name or "").strip(), (phone or "").strip()
    nkey, pkey = supplier_key(name), phone_key(phone)
    if not nkey:
        return None
    exact = db.one("SELECT id FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=?", (biz, nkey, pkey))
    if exact:
        return exact["id"]
    merged = db.one(
        """SELECT s.id FROM supplier_aliases a JOIN suppliers s ON s.id = a.supplier_id
           WHERE a.business_id=? AND a.name_key=? AND a.phone_key=?""",
        (biz, nkey, pkey),
    )
    if merged:
        return merged["id"]
    same = db.all("SELECT id, phone_key FROM suppliers WHERE business_id=? AND name_key=? LIMIT 2", (biz, nkey))
    if len(same) == 1:
        # No phone number given: the one account with this name. A phone number given for an account that had none is added to it.
        if not pkey:
            return same[0]["id"]
        if not same[0]["phone_key"]:
            db.execute("UPDATE suppliers SET phone=?, phone_key=? WHERE id=?", (phone, pkey, same[0]["id"]))
            return same[0]["id"]
    return db.insert(
        "INSERT INTO suppliers (business_id, name, phone, name_key, phone_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (biz, name, phone, nkey, pkey, when),
    )


def read_payment_details(strict):
    """Reads the payment method and its reference number from the form. Returns (method, reference, None) or (None, None, what is wrong).

    A reference is needed with every method; for cash the receipt number is enough. A form that sends no method at all
    (an older client) is accepted as 'Not recorded' unless strict.
    """
    if "payment_method" not in request.form and not strict:
        return NOT_RECORDED, "", None
    method = method_from_text(text("payment_method"))
    if method is None:
        return None, None, "Choose how it was paid: " + ", ".join(METHOD_NAMES) + "."
    reference = " ".join(text("payment_reference").split())[:60]
    if not reference:
        label = METHOD_LABELS[method]
        return None, None, f"Enter the {label.lower()} for this {method.lower()} payment." + (" For cash the receipt number is enough." if method == "Cash" else "")
    return method, reference, None


def add_supplier_payment(db, biz, supplier_id, amount, method, reference, paid_on, note, allocations, apply=True):
    """Records a payment to a supplier and the purchases it settled. The caller commits.

    allocations is a list of (procurement id, amount). With apply, each purchase's amount paid is raised by its share and the
    change is written to its payment history; without it (a purchase logged already paid) only the record is made.
    A negative amount is a correction.
    """
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    payment_id = db.insert(
        """INSERT INTO supplier_payments (business_id, supplier_id, amount, method, reference, paid_on, note, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (biz, supplier_id, round(amount, 2), method, reference, paid_on, note, g.user["username"], stamp),
    )
    how = method + (f", {METHOD_LABELS[method].lower()} {reference}" if reference and method in METHOD_LABELS else "")
    for procurement_id, part in allocations:
        if apply:
            row = db.one("SELECT COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE id=? AND business_id=?", (procurement_id, biz))
            new_paid = round(row["paid"] + part, 2)
            db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, procurement_id, biz))
            db.execute(
                """INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at, payment_id)
                   VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, ?)""",
                (biz, procurement_id, row["paid"], new_paid, (how + (f": {note}" if note else ""))[:200], g.user["username"], stamp, payment_id),
            )
        db.execute(
            "INSERT INTO supplier_payment_allocations (business_id, payment_id, procurement_id, amount) VALUES (?, ?, ?, ?)",
            (biz, payment_id, procurement_id, round(part, 2)),
        )
    supplier = db.one("SELECT name FROM suppliers WHERE id=? AND business_id=?", (supplier_id, biz))
    record_money(
        db, biz, "out", amount, method, "supplier", reference, supplier["name"] if supplier else "", note or "Payment to supplier",
        "supplier_payment", payment_id, stamp_for_day(paid_on),
    )
    return payment_id


# --- ACCOUNTS: THE CASHBOOK ---
# Every movement of money is written to the cashbook when it happens, by the page it happens on, in the same transaction:
#   in:  a sale paid at the till or when a pre-order is collected, a later payment on an invoice or on a customer's account,
#        and other income entered on the Accounts page;
#   out: payments to suppliers (when a purchase is logged, on its payment page, or on the supplier's statement), purchases
#        with no supplier account, salaries and NAPSA/NHIMA from payslips, refunds on cancelled invoices, and other expenses
#        entered on the Accounts page.
# A payment lowered later is a negative entry in the same direction with the method "Correction", so nothing is rewritten.
# The Accounts page reads from here alone.
CORRECTION = "Correction"
MONEY_IN = (
    ("sale", "Sales paid at the till"),
    ("customer_payment", "Payments on credit sales"),
    ("other_income", "Other income"),
    ("capital", "Owner's money put in"),
    ("loan_in", "Loans received"),
)
MONEY_OUT = (
    ("supplier", "Payments to suppliers"),
    ("purchase", "Purchases (no supplier account)"),
    ("salaries", "Salaries (net pay)"),
    ("statutory", "NAPSA & NHIMA"),
    ("refund", "Refunds to customers"),
    ("rent", "Rent"),
    ("utilities", "Electricity, water & internet"),
    ("transport", "Transport & fuel"),
    ("repairs", "Repairs & maintenance"),
    ("charges", "Bank & mobile money charges"),
    ("tax", "Taxes, levies & licences"),
    ("loan_out", "Loan repayments"),
    ("drawings", "Owner's drawings"),
    ("other_expense", "Other expenses"),
)
# Entered by hand on the Accounts page. The other categories are written only by the pages they belong to.
MANUAL_IN = ("other_income", "capital", "loan_in")
MANUAL_OUT = ("rent", "utilities", "transport", "repairs", "charges", "tax", "loan_out", "drawings", "other_expense")
CATEGORY_LABELS = dict(MONEY_IN + MONEY_OUT)
# Where money is held: cash, the bank (cheques and card slips are paid through it) or mobile money
CHANNELS = (
    ("Cash", ("Cash",)), ("Bank", ("Bank transfer", "Cheque", "Card")), ("Airtel Money", ("Airtel Money",)), ("MTN Money", ("MTN Money",)),
    ("Mobile money (wallet not said)", (LEGACY_MOBILE,)), ("Other", ("Other",)), (NOT_RECORDED, (NOT_RECORDED,)), ("Corrections", (CORRECTION,)),
)
CHANNEL_OF = {method: channel for channel, methods in CHANNELS for method in methods}
# The balances kept: (channel, what it is called). Money through Other, or that does not say where it went, is in none of them.
BALANCE_ACCOUNTS = (("Cash", "Cash at hand"), ("Bank", "Bank"), ("Airtel Money", "Airtel Money"), ("MTN Money", "MTN Money"))
BALANCE_FIELDS = {"Cash": "balance_cash", "Bank": "balance_bank", "Airtel Money": "balance_airtel", "MTN Money": "balance_mtn"}
BALANCE_METHODS = [method for channel, _ in BALANCE_ACCOUNTS for method in dict(CHANNELS)[channel]]
# Entries that do not say which balance they belong to, and can be assigned to one in Accounts
UNASSIGNED_METHODS = (NOT_RECORDED, LEGACY_MOBILE, CORRECTION)


def stamp_for_day(day):
    """A full timestamp for money dated by day only: now when it is today (so it sorts after today's earlier entries), else that midnight."""
    if not day or day[:10] == today_str():
        return now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{day[:10]} 00:00:00"


def record_money(db, biz, direction, amount, method, category, reference="", party="", description="", source="", source_id=None, when=None, by=None):
    """Writes one movement of money to the cashbook. The caller commits, so the entry stands or falls with what it records.

    amount is what moved; a negative amount corrects an earlier entry in the same direction. Nothing is written for zero.
    """
    amount = round(amount or 0.0, 2)
    if abs(amount) < 0.005:
        return None
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    return db.insert(
        """INSERT INTO cashbook (business_id, direction, amount, method, reference, category, party, description, source, source_id, occurred_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, direction, amount, method or NOT_RECORDED, (reference or "")[:60], category, (party or "")[:100], (description or "")[:200],
            source, source_id, when or stamp, by or g.user["username"], stamp,
        ),
    )


def read_money_method(required=True):
    """How money moved, from a payment form: (method, reference, problem). The reference (receipt number, transaction ID) is optional.

    A form without the method field at all (an older page, or nothing being paid) gives 'Not recorded'. A method field sent
    blank is a problem to show, unless not required (then it is 'Not recorded' too); something unknown always is.
    """
    if "payment_method" not in request.form or (not required and not text("payment_method")):
        return NOT_RECORDED, "", None
    method = method_from_text(text("payment_method"))
    if method is None:
        return None, None, "Choose how the money was paid: " + ", ".join(METHOD_NAMES) + "."
    return method, " ".join(text("payment_reference").split())[:60], None


def read_correction_method():
    """Which balance a lowered payment is taken back from: (method, reference, problem). An older page without the field gives
    'Correction', which belongs to no balance until it is assigned in Accounts."""
    if "payment_method" not in request.form:
        return CORRECTION, "", None
    method, reference, problem = read_money_method()
    if problem:
        return None, None, "Choose the account the money is taken back from: " + ", ".join(METHOD_NAMES) + "."
    return method, reference, None


def money_lines(db, biz, source, source_id):
    """The cashbook entries belonging to one invoice, purchase or payslip, oldest first."""
    return db.all(
        """SELECT direction, amount, method, reference, category, occurred_at FROM cashbook
           WHERE business_id=? AND source=? AND source_id=? ORDER BY occurred_at, id""",
        (biz, source, source_id),
    )


def record_payslip_money(db, biz, slip_id, employee, month, net, statutory, method, when, by=None):
    """A payslip's pay leaves the business as two entries: net pay to the employee, and NAPSA/NHIMA (both shares) to the authorities."""
    record_money(db, biz, "out", net, method, "salaries", "", employee, f"Pay for {month}", "payslip", slip_id, when, by)
    record_money(db, biz, "out", statutory, method, "statutory", "", employee, f"NAPSA & NHIMA for {month}", "payslip", slip_id, when, by)


@app.get("/suppliers")
@need("admin")
def suppliers():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    owing = request.args.get("balance", "") if request.args.get("balance") in ("owing", "settled") else ""
    sql = """SELECT s.id, s.name, s.phone, COUNT(pr.id) AS purchases, COALESCE(SUM(pr.total_cost), 0) AS total,
                    COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) AS owed
             FROM suppliers s LEFT JOIN procurements pr ON pr.supplier_id = s.id
             WHERE s.business_id=?"""
    params = [biz]
    if q:
        sql += " AND (s.name_key LIKE ?"
        params.append(f"%{supplier_key(q)}%")
        if phone_key(q):
            sql += " OR s.phone_key LIKE ?"
            params.append(f"%{phone_key(q)}%")
        sql += ")"
    sql += " GROUP BY s.id, s.name, s.phone"
    if owing == "owing":
        sql += " HAVING COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) > 0.005"
    elif owing == "settled":
        sql += " HAVING COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) <= 0.005"
    sql += " ORDER BY s.name LIMIT 100"
    rows = db.all(sql, params)
    totals = db.one(
        "SELECT COALESCE(SUM(total_cost - COALESCE(amount_paid, total_cost)), 0) AS owed FROM procurements WHERE business_id=? AND supplier_id IS NOT NULL", (biz,)
    )
    return render_template(
        "suppliers.html", results=rows, q=q, balance=owing, duplicate_count=len(find_supplier_duplicates(db, biz)), owed_all=totals["owed"],
    )


def get_supplier(db, supplier_id):
    supplier = db.one("SELECT id, name, phone, created_at FROM suppliers WHERE id=? AND business_id=?", (supplier_id, g.user["business_id"]))
    if supplier is None:
        abort(404)
    return supplier


def supplier_statement_data(db, supplier_id, start="", end=""):
    """Everything a supplier statement shows, shared by the page and the PDF. start and end (YYYY-MM-DD, either may be blank) limit what is listed."""
    biz = g.user["business_id"]
    purchases = db.all(
        """SELECT id, purchase_date, invoice_no, item_code, name, quantity, unit_cost, total_cost, COALESCE(amount_paid, total_cost) AS amount_paid,
                  COALESCE(in_inventory, 1) AS in_inventory, category, department
           FROM procurements WHERE supplier_id=? AND business_id=? ORDER BY purchase_date, id""",
        (supplier_id, biz),
    )
    payments = db.all(
        """SELECT id, amount, method, reference, paid_on, note, recorded_by, recorded_at FROM supplier_payments
           WHERE supplier_id=? AND business_id=? ORDER BY paid_on, id""",
        (supplier_id, biz),
    )
    allocations = db.all(
        """SELECT a.payment_id, a.procurement_id, a.amount FROM supplier_payment_allocations a
           JOIN supplier_payments sp ON sp.id = a.payment_id WHERE sp.supplier_id=? AND a.business_id=?""",
        (supplier_id, biz),
    )
    recorded, covered = {}, {}
    for a in allocations:
        recorded[a["procurement_id"]] = recorded.get(a["procurement_id"], 0.0) + a["amount"]
        covered.setdefault(a["payment_id"], []).append(a)
    by_id = {p["id"]: p for p in purchases}
    for p in purchases:
        p["balance"] = round(p["total_cost"] - p["amount_paid"], 2)
        p["status"] = payment_status_for(p["total_cost"], p["amount_paid"])
        p["label"] = p["invoice_no"] or p["name"]

    # The statement: every purchase as a charge and every payment as a credit, in date order with a running balance. Whatever was
    # paid on a purchase before payments were recorded individually shows as one payment with no method.
    entries = []
    for p in purchases:
        what = f"{p['quantity']} x {p['name']}" + (f" (invoice {p['invoice_no']})" if p["invoice_no"] else "")
        entries.append({"at": p["purchase_date"] or "", "order": 0, "id": p["id"], "what": "Purchase", "detail": what, "charge": p["total_cost"], "paid": 0.0, "method": "", "reference": ""})
        loose = round(p["amount_paid"] - recorded.get(p["id"], 0.0), 2)
        if abs(loose) > 0.005:
            entries.append({
                "at": p["purchase_date"] or "", "order": 1, "id": p["id"], "what": "Payment" if loose > 0 else "Payment correction", "charge": 0.0, "paid": loose,
                "detail": f"{p['label']}: paid before payments were recorded one by one", "method": NOT_RECORDED, "reference": "",
            })
    pay_rows = []
    for sp in payments:
        covers = ", ".join(
            f"{by_id[a['procurement_id']]['label'] if a['procurement_id'] in by_id else 'a purchase'} ({money(a['amount'])})" for a in covered.get(sp["id"], [])
        )
        pay_rows.append(dict(sp, covers=covers))
        reference = (METHOD_LABELS.get(sp["method"], "Reference") + " " + sp["reference"]) if sp["reference"] else ""
        detail = " · ".join(part for part in (sp["method"], reference, covers, sp["note"]) if part)
        entries.append({
            "at": sp["paid_on"] or "", "order": 1, "id": sp["id"], "what": "Payment" if sp["amount"] > 0 else "Payment correction", "detail": detail,
            "charge": 0.0, "paid": sp["amount"], "method": sp["method"], "reference": sp["reference"],
        })
    entries.sort(key=lambda e: (e["at"], e["order"], e["id"]))

    balance, opening, shown = 0.0, 0.0, []
    for e in entries:
        balance = round(balance + e["charge"] - e["paid"], 2)
        e["balance"] = balance
        if start and e["at"] < start:
            opening = balance
        elif not end or e["at"] <= end:
            shown.append(e)
    by_method = {}
    for e in shown:
        if e["paid"]:
            m = by_method.setdefault(e["method"], {"method": e["method"], "count": 0, "amount": 0.0})
            m["count"] += 1
            m["amount"] = round(m["amount"] + e["paid"], 2)
    in_period = [p for p in pay_rows if (not start or (p["paid_on"] or "") >= start) and (not end or (p["paid_on"] or "") <= end)]

    summary = {
        "purchases": len(purchases), "total": round(sum(p["total_cost"] for p in purchases), 2),
        "paid": round(sum(p["amount_paid"] for p in purchases), 2), "owed": round(sum(p["balance"] for p in purchases), 2),
    }
    return {
        "purchases": purchases, "entries": shown, "payments": sorted(in_period, key=lambda p: (p["paid_on"] or "", p["id"]), reverse=True),
        "by_method": sorted(by_method.values(), key=lambda m: -m["amount"]), "summary": summary, "printed": now().strftime("%d %b %Y %H:%M"),
        "period": {
            "start": start, "end": end, "on": bool(start or end), "opening": opening,
            "charged": round(sum(e["charge"] for e in shown), 2), "paid": round(sum(e["paid"] for e in shown), 2),
        },
    }


def statement_period():
    """The from/to dates of a statement filter; a value that is not a date is ignored."""
    start = day_or_none(request.args.get("from", "")) if request.args.get("from") else None
    end = day_or_none(request.args.get("to", "")) if request.args.get("to") else None
    if start and end and start > end:
        start, end = end, start
    return start or "", end or ""


@app.get("/suppliers/<int:supplier_id>")
@need("admin")
def supplier_statement(supplier_id):
    db, biz = get_db(), g.user["business_id"]
    start, end = statement_period()
    data = supplier_statement_data(db, supplier_id, start, end)
    # What a new payment would settle, oldest first, for the preview in the Record Payment overlay
    unpaid = [
        {"label": f"{p['purchase_date'] or 'No date'} · " + (f"{p['invoice_no']} · {p['name']}" if p["invoice_no"] else p["name"]), "owed": p["balance"]}
        for p in data["purchases"] if p["balance"] > 0.005
    ]
    return render_template(
        "supplier.html", supplier=get_supplier(db, supplier_id), twins=supplier_twins(db, biz, supplier_id), methods=PAYMENT_METHODS, methods_labels=METHOD_LABELS,
        today=today_str(), start=start, end=end, unpaid=unpaid, **data,
    )


@app.get("/suppliers/<int:supplier_id>/statement.pdf")
@need("admin")
def supplier_statement_pdf(supplier_id):
    db = get_db()
    supplier = get_supplier(db, supplier_id)
    start, end = statement_period()
    pdf = supplier_pdf(supplier, supplier_statement_data(db, supplier_id, start, end))
    filename = "supplier-statement-" + (re.sub(r"[^A-Za-z0-9]+", "-", supplier["name"]).strip("-") or "supplier") + ".pdf"
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def supplier_pdf(supplier, data):
    """Lays the supplier statement out on A4 pages and returns the PDF as bytes."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")

    def cell(value):
        return Paragraph(escape(str(value)), small)

    def amount(value, blank_zero=False):
        return "" if blank_zero and not value else money(value)

    def table(rows, widths, numeric_from, bold_last=False):
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), grey),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
        ]
        if bold_last:
            style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        return Table(rows, colWidths=[w * mm for w in widths], repeatRows=1, style=TableStyle(style))

    summary, period = data["summary"], data["period"]
    story = [Paragraph(escape(g.user["business_name"]), title)]
    for extra in (g.user["subtitle"], g.user["contact"]):
        if extra:
            story.append(Paragraph(escape(extra), centre))
    covering = f"{period['start'] or 'the start'} to {period['end'] or 'today'}" if period["on"] else "all dates"
    story += [
        Spacer(1, 4), Paragraph("<b>SUPPLIER STATEMENT</b>", centre), Spacer(1, 8),
        Table(
            [
                ["Supplier", cell(supplier["name"]), "Purchases", str(summary["purchases"])],
                ["Phone", cell(supplier["phone"] or "-"), "Total bought", money(summary["total"])],
                ["Covering", cell(covering), "Total paid", money(summary["paid"])],
                ["Statement date", data["printed"], "Balance owed", money(summary["owed"])],
            ],
            colWidths=[28 * mm, 62 * mm, 30 * mm, 60 * mm],
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.grey), ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("FONTNAME", (2, 3), (3, 3), "Helvetica-Bold"), ("BOX", (0, 0), (-1, -1), 0.4, line),
            ]),
        ),
        Paragraph("Statement of account", heading),
    ]
    ledger = [["Date", "Entry", "Details", "Charged", "Paid", "Balance owed"]]
    if period["on"]:
        ledger.append(["", cell("Balance brought forward"), "", "", "", money(period["opening"])])
    for e in data["entries"]:
        ledger.append([e["at"], cell(e["what"]), cell(e["detail"]), amount(e["charge"], True), amount(e["paid"], True), money(e["balance"])])
    if data["entries"]:
        ledger.append(["Totals", "", "", money(period["charged"]), money(period["paid"]), money(summary["owed"])])
        story.append(table(ledger, (22, 26, 70, 22, 22, 22), 3, bold_last=True))
    else:
        story.append(Paragraph("No purchases or payments in this period.", small))

    if data["payments"]:
        story.append(Paragraph("Payments made", heading))
        rows = [["Date", "Method", "Reference", "Amount", "Recorded by", "Covers"]]
        for p in data["payments"]:
            rows.append([p["paid_on"] or "", cell(p["method"]), cell(p["reference"] or "-"), money(p["amount"]), cell(p["recorded_by"] or "-"), cell(p["covers"] or "-")])
        story.append(table(rows, (22, 26, 30, 22, 24, 56), 3))
    if data["by_method"]:
        story.append(Paragraph("Payments by method", heading))
        rows = [["Method", "Payments", "Amount"]] + [[cell(m["method"]), str(m["count"]), money(m["amount"])] for m in data["by_method"]]
        story.append(table(rows, (60, 30, 40), 1))

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"{supplier['name']} - statement printed {data['printed']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Supplier statement - {supplier['name']}", author=g.user["business_name"],
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


@app.post("/suppliers/<int:supplier_id>/payment")
@need("admin")
def supplier_payment(supplier_id):
    """Spreads one payment to the supplier over their unpaid purchases, oldest first, and records how it was paid."""
    db, biz = get_db(), g.user["business_id"]
    get_supplier(db, supplier_id)
    back = redirect(url_for("supplier_statement", supplier_id=supplier_id))
    unpaid = [
        u for u in db.all(
            """SELECT id, total_cost, COALESCE(amount_paid, total_cost) AS paid FROM procurements
               WHERE supplier_id=? AND business_id=? ORDER BY purchase_date, id""",
            (supplier_id, biz),
        ) if u["total_cost"] - u["paid"] > 0.005
    ]
    owed = round(sum(u["total_cost"] - u["paid"] for u in unpaid), 2)
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    if amount <= 0 or amount > owed + 0.005:
        flash(f"Enter an amount above zero and no more than the {money(owed)} owed to this supplier.", "error")
        return back
    method, reference, problem = read_payment_details(strict=True)
    if problem:
        flash(problem, "error")
        return back
    paid_on = day_or_none(text("paid_on")) if text("paid_on") else today_str()
    if paid_on is None:
        flash("Enter the date the payment was made.", "error")
        return back

    if paid_on > today_str():
        flash("The payment date cannot be in the future.", "error")
        return back

    allocations = spread_over_unpaid(db, biz, supplier_id, min(amount, owed))
    try:
        add_supplier_payment(db, biz, supplier_id, amount, method, reference, paid_on, text("note")[:150], allocations)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be saved. Please try again.", "error")
        return back
    flash(f"Recorded {money(amount)} paid by {method.lower()} (reference {reference}) across {len(allocations)} purchase(s), oldest first. Balance now owed: {money(owed - min(amount, owed))}.", "ok")
    return back


def spread_over_unpaid(db, biz, supplier_id, amount):
    """How an amount paid to a supplier is shared over their unpaid purchases, oldest first: [(purchase id, part)]."""
    unpaid = db.all(
        "SELECT id, total_cost, COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE supplier_id=? AND business_id=? ORDER BY purchase_date, id",
        (supplier_id, biz),
    )
    allocations, remaining = [], round(amount, 2)
    for u in unpaid:
        if remaining <= 0.005:
            break
        owed = round(u["total_cost"] - u["paid"], 2)
        if owed > 0.005:
            part = round(min(remaining, owed), 2)
            allocations.append((u["id"], part))
            remaining = round(remaining - part, 2)
    return allocations


def supplier_owed(db, biz, supplier_id):
    rows = db.all("SELECT total_cost - COALESCE(amount_paid, total_cost) AS owed FROM procurements WHERE supplier_id=? AND business_id=?", (supplier_id, biz))
    return round(sum(r["owed"] for r in rows if r["owed"] > 0.005), 2)


def payment_how(method, reference):
    return method + (f", {METHOD_LABELS[method].lower()} {reference}" if reference and method in METHOD_LABELS else "")


def move_payment_share(db, biz, payment_id, procurement_id, part, note, stamp):
    """Changes what one purchase has had paid by one supplier payment (a negative part takes some back), with its payment history.
    Never takes a purchase below nothing paid. Returns the part actually moved. The caller commits."""
    row = db.one("SELECT COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE id=? AND business_id=?", (procurement_id, biz))
    if row is None:
        return 0.0
    part = round(max(part, -row["paid"]), 2)
    new_paid = round(row["paid"] + part, 2)
    db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, procurement_id, biz))
    db.execute(
        """INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at, payment_id)
           VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, ?)""",
        (biz, procurement_id, row["paid"], new_paid, note[:200], g.user["username"], stamp, payment_id),
    )
    return part


def take_back(db, biz, payment_id, amount, note, stamp):
    """Takes an amount off what a payment paid, from the newest purchase it settled first (the reverse of how it was spread)."""
    remaining = round(amount, 2)
    shares = db.all(
        """SELECT a.id, a.procurement_id, a.amount FROM supplier_payment_allocations a JOIN procurements pr ON pr.id = a.procurement_id
           WHERE a.payment_id=? AND a.business_id=? AND a.amount > 0 ORDER BY pr.purchase_date DESC, pr.id DESC, a.id DESC""",
        (payment_id, biz),
    )
    for share in shares:
        if remaining <= 0.005:
            break
        taken = -move_payment_share(db, biz, payment_id, share["procurement_id"], -min(remaining, share["amount"]), note, stamp)
        left = round(share["amount"] - taken, 2)
        if left <= 0.005:
            db.execute("DELETE FROM supplier_payment_allocations WHERE id=?", (share["id"],))
        else:
            db.execute("UPDATE supplier_payment_allocations SET amount=? WHERE id=?", (left, share["id"]))
        remaining = round(remaining - taken, 2)


def get_supplier_payment(db, biz, supplier_id, payment_id):
    pay = db.one(
        "SELECT id, amount, method, reference, paid_on, note FROM supplier_payments WHERE id=? AND supplier_id=? AND business_id=?",
        (payment_id, supplier_id, biz),
    )
    if pay is None:
        abort(404)
    return pay


@app.post("/suppliers/<int:supplier_id>/payments/<int:payment_id>/update")
@need("admin")
def update_supplier_payment(supplier_id, payment_id):
    """Raises or lowers a recorded payment, or corrects its date, method or reference. More paid is spread over the unpaid purchases
    oldest first; less is taken back from the newest purchase it paid. Its entry in Accounts follows."""
    db, biz = get_db(), g.user["business_id"]
    supplier = get_supplier(db, supplier_id)
    back = redirect(url_for("supplier_statement", supplier_id=supplier_id))
    pay = get_supplier_payment(db, biz, supplier_id, payment_id)
    if pay["amount"] <= 0.005:
        flash("A payment correction cannot be edited. Edit the payment it corrected, or record a new payment.", "error")
        return back
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    if amount <= 0:
        flash("Enter the amount paid, above zero. To take the whole payment off the statement, use Remove payment.", "error")
        return back
    method, reference, problem = read_payment_details(strict=True)
    if problem:
        flash(problem, "error")
        return back
    paid_on = day_or_none(text("paid_on")) if text("paid_on") else None
    if paid_on is None or paid_on > today_str():
        flash("Enter the date the payment was made (today or earlier).", "error")
        return back
    change = round(amount - pay["amount"], 2)
    owed = supplier_owed(db, biz, supplier_id)
    if change > owed + 0.005:
        flash(f"That adds {money(change)} to the payment, but only {money(owed)} is still owed to {supplier['name']}. Nothing was changed.", "error")
        return back

    note = " ".join(text("note").split())[:150]
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if change > 0.005:
            for procurement_id, part in spread_over_unpaid(db, biz, supplier_id, change):
                move_payment_share(db, biz, payment_id, procurement_id, part, f"{payment_how(method, reference)}: payment raised by {money(change)}", stamp)
                db.execute(
                    "INSERT INTO supplier_payment_allocations (business_id, payment_id, procurement_id, amount) VALUES (?, ?, ?, ?)",
                    (biz, payment_id, procurement_id, part),
                )
        elif change < -0.005:
            take_back(db, biz, payment_id, -change, f"{payment_how(method, reference)}: payment lowered by {money(-change)}", stamp)
        db.execute(
            "UPDATE supplier_payments SET amount=?, method=?, reference=?, paid_on=?, note=? WHERE id=? AND business_id=?",
            (amount, method, reference, paid_on, note, payment_id, biz),
        )
        # Its entry in Accounts is the same payment, so it changes with it (keeping its time of day if the date did not change)
        entry = db.one("SELECT id, occurred_at FROM cashbook WHERE business_id=? AND source='supplier_payment' AND source_id=?", (biz, payment_id))
        when = entry["occurred_at"] if entry and paid_on == pay["paid_on"] else stamp_for_day(paid_on)
        if entry:
            db.execute(
                "UPDATE cashbook SET amount=?, method=?, reference=?, occurred_at=?, party=?, description=? WHERE id=?",
                (amount, method, reference, when, supplier["name"], note or "Payment to supplier", entry["id"]),
            )
        else:
            record_money(db, biz, "out", amount, method, "supplier", reference, supplier["name"], note or "Payment to supplier", "supplier_payment", payment_id, when)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be updated, so nothing was changed. Please try again.", "error")
        return back

    if abs(change) < 0.005:
        flash(f"Payment of {paid_on} updated.", "ok")
    elif change > 0:
        flash(f"Payment raised from {money(pay['amount'])} to {money(amount)}; the extra {money(change)} went to the oldest unpaid purchases. Balance now owed: {money(owed - change)}.", "ok")
    else:
        flash(f"Payment lowered from {money(pay['amount'])} to {money(amount)}; {money(-change)} is owed again on the newest purchases it had paid. Balance now owed: {money(owed - change)}.", "ok")
    return back


@app.post("/suppliers/<int:supplier_id>/payments/<int:payment_id>/delete")
@need("admin")
def remove_supplier_payment(supplier_id, payment_id):
    """Takes a payment off the statement: what it paid on each purchase is owed again, and it leaves Accounts."""
    db, biz = get_db(), g.user["business_id"]
    get_supplier(db, supplier_id)
    back = redirect(url_for("supplier_statement", supplier_id=supplier_id))
    pay = get_supplier_payment(db, biz, supplier_id, payment_id)
    if pay["amount"] <= 0.005:
        flash("A payment correction cannot be removed here.", "error")
        return back
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        take_back(db, biz, payment_id, pay["amount"], f"{payment_how(pay['method'], pay['reference'])}: payment of {pay['paid_on'] or 'an earlier date'} removed", stamp)
        db.execute("DELETE FROM supplier_payment_allocations WHERE payment_id=? AND business_id=?", (payment_id, biz))
        db.execute("DELETE FROM supplier_payments WHERE id=? AND business_id=?", (payment_id, biz))
        db.execute("DELETE FROM cashbook WHERE business_id=? AND source='supplier_payment' AND source_id=?", (biz, payment_id))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be removed, so nothing was changed. Please try again.", "error")
        return back
    flash(f"The {money(pay['amount'])} payment of {pay['paid_on'] or 'an earlier date'} was removed. What it paid is owed again. Balance now owed: {money(supplier_owed(db, biz, supplier_id))}.", "ok")
    return back


@app.post("/suppliers/<int:supplier_id>/update")
@need("admin")
def update_supplier(supplier_id):
    db, biz = get_db(), g.user["business_id"]
    get_supplier(db, supplier_id)
    name, phone = text("name"), text("phone")
    nkey, pkey = supplier_key(name), phone_key(phone)
    clash = db.one("SELECT id FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?", (biz, nkey, pkey, supplier_id))
    if not nkey:
        flash("The supplier needs a name.", "error")
    elif clash:
        flash("Another supplier account already has that name and phone number.", "error")
    else:
        db.execute("UPDATE suppliers SET name=?, phone=?, name_key=?, phone_key=? WHERE id=? AND business_id=?", (name, phone, nkey, pkey, supplier_id, biz))
        db.commit()
        flash("Supplier details saved. Purchases already logged keep the supplier name typed on them.", "ok")
    return redirect(url_for("supplier_statement", supplier_id=supplier_id))


SUPPLIER_COLUMNS = "id, name, phone, name_key, phone_key, created_at"


def supplier_distinct_pairs(db, biz):
    return {(r["low_id"], r["high_id"]) for r in db.all("SELECT low_id, high_id FROM supplier_distinct WHERE business_id=?", (biz,))}


def find_supplier_duplicates(db, biz):
    """Pairs of supplier accounts that may be the same supplier, strongest evidence first."""
    people = db.all(
        """SELECT s.id, s.name, s.phone, s.name_key, s.phone_key, s.created_at,
                  COUNT(pr.id) AS purchases, COALESCE(SUM(pr.total_cost), 0) AS total,
                  COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) AS owed
           FROM suppliers s LEFT JOIN procurements pr ON pr.supplier_id = s.id
           WHERE s.business_id=? GROUP BY s.id, s.name, s.phone, s.name_key, s.phone_key, s.created_at ORDER BY s.id LIMIT ?""",
        (biz, DUPLICATE_SCAN_LIMIT),
    )
    return duplicate_pairs(people, supplier_distinct_pairs(db, biz))


def supplier_twins(db, biz, supplier_id):
    """Other accounts sharing this supplier's phone number, unless an admin already said they are different suppliers."""
    me = db.one("SELECT phone_key FROM suppliers WHERE id=? AND business_id=?", (supplier_id, biz))
    if not me or not me["phone_key"]:
        return []
    others = db.all("SELECT id, name, phone FROM suppliers WHERE business_id=? AND phone_key=? AND id<>? ORDER BY id", (biz, me["phone_key"], supplier_id))
    ruled_out = supplier_distinct_pairs(db, biz)
    return [other for other in others if pair_key(supplier_id, other["id"]) not in ruled_out]


@app.get("/suppliers/duplicates")
@need("admin")
def supplier_duplicates():
    pairs = find_supplier_duplicates(get_db(), g.user["business_id"])
    return render_template("supplier_duplicates.html", pairs=pairs[:DUPLICATES_SHOWN], total=len(pairs))


def supplier_pair_from_form(db, biz, first, second):
    a = db.one(f"SELECT {SUPPLIER_COLUMNS} FROM suppliers WHERE id=? AND business_id=?", (request.form.get(first, type=int), biz))
    b = db.one(f"SELECT {SUPPLIER_COLUMNS} FROM suppliers WHERE id=? AND business_id=?", (request.form.get(second, type=int), biz))
    return (a, b) if a and b and a["id"] != b["id"] else (None, None)


def absorb_supplier(db, biz, keep, remove):
    """Moves one supplier account into another: its purchases, its payments and the spellings that led to it. keep is updated
    in place (a phone number it lacked, the earliest start date). The caller commits. Returns (purchases moved, payments moved)."""
    moved = db.execute("UPDATE procurements SET supplier_id=? WHERE supplier_id=? AND business_id=?", (keep["id"], remove["id"], biz)).rowcount
    paid = db.execute("UPDATE supplier_payments SET supplier_id=? WHERE supplier_id=? AND business_id=?", (keep["id"], remove["id"], biz)).rowcount
    # The removed account's spelling, and any spellings merged into it earlier, now lead to the kept account
    db.execute("UPDATE supplier_aliases SET supplier_id=? WHERE supplier_id=? AND business_id=?", (keep["id"], remove["id"], biz))
    db.execute("DELETE FROM supplier_aliases WHERE business_id=? AND name_key=? AND phone_key=?", (biz, remove["name_key"], remove["phone_key"]))
    db.execute(
        "INSERT INTO supplier_aliases (business_id, name_key, phone_key, supplier_id) VALUES (?, ?, ?, ?)",
        (biz, remove["name_key"], remove["phone_key"], keep["id"]),
    )
    db.execute("DELETE FROM supplier_distinct WHERE business_id=? AND (low_id=? OR high_id=?)", (biz, remove["id"], remove["id"]))
    db.execute("DELETE FROM suppliers WHERE id=? AND business_id=?", (remove["id"], biz))

    since = min(keep["created_at"] or "9999", remove["created_at"] or "9999")
    phone, pkey = keep["phone"], keep["phone_key"]
    if not pkey and remove["phone_key"]:
        taken = db.one(
            "SELECT id FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?",
            (biz, keep["name_key"], remove["phone_key"], keep["id"]),
        )
        if not taken:
            phone, pkey = remove["phone"], remove["phone_key"]
    keep.update(phone=phone, phone_key=pkey, created_at=None if since == "9999" else since)
    db.execute(
        "UPDATE suppliers SET phone=?, phone_key=?, created_at=? WHERE id=? AND business_id=?",
        (phone, pkey, keep["created_at"], keep["id"], biz),
    )
    return moved, paid


@app.post("/suppliers/combine")
@need("admin")
def combine_suppliers():
    """Combines two or more ticked supplier statements into one, under the name given: every purchase and payment moves onto it,
    the other accounts are removed, and their spellings lead to it from now on."""
    db, biz = get_db(), g.user["business_id"]
    # Back to the list as it was: the search and balance filter come with the form
    back = redirect(url_for("suppliers", **{key: text(key) for key in ("q", "balance") if text(key)}))
    ids = set(read_ids())
    chosen = [r for r in db.all(f"SELECT {SUPPLIER_COLUMNS} FROM suppliers WHERE business_id=? ORDER BY id", (biz,)) if r["id"] in ids]
    final = clean_name(text("supplier_name"), 100)
    if len(ids) < 2:
        flash("Tick at least two supplier statements to combine.", "error")
        return back
    if len(chosen) != len(ids):
        flash("Some of the ticked suppliers no longer exist. Refresh the page and try again.", "error")
        return back
    if not final:
        flash("Type the name for the combined statement.", "error")
        return back

    # The account already called that stays, keeping its own spelling; otherwise the oldest one does, renamed
    keep = next((r for r in chosen if norm(r["name"]) == norm(final)), chosen[0])
    if norm(keep["name"]) == norm(final):
        final = keep["name"]
    others = [r for r in chosen if r is not keep]
    # The keys the kept account was found by before anything changed (it may take a phone number from another below)
    spellings = {(keep["name_key"], keep["phone_key"])}
    moved = paid = 0
    try:
        for other in others:
            m, p = absorb_supplier(db, biz, keep, other)
            moved, paid = moved + m, paid + p
        nkey = supplier_key(final)
        spellings.add((keep["name_key"], keep["phone_key"]))
        if final != keep["name"]:
            clash = db.one("SELECT name FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?", (biz, nkey, keep["phone_key"], keep["id"]))
            if clash:
                db.rollback()
                flash(f"There is already a supplier account called '{clash['name']}' with the same phone number. Tick it as well to combine it too. Nothing was changed.", "error")
                return back
            db.execute("UPDATE suppliers SET name=?, name_key=? WHERE id=? AND business_id=?", (final, nkey, keep["id"], biz))
        # The kept account's old spellings still lead to it; a remembered spelling that is now its own name is not needed
        for name_key_, phone_key_ in spellings:
            db.execute("DELETE FROM supplier_aliases WHERE business_id=? AND name_key=? AND phone_key=?", (biz, name_key_, phone_key_))
            if (name_key_, phone_key_) != (nkey, keep["phone_key"]):
                db.execute(
                    "INSERT INTO supplier_aliases (business_id, name_key, phone_key, supplier_id) VALUES (?, ?, ?, ?)",
                    (biz, name_key_, phone_key_, keep["id"]),
                )
        # Accounts lists supplier payments under the account's name
        db.execute(
            """UPDATE cashbook SET party=? WHERE business_id=? AND source='supplier_payment'
               AND source_id IN (SELECT id FROM supplier_payments WHERE supplier_id=? AND business_id=?)""",
            (final, biz, keep["id"], biz),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The statements could not be combined, so nothing was changed. Please try again.", "error")
        return back

    names = ", ".join(f"'{r['name']}'" for r in chosen)
    flash(f"Combined {names} into one statement, '{final}': {moved} purchase(s) and {paid} payment(s) moved onto it.", "ok")
    return redirect(url_for("supplier_statement", supplier_id=keep["id"]))


@app.post("/suppliers/merge")
@need("admin")
def merge_suppliers():
    db, biz = get_db(), g.user["business_id"]
    keep, remove = supplier_pair_from_form(db, biz, "keep", "remove")
    if keep is None:
        flash("Those two accounts could not be found. One may already have been merged.", "error")
        return redirect(url_for("supplier_duplicates"))

    try:
        moved, paid = absorb_supplier(db, biz, keep, remove)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be saved, so nothing was changed. Please try again.", "error")
        return redirect(url_for("supplier_duplicates"))

    flash(f"Merged '{remove['name']}' into '{keep['name']}': {moved} purchase(s) and {paid} payment(s) moved onto this statement.", "ok")
    return redirect(url_for("supplier_statement", supplier_id=keep["id"]))


@app.post("/suppliers/distinct")
@need("admin")
def mark_suppliers_distinct():
    db, biz = get_db(), g.user["business_id"]
    a, b = supplier_pair_from_form(db, biz, "a", "b")
    if a is not None:
        low, high = pair_key(a["id"], b["id"])
        if not db.one("SELECT id FROM supplier_distinct WHERE business_id=? AND low_id=? AND high_id=?", (biz, low, high)):
            db.execute("INSERT INTO supplier_distinct (business_id, low_id, high_id) VALUES (?, ?, ?)", (biz, low, high))
            db.commit()
        flash(f"Noted: '{a['name']}' and '{b['name']}' are different suppliers. They will not be suggested again.", "ok")
    back_to = request.form.get("back_to", type=int)
    return redirect(url_for("supplier_statement", supplier_id=back_to) if back_to else url_for("supplier_duplicates"))


def link_existing_suppliers():
    """Opens accounts for suppliers named on purchases logged before supplier accounts existed. Safe to repeat."""
    db = Database()
    try:
        unlinked = db.all(
            "SELECT id, business_id, supplier_name, purchase_date FROM procurements WHERE supplier_id IS NULL AND supplier_name IS NOT NULL AND supplier_name <> '' ORDER BY id"
        )
        for p in unlinked:
            supplier_id = supplier_for(db, p["business_id"], p["supplier_name"], "", p["purchase_date"])
            if supplier_id:
                db.execute("UPDATE procurements SET supplier_id=? WHERE id=?", (supplier_id, p["id"]))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()


link_existing_suppliers()


# --- REPORTS ---
REPORT_PERIODS = (
    ("this_month", "This month"), ("last_month", "Last month"), ("last_30", "Last 30 days"),
    ("this_year", "This year"), ("all", "All time"), ("custom", "Custom range"),
)
REPORT_PARAMS = ("period", "from", "to", "view", "dept")


def report_period(default="this_month"):
    """Returns (choice, first day, last day) from the query string; both days are None for 'all time'."""
    today = now().date()
    choice = request.args.get("period", default)
    start = end = None
    if choice == "custom":
        try:
            start = datetime.date.fromisoformat(request.args.get("from", ""))
            end = datetime.date.fromisoformat(request.args.get("to", ""))
            if end < start:
                start, end = end, start
        except ValueError:
            choice = "this_month"

    if choice == "last_month":
        end = today.replace(day=1) - datetime.timedelta(days=1)
        start = end.replace(day=1)
    elif choice == "last_30":
        start, end = today - datetime.timedelta(days=29), today
    elif choice == "this_year":
        start, end = today.replace(month=1, day=1), today.replace(month=12, day=31)
    elif choice not in ("all", "custom"):
        choice = "this_month"
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return choice, start, end


def report_url(endpoint="reports", **changes):
    """Link to a report page that keeps the current period and view."""
    params = {key: value for key, value in request.args.items() if key in REPORT_PARAMS}
    params.update(changes)
    if endpoint == "reports":
        params.pop("dept", None)
    return url_for(endpoint, **{key: value for key, value in params.items() if value})


def gather_report(db, biz, start, end, dept=None):
    """Sale lines and purchases dated inside the period, optionally for one department."""
    low = start.isoformat() if start else "0000-00-00"
    high = (end + datetime.timedelta(days=1)).isoformat() if end else "9999-99-99"
    sale_sql = """SELECT ii.department, ii.item_code, ii.item_name, ii.qty, ii.unit_price, ii.line_total,
                         i.id AS invoice_id, i.invoice_no, i.kind, i.customer_name, i.created_at, i.total, i.amount_paid
                  FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
                  WHERE ii.business_id=? AND i.payment_status <> 'Cancelled' AND i.created_at >= ? AND i.created_at < ?"""
    buy_sql = """SELECT id, department, item_code, name, supplier_name, purchase_date, quantity, total_cost, amount_paid
                 FROM procurements WHERE business_id=? AND purchase_date >= ? AND purchase_date < ?"""
    params = [biz, low, high]
    if dept is not None:
        sale_sql += " AND ii.department=?"
        buy_sql += " AND department=?"
        params.append(dept)
    return db.all(sale_sql + " ORDER BY i.created_at, ii.id", params), db.all(buy_sql + " ORDER BY purchase_date, id", params)


def blank_totals():
    return {"sales": 0.0, "received": 0.0, "purchases": 0.0, "paid": 0.0, "units_in": 0, "units_sold": 0, "units_farm": 0}


def summarise(sales, buys, sale_key, buy_key):
    """Totals of money and stock in and out, grouped by whatever the key functions return."""
    groups = {}
    for r in sales:
        t = groups.setdefault(sale_key(r), blank_totals())
        if r["kind"] == "FARM_USE":
            t["units_farm"] += r["qty"]
        else:
            t["sales"] += r["line_total"]
            t["units_sold"] += r["qty"]
            # An invoice's payment is shared across its lines in proportion to their value
            t["received"] += r["line_total"] * r["amount_paid"] / r["total"] if r["total"] else 0.0
    for r in buys:
        t = groups.setdefault(buy_key(r), blank_totals())
        t["purchases"] += r["total_cost"] or 0.0
        t["paid"] += r["amount_paid"] or 0.0
        t["units_in"] += r["quantity"] or 0
    return groups


def time_buckets(start, end, sales, buys):
    """Picks daily, monthly or yearly steps for the period. Returns ([(key, label)], key length)."""
    if start is None:
        seen = [r["created_at"][:10] for r in sales] + [r["purchase_date"][:10] for r in buys]
        try:
            start, end = datetime.date.fromisoformat(min(seen)), datetime.date.fromisoformat(max(seen))
        except ValueError:
            start = end = now().date()

    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if (end - start).days < 62:
        days = [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]
        return [(d.isoformat(), d.strftime("%d %b")) for d in days], 10
    if months <= 36:
        firsts = [datetime.date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 1) for i in range(months)]
        return [(d.strftime("%Y-%m"), d.strftime("%b %Y")) for d in firsts], 7
    return [(str(year), str(year)) for year in range(start.year, end.year + 1)], 4


def timeline_rows(start, end, sales, buys):
    buckets, length = time_buckets(start, end, sales, buys)
    by_time = summarise(sales, buys, lambda r: r["created_at"][:length], lambda r: r["purchase_date"][:length])
    return [dict(by_time.get(key, blank_totals()), label=label) for key, label in buckets]


def compact(n):
    """1250 -> 1.3K, for axis ticks."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:.2f}".rstrip("0").rstrip(".")


def nice_ceiling(value):
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    return next(step * magnitude for step in (1, 2, 4, 8, 10) if value <= step * magnitude)


def column_chart(labels, series, fmt):
    """Geometry for a grouped column chart drawn as SVG by _chart.html. series is a list of (name, values)."""
    width, height, left, right, top, bottom = 720, 250, 56, 8, 10, 30
    plot_w, plot_h, base = width - left - right, height - top - bottom, height - bottom
    peak = max([v for _, values in series for v in values] + [0])
    ceiling = nice_ceiling(peak)
    count = max(len(labels), 1)
    band = plot_w / count
    gap = 2
    bar_w = max(1.5, min(24.0, (band * 0.72 - gap * (len(series) - 1)) / len(series)))
    group_w = len(series) * bar_w + gap * (len(series) - 1)

    bars, hits = [], []
    for i, label in enumerate(labels):
        group_x = left + i * band + (band - group_w) / 2
        tip = [label]
        for j, (name, values) in enumerate(series):
            tip.append(f"{name}: {fmt(values[i])}")
            h = plot_h * values[i] / ceiling
            if h <= 0:
                continue
            h = max(h, 1.0)
            x, y, r = group_x + j * (bar_w + gap), base - h, min(4.0, h, bar_w / 2)
            # Rounded at the data end, square on the baseline
            bars.append({"series": j + 1, "d": (
                f"M{x:.1f},{base:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
                f"H{x + bar_w - r:.1f}Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f}V{base:.1f}Z"
            )})
        hits.append({"x": round(left + i * band, 1), "w": round(band, 1), "tip": "\n".join(tip)})

    every = math.ceil(count / 12)
    limit = 14 if count > 6 else 24
    return {
        "width": width, "height": height, "left": left, "right": width - right, "top": top, "plot_h": plot_h, "base": base,
        "series": [name for name, _ in series], "bars": bars, "hits": hits, "empty": peak <= 0,
        "ticks": [{"y": round(base - plot_h * i / 4, 1), "label": compact(ceiling * i / 4)} for i in range(5)],
        "xlabels": [
            {"x": round(left + i * band + band / 2, 1), "label": label if len(label) <= limit else label[:limit - 1] + "…"}
            for i, label in enumerate(labels) if i % every == 0
        ],
    }


def whole(value):
    return f"{int(value):,}"


def report_charts(rows):
    """The money and stock charts for a list of rows that each carry a 'label'."""
    labels = [r["label"] for r in rows]
    return {
        "money": column_chart(labels, [("Sales", [r["sales"] for r in rows]), ("Purchases", [r["purchases"] for r in rows])], money),
        "units": column_chart(
            labels,
            [("Units in (purchased)", [r["units_in"] for r in rows]), ("Units out (sold + farm use)", [r["units_sold"] + r["units_farm"] for r in rows])],
            whole,
        ),
    }


def report_context(choice, start, end):
    return {
        "periods": REPORT_PERIODS, "period": choice, "start": start, "end": end,
        "view": "table" if request.args.get("view") == "table" else "visual", "report_url": report_url,
    }


def sum_rows(rows):
    total = blank_totals()
    for r in rows:
        for key in total:
            total[key] += r[key]
    return total


@app.get("/reports")
@need(None)
def reports():
    db, biz = get_db(), g.user["business_id"]
    choice, start, end = report_period()
    sales, buys = gather_report(db, biz, start, end)

    by_dept = summarise(sales, buys, lambda r: r["department"] or "General", lambda r: r["department"] or "General")
    names = sorted(set(department_names(db, biz)) | set(by_dept))
    departments = [dict(by_dept.get(name, blank_totals()), label=name) for name in names]
    timeline = timeline_rows(start, end, sales, buys)
    return render_template(
        "reports.html", departments=departments, totals=sum_rows(departments), timeline=timeline,
        dept_charts=report_charts(departments), time_charts=report_charts(timeline), **report_context(choice, start, end),
    )


@app.get("/reports/department")
@need(None)
def report_department():
    db, biz = get_db(), g.user["business_id"]
    dept = request.args.get("dept", "").strip()
    if not dept:
        return redirect(report_url("reports"))
    choice, start, end = report_period()
    sales, buys = gather_report(db, biz, start, end, dept)

    timeline = timeline_rows(start, end, sales, buys)
    products = {}
    for r in sales:
        p = products.setdefault((r["item_code"], r["item_name"]), {"code": r["item_code"], "name": r["item_name"], "sold": 0, "value": 0.0, "farm": 0})
        if r["kind"] == "FARM_USE":
            p["farm"] += r["qty"]
        else:
            p["sold"] += r["qty"]
            p["value"] += r["line_total"]
    products = sorted(products.values(), key=lambda p: (-p["value"], -p["sold"], p["name"] or ""))
    best = [p for p in products if p["value"] > 0][:8]
    top_value = best[0]["value"] if best else 0.0

    return render_template(
        "report_department.html", dept=dept, totals=sum_rows(timeline), timeline=timeline, time_charts=report_charts(timeline),
        products=products, best=[dict(p, pct=round(100 * p["value"] / top_value, 1)) for p in best],
        purchases=[with_payment_state(dict(r)) for r in buys],
        sale_lines=[r for r in sales if r["kind"] != "FARM_USE"], farm_lines=[r for r in sales if r["kind"] == "FARM_USE"],
        **report_context(choice, start, end),
    )


# --- CSV IMPORT (ADMIN) ---
IMPORT_MAX_ROWS = 2000
IMPORT_MAX_ERRORS_SHOWN = 50

# Field -> accepted column headings (compared in lower case, with underscores read as spaces)
PURCHASE_COLUMNS = {
    "item_code": ("item code", "code", "sku", "barcode"),
    "name": ("name", "item name", "product name", "product", "item"),
    "category": ("category",),
    "department": ("department", "dept"),
    "quantity": ("quantity", "qty", "quantity bought"),
    "unit_cost": ("unit cost", "cost"),
    "price": ("selling price", "sell price", "price"),
    "supplier_name": ("supplier", "supplier name"),
    "invoice_no": ("invoice no", "invoice number", "invoice", "supplier invoice"),
    "purchase_date": ("date", "purchase date"),
    "batch_no": ("batch no", "batch number", "batch"),
    "date_planted": ("date planted",),
    "expiry_date": ("expiry", "expiry date"),
    "payment_status": ("payment status", "status"),
    "amount_paid": ("amount paid", "paid"),
    "seeds_per_pack": ("seeds per pack", "seeds in pack", "seeds in a pack", "pack size", "seeds/pack"),
    "unit": ("unit", "uom", "unit of measure", "packaging", "packaging unit"),
    "in_inventory": ("in inventory", "inventory", "inventory item", "add to inventory", "also inventory"),
    "supplier_phone": ("supplier phone", "supplier contact", "supplier phone number"),
    "payment_method": ("payment method", "method", "mode of payment", "paid by", "payment mode"),
    "payment_reference": ("payment reference", "reference", "receipt number", "receipt no", "reference number", "transaction id", "payment ref"),
}
YES_WORDS = ("yes", "y", "true", "1", "x")
NO_WORDS = ("no", "n", "false", "0")
SALES_COLUMNS = {
    "invoice_no": ("invoice no", "invoice number", "invoice"),
    "date": ("date", "date/time", "date time", "sale date"),
    "customer_name": ("customer", "customer name"),
    "customer_phone": ("phone", "customer phone", "phone number"),
    "item_code": ("item code", "code", "sku", "barcode"),
    "item_name": ("item name", "product name", "product", "item", "name"),
    "quantity": ("quantity", "qty"),
    "unit_price": ("unit price", "price"),
    "payment_status": ("payment status", "status"),
    "amount_paid": ("amount paid", "paid"),
    "payment_method": ("payment method", "method", "mode of payment", "paid by", "payment mode"),
    "payment_reference": ("payment reference", "reference", "receipt number", "receipt no", "reference number", "transaction id", "payment ref"),
}
IMPORT_TEMPLATES = {
    "purchases": (
        ["Item Code", "Name", "Category", "Department", "Quantity", "Unit Cost", "Selling Price", "Supplier", "Invoice No", "Purchase Date", "Batch No", "Date Planted", "Expiry Date", "Payment Status", "Amount Paid", "Seeds Per Pack", "Unit", "In Inventory", "Supplier Phone", "Payment Method", "Payment Reference"],
        ["SEED-001", "Tomato Seed Pack", "Seeds", "Greenhouse", 50, 450, 600, "Agro Supplies Ltd", "SUP-1001", "2026-10-01", "B-22", "", "2027-10-01", "Paid", "", 1000, "pack", "Yes", "0977123456", "Bank transfer", "TRX-5521"],
    ),
    "sales": (
        ["Invoice No", "Date", "Customer", "Phone", "Item Code", "Item Name", "Quantity", "Unit Price", "Payment Status", "Amount Paid", "Payment Method", "Payment Reference"],
        ["", "2026-10-01 09:30", "Jane Banda", "0977123456", "SEED-001", "Maize Seed 10kg", 2, 600, "Paid", "", "Airtel Money", "MP261001.0930"],
    ),
    "payroll": (
        ["Employee Name", "NRC Number", "Employment Type", "Pay Month", "Basic Salary", "Expected Days", "Worked Days", "Bonus: Transport", "Deduction: Advance", "Employee NAPSA", "Employee NHIMA", "Employer NAPSA", "Employer NHIMA", "Net Pay"],
        ["Mary Banda", "123456/10/1", "Permanent", "2026-10", 4400, 22, 22, 100, 0, "", "", "", "", ""],
    ),
}
PAYROLL_COLUMNS = {
    "employee": ("employee name", "employee", "full name", "name"),
    "nrc": ("nrc number", "nrc", "nrc no"),
    "employment_type": ("employment type", "employment status", "type"),
    "pay_month": ("pay month", "month", "pay period", "period", "date"),
    "basic_salary": ("basic salary", "basic", "salary", "basic wage", "wage"),
    "expected_days": ("expected days", "expected work days", "exp days"),
    "worked_days": ("worked days", "days worked", "work days", "actual days worked"),
    "bonuses": ("bonuses", "bonus", "allowances", "additions"),
    "deductions": ("deductions", "deduction", "other deductions"),
    "napsa": ("napsa", "employee napsa", "napsa employee"),
    "nhima": ("nhima", "employee nhima", "nhima employee"),
    "employer_napsa": ("employer napsa", "napsa employer"),
    "employer_nhima": ("employer nhima", "nhima employer"),
    "net_pay": ("net pay", "net payable", "net salary", "net"),
}
# Excel likes to turn "2026-10" into "Oct-26", so that spelling is accepted too
PAY_MONTH_FORMATS = ("%Y-%m", "%B %Y", "%b %Y", "%m/%Y", "%Y/%m", "%B-%Y", "%b-%Y", "%b-%y", "%B-%y")
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y/%m/%d",
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


class ImportProblem(Exception):
    pass


def read_csv_upload(columns, required):
    """Returns the uploaded file's rows as dicts keyed by field name, each with its spreadsheet row number in '_row'."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ImportProblem("Choose a CSV file first.")
    raw = upload.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1252", errors="replace")

    # Excel saves CSV with semicolons in some regional settings
    first_line = content.split("\n", 1)[0]
    delimiter = max(",;\t", key=first_line.count)
    rows = list(csv.reader(io.StringIO(content, newline=""), delimiter=delimiter))
    if not rows:
        raise ImportProblem("That file is empty.")

    lookup = {alias: field for field, aliases in columns.items() for alias in aliases}
    fields = [lookup.get(" ".join(heading.replace("_", " ").lower().split())) for heading in rows[0]]
    # Headings such as "Bonus: Transport" or "Deduction: Loan" are free-form named amounts (payroll files)
    named = [re.fullmatch(r"\s*(bonus|deduction)\s*:\s*(.+?)\s*", heading, re.I) for heading in rows[0]]
    missing = [columns[field][0].title() for field in required if field not in fields]
    if missing:
        raise ImportProblem("The file is missing these column headings: " + ", ".join(missing) + ". Download the template to see the expected layout.")

    records = []
    for row_no, cells in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue
        record = dict.fromkeys(columns, "")
        record["_named"] = []
        for field, extra, cell in zip(fields, named, cells):
            cell = cell.strip()
            if field:
                # Undo the apostrophe our own exports put in front of formula-like text
                record[field] = cell[1:] if len(cell) > 1 and cell[0] == "'" and cell[1] in "=@+-" else cell
            elif extra and cell:
                record["_named"].append((extra.group(1).lower(), extra.group(2), cell))
        record["_row"] = row_no
        records.append(record)
    if not records:
        raise ImportProblem("The file has headings but no data rows.")
    if len(records) > IMPORT_MAX_ROWS:
        raise ImportProblem(f"The file has {len(records)} rows. Import at most {IMPORT_MAX_ROWS} at a time by splitting it into smaller files.")
    return records


def parse_amount(raw):
    """Reads '1,250.50', 'ZMW 1250.5' or 'K1250.50' as a number."""
    # '12,5' or '12,50' with no dot is a decimal comma (common in semicolon-separated files); '1,250' is a thousands comma
    if "." not in raw and re.search(r",\d{1,2}$", raw.strip()):
        raw = raw.replace(".", "").replace(",", ".")
    value = float(re.sub(r"[^\d.\-]", "", raw))
    if not math.isfinite(value):
        raise ValueError(raw)
    return value


def parse_whole(raw):
    value = parse_amount(raw)
    if value != int(value):
        raise ValueError(raw)
    return int(value)


def parse_date(raw):
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(raw)


def import_errors(kind, errors):
    return render_template(
        "import.html", max_rows=IMPORT_MAX_ROWS, error_kind=kind, errors=errors[:IMPORT_MAX_ERRORS_SHOWN], error_count=len(errors), methods=PAYMENT_METHODS,
    )


@app.get("/import")
@need("admin")
def import_data():
    return render_template("import.html", max_rows=IMPORT_MAX_ROWS, methods=PAYMENT_METHODS)


@app.get("/import/template/<kind>.csv")
@need("admin")
def import_template(kind):
    if kind not in IMPORT_TEMPLATES:
        abort(404)
    header, example = IMPORT_TEMPLATES[kind]
    return csv_response(f"{kind}-import-template.csv", header, [example])


@app.post("/import/purchases")
@need("admin")
def import_purchases():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(PURCHASE_COLUMNS, ("item_code", "quantity", "unit_cost"))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))
    default_method, problem = import_default_method()
    if problem:
        flash(problem, "error")
        return redirect(url_for("import_data"))

    known_names = {r["item_code"]: r["name"] for r in db.all("SELECT item_code, name FROM products WHERE business_id=?", (biz,))}
    # Items already in stock, plus new ones as the file introduces them, so a seed item's pack size is held to one value
    stocked = {r["item_code"]: r for r in db.all("SELECT item_code, category, department, seeds_per_pack FROM products WHERE business_id=?", (biz,))}
    departments = set(department_names(db, biz))
    known_categories = {norm(r["name"]): r["name"] for r in category_rows(db, biz)}
    known_units = {**{norm(u): u for u in DEFAULT_UNITS}, **used_units(db, biz)}
    today = now().strftime("%Y-%m-%d")
    errors, purchases, new_departments, new_categories = [], [], [], []

    for rec in records:
        problems = []
        code = rec["item_code"]
        # A purchase is also an inventory item unless the file says No. Without the column, every row is, as before.
        word = rec["in_inventory"].strip().lower()
        inventory = word not in NO_WORDS
        if word and word not in NO_WORDS and word not in YES_WORDS:
            problems.append(f"In Inventory '{rec['in_inventory']}' must be Yes or No")
        name = rec["name"] or (known_names.get(code, "") if inventory else "")
        if inventory and not code:
            problems.append("item code is missing")
        elif not name:
            problems.append("name is missing" + (" (needed because this item code is new)" if inventory else ""))
        elif not inventory and code and code in stocked:
            problems.append(f"'{code}' is an inventory item: answer Yes in In Inventory to add to its stock, or use a different code")

        qty = unit_cost = 0
        price = None
        try:
            qty = parse_whole(rec["quantity"])
            if qty <= 0:
                raise ValueError
        except ValueError:
            problems.append(f"quantity '{rec['quantity']}' is not a whole number above zero")
        try:
            unit_cost = parse_amount(rec["unit_cost"])
            if unit_cost < 0:
                raise ValueError
        except ValueError:
            problems.append(f"unit cost '{rec['unit_cost']}' is not a valid amount")
        if rec["price"]:
            try:
                price = parse_amount(rec["price"])
                if price < 0:
                    raise ValueError
            except ValueError:
                problems.append(f"selling price '{rec['price']}' is not a valid amount")

        dates = {}
        for field, label in (("purchase_date", "purchase date"), ("date_planted", "date planted"), ("expiry_date", "expiry date")):
            dates[field] = ""
            if rec[field]:
                try:
                    dates[field] = parse_date(rec[field]).strftime("%Y-%m-%d")
                except ValueError:
                    problems.append(f"{label} '{rec[field]}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")

        amount_paid = None
        if not problems:
            try:
                amount_paid = amount_for_status(rec["payment_status"], rec["amount_paid"], round(qty * unit_cost, 2))
            except ValueError as e:
                problems.append(str(e))

        # How it was paid. Optional in a file (older records have none). Paid to a named supplier, a method given needs its reference number.
        pay_method, pay_ref = default_method, ""
        if not problems and amount_paid and amount_paid > 0.005 and (rec["payment_method"] or rec["payment_reference"]):
            found_method = method_from_text(rec["payment_method"])
            if found_method is None:
                problems.append(f"payment method '{rec['payment_method']}' must be one of: " + ", ".join(METHOD_NAMES))
            elif rec["supplier_name"] and not rec["payment_reference"]:
                problems.append(f"a {found_method.lower()} payment needs its {METHOD_LABELS[found_method].lower()}")
            else:
                pay_method, pay_ref = found_method, rec["payment_reference"][:60]

        # A restock row is filed where the item already lives; a new item uses the category and department in the file,
        # matched to the managed lists ignoring capitals and spacing. Names that are new are added when the file is saved.
        already = stocked.get(code) if inventory else None
        typed_category = clean_name(rec["category"]) or "General"
        category = already["category"] if already else known_categories.get(norm(typed_category), typed_category)
        dept = already["department"] if already else (rec["department"] or "General")

        # Packs, units and selling prices belong to inventory items; a purchase that is only an expense has none of them
        seeds_per_pack, pack_problem = (seeds_per_pack_for(already, category, rec["seeds_per_pack"]) if inventory else (None, None))
        if pack_problem:
            problems.append(pack_problem)

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue

        # Seed items are counted in packs; otherwise the unit in the file, in the spelling already in use
        unit = ""
        if inventory:
            unit = SEED_UNIT if seeds_per_pack else canonical_unit(rec["unit"], known_units)
            if unit:
                known_units.setdefault(norm(unit), unit)
            known_names.setdefault(code, name)
        else:
            price = None
        if not already:
            if inventory:
                stocked[code] = {"category": category, "department": dept, "seeds_per_pack": seeds_per_pack}
            if norm(category) not in known_categories:
                known_categories[norm(category)] = category
                new_categories.append(category)
            if dept not in departments:
                departments.add(dept)
                new_departments.append(dept)
        purchases.append({
            "item_code": code, "name": name, "category": category, "department": dept,
            "quantity": qty, "unit_cost": round(unit_cost, 2), "price": None if price is None else round(price, 2),
            "supplier_name": rec["supplier_name"], "supplier_phone": rec["supplier_phone"], "payment_method": pay_method, "payment_reference": pay_ref,
            "invoice_no": rec["invoice_no"], "purchase_date": dates["purchase_date"] or today,
            "batch_no": rec["batch_no"] if inventory else "", "date_planted": dates["date_planted"] if inventory else "", "expiry_date": dates["expiry_date"],
            "amount_paid": amount_paid, "seeds_per_pack": seeds_per_pack, "unit": unit, "in_inventory": inventory,
        })

    if errors:
        return import_errors("purchases", errors)

    restocked = 0
    try:
        for dept in new_departments:
            db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, dept))
        for category in new_categories:
            db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, category))
        for p in purchases:
            if record_purchase(db, biz, p):
                restocked += 1
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    in_stock = sum(1 for p in purchases if p["in_inventory"])
    only_logged = len(purchases) - in_stock
    flash(
        f"Imported {len(purchases)} purchase row(s): {in_stock - restocked} new item(s) created, {restocked} added to existing stock"
        + (f", {only_logged} logged in Procurements only." if only_logged else "."), "ok",
    )
    return redirect(url_for("procurements"))


@app.post("/import/sales")
@need("admin")
def import_sales():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(SALES_COLUMNS, ("quantity",))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))
    default_method, problem = import_default_method()
    if problem:
        flash(problem, "error")
        return redirect(url_for("import_data"))

    deduct = bool(request.form.get("deduct_stock"))
    listed = {r["item_code"]: r for r in db.all("SELECT id, item_code, name, department, batch_no, quantity, price FROM pos_products WHERE business_id=?", (biz,))}
    inventory_names = {r["item_code"]: r["name"] for r in db.all("SELECT item_code, name FROM products WHERE business_id=?", (biz,))}
    taken_numbers = {r["invoice_no"] for r in db.all("SELECT invoice_no FROM invoices WHERE business_id=?", (biz,))}

    # Rows sharing an invoice number form one invoice; rows without a number each become their own
    groups, by_number = [], {}
    for rec in records:
        number_given = rec["invoice_no"]
        if number_given and number_given in by_number:
            by_number[number_given].append(rec)
        else:
            rows = [rec]
            groups.append((number_given, rows))
            if number_given:
                by_number[number_given] = rows

    errors, invoices, needed = [], [], {}
    for number_given, rows in groups:
        first = rows[0]
        problems = []
        if number_given and number_given in taken_numbers:
            problems.append(f"invoice number '{number_given}' already exists (was this file imported before?)")

        when = now()
        if first["date"]:
            try:
                when = parse_date(first["date"])
            except ValueError:
                problems.append(f"date '{first['date']}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")

        lines, total = [], 0.0
        for rec in rows:
            row_problems = []
            code = rec["item_code"]
            pos_item = listed.get(code) if code else None
            name = rec["item_name"] or (pos_item["name"] if pos_item else inventory_names.get(code, ""))
            if not name:
                row_problems.append("item name is missing and the item code is not one of yours")

            qty, unit_price = 0, 0.0
            try:
                qty = parse_whole(rec["quantity"])
                if qty <= 0:
                    raise ValueError
            except ValueError:
                row_problems.append(f"quantity '{rec['quantity']}' is not a whole number above zero")
            if rec["unit_price"]:
                try:
                    unit_price = parse_amount(rec["unit_price"])
                    if unit_price < 0:
                        raise ValueError
                except ValueError:
                    row_problems.append(f"unit price '{rec['unit_price']}' is not a valid amount")
            elif pos_item:
                unit_price = pos_item["price"]
            else:
                row_problems.append("unit price is missing")
            if deduct and not pos_item:
                row_problems.append(f"item code '{code}' is not listed in the POS, so its stock cannot be deducted")

            if row_problems:
                errors.append(f"Row {rec['_row']}: " + "; ".join(row_problems))
                continue
            line_total = round(qty * unit_price, 2)
            total += line_total
            lines.append((code, name, pos_item["department"] if pos_item else "General", pos_item["batch_no"] if pos_item else "", qty, round(unit_price, 2), line_total))
            if deduct:
                needed[code] = needed.get(code, 0) + qty
        total = round(total, 2)

        status_text = first["payment_status"].lower()
        if status_text in ("", "paid"):
            status, amount_paid = "Paid", total
        elif status_text == "unpaid":
            status, amount_paid = "Unpaid", 0.0
        elif status_text.startswith("partial"):
            status, amount_paid = "Partial Payment", 0.0
            try:
                amount_paid = round(parse_amount(first["amount_paid"]), 2)
                if len(lines) == len(rows) and not 0 < amount_paid < total:
                    raise ValueError
            except ValueError:
                problems.append(f"amount paid '{first['amount_paid']}' must be more than zero and less than the invoice total for a partial payment")
        else:
            status, amount_paid = "Paid", total
            problems.append(f"payment status '{first['payment_status']}' must be Paid, Partial Payment or Unpaid")

        # How it was paid, for Accounts. Optional (older records have none); a method given must be one the app knows.
        pay_method = default_method
        if first.get("payment_method"):
            pay_method = method_from_text(first["payment_method"])
            if pay_method is None:
                problems.append(f"payment method '{first['payment_method']}' must be one of: " + ", ".join(METHOD_NAMES))
        first["pay_method"] = pay_method or NOT_RECORDED

        if problems:
            errors.append(f"Row {first['_row']}: " + "; ".join(problems))
        invoices.append((number_given, first, when, status, total, amount_paid, lines))

    for code, qty in needed.items():
        if qty > listed[code]["quantity"]:
            errors.append(f"Stock: the file sells {qty} of '{code}' but only {listed[code]['quantity']} are listed in the POS.")

    if errors:
        return import_errors("sales", errors)

    year, seq = now().strftime("%Y"), 1
    try:
        for number_given, first, when, status, total, amount_paid, lines in invoices:
            invoice_no = number_given
            while not invoice_no:
                candidate = f"INV-{year}-{seq:04d}"
                seq += 1
                if candidate not in taken_numbers:
                    invoice_no = candidate
            taken_numbers.add(invoice_no)

            sold_at = when.strftime("%Y-%m-%d %H:%M:%S")
            invoice_id = db.insert(
                """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id)
                   VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    biz, invoice_no, first["customer_name"] or WALK_IN, first["customer_phone"], status, total, amount_paid,
                    round(total - amount_paid, 2), g.user["username"] + " (import)", sold_at,
                    customer_for(db, biz, first["customer_name"], first["customer_phone"], sold_at),
                ),
            )
            record_money(
                db, biz, "in", amount_paid, first["pay_method"], "sale", (first.get("payment_reference") or "")[:60], first["customer_name"] or WALK_IN,
                f"Invoice {invoice_no} (imported)", "invoice", invoice_id, sold_at,
            )
            for code, name, dept, batch, qty, unit_price, line_total in lines:
                db.execute(
                    """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, biz, code, name, dept, batch, qty, unit_price, line_total),
                )
        for code, qty in needed.items():
            low_stock_alert(db, listed[code]["name"], code, listed[code]["quantity"], listed[code]["quantity"] - qty, "listed for sale in the POS")
            moved = db.execute(
                "UPDATE pos_products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
                (qty, listed[code]["id"], biz, qty),
            )
            if moved.rowcount != 1:
                raise StockError(code)
        db.commit()
    except StockError:
        db.rollback()
        flash("POS stock changed during the import, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    stock_note = " POS stock was reduced to match." if deduct else " Stock levels were not changed."
    flash(f"Imported {len(invoices)} invoice(s) from {len(records)} row(s).{stock_note}", "ok")
    return redirect(url_for("history"))


def import_default_method():
    """How the money in an imported file was paid, chosen on the upload form: (method, problem). A row's own Payment Method
    column overrides it. 'Not recorded' suits records from before the balances were counted, which never move them anyway."""
    if "default_method" not in request.form:
        return NOT_RECORDED, None
    raw = text("default_method")
    if raw == "not_recorded":
        return NOT_RECORDED, None
    method = method_from_text(raw)
    if method not in METHOD_NAMES:
        return None, "Choose how the money in this file was paid (a row's own Payment Method still counts first), or Not recorded for old records."
    return method, None


def parse_pay_month(raw):
    for fmt in PAY_MONTH_FORMATS:
        try:
            return datetime.datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return parse_date(raw.strip())


@app.post("/import/payroll")
@need("admin")
def import_payroll():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(PAYROLL_COLUMNS, ("employee", "pay_month", "basic_salary"))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))
    default_method, problem = import_default_method()
    if problem:
        flash(problem, "error")
        return redirect(url_for("import_data"))

    allow_repeats = bool(request.form.get("allow_repeats"))
    hr_set = hr_settings_for(db, biz)
    non_working = hr_set["non_working_days"].split(",")
    staff = db.all("SELECT id, full_name, nrc_number FROM employees WHERE business_id=?", (biz,))
    already_paid = {(r["employee_id"], r["pay_month"]) for r in db.all("SELECT employee_id, pay_month FROM payroll WHERE business_id=?", (biz,))}

    def tidy_nrc(value):
        return re.sub(r"\s+", "", value).upper()

    def optional_amount(rec, field, label, problems, minimum=0.0):
        """A blank cell means 'work it out'; anything else must be a valid amount."""
        if not rec[field]:
            return None
        try:
            value = parse_amount(rec[field])
            if minimum is not None and value < minimum:
                raise ValueError
            return value
        except ValueError:
            problems.append(f"{label} '{rec[field]}' is not a valid amount")
            return None

    errors, slips, hires = [], [], {}
    for rec in records:
        problems = []
        name, nrc = rec["employee"], tidy_nrc(rec["nrc"])
        if not name:
            problems.append("employee name is missing")

        period = None
        try:
            period = parse_pay_month(rec["pay_month"])
        except ValueError:
            problems.append(f"pay month '{rec['pay_month']}' is not a month (use YYYY-MM or October 2026)")

        basic = 0.0
        try:
            basic = parse_amount(rec["basic_salary"])
            if basic < 0:
                raise ValueError
        except ValueError:
            problems.append(f"basic salary '{rec['basic_salary']}' is not a valid amount")

        expected = optional_amount(rec, "expected_days", "expected days", problems)
        worked = optional_amount(rec, "worked_days", "worked days", problems)
        napsa = optional_amount(rec, "napsa", "NAPSA", problems)
        nhima = optional_amount(rec, "nhima", "NHIMA", problems)
        employer_napsa = optional_amount(rec, "employer_napsa", "employer NAPSA", problems)
        employer_nhima = optional_amount(rec, "employer_nhima", "employer NHIMA", problems)
        net = optional_amount(rec, "net_pay", "net pay", problems, minimum=None)
        if expected is not None and expected <= 0:
            problems.append("expected days must be above zero")

        named = {"bonus": {}, "deduction": {}}
        totals = (("bonus", "Bonuses", rec["bonuses"]), ("deduction", "Deductions", rec["deductions"]))
        for kind, label, raw in list(totals) + rec["_named"]:
            if not raw:
                continue
            try:
                value = parse_amount(raw)
                if value < 0:
                    raise ValueError
            except ValueError:
                problems.append(f"{kind} '{label}' amount '{raw}' is not a valid amount")
                continue
            if value:
                named[kind][label] = round(named[kind].get(label, 0.0) + value, 2)

        # Find the employee by NRC when the file has one, otherwise by name; unknown people are registered
        employee_id, hire_key = None, None
        if name:
            if nrc:
                matches = [e for e in staff if tidy_nrc(e["nrc_number"] or "") == nrc]
            else:
                matches = [e for e in staff if " ".join(e["full_name"].lower().split()) == " ".join(name.lower().split())]
            if len(matches) > 1:
                problems.append(f"more than one employee matches '{name}'; add an NRC Number column to tell them apart")
            elif matches:
                employee_id = matches[0]["id"]
            else:
                hire_key = nrc or " ".join(name.lower().split())

        if period and employee_id and not allow_repeats and (employee_id, period.strftime("%Y-%m")) in already_paid:
            problems.append(f"{name} already has a payslip for {period.strftime('%B %Y')} (was this file imported before?)")

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue

        if expected is None:
            expected = float(expected_work_days(period.year, period.month, non_working)) or 22.0
        if worked is None:
            worked = expected
        calculated_basic = round(basic / expected * worked, 2)
        # Blank cells follow the statutory rules in HR Settings, each side on its own; amounts in the file are kept as given
        share = statutory_amounts(hr_set, calculated_basic, sum(named["bonus"].values()))
        napsa = share["napsa_employee"] if napsa is None else napsa
        nhima = share["nhima_employee"] if nhima is None else nhima
        employer_napsa = share["napsa_employer"] if employer_napsa is None else round(employer_napsa, 2)
        employer_nhima = share["nhima_employer"] if employer_nhima is None else round(employer_nhima, 2)
        if net is None:
            net = calculated_basic + sum(named["bonus"].values()) - sum(named["deduction"].values()) - napsa - nhima

        if hire_key and hire_key not in hires:
            casual = rec["employment_type"].lower().startswith(("casual", "temp", "part"))
            hires[hire_key] = {"name": name, "nrc": rec["nrc"], "salary": round(basic, 2), "type": "Casual Worker" if casual else "Permanent", "since": period.strftime("%Y-%m-01")}
        slips.append({
            "employee_id": employee_id, "hire_key": hire_key, "name": name, "month": period.strftime("%Y-%m"), "basic": round(basic, 2),
            "expected": expected, "worked": worked, "calculated_basic": calculated_basic, "bonuses": named["bonus"], "deductions": named["deduction"],
            "napsa": round(napsa, 2), "nhima": round(nhima, 2), "employer_napsa": employer_napsa, "employer_nhima": employer_nhima, "net": round(net, 2),
        })

    if errors:
        return import_errors("salaries and wages", errors)

    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        new_ids = {}
        for key, hire in hires.items():
            new_ids[key] = db.insert(
                """INSERT INTO employees (business_id, full_name, nrc_number, basic_salary, employment_type, address, age, start_date, expected_days_week)
                   VALUES (?, ?, ?, ?, ?, '', NULL, ?, 5)""",
                (biz, hire["name"], hire["nrc"], hire["salary"], hire["type"], hire["since"]),
            )
        for s in slips:
            slip_id = db.insert(
                """INSERT INTO payroll (business_id, employee_id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic,
                                        bonuses_json, deductions_json, napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    biz, s["employee_id"] or new_ids[s["hire_key"]], s["name"], s["month"], s["basic"], s["expected"], s["worked"], s["calculated_basic"],
                    json.dumps(s["bonuses"]), json.dumps(s["deductions"]), s["napsa"], s["nhima"], s["employer_napsa"], s["employer_nhima"], s["net"], stamp,
                ),
            )
            # Imported pay was paid out at the time, dated here at the end of its pay month, by the method chosen for the file
            year, month = int(s["month"][:4]), int(s["month"][5:7])
            record_payslip_money(
                db, biz, slip_id, s["name"], s["month"], s["net"], s["napsa"] + s["nhima"] + s["employer_napsa"] + s["employer_nhima"], default_method,
                f"{s['month']}-{calendar.monthrange(year, month)[1]:02d} 00:00:00",
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    flash(f"Imported {len(slips)} payslip(s). {len(hires)} new employee(s) were registered from the file.", "ok")
    return redirect(url_for("hr"))


@app.errorhandler(413)
def upload_too_large(error):
    flash("That file is too large. The limit is 2 MB; split it into smaller files.", "error")
    return redirect(url_for("import_data"))


# --- PLANTING RECORDS (GREENHOUSE) ---
# A planting is a batch of seeds put in the ground. The seedlings expected are the seeds planted less the expected mortality.
# A batch appears on the POS RELEASE_DAYS before it is expected to be ready (at once if it is marked ready sooner), under its
# own code so that its sales can be told apart from other batches. Pre-orders hold seedlings from a batch straight away but only
# become a sale when the customer collects them and an admin has approved it.
RELEASE_DAYS = 7
PLANTING_MARK = "-SDL-P"
PLANTING_STAGES = {"growing": "Growing", "due": "Due", "ready": "Ready", "record": "Historical record"}


def today_str():
    return now().strftime("%Y-%m-%d")


def add_days(day, days):
    return (datetime.datetime.strptime(day, "%Y-%m-%d") + datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def day_or_none(raw):
    """A date typed or pasted in any of the accepted layouts as YYYY-MM-DD, or None when it is not a date."""
    try:
        return parse_date(raw.strip()).strftime("%Y-%m-%d")
    except ValueError:
        return None


def seedlings_from(seeds, mortality):
    return math.floor(seeds * (100 - mortality) / 100 + 1e-9)


def planting_label(p):
    """What the batch is called on the POS and on invoices, so two batches of the same plant can be told apart."""
    return f"{p['name']} ({p['batch_no'] or 'planted ' + (p['planted_on'] or '')})"


def on_pos_now(p, today):
    # A historical record is never listed, however old or "ready" its dates say it is - it is not really there
    if not p["still_in_greenhouse"]:
        return False
    return bool(p["actual_ready_on"]) or (p["ready_on"] or today) <= add_days(today, RELEASE_DAYS)


def can_plant():
    return can("procure") or can("transfer")


def with_figures(db, biz, rows):
    """Adds to each planting what has been sold, what is held by pre-orders, what is left, and where it stands."""
    today = today_str()
    sold = {
        r["code"]: r["n"]
        for r in db.all(
            """SELECT ii.item_code AS code, SUM(ii.qty) AS n FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
               WHERE ii.business_id=? AND ii.item_code LIKE ? AND i.payment_status <> 'Cancelled' GROUP BY ii.item_code""",
            (biz, "%" + PLANTING_MARK + "%"),
        )
    }
    held = {}
    # A collected pre-order counts only while its invoice exists and is not cancelled: deleting or cancelling that
    # invoice puts the seedlings back
    for r in db.all(
        """SELECT planting_id, status, SUM(qty) AS n FROM preorders po WHERE business_id=?
           AND (status='pending' OR (status='collected' AND EXISTS (SELECT 1 FROM invoices i WHERE i.id = po.invoice_id AND i.payment_status <> 'Cancelled')))
           GROUP BY planting_id, status""",
        (biz,),
    ):
        held[(r["planting_id"], r["status"])] = r["n"] or 0
    out = []
    for p in rows:
        s = int(sold.get(p["pos_code"], 0) or 0)
        pending = int(held.get((p["id"], "pending"), 0))
        collected = int(held.get((p["id"], "collected"), 0))
        ready_on = p["ready_on"] or today
        if not p["still_in_greenhouse"]:
            stage = "record"
        else:
            stage = "ready" if p["actual_ready_on"] else ("due" if ready_on <= today else "growing")
        days = (datetime.date.fromisoformat(ready_on) - datetime.date.fromisoformat(today)).days
        # A historical record cannot be sold or pre-ordered, whatever its seedling count says
        available = max(0, p["seedlings"] - s - pending) if p["still_in_greenhouse"] else 0
        out.append(dict(
            p, sold=s, pending=pending, collected=collected, direct=max(0, s - collected), available=available,
            stage=stage, stage_label=PLANTING_STAGES[stage], days=days, on_pos=on_pos_now(p, today), label=planting_label(p),
            goes_on_pos=add_days(ready_on, -RELEASE_DAYS),
        ))
    return out


def put_on_pos(db, biz, f):
    """Makes the POS listing of a released batch match the batch: its name, price and what is left to sell."""
    moved = db.execute(
        "UPDATE pos_products SET name=?, department=?, batch_no=?, date_planted=?, price=?, quantity=? WHERE business_id=? AND item_code=?",
        (f["label"], f["department"], f["batch_no"], f["planted_on"], f["price"], f["available"], biz, f["pos_code"]),
    ).rowcount
    if moved == 0:
        db.execute(
            """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
               VALUES (?, ?, ?, 'Seedlings', ?, ?, ?, ?, ?)""",
            (biz, f["pos_code"], f["label"], f["department"], f["batch_no"], f["planted_on"], f["available"], f["price"]),
        )


def sync_planting(db, biz, planting_id):
    p = db.one("SELECT * FROM plantings WHERE id=? AND business_id=?", (planting_id, biz))
    if p and p["released"]:
        put_on_pos(db, biz, with_figures(db, biz, [p])[0])


def release_plantings(db, biz):
    """Lists on the POS every batch that is now within a week of being ready (or already marked ready). Safe to repeat."""
    today = today_str()
    waiting = [p for p in db.all("SELECT * FROM plantings WHERE business_id=? AND released=0", (biz,)) if on_pos_now(p, today)]
    if not waiting:
        return
    try:
        for f in with_figures(db, biz, waiting):
            db.execute("UPDATE plantings SET released=1 WHERE id=? AND business_id=?", (f["id"], biz))
            put_on_pos(db, biz, f)
        db.commit()
    except DB_ERRORS:
        db.rollback()


def up_next_summary(db, biz):
    """For the POS tile: batches still growing that are not on the POS yet, and pre-orders waiting for collection."""
    rows = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? AND released=0 AND still_in_greenhouse=1 ORDER BY ready_on, id", (biz,)))
    waiting = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS qty FROM preorders WHERE business_id=? AND status='pending'", (biz,))
    return {"batches": len(rows), "next": rows[0] if rows else None, "preorders": waiting["n"], "preorder_qty": waiting["qty"]}


def insert_planting(db, biz, d):
    """Saves a planting and gives it its POS code. d carries every field; the caller has already checked them."""
    planting_id = db.insert(
        """INSERT INTO plantings (business_id, seed_item_id, seed_code, seed_name, name, department, batch_no, source, seeds_planted, packs_used, loose_delta,
                                  seeds_per_pack, mortality_pct, seedlings, seed_unit_cost, cost_per_seedling, price, planted_on, ready_on, actual_ready_on,
                                  still_in_greenhouse, notes, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, d.get("seed_item_id"), d["seed_code"], d["seed_name"], d["name"], d["department"], d["batch_no"], d["source"], d["seeds"],
            d.get("packs", 0), d.get("loose_delta", 0), d.get("seeds_per_pack"), d["mortality"], d["seedlings"], d["seed_cost"], d["cost_per_seedling"],
            d["price"], d["planted_on"], d["ready_on"], d.get("actual_ready_on"), 1 if d.get("still_in_greenhouse", True) else 0,
            d.get("notes", ""), g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.execute("UPDATE plantings SET pos_code=? WHERE id=?", (f"{d['seed_code']}{PLANTING_MARK}{planting_id}", planting_id))
    return planting_id


def seed_code_for(name):
    return re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:20] or "PLT"


@app.get("/planting")
@need(None)
def planting_list():
    db, biz = get_db(), g.user["business_id"]
    release_plantings(db, biz)
    rows = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY ready_on DESC, id DESC LIMIT 500", (biz,)))
    show = request.args.get("show", "all")
    counts = {
        "all": len(rows), "growing": sum(1 for r in rows if r["stage"] in ("growing", "due")), "ready": sum(1 for r in rows if r["stage"] == "ready"),
        "records": sum(1 for r in rows if r["stage"] == "record"),
    }
    if show == "growing":
        rows = [r for r in rows if r["stage"] in ("growing", "due")]
    elif show == "ready":
        rows = [r for r in rows if r["stage"] == "ready"]
    elif show == "records":
        rows = [r for r in rows if r["stage"] == "record"]
    else:
        show = "all"
    return render_template("plantings.html", rows=rows, show=show, counts=counts, editable=can_plant())


@app.get("/planting/new")
@need(None)
def planting_new():
    db, biz = get_db(), g.user["business_id"]
    seeds, info = [], {}
    for i in db.all(
        "SELECT id, item_code, name, category, department, quantity, unit_cost, seeds_per_pack, loose_seeds FROM products WHERE business_id=? ORDER BY name", (biz,)
    ):
        if not is_seed_category(i["category"]):
            continue
        i["allowed"] = item_grant(db, i["id"], "transfer") is not None
        seeds.append(i)
        info[str(i["id"])] = {
            "name": i["name"], "packs": i["quantity"], "loose": i["loose_seeds"] or 0, "size": i["seeds_per_pack"] or 0, "cost": i["unit_cost"],
            "seedling": seedling_name_for(i["name"]), "department": i["department"],
        }
    return render_template(
        "planting_new.html", seed_items=seeds, info=info, chosen=request.args.get("item", ""), departments=department_names(db, biz), today=today_str(),
        release_days=RELEASE_DAYS,
    )


@app.post("/planting")
@need(None)
def planting_create():
    db, biz = get_db(), g.user["business_id"]
    raw_item = text("item_id")
    back = redirect(url_for("planting_new", item=raw_item))
    today = today_str()

    item, grant = None, None
    if raw_item:
        try:
            item_id = int(raw_item)
        except ValueError:
            abort(400)
        item = db.one("SELECT " + ITEM_COLUMNS + ", loose_seeds FROM products WHERE id=? AND business_id=?", (item_id, biz))
        if item is None or not is_seed_category(item["category"]):
            flash("Choose one of your seed items, or leave the item empty to record a planting that is not from inventory.", "error")
            return back
        grant = item_grant(db, item_id, "transfer")
        if grant is None:
            return refuse_item_action(item_id)
    elif not can_plant():
        flash("Your account cannot record a planting that is not taken from inventory. Ask an admin.", "error")
        return back

    try:
        seeds = number("seeds_planted", int)
        if seeds < 1:
            raise ValueError
    except ValueError:
        flash("Enter how many seeds are being planted (a whole number above zero).", "error")
        return back
    try:
        mortality = number("mortality_pct")
        if not 0 <= mortality < 100:
            raise ValueError
    except ValueError:
        flash("Enter the expected mortality rate as a percentage from 0 up to (not including) 100.", "error")
        return back
    planted_on = day_or_none(text("planted_on")) if text("planted_on") else today
    ready_on = day_or_none(text("ready_on"))
    if planted_on is None or ready_on is None:
        flash("Enter the planting date and the date the seedlings are expected to be ready.", "error")
        return back
    if ready_on < planted_on:
        flash("The expected ready date cannot be before the planting date.", "error")
        return back

    size = item["seeds_per_pack"] if item else None
    if item and not size:
        try:
            size = number("seeds_per_pack", int)
            if size < 1:
                raise ValueError
        except ValueError:
            flash("Say how many seeds are in one pack (a whole number above zero).", "error")
            return back
    try:
        if text("seed_cost"):
            seed_cost = number("seed_cost")
        else:
            seed_cost = item["unit_cost"] / size if item else 0.0
        if seed_cost < 0:
            raise ValueError
    except ValueError:
        flash("The cost of one seed must be a number, 0 or more.", "error")
        return back
    seedlings = seedlings_from(seeds, mortality)
    if seedlings < 1:
        flash("At that mortality rate these seeds would give less than one seedling.", "error")
        return back
    cost_per_seedling = seed_cost / (1 - mortality / 100)
    try:
        price = number("price") if text("price") else round_up_to_cent(cost_per_seedling)
        if price < 0:
            raise ValueError
    except ValueError:
        flash("The selling price per seedling must be a number, 0 or more.", "error")
        return back

    name = clean_name(text("name"), 100) or (seedling_name_for(item["name"]) if item else "")
    if not name:
        flash("Give the planting a name, for example Tomato seedlings.", "error")
        return back
    if item:
        seed_code, seed_name, dept = item["item_code"], item["name"], item["department"]
    else:
        dept = text("department") if text("department") in department_names(db, biz) else "General"
        seed_name = name
        seed_code = seed_code_for(name)

    packs, loose_delta, loose_left = 0, 0, 0
    try:
        if item:
            # Loose seeds left from an earlier opened pack are used first; only whole packs leave inventory
            loose = item["loose_seeds"] or 0
            from_loose = min(loose, seeds)
            need_seeds = seeds - from_loose
            packs = -(-need_seeds // size)
            if packs > item["quantity"]:
                db.rollback()
                available = item["quantity"] * size + loose
                flash(f"Only {available:,} seeds are in stock ({item['quantity']} pack(s) of {size:,}" + (f" and {loose:,} loose seeds" if loose else "") + "). Nothing was planted.", "error")
                return back
            loose_left = loose - from_loose + (packs * size - need_seeds)
            loose_delta = loose_left - loose
            moved = db.execute(
                "UPDATE products SET quantity = quantity - ?, loose_seeds=?, seeds_per_pack = COALESCE(seeds_per_pack, ?) WHERE id=? AND business_id=? AND quantity >= ?",
                (packs, loose_left, size, item["id"], biz, packs),
            )
            if moved.rowcount != 1:
                raise StockError(item["name"])
            if packs:
                low_stock_alert(db, item["name"], item["item_code"], item["quantity"], item["quantity"] - packs, "in inventory")

        planting_id = insert_planting(db, biz, {
            "seed_item_id": item["id"] if item else None, "seed_code": seed_code, "seed_name": seed_name, "name": name, "department": dept,
            "batch_no": text("batch_no")[:40], "source": "inventory" if item else "manual", "seeds": seeds, "packs": packs, "loose_delta": loose_delta,
            "seeds_per_pack": size, "mortality": mortality, "seedlings": seedlings, "seed_cost": seed_cost, "cost_per_seedling": cost_per_seedling,
            "price": round(price, 2), "planted_on": planted_on, "ready_on": ready_on, "notes": text("notes")[:300],
            "still_in_greenhouse": checked("still_in_greenhouse"),
        })
        use_grant(db, grant)
        db.commit()
    except StockError:
        db.rollback()
        flash("The stock changed while you were working. Nothing was planted; please try again.", "error")
        return back
    except DB_ERRORS:
        db.rollback()
        flash("The planting could not be saved. Please try again.", "error")
        return back

    release_plantings(db, biz)
    made = db.one("SELECT released, still_in_greenhouse, ready_on FROM plantings WHERE id=?", (planting_id,))
    if not made["still_in_greenhouse"]:
        where = "It is kept as a historical record only: not listed on the POS and not available to pre-order."
    elif made["released"]:
        where = "It is on the POS now."
    else:
        where = f"It will appear on the POS on {add_days(ready_on, -RELEASE_DAYS)}, a week before it is ready."
    used = f", using {packs} pack(s) from inventory" if item else ""
    spare = f" {loose_left:,} loose seeds are kept for the next planting." if item and loose_left else ""
    flash(f"Planting recorded: {seeds:,} seeds of {seed_name}{used}. About {seedlings:,} seedlings are expected by {ready_on}.{spare} {where}", "ok")
    return redirect(url_for("planting_detail", planting_id=planting_id))


def get_planting(db, planting_id):
    p = db.one("SELECT * FROM plantings WHERE id=? AND business_id=?", (planting_id, g.user["business_id"]))
    if p is None:
        abort(404)
    return p


@app.get("/planting/<int:planting_id>")
@need(None)
def planting_detail(planting_id):
    db, biz = get_db(), g.user["business_id"]
    release_plantings(db, biz)
    p = get_planting(db, planting_id)
    f = with_figures(db, biz, [p])[0]
    orders = db.all("SELECT * FROM preorders WHERE planting_id=? AND business_id=? ORDER BY id DESC", (planting_id, biz))
    return render_template("planting_detail.html", p=f, orders=orders, editable=can_plant(), release_days=RELEASE_DAYS)


@app.post("/planting/<int:planting_id>/update")
@need(None)
def planting_update(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back
    f = with_figures(db, biz, [p])[0]
    name = clean_name(text("name"), 100)
    ready_on = day_or_none(text("ready_on"))
    try:
        mortality = number("mortality_pct")
        if not 0 <= mortality < 100:
            raise ValueError
        price = number("price")
        if price < 0:
            raise ValueError
    except ValueError:
        flash("Mortality must be from 0 up to (not including) 100, and the price must be 0 or more.", "error")
        return back
    if not name or ready_on is None or ready_on < (p["planted_on"] or ""):
        flash("Give the batch a name and an expected ready date that is not before it was planted.", "error")
        return back
    still = checked("still_in_greenhouse")
    if not still and (f["sold"] + f["pending"]) > 0:
        flash(f"{f['sold'] + f['pending']:,} seedlings from this batch are already sold or pre-ordered, so it cannot be marked as a historical record. Nothing was changed.", "error")
        return back
    # While the batch is only expected, the mortality rate decides how many seedlings to expect. Once it is marked ready the real count stands.
    seedlings = p["seedlings"] if p["actual_ready_on"] else seedlings_from(p["seeds_planted"], mortality)
    if seedlings < f["sold"] + f["pending"]:
        flash(f"That would leave {seedlings:,} seedlings, but {f['sold'] + f['pending']:,} are already sold or pre-ordered. Nothing was changed.", "error")
        return back
    cost_per_seedling = p["seed_unit_cost"] / (1 - mortality / 100)
    db.execute(
        """UPDATE plantings SET name=?, batch_no=?, ready_on=?, mortality_pct=?, seedlings=?, cost_per_seedling=?, price=?, still_in_greenhouse=?, notes=?
           WHERE id=? AND business_id=?""",
        (name, text("batch_no")[:40], ready_on, mortality, seedlings, cost_per_seedling, round(price, 2), 1 if still else 0, text("notes")[:300], planting_id, biz),
    )
    if not still and p["released"]:
        # It was listed for sale; now it is only a record, so it comes off the POS (the listing itself is kept, at zero, for its sales history)
        db.execute("UPDATE pos_products SET quantity=0 WHERE business_id=? AND item_code=?", (biz, p["pos_code"]))
    release_plantings(db, biz)
    sync_planting(db, biz, planting_id)
    db.commit()
    flash("Planting record saved." if still else "Planting record saved, marked as a historical record. It is off the POS and cannot be pre-ordered.", "ok")
    return back


@app.post("/planting/<int:planting_id>/ready")
@need(None)
def planting_ready(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back
    if not p["still_in_greenhouse"]:
        flash("This is a historical record, not currently in the greenhouse, so it cannot be marked ready for sale.", "error")
        return back
    f = with_figures(db, biz, [p])[0]
    ready_day = day_or_none(text("actual_ready_on")) if text("actual_ready_on") else today_str()
    if ready_day is None:
        flash("Enter the date the seedlings were ready.", "error")
        return back
    count = p["seedlings"]
    if text("ready_count"):
        try:
            count = number("ready_count", int)
            if count < 0:
                raise ValueError
        except ValueError:
            flash("Enter the number of seedlings that are really ready as a whole number.", "error")
            return back
    if count < f["sold"] + f["pending"]:
        flash(f"{f['sold'] + f['pending']:,} seedlings are already sold or pre-ordered, so the count cannot be below that. Nothing was changed.", "error")
        return back
    db.execute("UPDATE plantings SET actual_ready_on=?, seedlings=? WHERE id=? AND business_id=?", (ready_day, count, planting_id, biz))
    release_plantings(db, biz)
    sync_planting(db, biz, planting_id)
    db.commit()
    flash(f"Marked ready on {ready_day} with {count:,} seedlings. It is on the POS.", "ok")
    return back


@app.post("/planting/<int:planting_id>/toggle-record")
@need(None)
def planting_toggle_record(planting_id):
    """Quick action from the Planting Records list: mark a batch as a historical record, or growing again - without
    opening its own edit form. Marking it a record takes it off the POS at once, the same as the edit form does.
    """
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id) if text("back") == "detail" else url_for("planting_list"))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back

    still = text("to") != "record"
    if still == bool(p["still_in_greenhouse"]):
        return back
    if not still:
        f = with_figures(db, biz, [p])[0]
        if (f["sold"] + f["pending"]) > 0:
            flash(f"{f['sold'] + f['pending']:,} seedlings from this batch are already sold or pre-ordered, so it cannot be marked as a historical record.", "error")
            return back

    db.execute("UPDATE plantings SET still_in_greenhouse=? WHERE id=? AND business_id=?", (1 if still else 0, planting_id, biz))
    if not still and p["released"]:
        db.execute("UPDATE pos_products SET quantity=0 WHERE business_id=? AND item_code=?", (biz, p["pos_code"]))
    release_plantings(db, biz)
    sync_planting(db, biz, planting_id)
    db.commit()
    flash(f"'{p['name']}' marked as still growing." if still else f"'{p['name']}' marked as a historical record. It is off the POS and cannot be pre-ordered.", "ok")
    return back


@app.post("/planting/<int:planting_id>/delete")
@need("admin")
def planting_delete(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    f = with_figures(db, biz, [p])[0]
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if f["sold"] or f["pending"] or f["collected"]:
        flash("Seedlings from this batch have been sold or pre-ordered, so the record is kept. Cancel the pre-orders first if it was a mistake.", "error")
        return back
    try:
        if p["seed_item_id"] and p["packs_used"] is not None:
            db.execute(
                "UPDATE products SET quantity = quantity + ?, loose_seeds = COALESCE(loose_seeds, 0) - ? WHERE id=? AND business_id=?",
                (p["packs_used"], p["loose_delta"] or 0, p["seed_item_id"], biz),
            )
        db.execute("DELETE FROM pos_products WHERE business_id=? AND item_code=?", (biz, p["pos_code"]))
        db.execute("DELETE FROM preorders WHERE planting_id=? AND business_id=?", (planting_id, biz))
        db.execute("DELETE FROM plantings WHERE id=? AND business_id=?", (planting_id, biz))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The record could not be deleted. Please try again.", "error")
        return back
    flash("The planting record was deleted" + (f" and {p['packs_used']} pack(s) went back into inventory." if p["packs_used"] else "."), "ok")
    return redirect(url_for("planting_list"))


def up_next_url():
    """Back to Up Next after a pre-order action, keeping the search that was on screen."""
    q = text("q")
    return (url_for("up_next", q=q) if q else url_for("up_next")) + "#preorders"


def planting_matches(p, words):
    """True when every searched word appears in the batch's name, label, seed, batch number or POS code."""
    hay = norm(" ".join(str(p.get(key) or "") for key in ("label", "name", "seed_name", "seed_code", "batch_no", "pos_code")))
    return all(word in hay for word in words)


@app.get("/planting/up-next")
@need(None)
def up_next():
    db, biz = get_db(), g.user["business_id"]
    q = " ".join(request.args.get("q", "").split())[:100]
    words = norm(q).split()
    release_plantings(db, biz)
    rows = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY ready_on, id", (biz,)))
    batches = [f for f in rows if (f["available"] > 0 or f["pending"] > 0) and planting_matches(f, words)]

    # Pre-orders match on the product (batch) as above, or on the customer who placed them
    where, params = "po.business_id=?", [biz]
    for word in words:
        like = f"%{word}%"
        where += """ AND (LOWER(p.name) LIKE ? OR LOWER(p.seed_name) LIKE ? OR LOWER(p.seed_code) LIKE ? OR LOWER(COALESCE(p.batch_no, '')) LIKE ?
                          OR LOWER(COALESCE(p.pos_code, '')) LIKE ? OR LOWER(po.customer_name) LIKE ? OR LOWER(COALESCE(po.customer_phone, '')) LIKE ?)"""
        params += [like] * 7
    orders = db.all(
        f"""SELECT po.*, p.name AS planting_name, p.batch_no AS planting_batch, p.planted_on AS planting_planted
            FROM preorders po JOIN plantings p ON p.id = po.planting_id WHERE {where}
            ORDER BY CASE WHEN po.status='pending' THEN 0 ELSE 1 END, po.id DESC LIMIT 200""",
        params,
    )
    for o in orders:
        o["label"] = planting_label({"name": o["planting_name"], "batch_no": o["planting_batch"], "planted_on": o["planting_planted"]})
        o["total"] = round(o["qty"] * o["unit_price"], 2)
        o["ask"] = active_request(db, o["id"], COLLECT_PREORDER) if o["status"] == "pending" and g.user["role"] != "admin" else None
        if o["ask"] and o["ask"]["status"] == "approved":
            o["ask"] = {**o["ask"], "until": (o["ask"]["expires_at"] or "")[11:16]}
    return render_template("up_next.html", batches=batches, orders=orders, statuses=PAYMENT_STATUSES, release_days=RELEASE_DAYS, q=q, methods=PAYMENT_METHODS)


@app.post("/planting/<int:planting_id>/preorder")
@need("sell")
def preorder_create(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id) if text("back") == "detail" else up_next_url())
    if not p["still_in_greenhouse"]:
        flash("This is a historical record, not currently in the greenhouse, so it cannot be pre-ordered.", "error")
        return back
    customer = clean_name(text("customer_name"), 100)
    if not customer:
        flash("Enter the customer's name for the pre-order.", "error")
        return back
    try:
        qty = number("qty", int)
        if qty < 1:
            raise ValueError
    except ValueError:
        flash("Enter how many seedlings the customer wants (a whole number above zero).", "error")
        return back
    f = with_figures(db, biz, [p])[0]
    if qty > f["available"]:
        flash(f"Only {f['available']:,} seedlings of this batch are still free (not sold or pre-ordered). Nothing was recorded.", "error")
        return back
    db.insert(
        """INSERT INTO preorders (business_id, planting_id, customer_name, customer_phone, qty, unit_price, status, note, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (biz, planting_id, customer, text("customer_phone")[:40], qty, p["price"], text("note")[:200], g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    sync_planting(db, biz, planting_id)
    db.commit()
    flash(f"Pre-order recorded for {customer}: {qty:,} x {f['label']}. The seedlings are held. It becomes a sale when they are collected and an admin approves.", "ok")
    return back


def get_preorder(db, po_id):
    po = db.one("SELECT * FROM preorders WHERE id=? AND business_id=?", (po_id, g.user["business_id"]))
    if po is None:
        abort(404)
    return po


@app.post("/preorders/<int:po_id>/cancel")
@need("sell")
def preorder_cancel(po_id):
    db, biz = get_db(), g.user["business_id"]
    po = get_preorder(db, po_id)
    back = redirect(up_next_url())
    if po["status"] != "pending":
        flash("That pre-order has already been collected or cancelled.", "error")
        return back
    db.execute("UPDATE preorders SET status='cancelled', cancelled_at=? WHERE id=? AND business_id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), po_id, biz))
    sync_planting(db, biz, po["planting_id"])
    db.commit()
    flash(f"Pre-order for {po['customer_name']} cancelled. The {po['qty']:,} seedlings are free again.", "ok")
    return back


@app.post("/preorders/<int:po_id>/request-approval")
@need("sell")
def preorder_request(po_id):
    db, biz = get_db(), g.user["business_id"]
    po = get_preorder(db, po_id)
    back = redirect(up_next_url())
    if g.user["role"] == "admin":
        flash("Admins approve and collect directly.", "ok")
        return back
    if po["status"] != "pending" or active_request(db, po_id, COLLECT_PREORDER):
        flash("This pre-order is not waiting for collection, or you already asked.", "error")
        return back
    p = get_planting(db, po["planting_id"])
    request_id = db.insert(
        """INSERT INTO permission_requests (business_id, user_id, username, action, item_id, item_name, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (biz, g.user["id"], g.user["username"], COLLECT_PREORDER, po_id, f"Pre-order #{po_id} for {po['customer_name']}", text("reason")[:200], now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if alert_enabled("permission"):
        notify(
            db, "permission", f"Permission request from {g.user['username']}",
            f"{g.user['username']} asks to collect a pre-order: {po['customer_name']} takes {po['qty']:,} x {planting_label(p)} ({money(po['qty'] * po['unit_price'])}). Open the app to approve or deny.",
            url_for("permission_requests") + f"#request-{request_id}", whatsapp=True,
        )
    db.commit()
    flash("Your request has been sent to the admin. You will get a popup here when they answer.", "ok")
    return back


@app.post("/preorders/<int:po_id>/collect")
@need("sell")
def preorder_collect(po_id):
    db, biz = get_db(), g.user["business_id"]
    po = get_preorder(db, po_id)
    back = redirect(up_next_url())
    if po["status"] != "pending":
        flash("That pre-order has already been collected or cancelled.", "error")
        return back

    grant = None
    if g.user["role"] == "admin":
        approved_by = g.user["username"]
    else:
        row = active_request(db, po_id, COLLECT_PREORDER)
        if not row or row["status"] != "approved":
            flash("An admin has to approve this collection first. Use the request button.", "error")
            return back
        grant, approved_by = row, row["decided_by"] or ""

    p = get_planting(db, po["planting_id"])
    total = round(po["qty"] * po["unit_price"], 2)
    try:
        amount_paid = amount_for_status(text("payment_status") or "Paid", text("amount_paid"), total)
    except ValueError as e:
        flash(f"Payment: {e}.", "error")
        return back
    method, reference = NOT_RECORDED, ""
    if amount_paid > 0.005:
        method, reference, problem = read_money_method()
        if problem:
            flash(f"Payment: {problem}", "error")
            return back

    try:
        sold_at = now().strftime("%Y-%m-%d %H:%M:%S")
        invoice_no = next_invoice_no(db, biz)
        invoice_id = db.insert(
            """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id)
               VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, invoice_no, po["customer_name"], po["customer_phone"], payment_status_for(total, amount_paid), total, amount_paid,
                round(total - amount_paid, 2), g.user["username"], sold_at, customer_for(db, biz, po["customer_name"], po["customer_phone"], sold_at),
            ),
        )
        record_money(db, biz, "in", amount_paid, method, "sale", reference, po["customer_name"], f"Invoice {invoice_no} (pre-order collected)", "invoice", invoice_id, sold_at)
        # The seedlings were taken out of stock when the pre-order was recorded, so only the sale itself is entered here
        db.execute(
            """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice_id, biz, p["pos_code"], planting_label(p), p["department"], p["batch_no"], po["qty"], po["unit_price"], total),
        )
        db.execute(
            "UPDATE preorders SET status='collected', collected_by=?, approved_by=?, collected_at=?, invoice_id=? WHERE id=? AND business_id=?",
            (g.user["username"], approved_by, sold_at, invoice_id, po_id, biz),
        )
        use_grant(db, grant)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The collection could not be saved, so nothing was changed. Please try again.", "error")
        return back
    flash(f"Collected by {po['customer_name']} and recorded as a sale (approved by {approved_by}).", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


PLANTING_COLUMNS = {
    "item_code": ("item code", "code", "seed code", "sku"),
    "name": ("name", "seedling name", "seed name", "item name", "item", "seed", "plant"),
    "seeds": ("seeds planted", "seeds", "number of seeds", "quantity", "qty"),
    "planted_on": ("planted on", "date planted", "planting date", "date"),
    "ready_on": ("ready on", "expected ready", "expected ready date", "ready date", "ready", "date ready"),
    "actual_ready_on": ("actual ready date", "actual ready", "ready on actual", "marked ready"),
    "mortality": ("mortality", "expected mortality", "mortality rate", "expected mortality rate", "mortality %"),
    "price": ("selling price", "price", "seedling price"),
    "seed_cost": ("seed cost", "cost per seed", "seed unit cost", "unit cost"),
    "department": ("department", "dept"),
    "batch_no": ("batch no", "batch number", "batch"),
    "still_in_greenhouse": ("still in greenhouse", "in greenhouse", "still present", "currently in greenhouse", "in the greenhouse"),
    "notes": ("notes", "note", "comments"),
}
IMPORT_TEMPLATES["planting"] = (
    ["Item Code", "Name", "Seeds Planted", "Planted On", "Ready On", "Mortality", "Seed Cost", "Selling Price", "Department", "Batch No", "Actual Ready Date", "Still In Greenhouse", "Notes"],
    ["TOM-001", "Tomato seedlings", 2000, "2026-10-01", "2026-11-05", 15, "", "", "Greenhouse", "B-22", "", "Yes", "Tray block A"],
)


def in_record_only_window(day, start, end):
    """True when day falls inside an admin-set 'treat as a record only' date range (either end may be blank)."""
    if not start and not end:
        return False
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


@app.post("/import/planting")
@need("admin")
def import_planting():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(PLANTING_COLUMNS, ("seeds", "ready_on", "mortality"))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))

    # A row whose Still In Greenhouse column is blank falls back to this date filter: inside it, it is saved as a
    # historical record only; outside it (or with no filter set), it is treated as still growing, as before.
    record_from = record_to = ""
    if text("record_only_from"):
        record_from = day_or_none(text("record_only_from"))
        if record_from is None:
            flash("The 'treat as a record only from' date is not a date.", "error")
            return redirect(url_for("import_data"))
    if text("record_only_to"):
        record_to = day_or_none(text("record_only_to"))
        if record_to is None:
            flash("The 'treat as a record only to' date is not a date.", "error")
            return redirect(url_for("import_data"))
    if record_from and record_to and record_from > record_to:
        record_from, record_to = record_to, record_from

    departments = set(department_names(db, biz))
    fallback = "Greenhouse" if "Greenhouse" in departments else "General"
    seen = {(norm(r["name"]), r["planted_on"], r["batch_no"] or "", r["seeds_planted"]) for r in db.all("SELECT name, planted_on, batch_no, seeds_planted FROM plantings WHERE business_id=?", (biz,))}
    today = today_str()
    errors, plans, new_departments = [], [], []

    for rec in records:
        problems = []
        name = clean_name(rec["name"], 100)
        code = clean_name(rec["item_code"], 30)
        if not name and not code:
            problems.append("give a Name or an Item Code")
        seeds = 0
        try:
            seeds = parse_whole(rec["seeds"])
            if seeds < 1:
                raise ValueError
        except ValueError:
            problems.append(f"seeds planted '{rec['seeds']}' is not a whole number above zero")
        mortality = 0.0
        try:
            mortality = parse_amount(rec["mortality"])
            if not 0 <= mortality < 100:
                raise ValueError
        except ValueError:
            problems.append(f"mortality '{rec['mortality']}' must be a percentage from 0 up to (not including) 100")
        planted_on = day_or_none(rec["planted_on"]) if rec["planted_on"] else today
        ready_on = day_or_none(rec["ready_on"])
        actual = day_or_none(rec["actual_ready_on"]) if rec["actual_ready_on"] else None
        if planted_on is None:
            problems.append(f"planted on '{rec['planted_on']}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")
        if ready_on is None:
            problems.append(f"ready on '{rec['ready_on']}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")
        elif planted_on and ready_on < planted_on:
            problems.append("ready on is before planted on")
        if rec["actual_ready_on"] and actual is None:
            problems.append(f"actual ready date '{rec['actual_ready_on']}' is not a date")
        seed_cost = price = None
        for field, label in (("seed_cost", "seed cost"), ("price", "selling price")):
            if rec[field]:
                try:
                    value = parse_amount(rec[field])
                    if value < 0:
                        raise ValueError
                except ValueError:
                    problems.append(f"{label} '{rec[field]}' is not a valid amount")
                    continue
                if field == "seed_cost":
                    seed_cost = value
                else:
                    price = value

        # Whether this batch is really still in the greenhouse, or just a historical record. An explicit Yes/No
        # in the file always wins; a blank cell falls back to the record-only date filter set on the Import page.
        still = None
        word = rec["still_in_greenhouse"].strip().lower()
        if word:
            if word in YES_WORDS:
                still = True
            elif word in NO_WORDS:
                still = False
            else:
                problems.append(f"'Still In Greenhouse' value '{rec['still_in_greenhouse']}' must be Yes or No")

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue
        if still is None:
            still = not in_record_only_window(planted_on, record_from, record_to)
        label = name or code
        key = (norm(label), planted_on, rec["batch_no"] or "", seeds)
        if key in seen:
            errors.append(f"Row {rec['_row']}: '{label}' with {seeds:,} seeds planted on {planted_on} is already recorded (was this file imported before?)")
            continue
        seen.add(key)
        dept = rec["department"] or fallback
        if dept not in departments:
            departments.add(dept)
            new_departments.append(dept)
        seedlings = seedlings_from(seeds, mortality)
        if seedlings < 1:
            errors.append(f"Row {rec['_row']}: at that mortality rate these seeds would give less than one seedling")
            continue
        seed_cost = seed_cost if seed_cost is not None else 0.0
        cost_per = seed_cost / (1 - mortality / 100)
        plans.append({
            "seed_code": code or seed_code_for(label), "seed_name": label, "name": label, "department": dept, "batch_no": rec["batch_no"][:40], "source": "import",
            "seeds": seeds, "mortality": mortality, "seedlings": seedlings, "seed_cost": seed_cost, "cost_per_seedling": cost_per,
            "price": round(price if price is not None else round_up_to_cent(cost_per), 2), "planted_on": planted_on, "ready_on": ready_on,
            "actual_ready_on": actual, "still_in_greenhouse": still, "notes": rec["notes"][:300],
        })

    if errors:
        return import_errors("planting records", errors)
    try:
        for dept in new_departments:
            db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, dept))
        for d in plans:
            insert_planting(db, biz, d)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))
    release_plantings(db, biz)
    record_only = sum(1 for d in plans if not d["still_in_greenhouse"])
    still_growing = len(plans) - record_only
    note = f": {still_growing} still in the greenhouse, {record_only} as a historical record only" if record_only else ""
    flash(f"Imported {len(plans)} planting record(s){note}. Inventory was not changed. Batches within a week of being ready are already on the POS.", "ok")
    return redirect(url_for("planting_list"))


# --- STOCK ADJUSTMENTS (write-offs from the POS, inventory quantity corrections) ---
@app.get("/stock-adjustments")
@need("admin")
def stock_adjustments():
    db, biz = get_db(), g.user["business_id"]
    kind = request.args.get("kind", "") if request.args.get("kind") in ("pos_removal", "inventory_edit") else ""
    sql = """SELECT id, kind, item_code, item_name, department, quantity_before, quantity_after, delta, reason, note, created_by, created_at
             FROM stock_adjustments WHERE business_id=?"""
    params = [biz]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    rows = db.all(sql + " ORDER BY id DESC LIMIT 500", params)
    return render_template("stock_adjustments.html", rows=rows, kind=kind)


# --- ACCOUNTS PAGE (ADMIN) ---
# Money in and money out over a chosen period: how much came in and went out, through which channel (cash, bank, mobile money),
# where it came from and what it was spent on, how it moved day by day or month by month, and every entry behind those totals.
ACCOUNT_PARAMS = ("period", "from", "to", "direction", "channel", "category", "q")
ACCOUNT_LIST_LIMIT = 500


def account_filters(db=None, biz=None):
    """What the Accounts page shows: a date range for everything, and the narrowing of its list of entries. "Not in any balance
    yet" lists only what came after the balances were counted (what came before is part of the counts)."""
    period, start, end = invoice_period(default="this_month")
    pick = lambda key, allowed: request.args.get(key, "") if request.args.get(key, "") in allowed else ""  # noqa: E731
    since = None
    if db is not None and request.args.get("channel") == "Unassigned":
        counts = balance_counts(db, biz)
        since = min(boundary_of(c) for c in counts.values()) if counts else None
    return {
        "since": since,
        "period": period, "start": start, "end": end,
        "direction": pick("direction", ("in", "out")), "channel": pick("channel", list(dict(CHANNELS)) + ["Unassigned"]),
        "category": pick("category", CATEGORY_LABELS), "q": " ".join(request.args.get("q", "").split())[:100],
    }


def days_where(start, end, column="occurred_at"):
    """SQL limiting a timestamp column to whole days; either day may be None (no limit on that side)."""
    where, params = "", []
    if start:
        where += f" AND {column} >= ?"
        params.append(start.isoformat())
    if end:
        where += f" AND {column} < ?"
        params.append((end + datetime.timedelta(days=1)).isoformat())
    return where, params


def entries_where(biz, f):
    """The WHERE clause shared by the list of entries, its totals and its CSV export."""
    where, params = days_where(f["start"], f["end"], "c.occurred_at")
    if f["direction"]:
        where += " AND c.direction=?"
        params.append(f["direction"])
    if f.get("since"):
        clause, values = after(f["since"], "c.")
        where, params = where + " AND " + clause, params + values
    if f["channel"]:
        methods = UNASSIGNED_METHODS if f["channel"] == "Unassigned" else dict(CHANNELS)[f["channel"]]
        where += f" AND c.method IN ({', '.join('?' for _ in methods)})"
        params += list(methods)
    if f["category"]:
        where += " AND c.category=?"
        params.append(f["category"])
    for word in f["q"].lower().split():
        like = f"%{word}%"
        where += """ AND (LOWER(COALESCE(c.party, '')) LIKE ? OR LOWER(COALESCE(c.description, '')) LIKE ?
                          OR LOWER(COALESCE(c.reference, '')) LIKE ? OR LOWER(COALESCE(c.recorded_by, '')) LIKE ?)"""
        params += [like] * 4
    return "c.business_id=?" + where, [biz] + params


def entry_link(row):
    """Where an entry came from, for a link back to it."""
    source, ref = row["source"], row["source_id"]
    if source == "invoice" and ref:
        return url_for("invoice", invoice_id=ref)
    if source == "purchase" and ref:
        return url_for("procurement", purchase_id=ref)
    if source == "supplier_payment" and row.get("supplier_id"):
        return url_for("supplier_statement", supplier_id=row["supplier_id"])
    if source == "customer" and ref:
        return url_for("customer_statement", customer_id=ref)
    if source == "payslip" and ref:
        return url_for("payslips", id=ref)
    return None


def account_entries(db, biz, f, limit):
    where, params = entries_where(biz, f)
    rows = db.all(
        f"""SELECT c.id, c.direction, c.amount, c.method, c.reference, c.category, c.party, c.description, c.source, c.source_id,
                   c.occurred_at, c.recorded_by, sp.supplier_id
            FROM cashbook c LEFT JOIN supplier_payments sp ON c.source = 'supplier_payment' AND sp.id = c.source_id
            WHERE {where} ORDER BY c.occurred_at DESC, c.id DESC LIMIT ?""",
        params + [limit],
    )
    for r in rows:
        r["label"] = CATEGORY_LABELS.get(r["category"], r["category"])
        r["channel"] = CHANNEL_OF.get(r["method"], "Other")
        r["link"] = entry_link(r)
    return rows


def account_summary(db, biz, start, end):
    """Totals for the period: in, out, by channel and method, and by where money came from and what it went on."""
    where, params = days_where(start, end)
    groups = db.all(
        f"""SELECT direction, method, category, COALESCE(SUM(amount), 0) AS total FROM cashbook
            WHERE business_id=?{where} GROUP BY direction, method, category""",
        [biz] + params,
    )
    flows = {}
    for r in groups:
        flows.setdefault(r["method"], {"in": 0.0, "out": 0.0})[r["direction"]] += r["total"]

    channels, known = [], set(CHANNEL_OF)
    for name, methods in CHANNELS:
        # A method the app no longer knows (from an old record) is shown with Other
        members = list(methods) + ([m for m in flows if m not in known] if name == "Other" else [])
        rows = []
        for m in members:
            fin, fout = flows.get(m, {}).get("in", 0.0), flows.get(m, {}).get("out", 0.0)
            if abs(fin) > 0.005 or abs(fout) > 0.005:
                rows.append({"name": m, "in": fin, "out": fout, "net": fin - fout})
        if rows:
            channels.append({
                "name": name, "methods": rows if len(rows) > 1 else [],
                "in": sum(r["in"] for r in rows), "out": sum(r["out"] for r in rows), "net": sum(r["net"] for r in rows),
            })

    def ranked(direction, categories):
        totals = {}
        for r in groups:
            if r["direction"] == direction:
                totals[r["category"]] = totals.get(r["category"], 0.0) + r["total"]
        whole = sum(v for v in totals.values() if v > 0) or 1.0
        known_keys = [key for key, _ in categories]
        rows = [
            {"key": key, "label": CATEGORY_LABELS.get(key, key), "total": value, "pct": max(0.0, round(value / whole * 100, 1))}
            for key, value in totals.items() if abs(value) > 0.005
        ]
        return sorted(rows, key=lambda r: (-r["total"], known_keys.index(r["key"]) if r["key"] in known_keys else 99))

    money_in = sum(r["total"] for r in groups if r["direction"] == "in")
    money_out = sum(r["total"] for r in groups if r["direction"] == "out")
    return {
        "in": money_in, "out": money_out, "net": money_in - money_out, "channels": channels,
        "sources": ranked("in", MONEY_IN), "purposes": ranked("out", MONEY_OUT),
    }


def account_flow(db, biz, start, end):
    """Money in and out per day (a period up to two months) or per month, with a running balance. Once the balances are counted,
    that is what is held in them (worked out from each count, so it is right before the count as well as after); until then,
    everything ever recorded in less everything out."""
    by_day = bool(start and end and (end - start).days <= 62)
    width = 10 if by_day else 7
    where, params = days_where(start, end)
    rows = db.all(
        f"""SELECT SUBSTR(occurred_at, 1, {width}) AS bucket, direction, method, COALESCE(SUM(amount), 0) AS total FROM cashbook
            WHERE business_id=?{where} GROUP BY bucket, direction, method ORDER BY bucket""",
        [biz] + params,
    )
    counts = balance_counts(db, biz)
    if counts:
        tracked = {m for key in counts for m in dict(CHANNELS)[key]}
        first = start.isoformat() if start else (min((r["bucket"] for r in rows), default=today_str()) + "-01")[:10]
        opening, lines = flow_lines(rows, by_day, sum(balance_at(db, biz, key, count, first) for key, count in counts.items()), tracked)
        return {"by_day": by_day, "opening": opening, "lines": lines, "has_start": bool(start), "counted": True}
    opening = 0.0
    if start:
        before = db.one(
            """SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE 0 END), 0) AS cin,
                      COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS cout
               FROM cashbook WHERE business_id=? AND occurred_at < ?""",
            (biz, start.isoformat()),
        )
        opening = before["cin"] - before["cout"]
    opening, lines = flow_lines(rows, by_day, opening, None)
    return {"by_day": by_day, "opening": opening, "lines": lines, "has_start": bool(start), "counted": False}


def flow_lines(rows, by_day, opening, tracked):
    """The cash flow table's lines from (bucket, direction, method, total) rows. The balance moves with the entries through the
    tracked methods only (every method when tracked is None)."""
    buckets = {}
    for r in rows:
        b = buckets.setdefault(r["bucket"], {"in": 0.0, "out": 0.0, "moved": 0.0})
        b[r["direction"]] += r["total"]
        if tracked is None or r["method"] in tracked:
            b["moved"] += r["total"] if r["direction"] == "in" else -r["total"]
    lines, balance = [], opening
    for bucket in sorted(buckets):
        b = buckets[bucket]
        balance += b["moved"]
        if by_day:
            label = datetime.date.fromisoformat(bucket).strftime("%a %d %b %Y")
        else:
            label = datetime.date(int(bucket[:4]), int(bucket[5:7]), 1).strftime("%B %Y")
        lines.append({"label": label, "in": b["in"], "out": b["out"], "net": b["in"] - b["out"], "balance": balance})
    return opening, lines


# --- Balances: what is really held in cash, at the bank and in each mobile money wallet ---
# The owner counts each balance at a moment; from then on every entry paid through it moves it (cash for Cash, the bank for bank
# transfers, cheques and cards, each wallet for its own mobile money). Entries dated before the moment are already part of the count,
# so earlier records never change a balance. Counting again later starts afresh from the new count, and keeps what the app expected
# at that moment, so a shortfall or surplus shows.
# Times are kept to the second, so a count also keeps the last entry that existed when it was saved: an entry dated in that same
# second is after the count only if it was recorded after it. Every entry is then on exactly one side of every count.
def balance_counts(db, biz):
    """The latest count of each balance: {channel: row}."""
    rows = db.all(
        """SELECT account, amount, as_of, last_id, expected, set_by, set_at FROM money_balances
           WHERE id IN (SELECT MAX(id) FROM money_balances WHERE business_id=? GROUP BY account)""",
        (biz,),
    )
    return {r["account"]: r for r in rows if r["account"] in dict(BALANCE_ACCOUNTS)}


def boundary_of(count):
    return (count["as_of"], count["last_id"] or 0)


def after(point, prefix=""):
    """SQL for the entries after a point in time: (timestamp, last entry already there). A plain moment is (timestamp, 0)."""
    return f"({prefix}occurred_at > ? OR ({prefix}occurred_at = ? AND {prefix}id > ?))", [point[0], point[0], point[1]]


def moved_through(db, biz, channel, low=None, high=None):
    """Money in less money out through one balance, for the entries after the point low and not after the point high."""
    methods = dict(CHANNELS)[channel]
    where, params = "", []
    if low:
        clause, values = after(low)
        where, params = where + " AND " + clause, params + values
    if high:
        clause, values = after(high)
        where, params = where + " AND NOT " + clause, params + values
    return db.one(
        f"""SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END), 0) AS net FROM cashbook
            WHERE business_id=? AND method IN ({', '.join('?' for _ in methods)}){where}""",
        [biz, *methods, *params],
    )["net"]


def balance_at(db, biz, channel, count, point=None):
    """One balance at a point in time (now, when None), worked forwards or backwards from its count. A point is
    (timestamp, last entry) or just a timestamp (the start of that moment)."""
    base = boundary_of(count)
    if point is not None and not isinstance(point, tuple):
        point = (point, 0)
    if point is None or point >= base:
        return count["amount"] + moved_through(db, biz, channel, base, point)
    return count["amount"] - moved_through(db, biz, channel, point, base)


def balances_now(db, biz):
    """Each balance as it stands now, their total, and the entries since the counts that belong to none of them."""
    counts = balance_counts(db, biz)
    if not counts:
        return None
    accounts = [
        {"key": key, "label": label, "amount": round(balance_at(db, biz, key, counts[key]), 2), "count": counts[key]}
        for key, label in BALANCE_ACCOUNTS if key in counts
    ]
    earliest = min(boundary_of(c) for c in counts.values())
    since = earliest[0]
    clause, values = after(earliest)
    loose = db.one(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE 0 END), 0) AS cin,
                   COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS cout
            FROM cashbook WHERE business_id=? AND method IN ({', '.join('?' for _ in UNASSIGNED_METHODS)}) AND {clause}""",
        [biz, *UNASSIGNED_METHODS, *values],
    )
    return {
        "accounts": accounts, "total": round(sum(a["amount"] for a in accounts), 2), "since": since,
        "counted": max(c["as_of"] for c in counts.values()), "loose": loose,
    }


def balance_history(db, biz):
    """Every time the balances were counted, newest first: [{"as_of", "set_by", "rows": [(label, counted, expected)]}]."""
    labels = dict(BALANCE_ACCOUNTS)
    history = []
    for r in db.all("SELECT account, amount, expected, as_of, set_by, set_at FROM money_balances WHERE business_id=? ORDER BY id DESC LIMIT 80", (biz,)):
        label = labels.get(r["account"], r["account"])
        # One count saves every balance together; a balance seen again is the count before it
        if not history or history[-1]["set_at"] != r["set_at"] or history[-1]["as_of"] != r["as_of"] or label in [x[0] for x in history[-1]["rows"]]:
            history.append({"as_of": r["as_of"], "set_at": r["set_at"], "set_by": r["set_by"], "rows": []})
        history[-1]["rows"].append((label, r["amount"], r["expected"]))
    return history[:12]


@app.get("/accounts")
@need("admin")
def accounts():
    db, biz = get_db(), g.user["business_id"]
    f = account_filters(db, biz)
    where, params = entries_where(biz, f)
    listed = db.one(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN c.direction='in' THEN c.amount ELSE 0 END), 0) AS cin,
                   COALESCE(SUM(CASE WHEN c.direction='out' THEN c.amount ELSE 0 END), 0) AS cout
            FROM cashbook c WHERE {where}""",
        params,
    )
    owed_to_us = db.one(
        "SELECT COALESCE(SUM(balance_due), 0) AS owed FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled'", (biz,)
    )["owed"]
    we_owe = db.one("SELECT COALESCE(SUM(total_cost - COALESCE(amount_paid, total_cost)), 0) AS owed FROM procurements WHERE business_id=?", (biz,))["owed"]
    args = {key: request.args.get(key) for key in ACCOUNT_PARAMS if request.args.get(key)}
    period_args = {key: value for key, value in args.items() if key in ("period", "from", "to")}
    balances = balances_now(db, biz)
    return render_template(
        "accounts.html", f=f, periods=INVOICE_PERIODS, summary=account_summary(db, biz, f["start"], f["end"]),
        flow=account_flow(db, biz, f["start"], f["end"]), entries=account_entries(db, biz, f, ACCOUNT_LIST_LIMIT), listed=listed,
        owed_to_us=owed_to_us, we_owe=we_owe, channels=[name for name, _ in CHANNELS], money_in=MONEY_IN, money_out=MONEY_OUT,
        manual_in=[(k, CATEGORY_LABELS[k]) for k in MANUAL_IN], manual_out=[(k, CATEGORY_LABELS[k]) for k in MANUAL_OUT],
        methods=PAYMENT_METHODS, csv_url=url_for("accounts_csv", **args), return_to=url_for("accounts", **args), today=today_str(),
        list_filtered=bool(f["direction"] or f["channel"] or f["category"] or f["q"]),
        balances=balances, balance_accounts=BALANCE_ACCOUNTS, balance_fields=BALANCE_FIELDS, history=balance_history(db, biz),
        assign_url=url_for("accounts", period="all", channel="Unassigned") + "#transactions" if balances else None,
        moment=now().strftime("%Y-%m-%dT%H:%M"), unassigned=UNASSIGNED_METHODS, method_names=METHOD_NAMES,
        # The list of entries showing just one channel or category of the period (from the totals above it), or all of it again
        narrow=lambda key, value: url_for("accounts", **{**period_args, key: value}) + "#transactions",
        clear_url=url_for("accounts", **period_args) + "#transactions",
    )


@app.get("/accounts.csv")
@need("admin")
def accounts_csv():
    db, biz = get_db(), g.user["business_id"]
    rows = account_entries(db, biz, account_filters(db, biz), 100000)
    return csv_response(
        "accounts.csv",
        ["Date", "In / Out", "Category", "From / To", "Details", "Method", "Channel", "Reference", "Amount", "Recorded by"],
        [
            (r["occurred_at"], "In" if r["direction"] == "in" else "Out", r["label"], r["party"], r["description"], r["method"], r["channel"],
             r["reference"], r["amount"], r["recorded_by"])
            for r in rows
        ],
    )


def accounts_back():
    """Back to the Accounts page as it was, from the address the form was sent with (never anywhere else)."""
    back = text("back")
    return redirect(back if back.startswith("/accounts") and not back.startswith("//") else url_for("accounts"))


@app.post("/accounts/record")
@need("admin")
def record_account_entry():
    """Money in or out that no other page records: rent, fuel, owner's money put in, a loan, bank charges..."""
    db, biz = get_db(), g.user["business_id"]
    back = accounts_back()
    direction = text("direction")
    allowed = MANUAL_IN if direction == "in" else MANUAL_OUT if direction == "out" else ()
    category = text("category")
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    method = method_from_text(text("payment_method"))
    day = day_or_none(text("occurred_on")) if text("occurred_on") else None
    if category not in allowed:
        flash("Choose what the money was for from the list.", "error")
    elif amount <= 0:
        flash("Enter the amount, above zero.", "error")
    elif method is None:
        flash("Choose how the money was paid: " + ", ".join(METHOD_NAMES) + ".", "error")
    elif day is None or day > today_str():
        flash("Enter the date the money moved (today or earlier).", "error")
    else:
        record_money(
            db, biz, direction, amount, method, category, " ".join(text("payment_reference").split())[:60], clean_name(text("party"), 100),
            " ".join(text("description").split())[:200], "manual", None, stamp_for_day(day),
        )
        db.commit()
        flash(f"Recorded {money(amount)} {'in' if direction == 'in' else 'out'}: {CATEGORY_LABELS[category]}, by {method.lower()}.", "ok")
    return back


@app.post("/accounts/balances")
@need("admin")
def set_balances():
    """What is held in cash, at the bank and in each mobile money wallet at a moment (now, unless another time is given).
    From then on every payment in or out through each of them moves it."""
    db, biz = get_db(), g.user["business_id"]
    back = accounts_back()
    raw = text("as_of")
    current = now().replace(tzinfo=None)
    try:
        moment = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M") if raw else current
    except ValueError:
        flash("Enter the date and time the balances were counted.", "error")
        return back
    if moment > current + datetime.timedelta(minutes=1):
        flash("The balances cannot be counted at a time that has not come yet.", "error")
        return back
    # The form gives minutes only: counted "now" means this very second, so nothing recorded earlier this minute is counted twice
    if moment.replace(second=0, microsecond=0) == current.replace(second=0, microsecond=0):
        moment = current
    amounts = {}
    for key, label in BALANCE_ACCOUNTS:
        try:
            amounts[key] = round(number(BALANCE_FIELDS[key]), 2)
        except ValueError:
            flash(f"Enter what is held in {label} (0 if there is nothing). Nothing was changed.", "error")
            return back
    as_of = moment.strftime("%Y-%m-%d %H:%M:%S")
    stamp = current.strftime("%Y-%m-%d %H:%M:%S")
    counts = balance_counts(db, biz)
    # Everything already recorded and dated up to the count is part of it
    last_id = db.one("SELECT COALESCE(MAX(id), 0) AS n FROM cashbook WHERE business_id=?", (biz,))["n"]
    differences = []
    try:
        for key, label in BALANCE_ACCOUNTS:
            expected = round(balance_at(db, biz, key, counts[key], (as_of, last_id)), 2) if key in counts else None
            db.execute(
                "INSERT INTO money_balances (business_id, account, amount, as_of, last_id, expected, set_by, set_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (biz, key, amounts[key], as_of, last_id, expected, g.user["username"], stamp),
            )
            if expected is not None and abs(amounts[key] - expected) > 0.005:
                gap = amounts[key] - expected
                differences.append(f"{label} is {money(abs(gap))} {'more' if gap > 0 else 'less'} than the app expected ({money(expected)})")
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The balances could not be saved. Please try again.", "error")
        return back
    flash(
        f"Balances counted at {moment.strftime('%d %b %Y %H:%M')}: {money(sum(amounts.values()))} in all. From now on every payment in or out "
        "through cash, the bank, Airtel Money or MTN Money moves its balance." + (" " + "; ".join(differences) + "." if differences else ""),
        "ok",
    )
    return back


@app.post("/accounts/<int:entry_id>/assign")
@need("admin")
def assign_account_entry(entry_id):
    """Says how an entry was paid when it did not (an older record, or a correction from an older page), so its balance includes it."""
    db, biz = get_db(), g.user["business_id"]
    back = accounts_back()
    method = method_from_text(text("payment_method"))
    entry = db.one("SELECT id, method, source, source_id FROM cashbook WHERE id=? AND business_id=?", (entry_id, biz))
    if entry is None:
        abort(404)
    if method not in METHOD_NAMES:
        flash("Choose how the money was paid: " + ", ".join(METHOD_NAMES) + ".", "error")
        return back
    if entry["method"] not in UNASSIGNED_METHODS:
        flash("That entry already says how it was paid. Change it where it came from.", "error")
        return back
    db.execute("UPDATE cashbook SET method=? WHERE id=?", (method, entry_id))
    # A supplier payment keeps its method on the supplier's statement too
    if entry["source"] == "supplier_payment" and entry["source_id"]:
        db.execute("UPDATE supplier_payments SET method=? WHERE id=? AND business_id=? AND method=?", (method, entry["source_id"], biz, entry["method"]))
    db.commit()
    flash(f"Entry assigned to {method}" + (f"; the {CHANNEL_OF[method]} balance now includes it." if CHANNEL_OF.get(method) in dict(BALANCE_ACCOUNTS) else "."), "ok")
    return back


@app.post("/accounts/<int:entry_id>/delete")
@need("admin")
def delete_account_entry(entry_id):
    """Only entries made on the Accounts page can be removed here; the rest follow the sale, payment or payslip they belong to."""
    db, biz = get_db(), g.user["business_id"]
    removed = db.execute("DELETE FROM cashbook WHERE id=? AND business_id=? AND source='manual'", (entry_id, biz)).rowcount
    db.commit()
    flash("Entry removed from Accounts." if removed else "That entry belongs to a sale, payment, purchase or payslip; change it there.", "ok" if removed else "error")
    return accounts_back()


def history_stamp(value):
    """A stored date or date-and-time as a full timestamp, or None when there is none."""
    value = (value or "").strip()
    if len(value) >= 19:
        return value[:19]
    return f"{value[:10]} 00:00:00" if len(value) >= 10 else None


def cashbook_history(db, biz):
    """The money recorded before Accounts existed, as cashbook entries. How it was paid was not kept for sales or for purchases
    without a supplier account, so those say 'Not recorded'; supplier payments kept theirs."""
    entries = []
    fallback = now().strftime("%Y-%m-%d %H:%M:%S")

    def add(direction, amount, method, category, when, party, description, source, source_id, reference=""):
        if abs(round(amount or 0.0, 2)) >= 0.005:
            entries.append((direction, round(amount, 2), method, reference or "", category, party or "", (description or "")[:200], source, source_id, when or fallback))

    def changes(kind, extra=""):
        found = {}
        for row in db.all(f"SELECT ref_id, old_paid, new_paid, note, changed_at FROM payment_log WHERE business_id=? AND kind=?{extra} ORDER BY id", (biz, kind)):
            found.setdefault(row["ref_id"], []).append(row)
        return found

    # Sales: what was paid at the time of sale, then every later change to it
    sale_changes = changes("sale")
    for inv in db.all("SELECT id, invoice_no, customer_name, amount_paid, created_at FROM invoices WHERE business_id=? AND kind='SALE'", (biz,)):
        logged = sale_changes.get(inv["id"], [])
        when = history_stamp(inv["created_at"])
        add("in", logged[0]["old_paid"] if logged else inv["amount_paid"], NOT_RECORDED, "sale", when, inv["customer_name"], f"Invoice {inv['invoice_no']}", "invoice", inv["id"])
        for c in logged:
            delta = c["new_paid"] - c["old_paid"]
            add(
                "in", delta, NOT_RECORDED if delta > 0 else CORRECTION, "customer_payment", history_stamp(c["changed_at"]) or when, inv["customer_name"],
                f"Invoice {inv['invoice_no']}" + (f": {c['note']}" if c["note"] else ""), "invoice", inv["id"],
            )

    # Payments to supplier accounts were kept with their method and reference
    for sp in db.all(
        """SELECT sp.id, sp.amount, sp.method, sp.reference, sp.paid_on, sp.note, sp.recorded_at, s.name
           FROM supplier_payments sp LEFT JOIN suppliers s ON s.id = sp.supplier_id WHERE sp.business_id=?""",
        (biz,),
    ):
        add("out", sp["amount"], sp["method"] or NOT_RECORDED, "supplier", history_stamp(sp["paid_on"]) or history_stamp(sp["recorded_at"]),
            sp["name"], sp["note"] or "Payment to supplier", "supplier_payment", sp["id"], sp["reference"])

    # Purchases: for a supplier account, whatever was paid outside the supplier payments above (from before they were kept);
    # with no supplier account, what was paid when it was logged and every later change
    allocated = {
        r["procurement_id"]: r["total"]
        for r in db.all("SELECT procurement_id, COALESCE(SUM(amount), 0) AS total FROM supplier_payment_allocations WHERE business_id=? GROUP BY procurement_id", (biz,))
    }
    purchase_changes = changes("purchase", " AND payment_id IS NULL")
    for pr in db.all(
        "SELECT id, name, quantity, supplier_name, supplier_id, purchase_date, COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE business_id=?",
        (biz,),
    ):
        when, what = history_stamp(pr["purchase_date"]), f"{pr['name']} x {pr['quantity'] or 0:,}"
        if pr["supplier_id"]:
            loose = (pr["paid"] or 0.0) - allocated.get(pr["id"], 0.0)
            if loose > 0.005:
                add("out", loose, NOT_RECORDED, "supplier", when, pr["supplier_name"], what, "purchase", pr["id"])
            continue
        logged = purchase_changes.get(pr["id"], [])
        add("out", logged[0]["old_paid"] if logged else pr["paid"], NOT_RECORDED, "purchase", when, pr["supplier_name"], what, "purchase", pr["id"])
        for c in logged:
            delta = c["new_paid"] - c["old_paid"]
            add("out", delta, NOT_RECORDED if delta > 0 else CORRECTION, "purchase", history_stamp(c["changed_at"]) or when, pr["supplier_name"], what, "purchase", pr["id"])

    # Payslips: the net pay, and NAPSA/NHIMA (both shares)
    for s in db.all(
        "SELECT id, employee_name, pay_month, net_payable, napsa_amt, nhima_amt, employer_napsa, employer_nhima, created_at FROM payroll WHERE business_id=?",
        (biz,),
    ):
        when = history_stamp(s["created_at"])
        add("out", s["net_payable"], NOT_RECORDED, "salaries", when, s["employee_name"], f"Pay for {s['pay_month']}", "payslip", s["id"])
        add("out", (s["napsa_amt"] or 0) + (s["nhima_amt"] or 0) + (s["employer_napsa"] or 0) + (s["employer_nhima"] or 0), NOT_RECORDED, "statutory", when,
            s["employee_name"], f"NAPSA & NHIMA for {s['pay_month']}", "payslip", s["id"])
    return entries


def backfill_cashbook():
    """Copies each business's earlier money records into the cashbook, once. Safe to repeat and to run on two servers at once:
    a business is claimed before its records are copied, and if anything fails nothing is kept, so it is tried again next start."""
    db = Database()
    try:
        for row in db.all("SELECT id FROM businesses WHERE cashbook_ready IS NULL"):
            biz = row["id"]
            if db.execute("UPDATE businesses SET cashbook_ready=1 WHERE id=? AND cashbook_ready IS NULL", (biz,)).rowcount != 1:
                db.rollback()
                continue
            stamp = now().strftime("%Y-%m-%d %H:%M:%S")
            entries = cashbook_history(db, biz)
            # Many rows per statement, so a long history is copied in a few round trips
            for i in range(0, len(entries), 60):
                chunk = entries[i:i + 60]
                db.execute(
                    """INSERT INTO cashbook (business_id, direction, amount, method, reference, category, party, description, source, source_id,
                                             occurred_at, recorded_by, recorded_at) VALUES """
                    + ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk),
                    [value for e in chunk for value in (biz, *e, "Copied from earlier records", stamp)],
                )
            db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()


backfill_cashbook()


# --- BACKUPS (ADMIN) ---
# A backup is the business's records as Excel books - a master workbook holding every book, and each book on its own - and every
# table as a CSV file. backup.js saves them into a folder the admin chose once on their computer, replacing the files from the last
# backup (the file names never change). A browser that cannot write into a folder downloads everything as one ZIP instead.
# The Sales, Purchase, Planting and Inventory books come first everywhere, so they are always saved first.
BACKUP_HOUR_DEFAULT = 17
BACKUP_HOURS = [(-1, "Off: never remind me")] + [(h, f"{h:02d}:00") for h in range(12, 24)]
# Every table that holds this business's records, for the CSV copy. Users are written separately, without their passwords.
BACKUP_TABLES = (
    "invoices", "invoice_items", "payment_log", "deleted_invoices", "customers", "customer_aliases", "customer_distinct",
    "procurements", "suppliers", "supplier_aliases", "supplier_distinct", "supplier_payments", "supplier_payment_allocations",
    "products", "pos_products", "categories", "departments", "stock_adjustments", "plantings", "preorders", "seedling_batches",
    "cashbook", "money_balances", "employees", "payroll", "hr_settings", "notifications", "permission_requests",
)
TEXT, MONEY, WHOLE, NUMBER = "text", "money", "whole", "number"


def joined_amounts(raw):
    """A payslip's bonuses or deductions, stored as JSON, as one readable line."""
    try:
        items = json.loads(raw or "{}")
    except ValueError:
        return ""
    return "; ".join(f"{name} {float(value):,.2f}" for name, value in items.items())


def sales_book(db, biz):
    paid_by = {}
    for r in db.all("SELECT source_id, method FROM cashbook WHERE business_id=? AND source='invoice' AND direction='in' AND amount > 0 ORDER BY id", (biz,)):
        methods = paid_by.setdefault(r["source_id"], [])
        if r["method"] not in (NOT_RECORDED, CORRECTION) and r["method"] not in methods:
            methods.append(r["method"])
    invoices = db.all(
        """SELECT id, invoice_no, created_at, kind, customer_name, customer_phone, total, amount_paid, balance_due, payment_status, created_by,
                  cancelled_at, cancelled_by, cancel_reason
           FROM invoices WHERE business_id=? ORDER BY created_at, id""",
        (biz,),
    )
    lines = db.all(
        """SELECT i.invoice_no, i.created_at, i.customer_name, i.payment_status, ii.item_code, ii.item_name, ii.department, ii.batch_no, ii.qty,
                  ii.unit_price, ii.line_total
           FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE ii.business_id=? ORDER BY i.created_at, i.id, ii.id""",
        (biz,),
    )
    deleted = db.all(
        """SELECT invoice_no, created_at, customer_name, total, amount_paid, reason, deleted_by, approved_by, deleted_at
           FROM deleted_invoices WHERE business_id=? ORDER BY id""",
        (biz,),
    )
    return [
        ("Sales invoices",
         [("Invoice No", TEXT), ("Date / time", TEXT), ("Type", TEXT), ("Customer", TEXT), ("Phone", TEXT), ("Total", MONEY), ("Paid", MONEY),
          ("Balance", MONEY), ("Status", TEXT), ("Paid by", TEXT), ("Recorded by", TEXT), ("Cancelled", TEXT)],
         [(i["invoice_no"], i["created_at"], "Farm use" if i["kind"] == "FARM_USE" else "Sale", i["customer_name"], i["customer_phone"], i["total"],
           i["amount_paid"], i["balance_due"], i["payment_status"], ", ".join(paid_by.get(i["id"], [])), i["created_by"],
           f"{i['cancelled_at']} by {i['cancelled_by']}: {i['cancel_reason']}" if i["cancelled_at"] else "") for i in invoices]),
        ("Sales lines",
         [("Invoice No", TEXT), ("Date / time", TEXT), ("Customer", TEXT), ("Item code", TEXT), ("Item", TEXT), ("Department", TEXT), ("Batch", TEXT),
          ("Qty", WHOLE), ("Unit price", MONEY), ("Line total", MONEY), ("Invoice status", TEXT)],
         [(r["invoice_no"], r["created_at"], r["customer_name"], r["item_code"], r["item_name"], r["department"], r["batch_no"], r["qty"],
           r["unit_price"], r["line_total"], r["payment_status"]) for r in lines]),
        ("Deleted invoices",
         [("Invoice No", TEXT), ("Sold on", TEXT), ("Customer", TEXT), ("Total", MONEY), ("Paid", MONEY), ("Reason", TEXT), ("Deleted by", TEXT),
          ("Approved by", TEXT), ("Deleted on", TEXT)],
         [(r["invoice_no"], r["created_at"], r["customer_name"], r["total"], r["amount_paid"], r["reason"], r["deleted_by"], r["approved_by"],
           r["deleted_at"]) for r in deleted]),
    ]


def purchase_book(db, biz):
    purchases = db.all(
        """SELECT purchase_date, supplier_name, invoice_no, item_code, name, category, department, COALESCE(in_inventory, 1) AS in_inventory, quantity,
                  unit_cost, total_cost, COALESCE(amount_paid, total_cost) AS paid, expiry_date
           FROM procurements WHERE business_id=? ORDER BY purchase_date, id""",
        (biz,),
    )
    payments = db.all(
        """SELECT sp.paid_on, s.name AS supplier, sp.method, sp.reference, sp.amount, sp.note, sp.recorded_by, sp.recorded_at
           FROM supplier_payments sp LEFT JOIN suppliers s ON s.id = sp.supplier_id WHERE sp.business_id=? ORDER BY sp.paid_on, sp.id""",
        (biz,),
    )
    return [
        ("Purchases",
         [("Date", TEXT), ("Supplier", TEXT), ("Supplier invoice", TEXT), ("Item code", TEXT), ("Item", TEXT), ("Category", TEXT), ("Department", TEXT),
          ("In inventory", TEXT), ("Qty", WHOLE), ("Unit cost", MONEY), ("Total cost", MONEY), ("Paid", MONEY), ("Balance", MONEY), ("Status", TEXT),
          ("Expiry", TEXT)],
         [(r["purchase_date"], r["supplier_name"], r["invoice_no"], r["item_code"], r["name"], r["category"], r["department"],
           "Yes" if r["in_inventory"] else "No", r["quantity"], r["unit_cost"], r["total_cost"], r["paid"],
           round((r["total_cost"] or 0) - (r["paid"] or 0), 2), payment_status_for(r["total_cost"] or 0, r["paid"] or 0), r["expiry_date"])
          for r in purchases]),
        ("Supplier payments",
         [("Date paid", TEXT), ("Supplier", TEXT), ("Method", TEXT), ("Reference", TEXT), ("Amount", MONEY), ("Note", TEXT), ("Recorded by", TEXT),
          ("Recorded at", TEXT)],
         [(r["paid_on"], r["supplier"], r["method"], r["reference"], r["amount"], r["note"], r["recorded_by"], r["recorded_at"]) for r in payments]),
    ]


def planting_book(db, biz):
    plantings = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY planted_on, id", (biz,)))
    orders = db.all(
        """SELECT po.created_at, po.customer_name, po.customer_phone, p.name AS planting, po.qty, po.unit_price, po.status, po.collected_at,
                  po.approved_by, i.invoice_no
           FROM preorders po JOIN plantings p ON p.id = po.planting_id LEFT JOIN invoices i ON i.id = po.invoice_id
           WHERE po.business_id=? ORDER BY po.id""",
        (biz,),
    )
    sowings = db.all(
        """SELECT created_at, seed_code, seed_name, seedling_name, packs, seeds_sown, germination_pct, seedlings, cost_per_seedling, selling_price, created_by
           FROM seedling_batches WHERE business_id=? ORDER BY id""",
        (biz,),
    )
    return [
        ("Planting records",
         [("Batch", TEXT), ("Batch No", TEXT), ("Seed", TEXT), ("Seed code", TEXT), ("Department", TEXT), ("Planted on", TEXT), ("Ready on", TEXT),
          ("Actually ready", TEXT), ("Seeds planted", WHOLE), ("Packs used", WHOLE), ("Mortality %", NUMBER), ("Seedlings", WHOLE),
          ("Cost per seedling", NUMBER), ("Price", MONEY), ("Sold", WHOLE), ("Pre-ordered", WHOLE), ("Available", WHOLE), ("In greenhouse", TEXT),
          ("POS code", TEXT), ("Notes", TEXT), ("Recorded by", TEXT)],
         [(p["name"], p["batch_no"], p["seed_name"], p["seed_code"], p["department"], p["planted_on"], p["ready_on"], p["actual_ready_on"],
           p["seeds_planted"], p["packs_used"], p["mortality_pct"], p["seedlings"], p["cost_per_seedling"], p["price"], p["sold"], p["pending"],
           p["available"], "Yes" if p["still_in_greenhouse"] else "No (record)", p["pos_code"], p["notes"], p["created_by"]) for p in plantings]),
        ("Pre-orders",
         [("Placed", TEXT), ("Customer", TEXT), ("Phone", TEXT), ("Batch", TEXT), ("Qty", WHOLE), ("Unit price", MONEY), ("Total", MONEY),
          ("Status", TEXT), ("Collected", TEXT), ("Approved by", TEXT), ("Invoice No", TEXT)],
         [(r["created_at"], r["customer_name"], r["customer_phone"], r["planting"], r["qty"], r["unit_price"], round(r["qty"] * r["unit_price"], 2),
           r["status"], r["collected_at"], r["approved_by"], r["invoice_no"]) for r in orders]),
        ("Seed sowings",
         [("Sown", TEXT), ("Seed code", TEXT), ("Seed", TEXT), ("Seedlings listed as", TEXT), ("Packs", WHOLE), ("Seeds sown", WHOLE),
          ("Germination %", NUMBER), ("Seedlings", WHOLE), ("Cost per seedling", NUMBER), ("Selling price", MONEY), ("By", TEXT)],
         [(r["created_at"], r["seed_code"], r["seed_name"], r["seedling_name"], r["packs"], r["seeds_sown"], r["germination_pct"], r["seedlings"],
           r["cost_per_seedling"], r["selling_price"], r["created_by"]) for r in sowings]),
    ]


def inventory_book(db, biz):
    items = db.all(
        """SELECT item_code, name, category, department, unit, quantity, unit_cost, price, seeds_per_pack, supplier_name, invoice_no, purchase_date,
                  batch_no, date_planted, expiry_date
           FROM products WHERE business_id=? ORDER BY category, name""",
        (biz,),
    )
    listed = db.all(
        "SELECT item_code, name, category, department, date_planted, batch_no, quantity, price FROM pos_products WHERE business_id=? ORDER BY name",
        (biz,),
    )
    changes = db.all(
        """SELECT created_at, kind, item_code, item_name, department, quantity_before, quantity_after, delta, reason, note, created_by
           FROM stock_adjustments WHERE business_id=? ORDER BY id""",
        (biz,),
    )
    return [
        ("Inventory",
         [("Item code", TEXT), ("Item", TEXT), ("Category", TEXT), ("Department", TEXT), ("Unit", TEXT), ("In stock", WHOLE), ("Unit cost", MONEY),
          ("Stock value", MONEY), ("Selling price", MONEY), ("Seeds per pack", WHOLE), ("Supplier", TEXT), ("Supplier invoice", TEXT),
          ("Purchase date", TEXT), ("Batch", TEXT), ("Date planted", TEXT), ("Expiry", TEXT)],
         [(r["item_code"], r["name"], r["category"], r["department"], r["unit"], r["quantity"], r["unit_cost"],
           round((r["quantity"] or 0) * (r["unit_cost"] or 0), 2), r["price"], r["seeds_per_pack"], r["supplier_name"], r["invoice_no"],
           r["purchase_date"], r["batch_no"], r["date_planted"], r["expiry_date"]) for r in items]),
        ("POS listings",
         [("Code", TEXT), ("Name", TEXT), ("Category", TEXT), ("Department", TEXT), ("Planted", TEXT), ("Batch", TEXT), ("In stock", WHOLE),
          ("Price", MONEY), ("Value at price", MONEY)],
         [(r["item_code"], r["name"], r["category"], r["department"], r["date_planted"], r["batch_no"], r["quantity"], r["price"],
           round((r["quantity"] or 0) * (r["price"] or 0), 2)) for r in listed]),
        ("Stock adjustments",
         [("When", TEXT), ("Where", TEXT), ("Item code", TEXT), ("Item", TEXT), ("Department", TEXT), ("Before", WHOLE), ("After", WHOLE),
          ("Change", WHOLE), ("Reason", TEXT), ("Note", TEXT), ("By", TEXT)],
         [(r["created_at"], "POS" if r["kind"] == "pos_removal" else "Inventory", r["item_code"], r["item_name"], r["department"],
           r["quantity_before"], r["quantity_after"], r["delta"], r["reason"], r["note"], r["created_by"]) for r in changes]),
    ]


def accounts_book(db, biz):
    rows = db.all(
        """SELECT occurred_at, direction, category, party, description, method, reference, amount, recorded_by
           FROM cashbook WHERE business_id=? ORDER BY occurred_at, id""",
        (biz,),
    )
    return [(
        "Accounts entries",
        [("Date / time", TEXT), ("In / Out", TEXT), ("Category", TEXT), ("From / to", TEXT), ("Details", TEXT), ("Method", TEXT), ("Channel", TEXT),
         ("Reference", TEXT), ("Money in", MONEY), ("Money out", MONEY), ("Recorded by", TEXT)],
        [(r["occurred_at"], "In" if r["direction"] == "in" else "Out", CATEGORY_LABELS.get(r["category"], r["category"]), r["party"], r["description"],
          r["method"], CHANNEL_OF.get(r["method"], "Other"), r["reference"], r["amount"] if r["direction"] == "in" else None,
          r["amount"] if r["direction"] == "out" else None, r["recorded_by"]) for r in rows],
    ), (
        "Balance counts",
        [("Counted at", TEXT), ("Balance", TEXT), ("Counted", MONEY), ("The app expected", MONEY), ("Difference", MONEY), ("By", TEXT)],
        [(c["as_of"], dict(BALANCE_ACCOUNTS).get(c["account"], c["account"]), c["amount"], c["expected"],
          None if c["expected"] is None else round(c["amount"] - c["expected"], 2), c["set_by"])
         for c in db.all("SELECT account, amount, expected, as_of, set_by FROM money_balances WHERE business_id=? ORDER BY id", (biz,))],
    )]


def customers_book(db, biz):
    rows = db.all(
        """SELECT c.name, c.phone, c.created_at, COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.amount_paid), 0) AS paid,
                  COALESCE(SUM(i.balance_due), 0) AS owed
           FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind = 'SALE' AND i.payment_status <> 'Cancelled'
           WHERE c.business_id=? GROUP BY c.id, c.name, c.phone, c.created_at ORDER BY c.name""",
        (biz,),
    )
    return [(
        "Customers",
        [("Customer", TEXT), ("Phone", TEXT), ("Customer since", TEXT), ("Invoices", WHOLE), ("Total bought", MONEY), ("Paid", MONEY), ("Owes", MONEY)],
        [(r["name"], r["phone"], (r["created_at"] or "")[:10], r["invoices"], r["total"], r["paid"], r["owed"]) for r in rows],
    )]


def suppliers_book(db, biz):
    rows = db.all(
        """SELECT s.name, s.phone, s.created_at, COUNT(pr.id) AS purchases, COALESCE(SUM(pr.total_cost), 0) AS total,
                  COALESCE(SUM(COALESCE(pr.amount_paid, pr.total_cost)), 0) AS paid
           FROM suppliers s LEFT JOIN procurements pr ON pr.supplier_id = s.id
           WHERE s.business_id=? GROUP BY s.id, s.name, s.phone, s.created_at ORDER BY s.name""",
        (biz,),
    )
    return [(
        "Suppliers",
        [("Supplier", TEXT), ("Phone", TEXT), ("Supplier since", TEXT), ("Purchases", WHOLE), ("Total bought", MONEY), ("Paid", MONEY), ("We owe", MONEY)],
        [(r["name"], r["phone"], (r["created_at"] or "")[:10], r["purchases"], r["total"], r["paid"], round(r["total"] - r["paid"], 2)) for r in rows],
    )]


def payroll_book(db, biz):
    slips = db.all(
        """SELECT pay_month, employee_name, basic_salary, expected_days, worked_days, calculated_basic, bonuses_json, deductions_json, napsa_amt, nhima_amt,
                  employer_napsa, employer_nhima, net_payable, created_at
           FROM payroll WHERE business_id=? ORDER BY pay_month, employee_name""",
        (biz,),
    )
    staff = db.all(
        "SELECT full_name, nrc_number, employment_type, basic_salary, start_date, expected_days_week, address FROM employees WHERE business_id=? ORDER BY full_name",
        (biz,),
    )
    return [
        ("Payslips",
         [("Pay month", TEXT), ("Employee", TEXT), ("Basic salary", MONEY), ("Expected days", NUMBER), ("Days worked", NUMBER), ("Basic for days", MONEY),
          ("Bonuses", TEXT), ("Deductions", TEXT), ("NAPSA (employee)", MONEY), ("NHIMA (employee)", MONEY), ("NAPSA (employer)", MONEY),
          ("NHIMA (employer)", MONEY), ("Net pay", MONEY), ("Made", TEXT)],
         [(r["pay_month"], r["employee_name"], r["basic_salary"], r["expected_days"], r["worked_days"], r["calculated_basic"],
           joined_amounts(r["bonuses_json"]), joined_amounts(r["deductions_json"]), r["napsa_amt"], r["nhima_amt"], r["employer_napsa"],
           r["employer_nhima"], r["net_payable"], r["created_at"]) for r in slips]),
        ("Employees",
         [("Employee", TEXT), ("NRC", TEXT), ("Type", TEXT), ("Basic salary", MONEY), ("Started", TEXT), ("Days a week", WHOLE), ("Address", TEXT)],
         [(r["full_name"], r["nrc_number"], r["employment_type"], r["basic_salary"], r["start_date"], r["expected_days_week"], r["address"]) for r in staff]),
    ]


# (key, file name, what builds it). The first four are the priority books.
BACKUP_BOOKS = (
    ("sales", "Sales Book", sales_book),
    ("purchases", "Purchase Book", purchase_book),
    ("plantings", "Planting Records", planting_book),
    ("inventory", "Inventory Records", inventory_book),
    ("accounts", "Accounts", accounts_book),
    ("customers", "Customers", customers_book),
    ("suppliers", "Suppliers", suppliers_book),
    ("payroll", "Payroll", payroll_book),
)
PRIORITY_BOOKS = 4


def workbook_bytes(sheets, contents=None):
    """An Excel workbook of sheets [(title, [(column, kind)], rows)], with a contents sheet first when given. Text is always written
    as text (never run as a formula); amounts are real numbers, so they can be added up in Excel."""
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False})
    head = book.add_format({"bold": True, "bg_color": "#DCE6F2", "border": 1, "text_wrap": True, "valign": "top"})
    kinds = {
        MONEY: book.add_format({"num_format": "#,##0.00"}), WHOLE: book.add_format({"num_format": "#,##0"}),
        NUMBER: book.add_format({"num_format": "#,##0.####"}),
    }
    if contents:
        sheet = book.add_worksheet("Contents")
        title = book.add_format({"bold": True, "font_size": 14})
        sheet.write_string(0, 0, contents["title"], title)
        row = 2
        for label, value in contents["facts"]:
            sheet.write_string(row, 0, label, head)
            sheet.write_string(row, 1, str(value))
            row += 1
        row += 1
        for c, name in enumerate(("Book", "Sheet", "Rows")):
            sheet.write_string(row, c, name, head)
        for book_title, sheet_title, count in contents["sheets"]:
            row += 1
            sheet.write_string(row, 0, book_title)
            sheet.write_string(row, 1, sheet_title)
            sheet.write_number(row, 2, count, kinds[WHOLE])
        sheet.set_column(0, 0, 26)
        sheet.set_column(1, 1, 34)
        sheet.set_column(2, 2, 10)
    for title, columns, rows in sheets:
        sheet = book.add_worksheet(title[:31])
        widths = [len(name) for name, _ in columns]
        for c, (name, _) in enumerate(columns):
            sheet.write_string(0, c, name, head)
        for r, row in enumerate(rows, start=1):
            for c, (value, (_, kind)) in enumerate(zip(row, columns)):
                if value is None or value == "":
                    continue
                if kind != TEXT and isinstance(value, numbers.Number) and not isinstance(value, bool):
                    sheet.write_number(r, c, float(value), kinds[kind])
                    widths[c] = max(widths[c], len(f"{float(value):,.2f}"))
                else:
                    value = str(value)
                    sheet.write_string(r, c, value)
                    widths[c] = max(widths[c], min(len(value), 60))
        for c, width in enumerate(widths):
            sheet.set_column(c, c, min(max(width + 2, 8), 62))
        sheet.freeze_panes(1, 0)
        if rows:
            sheet.autofilter(0, 0, len(rows), len(columns) - 1)
    book.close()
    return buffer.getvalue()


def backup_facts():
    return [
        ("Business", g.user["business_name"]),
        ("Backed up", now().strftime("%d %b %Y %H:%M")),
        ("By", g.user["username"]),
    ]


def master_workbook(db, biz):
    sheets, listing = [], []
    for _, title, build in BACKUP_BOOKS:
        for sheet in build(db, biz):
            sheets.append(sheet)
            listing.append((title, sheet[0], len(sheet[2])))
    contents = {"title": f"{g.user['business_name']}: backup of all records", "facts": backup_facts(), "sheets": listing}
    return workbook_bytes(sheets, contents)


def book_workbook(db, biz, key):
    title, build = next(((t, b) for k, t, b in BACKUP_BOOKS if k == key), (None, None))
    if build is None:
        abort(404)
    sheets = build(db, biz)
    contents = {"title": f"{g.user['business_name']}: {title}", "facts": backup_facts(), "sheets": [(title, s[0], len(s[2])) for s in sheets]}
    return title, workbook_bytes(sheets, contents)


def table_csv(db, biz, table):
    """One table of this business's records as CSV. The table name comes from the list above, never from the request as such."""
    if table == "business":
        sql, params = "SELECT * FROM businesses WHERE id=?", (biz,)
    elif table == "users":
        sql, params = "SELECT id, username, role, can_sell, can_procure, can_transfer, can_delete FROM users WHERE business_id=? ORDER BY id", (biz,)
    elif table in BACKUP_TABLES:
        sql, params = f"SELECT * FROM {table} WHERE business_id=? ORDER BY 1", (biz,)
    else:
        abort(404)
    cursor = db.execute(sql + " LIMIT 0", params)
    columns = [d[0] for d in cursor.description]
    cursor.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in db.all(sql, params):
        writer.writerow([csv_safe(row[c]) for c in columns])
    return buffer.getvalue().encode("utf-8-sig")


def backup_tables():
    return list(BACKUP_TABLES) + ["business", "users"]


def backup_files():
    """Every file a backup holds, in the order they are saved: the priority books, the master workbook, the other books, then the data."""
    books = [{"path": f"Books/{title}.xlsx", "url": url_for("backup_book", key=key)} for key, title, _ in BACKUP_BOOKS]
    return (
        books[:PRIORITY_BOOKS] + [{"path": "Master Backup.xlsx", "url": url_for("backup_master")}] + books[PRIORITY_BOOKS:]
        + [{"path": f"Data (CSV)/{table}.csv", "url": url_for("backup_table", table=table)} for table in backup_tables()]
    )


def xlsx_response(name, data):
    return Response(
        data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


def note_backup(db, biz):
    db.execute("UPDATE businesses SET last_backup_at=?, last_backup_by=? WHERE id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), g.user["username"], biz))
    db.commit()


@app.get("/backup")
@need("admin")
def backup_page():
    hour = g.user["backup_hour"]
    return render_template(
        "backup.html", books=[(key, title) for key, title, _ in BACKUP_BOOKS], priority=PRIORITY_BOOKS, tables=backup_tables(),
        hours=BACKUP_HOURS, hour=BACKUP_HOUR_DEFAULT if hour is None else hour, file_count=len(backup_files()),
    )


@app.get("/backup/files.json")
@need("admin")
def backup_manifest():
    return Response(json.dumps(backup_files()), mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.get("/backup/master.xlsx")
@need("admin")
def backup_master():
    db, biz = get_db(), g.user["business_id"]
    return xlsx_response("Master Backup.xlsx", master_workbook(db, biz))


@app.get("/backup/books/<key>.xlsx")
@need("admin")
def backup_book(key):
    db, biz = get_db(), g.user["business_id"]
    title, data = book_workbook(db, biz, key)
    return xlsx_response(f"{title}.xlsx", data)


@app.get("/backup/data/<table>.csv")
@need("admin")
def backup_table(table):
    db, biz = get_db(), g.user["business_id"]
    return Response(
        table_csv(db, biz, table), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}.csv"', "Cache-Control": "no-store"},
    )


@app.get("/backup/all.zip")
@need("admin")
def backup_zip():
    """Everything in one ZIP, laid out like the backup folder, for a browser that cannot save into a folder."""
    db, biz = get_db(), g.user["business_id"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for key, title, _ in BACKUP_BOOKS[:PRIORITY_BOOKS]:
            archive.writestr(f"Books/{title}.xlsx", book_workbook(db, biz, key)[1])
        archive.writestr("Master Backup.xlsx", master_workbook(db, biz))
        for key, title, _ in BACKUP_BOOKS[PRIORITY_BOOKS:]:
            archive.writestr(f"Books/{title}.xlsx", book_workbook(db, biz, key)[1])
        for table in backup_tables():
            archive.writestr(f"Data (CSV)/{table}.csv", table_csv(db, biz, table))
        archive.writestr("Backup info.txt", "\r\n".join(f"{label}: {value}" for label, value in backup_facts()) + "\r\n")
    note_backup(db, biz)
    stamp = now().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="Backup {stamp}.zip"', "Cache-Control": "no-store"},
    )


@app.post("/backup/done")
@need("admin")
def backup_done():
    """backup.js reports a backup it finished saving into the folder, so the end-of-day reminder knows today is done."""
    db, biz = get_db(), g.user["business_id"]
    note_backup(db, biz)
    row = db.one("SELECT last_backup_at FROM businesses WHERE id=?", (biz,))
    return Response(json.dumps({"ok": True, "at": row["last_backup_at"]}), mimetype="application/json")


@app.post("/backup/settings")
@need("admin")
def backup_settings():
    db, biz = get_db(), g.user["business_id"]
    hour = request.form.get("backup_hour", type=int)
    if hour not in dict(BACKUP_HOURS):
        flash("Choose a time for the reminder from the list.", "error")
    else:
        db.execute("UPDATE businesses SET backup_hour=? WHERE id=?", (hour, biz))
        db.commit()
        flash("The end-of-day backup reminder is off." if hour < 0 else f"The backup reminder will appear from {hour:02d}:00 each day until that day's backup is made.", "ok")
    return redirect(url_for("backup_page"))


# --- RESET THE APP (ADMIN) ---
# A reset wipes the records the app has logged for this business. It keeps what makes the app usable
# and was set up rather than logged: user accounts (with their permissions and appearance), the business
# profile and its settings, departments, and the HR statutory rules. Each group is (what an admin reads
# on the confirmation page, the tables behind it); the first table of a group is the one that is counted.
RESET_GROUPS = (
    ("Inventory items", ("products",)),
    ("Items listed for sale in the POS", ("pos_products",)),
    ("Purchases", ("procurements",)),
    ("Invoices and farm-use notes", ("invoices", "invoice_items")),
    ("Payment changes", ("payment_log",)),
    ("Deleted invoice records", ("deleted_invoices",)),
    ("Customer accounts", ("customers", "customer_aliases", "customer_distinct")),
    ("Supplier accounts", ("suppliers", "supplier_aliases", "supplier_distinct")),
    ("Supplier payments", ("supplier_payments", "supplier_payment_allocations")),
    ("Accounts: money in and out, and the balances counted", ("cashbook", "money_balances")),
    ("Employees", ("employees",)),
    ("Payslips", ("payroll",)),
    ("Sowing batches", ("seedling_batches",)),
    ("Planting records", ("plantings",)),
    ("Pre-orders", ("preorders",)),
    ("Stock adjustments and removals", ("stock_adjustments",)),
    ("Alerts", ("notifications",)),
    ("Permission requests", ("permission_requests",)),
)
RESET_KEPT = (
    "User accounts: usernames, passwords, roles, permissions and each user's appearance.",
    "Business details and settings: name, contact, currency, low-stock threshold, alert switches and WhatsApp number.",
    "Departments and inventory categories.",
    "HR settings: statutory rates and non-working days.",
)
RESET_PHRASE = "RESET ALL DATA"

# A narrower version of the same reset, scoped to inventory alone: items, their POS listings, and the greenhouse
# records that live on top of them. Purchase history (Procurements) and sales history are kept, since those are
# financial records rather than stock, and deleting an item elsewhere already keeps its own history for the same reason.
CLEAR_INVENTORY_GROUPS = (
    ("Inventory items", ("products",)),
    ("Items listed for sale in the POS", ("pos_products",)),
    ("Sowing batches", ("seedling_batches",)),
    ("Planting records", ("plantings",)),
    ("Pre-orders", ("preorders",)),
    ("Stock adjustments and removals", ("stock_adjustments",)),
)
CLEAR_INVENTORY_KEPT = (
    "Procurements (purchase history) and supplier statements.",
    "Sales history, invoices and customer statements.",
    "Categories and departments.",
)
CLEAR_INVENTORY_PHRASE = "CLEAR INVENTORY"


def reset_counts(db, biz):
    # Table names come from the constants above, never from the request
    return [(label, db.one(f"SELECT COUNT(*) AS n FROM {tables[0]} WHERE business_id=?", (biz,))["n"]) for label, tables in RESET_GROUPS]


@app.get("/settings/reset")
@need("admin")
def reset_page():
    counts = reset_counts(get_db(), g.user["business_id"])
    return render_template("reset.html", counts=counts, total=sum(n for _, n in counts), phrase=RESET_PHRASE, kept=RESET_KEPT)


@app.post("/settings/reset")
@need("admin")
def reset_app():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("reset_page"))

    # Two deliberate steps: type the phrase, and prove it is really the admin by giving their own password
    if request.form.get("phrase", "").strip() != RESET_PHRASE:
        flash(f"Type {RESET_PHRASE} exactly to confirm. Nothing was deleted.", "error")
        return back
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
        flash("That password is not correct. Nothing was deleted.", "error")
        return back

    # Everything is deleted in one transaction and only for this business: if any step fails, nothing is lost
    deleted = 0
    try:
        for label, tables in RESET_GROUPS:
            for position, table in enumerate(tables):
                removed = db.execute(f"DELETE FROM {table} WHERE business_id=?", (biz,)).rowcount
                if position == 0:
                    deleted += removed
        # The only trace left is who did it, shown to the admins
        notify(
            db, "reset", "The app was reset",
            f"{g.user['username']} reset the app and deleted {deleted:,} records. Accounts and settings were kept.",
            url_for("notifications"),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The reset could not be completed, so nothing was deleted. Please try again.", "error")
        return back

    flash(f"The app has been reset: {deleted:,} records were deleted. User accounts and settings were kept.", "ok")
    return redirect(url_for("settings"))


def clear_inventory_counts(db, biz):
    return [(label, db.one(f"SELECT COUNT(*) AS n FROM {tables[0]} WHERE business_id=?", (biz,))["n"]) for label, tables in CLEAR_INVENTORY_GROUPS]


@app.get("/inventory/clear")
@need("admin")
def clear_inventory_page():
    counts = clear_inventory_counts(get_db(), g.user["business_id"])
    return render_template("clear_inventory.html", counts=counts, total=sum(n for _, n in counts), phrase=CLEAR_INVENTORY_PHRASE, kept=CLEAR_INVENTORY_KEPT)


@app.post("/inventory/clear")
@need("admin")
def clear_inventory():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("clear_inventory_page"))

    if request.form.get("phrase", "").strip() != CLEAR_INVENTORY_PHRASE:
        flash(f"Type {CLEAR_INVENTORY_PHRASE} exactly to confirm. Nothing was deleted.", "error")
        return back
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
        flash("That password is not correct. Nothing was deleted.", "error")
        return back

    deleted = 0
    try:
        for label, tables in CLEAR_INVENTORY_GROUPS:
            for position, table in enumerate(tables):
                removed = db.execute(f"DELETE FROM {table} WHERE business_id=?", (biz,)).rowcount
                if position == 0:
                    deleted += removed
        notify(
            db, "reset", "Inventory was cleared",
            f"{g.user['username']} cleared inventory and deleted {deleted:,} record(s). Purchase and sales history were kept.",
            url_for("notifications"),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("Inventory could not be cleared, so nothing was deleted. Please try again.", "error")
        return back

    flash(f"Inventory was cleared: {deleted:,} record(s) were deleted. Purchase and sales history were kept.", "ok")
    return redirect(url_for("inventory"))


# --- SETTINGS (ADMIN) ---
@app.route("/settings", methods=["GET", "POST"])
@need("admin")
def settings():
    db, biz = get_db(), g.user["business_id"]
    if request.method == "POST":
        name = text("name")
        try:
            threshold = number("low_stock_threshold", int)
        except ValueError:
            threshold = -1
        if not name or threshold < 0:
            flash("Business name is required and the low-stock threshold must be a whole number.", "error")
        else:
            # The currency keeps its trailing space ("ZMW ") so amounts read "ZMW 10.00"
            currency = request.form.get("currency", "")[:8]
            db.execute(
                "UPDATE businesses SET name=?, subtitle=?, contact=?, currency=?, low_stock_threshold=? WHERE id=?",
                (name, text("subtitle"), text("contact"), currency, threshold, biz),
            )
            db.commit()
            flash("Business details saved.", "ok")
        return redirect(url_for("settings"))

    departments = db.all("SELECT id, name FROM departments WHERE business_id=? ORDER BY name", (biz,))
    return render_template("settings.html", departments=departments, whatsapp=whatsapp_service())


@app.post("/settings/departments")
@need("admin")
def add_department():
    db, biz = get_db(), g.user["business_id"]
    name = text("name")
    if not name:
        flash("Enter a department name.", "error")
    elif name in department_names(db, biz):
        flash("That department already exists.", "error")
    else:
        db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, name))
        db.commit()
        flash(f"Department '{name}' created.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/departments/<int:dept_id>/delete")
@need("admin")
def delete_department(dept_id):
    db, biz = get_db(), g.user["business_id"]
    dept = db.one("SELECT name FROM departments WHERE id=? AND business_id=?", (dept_id, biz))
    if dept is None or dept["name"] == "General":
        flash("That department cannot be deleted.", "error")
        return redirect(url_for("settings"))

    db.execute("DELETE FROM departments WHERE id=? AND business_id=?", (dept_id, biz))
    db.execute("UPDATE products SET department='General' WHERE business_id=? AND department=?", (biz, dept["name"]))
    db.execute("UPDATE pos_products SET department='General' WHERE business_id=? AND department=?", (biz, dept["name"]))
    db.commit()
    flash(f"Department '{dept['name']}' deleted. Its items moved to 'General'.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/departments/<int:dept_id>/rename")
@need("admin")
def rename_department(dept_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, name FROM departments WHERE id=? AND business_id=?", (dept_id, biz))
    if row is None:
        abort(404)
    new = clean_name(text("name"))
    clash = next((n for n in department_names(db, biz) if norm(n) == norm(new) and n != row["name"]), None)
    if not new:
        flash("Type the new name.", "error")
    elif row["name"] == "General":
        flash("The General department cannot be renamed.", "error")
    elif clash:
        flash(f"There is already a department called '{clash}'.", "error")
    else:
        # Items, purchases, POS listings, past sales and plantings all carry the department by name, so all of them follow
        try:
            db.execute("UPDATE departments SET name=? WHERE id=? AND business_id=?", (new, dept_id, biz))
            for table in ("products", "pos_products", "procurements", "invoice_items", "plantings"):
                retag(db, table, biz, row["name"], new, column="department")
            db.commit()
        except DB_ERRORS:
            db.rollback()
            flash("The department could not be renamed. Please try again.", "error")
        else:
            flash(f"'{row['name']}' is now '{new}'. Its items and past records moved with it.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/departments/merge")
@need("admin")
def merge_departments():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("settings"))
    rows = db.all("SELECT id, name FROM departments WHERE business_id=? ORDER BY name", (biz,))
    keep, others, final, problem = plan_merge(rows, read_ids(), text("department_name"), "department")
    if problem:
        flash(problem, "error")
        return back

    # Items, purchases, POS listings and past sales all carry the department by name, so all of them move
    tables = ("products", "pos_products", "procurements", "invoice_items", "plantings")
    try:
        for r in others:
            for table in tables:
                retag(db, table, biz, r["name"], final, column="department")
            db.execute("DELETE FROM departments WHERE id=? AND business_id=?", (r["id"], biz))
        if keep["name"] != final:
            db.execute("UPDATE departments SET name=? WHERE id=? AND business_id=?", (final, keep["id"], biz))
            for table in tables:
                retag(db, table, biz, keep["name"], final, column="department")
        items = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND department=?", (biz, final))["n"]
        sales = db.one("SELECT COUNT(*) AS n FROM invoice_items WHERE business_id=? AND department=?", (biz, final))["n"]
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be completed, so nothing was changed. Please try again.", "error")
        return back

    names = ", ".join(f"'{r['name']}'" for r in [keep] + others)
    flash(f"Merged {names} into '{final}'. It now holds {items} item(s) and {sales} sale line(s); the others were removed.", "ok")
    return back


# --- OPERATORS & ADMINS (ADMIN) ---
@app.get("/operators")
@need("admin")
def operators():
    db, biz = get_db(), g.user["business_id"]
    rows = db.all(
        "SELECT id, username, can_sell, can_procure, can_transfer, can_delete FROM users WHERE business_id=? AND role='operator' ORDER BY username",
        (biz,),
    )
    editing = None
    edit_id = request.args.get("edit", type=int)
    if edit_id:
        editing = next((row for row in rows if row["id"] == edit_id), None)

    admins = db.all("SELECT id, username FROM users WHERE business_id=? AND role='admin' ORDER BY username", (biz,))
    editing_admin = None
    edit_admin_id = request.args.get("edit_admin", type=int)
    if edit_admin_id:
        editing_admin = next((row for row in admins if row["id"] == edit_admin_id), None)
    return render_template(
        "operators.html", operators=rows, editing=editing, admins=admins, editing_admin=editing_admin, admin_count=len(admins),
    )


@app.post("/operators/save")
@need("admin")
def save_operator():
    db, biz = get_db(), g.user["business_id"]
    op_id = request.form.get("id", type=int)
    username, password = text("username"), request.form.get("password", "")
    flags = [1 if request.form.get("can_" + perm) else 0 for perm in PERMISSIONS]
    back = redirect(url_for("operators", edit=op_id) if op_id else url_for("operators"))

    if not username:
        flash("Username is required.", "error")
        return back
    if password and len(password) < 8:
        flash("Choose a password of at least 8 characters.", "error")
        return back
    if not op_id and not password:
        flash("A new operator needs a password.", "error")
        return back
    taken = db.one("SELECT id FROM users WHERE username=?", (username,))
    if taken and taken["id"] != op_id:
        flash("That username is already taken.", "error")
        return back

    try:
        if op_id:
            db.execute(
                "UPDATE users SET username=?, can_sell=?, can_procure=?, can_transfer=?, can_delete=? WHERE id=? AND business_id=? AND role='operator'",
                (username, *flags, op_id, biz),
            )
            if password:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE id=? AND business_id=? AND role='operator'",
                    (generate_password_hash(password), op_id, biz),
                )
        else:
            db.execute(
                """INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete)
                   VALUES (?, ?, ?, 'operator', ?, ?, ?, ?)""",
                (biz, username, generate_password_hash(password), *flags),
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The operator could not be saved. Please try again.", "error")
        return back

    flash(f"Operator '{username}' saved.", "ok")
    return redirect(url_for("operators"))


@app.post("/operators/<int:op_id>/delete")
@need("admin")
def delete_operator(op_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id=? AND business_id=? AND role='operator'", (op_id, g.user["business_id"]))
    db.commit()
    flash("Operator account deleted.", "ok")
    return redirect(url_for("operators"))


@app.post("/admins/save")
@need("admin")
def save_admin():
    """Any admin can create another admin, or rename one / reset their password. Admins have every permission, so there are no flags to set."""
    db, biz = get_db(), g.user["business_id"]
    admin_id = request.form.get("id", type=int)
    username, password = text("username"), request.form.get("password", "")
    back = redirect(url_for("operators", edit_admin=admin_id) if admin_id else url_for("operators"))

    if not username:
        flash("Username is required.", "error")
        return back
    if password and len(password) < 8:
        flash("Choose a password of at least 8 characters.", "error")
        return back
    if not admin_id and not password:
        flash("A new admin needs a password.", "error")
        return back
    taken = db.one("SELECT id FROM users WHERE username=?", (username,))
    if taken and taken["id"] != admin_id:
        flash("That username is already taken.", "error")
        return back

    try:
        if admin_id:
            db.execute("UPDATE users SET username=? WHERE id=? AND business_id=? AND role='admin'", (username, admin_id, biz))
            if password:
                db.execute("UPDATE users SET password_hash=? WHERE id=? AND business_id=? AND role='admin'", (generate_password_hash(password), admin_id, biz))
        else:
            db.execute(
                """INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete)
                   VALUES (?, ?, ?, 'admin', 1, 1, 1, 1)""",
                (biz, username, generate_password_hash(password)),
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The admin account could not be saved. Please try again.", "error")
        return back

    flash(f"Admin '{username}' saved. They can see and manage every operator and admin for {g.user['business_name']}." if not admin_id else f"Admin '{username}' saved.", "ok")
    return redirect(url_for("operators"))


@app.post("/admins/<int:admin_id>/delete")
@need("admin")
def delete_admin(admin_id):
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("operators"))
    if admin_id == g.user["id"]:
        flash("You cannot delete your own admin account while signed in as them. Ask another admin, or create one first.", "error")
        return back
    target = db.one("SELECT username FROM users WHERE id=? AND business_id=? AND role='admin'", (admin_id, biz))
    if target is None:
        flash("That admin account could not be found.", "error")
        return back
    admin_count = db.one("SELECT COUNT(*) AS n FROM users WHERE business_id=? AND role='admin'", (biz,))["n"]
    if admin_count <= 1:
        flash("This is the only admin account, so it cannot be deleted. Create another admin first.", "error")
        return back
    db.execute("DELETE FROM users WHERE id=? AND business_id=? AND role='admin'", (admin_id, biz))
    db.commit()
    flash(f"Admin '{target['username']}' deleted.", "ok")
    return redirect(url_for("operators"))


if __name__ == "__main__":
    app.run(port=5000)
