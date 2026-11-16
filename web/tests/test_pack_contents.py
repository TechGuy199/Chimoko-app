"""What a pack holds, and using stock by amount.

Checks that a purchase can say how an item is bought (a bag, a tin) and what one holds (50 kg, 15,000 seeds); that using an amount
takes the opened pack first, opens whole packs for the rest and keeps what is left of the last one; that planting seeds does the same
across tins; that returning a farm-use invoice undoes it exactly; and that one item code cannot switch pack size.

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
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "pack-test.db")
import app as A  # noqa: E402

PASSWORD = "pack-test-1"


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


class PackContents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        cls.client.post("/setup", data={
            "csrf": csrf_of(cls.client, "/setup"), "mode": "real", "business_name": "Pack Test Farm", "username": "packowner",
            "password": PASSWORD, "confirm": PASSWORD,
        })
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Pack Test Farm'")["id"]
        cls.planter = x.insert("INSERT INTO employees (business_id, full_name, nrc_number, employment_type) VALUES (?, 'Pack Planter', '1', 'Casual Worker')", (cls.biz,))
        x.commit()
        x.close()

    def post(self, path, **data):
        data["csrf"] = csrf_of(self.client)
        return self.client.post(path, data=data, follow_redirects=True).get_data(as_text=True)

    def buy(self, code, name, qty, **more):
        data = {"origin": "inventory", "in_inventory": ["0", "1"], "category": "General", "department": "General", "item_code": code, "name": name,
                "quantity": str(qty), "unit_cost": "100", "payment_status": "Unpaid", "purpose": "farm"}
        data.update({key: str(value) for key, value in more.items()})
        return self.post("/inventory/purchase", **data)

    def item(self, code):
        x = db.Database()
        try:
            return x.one("SELECT id, quantity, unit, pack_size, pack_measure, loose_amount, seeds_per_pack, loose_seeds FROM products WHERE business_id=? AND item_code=?", (self.biz, code))
        finally:
            x.close()

    def use(self, code, amount):
        return self.post(f"/inventory/{self.item(code)['id']}/transfer", destination="farm", amount=str(amount))

    def test_1_a_purchase_says_how_it_is_bought_and_what_a_pack_holds(self):
        page = self.buy("DCOMP", "D-Compound", 3, unit="bag", pack_size="50", pack_measure="kg")
        self.assertIn("150 kg in 3 bags of 50 kg", page)
        i = self.item("DCOMP")
        self.assertEqual((i["quantity"], i["unit"], i["pack_size"], i["pack_measure"]), (3, "bag", 50.0, "kg"))
        self.assertIn("50 kg in a bag", self.client.get("/inventory").get_data(as_text=True))
        self.assertIn("150 kg in stock", self.client.get("/inventory").get_data(as_text=True))
        # The same code cannot come in a different size, and a size needs its measure
        self.assertIn("already counted in packs of 50 kg", self.buy("DCOMP", "D-Compound", 1, pack_size="25", pack_measure="kg"))
        self.assertIn("measured in", self.buy("NEW-1", "Something", 1, pack_size="5"))
        self.assertEqual(self.item("DCOMP")["quantity"], 3)
        self.assertIsNone(self.item("NEW-1"))

    def test_2_using_an_amount_opens_packs_and_keeps_the_rest(self):
        # 70 kg from 3 bags of 50: two bags opened, 30 kg left in the second
        page = self.use("DCOMP", 70)
        self.assertIn("Used 70 kg of D-Compound: 2 bags opened; 30 kg left in the opened bag. 1 full bag remains", page)
        i = self.item("DCOMP")
        self.assertEqual((i["quantity"], i["loose_amount"]), (1, 30.0))
        # 20 kg more comes out of the opened bag; no new bag is opened
        page = self.use("DCOMP", 20)
        self.assertIn("no new one opened; 10 kg left", page)
        self.assertEqual((self.item("DCOMP")["quantity"], self.item("DCOMP")["loose_amount"]), (1, 10.0))
        # 12.5 kg: the 10 kg left, then a new bag for the other 2.5
        self.use("DCOMP", 12.5)
        self.assertEqual((self.item("DCOMP")["quantity"], self.item("DCOMP")["loose_amount"]), (0, 47.5))
        # More than there is: nothing changes
        self.assertIn("Only 47.5 kg of D-Compound is in stock", self.use("DCOMP", 48))
        self.assertEqual((self.item("DCOMP")["quantity"], self.item("DCOMP")["loose_amount"]), (0, 47.5))
        self.assertIn("47.5 kg in an opened bag", self.client.get("/inventory").get_data(as_text=True))

    def test_3_returning_a_farm_use_invoice_undoes_it_exactly(self):
        self.buy("LIME", "Lime", 2, unit="bag", pack_size="25", pack_measure="kg")
        self.use("LIME", 30)
        self.assertEqual((self.item("LIME")["quantity"], self.item("LIME")["loose_amount"]), (0, 20.0))
        x = db.Database()
        try:
            invoice_id = x.one("SELECT ii.invoice_id AS id FROM invoice_items ii WHERE ii.business_id=? AND ii.item_code='LIME' ORDER BY ii.id DESC LIMIT 1", (self.biz,))["id"]
        finally:
            x.close()
        self.post(f"/invoices/{invoice_id}/delete", reason="Entered by mistake", password=PASSWORD, return_stock="1")
        self.assertEqual((self.item("LIME")["quantity"], self.item("LIME")["loose_amount"]), (2, 0.0))

    def test_4_seeds_come_in_tins_and_planting_draws_across_them(self):
        page = self.buy("CAB", "Cabbage Seed", 2, category="Seeds", unit="tin", seeds_per_pack="15000")
        self.assertIn("30,000 seeds", page)
        self.assertEqual((self.item("CAB")["unit"], self.item("CAB")["seeds_per_pack"]), ("tin", 15000))
        # 18,000 seeds planted (90 trays of 200): one whole tin and 3,000 from a second, which keeps 12,000
        day = A.today_str()
        page = self.post(f"/planting/planter/{self.planter}", date=day, item_id=str(self.item("CAB")["id"]), trays="90", holes="200", mortality_pct="10",
                         ready_on=A.add_days(day, 30))
        self.assertIn("18,000 seeds, 2 tins", page)
        self.assertEqual((self.item("CAB")["quantity"], self.item("CAB")["loose_seeds"]), (0, 12000))
        # Farm use by the seed takes from the opened tin
        page = self.use("CAB", 2000)
        self.assertIn("no new one opened; 10,000 seeds left in the opened tin", page)
        self.assertEqual((self.item("CAB")["quantity"], self.item("CAB")["loose_seeds"]), (0, 10000))
        self.assertIn("whole number", self.use("CAB", 10.5))

    def test_5_the_item_page_sets_or_clears_what_a_pack_holds(self):
        self.buy("OIL", "Spray Oil", 4, unit="bottle")
        self.assertIn("not measured yet", self.use("OIL", 1))
        item_id = self.item("OIL")["id"]
        self.post(f"/inventory/{item_id}/update", name="Spray Oil", unit_cost="100", price="100", unit="bottle", pack_size="1", pack_measure="litres")
        self.assertEqual((self.item("OIL")["pack_size"], self.item("OIL")["pack_measure"]), (1.0, "litres"))
        self.use("OIL", 2.5)
        self.assertEqual((self.item("OIL")["quantity"], self.item("OIL")["loose_amount"]), (1, 0.5))
        self.assertIn("0.5 litres in an opened bottle", self.client.get(f"/inventory/{item_id}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
