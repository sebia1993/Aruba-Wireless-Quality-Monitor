import tempfile,json,unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from awqm.storage import Store
from awqm.quality_ledger import QualityLedger
from awqm.incident_reports import summary,incident_csv,incident_html
from awqm.client_correlation import CorrelationApproval,load_correlation_approvals,CHECKS
from awqm.station_validation import StationApproval
from awqm.live_clients import LiveClients,ClientApproval
from test_incidents import observation,snap,feed

def persistent(root):
    store=Store(root,mode="diagnostic")
    ledger=QualityLedger(store)
    for t in (1000,1180,1360):
        quality=feed(store,ledger,t)
    return store,ledger,observation(1360),snap(1360),quality

def aggregate():
    return {"candidate_count":1,"min_comparable":2,"min_degraded":2,"interval_end":1360}

def neighbor():
    return {"ap_name":"DEMO-PEER","band":"5GHz","channel":44,"width":40,"snr":35,
        "discovery":"active","neighbor_at":1360,"target_radio_at":1360,"relation":"same_block",
        "overlap_mhz":40,"candidate":"RF05","confidence":"medium"}

class RF03EvidenceTests(unittest.TestCase):
    def test_retention_permission_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();cfg=SimpleNamespace(root=p,site="fixture")
            station={"w1":StationApproval("ct",900,3000,"station")}
            rec={"controller":"w1","station_tag":"station","approved_at":950,"valid_until":2000,
                "reviewed_by":"fixture","checks":{k:True for k in CHECKS}}
            d={"version":1,"enabled":True,"site":"fixture","contract":"aos8-indirect-retry-correlation-v1","controllers":[rec]}
            f=p/"config/client-correlation.json"
            f.write_text(json.dumps(d))
            self.assertFalse(load_correlation_approvals(cfg,station,1000)["w1"].persist_aggregate)
            rec["persist_aggregate"]=True
            f.write_text(json.dumps(d))
            with self.assertRaises(ValueError):load_correlation_approvals(cfg,station,1000,retention_enabled=True)
            rec["checks"]["aggregate_evidence_retention_approval"]=True;f.write_text(json.dumps(d))
            with self.assertRaises(ValueError):load_correlation_approvals(cfg,station,1000)
            self.assertTrue(load_correlation_approvals(cfg,station,1000,retention_enabled=True)["w1"].persist_aggregate)

    def test_aggregate_and_neighbor_preserve_both_sources(self):
        for client_first in (True,False):
            with self.subTest(client_first=client_first),tempfile.TemporaryDirectory() as root:
                st,l,r,s,q=persistent(root);i=l.incidents;ident=q["incident_id"]
                ctx=s.field_tag+"|"+s.field_connection+"|"+r.epoch
                record=lambda:i.record_client_summary(r,q,ctx,1365,aggregate())
                add_neighbor=lambda:i.record_neighbors(r,q,1365,[neighbor()])
                if client_first:
                    self.assertTrue(record());self.assertTrue(add_neighbor())
                else:
                    self.assertTrue(add_neighbor());self.assertTrue(record())
                d=i.detail(ident);rev=d["revision"]
                self.assertEqual(set(x["code"] for x in d["evidence"]["candidates"]),{"RF03","RF04","RF05"})
                self.assertEqual(d["evidence"]["client_correlation"]["scope"],"aggregate_historical_observation")
                self.assertEqual(d["evidence"]["client_correlation"]["min_degraded"],2)
                self.assertFalse(record());self.assertFalse(add_neighbor())
                self.assertEqual(i.detail(ident)["revision"],rev)
                self.assertNotIn(r.radio_id,json.dumps(d))
                self.assertIn("RF03",summary(d))
                self.assertIn("historical aggregate",incident_csv(d).decode("utf-8-sig"))
                self.assertIn("historical aggregate",incident_html(d).decode("utf-8"))
                self.assertTrue(i.review(ident,rev,"req","op","supported","radio_evidence","RF03")["recorded"])
                feed(st,l,1540)
                d=i.detail(ident)
                self.assertNotIn("client_correlation",d["evidence"])
                self.assertTrue(any("client_correlation" in x["evidence"] for x in d["timeline"]))
                self.assertFalse(record())

    def test_stale_context_and_invalid_aggregate_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            st,l,r,s,q=persistent(root);i=l.incidents;ctx=s.field_tag+"|"+s.field_connection+"|"+r.epoch
            self.assertFalse(i.record_client_summary(r,q,"wrong",1365,aggregate()))
            self.assertFalse(i.record_client_summary(r,q,ctx,1500,aggregate()))
            self.assertFalse(i.record_client_summary(r,{**q,"valid_until":1365},ctx,1365,aggregate()))
            for bad in ({**aggregate(),"client":"02:00:00:00:01:01"},{**aggregate(),"min_degraded":1},{**aggregate(),"candidate_count":True}):
                with self.assertRaises(ValueError):i.record_client_summary(r,q,ctx,1365,bad)
            self.assertNotIn("client_correlation",i.detail(q["incident_id"])["evidence"])

    def test_live_persistence_gate_and_no_identifiers(self):
        with tempfile.TemporaryDirectory() as root:
            st,l,r,s,q=persistent(root)
            client=LiveClients({"w1":ClientApproval("tag",900,3000,True,"ct")},key=b"fixture",
                station_approvals={"w1":StationApproval("ct",900,3000,"station")},
                correlation_approvals={"w1":CorrelationApproval("station",900,3000)})
            ident=client.token("client","02:00:00:00:01:01")
            client.rows={ident:{"radio_id":r.radio_id}}
            client.views=[{"id":ident,"controller":"w1","candidate":"RF03","confidence":"low","candidate_until":1420,
                "station_start":1300,"station_end":1360,"peer_comparable":2,"peer_degraded":2}]
            self.assertEqual(client.persist_correlations(l.incidents,[r],[s],{r.radio_id:q},1365),0)
            client.correlation.approvals["w1"]=CorrelationApproval("station",900,3000,True)
            self.assertEqual(client.persist_correlations(l.incidents,[r],[s],{r.radio_id:q},1365),1)
            self.assertEqual(client.persist_correlations(l.incidents,[r],[s],{r.radio_id:q},1366),0)
            d=l.incidents.detail(q["incident_id"])
            self.assertNotIn(ident,json.dumps(d))
            self.assertNotIn("02:00:00:00:01:01",json.dumps(d))
            self.assertEqual(d["evidence"]["client_correlation"]["candidate_count"],1)
