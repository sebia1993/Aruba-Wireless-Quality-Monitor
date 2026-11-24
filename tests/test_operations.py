import tempfile,unittest,time,threading,json
from pathlib import Path
from types import SimpleNamespace
from awqm.operations import Operations,OperationConflict,OperationRejected,configured_ops
from awqm.worker import ControllerWorker
from awqm.fixture_session import FixtureSession
from awqm.engine import Engine
from awqm.storage import Store
from awqm.scheduler import Scheduler,Poll
from awqm.diagnostic_web import CoordinatedVerifier
from awqm.auth import AuthBusy

class OperationTests(unittest.TestCase):
    def test_persistence_cas_and_configuration_binding(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"ops.db"
            ops=Operations(p,("w1","w2"),"site1",lambda:1000)
            a=ops.set("web:operator","w1",True,0)
            self.assertEqual(a["revision"],1);self.assertTrue(a["controllers"]["w1"]["requested_paused"])
            self.assertFalse(a["controllers"]["w2"]["requested_paused"])
            self.assertFalse(ops.set("web:operator","w1",True,1)["changed"])
            with self.assertRaises(OperationConflict):ops.set("web:operator","all",False,0)
            with self.assertRaises(OperationRejected):ops.set("web:operator","other",True,1)
            with self.assertRaises(ValueError):Operations(p,("w1","w2"),"different")
            restored=Operations(p,("w1","w2"),"site1")
            self.assertTrue(restored.paused("w1"))
            b=restored.set("local:admin","all",True,1)
            self.assertEqual(b["revision"],2)
            self.assertTrue(restored.paused("w2"))
            self.assertEqual(len(restored.recent()),2)
            self.assertEqual(restored.set("local:admin","all",False,2)["revision"],3)
    def test_lost_db_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir()
            cfg=SimpleNamespace(root=p,site="fixture",targets=[SimpleNamespace(id="w1",ip="192.0.2.1")])
            ops=configured_ops(cfg)
            ops.set("local:admin","all",True,0)
            self.assertTrue(configured_ops(cfg).paused("w1"))
            ops.path.unlink()
            with self.assertRaises(RuntimeError):ops.snapshot()
            with self.assertRaises(ValueError):configured_ops(cfg)
    def test_reconnect_preserves_budget_and_critical_latch(self):
        s=Scheduler(mode="standard");s.protection.cpu(90,10)
        s.protection.account(0,10,True);s.reconnect(11)
        self.assertEqual(s.protection.budget(11,60,True),10)
        self.assertEqual(s.protection.effective(11),"unknown")
        s.protection.cpu(20,12)
        self.assertEqual(s.protection.effective(12),"critical")
        s.protection.cpu(20,13);s.protection.cpu(20,14)
        self.assertEqual(s.protection.effective(14),"normal")
    def test_drain_then_resume(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);t=[1000];clock=lambda:t[0]
            started=threading.Event();release=threading.Event();calls=[]
            class Slow(FixtureSession):
                def run(self,name,*a,**kw):
                    calls.append(name)
                    if len(calls)==1:
                        started.set()
                        if not release.wait(5):raise RuntimeError("fixture timeout")
                    return super().run(name,*a,**kw)
            w=ControllerWorker("w1",lambda:Slow(1),clock,clock,mode="standard")
            store=Store(p/"data",mode="diagnostic")
            ops=Operations(p/"ops.db",("w1",),clock=clock)
            e=Engine([w],store,clock=clock,wall=clock)
            try:
                e.apply_operations(ops.snapshot())
                job=w.submit_detail("pending","show ap debug client-table","DEMO-AP-001")
                e.pulse();self.assertTrue(started.wait(2))
                e.apply_operations(ops.set("local:admin","all",True,0))
                r=e.pulse()
                self.assertEqual(r["operations"]["controllers"][0]["phase"],"draining")
                self.assertTrue(r["paused"]);self.assertEqual(len(calls),1)
                release.set()
                for _ in range(100):
                    r=e.pulse()
                    if r["operations"]["controllers"][0]["phase"]=="paused":break
                    time.sleep(.01)
                self.assertEqual(r["operations"]["controllers"][0]["phase"],"paused")
                self.assertIsNone(w.session);self.assertEqual(w.jobs.get(job.job_id).reason,"maintenance_context_changed")
                self.assertFalse(e.published_radios);self.assertFalse(e.inflight)
                t[0]+=300
                for _ in range(5):e.pulse()
                self.assertEqual(len(calls),1)
                e.apply_operations(ops.set("local:admin","all",False,1))
                e.pulse()
                for _ in range(100):
                    e.pulse()
                    if len(calls)>1:break
                    time.sleep(.01)
                self.assertGreater(len(calls),1)
                self.assertEqual(calls[1],"show cpuload")
                self.assertIsNone(w.snapshot())
            finally:release.set();e.close()
    def test_one_controller_paused_other_continues(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);t=[1000];clock=lambda:t[0]
            w=[ControllerWorker(f"w{i}",lambda i=i:FixtureSession(i),clock,clock,mode="standard") for i in (1,2)]
            ops=Operations(p/"ops.db",("w1","w2"),clock=clock)
            e=Engine(w,Store(p/"data",mode="diagnostic"),clock=clock,wall=clock)
            try:
                e.apply_operations(ops.set("local:admin","w1",True,0))
                for _ in range(80):
                    r=e.pulse();t[0]+=.25;time.sleep(.01)
                    if r["mapped_radios"]==2:break
                self.assertEqual(r["mapped_radios"],2)
                self.assertFalse(r["paused"])
                self.assertEqual(r["operations"]["controllers"][0]["phase"],"paused")
                self.assertIsNone(w[0].session)
                self.assertIsNotNone(w[1].session)
                e.apply_operations(None,error="ValueError")
                r=e.pulse();self.assertTrue(r["paused"])
                self.assertFalse(e.published_radios)
            finally:e.close()
    def test_personal_auth_respects_desired_pause(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);ops=Operations(p/"ops.db",("w1",),clock=lambda:1000)
            w=ControllerWorker("w1",lambda:FixtureSession(1),lambda:1000,lambda:1000)
            self.assertEqual(w.tick().status,"success")
            called=[];verifier=CoordinatedVerifier(w,SimpleNamespace(lock=threading.Lock()),lambda *a:called.append(a) or True,threading.Event(),ops)
            ops.set("local:admin","all",True,0)
            with self.assertRaises(AuthBusy):verifier("op","fixture")
            self.assertEqual(called,[]);w.close()
