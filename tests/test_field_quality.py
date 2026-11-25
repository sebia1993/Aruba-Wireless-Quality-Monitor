import tempfile,unittest,json,base64,hashlib
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from awqm.field_validation import Approval,load_approvals,CHECKS,version_identity,fingerprint
from awqm.aos8 import ControllerSnapshot,ParseError
from awqm.quality_ledger import QualityLedger
from awqm.storage import Store
from awqm.model import Radio
from awqm.worker import ControllerWorker
from awqm.fixture_session import FixtureSession
from awqm.diagnostic_view import DiagnosticView

FIRMWARE="8.10.0.24"
MODEL="Aruba7240XM"
KEY="SHA256:"+base64.b64encode(hashlib.sha256(b"fictional-key").digest()).decode().rstrip("=")
VERSION_TEXT=f"Aruba Operating System Software.\nArubaOS (MODEL: {MODEL}), Version {FIRMWARE}\n"

class ApprovalTests(unittest.TestCase):
    def test_version_and_key(self):
        self.assertEqual(version_identity(VERSION_TEXT),(MODEL,FIRMWARE))
        self.assertEqual(version_identity(VERSION_TEXT.replace(", Version",",\nVersion")),(MODEL,FIRMWARE))
        for s in ("ROM: CPBoot 8.10.0.24",VERSION_TEXT+VERSION_TEXT,VERSION_TEXT.replace(FIRMWARE,"8.10.0.24-1")):
            with self.assertRaises(ParseError):version_identity(s)
        a=Approval("w1","192.0.2.1",MODEL,FIRMWARE,KEY,900,2000,"tag")
        self.assertTrue(a.permits((MODEL,FIRMWARE),KEY,1001,1002,1000))
        self.assertFalse(a.permits((MODEL,"8.10.0.25"),KEY,1001,1002,1000))
        self.assertFalse(a.permits((MODEL,FIRMWARE),"other",1001,1002,1000))
        self.assertFalse(a.permits((MODEL,FIRMWARE),KEY,1001,2000,1000))
        self.assertFalse(a.permits((MODEL,FIRMWARE),KEY,1001,1002,1002))
        self.assertFalse(a.permits((MODEL,FIRMWARE),KEY,1001,1700,1000))
    def test_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir()
            cfg=SimpleNamespace(root=p,site="demo",targets=[SimpleNamespace(id="w1",ip="192.0.2.1")])
            self.assertEqual(load_approvals(cfg,1000),{})
            d={"controller":"w1","ip":"192.0.2.1","model":MODEL,"firmware":FIRMWARE,"host_key":KEY,
               "approved_at":900,"valid_until":2000,"reviewed_by":"fixture-reviewer","checks":{k:True for k in CHECKS}}
            manifest={"version":1,"enabled":True,"site":"demo","contract":"aos8-radio-v1","controllers":[d]}
            path=p/"config/radio-validation.json";path.write_text(json.dumps(manifest))
            self.assertIn("w1",load_approvals(cfg,1000))
            d["checks"]["nf_dbm"]=False;path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):load_approvals(cfg,1000)
    def test_worker_attestation(self):
        class Transport:
            def is_authenticated(self):return True
            def get_remote_server_key(self):return SimpleNamespace(asbytes=lambda:b"fictional-key")
        s=FixtureSession(1)
        s.responses["show version"]=VERSION_TEXT
        s.transport=SimpleNamespace(client=SimpleNamespace(get_transport=lambda:Transport()))
        self.assertEqual(fingerprint(s),KEY)
        a=Approval("w1","192.0.2.1",MODEL,FIRMWARE,KEY,900,2000,"tag")
        t=[1000]
        w=ControllerWorker("w1",lambda:s,lambda:t[0],lambda:t[0],mode="standard",field_approval=a)
        for _ in range(6):
            reading=w.tick()
            self.assertEqual(reading.status,"success")
        snap=w.snapshot()
        self.assertEqual(snap.field_tag,"tag")
        w.field_approval=replace(a,firmware="8.10.0.25")
        self.assertIsNone(w.snapshot().field_tag)
        w.field_approval=a
        t[0]=2000
        self.assertIsNone(w.snapshot())
        w.close()

def observation(t,u=90,complete=True):
    return Radio("site:02:00:00:00:00:01:0","ap","DEMO","w1","5GHz",44,t,u,5,-94,
                 source="aos8-cli-unvalidated",epoch="radio-config-1",complete=complete)
def snapshot(t,tag="approval",connection="session-1"):
    return ControllerSnapshot("w1",t,[],[],[],field_tag=tag,field_until=10000,
                              field_connection=connection,field_profile="safe")

class QualityTests(unittest.TestCase):
    def test_safe_profile_persistence_and_no_false_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s)
            for t,status in ((1000,"DETECTED"),(1180,"DETECTED"),(1360,"PERSISTENT")):
                r=observation(t);s.save(r)
                got=l.evaluate([r],[snapshot(t)],t,{r.radio_id:t})[r.radio_id]
                self.assertEqual(got["status"],status)
                self.assertEqual(got["score"],50)
                self.assertEqual(l.evaluate([r],[snapshot(t)],t,{r.radio_id:t})[r.radio_id],got)
            # Missing or invalid data cannot manufacture a recovery.
            r=observation(1540,u=None);s.save(r)
            self.assertEqual(l.evaluate([r],[snapshot(1540)],1540,{r.radio_id:1540})[r.radio_id]["status"],"UNKNOWN")
            self.assertEqual(l.evaluate([r],[snapshot(1540,tag=None)],1540,{r.radio_id:1540}),{})
            self.assertEqual(l.evaluate([r],[snapshot(1540)],1540,{}),{})
            for t,status in ((1720,"RECOVERING"),(1900,"RECOVERING"),(2080,"RESOLVED")):
                r=observation(t,20);s.save(r)
                self.assertEqual(l.evaluate([r],[snapshot(t)],t,{r.radio_id:t})[r.radio_id]["status"],status)
            # Diagnostic raw data remains unvalidated; quality is a separate ledger.
            self.assertEqual(s.snapshot()["radios"][0]["source"],"aos8-cli-unvalidated")
            self.assertIsNone(s.snapshot()["radios"][0]["score"])
    def test_context_change_and_view_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s)
            for t in (1000,1180,1360):
                r=observation(t);s.save(r);l.evaluate([r],[snapshot(t)],t,{r.radio_id:t})
            r=observation(1540);s.save(r)
            q=l.evaluate([r],[snapshot(1540,connection="new-session")],1540,{r.radio_id:1540})
            self.assertEqual(q[r.radio_id]["status"],"DETECTED")
            now=[1540];v=DiagnosticView(s,lambda:now[0],key=b"fixture")
            v.publish({},[(r.radio_id,1540)],quality=q)
            self.assertEqual(v.page()["rows"][0]["quality_score"],50)
            now[0]=1561
            self.assertIsNone(v.page()["rows"][0]["quality_score"])
            now[0]=1540;v.publish({},[(r.radio_id,1540)],quality={})
            self.assertEqual(v.page()["rows"][0]["risk_status"],"UNKNOWN")
