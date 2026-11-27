import json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from awqm.storage import Store,day_start
from awqm.quality_ledger import QualityLedger
from awqm.aos8 import ControllerSnapshot
from awqm.model import Radio
from awqm.maintenance import verified_backup,verify,restore_new
from awqm.retention import Retention,load_retention,POLICY,readonly
from awqm.audit import AuditLog

DAY="20261101";T=day_start(DAY)+3600
def sample(t,u=90):
    return Radio("r:0","r","DEMO-AP","w1","5GHz",44,t,u,5,-94,source="aos8-cli-unvalidated")

def prepare(root,open_incident=False):
    s=Store(root/"data",mode="diagnostic");l=QualityLedger(s)
    for i,u in enumerate((90,90,90) if open_incident else (90,90,90,20,20,20)):
        t=T+i*180;r=sample(t,u);s.save(r)
        snap=ControllerSnapshot("w1",t,[],[],[],field_tag="fixture",field_until=T+86400,
            field_connection="session",field_profile="safe")
        q=l.evaluate([r],[snap],t,{r.radio_id:t})
    s.aggregate_day(DAY,day_start("20261102"))
    return s,l

class RetentionTests(unittest.TestCase):
    def test_policy_opt_in(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();cfg=SimpleNamespace(root=p)
            self.assertFalse(load_retention(cfg))
            f=p/"config/retention.json";f.write_text(json.dumps({"version":1,"enabled":True}))
            with self.assertRaises(ValueError):load_retention(cfg)
            f.write_text(json.dumps(POLICY));self.assertTrue(load_retention(cfg))
    def test_verified_backup_and_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s,l=prepare(p);backup=p/"backup";backup.mkdir()
            ret=Retention(s,backup)
            now=day_start("20261112")
            self.assertEqual(ret.run(now)["raw_deleted"],[])
            self.assertTrue((s.root/"radio_20261101.db").exists())
            dst=verified_backup(s,backup/"aggregate-fixture")
            result=ret.run(now,dst.name)
            self.assertEqual(result["raw_deleted"],[DAY])
            self.assertFalse((s.root/"radio_20261101.db").exists())
            self.assertGreater(len(s.history("r:0",T,T+3600,"5m")),0)
            self.assertEqual(l.incidents.list(start=T-1,end=T+86400)["total"],1)
            r=ret.run(day_start("20261215"),dst.name)
            self.assertGreater(r["aggregate_rows_deleted"]["5m"],0)
            self.assertEqual(s.history("r:0",T,T+3600,"5m"),[])
            self.assertGreater(len(s.history("r:0",T,T+3600,"1h")),0)
            r=ret.run(T+367*86400,dst.name)
            self.assertGreater(r["aggregate_rows_deleted"]["1h"],0)
            self.assertEqual(r["incidents_deleted"],1)
            self.assertEqual(l.incidents.list(start=T-1,end=T+86400)["total"],0)
            self.assertTrue(dst.exists()) # no automatic archive removal
    def test_open_incident_is_held(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s,l=prepare(p,True);backup=p/"backup";backup.mkdir()
            dst=verified_backup(s,backup/"aggregate-fixture")
            ret=Retention(s,backup)
            r=ret.run(day_start("20261112"),dst.name)
            self.assertEqual(r["raw_deleted"],[])
            self.assertEqual(r["raw_held"][DAY],"open_incident_evidence")

    def test_review_added_after_backup_holds_raw_until_review_is_backed_up(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            store, ledger = prepare(p)
            backups = p / "backups"
            old = verified_backup(store, backups / "aggregate-before-review")
            incident = ledger.incidents.list(start=T-1, end=T+86400)["rows"][0]
            ledger.incidents.review(incident["incident_id"], 1, "review-1", "operator",
                                    "inconclusive", "insufficient_evidence", "RF04")
            retention = Retention(store, backups)
            now = day_start("20261112")
            result = retention.run(now, old.name)
            self.assertEqual(result["raw_deleted"], [])
            self.assertEqual(result["raw_held"][DAY], "review_backup_behind")
            updated = verified_backup(store, backups / "aggregate-with-review")
            self.assertEqual(retention.run(now, updated.name)["raw_deleted"], [DAY])
    def test_same_revision_and_count_different_evidence_holds_raw(self):
        # Each valid, hash-verified backup predates a synthetic evidence
        # discrepancy. Revision and row count alone must not authorize expiry.
        cases = (
            ("UPDATE rf_incidents SET ap_name='DEMO-AP-CHANGED'",
             "incident_backup_snapshot_mismatch"),
            ("UPDATE rf_incident_events SET at=at+1 WHERE revision=1",
             "incident_backup_event_mismatch"),
            ("UPDATE radio_quality_events SET score=COALESCE(score,0)+1 WHERE status='DETECTED'",
             "quality_event_backup_mismatch"),
        )
        for sql, expected in cases:
            with self.subTest(reason=expected), tempfile.TemporaryDirectory() as root:
                p = Path(root)
                store, ledger = prepare(p)
                backups = p / "backups"
                old = verified_backup(store, backups / "aggregate-before-discrepancy")
                verify(old)
                with store.lock, store._db(store.root / "core.db") as db:
                    self.assertGreater(db.execute(sql).rowcount, 0)
                raw = store.root / ("radio_" + DAY + ".db")
                retention = Retention(store, backups)
                now = day_start("20261112")
                result = retention.run(now, old.name)
                self.assertEqual(result["raw_deleted"], [])
                self.assertEqual(result["raw_held"][DAY], expected)
                self.assertTrue(raw.is_file())
                self.assertTrue(old.is_dir())
                # The hold is not permanent when the actual current evidence
                # is subsequently preserved by a new verified backup.
                current = verified_backup(store, backups / "aggregate-current-evidence")
                self.assertEqual(retention.run(now, current.name)["raw_deleted"], [DAY])

    def test_same_aggregate_count_different_content_holds_raw(self):
        # A valid backup of different aggregate content must not authorize
        # retirement, even if the manifest and observation totals agree.
        cases = (("5m", "utilization_sum=COALESCE(utilization_sum,0)+1"),
                 ("1h", "source=source||'-different'"))
        for resolution, change in cases:
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as root:
                p = Path(root)
                store, ledger = prepare(p)
                backups = p / "backups"
                path = store.root / f"radio_{resolution}_{DAY[:6]}.db"
                with store.lock, store._db(path) as db:
                    self.assertGreater(db.execute(f"UPDATE radio_aggregate SET {change}").rowcount, 0)
                old = verified_backup(store, backups / "aggregate-different-content")
                verify(old)
                # Rebuild from unchanged raw records, without changing revision.
                store.aggregate_day(DAY, day_start("20261102"))
                with store._db(path) as db, readonly(old / path.name) as archived:
                    query = "SELECT SUM(observed) FROM radio_aggregate"
                    self.assertEqual(db.execute(query).fetchone(), archived.execute(query).fetchone())
                retention = Retention(store, backups)
                now = day_start("20261112")
                result = retention.run(now, old.name)
                self.assertEqual(result["raw_deleted"], [])
                self.assertEqual(result["raw_held"][DAY], "aggregate_backup_behind")
                self.assertTrue((store.root / f"radio_{DAY}.db").is_file())
                current = verified_backup(store, backups / "aggregate-matching-content")
                self.assertEqual(retention.run(now, current.name)["raw_deleted"], [DAY])
                self.assertTrue(old.is_dir())

    def test_incident_reference_holds_expired_target_from_old_revision(self):
        # Use the actual ledger to create and close a repeated RF observation.
        # A later revision need not carry its creation-time relationship.
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);store,ledger=prepare(p)
            old=ledger.incidents.list(start=T-1,end=T+86400)["rows"][0]["incident_id"]
            for i,u in enumerate((90,90,90,20,20,20),6):
                t=T+i*180;r=sample(t,u);store.save(r)
                snap=ControllerSnapshot("w1",t,[],[],[],field_tag="fixture",field_until=T+86400,
                    field_connection="session",field_profile="safe")
                ledger.evaluate([r],[snap],t,{r.radio_id:t})
            rows=ledger.incidents.list(start=T-1,end=T+86400)["rows"]
            self.assertEqual(len(rows),2)
            new=rows[0]["incident_id"]
            self.assertNotEqual(new,old)
            self.assertEqual(ledger.incidents.detail(new)["repeat_observation"]["prior_incident_id"],old)
            store.aggregate_day(DAY,day_start("20261102"))
            backups=p/"backup"
            dst=verified_backup(store,backups/"aggregate-fixture")
            retention=Retention(store,backups)
            self.assertEqual(retention.run(day_start("20261112"),dst.name)["raw_deleted"],[DAY])
            # Only the older incident has crossed the 365-day threshold.
            result=retention.run(T+365*86400+1000,dst.name)
            self.assertEqual(result["incidents_deleted"],0)
            self.assertEqual(ledger.incidents.detail(new)["repeat_observation"]["prior_incident_id"],old)
            self.assertEqual(ledger.incidents.detail(old)["state"],"RESOLVED")
            # Both are now eligible; retire the referencing source first.
            self.assertEqual(retention.run(T+367*86400,dst.name)["incidents_deleted"],1)
            with store._db(store.root/"core.db") as db:
                self.assertEqual(db.execute("SELECT incident_id FROM rf_incidents").fetchall(),[(old,)])
            self.assertEqual(retention.run(T+367*86400,dst.name)["incidents_deleted"],1)

    def test_incident_reference_scan_peer_and_invalid_evidence(self):
        from awqm.retention import _incident_reference_holds
        # Isolate the scan against the actual schema. Peer links can live in
        # either the latest snapshot or an older revision, not necessarily both.
        with tempfile.TemporaryDirectory() as root:
            store,ledger=prepare(Path(root))
            target=ledger.incidents.list(start=T-1,end=T+86400)["rows"][0]["incident_id"]
            other="f"*32 if target!="f"*32 else "e"*32
            unrelated="a"*32 if target!="a"*32 else "b"*32
            peer=json.dumps({"neighbor_evidence":[{"peer_incident_id":target,"peer_incident_revision":1}]})
            with store.lock,store._db(store.root/"core.db") as db:
                db.execute("""INSERT INTO rf_incidents SELECT ?,radio_id,context,ap_name,controller,band,
                    first_at,last_at,persistent_at,ended_at,state,evidence_rev,?
                    FROM rf_incidents WHERE incident_id=?""",(other,peer,target))
                self.assertEqual(_incident_reference_holds(db,{target,unrelated}),{target})
                db.execute("UPDATE rf_incidents SET evidence='{}' WHERE incident_id=?",(other,))
                db.execute("INSERT INTO rf_incident_events VALUES(?,?,?,?,?)",(other,1,T,"RESOLVED",peer))
                self.assertEqual(_incident_reference_holds(db,{target,unrelated}),{target})
                db.execute("UPDATE rf_incident_events SET evidence='invalid' WHERE incident_id=?",(other,))
                self.assertEqual(_incident_reference_holds(db,{target,unrelated}),{target,unrelated})
                self.assertEqual(_incident_reference_holds(db,set()),set())

    def test_late_arrival_or_corruption_is_held(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s,l=prepare(p);backup=p/"backup";backup.mkdir()
            dst=verified_backup(s,backup/"aggregate-fixture")
            s.save(sample(T+1500,25))
            ret=Retention(s,backup)
            self.assertEqual(ret.run(day_start("20261112"),dst.name)["raw_held"][DAY],"aggregates_incomplete")
            s.aggregate_day(DAY,day_start("20261102"))
            self.assertEqual(ret.run(day_start("20261112"),dst.name)["raw_held"][DAY],"aggregate_backup_behind")
            with (dst/"core.db").open("ab") as f:f.write(b"corrupt")
            with self.assertRaises(ValueError):ret.run(day_start("20261112"),dst.name)
            self.assertTrue((s.root/"radio_20261101.db").exists())
    def test_audit_backup_and_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s,l=prepare(p);backup=p/"backup";backup.mkdir()
            audit_path=p/"audit.db";a=AuditLog(audit_path,clock=lambda:T)
            a.record("fixture-old","login")
            a.clock=lambda:T+366*86400;a.record("fixture-new","login")
            dst=verified_backup(s,backup/"aggregate-fixture",audit_path=audit_path)
            self.assertEqual(verify(dst)["scope"],"radio-core-audit")
            restored=restore_new(dst,p/"restored")
            self.assertTrue((restored/"web-audit.db").exists())
            r=Retention(s,backup,audit_path).run(T+366*86400,dst.name)
            self.assertEqual(r["audit_deleted"],1)
            self.assertEqual(a.summary()["events"],1)
            self.assertTrue(dst.exists())
