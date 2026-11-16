"""Kinds of tray, each with its own pay.

Checks that kinds of tray (Big, Small...) are set up under HR Settings with their pay; that a planting entry says which kind was
planted; that a person is paid for their trays by kind (2 Big at 0.75 and 1 Small at 0.25 is 1.75); that trays entered before kinds
existed are still paid at the one usual rate; and that a payslip keeps the rates it paid at when the rates change afterwards.

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
    db.LOCAL_DB = os.path.join(tempfile.mkdtemp(), "tray-types-test.db")
import app as A  # noqa: E402

PASSWORD = "tray-types-test-1"


def csrf_of(client, path="/appearance"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


class TrayTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = A.app.test_client()
        cls.client.post("/setup", data={
            "csrf": csrf_of(cls.client, "/setup"), "mode": "real", "business_name": "Tray Kind Farm", "username": "traykindowner",
            "password": PASSWORD, "confirm": PASSWORD,
        })
        x = db.Database()
        cls.biz = x.one("SELECT id FROM businesses WHERE name='Tray Kind Farm'")["id"]
        cls.mary = x.insert("INSERT INTO employees (business_id, full_name, nrc_number, employment_type, pay_schedule) VALUES (?, 'Mary Kind', '1', 'Casual Worker', 'manual')", (cls.biz,))
        cls.seed = x.insert(
            "INSERT INTO products (business_id, item_code, name, category, department, quantity, unit_cost, price, seeds_per_pack, loose_seeds, unit) "
            "VALUES (?, 'TOM', 'Tomato Seed', 'Seeds', 'General', 100, 100, 0, 1000, 0, 'pack')", (cls.biz,))
        x.commit()
        x.close()
        cls.day = A.today_str()

    def post(self, path, **data):
        data["csrf"] = csrf_of(self.client)
        return self.client.post(path, data=data, follow_redirects=True).get_data(as_text=True)

    def plant(self, entries, day=None):
        day = day or self.day
        data = {"date": day}
        for key in ("item_id", "trays", "holes", "mortality_pct", "ready_on", "tray_type"):
            data[key] = [str(entry.get(key, "")) for entry in entries]
        return self.post(f"/planting/planter/{self.mary}", **data)

    def entry(self, trays, kind="", holes="", **more):
        return dict({"item_id": self.seed, "trays": trays, "tray_type": kind, "holes": holes, "mortality_pct": 10, "ready_on": A.add_days(self.day, 30)}, **more)

    def kinds(self):
        x = db.Database()
        try:
            return {t["name"]: t for t in x.all("SELECT id, name, holes, rate, active FROM tray_types WHERE business_id=?", (self.biz,))}
        finally:
            x.close()

    def owed(self):
        return self.client.get(f"/hr/trays?employee_id={self.mary}&pay_date={self.day}").get_json()

    def test_1_before_kinds_every_tray_is_paid_at_the_one_rate(self):
        self.post("/hr/settings/trays", holes_per_tray="100", tray_rate="0.5")
        self.assertIn("Saved for Mary Kind", self.plant([self.entry(4, holes=100)], day=A.add_days(self.day, -1)))
        owed = self.owed()
        self.assertEqual((owed["trays"], owed["amount"]), (4.0, 2.0))

    def test_2_kinds_are_set_up_under_hr_settings(self):
        page = self.post("/hr/tray-types", kind_id=["", "", ""], kind_name=["Big", "Small", ""], kind_holes=["200", "128", ""], kind_rate=["0.75", "0.25", ""])
        self.assertIn("Kinds of tray saved", page)
        kinds = self.kinds()
        self.assertEqual({name: (k["holes"], k["rate"]) for name, k in kinds.items()}, {"Big": (200, 0.75), "Small": (128, 0.25)})
        self.assertIn('value="Big"', self.client.get("/hr/settings").get_data(as_text=True))
        # Two kinds cannot share a name
        page = self.post("/hr/tray-types", kind_id=[str(kinds["Big"]["id"]), str(kinds["Small"]["id"])], kind_name=["Big", "big"], kind_holes=["200", "128"], kind_rate=["0.75", "0.25"])
        self.assertIn("both called", page)
        # The planter's form now offers them
        form = self.client.get(f"/planting/planter/{self.mary}").get_data(as_text=True)
        self.assertIn('name="tray_type"', form)
        self.assertIn("Small", form)

    def test_3_each_person_is_paid_by_the_kind_of_tray(self):
        kinds = self.kinds()
        # An entry must now say which kind
        self.assertIn("choose the kind of tray", self.plant([self.entry(2, holes=200)]))
        # 2 Big and 1 Small of the same seed; the holes come from the kind
        page = self.plant([self.entry(2, kind=kinds["Big"]["id"]), self.entry(1, kind=kinds["Small"]["id"])])
        self.assertIn("2 Big trays of Tomato Seed (400 seeds", page)
        self.assertIn("1 Small tray of Tomato Seed (128 seeds", page)
        owed = self.owed()
        # 4 trays from before at 0.5, 2 Big at 0.75, 1 Small at 0.25
        self.assertEqual((owed["trays"], owed["amount"]), (7.0, 3.75))
        self.assertEqual([(k["name"], k["trays"], k["rate"], k["amount"]) for k in owed["kinds"]], [("", 4.0, 0.5, 2.0), ("Big", 2.0, 0.75, 1.5), ("Small", 1.0, 0.25, 0.25)])
        self.assertIn("1.50 due", self.client.get(f"/planting/planter/{self.mary}").get_data(as_text=True).replace("ZMW ", ""))

    def test_4_a_payslip_pays_by_kind_and_keeps_its_rates(self):
        page = self.post("/hr/payslips/new", employee_id=str(self.mary), pay_date=self.day, pay_basis="trays", payment_method="Cash")
        self.assertIn("Payslip saved for Mary Kind", page)
        self.assertIn("Big trays", page)
        x = db.Database()
        try:
            slip = x.one("SELECT id, trays, calculated_basic FROM payroll WHERE business_id=? AND employee_id=? ORDER BY id DESC LIMIT 1", (self.biz, self.mary))
            self.assertEqual((slip["trays"], slip["calculated_basic"]), (7.0, 3.75))
            paid = x.all("SELECT tray_type, trays, type_rate FROM planting_trays WHERE business_id=? AND paid_payslip_id=? ORDER BY id", (self.biz, slip["id"]))
            self.assertEqual([(r["tray_type"], r["trays"], r["type_rate"]) for r in paid], [(None, 4.0, 0.5), ("Big", 2.0, 0.75), ("Small", 1.0, 0.25)])
        finally:
            x.close()
        self.assertEqual(self.owed()["trays"], 0)
        # Big goes up to 1.00: what was paid stays at 0.75, new Big trays earn 1.00
        kinds = self.kinds()
        self.post("/hr/tray-types", kind_id=[str(kinds["Big"]["id"]), str(kinds["Small"]["id"])], kind_name=["Big", "Small"], kind_holes=["200", "128"], kind_rate=["1", "0.25"])
        self.plant([self.entry(3, kind=kinds["Big"]["id"])])
        owed = self.owed()
        self.assertEqual((owed["trays"], owed["amount"]), (3.0, 3.0))
        x = db.Database()
        try:
            self.assertEqual(x.one("SELECT type_rate FROM planting_trays WHERE business_id=? AND paid_payslip_id IS NOT NULL AND tray_type='Big'", (self.biz,))["type_rate"], 0.75)
        finally:
            x.close()

    def test_5_a_kind_in_use_is_retired_not_lost(self):
        kinds = self.kinds()
        # Small is removed (its name cleared): the trays planted in it keep their kind; it is no longer offered
        self.post("/hr/tray-types", kind_id=[str(kinds["Big"]["id"]), str(kinds["Small"]["id"])], kind_name=["Big", ""], kind_holes=["200", "128"], kind_rate=["1", "0.25"])
        self.assertEqual(self.kinds()["Small"]["active"], 0)
        form = self.client.get(f"/planting/planter/{self.mary}").get_data(as_text=True)
        self.assertNotIn(f'<option value="{kinds["Small"]["id"]}"', form)
        self.assertIn("choose the kind of tray", self.plant([self.entry(1, kind=kinds["Small"]["id"])]))

    def test_6_the_full_form_gives_each_person_a_kind_too(self):
        kinds = self.kinds()
        page = self.post("/planting", item_id=str(self.seed), holes_per_tray="200", mortality_pct="10", planted_on=A.add_days(self.day, -3),
                         ready_on=A.add_days(self.day, 30), planter_id=[str(self.mary)], planter_trays=["5"], planter_type=[str(kinds["Big"]["id"])],
                         still_in_greenhouse=["0", "1"])
        self.assertIn("Planting recorded", page)
        owed = self.owed()
        self.assertEqual((owed["trays"], owed["amount"]), (8.0, 8.0))   # 3 Big from before and these 5, at 1.00


if __name__ == "__main__":
    unittest.main()
