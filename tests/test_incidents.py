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
            new=feed(s,l,2260)["incident_id"]
            self.assertNotEqual(new,ident)
            prior=i.detail(new)["prior_resolved_observation"]
            self.assertEqual(prior,{"incident_id":ident,"first_at":1000,"persistent_at":1360,
                "ended_at":2080,"gap_seconds":180,"relation":"same_observation_context",
                "service_impact":"unverified"})
            self.assertEqual(i.detail(new)["prior_observation_window_days"],30)
            self.assertIsNone(i.detail(ident)["prior_resolved_observation"])
            self.assertNotIn("02:00:00:00:00:01",json.dumps(prior))
            self.assertEqual(Incidents(s).detail(new)["prior_resolved_observation"],prior)
            from awqm.incident_reports import summary
            self.assertIn("동일 관측 문맥 이전 RF 정책 정상화",summary(i.detail(new)))

    def test_repeat_observation_recorded_once_at_creation_and_survives_lifecycle(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s)
            for t in (1000,1180,1360):
                old=feed(s,l,t)["incident_id"]
            for t in (1540,1720,1900):feed(s,l,t,20)
            self.assertEqual(l.incidents.detail(old)["state"],"RESOLVED")
            ident=feed(s,l,2080)["incident_id"]
            self.assertNotEqual(ident,old)
            expected={"prior_incident_id":old,"prior_ended_at":1900,"gap_seconds":180,
                      "relation":"repeat_rf_observation","service_impact":"unverified"}
            d=l.incidents.detail(ident)
            self.assertTrue(d["repeat_observation_assessed"])
            self.assertEqual(d["repeat_observation_window_seconds"],1800)
            self.assertEqual(d["repeat_observation"],expected)
            self.assertEqual(d["timeline"][0]["evidence"]["repeat_observation"],expected)
            self.assertEqual(feed(s,l,2080)["incident_id"],ident)
            self.assertEqual(l.incidents.detail(ident)["revision"],1)
            for t in (2260,2440):feed(s,l,t)
            for t in (2620,2800,2980):feed(s,l,t,20)
            d=Incidents(Store(root,mode="diagnostic")).detail(ident)
            self.assertEqual(d["state"],"RESOLVED")
            self.assertEqual(d["repeat_observation"],expected)
            self.assertEqual(d["timeline"][0]["evidence"]["repeat_observation"],expected)
            self.assertNotIn("02:00:00:00:00:01",json.dumps(d))
            from awqm.incident_reports import summary,incident_html
            self.assertIn("30분 이내 반복 RF 위험 관측",summary(d))
            self.assertIn("30분 이내 반복 RF 위험 관측",incident_html(d).decode("utf-8"))

    def test_repeat_observation_boundary_and_intervening_context(self):
        for gap,changed,eligible in ((1800,False,True),(1801,False,False),
                                     (180,True,False)):
            with self.subTest(gap=gap,changed=changed), tempfile.TemporaryDirectory() as root:
                s=Store(root,mode="diagnostic");l=QualityLedger(s)
                for t in (1000,1180,1360):
                    old=feed(s,l,t)["incident_id"]
                for t in (1540,1720,1900):feed(s,l,t,20)
                self.assertEqual(l.incidents.detail(old)["state"],"RESOLVED")
                self.assertTrue(l.incidents.detail(old)["repeat_observation_assessed"])
                self.assertIsNone(l.incidents.detail(old)["repeat_observation"])
                if changed:
                    # A changed context even without a new incident prevents
                    # a later return to the old context from counting as repeat.
                    feed(s,l,1990,20,connection="replacement")
                ident=feed(s,l,1900+gap)["incident_id"]
                self.assertNotEqual(ident,old)
                d=l.incidents.detail(ident)
                self.assertTrue(d["repeat_observation_assessed"])
                self.assertEqual(d["repeat_observation"] is not None,eligible)
                self.assertEqual(d["prior_resolved_observation"]["incident_id"],old)
                if eligible:self.assertEqual(d["repeat_observation"]["gap_seconds"],1800)

    def test_prior_observation_requires_resolved_persistent_context_and_window(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s);i=l.incidents
            transient=feed(s,l,1000)["incident_id"]
            feed(s,l,1180,20)
            self.assertEqual(i.detail(transient)["state"],"TRANSIENT")
            first=feed(s,l,1360)["incident_id"]
            self.assertIsNone(i.detail(first)["prior_resolved_observation"])
            for at in (1540,1720):feed(s,l,at)
            for at in (1900,2080,2260):feed(s,l,at,20)
            self.assertEqual(i.detail(first)["state"],"RESOLVED")
            # Exact inclusive 30-day floor, no claim of service continuity.
            boundary=2260+30*86400
            # Fixture approval expires at 10000. Exercise the bound using
            # actual committed observations with an explicitly extended fixture.
            def later(at,connection="session"):
                radio=observation(at);s.save(radio)
                snapshot=replace(snap(at,connection=connection),field_until=at+3600)
                return l.evaluate([radio],[snapshot],at,{radio.radio_id:at})[radio.radio_id]["incident_id"]
            new=later(boundary)
            self.assertEqual(i.detail(new)["prior_resolved_observation"]["incident_id"],first)
            other=later(boundary+1,connection="different")
            self.assertIsNone(i.detail(other)["prior_resolved_observation"])
            self.assertEqual(i.detail(new)["state"],"CONTEXT_LOST")
            beyond=later(boundary+2,connection="session")
            self.assertIsNone(i.detail(beyond)["prior_resolved_observation"])
            self.assertEqual(i.detail(first)["state"],"RESOLVED")
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
