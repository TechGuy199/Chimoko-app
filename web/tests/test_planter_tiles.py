"""Planting by planter: the tiles on Planting Records and each planter's day form.

Checks that only the people who plant trays get a tile, that a day's entries take their seeds out of inventory, that an entry for an
item already planted that day joins that batch (each person's trays recorded against it), that the date can be changed, and that a
form with any mistake saves nothing.

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
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "planter-test.db")
import app as A  # noqa: E402

PASSWORD = "planter-test-1"


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


class PlanterTiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        cls.client.post("/setup", data={
            "csrf": csrf_of(cls.client, "/setup"), "mode": "real", "business_name": "Planter Test Farm", "username": "planterowner",
            "password": PASSWORD, "confirm": PASSWORD,
        })
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Planter Test Farm'")["id"]
        add = "INSERT INTO employees (business_id, full_name, nrc_number, employment_type, plants) VALUES (?, ?, ?, ?, ?)"
        cls.mary = x.insert(add, (cls.biz, "Mary Planter", "111/11/1", "Casual Worker", None))     # casual, never set: plants
        cls.ruth = x.insert(add, (cls.biz, "Ruth Planter", "222/22/2", "Permanent", 1))            # permanent, ticked: plants
        cls.john = x.insert(add, (cls.biz, "John Driver", "333/33/3", "Permanent", None))          # permanent, never set: does not
        cls.anna = x.insert(add, (cls.biz, "Anna Office", "444/44/4", "Casual Worker", 0))         # casual, unticked: does not
        item = ("INSERT INTO products (business_id, item_code, name, category, department, quantity, unit_cost, price, seeds_per_pack, loose_seeds) "
                "VALUES (?, ?, ?, 'Seeds', 'Greenhouse', ?, ?, 0, ?, 0)")
        cls.tomato = x.insert(item, (cls.biz, "TOM-SEED", "Tomato Seed", 10, 100.0, 1000))
        cls.cabbage = x.insert(item, (cls.biz, "CAB-SEED", "Cabbage Seed", 1, 50.0, 500))
        x.commit()
        x.close()

    def day_csrf(self, employee_id, day=None):
        return csrf_of(self.client, f"/planting/planter/{employee_id}" + (f"?date={day}" if day else ""))

    def save(self, employee_id, day, entries):
        data = {"csrf": self.day_csrf(employee_id, day), "date": day}
        for key in ("item_id", "trays", "holes", "mortality_pct", "ready_on"):
            data[key] = [str(entry.get(key, "")) for entry in entries]
        return self.client.post(f"/planting/planter/{employee_id}", data=data, follow_redirects=True).get_data(as_text=True)

    def stock(self, item_id):
        x = db.Database()
        try:
            return x.one("SELECT quantity, loose_seeds FROM products WHERE id=?", (item_id,))
        finally:
            x.close()

    def test_1_only_planters_get_tiles(self):
        page = self.client.get("/planting").get_data(as_text=True)
        self.assertIn("Mary Planter", page)
        self.assertIn("Ruth Planter", page)
        self.assertNotIn("John Driver", page)
        self.assertNotIn("Anna Office", page)
        self.assertIn(f"/planting/planter/{self.mary}", page)

    def test_2_entries_take_seeds_and_join_the_days_batch(self):
        day = A.today_str()
        page = self.save(self.mary, day, [
            {"item_id": self.tomato, "trays": 2, "holes": 200, "mortality_pct": 10, "ready_on": A.add_days(day, 30)},
            {"item_id": self.cabbage, "trays": 1, "holes": 100, "mortality_pct": 5, "ready_on": A.add_days(day, 20)},
        ])
        self.assertIn("Saved for Mary Planter", page)
        # 400 tomato seeds: one pack of 1,000 opened, 600 kept loose; 100 cabbage seeds: one pack of 500, 400 loose
        self.assertEqual(self.stock(self.tomato), {"quantity": 9, "loose_seeds": 600})
        self.assertEqual(self.stock(self.cabbage), {"quantity": 0, "loose_seeds": 400})

        # Ruth plants tomatoes the same day in the same trays: her trays join Mary's batch, from the loose seeds first
        page = self.save(self.ruth, day, [{"item_id": self.tomato, "trays": 3, "holes": 200}])
        self.assertIn("added to that day&#39;s batch", page)
        self.assertEqual(self.stock(self.tomato), {"quantity": 9, "loose_seeds": 0})
        x = db.Database()
        try:
            batches = x.all("SELECT * FROM plantings WHERE business_id=? AND seed_item_id=?", (self.biz, self.tomato))
            self.assertEqual(len(batches), 1)
            b = batches[0]
            self.assertEqual((b["seeds_planted"], b["trays"], b["holes_per_tray"], b["packs_used"], b["seedlings"]), (1000, 5.0, 200, 1, 900))
            trays = x.all("SELECT employee_name, trays FROM planting_trays WHERE planting_id=? ORDER BY id", (b["id"],))
            self.assertEqual([(t["employee_name"], t["trays"]) for t in trays], [("Mary Planter", 2.0), ("Ruth Planter", 3.0)])
        finally:
            x.close()
        tiles = self.client.get("/planting").get_data(as_text=True)
        self.assertEqual(tiles.count("3 trays today"), 2)   # Mary: 2 of tomato and 1 of cabbage; Ruth: 3 of tomato

    def test_3_another_day_and_a_bad_entry_saves_nothing(self):
        yesterday = A.add_days(A.today_str(), -1)
        before = self.stock(self.tomato)
        page = self.save(self.mary, yesterday, [
            {"item_id": self.tomato, "trays": 1, "holes": 200, "mortality_pct": 10, "ready_on": A.add_days(yesterday, 30)},
            {"item_id": self.cabbage, "trays": 50, "holes": 100, "mortality_pct": 5, "ready_on": A.add_days(yesterday, 20)},   # more than in stock
        ])
        self.assertIn("Nothing was saved", page)
        self.assertEqual(self.stock(self.tomato), before)
        page = self.save(self.mary, yesterday, [{"item_id": self.tomato, "trays": 1, "holes": 200, "mortality_pct": 10, "ready_on": A.add_days(yesterday, 30)}])
        self.assertIn(f"Saved for Mary Planter on {yesterday}", page)
        x = db.Database()
        try:
            self.assertEqual(x.one("SELECT COUNT(*) AS n FROM plantings WHERE business_id=? AND planted_on=?", (self.biz, yesterday))["n"], 1)
        finally:
            x.close()
        # The day page lists what was entered for that day
        page = self.client.get(f"/planting/planter/{self.mary}?date={yesterday}").get_data(as_text=True)
        self.assertIn("Tomato seedlings", page)

    def test_4_a_new_batch_needs_its_mortality_and_ready_date(self):
        page = self.save(self.ruth, A.add_days(A.today_str(), -2), [{"item_id": self.tomato, "trays": 1, "holes": 200}])
        self.assertIn("expected mortality", page)
        self.assertIn("Nothing was saved", page)

    def test_5_someone_who_does_not_plant_cannot_be_given_trays(self):
        page = self.save(self.john, A.today_str(), [{"item_id": self.tomato, "trays": 1, "holes": 200, "mortality_pct": 10, "ready_on": A.add_days(A.today_str(), 30)}])
        self.assertIn("is not set to plant trays", page)

    def test_6_the_hr_form_sets_who_plants(self):
        csrf = csrf_of(self.client, f"/hr/employees/{self.john}/edit")
        self.client.post(f"/hr/employees/{self.john}/edit", data={
            "csrf": csrf, "full_name": "John Driver", "nrc_number": "333/33/3", "employment_type": "Permanent", "basic_salary": "1000",
            "pay_schedule": "manual", "expected_days_week": "5", "plants": ["0", "1"],
        })
        self.assertIn("John Driver", self.client.get("/planting").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
