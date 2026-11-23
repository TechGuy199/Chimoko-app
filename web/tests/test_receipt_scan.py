"""Scan a receipt.

Checks that a receipt photo is kept and opened for review; that reading it with Gemini fills in the supplier and lines (matched to
inventory where they can be), and says so plainly when Gemini cannot be used; that saving logs every line as a purchase, all or none,
with the payment spread over the lines and each purchase linked to the receipt; and that a file that is not a photo or PDF is refused.
Gemini is replaced by a stand-in: no key or network is used.

Run from the web folder:   python -m unittest discover -s tests -v
It never touches the real database: the database settings are removed before the app is loaded.
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest

for setting in ("DATABASE_URL", "POSTGRES_URL", "VERCEL", "ALLOW_SIGNUP", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(setting, None)
WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEB)

import db  # noqa: E402

if "app" not in sys.modules:   # run with the other tests, the app is already loaded on their throwaway database
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "receipt-test.db")
import app as A  # noqa: E402

PASSWORD = "receipt-scan-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64   # enough for the app to know it as a PNG


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


class ReadsReceipts:
    """Gemini, for these tests: answers a receipt with the reading it is given."""

    def __init__(self, reading):
        self.reading, self.asked = reading, 0

    def turn(self, system, steps, tools, timeout=None):
        self.asked += 1
        return A.assistant.Turn([], [], "```json\n" + json.dumps(self.reading) + "\n```", 100, 50)


class ReceiptScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        cls.client.post("/setup", data={"csrf": csrf_of(cls.client, "/setup"), "mode": "real", "business_name": "Receipt Farm",
                                        "username": "receiptowner", "password": PASSWORD, "confirm": PASSWORD})
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Receipt Farm'")["id"]
        cls.urea = x.insert("INSERT INTO products (business_id, item_code, name, category, department, quantity, unit_cost, price, unit, pack_size, pack_measure) "
                            "VALUES (?, 'UREA', 'Urea 50kg', 'Fertiliser', 'General', 2, 600, 0, 'bag', 50, 'kg')", (cls.biz,))
        x.commit()
        x.close()
        cls.saved_client = A.assistant._client

    @classmethod
    def tearDownClass(cls):
        A.assistant._client = cls.saved_client

    def upload(self, read="hand", data=PNG):
        r = self.client.post("/procurements/receipt", data={"csrf": csrf_of(self.client), "read": read, "receipt": (io.BytesIO(data), "r.png")},
                             content_type="multipart/form-data")
        return r.status_code, r.get_json()

    def assistant_on(self, on):
        x = db.Database()
        try:
            x.execute("UPDATE businesses SET assistant_enabled=? WHERE id=?", (1 if on else 0, self.biz))
            x.commit()
        finally:
            x.close()

    def scan_id(self, url):
        return int(url.rstrip("/").split("/")[-1])

    def save(self, scan_id, lines, **head):
        keys = ("name", "item", "qty", "cost", "category", "department", "unit", "price", "seeds", "size", "measure", "purpose", "sale_qty")
        data = {"csrf": csrf_of(self.client), "purchase_date": A.today_str(), "payment_status": "Unpaid"}
        data.update(head)
        for k in keys:
            data["line_" + k] = [str(l.get(k, "")) for l in lines]
        return self.client.post(f"/procurements/receipt/{scan_id}", data=data, follow_redirects=True).get_data(as_text=True)

    def stock(self, code):
        x = db.Database()
        try:
            r = x.one("SELECT quantity FROM products WHERE business_id=? AND item_code=?", (self.biz, code))
            return r["quantity"] if r else None
        finally:
            x.close()

    def purchases(self, scan_id):
        x = db.Database()
        try:
            return x.all("SELECT name, quantity, unit_cost, amount_paid, in_inventory FROM procurements WHERE business_id=? AND receipt_id=? ORDER BY id", (self.biz, scan_id))
        finally:
            x.close()

    def test_1_typed_in_by_hand(self):
        status, answer = self.upload("hand")
        self.assertEqual((status, answer["ok"]), (200, True))
        page = self.client.get(answer["url"]).get_data(as_text=True)
        self.assertIn("Type in the receipt", page)
        scan = self.scan_id(answer["url"])
        image = self.client.get(f"/procurements/receipt/{scan}/image")
        self.assertEqual((image.status_code, image.mimetype, image.data), (200, "image/png", PNG))
        page = self.save(scan, [{"name": "Urea 50kg", "item": self.urea, "qty": 3, "cost": 620, "purpose": "farm"},
                                {"name": "Diesel", "item": "expense", "qty": 1, "cost": 300}])
        self.assertIn("Receipt saved: 2 purchases", page)
        self.assertEqual(self.stock("UREA"), 5)
        self.assertEqual([(p["name"], p["quantity"], p["in_inventory"]) for p in self.purchases(scan)], [("Urea 50kg", 3, 1), ("Diesel", 1, 0)])
        # Saved once: opening or saving it again does not log it twice
        self.assertIn("saved already", self.save(scan, [{"name": "Urea 50kg", "item": self.urea, "qty": 3, "cost": 620}]))
        self.assertEqual(self.stock("UREA"), 5)

    def test_2_gemini_off_says_so_and_keeps_the_photo(self):
        self.assistant_on(False)
        status, answer = self.upload("gemini")
        self.assertTrue(answer["ok"])
        self.assertIn("switched off", answer["note"])
        self.assertIn("switched off", self.client.get(answer["url"]).get_data(as_text=True))
        self.assertIn("switched off", self.client.get("/procurements/receipt").get_data(as_text=True))

    def test_3_gemini_reads_and_matches(self):
        self.assistant_on(True)
        A.assistant._client = ReadsReceipts({
            "readable": True, "supplier": "Agro Supplies", "supplier_phone": "0977000000", "receipt_no": "R-881", "date": A.today_str(),
            "payment": "Paid", "total": 2140,
            "lines": [{"name": "UREA 50KG BAG", "match": "UREA", "quantity": 2, "unit_cost": 620},
                      {"name": "Spray oil 1L", "match": "", "quantity": 3, "line_total": 300}]})
        status, answer = self.upload("gemini")
        self.assertEqual(answer["note"], "")
        page = self.client.get(answer["url"]).get_data(as_text=True)
        self.assertIn("Read by Gemini", page)
        self.assertIn('value="Agro Supplies"', page)
        self.assertIn("Matched", page)
        self.assertIn('value="100.0"', page)   # 300 for 3: worked out as 100 each
        scan = self.scan_id(answer["url"])
        lines = [{"name": "Urea 50kg", "item": self.urea, "qty": 2, "cost": 620, "purpose": "farm"},
                 {"name": "Spray Oil", "item": "new", "qty": 3, "cost": 100, "category": "Chemicals", "department": "General", "unit": "bottle",
                  "size": 1, "measure": "litres", "purpose": "farm"}]
        # Paid to a named supplier needs its reference: nothing is saved without it
        page = self.save(scan, lines, supplier_name="Agro Supplies", invoice_no="R-881", payment_status="Paid", payment_method="Cash")
        self.assertIn("Nothing was saved", page)
        self.assertEqual(self.purchases(scan), [])
        page = self.save(scan, lines, supplier_name="Agro Supplies", invoice_no="R-881", payment_status="Paid", payment_method="Cash", payment_reference="R-881")
        self.assertIn("Receipt saved: 2 purchases from Agro Supplies", page)
        self.assertEqual(self.stock("UREA"), 7)
        x = db.Database()
        try:
            oil = x.one("SELECT quantity, unit, pack_size, pack_measure FROM products WHERE business_id=? AND name='Spray Oil'", (self.biz,))
            self.assertEqual((oil["quantity"], oil["unit"], oil["pack_size"], oil["pack_measure"]), (3, "bottle", 1.0, "litres"))
            self.assertEqual(x.one("SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_payments WHERE business_id=?", (self.biz,))["n"], 1540)
        finally:
            x.close()

    def test_4_a_part_payment_goes_to_the_lines_in_order(self):
        status, answer = self.upload("hand")
        scan = self.scan_id(answer["url"])
        self.save(scan, [{"name": "Urea 50kg", "item": self.urea, "qty": 1, "cost": 600}, {"name": "Fuel", "item": "expense", "qty": 1, "cost": 400}],
                  payment_status="Partial Payment", amount_paid="700", payment_method="Cash")
        self.assertEqual([p["amount_paid"] for p in self.purchases(scan)], [600, 100])

    def test_5_a_bad_line_saves_nothing(self):
        status, answer = self.upload("hand")
        scan = self.scan_id(answer["url"])
        before = self.stock("UREA")
        page = self.save(scan, [{"name": "Urea 50kg", "item": self.urea, "qty": 1, "cost": 600}, {"name": "Mystery", "item": "", "qty": 1, "cost": 5}])
        self.assertIn("choose the inventory item it is, a new item, or an expense", page)
        page = self.save(scan, [{"name": "Seeds", "item": "new", "qty": 1, "cost": 5, "category": "Seeds"}])
        self.assertIn("seeds in one pack", page)
        self.assertEqual((self.stock("UREA"), self.purchases(scan)), (before, []))

    def test_6_only_photos_and_pdfs(self):
        status, answer = self.upload("hand", data=b"just some text")
        self.assertEqual((status, answer["ok"]), (415, False))
        status, answer = self.upload("hand", data=b"")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
