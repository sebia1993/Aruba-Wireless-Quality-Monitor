import json,tempfile,unittest
from dataclasses import replace

from awqm.aos8 import ControllerSnapshot,merge_radios
from awqm.cluster_transition_review import ClusterTransitionReview
from awqm.diagnostic_view import DiagnosticView
from awqm.quality_ledger import QualityLedger
from awqm.storage import Store
from awqm.cluster import MEMBERSHIP,group_membership
from awqm.worker import Reading


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

    def test_old_window_replay_cannot_recreate_transition(self):
        before=self.sample(1000,"w1")
        self.observe(before,1000)
        after=self.sample(1060,"w2",risk=True)
        result=self.observe(after,1060)
        self.assertEqual(result["status"],"transition_observation")
        self.assertEqual(self.observe(after,1061),result)
        self.assertEqual(self.observe(before,1062)["reason"],"window_replay_or_overlap")
        self.assertEqual(self.observe(after,1063)["reason"],"window_replay_or_overlap")
        # A previously seen frame must not become a new baseline even
        # after a complete-cluster failure clears the comparison state.
        self.assertEqual(self.observe((after[0][:1],after[1],after[2]),1064)["reason"],
                         "complete_cluster_required")
        self.assertEqual(self.observe(before,1065)["reason"],"window_replay_or_overlap")
        new_before=self.sample(1120,"w2")
        self.assertEqual(self.observe(new_before,1120)["reason"],"new_window_required")
        new_after=self.sample(1180,"w1",risk=True)
        self.assertEqual(self.observe(new_after,1180)["rows"],
                         [{"from":"w2","to":"w1","ap_count":1,"radio_count":1}])

    def test_overlap_and_same_envelope_source_time_change(self):
        before=self.sample(1000,"w1")
        self.observe(before,1000)
        overlap=self.sample(1020,"w2")
        mixed=([replace(overlap[0][0],anchor_at=995),overlap[0][1]],overlap[1],overlap[2])
        self.assertEqual(self.observe(mixed,1020)["reason"],"window_replay_or_overlap")
        self.assertEqual(self.observe(overlap,1021)["reason"],"window_replay_or_overlap")
        after=self.sample(1060,"w1",risk=True)
        self.assertEqual(self.observe(after,1060)["reason"],"new_window_required")
        # Identical min/max times do not establish identical source windows.
        self.review.clear()
        normal=self.sample(1120,"w2")
        original=([replace(normal[0][0],anchor_at=1110),normal[0][1]],normal[1],normal[2])
        changed=([original[0][0],replace(original[0][1],anchor_at=1115)],normal[1],normal[2])
        self.assertEqual(self.observe(original,1120)["reason"],"new_window_required")
        self.assertEqual(self.observe(changed,1121)["reason"],"window_replay_or_overlap")
        self.assertEqual(self.observe(original,1122)["reason"],"window_replay_or_overlap")
        self.assertEqual(self.observe(self.sample(1180,"w1",risk=True),1180)["reason"],
                         "new_window_required")

    def test_clock_rollback_withholds_public_evidence_until_fresh_windows(self):
        before=self.sample(1000,"w1")
        self.observe(before,1000)
        after=self.sample(1060,"w2",risk=True)
        self.assertEqual(self.observe(after,1060)["status"],"transition_observation")
        withheld=self.observe(before,1059)
        self.assertEqual(withheld,{"status":"not_evaluated","reason":"window_replay_or_overlap"})
        self.assertEqual(self.observe(after,1060),withheld)
        view=DiagnosticView(self.store,lambda:1060)
        view.publish({"cluster":{"nodes":[],"transition_review":withheld}})
        self.assertEqual(view.page()["cluster"]["transition_review"],withheld)
        self.assertEqual(self.observe(self.sample(1120,"w2"),1120)["reason"],"new_window_required")
        self.assertEqual(self.observe(self.sample(1180,"w1",risk=True),1180)["status"],
                         "transition_observation")

    def membership(self,at,*,peer_state="CONNECTED",omit=False,skew=0):
        targets={"w1":"192.0.2.1","w2":"192.0.2.2"}
        for name,address in targets.items():
            peer=next(n for n in targets if n!=name)
            raw=("Cluster Info Table\n------------------\n"
                 "Type IPv4 Address Priority Connection-Type STATUS\n"
                 "---- --------------- -------- --------------- ------\n"
                 f"self {address} 128 N/A CONNECTED (Member)\n")
            if not (omit and name=="w1"):
                raw+=f"peer {targets[peer]} 128 L2-Connected {peer_state if name=='w1' else 'CONNECTED'}\n"
            self.frames[name]["readings"]={MEMBERSHIP:Reading(MEMBERSHIP,"success",
                at+skew if name=="w1" else at,group_membership(raw))}
        return targets

    def with_membership(self,sample,now,**kwargs):
        targets=self.membership(now,**kwargs)
        return self.review.observe(sample[0],self.frames,sample[1],sample[2],now,targets=targets)

    def test_independent_membership_comparison_and_publication(self):
        self.with_membership(self.sample(1000,"w1"),1000)
        after=self.sample(1060,"w2",risk=True)
        result=self.with_membership(after,1060,peer_state="DISCONNECTED")
        evidence=result["membership_context"]
        self.assertEqual(evidence,{"status":"observed","scope":"field_unvalidated_cluster_observations",
            "prior_start":1000,"prior_end":1000,"current_start":1060,"current_end":1060,
            "directional_observations":4,"changed_observations":1,"current_peer_self_disagreements":1})
        self.assertEqual(result["cause_assessment"],"withheld_epoch_change")
        self.assertFalse(result["verified"])
        view=DiagnosticView(self.store,lambda:1060)
        view.publish({"cluster":{"nodes":[],"transition_review":result}})
        public=view.page()["cluster"]["transition_review"]
        self.assertEqual(public["membership_context"],evidence)
        self.assertNotIn("192.0.2.",json.dumps(public))
        changed=self.with_membership(after,1060)
        self.assertEqual(changed["status"],"transition_observation")
        self.assertEqual(changed["membership_context"]["reason"],"membership_observation_changed")
        self.assertEqual(self.with_membership(after,1060,peer_state="DISCONNECTED")["membership_context"],
                         changed["membership_context"])

    def test_incomplete_unaligned_and_overlapping_membership_not_compared(self):
        for at,options in ((1000,{"omit":True}),(1120,{"skew":-31})):
            with self.subTest(options=options):
                self.review.clear()
                self.with_membership(self.sample(at,"w1"),at)
                result=self.with_membership(self.sample(at+60,"w2",risk=True),at+60,**options)
                self.assertEqual(result["status"],"transition_observation")
                self.assertEqual(result["membership_context"],{"status":"not_evaluated",
                    "reason":"complete_aligned_membership_required"})
        self.review.clear()
        self.with_membership(self.sample(1240,"w1"),1240)
        result=self.with_membership(self.sample(1270,"w2",risk=True),1270,skew=-30)
        self.assertEqual(result["status"],"transition_observation")
        self.assertEqual(result["membership_context"]["status"],"not_evaluated")
        self.review.clear()
        self.with_membership(self.sample(1300,"w1"),1300)
        result=self.with_membership(self.sample(1360,"w2",risk=True),1360)
        self.assertEqual(result["membership_context"]["changed_observations"],0)
        self.assertEqual(result["cause_assessment"],"withheld_epoch_change")
