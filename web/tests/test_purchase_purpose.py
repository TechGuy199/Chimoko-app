"""What a purchase is for: farm use, for sale, or both.

Checks that farm-use stock stays in inventory only, that what is for sale goes straight on the POS, that a purchase for both is split
by quantity, that the item remembers what it is bought for, and that a purchase which cannot be split as asked saves nothing.

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
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "purpose-test.db")
import app as A  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

PASSWORD = "purpose-test-1"


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


class PurchasePurpose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        cls.client.post("/setup", data={
            "csrf": csrf_of(cls.client, "/setup"), "mode": "real", "business_name": "Purpose Test Farm", "username": "purposeowner",
            "password": PASSWORD, "confirm": PASSWORD,
        })
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Purpose Test Farm'")["id"]
        # An operator who can log purchases but cannot move stock to the POS
        x.insert("INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete) VALUES (?, 'purposebuyer', ?, 'operator', 0, 1, 0, 0)",
                 (cls.biz, generate_password_hash(PASSWORD)))
        x.commit()
        x.close()
        cls.buyer = A.app.test_client()
        cls.buyer.post("/login", data={"csrf": csrf_of(cls.buyer, "/login"), "username": "purposebuyer", "password": PASSWORD})

    def buy(self, code, qty, client=None, **more):
        client = client or self.client
        data = {
            "csrf": csrf_of(client), "origin": "inventory", "in_inventory": ["0", "1"], "category": "General", "department": "General",
            "item_code": code, "name": f"Item {code}", "quantity": str(qty), "unit_cost": "10", "payment_status": "Unpaid",
        }
        data.update({key: str(value) for key, value in more.items()})
        return client.post("/inventory/purchase", data=data, follow_redirects=True).get_data(as_text=True)

    def stock(self, code):
        """(in inventory, on the POS, what it is for, the POS price) - None where there is no such row."""
        x = db.Database()
        try:
            item = x.one("SELECT quantity, purpose FROM products WHERE business_id=? AND item_code=?", (self.biz, code))
            listed = x.one("SELECT quantity, price FROM pos_products WHERE business_id=? AND item_code=?", (self.biz, code))
            return (item["quantity"] if item else None, listed["quantity"] if listed else None, item["purpose"] if item else None,
                    listed["price"] if listed else None)
        finally:
            x.close()

    def last_purchase(self, code):
        x = db.Database()
        try:
            return x.one("SELECT quantity, sale_qty FROM procurements WHERE business_id=? AND item_code=? ORDER BY id DESC LIMIT 1", (self.biz, code))
        finally:
            x.close()

    def test_1_farm_use_stays_in_inventory_only(self):
        page = self.buy("FARM-1", 50, purpose="farm")
        self.assertIn("added to inventory", page)
        self.assertEqual(self.stock("FARM-1"), (50, None, "farm", None))
        self.assertEqual(self.last_purchase("FARM-1"), {"quantity": 50, "sale_qty": 0})
        self.assertNotIn("Item FARM-1", self.client.get("/").get_data(as_text=True))          # not on the POS
        self.assertIn("Item FARM-1", self.client.get("/inventory?use=farm").get_data(as_text=True))
        self.assertNotIn("Item FARM-1", self.client.get("/inventory?use=sale").get_data(as_text=True))

    def test_2_for_sale_goes_on_the_pos(self):
        page = self.buy("SALE-1", 20, purpose="sale", price="25")
        self.assertIn("20 for sale, now on the POS", page)
        self.assertEqual(self.stock("SALE-1"), (0, 20, "sale", 25.0))
        self.assertEqual(self.last_purchase("SALE-1"), {"quantity": 20, "sale_qty": 20})
        self.assertIn("Item SALE-1", self.client.get("/").get_data(as_text=True))
        self.assertIn("+ 20 on the POS", self.client.get("/inventory?use=sale").get_data(as_text=True))

    def test_3_both_is_split_and_the_item_remembers(self):
        page = self.buy("BOTH-1", 50, purpose="both", sale_qty=30, price="18")
        self.assertIn("30 for sale, now on the POS, and 20 for farm use", page)
        self.assertEqual(self.stock("BOTH-1"), (20, 30, "both", 18.0))
        # A restock adds to each side, keeps the price when none is given, and the form offers the same share again
        self.buy("BOTH-1", 10, purpose="both", sale_qty=4)
        self.assertEqual(self.stock("BOTH-1"), (26, 34, "both", 18.0))
        form = self.client.get("/inventory/new?code=BOTH-1").get_data(as_text=True)
        self.assertIn("&#34;share&#34;: 0.4", form)
        # An item bought for the farm and later for sale is for both
        self.buy("FARM-2", 5, purpose="farm")
        self.buy("FARM-2", 5, purpose="sale", price="12")
        self.assertEqual(self.stock("FARM-2"), (5, 5, "both", 12.0))

    def test_4_a_purchase_that_cannot_be_split_saves_nothing(self):
        self.assertIn("Enter the selling price", self.buy("BAD-1", 10, purpose="sale"))                           # new, for sale, no price
        self.assertIn("must be between 0 and", self.buy("BAD-1", 10, purpose="both", sale_qty=11, price="5"))     # more for sale than bought
        self.assertIn("how many of them are for sale", self.buy("BAD-1", 10, purpose="both", price="5"))          # both, but no number
        self.assertEqual(self.stock("BAD-1"), (None, None, None, None))
        self.assertIsNone(self.last_purchase("BAD-1"))

    def test_5_seeds_are_never_for_sale(self):
        page = self.buy("SEED-1", 3, category="Seeds", seeds_per_pack="100", purpose="sale", price="5")
        self.assertIn("Seed packs are not sold as they are", page)
        self.assertEqual(self.stock("SEED-1"), (None, None, None, None))
        self.buy("SEED-1", 3, category="Seeds", seeds_per_pack="100", purpose="farm")
        self.assertEqual(self.stock("SEED-1"), (3, None, "farm", None))

    def test_6_a_form_that_does_not_say_works_as_before(self):
        self.buy("OLD-1", 7)
        self.assertEqual(self.stock("OLD-1"), (7, None, None, None))
        self.assertEqual(self.last_purchase("OLD-1"), {"quantity": 7, "sale_qty": None})

    def test_7_an_account_that_cannot_use_the_pos_buys_for_the_farm_only(self):
        self.assertIn("cannot put stock on the POS", self.buy("OP-1", 5, client=self.buyer, purpose="sale", price="9"))
        self.assertEqual(self.stock("OP-1"), (None, None, None, None))
        self.buy("OP-1", 5, client=self.buyer, purpose="farm")
        self.assertEqual(self.stock("OP-1"), (5, None, "farm", None))

    def test_8_the_item_page_sets_what_it_is_for(self):
        x = db.Database()
        try:
            item_id = x.one("SELECT id FROM products WHERE business_id=? AND item_code='OLD-1'", (self.biz,))["id"]
        finally:
            x.close()
        self.client.post(f"/inventory/{item_id}/update", data={"csrf": csrf_of(self.client), "name": "Item OLD-1", "unit_cost": "10", "price": "10", "purpose": "farm"})
        self.assertEqual(self.stock("OLD-1")[2], "farm")


if __name__ == "__main__":
    unittest.main()
