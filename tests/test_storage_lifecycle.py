import json,sqlite3,tempfile,unittest
from pathlib import Path
from dataclasses import replace
from awqm.storage import Store,day_for,day_start,disk_level
from awqm.model import Radio

T=day_start("20261101")+3600
def row(t=T,u=90,epoch="1",**kw):
    return replace(Radio("r:0","r","DEMO","w1","5GHz",44,t,u,5,-94,2,epoch=epoch),**kw)

class StorageLifecycle(unittest.TestCase):
    def test_batch_and_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            self.assertEqual(s.save_batch([row(T),row(T+60),row(T+120)]),3)
            self.assertTrue(s.snapshot()["radios"][0]["open_incident"])
            self.assertEqual(s.save_batch([row(T),row(T+120)]),0)
            # Simulate a stop after core staging, before any raw commit.
            payload=s._encode(row(T+180,u=25))
            with s._db(s.root/"core.db") as c:
                c.execute("INSERT INTO pending VALUES(?,?,?,?)",payload)
            s2=Store(root)
            self.assertEqual(s2.pending_count(),0)
            self.assertEqual(s2.snapshot()["radios"][0]["observed_at"],T+180)
            self.assertEqual(s2.snapshot()["radios"][0]["status"],"RECOVERING")
            self.assertFalse(s2.save(row(T+180,u=25)))
    def test_replay_after_raw_commit(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            payload=s._encode(row())
            with s._db(s.root/"core.db") as c:
                c.execute("INSERT INTO pending VALUES(?,?,?,?)",payload)
            from awqm.storage import RAW_SCHEMA
            with s._db(s._raw_path(payload[3])) as c:
                c.execute(RAW_SCHEMA)
                c.execute("INSERT INTO radio_raw VALUES(?,?,?)",payload[:3])
            recovered=Store(root)
            self.assertEqual(recovered.pending_count(),0)
            self.assertEqual(len(recovered.history("r:0",T,T+1)),1)
            self.assertEqual(recovered.snapshot()["radios"][0]["observed_at"],T)
    def test_conflict_and_modes(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            s.save(row())
            with self.assertRaises(ValueError):s.save(row(u=22))
            with self.assertRaises(ValueError):s.save(row(source="aos8-cli-unvalidated"))
            with self.assertRaises(ValueError):Store(root,mode="diagnostic")
            self.assertEqual(s.pending_count(),0)
    def test_aggregate_and_late_arrival(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            s.save_batch([row(T,u=20),row(T+60,u=30),row(T+120,u=95),row(T+180,u=None)])
            end=day_start("20261102")
            self.assertEqual(s.aggregate_day("20261101",end),{"5m":4,"1h":4})
            data=s.history("r:0",T,T+300,"5m")
            self.assertEqual(len(data),1)
            a=data[0]
            self.assertEqual((a["observed"],a["valid"],a["risky"]),(4,3,1))
            self.assertEqual(a["utilization_max"],95)
            self.assertAlmostEqual(a["utilization_avg"],145/3)
            s.save(row(T+240,u=40,epoch="2"))
            self.assertEqual(s.history("r:0",T,T+300,"5m"),[])
            now=day_start("20261112")
            self.assertEqual(s.prune_raw(now,["20261101"]),[])
            s.aggregate_day("20261101",end)
            self.assertEqual(len(s.history("r:0",T,T+300,"1h")),2)
            self.assertEqual(s.prune_raw(now),[]) # no evidence approval
            self.assertEqual(s.prune_raw(now,["20261101"]),["20261101"])
            self.assertFalse((s.root/"radio_20261101.db").exists())
            with self.assertRaises(ValueError):s.save(row(T+250))
    def test_backup(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s=Store(p/"data");s.save(row())
            s.aggregate_day("20261101",day_start("20261102"))
            dest=s.backup(p/"backup")
            self.assertTrue((dest/"core.db").exists())
            self.assertTrue((dest/"radio_5m_202611.db").exists())
            self.assertFalse((dest/"radio_20261101.db").exists())
            self.assertFalse(json.loads((dest/"manifest.json").read_text())["include_raw"])
            c=sqlite3.connect(dest/"core.db")
            try:
                self.assertEqual(c.execute("PRAGMA quick_check").fetchone()[0],"ok")
            finally:c.close()
    def test_disk_levels(self):
        self.assertEqual(disk_level(4*10**9,100*10**9),"critical")
        self.assertEqual(disk_level(9*10**9,100*10**9),"limited")
        self.assertEqual(disk_level(15*10**9,100*10**9),"warning")
        self.assertEqual(disk_level(30*10**9,100*10**9),"normal")
