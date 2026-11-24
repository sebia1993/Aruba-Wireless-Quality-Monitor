import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from awqm.association_history import AssociationHistory,HistoryApproval,CHECKS,CONTRACT,COMMANDS,user_table,load_history_approvals,key_envelope,load_key
from awqm.field_validation import Approval
from awqm.storage import Store
from awqm.retention import Retention
from awqm.maintenance import verified_backup
from awqm.aos8 import ControllerSnapshot,ParseError
from awqm.model import Radio
from awqm.worker import Reading,ControllerWorker
from awqm.diagnostic_view import DiagnosticView

MAC="02:ab:00:00:00:11";BSS="02:ab:00:00:00:12";BSS2="02:ab:00:00:00:13";SSID="fictional lab ssid"
KEY=b"x"*32
def table():
    fields=["IP","MAC","Name","Role","Age(d:h:m)","Auth","VPN link","AP name","Roaming","Essid/Bssid/Phy","Profile","Forward mode","Type"]
    sizes=[20,20,24,18,15,12,14,18,16,60,16,16,12]
    def line(row):return "  ".join(v.ljust(n) for v,n in zip(row,sizes))
    return "\n".join(["Users","-----",line(fields),line(["-"*n for n in sizes]),line(["192.0.2.99",MAC,"fictional-user","fictional-role","00:00:45","802.1X","","AP1","Associated",SSID+"/"+BSS+"/a-HT","default","tunnel",""]),"User Entries: 1/1","Curr/Cum Alloc:1/1 Free:0/0 Dyn:1 AllocErr:0 FreeErr:0"])
def approvals():
    return {w:HistoryApproval("tag-"+w,900,4000000) for w in ("w1","w2")}
def frame(t,bssid=BSS,conflict=False):
    r=Radio("site:02:ab:00:00:00:01:0","site:02:ab:00:00:00:01","AP1","w1","5GHz",44,t,70,10,-95,source="aos8-cli-unvalidated")
    row={"client_mac":MAC,"ap_name":"AP1","bssid":bssid,"ssid":SSID,"active":True}
    snaps=[ControllerSnapshot(w,t,[],[{"name":"AP1","ip":"192.0.2.1","roles":["A"]}] if w=="w1" else [],[],[row] if w=="w1" else [],
            inventory_at=t,anchor_at=t,association_at=t,field_tag="tag-"+w,field_until=4000000,field_connection="session-"+w) for w in ("w1","w2")]
    b=Reading("show ap bss-table","success",t,[{"bssid":bssid,"ssid":SSID,"ap_name":"AP1","ip":"192.0.2.1","band":"5GHz","channel":44,"service":True,"forward_mode":"T"}],generation=1)
    user=dict(row,forward_mode="tunnel")
    reading=lambda cmd:Reading(cmd,"success",t,[user],generation=1)
    frames={"w1":(snaps[0],b,{COMMANDS[1]:reading(COMMANDS[1]),**({COMMANDS[0]:reading(COMMANDS[0])} if conflict else {})}),"w2":(snaps[1],None,{COMMANDS[0]:reading(COMMANDS[0])})}
    return [r],snaps,frames,{MAC:row}

class AssociationTests(unittest.TestCase):
    def test_user_table_minimization(self):
        rows=user_table(table())
        self.assertEqual(rows,[{"client_mac":MAC,"ap_name":"AP1","ssid":SSID,"bssid":BSS,"forward_mode":"tunnel"}])
        for forbidden in ("192.0.2.99","fictional-user","fictional-role"):self.assertNotIn(forbidden,json.dumps(rows))
        self.assertEqual(user_table(table().replace("tunnel","bridge")),[])
        with self.assertRaises(ParseError):user_table(table()+"\n--More--")
        with self.assertRaises(ParseError):user_table("Users\nMAC\n---\n"+MAC)
    def test_opt_in_and_key_context(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();(p/"secrets").mkdir();cfg=SimpleNamespace(root=p,site="lab")
            radio=Approval("w1","192.0.2.1","model","8.10.0.24","key",900,4000000,"tag-w1")
            self.assertEqual(load_history_approvals(cfg,{"w1":radio},False,1000),{})
            rec={"controller":"w1","radio_tag":"tag-w1","approved_at":950,"valid_until":3000000,"reviewed_by":"local","checks":{k:True for k in CHECKS}}
            (p/"config/association-history.json").write_text(json.dumps({"version":1,"enabled":True,"authorized":True,"site":"lab","contract":CONTRACT,"controllers":[rec]}))
            with self.assertRaises(ValueError):load_history_approvals(cfg,{"w1":radio},False,1000)
            self.assertIn("w1",load_history_approvals(cfg,{"w1":radio},True,1000))
            (p/"secrets/association-history.dpapi").write_bytes(key_envelope(cfg,lambda x:x))
            self.assertEqual(len(load_key(cfg,lambda x:x)),32)
            with self.assertRaises(ValueError):load_key(SimpleNamespace(root=p,site="other"),lambda x:x)
    def test_positive_history_changes_conflict_and_retention(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s=Store(p/"data",mode="diagnostic");h=AssociationHistory(s,"lab",KEY,approvals())
            self.assertEqual(h.ingest(*frame(1000),1000),1)
            self.assertEqual(h.ingest(*frame(1000),1000),0)
            self.assertEqual(h.ingest(*frame(1060),1060),0)
            rows=h.page(1060)["rows"]
            self.assertEqual((rows[0]["first_at"],rows[0]["last_at"],rows[0]["aac"],rows[0]["auac"],rows[0]["suac"]),(1000,1060,"w1","w2","w1"))
            self.assertEqual(h.ingest(*frame(1120,BSS2),1120),1)
            self.assertEqual(h.page(1120)["rows"][0]["event"],"attachment_changed")
            self.assertEqual(h.ingest(*frame(1180,BSS2,True),1180),1)
            self.assertEqual(h.page(1180)["rows"][0]["uac_status"],"conflict")
            self.assertIsNone(h.page(1180)["rows"][0]["auac"])
            self.assertEqual(h.ingest([],[],{}, {},1190),0)
            self.assertEqual(h.page(1190)["rows"][0]["last_at"],1180)
            self.assertEqual(h.ingest(*frame(1500,BSS2),1500),1)
            self.assertEqual(h.page(1500)["rows"][0]["event"],"after_observation_gap")
            ident=rows[0]["id"];self.assertEqual(h.page(1500,ident)["total"],4)
            self.assertEqual(AssociationHistory(s,"lab",KEY,approvals()).page(1500,ident)["total"],4)
            for forbidden in (MAC,BSS,BSS2,SSID):
                self.assertNotIn(forbidden,json.dumps(h.page(1500)))
                self.assertNotIn(forbidden.encode(),(s.root/"association.db").read_bytes())
            with self.assertRaises(ValueError):h.page(1500,MAC)
            backup=verified_backup(s,p/"backup"/"core-fixture")
            self.assertFalse((backup/"association.db").exists())
            later=1500+31*86400
            self.assertEqual(h.page(later)["total"],0)
            self.assertEqual(Retention(s,p/"backup").run(later)["association_segments_deleted"],4)
    def test_no_opt_in_no_user_poll(self):
        w=ControllerWorker("w1",lambda:None,enable_bss=True)
        self.assertTrue(all(c not in w.schedule.polls for c in COMMANDS))
        w=ControllerWorker("w1",lambda:None,enable_bss=True,history_approval=approvals()["w1"])
        self.assertTrue(all(c in w.schedule.polls for c in COMMANDS))
    def test_cluster_published_and_history_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic")
            v=DiagnosticView(s,lambda:1000)
            v.publish({"cluster":{"representative":"w1"}},[])
            self.assertEqual(v.page()["cluster"]["representative"],"w1")
            self.assertFalse(v.association_page("",1)["enabled"])
