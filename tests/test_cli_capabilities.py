"""Approved command inventory: no implicit field support or live expansion."""
import json
import tempfile
import unittest
from pathlib import Path
from awqm.cli_capabilities import CATALOG
from awqm.worker import ControllerWorker, Reading
from awqm.fixture_session import FixtureSession
from awqm.jobs import DETAIL_COMMANDS
from awqm.engine import Engine
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView

class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.calls = []
        calls = self.calls
        class Session(FixtureSession):
            def run(self, name, *args, **kwargs):
                calls.append(name)
                return super().run(name, *args, **kwargs)
        self.worker = ControllerWorker("w1", lambda: Session(1),
            clock=lambda: self.now, wall=lambda: self.now, mode="standard")
        self.addCleanup(self.worker.close)

    def rows(self):
        return {r["id"]: r for r in self.worker.protection_frame()["cli_capabilities"]}

    def test_catalog_exact_scope_and_no_live_expansion(self):
        rows = self.rows()
        self.assertEqual(len(rows), 33)
        self.assertEqual(len({name for _,name,_ in CATALOG}),33)
        for cid in ("D03","D04","D07","D08","D11"):
            self.assertEqual(rows[cid]["implementation"], "offline")
            self.assertFalse(rows[cid]["configured"])
            self.assertNotIn(rows[cid]["command"], DETAIL_COMMANDS)
        for cid in ("D06","D09","D10","D12","C08","C09","C10","C11","V02"):
            self.assertEqual(rows[cid]["implementation"], "pending")
        self.assertEqual(rows["D05"]["implementation"], "detail_variant")
        self.assertTrue(all(not r["field_support_verified"] for r in rows.values()))
        self.assertTrue(all(r["observation"] == "unobserved" for r in rows.values()))
        self.assertEqual(self.calls, [])

    def test_worker_observation_and_privacy_lifecycle(self):
        w = self.worker
        self.assertEqual(w.tick().status,"success")
        self.assertEqual(self.calls, ["show cpuload"])
        self.assertEqual(self.rows()["C02"]["observation"], "parsed_response")
        self.assertEqual(self.rows()["C02"]["observed_at"], self.now)
        w.outcomes["show ap radio-summary"] = Reading("show ap radio-summary",
            "permission_denied", self.now, {"raw":"SECRET"}, "PRIVATE", w.generation)
        w.disabled.add("show ap radio-summary")
        w.detail_outcomes["show ap debug client-table"] = ("unsupported",self.now,w.generation)
        w.detail_disabled.add("show ap debug client-table")
        rows = self.rows()
        self.assertEqual(rows["R01"]["observation"], "permission_denied")
        self.assertTrue(rows["R01"]["disabled_latched"])
        self.assertEqual(rows["D01"]["observation"], "detail_unavailable")
        self.assertNotIn("SECRET", json.dumps(rows))
        self.assertNotIn("PRIVATE", json.dumps(rows))
        self.assertEqual(self.calls, ["show cpuload"])
        self.now += 661
        self.assertEqual(self.rows()["C02"]["observation"], "unobserved")
        self.assertTrue(self.rows()["R01"]["disabled_latched"])
        self.now -= 661
        w.generation += 1
        self.assertEqual(self.rows()["C02"]["observation"], "unobserved")
        self.assertTrue(w.quiesce_collection(True))
        self.assertEqual(w.detail_outcomes, {})
        self.assertEqual(self.rows()["D01"]["observation"],"unobserved")
        self.assertTrue(self.rows()["D01"]["disabled_latched"])

    def test_busy_and_engine_status_no_cli(self):
        w = self.worker
        with w.lock:
            self.assertIsNone(w.protection_frame()["cli_capabilities"])
        with tempfile.TemporaryDirectory() as root:
            e = Engine([w], Store(Path(root)/"data",mode="diagnostic"),
                       clock=lambda:self.now,wall=lambda:self.now)
            try:
                row=e.operation_status()["controllers"][0]
                self.assertEqual(len(row["cli_capabilities"]),33)
                self.assertEqual(row["cli_capabilities"][0]["id"],"C01")
                self.assertEqual(self.calls,[])
            finally:
                e.close()
