import hmac,json,tempfile,unittest
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
def frame(t,bssid=BSS,conflict=False,anchor="w1"):
    peer = "w2" if anchor == "w1" else "w1"
    r=Radio("lab:02:ab:00:00:00:01:0","lab:02:ab:00:00:00:01","AP1",anchor,"5GHz",44,t,70,10,-95,source="aos8-cli-unvalidated")
    row={"client_mac":MAC,"ap_name":"AP1","bssid":bssid,"ssid":SSID,"active":True}
    inventory=[{"name":"AP1","ip":"192.0.2.1","mac":"02:ab:00:00:00:01"}]
    snaps=[ControllerSnapshot(w,t,inventory,[{"name":"AP1","ip":"192.0.2.1","roles":["A"]}] if w==anchor else [],[],[row] if w==anchor else [],
            inventory_at=t,anchor_at=t,association_at=t,field_tag="tag-"+w,field_until=4000000,field_connection="session-"+w) for w in (anchor,peer)]
    b=Reading("show ap bss-table","success",t,[{"bssid":bssid,"ssid":SSID,"ap_name":"AP1","ip":"192.0.2.1","band":"5GHz","channel":44,"service":True,"forward_mode":"T"}],generation=1)
    user=dict(row,forward_mode="tunnel")
    reading=lambda cmd:Reading(cmd,"success",t,[user],generation=1)
    frames={anchor:(snaps[0],b,{COMMANDS[1]:reading(COMMANDS[1]),**({COMMANDS[0]:reading(COMMANDS[0])} if conflict else {})}),peer:(snaps[1],None,{COMMANDS[0]:reading(COMMANDS[0])})}
    return [r],snaps,frames,{MAC:row}

class AssociationTests(unittest.TestCase):
    def test_old_and_unknown_physical_mapping_records_survive_upgrade_without_claiming_continuity(self):
        def token(kind, value):
            return hmac.digest(KEY, ("lab\0" + kind + "\0" + value).encode(), "sha256").hex()[:32]

        bare = token("context", "session-w1|1")
        ident = token("device", MAC)
        for context in (bare, "wired-ap-v2:" + bare, "wired-ap-v1:incomplete"):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as root:
                store = Store(root, mode="diagnostic")
                path = store.root / "association.db"
                radio = frame(1000)[0][0]
                legacy = (ident, 1000, 1060, token("ap", radio.ap_id), "AP1", token("radio", radio.radio_id),
                          radio.band, token("bss", BSS), token("network", SSID), "w1", context,
                          "w2", "w1", "positive_table_observation", "attachment_changed")
                with store._db(path) as db:
                    db.execute("""CREATE TABLE association_segments(
                        id TEXT NOT NULL,first_at INTEGER NOT NULL,last_at INTEGER NOT NULL,
                        ap_token TEXT NOT NULL,ap_name TEXT NOT NULL,radio_token TEXT NOT NULL,band TEXT NOT NULL,
                        bss_token TEXT NOT NULL,network_token TEXT NOT NULL,aac TEXT NOT NULL,
                        context_token TEXT NOT NULL,auac TEXT,suac TEXT,uac_status TEXT NOT NULL,
                        event TEXT NOT NULL,PRIMARY KEY(id,first_at))""")
                    db.execute("INSERT INTO association_segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", legacy)
                history = AssociationHistory(store, "lab", KEY, approvals())
                self.assertEqual(history.ingest(*frame(1120), 1120), 1)
                self.assertEqual(history.ingest(*frame(1180), 1180), 0)
                reopened = AssociationHistory(store, "lab", KEY, {"w1": approvals()["w1"]})
                view = DiagnosticView(store, lambda: 1180, association_history=reopened)
                for query in ("", ident):
                    page = view.association_page(query, 1)
                    self.assertEqual(page["total"], 2)
                    new, old = page["rows"]
                    self.assertEqual((new["first_at"], new["last_at"], new["event"], new["mapping_status"]),
                                     (1120, 1180, "first_observation", "wired_inventory_matched"))
                    self.assertEqual((old["first_at"], old["last_at"], old["event"], old["mapping_status"]),
                                     (1000, 1060, "physical_mapping_unverified", "unverified"))
                    self.assertIsNone(old["auac"])
                    self.assertEqual(old["suac"], "w1")
                    self.assertNotIn(context, json.dumps(page))
                with store._db(path) as db:
                    self.assertEqual(db.execute("SELECT * FROM association_segments WHERE first_at=1000").fetchone(), legacy)

    def test_same_named_aps_must_join_bss_to_its_own_physical_radio(self):
        cases = (("other AP owns channel", 36, False, False),
                 ("exact IP binds two cochannel APs", 44, False, True),
                 ("conflicting inventory at BSS IP", 44, True, False))
        for label, first_channel, conflict, accepted in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                radios, snaps, frames, claims = frame(1000)
                first = replace(radios[0], channel=first_channel)
                second_id = "lab:02:ab:00:00:00:02"
                second = replace(first, ap_id=second_id, radio_id=second_id + ":0", channel=44)
                owner = snaps[0]
                other = dict(owner.inventory[0], mac="02:ab:00:00:00:02", ip="192.0.2.2")
                inventory = [*owner.inventory, other]
                if conflict:
                    inventory.append(dict(other, ip=owner.inventory[0]["ip"]))
                snaps[0] = replace(owner, inventory=inventory,
                                   active=[*owner.active, dict(owner.active[0], ip=other["ip"])])
                frames["w1"] = (snaps[0], *frames["w1"][1:])
                store = Store(root, mode="diagnostic")
                history = AssociationHistory(store, "lab", KEY, approvals())
                history.ingest([first, second], snaps, frames, claims, 1000)
                with store._db(store.root / "association.db") as db:
                    saved = db.execute("SELECT ap_token,radio_token FROM association_segments").fetchall()
                expected = [(history.token("ap", first.ap_id), history.token("radio", first.radio_id))]
                self.assertEqual(saved, expected if accepted else [])

    def test_unmapped_active_claim_blocks_duplicate_bssid_until_refresh(self):
        radios, snaps, frames, claims = frame(1000)
        owner, bss, users = frames["w1"]
        second = dict(owner.active[0], ip="192.0.2.2")
        snaps[0] = replace(owner, active=[*owner.active, second])
        duplicate = dict(bss.data[0], ip=second["ip"])
        frames["w1"] = (snaps[0], replace(bss, data=[*bss.data, duplicate]), users)
        with tempfile.TemporaryDirectory() as root:
            history = AssociationHistory(Store(root, mode="diagnostic"), "lab", KEY, approvals())
            self.assertEqual(history.ingest(radios, snaps, frames, claims, 1000), 0)
            self.assertEqual(history.page(1000)["total"], 0)
            # A refreshed AAC table removes the competing claim. The BSS and association still qualify.
            snaps[0] = replace(owner, anchor_at=1001)
            frames["w1"] = (snaps[0], frames["w1"][1], users)
            self.assertEqual(history.ingest(radios, snaps, frames, claims, 1001), 1)
            self.assertEqual(history.page(1001)["rows"][0]["last_at"], 1000)

    def test_revoked_or_expired_aac_is_excluded_before_count_and_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, mode="diagnostic")
            history = AssociationHistory(store, "lab", KEY, approvals())
            history.ingest(*frame(1000), 1000)
            for i in range(55):
                at = 1060 + i * 5
                history.ingest(*frame(at, BSS2 if i % 2 else BSS, anchor="w2"), at)
            ident = history.token("device", MAC)
            self.assertEqual(history.page(1400, ident)["total"], 56)
            for permission in (None, replace(approvals()["w2"], valid_until=1400)):
                allowed = {"w1": approvals()["w1"]}
                if permission:
                    allowed["w2"] = permission
                view = AssociationHistory(store, "lab", KEY, allowed)
                for query in ("", ident):
                    result = view.page(1400, query)
                    self.assertEqual(result["total"], 1)
                    self.assertEqual([r["first_at"] for r in result["rows"]], [1000])
                    row = result["rows"][0]
                    self.assertEqual((row["aac"], row["auac"], row["suac"]), ("w1", None, "w1"))
                    self.assertEqual(row["uac_status"], "positive_table_observation")
                    self.assertNotIn("w2", json.dumps(result))
                    self.assertFalse(view.page(1400, query, 2)["rows"])

    def test_revoked_standby_and_only_positive_source_are_masked(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, mode="diagnostic")
            history = AssociationHistory(store, "lab", KEY, approvals())
            for at, keep_active in ((1000, True), (1060, False)):
                radios, snaps, frames, claims = frame(at)
                for ctrl, command in (("w1", COMMANDS[0]), ("w2", COMMANDS[1])):
                    snapshot, bss, readings = frames[ctrl]
                    source = next(iter(readings.values()))
                    frames[ctrl] = (snapshot, bss, {command: source} if keep_active or ctrl == "w2" else {})
                history.ingest(radios, snaps, frames, claims, at)
            current = AssociationHistory(store, "lab", KEY, {"w1": approvals()["w1"]})
            only_revoked, partial = current.page(1080)["rows"]
            self.assertEqual((partial["auac"], partial["suac"], partial["uac_status"]),
                             ("w1", None, "positive_table_observation"))
            self.assertEqual((only_revoked["auac"], only_revoked["suac"], only_revoked["uac_status"]),
                             (None, None, "unknown"))

    def test_renewed_grant_begins_with_next_positive_observation(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, mode="diagnostic")
            history = AssociationHistory(store, "lab", KEY, approvals())
            history.ingest(*frame(1000), 1000)
            history.ingest(*frame(1060), 1060)
            renewed = approvals()
            renewed["w1"] = replace(renewed["w1"], approved_at=1050)
            current = AssociationHistory(store, "lab", KEY, renewed)
            self.assertEqual(current.page(1070)["total"], 0)
            self.assertEqual(current.ingest(*frame(1080), 1080), 1)
            self.assertEqual([(r["first_at"], r["event"]) for r in current.page(1090)["rows"]],
                             [(1080, "first_observation")])

    def test_renewed_peer_grant_does_not_reveal_older_uac_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, mode="diagnostic")
            history = AssociationHistory(store, "lab", KEY, approvals())
            history.ingest(*frame(1000), 1000)
            renewed = approvals()
            renewed["w2"] = replace(renewed["w2"], approved_at=1050)
            current = AssociationHistory(store, "lab", KEY, renewed)
            current.ingest(*frame(1060), 1060)
            new, old = current.page(1070)["rows"]
            self.assertEqual((old["first_at"], old["auac"], old["suac"]), (1000, None, "w1"))
            self.assertEqual((new["first_at"], new["auac"], new["suac"]), (1060, "w2", "w1"))

    def test_physical_radio_only_move_is_an_observed_attachment_change(self):
        with tempfile.TemporaryDirectory() as root:
            history = AssociationHistory(Store(root, mode="diagnostic"), "lab", KEY, approvals())
            history.ingest(*frame(1000), 1000)
            radios, snaps, frames, claims = frame(1060)
            moved = replace(radios[0], radio_id=radios[0].ap_id + ":1")
            self.assertEqual(history.ingest([moved], snaps, frames, claims, 1060), 1)
            new, old = history.page(1060)["rows"]
            self.assertEqual(new["event"], "attachment_changed")
            self.assertEqual((old["last_at"], new["first_at"]), (1000, 1060))

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
