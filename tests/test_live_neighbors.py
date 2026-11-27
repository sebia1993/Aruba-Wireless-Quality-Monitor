import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace
from dataclasses import replace
from awqm.live_neighbors import LiveNeighbors,NeighborApproval,load_neighbor_approvals,CHECKS,COMMAND
from awqm.rf_neighbors import ChannelBlock
from awqm.field_validation import Approval
from awqm.aos8 import ControllerSnapshot
from awqm.model import Radio
from awqm.worker import Reading,ControllerWorker
from awqm.storage import Store
from awqm.quality_ledger import QualityLedger
from awqm.diagnostic_view import DiagnosticView
from awqm.collector import command

B1="02:ff:00:01:00:01";B2="02:ff:00:02:00:01";UNKNOWN="02:ff:00:03:00:01"
def approvals():
    blocks={("5GHz","44+"):ChannelBlock("5GHz",44,(44,48),True),("5GHz","48-"):ChannelBlock("5GHz",48,(44,48),True)}
    return {k:NeighborApproval("tag-"+k,900,1900,True,0,blocks) for k in ("w1","w2")}
def data(t):
    r=[Radio("site:02:00:00:00:00:01:0","ap1","AP1","w1","5GHz",44,t,90,5,-94,5,
             source="aos8-cli-unvalidated",epoch="w1:5GHz:AP:44+"),
       Radio("site:02:00:00:00:00:02:0","ap2","AP2","w2","5GHz",48,t,90,5,-94,5,
             source="aos8-cli-unvalidated",epoch="w2:5GHz:AP:48-")]
    ss=[];frames={}
    for i,(radio,bss) in enumerate(zip(r,(B1,B2)),1):
        ip=f"192.0.2.{i}";w=radio.controller
        s=ControllerSnapshot(w,t,[],[{"name":radio.ap_name,"ip":ip,"roles":["A"]}],[],
            field_tag="tag-"+w,field_until=2000,field_connection="session-"+w)
        ss.append(s)
        frames[w]=(s,Reading("show ap bss-table","success",t,[{"bssid":bss,"ap_name":radio.ap_name,"ip":ip,
            "band":"5GHz","channel":radio.channel,"width":"40MHz","service":True}],generation=1))
    q={x.radio_id:{"observed_at":t,"status":"PERSISTENT","score":50} for x in r}
    return r,ss,frames,q
def neighbor(t,bssid=B2,discovery="active",raw="48-"):
    return {"bssid":bssid,"band":"5GHz","primary_channel":48,"width_candidate":40,
            "channel_raw":raw,"snr":35,"discovery":discovery,
            "last_update_raw":datetime.fromtimestamp(t,timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}

class LiveNeighborTests(unittest.TestCase):
    def test_approval_requires_explicit_block(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();cfg=SimpleNamespace(root=p,site="fixture")
            radio=Approval("w1","192.0.2.1","model","8.10.0.24","key",900,2000,"tag-w1")
            self.assertEqual(load_neighbor_approvals(cfg,{"w1":radio},1000),{})
            row={"controller":"w1","radio_tag":"tag-w1","approved_at":950,"valid_until":1800,"automatic":True,
                 "timezone_minutes":540,"blocks":[{"band":"5GHz","raw":"44+","subchannels":[44,48]}],
                 "reviewed_by":"fixture","checks":{k:True for k in CHECKS}}
            d={"version":1,"enabled":True,"site":"fixture","contract":"aos8-bssid-neighbors-v1","controllers":[row]}
            f=p/"config/neighbor-validation.json";f.write_text(json.dumps(d))
            a=load_neighbor_approvals(cfg,{"w1":radio},1000)["w1"]
            self.assertIsNotNone(a.block("5GHz","44+",40));self.assertIsNone(a.block("5GHz","44+",80))
            row["blocks"][0]["subchannels"]=[40,44];f.write_text(json.dumps(d))
            with self.assertRaises(ValueError):load_neighbor_approvals(cfg,{"w1":radio},1000)
    def test_mapping_candidates_and_context(self):
        r,s,f,q=data(1000);n=LiveNeighbors(approvals(),key=b"fixture")
        index=n.index(r,s,f,1000);self.assertEqual(set(index),{B1,B2})
        reading=Reading(COMMAND,"success",1005,[neighbor(1000),neighbor(1000,UNKNOWN)],generation=1,target=B1)
        n.ingest(reading,"w1",s[0],index,q,1005)
        view=n.publish(r,s,q,1005)[r[0].radio_id]
        self.assertEqual(view["rows"][0]["candidate"],"RF05")
        self.assertIsNone(view["rows"][1]["candidate"])
        self.assertNotIn(B2,json.dumps(view))
        other=replace(s[1],field_connection="new")
        self.assertIsNone(n.publish(r,[s[0],other],q,1005)[r[0].radio_id]["rows"][0]["candidate"])
        n.ingest(replace(reading,data=[neighbor(1000,discovery="passive")]),"w1",s[0],index,q,1005)
        self.assertIsNone(n.publish(r,s,q,1005)[r[0].radio_id]["rows"][0]["candidate"])
        n.ingest(replace(reading,data=[neighbor(1000,raw="48+")]),"w1",s[0],index,q,1005)
        self.assertIsNone(n.publish(r,s,q,1005)[r[0].radio_id]["rows"][0]["candidate"])
        self.assertEqual(n.publish(r,s,q,1100),{})
    def test_incident_revision_and_masked_view(self):
        with tempfile.TemporaryDirectory() as root:
            st=Store(root,mode="diagnostic");l=QualityLedger(st)
            for t in (1000,1180,1360):
                r,s,f,q=data(t)
                st.save_batch(r);q=l.evaluate(r,s,t,{x.radio_id:t for x in r})
            n=LiveNeighbors(approvals(),l.incidents,key=b"fixture")
            ix=n.index(r,s,f,1360)
            old=l.incidents.detail(q[r[0].radio_id]["incident_id"])
            read=Reading(COMMAND,"success",1365,[neighbor(1360)],generation=1,target=B1)
            n.ingest(read,"w1",s[0],ix,q,1365)
            d=l.incidents.detail(old["incident_id"])
            self.assertEqual(d["revision"],old["revision"]+1)
            self.assertIn("RF05",[c["code"] for c in d["evidence"]["candidates"]])
            peer=q[r[1].radio_id]["incident_id"]
            link=d["evidence"]["neighbor_evidence"][0]
            self.assertEqual(link["ap_name"],"AP2")
            self.assertEqual(link["peer_incident_id"],peer)
            self.assertEqual(link["peer_incident_revision"],l.incidents.detail(peer)["revision"])
            self.assertNotIn(r[1].radio_id,json.dumps(d))
            self.assertNotIn("session-w2",json.dumps(d))
            self.assertNotIn(B2,json.dumps(d))
            l.incidents.review(d["incident_id"],d["revision"],"review","op","supported","radio_evidence","RF05")
            n.ingest(read,"w1",s[0],ix,q,1365)
            self.assertEqual(l.incidents.detail(d["incident_id"])["revision"],d["revision"])
            now=[1365];v=DiagnosticView(st,lambda:now[0])
            v.publish({},[(x.radio_id,x.observed_at) for x in r],quality=q,neighbor_views=n.publish(r,s,q,1365),neighbor_enabled=True)
            result=v.neighbors(v.token(r[0].radio_id))
            self.assertEqual(result["rows"][0]["candidate"],"RF05")
            self.assertNotIn(r[1].radio_id,json.dumps(result));self.assertNotIn(B2,json.dumps(result))
            related=l.incidents.detail(peer)
            self.assertEqual(related["incident_id"],peer)
            self.assertEqual(related["state"],"PERSISTENT")
            now[0]=1386;self.assertFalse(v.neighbors(v.token(r[0].radio_id))["available"])

    def test_neighbor_link_requires_exact_persistent_peer_context(self):
        with tempfile.TemporaryDirectory() as root:
            st=Store(root,mode="diagnostic");l=QualityLedger(st)
            for t in (1000,1180,1360):
                r,s,f,q=data(t);st.save_batch(r)
                q=l.evaluate(r,s,t,{x.radio_id:t for x in r})
            n=LiveNeighbors(approvals(),l.incidents,key=b"fixture")
            ix=n.index(r,s,f,1360)
            source=q[r[0].radio_id]["incident_id"];peer=q[r[1].radio_id]["incident_id"]
            read=Reading(COMMAND,"success",1365,[neighbor(1360)],generation=1,target=B1)
            # Wrong incident ID cannot be linked by display name.
            wrong={k:dict(v) for k,v in q.items()}
            wrong[r[1].radio_id]["incident_id"]=source
            n.ingest(read,"w1",s[0],ix,wrong,1365)
            d=l.incidents.detail(source)
            self.assertNotIn("peer_incident_id",d["evidence"]["neighbor_evidence"][0])
            # A real peer ID with stale connection context cannot be linked
            # after its context changed in the committed ledger.
            target=replace(r[1],observed_at=1370)
            new_snap=replace(s[1],observed_at=1370,field_connection="replacement")
            st.save(target)
            l.evaluate([target],[new_snap],1370,{target.radio_id:1370})
            n.ingest(replace(read,observed_at=1370),"w1",s[0],ix,q,1370)
            self.assertNotIn("peer_incident_id",l.incidents.detail(source)["evidence"]["neighbor_evidence"][0])
            self.assertEqual(l.incidents.detail(peer)["state"],"CONTEXT_LOST")
            self.assertEqual(l.incidents.detail(source)["state"],"PERSISTENT")

    def test_neighbor_link_requires_current_immutable_evidence_revision(self):
        with tempfile.TemporaryDirectory() as root:
            st=Store(root,mode="diagnostic");l=QualityLedger(st)
            n=LiveNeighbors(approvals(),l.incidents,key=b"fixture")
            for t in (1000,1180,1360,1420,1480):
                r,s,f,_=data(t)
                if t==1480:r[1]=replace(r[1],interference=35)
                st.save_batch(r);q=l.evaluate(r,s,t,{x.radio_id:t for x in r})
                if t<1360:continue
                source=q[r[0].radio_id]["incident_id"]
                peer=q[r[1].radio_id]["incident_id"]
                ix=n.index(r,s,f,t)
                read=Reading(COMMAND,"success",t+5,[neighbor(t)],generation=1,target=B1)
                n.ingest(read,"w1",s[0],ix,q,t+5)
                d=l.incidents.detail(source)
                link=d["evidence"]["neighbor_evidence"][0]
                self.assertEqual(link["candidate"],"RF05")
                target=l.incidents.detail(peer)
                if t==1420:
                    # Unchanged persistent quality advanced last_at but not
                    # the immutable evidence revision for the peer.
                    self.assertEqual(target["last_at"],t)
                    self.assertEqual(target["evidence"]["observed_at"],1360)
                    self.assertNotIn("peer_incident_id",link)
                    self.assertNotIn("peer_incident_revision",link)
                else:
                    self.assertEqual(target["evidence"]["observed_at"],t)
                    self.assertEqual(link["peer_incident_id"],peer)
                    self.assertEqual(link["peer_incident_revision"],target["revision"])
            self.assertNotIn(r[1].radio_id,json.dumps(d))
            self.assertNotIn(B2,json.dumps(d))
            self.assertTrue(any(e["evidence"].get("neighbor_evidence",[{}])[0].get("peer_incident_id")==peer
                                for e in d["timeline"] if e["evidence"].get("neighbor_evidence")))
            # A future or inconsistent event must not be used as a current
            # reference. Corruption is injected only into the temporary DB.
            with st.lock,st._db(st.root/"core.db") as c:
                c.execute("UPDATE rf_incident_events SET at=? WHERE incident_id=? AND revision=?",
                          (1490,peer,target["revision"]))
            n.ingest(read,"w1",s[0],ix,q,1485)
            d=l.incidents.detail(source)
            self.assertNotIn("peer_incident_id",d["evidence"]["neighbor_evidence"][0])
            with st.lock,st._db(st.root/"core.db") as c:
                c.execute("UPDATE rf_incident_events SET at=?,evidence=? WHERE incident_id=? AND revision=?",
                          (1480,"{}",peer,target["revision"]))
            n.ingest(replace(read,observed_at=1486),"w1",s[0],ix,q,1486)
            d=l.incidents.detail(source)
            self.assertNotIn("peer_incident_revision",d["evidence"]["neighbor_evidence"][0])
            self.assertIn("RF05",[x["code"] for x in d["evidence"]["candidates"]])

    def test_worker_query_gate(self):
        from awqm.fixture_session import FixtureSession
        from awqm.client import bss_table
        from test_rf_neighbors import fixture
        from unittest.mock import patch
        s=FixtureSession(1);t=[1000]
        s.responses["show version"]="ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24"
        s.responses[COMMAND]=fixture()
        b=next(x for x in bss_table(s.responses["show ap bss-table"]) if x["band"]=="5GHz")["bssid"]
        a=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","key",900,2000,"tag-w1")
        w=ControllerWorker("w1",lambda:s,lambda:t[0],lambda:t[0],mode="standard",enable_bss=True,
            field_approval=a,live_detail=True,neighbor_approval=approvals()["w1"])
        with patch("awqm.worker.fingerprint",return_value="key"):
            for _ in range(7):self.assertEqual(w.tick().status,"success")
            j=w.submit_detail("neighbor-fixture",COMMAND,b)
            self.assertTrue(w._live_detail_allowed(j));self.assertTrue(w._detail_target(j))
            reading=w.tick();self.assertEqual(reading.command,COMMAND);self.assertEqual(reading.status,"success")
            self.assertIsNone(w.jobs.get(j.job_id));self.assertFalse(w.detail_latest)
            w.neighbor_approval=None
            j=w.submit_detail("neighbor-no-approval",COMMAND,b)
            self.assertFalse(w._live_detail_allowed(j))
        self.assertEqual(command(COMMAND,b),COMMAND+" "+b)
        with self.assertRaises(ValueError):command(COMMAND,b+"\nshow version")
        w.close()
