"""Database access. Postgres when DATABASE_URL is set (Vercel), a local SQLite file otherwise.

Queries are written once with ? placeholders and must work on both engines, so keep
them to plain SQL: no literal % or ? characters, and always alias aggregates.
"""
import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
IS_POSTGRES = bool(DATABASE_URL)
LOCAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_dev.db")

if IS_POSTGRES:
    import psycopg
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
]


class Database:
    def __init__(self):
        if IS_POSTGRES:
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(LOCAL_DB)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if IS_POSTGRES:
            sql = sql.replace("?", "%s")
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
        self.conn.close()


def init_schema():
    db = Database()
    try:
        for statement in SCHEMA:
            db.execute(statement.replace("{pk}", PRIMARY_KEY))
        db.commit()
    finally:
        db.close()
