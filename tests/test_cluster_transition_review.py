import json,tempfile,unittest
from dataclasses import replace

from awqm.aos8 import ControllerSnapshot,merge_radios
from awqm.cluster_transition_review import ClusterTransitionReview
from awqm.diagnostic_view import DiagnosticView
from awqm.quality_ledger import QualityLedger
from awqm.storage import Store


class ClusterTransitionReviewTests(unittest.TestCase):
    def setUp(self):
        self.review=ClusterTransitionReview(("w1","w2"))
        self.frames={n:{"connected":True,"_field_connection":n+":1"} for n in ("w1","w2")}
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name,mode="diagnostic")
        self.ledger=QualityLedger(self.store)

    def sample(self,at,owner,*,risk=False,channel=44,split=False):
        inv=[{"name":"private-ap","ip":"192.0.2.10","mac":"00:11:22:33:44:55"}]
        active=[{"name":"private-ap","ip":"192.0.2.10","roles":["A"],
                 "radios":[{"band":"5GHz","channel":channel,"radio_id":0,"clients":1}]}]
        rf=[{"name":"private-ap","ip":"192.0.2.10","service":True,"band":"5GHz",
             "channel":channel,"mode":"AP","utilization":90 if risk else 20,
             "interference":30 if risk else 2,"noise":-95}]
        snaps=[ControllerSnapshot(n,at,inv,active if n==owner or split else [],
                    rf if n==owner else [],inventory_at=at,anchor_at=at,
                    field_tag="approval-"+n,field_until=1400,field_connection=n+":1",
                    field_profile="standard") for n in ("w1","w2")]
        radios,_=merge_radios(snaps,site="test-site")
        self.store.save_batch(radios)
        q=self.ledger.evaluate(radios,snaps,at,{r.radio_id:r.observed_at for r in radios})
        return snaps,radios,q

    def observe(self,sample,now):
        return self.review.observe(sample[0],self.frames,sample[1],sample[2],now)

    def test_merge_ledger_transition_and_web_expiry(self):
        self.assertEqual(self.observe(self.sample(1000,"w1"),1000)["status"],"not_evaluated")
        after=self.sample(1060,"w2",risk=True)
        observed=self.observe(after,1060)
        self.assertEqual(observed["rows"],[{"from":"w1","to":"w2","ap_count":1,"radio_count":1}])
        self.assertEqual(observed["cause_assessment"],"withheld_epoch_change")
        self.assertEqual(observed["service_impact"],"unverified")
        self.assertFalse(observed["verified"])
        self.assertNotIn("private-ap",json.dumps(observed))
        self.assertNotIn("00:11",json.dumps(observed))
        self.assertEqual(self.observe(after,1061),observed)
        clock=[1061]
        view=DiagnosticView(self.store,lambda:clock[0])
        meta={"cluster":{"nodes":[],"transition_review":observed}}
        view.publish(meta)
        public=view.page()["cluster"]["transition_review"]
        self.assertEqual(public["rows"],observed["rows"])
        self.assertNotIn("valid_until",public)
        clock[0]=observed["valid_until"]
        view.publish(meta)
        self.assertEqual(view.page()["cluster"]["transition_review"],
            {"status":"not_evaluated","reason":"stale_or_paused"})
        clock[0]=1061
        view.publish(dict(meta,paused=True))
        self.assertEqual(view.page()["cluster"]["transition_review"]["status"],"not_evaluated")
        self.assertIn("valid_until",observed)

    def test_partial_cluster_conflict_changed_channel_and_approval(self):
        self.observe(self.sample(1000,"w1"),1000)
        after=self.sample(1060,"w2",risk=True)
        self.assertEqual(self.observe((after[0][:1],after[1],after[2]),1060)["reason"],
                         "complete_cluster_required")
        self.assertEqual(self.observe(after,1060)["status"],"not_evaluated")
        self.review.clear()
        self.observe(self.sample(1000,"w1"),1000)
        # Both WLCs claim active ownership: no merged physical Radio.
        conflict=self.sample(1060,"w2",risk=True,split=True)
        self.assertFalse(conflict[1])
        self.assertEqual(self.observe(conflict,1060)["status"],"not_evaluated")
        self.review.clear()
        self.observe(self.sample(1120,"w1"),1120)
        moved=self.sample(1180,"w2",risk=True,channel=48)
        self.assertEqual(self.observe(moved,1180)["status"],"not_evaluated")
        self.review.clear()
        self.observe(self.sample(1240,"w1"),1240)
        moved=self.sample(1300,"w2",risk=True)
        self.frames["w1"]["_field_connection"]="w1:2"
        self.assertEqual(self.observe(moved,1300)["reason"],"matching_attestation_required")

    def test_replay_and_unaligned_window(self):
        self.observe(self.sample(1000,"w1"),1000)
        after=self.sample(1060,"w2",risk=True)
        self.assertEqual(self.observe(after,1060)["status"],"transition_observation")
        changed=(after[0],[replace(after[1][0],utilization=91)],after[2])
        self.assertEqual(self.observe(changed,1060)["reason"],"radio_observation_unusable")
        self.assertEqual(self.observe(after,1061)["status"],"not_evaluated")
        later=self.sample(1120,"w2",risk=True)
        bad=([replace(later[0][0],anchor_at=1089),later[0][1]],later[1],later[2])
        self.assertEqual(self.observe(bad,1120)["reason"],"time_unaligned")
