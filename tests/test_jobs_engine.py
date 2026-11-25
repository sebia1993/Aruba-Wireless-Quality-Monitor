import tempfile,unittest,time
from threading import Event
from awqm.jobs import Jobs
from awqm.engine import Engine
from awqm.storage import Store
from awqm.worker import ControllerWorker
from test_worker import setup,Session
from test_client import fixture

class QueueTests(unittest.TestCase):
    def test_reservation_idempotency_cancel_expire(self):
        q=Jobs(limit=1)
        j=q.submit("req1","show ap debug client-table","AP",0)
        self.assertEqual(q.submit("req1","show ap debug client-table","AP",1).job_id,j.job_id)
        self.assertEqual(q.submit("req2","show ap debug client-table","AP",2).job_id,j.job_id)
        self.assertEqual(q.by_request("req2").job_id,j.job_id)
        with self.assertRaises(ValueError):q.submit("req1","show ap debug client-table","Other",1)
        with self.assertRaises(RuntimeError):q.submit("req3","show ap debug client-table","Other",1)
        self.assertEqual(q.cancel(j.job_id,3).status,"cancelled")
        a=q.submit("req4","show ap debug client-table","AP",4,ttl=1)
        self.assertEqual(q.pending(5),[])
        self.assertEqual(q.get(a.job_id).status,"expired")
    def test_worker_details_same_scheduler(self):
        clock,session,w=setup()
        # Existing fixture session has no target argument; adapt for detail only.
        original=session.run
        def run(name,timeout=30,target=None,idle_timeout=10):
            if name=="show ap debug client-table":
                self.assertEqual(target,"DEMO-AP")
                return fixture()
            return original(name,timeout)
        session.run=run
        j=w.submit_detail("r1","show ap debug client-table","DEMO-AP")
        for _ in range(5):self.assertEqual(w.tick().status,"success")
        self.assertEqual(w.jobs.get(j.job_id).status,"queued")
        self.assertEqual(w.tick().status,"success")
        self.assertEqual(w.jobs.get(j.job_id).status,"completed")
        self.assertEqual(len(w.detail_latest),1)
        # CPU safety protection overrides requested detail.
        j=w.submit_detail("r2","show ap debug client-table","DEMO-AP")
        w.schedule.protection.cpu(85,clock.t)
        self.assertIsNone(w.tick())
        self.assertEqual(w.jobs.get(j.job_id).reason,"controller_protection")
    def test_target_not_observed(self):
        c,s,w=setup()
        for _ in range(5):w.tick()
        j=w.submit_detail("r","show ap debug client-table","OTHER")
        self.assertIsNone(w.tick())
        self.assertEqual(w.jobs.get(j.job_id).reason,"target_anchor_unverified")

class EngineTests(unittest.TestCase):
    def test_independent_worker_and_single_writer(self):
        c,s,w=setup()
        # Real-time execution uses a monotonic clock; advance the injected
        # fixture clock between pulses while no device is contacted.
        gate=Event()
        class Slow(Session):
            def open(self):
                gate.wait(3)
        slow=ControllerWorker("W2",lambda:Slow({}),c.now,c.now,mode="standard")
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic")
            e=Engine([w,slow],store,clock=c.now,wall=c.now)
            try:
                for _ in range(30):
                    e.pulse()
                    time.sleep(.02)
                    c.t+=1
                    if store.snapshot()["radios"]:break
                self.assertEqual(len(store.snapshot()["radios"]),2)
                self.assertEqual(e.last_result["coherent_controllers"],1)
                self.assertFalse(e.last_result["field_validated"])
                self.assertEqual(store.pending_count(),0)
            finally:
                gate.set()
                e.close()
    def test_store_error_blocks_dispatch(self):
        c,s,w=setup()
        for _ in range(5):w.tick()
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic");e=Engine([w],store,clock=c.now,wall=c.now)
            try:
                e.snapshots[w.controller]=w.snapshot()
                original=store.save_batch
                def fail(rows):raise OSError("fixture disk error")
                store.save_batch=fail
                r=e.pulse()
                self.assertEqual(r["storage_error"],"OSError")
                self.assertEqual(e.inflight,{})
                store.save_batch=original
                e.recover_storage()
                self.assertIsNone(e.pulse()["storage_error"])
                self.assertEqual(len(store.snapshot()["radios"]),2)
            finally:e.close()
