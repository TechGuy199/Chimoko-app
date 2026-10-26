"""Database access. Postgres when DATABASE_URL is set (Vercel), a local SQLite file otherwise.

Queries are written once with ? placeholders and must work on both engines, so keep
them to plain SQL: no literal % or ? characters, and always alias aggregates.
"""
import hashlib
import inspect
import os
import re
import sqlite3
import threading
import time

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
IS_POSTGRES = bool(DATABASE_URL)
LOCAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_dev.db")

if IS_POSTGRES:
    import psycopg
    from psycopg.pq import TransactionStatus
    from psycopg.rows import dict_row
    DB_ERRORS = (psycopg.Error,)
else:
    DB_ERRORS = (sqlite3.Error,)

PRIMARY_KEY = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS businesses (
        id {pk}, name TEXT NOT NULL, subtitle TEXT DEFAULT '', contact TEXT DEFAULT '',
        currency TEXT DEFAULT 'ZMW ', low_stock_threshold INTEGER DEFAULT 10
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id {pk}, business_id INTEGER NOT NULL, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'operator', can_sell INTEGER DEFAULT 1, can_procure INTEGER DEFAULT 1,
        can_transfer INTEGER DEFAULT 1, can_delete INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS departments (
        id {pk}, business_id INTEGER NOT NULL, name TEXT NOT NULL, UNIQUE (business_id, name)
    )""",
    # The managed list of inventory categories shown as tiles. An item keeps its category by name, so the
    # name is what links the two. A name with the word seed or seeds marks a seed category (counted in packs).
    """CREATE TABLE IF NOT EXISTS categories (
        id {pk}, business_id INTEGER NOT NULL, name TEXT NOT NULL, UNIQUE (business_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS products (
        id {pk}, business_id INTEGER NOT NULL, item_code TEXT NOT NULL, name TEXT NOT NULL,
        category TEXT DEFAULT '', department TEXT DEFAULT 'General', batch_no TEXT DEFAULT '', date_planted TEXT DEFAULT '',
        quantity INTEGER NOT NULL DEFAULT 0, unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0, price DOUBLE PRECISION NOT NULL DEFAULT 0,
        supplier_name TEXT DEFAULT '', invoice_no TEXT DEFAULT '', purchase_date TEXT DEFAULT '', expiry_date TEXT DEFAULT '',
        UNIQUE (business_id, item_code)
    )""",
    """CREATE TABLE IF NOT EXISTS pos_products (
        id {pk}, business_id INTEGER NOT NULL, item_code TEXT NOT NULL, name TEXT NOT NULL,
        category TEXT DEFAULT '', department TEXT DEFAULT 'General', batch_no TEXT DEFAULT '', date_planted TEXT DEFAULT '',
        quantity INTEGER NOT NULL DEFAULT 0, price DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (business_id, item_code)
    )""",
    """CREATE TABLE IF NOT EXISTS procurements (
        id {pk}, business_id INTEGER NOT NULL, item_code TEXT, name TEXT, category TEXT, department TEXT,
        supplier_name TEXT, invoice_no TEXT, purchase_date TEXT, quantity INTEGER,
        unit_cost DOUBLE PRECISION, total_cost DOUBLE PRECISION, expiry_date TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS invoices (
        id {pk}, business_id INTEGER NOT NULL, invoice_no TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'SALE',
        customer_name TEXT, customer_phone TEXT, payment_status TEXT,
        total DOUBLE PRECISION NOT NULL DEFAULT 0, amount_paid DOUBLE PRECISION NOT NULL DEFAULT 0,
        balance_due DOUBLE PRECISION NOT NULL DEFAULT 0, created_by TEXT, created_at TEXT,
        UNIQUE (business_id, invoice_no)
    )""",
    """CREATE TABLE IF NOT EXISTS invoice_items (
        id {pk}, invoice_id INTEGER NOT NULL, business_id INTEGER NOT NULL, item_code TEXT, item_name TEXT,
        department TEXT, batch_no TEXT, qty INTEGER NOT NULL,
        unit_price DOUBLE PRECISION NOT NULL DEFAULT 0, line_total DOUBLE PRECISION NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS employees (
        id {pk}, business_id INTEGER NOT NULL, full_name TEXT NOT NULL, nrc_number TEXT NOT NULL,
        basic_salary DOUBLE PRECISION NOT NULL DEFAULT 0, employment_type TEXT DEFAULT 'Permanent', address TEXT DEFAULT '',
        age INTEGER, start_date TEXT DEFAULT '', expected_days_week INTEGER DEFAULT 5
    )""",
    """CREATE TABLE IF NOT EXISTS hr_settings (
        business_id INTEGER PRIMARY KEY, non_working_days TEXT NOT NULL, napsa_pct DOUBLE PRECISION NOT NULL,
        nhima_pct DOUBLE PRECISION NOT NULL, napsa_bearer TEXT NOT NULL, nhima_bearer TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS payroll (
        id {pk}, business_id INTEGER NOT NULL, employee_id INTEGER, employee_name TEXT NOT NULL, pay_month TEXT NOT NULL,
        basic_salary DOUBLE PRECISION NOT NULL, expected_days DOUBLE PRECISION NOT NULL, worked_days DOUBLE PRECISION NOT NULL,
        calculated_basic DOUBLE PRECISION NOT NULL, bonuses_json TEXT NOT NULL, deductions_json TEXT NOT NULL,
        napsa_amt DOUBLE PRECISION NOT NULL DEFAULT 0, nhima_amt DOUBLE PRECISION NOT NULL DEFAULT 0,
        employer_napsa DOUBLE PRECISION NOT NULL DEFAULT 0, employer_nhima DOUBLE PRECISION NOT NULL DEFAULT 0,
        net_payable DOUBLE PRECISION NOT NULL, created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS payment_log (
        id {pk}, business_id INTEGER NOT NULL, kind TEXT NOT NULL, ref_id INTEGER NOT NULL,
        old_paid DOUBLE PRECISION NOT NULL, new_paid DOUBLE PRECISION NOT NULL, note TEXT DEFAULT '',
        changed_by TEXT, changed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS customers (
        id {pk}, business_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT DEFAULT '',
        name_key TEXT NOT NULL, phone_key TEXT NOT NULL, created_at TEXT,
        UNIQUE (business_id, name_key, phone_key)
    )""",
    # Name/phone spellings of accounts that were merged away, so later sales typed that way still find the kept account
    """CREATE TABLE IF NOT EXISTS customer_aliases (
        id {pk}, business_id INTEGER NOT NULL, name_key TEXT NOT NULL, phone_key TEXT NOT NULL, customer_id INTEGER NOT NULL,
        UNIQUE (business_id, name_key, phone_key)
    )""",
    # In-app alerts. for_user_id is the one user it is for, or NULL when it is for the business's admins.
    # wa_status records what happened to the WhatsApp copy: sent, failed, or skipped (with the reason in wa_error).
    """CREATE TABLE IF NOT EXISTS notifications (
        id {pk}, business_id INTEGER NOT NULL, for_user_id INTEGER, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT DEFAULT '',
        link TEXT DEFAULT '', created_at TEXT, read_at TEXT, wa_status TEXT DEFAULT '', wa_error TEXT DEFAULT ''
    )""",
    # An operator asking an admin to allow one action on one item that their account cannot normally do
    """CREATE TABLE IF NOT EXISTS permission_requests (
        id {pk}, business_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL,
        item_id INTEGER NOT NULL, item_name TEXT NOT NULL, reason TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT, decided_by TEXT, decided_at TEXT, expires_at TEXT
    )""",
    # The permanent record of an invoice that was deleted: what it held (as it was), who deleted it, who allowed it and why.
    # Nothing deletes from this table except a full reset of the app.
    """CREATE TABLE IF NOT EXISTS deleted_invoices (
        id {pk}, business_id INTEGER NOT NULL, invoice_no TEXT NOT NULL, kind TEXT, customer_name TEXT, customer_phone TEXT,
        payment_status TEXT, total DOUBLE PRECISION NOT NULL DEFAULT 0, amount_paid DOUBLE PRECISION NOT NULL DEFAULT 0,
        balance_due DOUBLE PRECISION NOT NULL DEFAULT 0, created_by TEXT, created_at TEXT, items_json TEXT DEFAULT '[]',
        payments_json TEXT DEFAULT '[]', stock_note TEXT DEFAULT '', reason TEXT NOT NULL, deleted_by TEXT NOT NULL,
        approved_by TEXT DEFAULT '', request_id INTEGER, deleted_at TEXT
    )""",
    # Greenhouse: every time seed packs are sown and the seedlings listed for sale. Keeps the germination
    # estimate and cost assumptions that were used, so a batch can be looked back on later.
    """CREATE TABLE IF NOT EXISTS seedling_batches (
        id {pk}, business_id INTEGER NOT NULL, seed_item_id INTEGER, seed_code TEXT NOT NULL, seed_name TEXT NOT NULL,
        pos_code TEXT NOT NULL, seedling_name TEXT NOT NULL, packs INTEGER NOT NULL, seeds_per_pack INTEGER NOT NULL,
        seeds_sown INTEGER NOT NULL, germination_pct DOUBLE PRECISION NOT NULL, seed_unit_cost DOUBLE PRECISION NOT NULL,
        seedlings INTEGER NOT NULL, cost_per_seedling DOUBLE PRECISION NOT NULL, selling_price DOUBLE PRECISION NOT NULL,
        created_by TEXT, created_at TEXT
    )""",
    # Every manual stock correction: an item removed from the POS (disease, damage, mortality...) or an inventory
    # quantity corrected by hand. kind is 'pos_removal' or 'inventory_edit'. delta is negative for a decrease.
    """CREATE TABLE IF NOT EXISTS stock_adjustments (
        id {pk}, business_id INTEGER NOT NULL, kind TEXT NOT NULL, item_code TEXT NOT NULL, item_name TEXT NOT NULL, department TEXT DEFAULT '',
        quantity_before INTEGER NOT NULL, quantity_after INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT DEFAULT '', note TEXT DEFAULT '',
        pos_product_id INTEGER, product_id INTEGER, created_by TEXT, created_at TEXT
    )""",
    # Supplier accounts, opened from the supplier named on a purchase. They work like customer accounts: aliases remember the spellings
    # of accounts that were merged away, and distinct pairs are ones an admin said are different suppliers.
    """CREATE TABLE IF NOT EXISTS suppliers (
        id {pk}, business_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT DEFAULT '',
        name_key TEXT NOT NULL, phone_key TEXT NOT NULL, created_at TEXT,
        UNIQUE (business_id, name_key, phone_key)
    )""",
    """CREATE TABLE IF NOT EXISTS supplier_aliases (
        id {pk}, business_id INTEGER NOT NULL, name_key TEXT NOT NULL, phone_key TEXT NOT NULL, supplier_id INTEGER NOT NULL,
        UNIQUE (business_id, name_key, phone_key)
    )""",
    """CREATE TABLE IF NOT EXISTS supplier_distinct (
        id {pk}, business_id INTEGER NOT NULL, low_id INTEGER NOT NULL, high_id INTEGER NOT NULL,
        UNIQUE (business_id, low_id, high_id)
    )""",
    # Every payment made to a supplier: how it was paid and the receipt or reference number. A negative amount is a correction.
    # allocations say which purchases a payment settled (oldest first for a lump sum).
    """CREATE TABLE IF NOT EXISTS supplier_payments (
        id {pk}, business_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL, amount DOUBLE PRECISION NOT NULL, method TEXT NOT NULL,
        reference TEXT DEFAULT '', paid_on TEXT, note TEXT DEFAULT '', recorded_by TEXT, recorded_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS supplier_payment_allocations (
        id {pk}, business_id INTEGER NOT NULL, payment_id INTEGER NOT NULL, procurement_id INTEGER NOT NULL, amount DOUBLE PRECISION NOT NULL
    )""",
    # Greenhouse planting records. seedlings is the number expected to be ready (seeds planted less the expected mortality),
    # or the real count once the batch is marked ready. pos_code is the code the batch is sold under in the POS.
    """CREATE TABLE IF NOT EXISTS plantings (
        id {pk}, business_id INTEGER NOT NULL, seed_item_id INTEGER, seed_code TEXT NOT NULL, seed_name TEXT NOT NULL,
        name TEXT NOT NULL, department TEXT DEFAULT 'General', batch_no TEXT DEFAULT '', source TEXT NOT NULL DEFAULT 'inventory',
        seeds_planted INTEGER NOT NULL, packs_used INTEGER NOT NULL DEFAULT 0, loose_delta INTEGER NOT NULL DEFAULT 0,
        seeds_per_pack INTEGER, mortality_pct DOUBLE PRECISION NOT NULL DEFAULT 0, seedlings INTEGER NOT NULL,
        seed_unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0, cost_per_seedling DOUBLE PRECISION NOT NULL DEFAULT 0,
        price DOUBLE PRECISION NOT NULL DEFAULT 0, planted_on TEXT, ready_on TEXT, actual_ready_on TEXT,
        pos_code TEXT DEFAULT '', released INTEGER NOT NULL DEFAULT 0, notes TEXT DEFAULT '', created_by TEXT, created_at TEXT
    )""",
    # A customer's order for seedlings from a batch. It holds the stock straight away but becomes a sale only on collection, with approval.
    """CREATE TABLE IF NOT EXISTS preorders (
        id {pk}, business_id INTEGER NOT NULL, planting_id INTEGER NOT NULL, customer_name TEXT NOT NULL, customer_phone TEXT DEFAULT '',
        qty INTEGER NOT NULL, unit_price DOUBLE PRECISION NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending', note TEXT DEFAULT '',
        created_by TEXT, created_at TEXT, collected_by TEXT, approved_by TEXT, collected_at TEXT, invoice_id INTEGER, cancelled_at TEXT
    )""",
    # Pairs an admin has confirmed are different people, so they stop being suggested as duplicates
    """CREATE TABLE IF NOT EXISTS customer_distinct (
        id {pk}, business_id INTEGER NOT NULL, low_id INTEGER NOT NULL, high_id INTEGER NOT NULL,
        UNIQUE (business_id, low_id, high_id)
    )""",
    # Accounts: every movement of money into or out of the business, one row each, written in the same transaction as the
    # sale, payment, purchase or payslip it belongs to. direction is 'in' or 'out'; a negative amount corrects an earlier
    # entry in the same direction. source/source_id point back at what it came from ('invoice', 'purchase', 'supplier_payment',
    # 'customer', 'payslip'), or 'manual' for income and expenses entered on the Accounts page itself.
    """CREATE TABLE IF NOT EXISTS cashbook (
        id {pk}, business_id INTEGER NOT NULL, direction TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL, method TEXT NOT NULL,
        reference TEXT DEFAULT '', category TEXT NOT NULL, party TEXT DEFAULT '', description TEXT DEFAULT '',
        source TEXT DEFAULT '', source_id INTEGER, occurred_at TEXT NOT NULL, recorded_by TEXT, recorded_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS cashbook_by_date ON cashbook (business_id, occurred_at)",
    # Daily activities. A plot is a work station (a greenhouse block, a field, a nursery bench) in a department, with workers from the
    # payroll assigned to it. An activity is one thing a worker did there on a day; the plot's and worker's names are kept with it,
    # so the record still reads right after a plot is renamed or a worker leaves. active = 0 is a plot taken out of use.
    """CREATE TABLE IF NOT EXISTS plots (
        id {pk}, business_id INTEGER NOT NULL, department TEXT NOT NULL, name TEXT NOT NULL, notes TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS plot_workers (
        id {pk}, business_id INTEGER NOT NULL, plot_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, UNIQUE (plot_id, employee_id)
    )""",
    """CREATE TABLE IF NOT EXISTS activities (
        id {pk}, business_id INTEGER NOT NULL, plot_id INTEGER, plot_name TEXT NOT NULL, department TEXT DEFAULT '', work_date TEXT NOT NULL,
        employee_id INTEGER, worker_name TEXT NOT NULL, period TEXT NOT NULL, description TEXT NOT NULL, tools TEXT DEFAULT '',
        product TEXT DEFAULT '', product_qty DOUBLE PRECISION, product_unit TEXT DEFAULT '', work_done TEXT DEFAULT '',
        recorded_by TEXT, recorded_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS activities_by_day ON activities (business_id, work_date)",
    # Accounts: what was really held in each balance (Cash, Bank, Airtel Money, MTN Money) when the owner counted it, and what the
    # app expected it to be at that moment (NULL the first time). The latest count of each is where its balance starts from.
    """CREATE TABLE IF NOT EXISTS money_balances (
        id {pk}, business_id INTEGER NOT NULL, account TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL, as_of TEXT NOT NULL,
        last_id INTEGER, expected DOUBLE PRECISION, set_by TEXT, set_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS cashbook_by_source ON cashbook (business_id, source, source_id)",
    # Which version of the start-up work (the schema above, its migrations and the app's one-off repairs) has been run
    "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)",
]

# What the pages look things up by, so they stay quick as the records grow. Made after the migrations in init_schema, as some
# of these columns are added by them.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS invoices_by_date ON invoices (business_id, created_at)",
    "CREATE INDEX IF NOT EXISTS invoices_by_customer ON invoices (business_id, customer_id)",
    "CREATE INDEX IF NOT EXISTS invoice_items_by_invoice ON invoice_items (invoice_id)",
    "CREATE INDEX IF NOT EXISTS invoice_items_by_code ON invoice_items (business_id, item_code)",
    "CREATE INDEX IF NOT EXISTS procurements_by_date ON procurements (business_id, purchase_date)",
    "CREATE INDEX IF NOT EXISTS procurements_by_supplier ON procurements (business_id, supplier_id)",
    "CREATE INDEX IF NOT EXISTS payment_log_by_record ON payment_log (business_id, kind, ref_id)",
    "CREATE INDEX IF NOT EXISTS customers_by_phone ON customers (business_id, phone_key)",
    "CREATE INDEX IF NOT EXISTS supplier_payments_by_supplier ON supplier_payments (business_id, supplier_id)",
    "CREATE INDEX IF NOT EXISTS allocations_by_payment ON supplier_payment_allocations (payment_id)",
    "CREATE INDEX IF NOT EXISTS allocations_by_purchase ON supplier_payment_allocations (procurement_id)",
    "CREATE INDEX IF NOT EXISTS preorders_by_planting ON preorders (business_id, planting_id)",
    "CREATE INDEX IF NOT EXISTS plantings_by_business ON plantings (business_id, ready_on)",
    "CREATE INDEX IF NOT EXISTS notifications_by_business ON notifications (business_id, read_at)",
    "CREATE INDEX IF NOT EXISTS stock_adjustments_by_date ON stock_adjustments (business_id, created_at)",
    "CREATE INDEX IF NOT EXISTS payroll_by_employee ON payroll (business_id, employee_id)",
    "CREATE INDEX IF NOT EXISTS money_balances_by_account ON money_balances (business_id, account)",
]

# --- Postgres connections, kept between requests ---
# Opening a connection to a hosted Postgres costs several network round trips (and an encryption handshake), often more than
# the queries a page runs. So a connection is kept when a request finishes and handed to the next one on the same thread,
# for as long as it is in steady use. Before it is kept, anything left open is rolled back, so no request ever sees another's
# unfinished work and no transaction sits open between requests. A kept connection the server has closed meanwhile is
# noticed on its first query and replaced, once. Server-side prepared statements are off, so connection poolers in front
# of the database work too.
KEEP_FOR = 120   # seconds a connection may sit unused and still be handed on
_kept = threading.local()


def _connect():
    options = {"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}
    if os.name != "nt":
        options["tcp_user_timeout"] = 15000   # a connection that silently died fails within 15 s instead of hanging
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None, **options)


def _usable(conn):
    return not conn.closed and not conn.broken


def _take_connection():
    """A kept connection if there is a fresh one, otherwise a new one: (connection, whether it was kept)."""
    conn, since = getattr(_kept, "conn", None), getattr(_kept, "since", 0.0)
    _kept.conn = None
    if conn is not None:
        if _usable(conn) and time.monotonic() - since < KEEP_FOR and conn.info.transaction_status == TransactionStatus.IDLE:
            return conn, True
        _close_quietly(conn)
    return _connect(), False


def _keep_connection(conn):
    if not _usable(conn):
        _close_quietly(conn)
        return
    try:
        if conn.info.transaction_status != TransactionStatus.IDLE:
            conn.rollback()
    except DB_ERRORS:
        _close_quietly(conn)
        return
    if getattr(_kept, "conn", None) is not None:
        _close_quietly(conn)   # one is kept already (a second connection opened during the same request)
        return
    _kept.conn, _kept.since = conn, time.monotonic()


def _close_quietly(conn):
    try:
        conn.close()
    except DB_ERRORS:
        pass


# Every table holding one business's records, read from the schema above so a table added later is never missed
BUSINESS_TABLES = [
    re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement).group(1)
    for statement in SCHEMA if "CREATE TABLE" in statement and "business_id INTEGER" in statement
]


class Database:
    def __init__(self):
        self.untried = False
        if IS_POSTGRES:
            self.conn, self.untried = _take_connection()
        else:
            self.conn = sqlite3.connect(LOCAL_DB)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if IS_POSTGRES:
            sql = sql.replace("?", "%s")
            if self.untried:
                # The first query on a kept connection: if the server closed it while it sat unused, nothing has been
                # done on it yet, so a new connection can simply run the same query
                self.untried = False
                try:
                    return self._run(sql, params)
                except psycopg.OperationalError:
                    _close_quietly(self.conn)
                    self.conn = _connect()
        return self._run(sql, params)

    def _run(self, sql, params):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def all(self, sql, params=()):
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def insert(self, sql, params=()):
        """Runs an INSERT and returns the new row's id."""
        cur = self.execute(sql + " RETURNING id", params)
        new_id = cur.fetchone()["id"]
        cur.close()
        return new_id

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        if IS_POSTGRES:
            _keep_connection(self.conn)
        else:
            self.conn.close()


def delete_business(db, biz):
    """Deletes one business with every record and user account it has. The caller commits."""
    for table in BUSINESS_TABLES:
        db.execute(f"DELETE FROM {table} WHERE business_id=?", (biz,))
    db.execute("DELETE FROM businesses WHERE id=?", (biz,))


def add_column(db, table, column, definition):
    """Adds a column to a table created by an earlier version, if it is not there yet."""
    if IS_POSTGRES:
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
    elif column not in [row["name"] for row in db.all(f"PRAGMA table_info({table})")]:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_schema():
    db = Database()
    try:
        for statement in SCHEMA:
            db.execute(statement.replace("{pk}", PRIMARY_KEY))
        # Purchases recorded before payments were tracked are treated as paid in full
        add_column(db, "procurements", "amount_paid", "DOUBLE PRECISION")
        db.execute("UPDATE procurements SET amount_paid = total_cost WHERE amount_paid IS NULL")
        add_column(db, "invoices", "customer_id", "INTEGER")
        # A cancelled invoice is kept (unlike a deleted one): it stays visible with its original amounts, but its
        # balance owed is zeroed and it is left out of revenue, receivables and reports.
        add_column(db, "invoices", "cancelled_at", "TEXT")
        add_column(db, "invoices", "cancelled_by", "TEXT")
        add_column(db, "invoices", "cancel_reason", "TEXT")
        # Whether a purchase was also added to inventory. Every purchase before this existed went into inventory, so they count as yes.
        add_column(db, "procurements", "in_inventory", "INTEGER DEFAULT 1")
        # The supplier account a purchase belongs to, and the supplier payment a payment-log row came from
        add_column(db, "procurements", "supplier_id", "INTEGER")
        add_column(db, "payment_log", "payment_id", "INTEGER")
        # Each user's own appearance choices: colour theme and how see-through the interface is (0-100)
        add_column(db, "users", "theme", "TEXT")
        add_column(db, "users", "glass", "INTEGER")
        # Alert settings per business: the WhatsApp number and which alerts are switched on (NULL counts as on)
        # How many seeds one pack holds (seed items only); stock of a seed item is counted in packs
        add_column(db, "products", "seeds_per_pack", "INTEGER")
        # The packaging unit stock is counted in (bag, box, kg, litre...); seed items are always counted in packs
        add_column(db, "products", "unit", "TEXT")
        # Seeds left over from an opened pack after a planting; the next planting uses them first
        add_column(db, "products", "loose_seeds", "INTEGER DEFAULT 0")
        # Whether a planting is really still out there (goes on the POS, can be pre-ordered) or is a historical
        # record only - for example backfilled greenhouse history that no longer physically exists.
        add_column(db, "plantings", "still_in_greenhouse", "INTEGER NOT NULL DEFAULT 1")
        # Set once a business's earlier money records have been copied into the cashbook (see backfill_cashbook in app.py).
        # A business created after Accounts existed starts with it set, since everything it does is recorded as it happens.
        add_column(db, "businesses", "cashbook_ready", "INTEGER")
        # Backups: the hour the end-of-day reminder appears (NULL = the default, -1 = off), and the last backup made
        add_column(db, "businesses", "backup_hour", "INTEGER")
        # The last cashbook entry already recorded when a balance was counted (see balance_counts in app.py)
        add_column(db, "money_balances", "last_id", "INTEGER")
        add_column(db, "businesses", "last_backup_at", "TEXT")
        add_column(db, "businesses", "last_backup_by", "TEXT")
        # Pre-orders: the alert for batches almost ready (NULL counts as on), how many days ahead it comes (NULL = the default),
        # and when each pre-order's reminder was raised, so it is raised once
        add_column(db, "businesses", "alert_preorders", "INTEGER")
        add_column(db, "businesses", "preorder_remind_days", "INTEGER")
        add_column(db, "preorders", "reminded_at", "TEXT")
        add_column(db, "businesses", "alert_phone", "TEXT")
        add_column(db, "businesses", "alert_low_stock", "INTEGER")
        add_column(db, "businesses", "alert_permission", "INTEGER")
        # A demo company: when it ends (local time). At that moment it is deleted with all its records and logins. NULL = a real business.
        add_column(db, "businesses", "demo_expires_at", "TEXT")
        # Statutory rules became separate employee and employer rates. Settings saved under the old
        # "one rate, paid by one side" rule keep exactly the same effect.
        for column in ("napsa_employee_pct", "napsa_employer_pct", "nhima_employee_pct", "nhima_employer_pct"):
            add_column(db, "hr_settings", column, "DOUBLE PRECISION")
        add_column(db, "hr_settings", "contribution_base", "TEXT")
        db.execute(
            """UPDATE hr_settings SET
                   napsa_employee_pct = CASE WHEN napsa_bearer = 'employee' THEN napsa_pct ELSE 0 END,
                   napsa_employer_pct = CASE WHEN napsa_bearer = 'employee' THEN 0 ELSE napsa_pct END,
                   nhima_employee_pct = CASE WHEN nhima_bearer = 'employee' THEN nhima_pct ELSE 0 END,
                   nhima_employer_pct = CASE WHEN nhima_bearer = 'employee' THEN 0 ELSE nhima_pct END,
                   contribution_base = 'basic'
               WHERE napsa_employee_pct IS NULL"""
        )
        for statement in INDEXES:
            db.execute(statement)
        db.commit()
    finally:
        db.close()


# --- Start-up work, once per version of the code ---
# Creating the tables, adding columns and repairing older records only has anything to do when the code that does it has
# changed. Every check costs a round trip to the database and ALTER TABLE briefly locks its table, so a cold start skips
# all of it when the database says it already ran for this exact code: the schema, init_schema and the repairs passed in.
def startup_signature(repairs=()):
    parts = list(SCHEMA) + INDEXES
    try:
        parts += [inspect.getsource(init_schema)] + [inspect.getsource(fn) for fn in repairs]
    except (OSError, TypeError):
        return None   # the source cannot be read, so the start-up work always runs
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]


def startup_done(signature):
    if signature is None:
        return False
    db = Database()
    try:
        row = db.one("SELECT value FROM app_meta WHERE key='startup'")
        return bool(row) and row["value"] == signature
    except DB_ERRORS:
        db.rollback()   # no app_meta table yet: a new database, or one from before this existed
        return False
    finally:
        db.close()


def mark_startup_done(signature):
    if signature is None:
        return
    db = Database()
    try:
        db.execute("DELETE FROM app_meta WHERE key='startup'")
        db.execute("INSERT INTO app_meta (key, value) VALUES ('startup', ?)", (signature,))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()
