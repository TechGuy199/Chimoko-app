"""Proves that one business can never see or change another business's records.

Two businesses are made in a throwaway database: Alpha, filled with records of every kind, and Beta, empty. Signed in as
Beta's admin, the test then goes through every page and every action of the app, pointing each one at Alpha's records:
    - no page or download (CSV, Excel, ZIP, PDF) may show any of Alpha's names;
    - no action (save, pay, delete, merge, reset, import...) may change a single row of Alpha's data;
    - Beta deleting its whole business leaves Alpha exactly as it was.
A new page whose address takes a number this test does not know about makes it fail, so it is never skipped by accident.

Run from the web folder:   python -m unittest discover -s tests -v
It never touches the real database: the database settings are removed before the app is loaded.
"""
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
import zlib

for setting in ("DATABASE_URL", "POSTGRES_URL", "VERCEL", "ALLOW_SIGNUP", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(setting, None)
WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEB)

import db  # noqa: E402

db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "separation-test.db")
import app as A  # noqa: E402

PASSWORD = "separation-test-1"

# Which table the number in an address points into, by the address's parameter name
PARAM_TABLES = {
    "invoice_id": "invoices", "customer_id": "customers", "supplier_id": "suppliers", "item_id": "products",
    "purchase_id": "procurements", "planting_id": "plantings", "employee_id": "employees", "payslip_id": "payroll",
    "pos_id": "pos_products", "po_id": "preorders", "request_id": "permission_requests", "notification_id": "notifications",
    "payment_id": "supplier_payments", "category_id": "categories", "dept_id": "departments", "plot_id": "plots",
    "op_id": "users", "admin_id": "users", "action_id": "assistant_actions",
}
# "entry_id" means a different table on different pages
ENTRY_TABLES = {"assign_account_entry": "cashbook", "delete_account_entry": "cashbook", "delete_activity": "activities"}
# Pages that are not about a business's records, or that sign in, out or register. Changing Beta's own password is left out
# too, so the password-confirmed actions after it can still be reached with the same password.
# Pages whose address also needs the customer's invoices (?invoice=...)
QUERY_INVOICES = {"customer_delivery", "customer_delivery_pdf"}
# Pages that find their records from the query alone: a delivery list (?invoice=... or ?day=...) and who bought a product (?code=...)
QUERY_PAGES = {"delivery_list", "delivery_list_pdf", "item_sales", "item_sales_csv"}
SKIP = {"static", "login", "logout", "setup", "pwa_manifest", "pwa_service_worker", "delete_my_login", "delete_my_business", "change_password"}
# Confirmation phrases, so the reset pages really run (for Beta) instead of stopping at the confirmation
PHRASES = {"reset_app": A.RESET_PHRASE, "clear_inventory": A.CLEAR_INVENTORY_PHRASE}


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def register(name, username):
    client = A.app.test_client()
    client.post("/setup", data={
        "csrf": csrf_of(client, "/setup"), "mode": "real", "business_name": name, "username": username,
        "password": PASSWORD, "confirm": PASSWORD,
    })
    return client


def readable(data):
    """Everything a response holds as text: the body, and inside Excel/ZIP files and compressed PDF streams too."""
    parts = [data.decode("utf-8", "ignore")]
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                parts += [readable(archive.read(name)) for name in archive.namelist()]
        except zipfile.BadZipFile:
            pass
    if data[:4] == b"%PDF":
        for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
            try:
                parts.append(zlib.decompress(stream).decode("latin-1", "ignore"))
            except zlib.error:
                pass
    return "\n".join(parts)


def form_fields():
    """Every form field name the app reads anywhere, so each action gets a full form and goes as deep as it can."""
    with open(os.path.join(WEB, "app.py"), encoding="utf-8") as source:
        code = source.read()
    names = set(re.findall(r'\b(?:text|checked|number|day_or_none|named_amounts)\(\s*"(\w+)"', code))
    names |= set(re.findall(r'request\.(?:form|args)\.(?:get|getlist)\(\s*"(\w+)"', code))
    return names


def value_for(name, alpha_ids):
    """A believable value for a form field. Anything that names a record gets one of Alpha's, which Beta must not reach."""
    today = datetime.date.today().isoformat()
    if name in ("ids", "id") or name.endswith("_id") or name in ("first", "second", "keep", "remove", "target", "into", "merge_into", "invoice"):
        return [str(i) for i in alpha_ids[:3]] if name == "ids" else str(alpha_ids[0])
    if "date" in name or name in ("planted_on", "ready_on", "paid_on", "day", "start", "end", "from", "to", "as_of", "work_date"):
        return today
    if name in ("pay_month", "month"):
        return today[:7]
    if name in ("password", "current_password"):
        return PASSWORD
    if name in ("new_password", "confirm_password", "confirm"):
        return "separation-test-2"
    if name == "payment_method":
        return "Cash"
    if name == "payment_status":
        return "Paid"
    if name == "destination":
        return "pos"
    if name == "role":
        return "operator"
    if name == "decision":
        return "approve"
    if name == "cart":
        return json.dumps([{"id": alpha_ids[0], "qty": 1}])
    if name in ("theme",):
        return "dark"
    return "1"


class StandInGemini:
    """Gemini, for the tests: answers "ok" to everything, so no key or network is ever used. A test can have it ask for look-ups first."""

    def __init__(self):
        self.ask, self.results = None, []

    def turn(self, system, steps, tools):
        if self.ask is not None and not any(s.get("type") == "function_result" for s in steps):
            made = [{"id": f"c{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(self.ask(tools))]
            return A.assistant.Turn([{"type": "function_call", "id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in made], made, "", 1, 1)
        self.results = [json.loads(s["result"][0]["text"]) for s in steps if s.get("type") == "function_result"]
        return A.assistant.Turn([{"type": "model_output", "content": [{"type": "text", "text": "ok"}]}], [], "ok", 1, 1)

    def transcribe(self, wav):
        return "hello"

    def speak(self, text):
        return A.assistant.as_wav(b"\x00\x00" * 240, None, 24000)


class BusinessSeparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alpha = register("Alpha Isolation Farm", "alphaowner")
        cls.beta = register("Beta Isolation Shop", "betaowner")
        x = db.Database()
        cls.alpha_id = x.one("SELECT id FROM businesses WHERE name='Alpha Isolation Farm'")["id"]
        cls.beta_id = x.one("SELECT id FROM businesses WHERE name='Beta Isolation Shop'")["id"]
        alpha_admin = x.one("SELECT id FROM users WHERE username='alphaowner'")["id"]
        with A.app.test_request_context():
            A.g.user = {"id": alpha_admin, "business_id": cls.alpha_id, "username": "alphaowner", "role": "admin"}
            A.fill_demo_data(x, cls.alpha_id)
            cls.add_every_other_record(x, cls.alpha_id)
        # Both businesses use the assistant (so its pages and tools are really exercised), with Gemini replaced by a stand-in
        x.execute("UPDATE businesses SET assistant_enabled=1 WHERE id IN (?, ?)", (cls.alpha_id, cls.beta_id))
        A.assistant._client = StandInGemini()
        x.commit()
        # What Beta must never see: every name Alpha has given to anything
        cls.markers = {"Alpha Isolation Farm", "alphaowner", "alpha-operator", "alpha-second-admin", "Alpha secret note"}
        for table, column in (("customers", "name"), ("suppliers", "name"), ("products", "name"), ("employees", "full_name"),
                              ("plantings", "name"), ("plots", "name")):
            cls.markers |= {r["v"] for r in x.all(f"SELECT {column} AS v FROM {table} WHERE business_id=?", (cls.alpha_id,))}
        x.close()

    @staticmethod
    def add_every_other_record(x, biz):
        """Records the sample data does not make, so every kind of page has one of Alpha's to point at."""
        from werkzeug.security import generate_password_hash
        stamp = A.now().strftime("%Y-%m-%d %H:%M:%S")
        today = A.today_str()
        hashed = generate_password_hash(PASSWORD)
        x.insert("INSERT INTO users (business_id, username, password_hash, role) VALUES (?, 'alpha-operator', ?, 'operator')", (biz, hashed))
        x.insert("INSERT INTO users (business_id, username, password_hash, role) VALUES (?, 'alpha-second-admin', ?, 'admin')", (biz, hashed))
        item = x.one("SELECT id, name FROM products WHERE business_id=? ORDER BY id LIMIT 1", (biz,))
        planting = x.one("SELECT id FROM plantings WHERE business_id=? ORDER BY id LIMIT 1", (biz,))
        employee = x.one("SELECT id, full_name FROM employees WHERE business_id=? ORDER BY id LIMIT 1", (biz,))
        x.insert("""INSERT INTO notifications (business_id, kind, title, body, link, created_at) VALUES (?, 'low_stock', 'Alpha secret note', 'x', '/', ?)""", (biz, stamp))
        x.insert("""INSERT INTO permission_requests (business_id, user_id, username, action, item_id, item_name, reason, status, created_at)
                    VALUES (?, 1, 'alpha-operator', 'delete', ?, ?, 'Alpha secret note', 'pending', ?)""", (biz, item["id"], item["name"], stamp))
        x.insert("""INSERT INTO preorders (business_id, planting_id, customer_name, qty, unit_price, status, note, created_at)
                    VALUES (?, ?, 'Alpha secret note', 5, 2, 'pending', 'Alpha secret note', ?)""", (biz, planting["id"], stamp))
        x.insert("""INSERT INTO deleted_invoices (business_id, invoice_no, total, reason, deleted_by, deleted_at)
                    VALUES (?, 'INV-ALPHA-DEL', 10, 'Alpha secret note', 'alphaowner', ?)""", (biz, stamp))
        x.insert("""INSERT INTO stock_adjustments (business_id, kind, item_code, item_name, quantity_before, quantity_after, delta, reason, created_at)
                    VALUES (?, 'inventory_edit', 'X', ?, 5, 4, -1, 'Alpha secret note', ?)""", (biz, item["name"], stamp))
        # What Alpha has said to the assistant, and a change it prepared and left waiting: Beta must not read or confirm either
        alpha_user = x.one("SELECT id FROM users WHERE business_id=? AND username='alphaowner'", (biz,))["id"]
        x.insert("INSERT INTO assistant_messages (business_id, user_id, role, content, created_at) VALUES (?, ?, 'user', 'Alpha secret note', ?)", (biz, alpha_user, stamp))
        x.insert("""INSERT INTO assistant_actions (business_id, user_id, kind, request, summary, status, created_at, expires_at)
                    VALUES (?, ?, 'handover', ?, '{"title": "Alpha secret note", "rows": [["Alpha secret note", "x"]]}', 'pending', ?, '2999-01-01 00:00:00')""",
                 (biz, alpha_user, '{"path": "/invoices/1/delivered", "form": {}, "perm": "sell"}', stamp))
        plot = x.insert("INSERT INTO plots (business_id, department, name, notes, created_at) VALUES (?, 'Greenhouse', 'Alpha Block 7', 'Alpha secret note', ?)", (biz, stamp))
        x.insert("INSERT INTO plot_workers (business_id, plot_id, employee_id) VALUES (?, ?, ?)", (biz, plot, employee["id"]))
        # A sale to deliver today (so the day's delivery list has Alpha's customers on it), with a discounted line
        sale = x.one("SELECT id FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled' ORDER BY id LIMIT 1", (biz,))
        x.execute("UPDATE invoices SET deliver_on=? WHERE id=?", (today, sale["id"]))
        x.execute("UPDATE invoice_items SET discount=0.5 WHERE id=(SELECT MIN(id) FROM invoice_items WHERE invoice_id=?)", (sale["id"],))
        x.insert("""INSERT INTO activities (business_id, plot_id, plot_name, department, work_date, employee_id, worker_name, period, description, recorded_at)
                    VALUES (?, ?, 'Alpha Block 7', 'Greenhouse', ?, ?, ?, 'morning', 'Alpha secret note', ?)""", (biz, plot, today, employee["id"], employee["full_name"], stamp))

    def snapshot(self):
        """Every row Alpha has, in every table, plus its business and users."""
        x = db.Database()
        try:
            rows = {t: x.all(f"SELECT * FROM {t} WHERE business_id=? ORDER BY id", (self.alpha_id,)) for t in db.BUSINESS_TABLES if t != "hr_settings"}
            rows["hr_settings"] = x.all("SELECT * FROM hr_settings WHERE business_id=?", (self.alpha_id,))
            rows["businesses"] = x.all("SELECT * FROM businesses WHERE id=?", (self.alpha_id,))
            return json.loads(json.dumps(rows, default=str))
        finally:
            x.close()

    def alpha_ids(self, table):
        x = db.Database()
        try:
            ids = [r["id"] for r in x.all(f"SELECT id FROM {table} WHERE business_id=? ORDER BY id", (self.alpha_id,))]
        finally:
            x.close()
        self.assertTrue(ids, f"Alpha has no {table} rows: add one in add_every_other_record so its pages are tested")
        return ids

    def urls_for(self, rule):
        """The rule's address filled in with Alpha's records (or every valid word, for the backup and template downloads)."""
        if rule.endpoint in QUERY_PAGES:
            return self.query_urls(rule.endpoint)
        if not rule.arguments:
            return [rule.rule]
        values = {}
        for name in rule.arguments:
            if name == "kind":
                values[name] = list(A.IMPORT_TEMPLATES)
            elif name == "key":
                values[name] = [key for key, _, _ in A.BACKUP_BOOKS]
            elif name == "table":
                values[name] = A.backup_tables()
            else:
                table = ENTRY_TABLES.get(rule.endpoint) if name == "entry_id" else PARAM_TABLES.get(name)
                self.assertIsNotNone(table, f"{rule.rule}: the test does not know which table <{name}> points into. Add it to PARAM_TABLES.")
                if name == "op_id":
                    values[name] = [self.alpha_user("alpha-operator")]
                elif name == "admin_id":
                    values[name] = [self.alpha_user("alpha-second-admin")]
                else:
                    values[name] = self.alpha_ids(table)[:1]
        urls = [{}]
        for name, options in values.items():
            urls = [dict(u, **{name: o}) for u in urls for o in options]
        # A delivery list is made from invoices ticked on the customer's statement: the address carries that customer's invoices
        if rule.endpoint in QUERY_INVOICES:
            for u in urls:
                u["invoice"] = self.customer_invoices(u["customer_id"])
        with A.app.test_request_context():
            return [A.url_for(rule.endpoint, **u) for u in urls]

    def query_urls(self, endpoint):
        """A page that finds its records from the query, pointed at Alpha's: its sales, its delivery day, a product it has sold."""
        x = db.Database()
        try:
            if endpoint.startswith("delivery_list"):
                sales = [r["id"] for r in x.all(
                    "SELECT id FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled' ORDER BY id LIMIT 5", (self.alpha_id,)
                )]
                day = x.one("SELECT deliver_on FROM invoices WHERE business_id=? AND deliver_on IS NOT NULL", (self.alpha_id,))["deliver_on"]
                queries = [{"invoice": sales}, {"day": day}]
            else:
                code = x.one("SELECT item_code FROM invoice_items WHERE business_id=? AND COALESCE(item_code, '') <> '' ORDER BY id LIMIT 1", (self.alpha_id,))
                self.assertIsNotNone(code, "Alpha has sold nothing with a code: add a sale so who bought it is tested")
                queries = [{"code": code["item_code"]}]
        finally:
            x.close()
        with A.app.test_request_context():
            return [A.url_for(endpoint, **q) for q in queries]

    def customer_invoices(self, customer_id):
        x = db.Database()
        try:
            ids = [r["id"] for r in x.all(
                "SELECT id FROM invoices WHERE business_id=? AND customer_id=? AND kind='SALE' AND payment_status <> 'Cancelled' ORDER BY id", (self.alpha_id, customer_id)
            )]
        finally:
            x.close()
        self.assertTrue(ids, "Alpha's first customer has no invoices: add one so the delivery list pages are tested")
        return ids

    def alpha_user(self, username):
        x = db.Database()
        try:
            return x.one("SELECT id FROM users WHERE username=? AND business_id=?", (username, self.alpha_id))["id"]
        finally:
            x.close()

    def rules(self, method):
        return sorted((r for r in A.app.url_map.iter_rules() if method in r.methods and r.endpoint not in SKIP), key=lambda r: r.rule)

    def leaks_in(self, response):
        text = readable(response.get_data())
        return sorted(m for m in self.markers if m and m in text)

    def test_1_alpha_can_open_its_own_records(self):
        """Without this the other tests could pass for the wrong reason (Alpha's addresses not pointing at anything)."""
        failures = []
        for rule in self.rules("GET"):
            if not any(a not in ("kind", "key", "table") for a in rule.arguments) and rule.endpoint not in QUERY_PAGES:
                continue
            for url in self.urls_for(rule):
                status = self.alpha.get(url).status_code
                # Opening an alert marks it read and forwards to what it is about
                if status != 200 and not (rule.endpoint == "open_notification" and status == 302):
                    failures.append(f"{url} -> {status}")
        self.assertEqual(failures, [], "Alpha could not open some of its own records, so those pages are not really being tested")
        self.assertTrue(set(self.leaks_in(self.alpha.get("/customers"))), "Alpha's names were not found on Alpha's own page")
        # ...and the pages found from the query really show Alpha's records to Alpha, so Beta seeing none of them means something
        for endpoint in ("delivery_list", "item_sales"):
            for url in self.query_urls(endpoint):
                self.assertTrue(self.leaks_in(self.alpha.get(url)), f"{url} showed Alpha none of its own records")

    def test_2_beta_sees_nothing_of_alpha(self):
        found = []
        id_args = {name: value_for(name, [self.alpha_ids("invoices")[0]]) for name in ("id",)}
        for rule in self.rules("GET"):
            for url in self.urls_for(rule):
                for query in ({}, id_args):
                    if "?" in url:   # an address that already has a query: the extra ones go on the end
                        response = self.beta.get(url + "".join(f"&{k}={v}" for k, v in query.items()))
                    else:
                        response = self.beta.get(url, query_string=query)
                    leaked = self.leaks_in(response)
                    if leaked:
                        found.append(f"GET {url} {query or ''} -> {response.status_code} shows {leaked[:5]}")
        self.assertEqual(found, [], "Beta could see Alpha's records:\n" + "\n".join(found))

    def test_3_beta_changes_nothing_of_alpha(self):
        before = self.snapshot()
        fields = form_fields()
        crashes, changed = [], []
        for rule in self.rules("POST"):
            any_alpha = self.alpha_ids("invoices") if rule.endpoint.startswith("invoice") else None
            for url in self.urls_for(rule):
                # Every id-like field points at an Alpha record of the table the page is about (or a few, for "ids")
                table = next((PARAM_TABLES.get(a) for a in rule.arguments if a in PARAM_TABLES), None)
                ids = any_alpha or self.alpha_ids(table or guess_table(rule.endpoint))
                data = {name: value_for(name, ids) for name in fields}
                data["csrf"] = csrf_of(self.beta)
                if rule.endpoint in PHRASES:
                    data["phrase"] = PHRASES[rule.endpoint]
                response = self.beta.post(url, data=data)
                if response.status_code >= 500:
                    crashes.append(f"POST {url} -> {response.status_code}")
                after = self.snapshot()
                if after != before:
                    tables = sorted(t for t in after if after[t] != before[t])
                    changed.append(f"POST {url} changed Alpha's {tables}")
                    before = after
        self.assertEqual(changed, [], "Beta changed Alpha's records:\n" + "\n".join(changed))
        self.assertEqual(crashes, [], "These actions crashed when pointed at another business's records:\n" + "\n".join(crashes))

    def test_assistant_tools_stay_inside_their_business(self):
        """Beta's admin has Gemini ask every look-up, using Alpha's names to search for, and Alpha's invoice numbers: nothing of Alpha's may come back.
        (An error may repeat the words that were searched for, so only answers that found something are read.)"""
        x = db.Database()
        try:
            numbers = [r["invoice_no"] for r in x.all("SELECT invoice_no FROM invoices WHERE business_id=? LIMIT 5", (self.alpha_id,))]
        finally:
            x.close()
        terms = sorted(m for m in self.markers if m and " " in m or len(m) > 3)[:12]
        searches = ("query", "product", "supplier", "employee", "customer", "plot_or_work", "worker", "batch")

        def ask(tools):
            made = []
            for t in tools:
                props = set(t["parameters"]["properties"])
                for term in terms:
                    args = {key: term for key in searches if key in props}
                    if "invoice_no" in props:
                        args = {"invoice_no": numbers[0] if numbers else "INV-1"}
                    if args:
                        made.append((t["name"], args))
                    if "invoice_no" in props:
                        break
                if not props & set(searches) | {"invoice_no"}:
                    made.append((t["name"], {}))
            return made

        stand_in = A.assistant._client
        stand_in.ask = ask
        try:
            response = self.beta.post("/assistant/chat", data={"csrf": csrf_of(self.beta), "message": "look at everything"})
        finally:
            stand_in.ask = None
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertGreater(len(stand_in.results), 40, "Gemini was not given enough look-ups for this test to mean anything")
        answered = [r for r in stand_in.results if r.get("ok")]
        self.assertTrue(answered, "no look-up answered, so none could have leaked")
        leaked = sorted({m for m in self.markers if m for r in answered if m in json.dumps(r["data"], default=str)})
        self.assertEqual(leaked, [], "Beta's assistant looked up Alpha's records: " + ", ".join(leaked))
        self.assertEqual(self.alpha_assistant_state(), "Alpha secret note", "Alpha's chat changed")

    def alpha_assistant_state(self):
        x = db.Database()
        try:
            return x.one("SELECT content FROM assistant_messages WHERE business_id=? ORDER BY id LIMIT 1", (self.alpha_id,))["content"]
        finally:
            x.close()

    def test_4_beta_deleting_its_business_leaves_alpha_alone(self):
        before = self.snapshot()
        client = register("Gamma Isolation Co", "gammaowner")
        response = client.post("/account/delete-business", data={"csrf": csrf_of(client), "phrase": A.DELETE_BUSINESS_PHRASE, "password": PASSWORD})
        self.assertEqual(response.headers.get("Location"), "/login")
        self.assertEqual(self.snapshot(), before, "Deleting another business changed Alpha's records")
        x = db.Database()
        try:
            self.assertIsNone(x.one("SELECT id FROM businesses WHERE name='Gamma Isolation Co'"))
        finally:
            x.close()


def guess_table(endpoint):
    """The records a page without a number in its address works on, for the ids its form sends."""
    for word, table in (("customer", "customers"), ("supplier", "suppliers"), ("categor", "categories"), ("department", "departments"),
                        ("employee", "employees"), ("payslip", "payroll"), ("plot", "plots"), ("activit", "activities"), ("operator", "users"),
                        ("admin", "users"), ("planting", "plantings"), ("account", "cashbook"), ("purchase", "products"), ("import", "products"),
                        ("checkout", "pos_products"), ("inventory", "products")):
        if word in endpoint:
            return table
    return "invoices"


if __name__ == "__main__":
    unittest.main(verbosity=2)
