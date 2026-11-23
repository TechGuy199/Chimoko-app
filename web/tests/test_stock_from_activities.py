"""Products used in daily activities come out of inventory.

Checks that a product logged on an activity that is an inventory item is taken out of stock by its measure (kg, litres) or in
whole units; that one logged for everyone on a plot is taken once; that removing the activities puts it back; that something not in
inventory is only noted; and that a quantity that cannot be taken (wrong unit, not enough stock) saves nothing.

Run from the web folder:   python -m unittest discover -s tests -v
It never touches the real database: the database settings are removed before the app is loaded.
"""
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
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "activity-stock-test.db")
import app as A  # noqa: E402

PASSWORD = "activity-stock-1"


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


class ActivityStock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        cls.client.post("/setup", data={"csrf": csrf_of(cls.client, "/setup"), "mode": "real", "business_name": "Activity Stock Farm",
                                        "username": "actstockowner", "password": PASSWORD, "confirm": PASSWORD})
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Activity Stock Farm'")["id"]
        add = "INSERT INTO employees (business_id, full_name, nrc_number, employment_type) VALUES (?, ?, ?, 'Casual Worker')"
        cls.ann, cls.ben = x.insert(add, (cls.biz, "Ann Field", "1")), x.insert(add, (cls.biz, "Ben Field", "2"))
        cls.plot = x.insert("INSERT INTO plots (business_id, department, name, active) VALUES (?, 'General', 'Block A', 1)", (cls.biz,))
        for e in (cls.ann, cls.ben):
            x.insert("INSERT INTO plot_workers (business_id, plot_id, employee_id) VALUES (?, ?, ?)", (cls.biz, cls.plot, e))
        item = ("INSERT INTO products (business_id, item_code, name, category, department, quantity, unit_cost, price, unit, pack_size, pack_measure, loose_amount) "
                "VALUES (?, ?, ?, 'Chemicals', 'General', ?, 100, 0, ?, ?, ?, 0)")
        x.insert(item, (cls.biz, "UREA", "Urea", 2, "bag", 50, "kg"))
        x.insert(item, (cls.biz, "NOZ", "Sprayer nozzle", 5, "piece", None, None))
        x.commit()
        x.close()

    def log(self, *rows):
        data = {"csrf": csrf_of(self.client), "date": A.today_str()}
        for key in ("worker", "period", "description", "tools", "product", "product_qty", "product_unit", "work_done"):
            data[key] = [str(r.get(key, {"period": "morning", "description": "Fed the tomatoes"}.get(key, ""))) for r in rows]
        return self.client.post(f"/activities/plots/{self.plot}/log", data=data, follow_redirects=True).get_data(as_text=True)

    def stock(self, code):
        x = db.Database()
        try:
            r = x.one("SELECT quantity, loose_amount FROM products WHERE business_id=? AND item_code=?", (self.biz, code))
            return (r["quantity"], r["loose_amount"] or 0.0)
        finally:
            x.close()

    def entries(self):
        x = db.Database()
        try:
            return x.all("SELECT id, worker_name, product, stock_line_id FROM activities WHERE business_id=? ORDER BY id", (self.biz,))
        finally:
            x.close()

    def remove(self, entry_id):
        return self.client.post(f"/activities/{entry_id}/delete", data={"csrf": csrf_of(self.client)}, follow_redirects=True).get_data(as_text=True)

    def test_1_a_measured_product_comes_out_by_the_kg(self):
        page = self.log({"worker": self.ann, "product": "Urea", "product_qty": "70", "product_unit": "kg"})
        self.assertIn("Taken out of inventory: 70 kg of Urea (30 kg left)", page)
        self.assertEqual(self.stock("UREA"), (0, 30.0))
        self.assertIn("From stock", page)
        # Removing the activity puts it back
        page = self.remove(self.entries()[-1]["id"])
        self.assertIn("went back into inventory", page)
        self.assertEqual(self.stock("UREA"), (2, 0.0))

    def test_2_for_everyone_on_the_plot_it_is_taken_once(self):
        page = self.log({"worker": "all", "product": "urea", "product_qty": "10", "product_unit": "KG"})
        self.assertIn("2 entries, one for each worker", page)
        self.assertEqual(self.stock("UREA"), (1, 40.0))
        both = [e for e in self.entries() if e["stock_line_id"]]
        self.assertEqual(len(both), 2)
        self.assertEqual(both[0]["stock_line_id"], both[1]["stock_line_id"])
        # One removed: the other still used it, so nothing comes back; both removed: it does
        self.remove(both[0]["id"])
        self.assertEqual(self.stock("UREA"), (1, 40.0))
        self.remove(both[1]["id"])
        self.assertEqual(self.stock("UREA"), (2, 0.0))

    def test_3_whole_units_and_whole_bags(self):
        self.log({"worker": self.ben, "product": "NOZ", "product_qty": "2", "product_unit": "pieces"}, {"worker": self.ben, "product": "Urea", "product_qty": "1", "product_unit": "bags"})
        self.assertEqual(self.stock("NOZ"), (3, 0.0))
        self.assertEqual(self.stock("UREA"), (1, 0.0))
        for e in [e for e in self.entries() if e["stock_line_id"]]:
            self.remove(e["id"])
        self.assertEqual((self.stock("NOZ"), self.stock("UREA")), ((5, 0.0), (2, 0.0)))

    def test_4_what_cannot_be_taken_saves_nothing(self):
        before = len(self.entries())
        self.assertIn("give the quantity in bags or kg, not litres", self.log({"worker": self.ann, "product": "Urea", "product_qty": "5", "product_unit": "litres"}))
        self.assertIn("give a whole number", self.log({"worker": self.ann, "product": "NOZ", "product_qty": "1.5"}))
        page = self.log({"worker": self.ann, "product": "Urea", "product_qty": "500", "product_unit": "kg"},
                        {"worker": self.ann, "product": "NOZ", "product_qty": "1"})
        self.assertIn("Only 100 kg of Urea is in stock", page)
        self.assertIn("Nothing was saved", page)
        self.assertEqual((self.stock("UREA"), self.stock("NOZ"), len(self.entries())), ((2, 0.0), (5, 0.0), before))

    def test_5_something_not_in_inventory_is_only_noted(self):
        page = self.log({"worker": self.ann, "product": "Borrowed manure", "product_qty": "3", "product_unit": "wheelbarrows"})
        self.assertIn("Saved 1 activity", page)
        self.assertNotIn("Taken out of inventory", page)
        self.assertIsNone(self.entries()[-1]["stock_line_id"])


if __name__ == "__main__":
    unittest.main()
