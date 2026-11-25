import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from awqm.live_clients import LiveClients,ClientApproval,load_client_approvals,CHECKS
from awqm.field_validation import Approval
from awqm.model import Radio
from awqm.aos8 import ControllerSnapshot
from awqm.worker import Reading
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView

MAC="02:00:00:00:01:01";BSS="02:00:00:00:01:02";SSID="fictional-ssid"
def fixture():
    r=Radio("r1","ap1","DEMO-AP","w1","5GHz",44,1000,90,5,-94,5,source="aos8-cli-unvalidated")
    s=ControllerSnapshot("w1",1000,[],[],[],field_tag="tag",field_until=2000,field_connection="generation1")
    q={"r1":{"observed_at":1000,"status":"PERSISTENT","score":50}}
    claim={"client_mac":MAC,"ap_name":"DEMO-AP","bssid":BSS,"ssid":SSID,"active":True}
    assoc=Reading("show ap association","success",995,[claim],generation=1)
    bss=Reading("show ap bss-table","success",980,[dict(ap_name="DEMO-AP",bssid=BSS,ssid=SSID,band="5GHz",channel=44,service=True)],generation=1)
    d=dict(client_mac=MAC,bssid=BSS,ssid=SSID,health=20,ack_snr=12,tx_rate=24)
    detail=Reading("show ap debug client-table","success",1010,[d],generation=1,target="DEMO-AP")
    return r,s,q,claim,assoc,bss,detail

class LiveClientTests(unittest.TestCase):
    def test_ephemeral_pseudonyms_and_mapping(self):
        r,s,q,a,assoc,bss,d=fixture()
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True)},key=b"fixture")
        self.assertEqual(l.ingest(d,"w1",s,assoc,bss,[r],q,{MAC:a},1010),1)
        views=l.publish([r],[s],q,{MAC:a},1010)
        self.assertEqual(len(views),1)
        self.assertEqual(views[0]["quality_status"],"partial")
        self.assertIsNone(views[0]["airtime"]);self.assertIsNone(views[0]["candidate"]);self.assertIsNone(views[0]["retry"])
        for forbidden in (MAC,BSS,SSID):
            self.assertNotIn(forbidden,json.dumps(l.rows))
            self.assertNotIn(forbidden,json.dumps(views))
        self.assertEqual(l.publish([r],[replace(s,field_connection="new")],q,{MAC:a},1010),[])
        self.assertEqual(l.publish([r],[s],q,{MAC:{**a,"ssid":"different"}},1010),[])
        self.assertEqual(l.publish([r],[s],q,{MAC:a},2000),[])
    def test_duplicate_and_stale_metadata(self):
        r,s,q,a,assoc,bss,d=fixture()
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True)},key=b"fixture")
        duplicate=replace(d,data=d.data+d.data)
        self.assertEqual(l.ingest(duplicate,"w1",s,assoc,bss,[r],q,{MAC:a},1010),0)
        self.assertEqual(l.ingest(d,"w1",s,replace(assoc,observed_at=700),bss,[r],q,{MAC:a},1010),0)
        self.assertEqual(l.ingest(d,"w1",s,assoc,bss,[r],q,{},1010),0)
        self.assertEqual(l.ingest(d,"w1",replace(s,field_tag="wrong"),assoc,bss,[r],q,{MAC:a},1010),0)
    def test_separate_approval(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir()
            cfg=SimpleNamespace(root=p,site="fixture")
            a=Approval("w1","192.0.2.1","model","8.10.0.24","key",900,2000,"tag")
            self.assertEqual(load_client_approvals(cfg,{"w1":a},1000),{})
            d={"controller":"w1","radio_tag":"tag","approved_at":950,"valid_until":1800,"automatic":True,
                "reviewed_by":"fixture","checks":{k:True for k in CHECKS}}
            doc={"version":1,"enabled":True,"site":"fixture","contract":"aos8-client-table-v1","controllers":[d]}
            path=p/"config/client-validation.json";path.write_text(json.dumps(doc))
            self.assertTrue(load_client_approvals(cfg,{"w1":a},1000)["w1"].automatic)
            d["checks"]["privacy_approval"]=False;path.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):load_client_approvals(cfg,{"w1":a},1000)
    def test_published_client_data_expires(self):
        r,s,q,a,assoc,bss,d=fixture()
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True)},key=b"fixture")
        l.ingest(d,"w1",s,assoc,bss,[r],q,{MAC:a},1010)
        rows=l.publish([r],[s],q,{MAC:a},1010)
        with tempfile.TemporaryDirectory() as root:
            st=Store(root,mode="diagnostic");st.save(r)
            now=[1010];v=DiagnosticView(st,lambda:now[0])
            v.publish({},[(r.radio_id,1000)],client_views=rows,client_enabled=True)
            self.assertEqual(v.clients_page()["total"],1)
            now[0]=1031
            self.assertEqual(v.clients_page()["total"],0)
            v.publish({},available=False,client_views=rows,client_enabled=True)
            self.assertEqual(v.clients_page()["total"],0)

    def test_worker_rechecks_before_live_cli(self):
        from awqm.worker import ControllerWorker
        from awqm.fixture_session import FixtureSession
        from unittest.mock import patch
        s=FixtureSession(1)
        now=[1000]
        s.responses["show version"]="ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24"
        a=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","fixture-key",900,2000,"tag")
        w=ControllerWorker("w1",lambda:s,lambda:now[0],lambda:now[0],mode="standard",
            enable_bss=True,field_approval=a,live_detail=True,client_approval=ClientApproval("tag",900,1900,True))
        with patch("awqm.worker.fingerprint",return_value="fixture-key"):
            for _ in range(7):
                self.assertEqual(w.tick().status,"success")
            job=w.submit_detail("test-live","show ap debug client-table","DEMO-AP-001")
            self.assertTrue(w._live_detail_allowed(job))
            result=w.tick()
            self.assertEqual(result.command,"show ap debug client-table")
            self.assertEqual(result.status,"success")
            self.assertEqual(w.jobs.get(job.job_id).status,"completed")
            self.assertFalse(w.detail_latest) # do not retain raw live detail
            w.client_approval=None
            j=w.submit_detail("test-deny","show ap debug client-table","DEMO-AP-001")
            self.assertIsNone(w.tick())
            self.assertEqual(w.jobs.get(j.job_id).reason,"client_validation_or_freshness_required")
            w.client_approval=ClientApproval("tag",900,1900,True)
            now[0]=1900
            self.assertFalse(w._live_detail_allowed(j))
        w.close()

    def test_selection_rate_limit(self):
        from awqm.jobs import Jobs
        class W:
            def __init__(self):self.jobs=Jobs()
            def submit_detail(self,req,name,target,ttl):return self.jobs.submit(req,name,target,1000,ttl)
        r,s,q,a,assoc,bss,d=fixture()
        radios=[replace(r,radio_id=str(i),ap_id=str(i),ap_name="AP-"+str(i)) for i in range(8)]
        quality={x.radio_id:{"observed_at":1000,"status":"PERSISTENT","score":50} for x in radios}
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True)})
        workers={"w1":W()}
        self.assertEqual(len(l.plan(radios,[s],quality,1000,workers)),2)
        self.assertEqual(l.plan(radios,[s],quality,1001,workers),[])

    def test_failed_table_invalidates_previous_ap_view(self):
        r,s,q,a,assoc,bss,d=fixture()
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True)},key=b"fixture")
        self.assertEqual(l.ingest(d,"w1",s,assoc,bss,[r],q,{MAC:a},1010),1)
        self.assertEqual(len(l.publish([r],[s],q,{MAC:a},1010)),1)
        failed=replace(d,status="parse_error",data=None)
        self.assertEqual(l.ingest(failed,"w1",s,assoc,bss,[r],q,{MAC:a},1011),0)
        self.assertEqual(l.publish([r],[s],q,{MAC:a},1011),[])
