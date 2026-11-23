"""Planting entries made offline.

Checks that the device's snapshot carries what the offline planting form needs, that a planter's day uploaded from a device is
recorded exactly once however often it is sent, and that entries the app cannot accept are turned down with the reason and save nothing.

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
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "offline-planting-test.db")
import app as A  # noqa: E402

PASSWORD = "offline-planting-1"


class OfflinePlanting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        token = re.search(r'name="csrf" value="([^"]+)"', cls.client.get("/setup").get_data(as_text=True)).group(1)
        cls.client.post("/setup", data={"csrf": token, "mode": "real", "business_name": "Offline Planting Farm", "username": "offplantowner",
                                        "password": PASSWORD, "confirm": PASSWORD})
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Offline Planting Farm'")["id"]
        cls.mary = x.insert("INSERT INTO employees (business_id, full_name, nrc_number, employment_type) VALUES (?, 'Mary Offline', '1', 'Casual Worker')", (cls.biz,))
        cls.seed = x.insert(
            "INSERT INTO products (business_id, item_code, name, category, department, quantity, unit_cost, price, seeds_per_pack, loose_seeds, unit) "
            "VALUES (?, 'TOM', 'Tomato Seed', 'Seeds', 'General', 5, 100, 0, 1000, 0, 'tin')", (cls.biz,))
        x.commit()
        x.close()

    def snapshot(self):
        return self.client.get("/offline/snapshot").get_json()

    def upload(self, key, trays, **more):
        snap = self.snapshot()
        day = A.today_str()
        data = {"csrf": snap["csrf"], "upload_key": key, "employee_id": str(self.mary), "date": day, "item_id": str(self.seed), "trays": str(trays), "holes": "200",
                "tray_type": "", "mortality_pct": "10", "ready_on": A.add_days(day, 30)}
        data.update(more)
        return self.client.post(snap["urls"]["plantings"], data=data)

    def stock(self):
        x = db.Database()
        try:
            return x.one("SELECT quantity, loose_seeds FROM products WHERE id=?", (self.seed,))
        finally:
            x.close()

    def test_1_the_snapshot_carries_the_planting_form(self):
        planting = self.snapshot()["planting"]
        self.assertEqual(planting["planters"], [{"id": self.mary, "name": "Mary Offline"}])
        self.assertEqual([(s["name"], s["packs"], s["size"], s["usable"]) for s in planting["seeds"]], [("Tomato Seed", 5, 1000, True)])
        self.assertEqual(planting["kinds"], [])

    def test_2_an_upload_is_recorded_once_however_often_it_is_sent(self):
        first = self.upload("offline-planting-key-1", 3)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()["ok"])
        self.assertIn("3 trays of Tomato Seed", first.get_json()["message"])
        self.assertEqual(self.stock(), {"quantity": 4, "loose_seeds": 400})   # 600 seeds: one tin opened, 400 kept
        again = self.upload("offline-planting-key-1", 3).get_json()
        self.assertTrue(again["ok"] and again["again"])
        self.assertEqual(self.stock(), {"quantity": 4, "loose_seeds": 400})
        x = db.Database()
        try:
            self.assertEqual(x.one("SELECT COALESCE(SUM(trays), 0) AS n FROM planting_trays WHERE business_id=? AND employee_id=?", (self.biz, self.mary))["n"], 3)
        finally:
            x.close()

    def test_3_entries_the_app_cannot_accept_are_turned_down_and_save_nothing(self):
        short = self.upload("offline-planting-key-2", 500)   # 100,000 seeds: far more than in stock
        self.assertEqual(short.status_code, 409)
        self.assertIn("Only 4,400 seeds of Tomato Seed are in stock (4 tins", short.get_json()["error"])
        self.assertEqual(self.stock(), {"quantity": 4, "loose_seeds": 400})
        self.assertEqual(self.upload("bad key!", 1).status_code, 400)
        gone = self.upload("offline-planting-key-3", 1, employee_id="999999")
        self.assertIn("no longer in HR", gone.get_json()["error"])
        # Put right, the same key goes through
        self.assertTrue(self.upload("offline-planting-key-2", 2).get_json()["ok"])
        self.assertEqual(self.stock(), {"quantity": 4, "loose_seeds": 0})


if __name__ == "__main__":
    unittest.main()
