import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
from awqm.ap_anchors import STANDBY,UAC,CONTRACT,CHECKS,standby,uac_database,load_anchor_approvals,AnchorApproval,reconcile
from awqm.aos8 import ParseError,inventory,merge_radios,ControllerSnapshot,active_details,radio_summary
from awqm.collector import command
from awqm.worker import ControllerWorker,Reading
from awqm.fixture_session import FixtureSession,table
from awqm.field_validation import Approval
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView

NAME="DEMO-AP-001";IP="192.0.2.11";MAC="02:00:00:00:00:01"
def st(rows=None):
    if rows is None:rows=[[NAME,IP,"SA/SU"]]
    return table("Standby AP Table",["Name","IP Address","Cluster Role"],rows,"Num APs:"+str(len(rows)))
def uc(rows=None):
    if rows is None:rows=[[NAME,MAC,IP,"demo","535","Up","02:ff:00:01:00:01","02:ff:00:01:01:01"]]
    return table("UAC AP Database",["AP Name","MAC Address","IP Address","AP Group","AP Type","Status","Radio 0 BSSID","Radio 1 BSSID"],rows,"Total APs:"+str(len(rows)))
def fixture():
    sess=FixtureSession(1)
    inv=inventory(sess.responses["show ap database long"])
    snap=ControllerSnapshot("w1",1000,inv,active_details(sess.responses["show ap active details"]),radio_summary(sess.responses["show ap radio-summary"]))
    radios,unresolved=merge_radios([snap],"fixture")
    assert len(radios)==2 and not unresolved
    frame={"connected":True,"anchor_validation":{"valid":True,"approved_at":900,"until":1500},
           "readings":{STANDBY:Reading(STANDBY,"success",1000,standby(st()),generation=1),
                       UAC:Reading(UAC,"success",1000,uac_database(uc()),generation=1),
                       "show ap database long":Reading("show ap database long","success",1000,inv,generation=1)}}
    return radios,frame

class APAnchorTests(unittest.TestCase):
    def test_parse_positive_and_empty(self):
        self.assertEqual(standby(st())[0]["roles"],["SA","SU"])
        self.assertEqual(uac_database(uc())[0]["mac"],MAC)
        self.assertEqual(standby(st([])),[])
        self.assertEqual(uac_database(uc([])),[])
        self.assertNotIn("02:ff",json.dumps(uac_database(uc())))
        for bad in (st().replace("Num APs:1","Num APs:2"),st().replace("SA/SU","MYSTERY"),st([[NAME,IP,"SA"],[NAME,IP,"SU"]]),st()+"\n--More--"):
            with self.assertRaises(ParseError):standby(bad)
        for bad in (uc().replace("Total APs:1","Total APs:2"),uc().replace(MAC,"bad"),uc()+"\n--More--"):
            with self.assertRaises(ParseError):uac_database(bad)
        self.assertEqual(command(UAC),UAC)
        with self.assertRaises(ValueError):command(UAC,"arbitrary")

    def test_optional_approval(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();cfg=SimpleNamespace(root=p,site="fixture")
            a=Approval("w1","192.0.2.1","model","8.10.0.24","key",900,2000,"tag")
            self.assertEqual(load_anchor_approvals(cfg,{"w1":a},1000),{})
            rec={"controller":"w1","radio_tag":"tag","approved_at":950,"valid_until":1800,"reviewed_by":"fixture","checks":{k:True for k in CHECKS}}
            doc={"version":1,"enabled":True,"site":"fixture","contract":CONTRACT,"controllers":[rec]}
            path=p/"config/ap-anchor-validation.json";path.write_text(json.dumps(doc))
            self.assertTrue(load_anchor_approvals(cfg,{"w1":a},1000)["w1"].permits("tag",1000,1000))
            rec["checks"]["uac_mac_matches_wired_identity"]=False;path.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):load_anchor_approvals(cfg,{"w1":a},1000)

    def test_reconcile_positive_observations_not_readiness(self):
        radios,frame=fixture()
        rows,summaries=reconcile(radios,{"w2":frame},1000)
        self.assertEqual(len(rows),2)
        for row in rows.values():
            self.assertEqual(row["aac"],"w1")
            self.assertEqual([x["source"] for x in row["observations"]],["w2","w2"])
            self.assertEqual(row["service_impact"],"unverified")
            self.assertNotIn(MAC,json.dumps(row));self.assertNotIn(IP,json.dumps(row))
        self.assertEqual(summaries["w2"]["standby"]["matched"],1)
        self.assertEqual(summaries["w2"]["uac_database"]["matched"],1)
        frame["readings"][UAC]=replace(frame["readings"][UAC],data=[{"name":NAME,"ip":IP,"mac":"02:00:00:00:00:99"}])
        rows,summaries=reconcile(radios,{"w2":frame},1000)
        self.assertEqual(summaries["w2"]["uac_database"]["unmatched"],1)
        self.assertEqual(len(rows[radios[0].radio_id]["observations"]),1)
        frame["readings"][STANDBY]=replace(frame["readings"][STANDBY],data=[])
        rows,summaries=reconcile(radios,{"w2":frame},1000)
        self.assertEqual(rows,{})
        self.assertEqual(summaries["w2"]["standby"]["total"],0)
        frame["anchor_validation"]["valid"]=False
        self.assertFalse(reconcile(radios,{"w2":frame},1000)[0])

    def test_conflicts_and_stale_metadata(self):
        radios,frame=fixture()
        inv=frame["readings"]["show ap database long"]
        frame["readings"]["show ap database long"]=replace(inv,data=inv.data+inv.data)
        self.assertFalse(reconcile(radios,{"w2":frame},1000)[0])
        frame["readings"]["show ap database long"]=replace(inv,generation=2)
        self.assertFalse(reconcile(radios,{"w2":frame},1000)[0])
        frame["readings"]["show ap database long"]=inv
        self.assertFalse(reconcile(radios+[replace(radios[0],controller="w3")],{"w2":frame},1000)[0])
        self.assertFalse(reconcile(radios,{"w2":frame},1500)[0])

    def test_worker_gate_and_expired_api(self):
        now=[1000];clock=lambda:now[0];sess=FixtureSession(1)
        sess.responses.update({"show version":"ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24",STANDBY:st(),UAC:uc()})
        a=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","fixture-key",900,2000,"tag")
        w=ControllerWorker("w1",lambda:sess,clock,clock,mode="standard",field_approval=a,
            enable_cluster=True,anchor_approval=AnchorApproval("tag",900,1005))
        w.disabled.add("show lc-cluster group-membership")
        with patch("awqm.worker.fingerprint",return_value="fixture-key"):
            for _ in range(15):w.tick()
            frame=w.cluster_frame()
            self.assertEqual(frame["readings"][STANDBY].status,"success")
            self.assertEqual(frame["readings"][UAC].status,"success")
            r,expected=fixture();views,sums=reconcile(r,{"w1":frame},1000)
            self.assertEqual(len(views),2)
            with tempfile.TemporaryDirectory() as root:
                s=Store(root,mode="diagnostic");s.save_batch(r);v=DiagnosticView(s,clock)
                v.publish({"cluster":{"nodes":[{"controller":"w1","ap_anchors":sums["w1"]}]}},
                    [(x.radio_id,x.observed_at) for x in r],anchor_views=views)
                page=v.page();self.assertIsNotNone(page["rows"][0]["ap_anchor"])
                self.assertNotIn("valid_until",json.dumps(page["rows"][0]["ap_anchor"]))
                now[0]=1005
                page=v.page();self.assertIsNone(page["rows"][0]["ap_anchor"])
                self.assertIsNone(page["cluster"]["nodes"][0]["ap_anchors"]["standby"]["total"])
            for cmd in w.schedule.due:w.schedule.due[cmd]=float("inf")
            w.schedule.due[UAC]=now[0]
            self.assertEqual(w.tick().status,"withheld")
            w.close()
        other=ControllerWorker("w2",lambda:None,clock,clock,enable_cluster=True)
        self.assertNotIn(STANDBY,other.schedule.polls);self.assertNotIn(UAC,other.schedule.polls)
