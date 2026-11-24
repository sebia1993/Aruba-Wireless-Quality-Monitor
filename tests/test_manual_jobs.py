import tempfile,unittest,threading,json
from pathlib import Path
from types import SimpleNamespace
from awqm.manual_jobs import ManualReservations,ReservationRejected,load_manual_policy,COMMAND
from awqm.jobs import Jobs
from awqm.model import Radio
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from awqm.live_clients import ClientApproval
from awqm.field_validation import Approval
from awqm.audit import AuditLog

class Worker:
    def __init__(self,clock):
        self.manual_detail=True;self.live_detail=True;self.jobs=Jobs();self.clock=clock
        self.client_approval=ClientApproval("tag",900,2000,False)
        self.field_approval=Approval("w1","192.0.2.1","model","8.10.0.24","key",900,2000,"tag")
        self.detail_disabled=set()
    def submit_detail(self,request,name,target,ttl=180,binding=None):
        return self.jobs.submit(request,name,target,self.clock(),ttl,binding=binding)

class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=Path(self.temp.name);self.t=[1000];self.clock=lambda:self.t[0]
        self.store=Store(p/"data",mode="diagnostic")
        self.radio=Radio("site:02:00:00:00:00:01:0","ap","DEMO-AP","w1","5GHz",44,1000,90,5,-94,source="aos8-cli-unvalidated")
        self.store.save(self.radio)
        self.view=DiagnosticView(self.store,self.clock)
        self.view.publish({},[(self.radio.radio_id,1000)],quality={self.radio.radio_id:{"observed_at":1000,"status":"PERSISTENT","score":50,"valid_until":2000}})
        self.token=self.view.token(self.radio.radio_id);self.worker=Worker(self.clock)
        self.audit=AuditLog(p/"audit.db",self.clock);self.stop=threading.Event()
        self.broker=ManualReservations(self.view,{"w1":self.worker},self.audit,self.stop,self.clock)
    def reserve(self,user="op",req="request-0000000001",token=None):
        return self.broker.reserve(user,self.broker.epoch,req,token or self.token)
    def test_receipt_idempotency_and_owned_cancel(self):
        a=self.reserve();self.assertEqual(a["status"],"queued")
        self.assertEqual(self.reserve()["receipt_id"],a["receipt_id"])
        self.assertEqual(len(self.worker.jobs.list(1000)),1)
        self.assertEqual(self.audit.summary()["events"],1)
        self.assertNotIn(self.radio.radio_id,json.dumps(a))
        self.assertFalse(self.broker.list("other")["rows"])
        with self.assertRaises(ReservationRejected):self.broker.cancel("other",self.broker.epoch,a["receipt_id"])
        result=self.broker.cancel("op",self.broker.epoch,a["receipt_id"])
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(self.reserve()["status"],"cancelled")
        with self.assertRaises(ReservationRejected):self.reserve(req="request-0000000002")
    def test_shared_and_running_not_cancelled(self):
        auto=self.worker.submit_detail("auto-fixture",COMMAND,"DEMO-AP")
        a=self.reserve();self.assertTrue(a["joined"])
        self.assertFalse(a["can_cancel"])
        self.assertEqual(a["job_id"],auto.job_id)
        self.assertEqual(self.broker.cancel("op",self.broker.epoch,a["receipt_id"])["status"],"queued")
        self.worker.jobs.start(auto.job_id,1001)
        self.assertEqual(self.reserve()["status"],"running")
        self.worker.jobs.finish(auto.job_id,"completed","field_validation_pending",1002)
        self.assertEqual(self.reserve()["status"],"completed")
    def test_stale_or_restart_no_job(self):
        with self.assertRaises(ReservationRejected):self.broker.reserve("op","bad","request-0000000001",self.token)
        self.t[0]=1030
        with self.assertRaises(ReservationRejected):self.reserve()
        self.assertFalse(self.worker.jobs.list(self.t[0]))
    def test_manual_priority_and_aging(self):
        q=Jobs()
        old=q.submit("auto-old",COMMAND,"AP1",100,ttl=300)
        young=q.submit("auto-young",COMMAND,"AP2",220)
        manual=q.submit("manual-new",COMMAND,"AP3",221)
        self.assertEqual([x.job_id for x in q.pending(222)],[old.job_id,manual.job_id,young.job_id])
    def test_policy_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/"config").mkdir();cfg=SimpleNamespace(root=p)
            self.assertFalse(load_manual_policy(cfg))
            f=p/"config/manual-detail.json";f.write_text('{"version":1,"enabled":true}')
            with self.assertRaises(ValueError):load_manual_policy(cfg)
            f.write_text('{"version":1,"enabled":true,"authorized":true}')
            self.assertTrue(load_manual_policy(cfg))

    def test_manual_binding_and_worker_permission(self):
        from awqm.worker import ControllerWorker
        from awqm.fixture_session import FixtureSession
        from awqm.aos8 import inventory
        from awqm.manual_jobs import bind_ap
        from unittest.mock import patch
        session=FixtureSession(1)
        session.responses["show version"]="ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24"
        a=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","fixture-key",900,2000,"tag")
        w=ControllerWorker("w1",lambda:session,self.clock,self.clock,mode="standard",enable_bss=True,
            field_approval=a,live_detail=True,client_approval=ClientApproval("tag",900,1900,False),manual_detail=True)
        with patch("awqm.worker.fingerprint",return_value="fixture-key"):
            for _ in range(7):self.assertEqual(w.tick().status,"success")
            mac=inventory(session.responses["show ap database long"])[0]["mac"]
            job=w.submit_detail("manual-fixture",COMMAND,"DEMO-AP-001",binding=bind_ap(mac))
            self.assertTrue(w._live_detail_allowed(job));self.assertTrue(w._detail_target(job))
            from dataclasses import replace
            self.assertFalse(w._detail_target(replace(job,binding="0"*64)))
            w.manual_detail=False
            self.assertFalse(w._live_detail_allowed(job))
            w.manual_detail=True
            self.assertEqual(w.tick().status,"success")
            self.assertEqual(w.jobs.get(job.job_id).status,"completed")
        w.close()
    def test_shared_manual_owner_cannot_cancel(self):
        a=self.reserve();b=self.reserve(user="op2")
        self.assertTrue(b["joined"])
        self.assertFalse(self.reserve()["can_cancel"])
        self.assertEqual(self.broker.cancel("op",self.broker.epoch,a["receipt_id"])["status"],"queued")

    def test_pause_requested_rejects_new_reservation(self):
        from awqm.operations import Operations
        ops=Operations(Path(self.temp.name)/"ops.db",("w1",),clock=self.clock)
        self.broker.operations=ops
        ops.set("local:admin","all",True,0)
        with self.assertRaises(ReservationRejected):self.reserve()
        self.assertEqual(self.worker.jobs.list(self.t[0]),[])
