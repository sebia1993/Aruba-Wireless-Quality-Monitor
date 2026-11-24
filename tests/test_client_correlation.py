import tempfile,json,unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from awqm.client_correlation import ClientCorrelation,CorrelationApproval,load_correlation_approvals,CHECKS
from awqm.station_validation import StationApproval,AssociationStamp
from awqm.live_clients import LiveClients,ClientApproval
from awqm.worker import Reading
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from test_live_clients import fixture,MAC,BSS,SSID

def observation(t,health=90,retry=5,score=100,sequence="seq",radio="r1",network="network",activity="successful_tx_observed"):
    return dict(controller="w1",station_tag="station",radio_id=radio,radio_epoch="channel44",
        network=network,sequence=sequence,start=t-60,end=t,activity=activity,retry=retry,
        health=health,score=score,link_at=t,radio_at=t,persistent=True,utilization=90,until=3000)

class CorrelationTests(unittest.TestCase):
    def test_required_baseline_overlap_and_two_peers(self):
        c=ClientCorrelation({"w1":CorrelationApproval("station",900,3000)})
        for t in (1000,1060,1120,1180,1240,1300):
            self.assertFalse(c.evaluate({k:observation(t) for k in ("a","b","c")},t))
        t=1360
        rows={"a":observation(t,20,35,0),"b":observation(t,60,25,60),"c":observation(t,65,22,70)}
        got=c.evaluate(rows,t)
        self.assertEqual(got["a"]["candidate"],"RF03")
        self.assertEqual(got["a"]["confidence"],"low")
        self.assertEqual(got["a"]["peer_degraded"],2)
        self.assertEqual(got["a"]["peer_comparable"],2)
        self.assertNotIn("b",got)
        self.assertEqual(len(c.history["a"][1]),7)
        self.assertEqual(c.evaluate(rows,t),got)
        self.assertEqual(len(c.history["a"][1]),7)
        # Same group but a short/disjoint interval is not comparable.
        bad={**rows,"c":{**rows["c"],"start":1330}}
        self.assertFalse(c.evaluate(bad,t))
        self.assertFalse(c.evaluate({**rows,"c":{**rows["c"],"network":"other"}},t))
        self.assertFalse(c.evaluate({**rows,"c":{**rows["c"],"activity":"no_successful_tx_increase"}},t))
        self.assertFalse(c.evaluate({**rows,"c":{**rows["c"],"sequence":"new-session"}},t))
        self.assertFalse(c.evaluate(rows,t+91))
        self.assertFalse(c.history)
    def test_disabled_and_expired_approval(self):
        self.assertFalse(ClientCorrelation({}).evaluate({"a":observation(1000)},1000))
        c=ClientCorrelation({"w1":CorrelationApproval("wrong",900,3000)})
        self.assertFalse(c.evaluate({"a":observation(1000)},1000));self.assertFalse(c.history)
        c=ClientCorrelation({"w1":CorrelationApproval("station",900,1001)})
        c.evaluate({"a":observation(1000)},1000)
        self.assertTrue(c.history)
        self.assertFalse(c.evaluate({"a":observation(1000)},1001));self.assertFalse(c.history)
    def test_manifest_requires_privacy_and_exact_station_tag(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();cfg=SimpleNamespace(root=p,site="lab")
            stations={"w1":StationApproval("client",900,3000,"station")}
            self.assertEqual(load_correlation_approvals(cfg,stations,1000),{})
            rec={"controller":"w1","station_tag":"station","approved_at":950,"valid_until":2000,
                 "reviewed_by":"fixture","checks":{k:True for k in CHECKS}}
            d={"version":1,"enabled":True,"site":"lab","contract":"aos8-indirect-retry-correlation-v1","controllers":[rec]}
            f=p/"config/client-correlation.json";f.write_text(json.dumps(d))
            self.assertTrue(load_correlation_approvals(cfg,stations,1000)["w1"].permits("station",1000,1000))
            rec["checks"]["baseline_privacy_15_minutes"]=False;f.write_text(json.dumps(d))
            with self.assertRaises(ValueError):load_correlation_approvals(cfg,stations,1000)
    def test_live_pipeline_and_expiry(self):
        r,s,q,a,assoc,bss,d=fixture()
        devices=[MAC,"02:00:00:00:01:03","02:00:00:00:01:04"]
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True,"ct")},key=b"fixture",
             station_approvals={"w1":StationApproval("ct",900,1800,"station")},
             correlation_approvals={"w1":CorrelationApproval("station",900,1800)})
        claims={m:{**a,"client_mac":m} for m in devices}
        counters={m:[1000,100] for m in devices}
        for i,t in enumerate(range(1000,1421,60)):
            r=replace(r,observed_at=t);s=replace(s,observed_at=t)
            q={r.radio_id:{"observed_at":t,"status":"PERSISTENT","score":50}}
            assoc=replace(assoc,observed_at=t-1,data=list(claims.values()))
            bss=replace(bss,observed_at=t-2)
            health=[90,90,90] if t<1420 else [20,60,60]
            snr=[30,30,30] if t<1420 else [12,22,22]
            detail=replace(d,observed_at=t,data=[{**d.data[0],"client_mac":m,"health":h,"ack_snr":n}
                 for m,h,n in zip(devices,health,snr)])
            self.assertEqual(l.ingest(detail,"w1",s,assoc,bss,[r],q,claims,t),3)
            for m in devices:
                counters[m][0]+=200;counters[m][1]+=70 if t==1420 else 10
                stamp=AssociationStamp(a["ap_name"],BSS,SSID,2,1,471+i*60,t)
                read=Reading("show ap debug client-stats","success",t,{"before":stamp,"after":stamp,
                      "counters":{"sent":counters[m][0],"retried":counters[m][1]}},generation=1,target=m)
                self.assertEqual(l.ingest(read,"w1",s,assoc,bss,[r],q,claims,t),1)
            views=l.publish([r],[s],q,claims,t)
        self.assertEqual(len(views),3)
        candidate=next(x for x in views if x["candidate"]=="RF03")
        self.assertEqual(candidate["peer_degraded"],2)
        self.assertEqual(candidate["peer_comparable"],2)
        self.assertIsNone(candidate["airtime"]);self.assertIsNone(candidate["retry"])
        for forbidden in (*devices,BSS,SSID):
            self.assertNotIn(forbidden,json.dumps(views))
            self.assertNotIn(forbidden,repr(l.correlation.history))
        with tempfile.TemporaryDirectory() as root:
            st=Store(root,mode="diagnostic");st.save(r)
            now=[1420];v=DiagnosticView(st,lambda:now[0])
            v.publish({},[(r.radio_id,1420)],client_views=views,client_enabled=True)
            self.assertTrue(any(x["candidate"]=="RF03" for x in v.clients_page()["rows"]))
            # The candidate is independently expired at read time.
            expired=[{**x,"candidate_until":1420} for x in views]
            v.publish({},[(r.radio_id,1420)],client_views=expired,client_enabled=True)
            self.assertFalse(any(x["candidate"] for x in v.clients_page()["rows"]))
        failed=Reading("show ap debug client-stats","parse_error",1421,generation=1,target=devices[2])
        self.assertEqual(l.ingest(failed,"w1",s,assoc,bss,[r],q,claims,1421),0)
        self.assertFalse(any(x["candidate"] for x in l.publish([r],[s],q,claims,1421)))
        # Same physical Radio ID, changed channel epoch, must not retain views.
        self.assertEqual(l.publish([replace(r,epoch="changed")],[s],q,claims,1421),[])
        self.assertFalse(l.correlation.history)

    def test_cohort_selection_preserves_controller_cap(self):
        from awqm.jobs import Jobs
        r,s,q,a,assoc,bss,d=fixture()
        devices=[MAC,"02:00:00:00:01:03","02:00:00:00:01:04","02:00:00:00:01:05"]
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True,"ct")},key=b"fixture",
             station_approvals={"w1":StationApproval("ct",900,1800,"station")},
             correlation_approvals={"w1":CorrelationApproval("station",900,1800)})
        claims={m:{**a,"client_mac":m} for m in devices}
        assoc=replace(assoc,data=list(claims.values()))
        detail=replace(d,data=[{**d.data[0],"client_mac":m,"health":20 if m==MAC else 95,"ack_snr":12 if m==MAC else 30} for m in devices])
        self.assertEqual(l.ingest(detail,"w1",s,assoc,bss,[r],q,claims,1010),4)
        l.publish([r],[s],q,claims,1010)
        class Worker:
            def __init__(self):self.jobs=Jobs()
            def submit_detail(self,req,name,target,ttl=60):return self.jobs.submit(req,name,target,1010,ttl)
        w=Worker()
        self.assertEqual(len(l.plan_station(claims,1010,{"w1":w})),3)
        targets={j.target for j in w.jobs.list(1010)}
        self.assertIn(MAC,targets);self.assertEqual(len(targets),3)
        self.assertFalse(l.plan_station(claims,1011,{"w1":w}))
        # Without the additional approval, only the poor-link client is
        # selected, not the two comparison peers.
        l.correlation.approvals.clear();l.station_plan.clear()
        w=Worker()
        self.assertEqual(len(l.plan_station(claims,1010,{"w1":w})),1)
        self.assertEqual({j.target for j in w.jobs.list(1010)},{MAC})
