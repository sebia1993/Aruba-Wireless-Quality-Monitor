import json,tempfile,unittest
from dataclasses import replace
from awqm.storage import Store
from awqm.model import Radio
from awqm.aos8 import ControllerSnapshot
from awqm.quality_ledger import QualityLedger
from awqm.incidents import Incidents,ReviewConflict

def observation(t,u=90,i=5,n=-94,epoch="e"):
    return Radio("site:02:00:00:00:00:01:0","ap","DEMO","w1","5GHz",44,t,u,i,n,
        source="aos8-cli-unvalidated",epoch=epoch)
def snap(t,tag="tag",connection="session"):
    return ControllerSnapshot("w1",t,[],[],[],field_tag=tag,field_until=10000,field_connection=connection,field_profile="safe")
def feed(s,l,t,u=90,i=5,n=-94,epoch="e",connection="session",tag="tag"):
    r=observation(t,u,i,n,epoch);s.save(r)
    return l.evaluate([r],[snap(t,tag,connection)],t,{r.radio_id:t}).get(r.radio_id,{})

class IncidentTests(unittest.TestCase):
    def test_lifecycle_and_historical_review(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s);i=l.incidents
            ident=feed(s,l,1000)["incident_id"]
            self.assertEqual(feed(s,l,1180)["incident_id"],ident)
            self.assertEqual(feed(s,l,1360)["incident_id"],ident)
            d=i.detail(ident);self.assertEqual(d["state"],"PERSISTENT")
            self.assertEqual(d["first_at"],1000);self.assertEqual(d["persistent_at"],1360)
            revision=d["revision"]
            self.assertEqual(d["evidence"]["candidates"][0]["code"],"RF04")
            self.assertNotIn("02:00:00:00:00:01",json.dumps(d))
            result=i.review(ident,revision,"request1","operator","supported","radio_evidence","RF04")
            self.assertFalse(result["duplicate"])
            self.assertTrue(i.review(ident,revision,"request1","operator","supported","radio_evidence","RF04")["duplicate"])
            with self.assertRaises(ValueError):i.review(ident,revision,"request1","operator","inconclusive","insufficient_evidence","RF04")
            # No data does not resolve; missing sample cannot count as good.
            feed(s,l,1540,None)
            self.assertEqual(i.detail(ident)["state"],"UNCONFIRMED")
            self.assertIsNone(i.detail(ident)["ended_at"])
            for t in (1720,1900):feed(s,l,t,20)
            self.assertIsNone(i.detail(ident)["ended_at"])
            feed(s,l,2080,20)
            self.assertEqual(i.detail(ident)["state"],"RESOLVED")
            self.assertEqual(i.detail(ident)["ended_at"],2080)
            self.assertEqual(i.detail(ident)["service_impact"],"unverified")
            # Historical candidate remains reviewable after recovery, not current.
            i.review(ident,revision,"request2","operator","not_supported","conflicting_evidence","RF04")
            page=i.list(start=0,end=3000,status="closed")
            self.assertEqual(page["total"],1);self.assertFalse(page["rows"][0]["review_current"])
            self.assertEqual(len(Incidents(s).detail(ident)["reviews"]),2)
            self.assertNotEqual(feed(s,l,2260)["incident_id"],ident)
    def test_context_and_stale(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s);i=l.incidents
            old=feed(s,l,1000)["incident_id"]
            feed(s,l,1180);feed(s,l,1360)
            self.assertEqual(i.list(start=0,end=2000,current=())["rows"][0]["display_state"],"UNCONFIRMED")
            self.assertEqual(i.list(start=0,end=2000,current={old})["rows"][0]["display_state"],"PERSISTENT")
            new=feed(s,l,1540,connection="reconnected")["incident_id"]
            self.assertNotEqual(old,new)
            self.assertEqual(i.detail(old)["state"],"CONTEXT_LOST")
            self.assertEqual(i.detail(new)["state"],"DETECTED")
            self.assertEqual(i.list(start=0,end=2000,status="open")["total"],1)
            self.assertEqual(i.list(start=0,end=2000,status="closed")["total"],1)
    def test_no_unverified_or_external_cause(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s);i=l.incidents
            self.assertEqual(feed(s,l,1000,tag=None),{})
            self.assertEqual(i.list(start=0,end=2000)["total"],0)
            ident=feed(s,l,1180,20,55,-70)["incident_id"]
            d=i.detail(ident)
            self.assertEqual([x["code"] for x in d["evidence"]["candidates"]],["UNKNOWN"])
            with self.assertRaises(ValueError):i.review(ident,d["revision"],"r","op","supported","radio_evidence","UNKNOWN")
            with self.assertRaises(ReviewConflict):i.review(ident,999,"r","op","inconclusive","insufficient_evidence","UNKNOWN")
            feed(s,l,1360,20,5,-94)
            self.assertEqual(i.detail(ident)["state"],"TRANSIENT")
