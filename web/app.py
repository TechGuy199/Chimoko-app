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
    last = db.one(
        "SELECT invoice_no FROM invoices WHERE business_id=? AND invoice_no LIKE ? ORDER BY id DESC LIMIT 1",
        (biz, f"INV-{year}-%"),
    )
    seq = 1
    if last:
        try:
            seq = int(last["invoice_no"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            pass
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
    revenue = db.one("SELECT COALESCE(SUM(amount_paid), 0) AS n FROM invoices WHERE business_id=? AND kind='SALE'", (biz,))["n"]
    stats = {
        "listed": len(products),
        "low": sum(1 for p in products if p["quantity"] < g.user["low_stock_threshold"]),
        "units": units,
        "revenue": revenue,
    }
    return render_template("pos.html", products=products, stats=stats, departments=department_names(db, biz), statuses=PAYMENT_STATUSES)


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
        invoice_id = db.insert(
            """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at)
               VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, next_invoice_no(db, biz), text("customer_name") or "Walk-in Customer", text("customer_phone"),
                status, total, amount_paid, round(total - amount_paid, 2), g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"),
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
        price = number("price", default=unit_cost)
    except ValueError:
        flash("Quantity must be a whole number; unit cost and selling price must be numbers.", "error")
        return redirect(url_for("inventory"))
    if not code or not name or qty <= 0 or unit_cost < 0 or price < 0:
        flash("Item code and name are required, quantity must be above zero and prices cannot be negative.", "error")
        return redirect(url_for("inventory"))

    category, dept = text("category") or "General", chosen_department(db, biz)
    supplier, invoice_no, expiry, batch = text("supplier_name"), text("invoice_no"), text("expiry_date"), text("batch_no")
    purchase_date = text("purchase_date") or now().strftime("%Y-%m-%d")

    existing = db.one("SELECT id, name FROM products WHERE business_id=? AND item_code=?", (biz, code))
    try:
        if existing:
            db.execute(
                """UPDATE products SET quantity = quantity + ?, unit_cost=?, price=?, batch_no=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?
                   WHERE id=? AND business_id=?""",
                (qty, unit_cost, price, batch, supplier, invoice_no, purchase_date, expiry, existing["id"], biz),
            )
        else:
            db.execute(
                """INSERT INTO products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (biz, code, name, category, dept, batch, text("date_planted"), qty, unit_cost, price, supplier, invoice_no, purchase_date, expiry),
            )
        db.execute(
            """INSERT INTO procurements (business_id, item_code, name, category, department, supplier_name, invoice_no, purchase_date, quantity, unit_cost, total_cost, expiry_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (biz, code, existing["name"] if existing else name, category, dept, supplier, invoice_no, purchase_date, qty, unit_cost, round(qty * unit_cost, 2), expiry),
        )
        db.execute("UPDATE pos_products SET price=? WHERE business_id=? AND item_code=?", (price, biz, code))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The purchase could not be saved. Please try again.", "error")
        return redirect(url_for("inventory"))

    if existing:
        flash(f"Added {qty} unit(s) to the existing stock of '{existing['name']}' (code {code}).", "ok")
    else:
        flash(f"Purchase logged for '{name}' and added to inventory.", "ok")
    return redirect(url_for("inventory"))


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
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at
           FROM invoices WHERE id=? AND business_id=?""",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    lines = db.all(
        "SELECT item_code, item_name, qty, unit_price, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (invoice_id, biz),
    )
    return render_template("invoice.html", inv=inv, lines=lines)


# --- PROCUREMENTS ---
def procurement_rows(db, biz, category):
    sql = """SELECT purchase_date, supplier_name, invoice_no, item_code, name, category, department, quantity, unit_cost, total_cost, expiry_date
             FROM procurements WHERE business_id=?"""
    params = [biz]
    if category:
        sql += " AND category=?"
        params.append(category)
    return db.all(sql + " ORDER BY id DESC", params)


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
    )


@app.get("/procurements.csv")
@need(None)
def procurements_csv():
    rows = procurement_rows(get_db(), g.user["business_id"], request.args.get("category", "").strip())
    return csv_response(
        "procurements.csv",
        ["Date", "Supplier", "Invoice No", "Item Code", "Product Name", "Category", "Dept", "Qty", "Unit Cost", "Total Cost", "Expiry"],
        [
            (r["purchase_date"], r["supplier_name"], r["invoice_no"], r["item_code"], r["name"], r["category"], r["department"],
             r["quantity"], r["unit_cost"], r["total_cost"], r["expiry_date"])
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
