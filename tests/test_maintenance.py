import json,tempfile,unittest,sqlite3
from collections import namedtuple
from pathlib import Path
from awqm.maintenance import Maintenance,verified_backup,verify,restore_new
from awqm.storage import Store,day_start
from awqm.model import Radio

def sample(t):
    return Radio("r","a","DEMO","w","5GHz",44,t,85,5,-94)

class MaintenanceTests(unittest.TestCase):
    def test_schedule_and_restore(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s=Store(p/"data")
            t=day_start("20261101")+3600;s.save(sample(t))
            now=[day_start("20261102")]
            Usage=namedtuple("Usage","total used free")
            m=Maintenance(s,p/"backups",lambda:now[0],lambda _:Usage(100*10**9,40*10**9,60*10**9))
            a=m.tick()
            self.assertEqual(a["status"],"ok")
            self.assertEqual(a["last_aggregated_day"],"20261101")
            b=p/"backups"/a["last_backup"]
            self.assertTrue(verify(b)["include_aggregates"])
            restored=Store(restore_new(b,p/"restored"))
            self.assertEqual(restored.snapshot()["radios"][0]["radio_id"],"r")
            self.assertEqual(len(restored.history("r",t,t+300,"5m")),1)
            self.assertEqual(restored.history("r",t,t+1),[])
            with self.assertRaises(ValueError):restored.save(sample(t+1))
            self.assertEqual(m.tick()["last_backup"],a["last_backup"])
            now[0]+=86400
            daily=m.tick()
            self.assertFalse(verify(p/"backups"/daily["last_backup"])["include_aggregates"])
            self.assertTrue((s.root/"radio_20261101.db").exists())
    def test_corrupt_backup_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s=Store(p/"data");s.save(sample(1000))
            b=verified_backup(s,p/"backup")
            with (b/"core.db").open("ab") as f:f.write(b"damage")
            with self.assertRaises(ValueError):restore_new(b,p/"restored")
            self.assertFalse((p/"restored").exists())
    def test_low_disk_defers(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);s=Store(p/"data")
            Usage=namedtuple("Usage","total used free")
            m=Maintenance(s,p/"backups",lambda:1000,lambda _:Usage(100*10**9,96*10**9,4*10**9))
            self.assertEqual(m.tick()["status"],"disk_protection")
            self.assertEqual(list((p/"backups").iterdir()),[])
