"""Fixture-driven AP detail target revalidation and binding regression."""
import unittest
from dataclasses import replace
from unittest.mock import patch
from awqm.ap_detail_target import current_ap_binding, binding_from_physical_id
from awqm.field_validation import Approval
from awqm.fixture_session import FixtureSession
from awqm.live_clients import ClientApproval
from awqm.jobs import Jobs
from awqm.worker import ControllerWorker

COMMAND = "show ap debug client-table"
ACTIVE = "show ap active details"
INVENTORY = "show ap database long"
AP = "DEMO-AP-001"
MAC = "02:00:00:00:00:01"
BIND = binding_from_physical_id("site:"+MAC)


class APDetailTargetTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.calls = []
        calls = self.calls
        class Session(FixtureSession):
            def run(self, name, *args, **kwargs):
                calls.append(name)
                return super().run(name, *args, **kwargs)
        session = Session(1)
        session.responses["show version"] = "ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24"
        self.w = ControllerWorker("w1", lambda: session, lambda: self.now, lambda: self.now,
            mode="standard", enable_bss=True, live_detail=True,
            field_approval=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","fixture-key",900,2000,"tag"),
            client_approval=ClientApproval("tag",900,1900,True))
        self.addCleanup(self.w.close)
        p = patch("awqm.worker.fingerprint", return_value="fixture-key")
        p.start()
        self.addCleanup(p.stop)
        for _ in range(7):
            self.assertEqual(self.w.tick().status, "success")
        self.assertEqual(current_ap_binding(self.w, AP, self.now), BIND)

    def change(self, command, **changes):
        rd = replace(self.w.latest[command], **changes)
        self.w.latest[command] = rd
        self.w.outcomes[command] = rd

    def test_unbound_or_changed_job_never_sends_detail(self):
        for binding in (None, "0"*64):
            job = self.w.submit_detail("wrong-"+str(binding), COMMAND, AP, binding=binding)
            self.assertIsNone(self.w.tick())
            self.assertEqual(self.w.jobs.get(job.job_id).reason, "target_anchor_unverified")
            self.assertNotIn(COMMAND, self.calls)
            self.w.jobs.cancel(job.job_id, self.now)
        job = self.w.submit_detail("correct", COMMAND, AP, binding=BIND)
        self.assertEqual(self.w.tick().status, "success")
        self.assertEqual(self.w.jobs.get(job.job_id).status, "completed")
        self.assertEqual(self.calls.count(COMMAND), 1)

    def test_duplicate_name_ip_mac_and_non_aac_are_unusable(self):
        rd = self.w.latest[INVENTORY]
        row = rd.data[0]
        for extra in (dict(row,ip="192.0.2.222",mac="02:00:00:00:00:02"),
                      dict(row,name="Other",mac="02:00:00:00:00:02"),
                      dict(row,name="Other",ip="192.0.2.222")):
            with self.subTest(extra=extra):
                self.change(INVENTORY,data=[row,extra])
                self.assertIsNone(current_ap_binding(self.w, AP, self.now))
        self.w.latest[INVENTORY] = self.w.outcomes[INVENTORY] = rd
        active = self.w.latest[ACTIVE].data[0]
        self.change(ACTIVE,data=[active,dict(active,name="Other")])
        self.assertIsNone(current_ap_binding(self.w, AP, self.now))
        self.change(ACTIVE,data=[dict(active,roles=["SA"])])
        self.assertIsNone(current_ap_binding(self.w, AP, self.now))

    def test_stale_failed_generation_and_replacement(self):
        original = self.w.latest[INVENTORY]
        self.change(INVENTORY,observed_at=339)
        self.assertIsNone(current_ap_binding(self.w, AP, self.now))
        self.w.latest[INVENTORY] = self.w.outcomes[INVENTORY] = original
        self.w.outcomes[INVENTORY] = replace(original,status="parse_error",data=None)
        self.assertIsNone(current_ap_binding(self.w, AP, self.now))
        self.w.outcomes[INVENTORY] = original
        self.w.generation += 1
        self.assertIsNone(current_ap_binding(self.w, AP, self.now))
        self.w.generation -= 1
        self.change(INVENTORY,data=[dict(original.data[0],mac="02:00:00:00:00:09")])
        job = self.w.submit_detail("old-physical",COMMAND,AP,binding=BIND)
        self.assertIsNone(self.w.tick())
        self.assertEqual(self.w.jobs.get(job.job_id).reason,"target_anchor_unverified")
        self.assertNotIn(COMMAND,self.calls)

    def test_canonical_id_and_coalescence(self):
        for bad in ("",MAC,"site:bad",None):
            with self.assertRaises(ValueError):
                binding_from_physical_id(bad)
        q=Jobs()
        old=q.submit("legacy",COMMAND,AP,1000)
        joined=q.submit("bound",COMMAND,AP,1000,binding=BIND)
        self.assertEqual(old.job_id,joined.job_id)
        self.assertEqual(q.get(old.job_id).binding,BIND)
        with self.assertRaises(ValueError):
            q.submit("other",COMMAND,AP,1000,binding="0"*64)
        q2=Jobs()
        old=q2.submit("legacy",COMMAND,AP,1000)
        q2.start(old.job_id,1000)
        with self.assertRaises(ValueError):
            q2.submit("bound",COMMAND,AP,1000,binding=BIND)
        q2.finish(old.job_id,"completed","fixture",1001)
        with self.assertRaises(ValueError):
            q2.submit("legacy",COMMAND,AP,1001,binding=BIND)
