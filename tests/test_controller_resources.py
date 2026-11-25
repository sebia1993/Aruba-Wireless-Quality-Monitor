import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
from awqm.controller_resources import (memory,datapath,ResourceApproval,load_resource_approvals,
    observations,MEMORY,DATAPATH,CONTRACT,CHECKS)
from awqm.aos8 import ParseError
from awqm.field_validation import Approval
from awqm.worker import Reading,ControllerWorker
from awqm.fixture_session import FixtureSession
from awqm.cluster import ClusterMonitor
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView

MEM="Memory (Kb): total: 8192000, used: 4096000, free: 4096000"
DP="""Datapath Network Processor Utilization
------+---------+---------+----------+
| Cpu utilization during past |
Cpu | 1 Sec 4 Secs 64 Secs |
------+---------+---------+----------+
1 | 20% | 30% | 99% |
2 | 85% | 72% | 0% |"""
TYPED="""Datapath Network Processor Utilization
+------+-----+---------+---------+----------+
| Cpu | Cpu utilization during past |
| Type | Id | 1 Sec 4 Secs 64 Secs |
+------+-----+---------+---------+----------+
SPGW | 3 | 1% | 3% | 5% |
SP | 4 | 20% | 15% | 9% |
FP | 5 | 9% | 10% | 15% |
Datapath CPU Allocation Summary
Slow Path (SP) : 1, Slow Path Gateway (SPGW) : 1
Fast Path (FP) : 1, Fast Path Gateway (FPGW) : 0
DPI : 0, Crypto (CRYP) : 0"""

class ResourceTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(memory(MEM)["reported_used_percent"],50)
        for bad in (MEM.replace("4096000","100",1),"total 10 used 5 free 5",MEM+"\n--More--",MEM+"\n"+MEM):
            with self.assertRaises(ParseError):memory(bad)
        self.assertEqual([r["id"] for r in datapath(DP)["rows"]],[1,2])
        self.assertFalse(datapath(DP)["row_count_verified"])
        self.assertTrue(datapath(TYPED)["allocation_checked"])
        for bad in (DP.replace("85%","101%"),DP.replace("2 |","1 |"),DP+"\nwrapped",
                    DP.replace("64 Secs","60 Secs"),DP+"\n--More--",TYPED.replace("Fast Path (FP) : 1","Fast Path (FP) : 2")):
            with self.assertRaises(ParseError):datapath(bad)
    def test_config_and_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir()
            cfg=SimpleNamespace(root=p,site="fixture")
            a=Approval("w1","192.0.2.1","model","8.10.0.24","key",900,2000,"tag")
            self.assertEqual(load_resource_approvals(cfg,{"w1":a},1000),{})
            row={"controller":"w1","radio_tag":"tag","approved_at":950,"valid_until":1900,"reviewed_by":"fixture",
                "cpu_ids":[2,1],"checks":{k:True for k in CHECKS}}
            d={"version":1,"enabled":True,"site":"fixture","contract":CONTRACT,"controllers":[row]}
            f=p/"config/resource-validation.json";f.write_text(json.dumps(d),encoding="utf-8")
            self.assertEqual(load_resource_approvals(cfg,{"w1":a},1000)["w1"].cpu_ids,(1,2))
            row["cpu_ids"]=[1,1];f.write_text(json.dumps(d),encoding="utf-8")
            with self.assertRaises(ValueError):load_resource_approvals(cfg,{"w1":a},1000)
    def frame(self):
        return {"connected":True,"readings":{
            MEMORY:Reading(MEMORY,"success",1000,memory(MEM)),
            DATAPATH:Reading(DATAPATH,"success",1000,datapath(DP))},
            "resource_validation":{"valid":True,"approved_at":900,"until":1100,"cpu_ids":(1,2)}}
    def test_no_stale_or_partial_data(self):
        f=self.frame();r=observations(f,1000)
        self.assertEqual(r["datapath"]["data"]["max_4s_percent"],72)
        self.assertNotIn("reported_64s_percent",json.dumps(r))
        self.assertEqual(observations(f,1100)["memory"]["status"],"attestation_unavailable")
        f["resource_validation"]["cpu_ids"]=(1,2,3)
        self.assertIsNone(observations(f,1000)["datapath"]["data"])
        f["readings"][MEMORY]=Reading(MEMORY,"parse_error",1001)
        self.assertIsNone(observations(f,1001)["memory"]["data"])
        f["connected"]=False
        self.assertIsNone(observations(f,1001)["datapath"]["data"])
    def test_cluster_api_masks_between_publications(self):
        m=ClusterMonitor({"w1":"192.0.2.1"});f=self.frame();f["resource_validation"]["until"]=1005
        c=m.report({"w1":f},1000)
        with tempfile.TemporaryDirectory() as root:
            t=[1000];s=Store(root,mode="diagnostic");v=DiagnosticView(s,lambda:t[0])
            v.publish({"cluster":c},[])
            r=v.page()["cluster"]["nodes"][0]["resources"]
            self.assertEqual(r["memory"]["data"]["reported_used_percent"],50)
            self.assertNotIn("valid_until",r["memory"])
            t[0]=1005
            r=v.page()["cluster"]["nodes"][0]["resources"]
            self.assertIsNone(r["memory"]["data"])
            self.assertIsNone(r["datapath"]["data"])
    def test_worker_gates(self):
        now=[1000];clock=lambda:now[0];sess=FixtureSession(1)
        sess.responses.update({"show version":"ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24",MEMORY:MEM,DATAPATH:DP})
        a=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","fixture-key",900,2000,"tag")
        w=ControllerWorker("w1",lambda:sess,clock,clock,mode="standard",enable_bss=True,
            field_approval=a,enable_cluster=True,resource_approval=ResourceApproval("tag",900,1900,(1,2)))
        # Cluster polling is irrelevant in this fixture; supply successful
        # responses for the two optional resource commands and suppress
        # membership to isolate resource gate and scheduler integration.
        w.disabled.add("show lc-cluster group-membership")
        with patch("awqm.worker.fingerprint",return_value="fixture-key"):
            for _ in range(12):w.tick()
            frame=w.cluster_frame()
            self.assertEqual(observations(frame,1000)["memory"]["status"],"observed")
            self.assertEqual(observations(frame,1000)["datapath"]["status"],"observed")
            now[0]=1180
            w.schedule.protection.cpu(75,now[0]);w.schedule.protection.cpu(75,now[0])
            for cmd in w.schedule.due:w.schedule.due[cmd]=float("inf")
            w.schedule.due[MEMORY]=now[0]
            reading=w.tick()
            self.assertEqual(reading.status,"withheld")
            self.assertIsNone(observations(w.cluster_frame(),1180)["memory"]["data"])
            w.close()
        other=ControllerWorker("w2",lambda:None,clock,clock)
        self.assertFalse(any(cmd in other.schedule.polls for cmd in (MEMORY,DATAPATH)))
