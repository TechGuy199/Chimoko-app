import calendar
import csv
import datetime
import functools
import hmac
import io
import json
import math
import os
import re
import secrets
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from db import DB_ERRORS, IS_POSTGRES, Database, init_schema

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Lusaka"))
PAYMENT_STATUSES = ("Paid", "Partial Payment", "Unpaid")
PERMISSIONS = ("sell", "procure", "transfer", "delete")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
HR_DEFAULTS = {"non_working_days": "saturday,sunday", "napsa_pct": 5.0, "nhima_pct": 2.0, "napsa_bearer": "employee", "nhima_bearer": "employee"}

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
            """SELECT u.id, u.business_id, u.username, u.role, u.can_sell, u.can_procure, u.can_transfer, u.can_delete,
                      b.name AS business_name, b.subtitle, b.contact, b.currency, b.low_stock_threshold
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
    return {"csrf_field": csrf_field, "user": g.get("user"), "can": can}


@app.template_filter("money")
def money(value):
    user = g.get("user")
    currency = user["currency"] if user else ""
    return f"{currency}{float(value or 0):,.2f}"


def text(key):
    return request.form.get(key, "").strip()


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
    # Imported invoices can carry any number, so make sure the candidate really is free
    while db.one("SELECT id FROM invoices WHERE business_id=? AND invoice_no=?", (biz, f"INV-{year}-{seq:04d}")):
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
                    "INSERT INTO businesses (name, subtitle, contact) VALUES (?, ?, ?)",
                    (business, text("subtitle"), text("contact")),
                )
                db.execute("INSERT INTO departments (business_id, name) VALUES (?, 'General')", (biz,))
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
    products = db.all(
        "SELECT id, item_code, name, department, quantity, price FROM pos_products WHERE business_id=? AND quantity > 0 ORDER BY name",
        (biz,),
    )
    units = db.one(
        """SELECT COALESCE(SUM(ii.qty), 0) AS n FROM invoice_items ii
           JOIN invoices i ON i.id = ii.invoice_id WHERE i.business_id=? AND i.kind='SALE'""",
        (biz,),
    )["n"]
    sales = db.one(
        "SELECT COALESCE(SUM(amount_paid), 0) AS received, COALESCE(SUM(balance_due), 0) AS owed FROM invoices WHERE business_id=? AND kind='SALE'",
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
        "pos.html", products=products, stats=stats, departments=department_names(db, biz), statuses=PAYMENT_STATUSES,
        known_customers=known_customers,
    )


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

    try:
        sold_at = now().strftime("%Y-%m-%d %H:%M:%S")
        invoice_id = db.insert(
            """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id)
               VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, next_invoice_no(db, biz), text("customer_name") or WALK_IN, text("customer_phone"),
                status, total, amount_paid, round(total - amount_paid, 2), g.user["username"], sold_at,
                customer_for(db, biz, text("customer_name"), text("customer_phone"), sold_at),
            ),
        )
        for p, qty, line_total in lines:
            # The quantity check in the WHERE clause stops two tills selling the same last units
            moved = db.execute(
                "UPDATE pos_products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
                (qty, p["id"], biz, qty),
            )
            if moved.rowcount != 1:
                raise StockError(p["name"])
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
ITEM_COLUMNS = "id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date"


def get_item(db, item_id):
    item = db.one(f"SELECT {ITEM_COLUMNS} FROM products WHERE id=? AND business_id=?", (item_id, g.user["business_id"]))
    if item is None:
        abort(404)
    return item


def chosen_department(db, biz):
    dept = text("department")
    return dept if dept in department_names(db, biz) else "General"


@app.get("/inventory")
@need(None)
def inventory():
    db, biz = get_db(), g.user["business_id"]
    items = db.all(f"SELECT {ITEM_COLUMNS} FROM products WHERE business_id=? ORDER BY name", (biz,))
    return render_template("inventory.html", items=items, departments=department_names(db, biz), today=now().strftime("%Y-%m-%d"))


@app.post("/inventory/purchase")
@need("procure")
def purchase():
    db, biz = get_db(), g.user["business_id"]
    code, name = text("item_code"), text("name")
    try:
        qty = number("quantity", int)
        unit_cost = number("unit_cost")
        price = number("price") if text("price") else None
    except ValueError:
        flash("Quantity must be a whole number; unit cost and selling price must be numbers.", "error")
        return redirect(url_for("inventory"))
    if not code or not name or qty <= 0 or unit_cost < 0 or (price is not None and price < 0):
        flash("Item code and name are required, quantity must be above zero and prices cannot be negative.", "error")
        return redirect(url_for("inventory"))
    try:
        amount_paid = amount_for_status(text("payment_status"), text("amount_paid"), round(qty * unit_cost, 2))
    except ValueError as e:
        flash(str(e).capitalize() + ".", "error")
        return redirect(url_for("inventory"))

    try:
        existing_name = record_purchase(db, biz, {
            "amount_paid": amount_paid,
            "item_code": code, "name": name, "category": text("category") or "General", "department": chosen_department(db, biz),
            "quantity": qty, "unit_cost": unit_cost, "price": price, "supplier_name": text("supplier_name"),
            "invoice_no": text("invoice_no"), "purchase_date": text("purchase_date") or now().strftime("%Y-%m-%d"),
            "batch_no": text("batch_no"), "date_planted": text("date_planted"), "expiry_date": text("expiry_date"),
        })
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The purchase could not be saved. Please try again.", "error")
        return redirect(url_for("inventory"))

    if existing_name:
        flash(f"Added {qty} unit(s) to the existing stock of '{existing_name}' (code {code}).", "ok")
    else:
        flash(f"Purchase logged for '{name}' and added to inventory.", "ok")
    return redirect(url_for("inventory"))


def record_purchase(db, biz, p):
    """Adds one purchase to stock and to the procurement log. Shared by the purchase form and the CSV import.

    A blank price (None) keeps an existing item's selling price and uses the unit cost for a new item.
    Returns the existing item's name when the code was already in stock, otherwise None. The caller commits.
    """
    existing = db.one("SELECT id, name, price FROM products WHERE business_id=? AND item_code=?", (biz, p["item_code"]))
    if existing:
        price = existing["price"] if p["price"] is None else p["price"]
        db.execute(
            """UPDATE products SET quantity = quantity + ?, unit_cost=?, price=?, batch_no=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?
               WHERE id=? AND business_id=?""",
            (p["quantity"], p["unit_cost"], price, p["batch_no"], p["supplier_name"], p["invoice_no"], p["purchase_date"], p["expiry_date"], existing["id"], biz),
        )
    else:
        price = p["unit_cost"] if p["price"] is None else p["price"]
        db.execute(
            """INSERT INTO products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, p["item_code"], p["name"], p["category"], p["department"], p["batch_no"], p["date_planted"], p["quantity"],
                p["unit_cost"], price, p["supplier_name"], p["invoice_no"], p["purchase_date"], p["expiry_date"],
            ),
        )
    total_cost = round(p["quantity"] * p["unit_cost"], 2)
    db.execute(
        """INSERT INTO procurements (business_id, item_code, name, category, department, supplier_name, invoice_no, purchase_date, quantity, unit_cost, total_cost, expiry_date, amount_paid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, p["item_code"], existing["name"] if existing else p["name"], p["category"], p["department"], p["supplier_name"],
            p["invoice_no"], p["purchase_date"], p["quantity"], p["unit_cost"], total_cost, p["expiry_date"],
            total_cost if p.get("amount_paid") is None else p["amount_paid"],
        ),
    )
    db.execute("UPDATE pos_products SET price=? WHERE business_id=? AND item_code=?", (price, biz, p["item_code"]))
    return existing["name"] if existing else None


@app.get("/inventory/<int:item_id>")
@need(None)
def item(item_id):
    db = get_db()
    return render_template("item.html", item=get_item(db, item_id), departments=department_names(db, g.user["business_id"]))


@app.post("/inventory/<int:item_id>/update")
@need("procure")
def update_item(item_id):
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)
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

    category, dept = text("category") or "General", chosen_department(db, biz)
    # Item code and quantity are not editable: stock only moves through purchases, transfers and sales
    try:
        db.execute(
            """UPDATE products SET name=?, category=?, department=?, batch_no=?, date_planted=?, unit_cost=?, price=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?
               WHERE id=? AND business_id=?""",
            (
                name, category, dept, text("batch_no"), text("date_planted"), unit_cost, price, text("supplier_name"),
                text("invoice_no"), text("purchase_date"), text("expiry_date"), item_id, biz,
            ),
        )
        db.execute(
            "UPDATE pos_products SET name=?, category=?, department=?, price=? WHERE business_id=? AND item_code=?",
            (name, category, dept, price, biz, current["item_code"]),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The item could not be updated. Please try again.", "error")
        return redirect(url_for("item", item_id=item_id))

    flash(f"Details saved for '{name}'.", "ok")
    return redirect(url_for("inventory"))


@app.post("/inventory/<int:item_id>/transfer")
@need("transfer")
def transfer(item_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_item(db, item_id)
    destination = text("destination")
    try:
        qty = number("quantity", int)
    except ValueError:
        qty = 0
    if destination not in ("farm", "pos") or qty <= 0 or qty > p["quantity"]:
        flash(f"Choose a destination and a quantity between 1 and {p['quantity']}.", "error")
        return redirect(url_for("item", item_id=item_id))

    invoice_id = None
    try:
        moved = db.execute(
            "UPDATE products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
            (qty, item_id, biz, qty),
        )
        if moved.rowcount != 1:
            raise StockError(p["name"])

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
    flash(f"Transferred {qty} unit(s) of '{p['name']}' to Sales & POS.", "ok")
    return redirect(url_for("inventory"))


@app.post("/inventory/<int:item_id>/delete")
@need("delete")
def delete_item(item_id):
    db = get_db()
    p = get_item(db, item_id)
    db.execute("DELETE FROM products WHERE id=? AND business_id=?", (item_id, g.user["business_id"]))
    db.commit()
    flash(f"'{p['name']}' was deleted from inventory.", "ok")
    return redirect(url_for("inventory"))


# --- SALES & INVOICES HISTORY ---
def history_rows(db, biz, q, limit):
    sql = """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_at
             FROM invoices WHERE business_id=?"""
    params = [biz]
    if q:
        like = f"%{q.lower()}%"
        sql += """ AND (LOWER(invoice_no) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(customer_phone) LIKE ?
                   OR EXISTS (SELECT 1 FROM invoice_items ii WHERE ii.invoice_id = invoices.id AND LOWER(ii.item_name) LIKE ?))"""
        params += [like, like, like, like]
    sql += " ORDER BY id DESC LIMIT ?"
    return db.all(sql, params + [limit])


@app.get("/history")
@need(None)
def history():
    q = request.args.get("q", "").strip()
    return render_template("history.html", invoices=history_rows(get_db(), g.user["business_id"], q, 500), q=q)


@app.get("/history.csv")
@need(None)
def history_csv():
    q = request.args.get("q", "").strip()
    rows = history_rows(get_db(), g.user["business_id"], q, 50000)
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
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id
           FROM invoices WHERE id=? AND business_id=?""",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    lines = db.all(
        "SELECT item_code, item_name, qty, unit_price, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (invoice_id, biz),
    )
    return render_template("invoice.html", inv=inv, lines=lines, payments=payment_history(db, "sale", invoice_id))


@app.post("/invoices/<int:invoice_id>/payment")
@need("admin")
def invoice_payment(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT total, amount_paid FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz))
    if inv is None:
        abort(404)
    try:
        new_paid = update_payment(db, "sale", invoice_id, inv["total"], inv["amount_paid"])
    except ValueError:
        db.rollback()
        flash(f"Enter an amount between 0 and the invoice total ({money(inv['total'])}).", "error")
        return redirect(url_for("invoice", invoice_id=invoice_id))

    db.execute(
        "UPDATE invoices SET amount_paid=?, balance_due=?, payment_status=? WHERE id=? AND business_id=?",
        (new_paid, round(inv["total"] - new_paid, 2), payment_status_for(inv["total"], new_paid), invoice_id, biz),
    )
    db.commit()
    flash("Payment updated.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


# --- PROCUREMENTS ---
PROCUREMENT_COLUMNS = "id, purchase_date, supplier_name, invoice_no, item_code, name, category, department, quantity, unit_cost, total_cost, expiry_date, amount_paid"


def with_payment_state(row):
    row["balance"] = round((row["total_cost"] or 0.0) - (row["amount_paid"] or 0.0), 2)
    row["status"] = payment_status_for(row["total_cost"] or 0.0, row["amount_paid"] or 0.0)
    return row


def procurement_rows(db, biz, category):
    sql = f"SELECT {PROCUREMENT_COLUMNS} FROM procurements WHERE business_id=?"
    params = [biz]
    if category:
        sql += " AND category=?"
        params.append(category)
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
    return render_template("purchase.html", p=get_procurement(db, purchase_id), payments=payment_history(db, "purchase", purchase_id))


@app.post("/procurements/<int:purchase_id>/payment")
@need("admin")
def procurement_payment(purchase_id):
    db = get_db()
    p = get_procurement(db, purchase_id)
    try:
        new_paid = update_payment(db, "purchase", purchase_id, p["total_cost"], p["amount_paid"])
    except ValueError:
        db.rollback()
        flash(f"Enter an amount between 0 and the purchase total ({money(p['total_cost'])}).", "error")
        return redirect(url_for("procurement", purchase_id=purchase_id))

    db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, purchase_id, g.user["business_id"]))
    db.commit()
    flash("Payment updated.", "ok")
    return redirect(url_for("procurement", purchase_id=purchase_id))


@app.get("/procurements")
@need(None)
def procurements():
    db, biz = get_db(), g.user["business_id"]
    category = request.args.get("category", "").strip()
    rows = procurement_rows(db, biz, category)
    by_category = {}
    for r in rows:
        label = r["category"] or "Uncategorised"
        by_category[label] = by_category.get(label, 0.0) + (r["total_cost"] or 0.0)
    categories = [c["category"] for c in db.all("SELECT DISTINCT category FROM procurements WHERE business_id=? ORDER BY category", (biz,))]
    return render_template(
        "procurements.html", rows=rows, category=category, categories=categories,
        by_category=sorted(by_category.items()), grand_total=sum(by_category.values()),
        total_paid=sum(r["amount_paid"] or 0.0 for r in rows), total_owed=sum(r["balance"] for r in rows),
    )


@app.get("/procurements.csv")
@need(None)
def procurements_csv():
    rows = procurement_rows(get_db(), g.user["business_id"], request.args.get("category", "").strip())
    return csv_response(
        "procurements.csv",
        ["Date", "Supplier", "Invoice No", "Item Code", "Product Name", "Category", "Dept", "Qty", "Unit Cost", "Total Cost", "Expiry", "Amount Paid", "Balance", "Payment Status"],
        [
            (r["purchase_date"], r["supplier_name"], r["invoice_no"], r["item_code"], r["name"], r["category"], r["department"],
             r["quantity"], r["unit_cost"], r["total_cost"], r["expiry_date"], r["amount_paid"], r["balance"], r["status"])
            for r in rows
        ],
    )


# --- HR & PAYROLL (ADMIN) ---
def hr_settings_for(db, biz):
    return db.one("SELECT non_working_days, napsa_pct, nhima_pct, napsa_bearer, nhima_bearer FROM hr_settings WHERE business_id=?", (biz,)) or dict(HR_DEFAULTS)


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
        napsa_bearer, nhima_bearer = text("napsa_bearer"), text("nhima_bearer")
        try:
            napsa, nhima = number("napsa_pct"), number("nhima_pct")
        except ValueError:
            napsa = nhima = -1
        if not (0 <= napsa <= 100 and 0 <= nhima <= 100) or napsa_bearer not in ("employee", "employer") or nhima_bearer not in ("employee", "employer"):
            flash("Rates must be percentages between 0 and 100.", "error")
        elif len(non_working.split(",")) == 7:
            flash("At least one day of the week must be a working day.", "error")
        else:
            values = (non_working, napsa, nhima, napsa_bearer, nhima_bearer, biz)
            if db.one("SELECT business_id FROM hr_settings WHERE business_id=?", (biz,)):
                db.execute("UPDATE hr_settings SET non_working_days=?, napsa_pct=?, nhima_pct=?, napsa_bearer=?, nhima_bearer=? WHERE business_id=?", values)
            else:
                db.execute("INSERT INTO hr_settings (non_working_days, napsa_pct, nhima_pct, napsa_bearer, nhima_bearer, business_id) VALUES (?, ?, ?, ?, ?, ?)", values)
            db.commit()
            flash("HR settings saved. They apply to payslips computed from now on.", "ok")
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

        basic = employee["basic_salary"]
        calculated_basic = round(basic / expected * worked, 2)
        napsa = round(calculated_basic * hr_set["napsa_pct"] / 100.0, 2)
        nhima = round(calculated_basic * hr_set["nhima_pct"] / 100.0, 2)
        # A contribution is deducted from pay only when the employee bears it; otherwise it is an employer cost
        emp_napsa, employer_napsa = (napsa, 0.0) if hr_set["napsa_bearer"] == "employee" else (0.0, napsa)
        emp_nhima, employer_nhima = (nhima, 0.0) if hr_set["nhima_bearer"] == "employee" else (0.0, nhima)
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
        db.commit()
        return redirect(url_for("payslips", id=payslip_id))

    return render_template("payslip_new.html", employees=employees, hr=hr_set, this_month=now().strftime("%Y-%m"))


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
                   napsa_amt, nhima_amt, net_payable FROM payroll WHERE business_id=? AND id IN ({marks}) ORDER BY id""",
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
    db.commit()
    flash("Payslip deleted.", "ok")
    return redirect(url_for("hr"))


# --- CUSTOMER STATEMENTS (ADMIN) ---
@app.get("/customers")
@need("admin")
def customers():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    sql = """SELECT c.id, c.name, c.phone, COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed
             FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind='SALE'
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
    return render_template("customers.html", results=db.all(sql, params), q=q)


def get_customer(db, customer_id):
    customer = db.one("SELECT id, name, phone, created_at FROM customers WHERE id=? AND business_id=?", (customer_id, g.user["business_id"]))
    if customer is None:
        abort(404)
    return customer


def statement_data(db, customer_id):
    """Everything a customer statement shows, shared by the page and the PDF."""
    biz = g.user["business_id"]
    invoices = db.all(
        """SELECT id, invoice_no, created_at, created_by, payment_status, total, amount_paid, balance_due
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
        for line in inv["lines"]:
            p = products.setdefault((line["item_code"], line["item_name"]), {"code": line["item_code"], "name": line["item_name"], "qty": 0, "value": 0.0, "last": ""})
            p["qty"] += line["qty"]
            p["value"] += line["line_total"]
            p["last"] = max(p["last"], inv["created_at"][:10])

    # The statement: every invoice as a charge and every payment as a credit, in date order with a running balance.
    # Whatever was paid before the first logged change was paid at the time of sale.
    entries = []
    for inv in invoices:
        items = ", ".join(f"{line['qty']} x {line['item_name']}" for line in inv["lines"])
        entries.append({"at": inv["created_at"], "order": 0, "what": f"Invoice {inv['invoice_no']}", "detail": items, "charge": inv["total"], "paid": 0.0, "invoice_id": inv["id"]})
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

    summary = {
        "invoices": len(invoices), "total": sum(inv["total"] for inv in invoices),
        "paid": sum(inv["amount_paid"] for inv in invoices), "owed": sum(inv["balance_due"] for inv in invoices),
    }
    return {
        "invoices": invoices, "entries": entries, "summary": summary,
        "products": sorted(products.values(), key=lambda p: -p["value"]), "printed": now().strftime("%d %b %Y %H:%M"),
    }


@app.get("/customers/<int:customer_id>")
@need("admin")
def customer_statement(customer_id):
    db = get_db()
    return render_template("customer.html", customer=get_customer(db, customer_id), **statement_data(db, customer_id))


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
    """Spreads one lump-sum payment over the customer's unpaid invoices, oldest first."""
    db, biz = get_db(), g.user["business_id"]
    get_customer(db, customer_id)
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


# --- REPORTS ---
REPORT_PERIODS = (
    ("this_month", "This month"), ("last_month", "Last month"), ("last_30", "Last 30 days"),
    ("this_year", "This year"), ("all", "All time"), ("custom", "Custom range"),
)
REPORT_PARAMS = ("period", "from", "to", "view", "dept")


def report_period():
    """Returns (choice, first day, last day) from the query string; both days are None for 'all time'."""
    today = now().date()
    choice = request.args.get("period", "this_month")
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
                  WHERE ii.business_id=? AND i.created_at >= ? AND i.created_at < ?"""
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
}
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
}
IMPORT_TEMPLATES = {
    "purchases": (
        ["Item Code", "Name", "Category", "Department", "Quantity", "Unit Cost", "Selling Price", "Supplier", "Invoice No", "Purchase Date", "Batch No", "Date Planted", "Expiry Date", "Payment Status", "Amount Paid"],
        ["SEED-001", "Maize Seed 10kg", "Seeds", "General", 50, 450, 600, "Agro Supplies Ltd", "SUP-1001", "2026-10-01", "B-22", "", "2027-10-01", "Paid", ""],
    ),
    "sales": (
        ["Invoice No", "Date", "Customer", "Phone", "Item Code", "Item Name", "Quantity", "Unit Price", "Payment Status", "Amount Paid"],
        ["", "2026-10-01 09:30", "Jane Banda", "0977123456", "SEED-001", "Maize Seed 10kg", 2, 600, "Paid", ""],
    ),
}
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
    missing = [columns[field][0].title() for field in required if field not in fields]
    if missing:
        raise ImportProblem("The file is missing these column headings: " + ", ".join(missing) + ". Download the template to see the expected layout.")

    records = []
    for row_no, cells in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue
        record = dict.fromkeys(columns, "")
        for field, cell in zip(fields, cells):
            if field:
                cell = cell.strip()
                # Undo the apostrophe our own exports put in front of formula-like text
                record[field] = cell[1:] if len(cell) > 1 and cell[0] == "'" and cell[1] in "=@+-" else cell
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
        "import.html", max_rows=IMPORT_MAX_ROWS, error_kind=kind, errors=errors[:IMPORT_MAX_ERRORS_SHOWN], error_count=len(errors),
    )


@app.get("/import")
@need("admin")
def import_data():
    return render_template("import.html", max_rows=IMPORT_MAX_ROWS)


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

    known_names = {r["item_code"]: r["name"] for r in db.all("SELECT item_code, name FROM products WHERE business_id=?", (biz,))}
    departments = set(department_names(db, biz))
    today = now().strftime("%Y-%m-%d")
    errors, purchases, new_departments = [], [], []

    for rec in records:
        problems = []
        code = rec["item_code"]
        name = rec["name"] or known_names.get(code, "")
        if not code:
            problems.append("item code is missing")
        elif not name:
            problems.append("name is missing (needed because this item code is new)")

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

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue

        known_names.setdefault(code, name)
        dept = rec["department"] or "General"
        if dept not in departments:
            departments.add(dept)
            new_departments.append(dept)
        purchases.append({
            "item_code": code, "name": name, "category": rec["category"] or "General", "department": dept,
            "quantity": qty, "unit_cost": round(unit_cost, 2), "price": None if price is None else round(price, 2),
            "supplier_name": rec["supplier_name"], "invoice_no": rec["invoice_no"], "purchase_date": dates["purchase_date"] or today,
            "batch_no": rec["batch_no"], "date_planted": dates["date_planted"], "expiry_date": dates["expiry_date"],
            "amount_paid": amount_paid,
        })

    if errors:
        return import_errors("purchases", errors)

    restocked = 0
    try:
        for dept in new_departments:
            db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, dept))
        for p in purchases:
            if record_purchase(db, biz, p):
                restocked += 1
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    flash(f"Imported {len(purchases)} purchase row(s): {len(purchases) - restocked} new item(s) created, {restocked} added to existing stock.", "ok")
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
            for code, name, dept, batch, qty, unit_price, line_total in lines:
                db.execute(
                    """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, biz, code, name, dept, batch, qty, unit_price, line_total),
                )
        for code, qty in needed.items():
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


@app.errorhandler(413)
def upload_too_large(error):
    flash("That file is too large. The limit is 2 MB; split it into smaller files.", "error")
    return redirect(url_for("import_data"))


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
    return render_template("settings.html", departments=departments)


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


# --- OPERATORS (ADMIN) ---
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
    return render_template("operators.html", operators=rows, editing=editing)


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


if __name__ == "__main__":
    app.run(port=5000)
