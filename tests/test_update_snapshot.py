import json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace
from awqm.storage import Store
from awqm.model import Radio
from awqm.update_snapshot import _validate_program,prepare,verify,restore_copy
from awqm.package_stage import digest

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.installed=self.root/"program";self.installed.mkdir()
        for name in ("AWQM.exe","AWQM_Service.exe","AWQM_Manager.exe"):(self.installed/name).write_bytes(b"old")
        (self.installed/"VERSION.txt").write_text("0.23.0-dev\n")
        (self.installed/"_internal").mkdir();(self.installed/"_internal/python.dll").write_bytes(b"old runtime")
        self.state=self.root/"state";self.state.mkdir()
        for name in ("config","secrets","data","logs","backup"):(self.state/name).mkdir()
        (self.state/"config/service.json").write_text('{"fixture":true}')
        (self.state/"config/operations.initialized").write_text("AWQM-operations-v1\n")
        with closing(sqlite3.connect(self.state/"config/operations.db")) as c, c:
            c.execute("CREATE TABLE state(paused INTEGER)");c.execute("INSERT INTO state VALUES(1)")
        (self.state/"secrets/fixture.dpapi").write_bytes(b"synthetic encrypted placeholder")
        (self.state/"logs/service-status.json").write_text('{"state":"stopped"}')
        (self.state/"backup/excluded").write_text("not copied recursively")
        self.store=Store(self.state/"data",mode="diagnostic")
        self.store.save(Radio("r:0","r","DEMO","w1","5GHz",44,1000,80,5,-94,source="aos8-cli-unvalidated"))
        self.archive=self.root/"candidate.zip"
        with zipfile.ZipFile(self.archive,"w") as z:
            for name in ("AWQM.exe","AWQM_Service.exe","AWQM_Manager.exe","README.md","BUILD-DEPENDENCIES.txt","docs/guide.md","_internal/python.dll"):
                z.writestr("AWQM/"+name,b"new")
            z.writestr("AWQM/VERSION.txt","0.24.0-dev\n")
        self.dest=self.state/"backup/transaction1";self.guard_calls=[]
        self.addCleanup(patch.stopall)
        patch("awqm.update_snapshot.shutil.disk_usage",return_value=SimpleNamespace(free=100*1024**3)).start()

    def guard(self):self.guard_calls.append(True)
    def prepare(self,**kw):
        return prepare(self.installed,self.state,self.archive,digest(self.archive),self.dest,self.guard,**kw)
    def test_installed_layout_accepts_unified_and_rejects_missing_legacy_roles(self):
        aliases = (self.installed / "AWQM_Service.exe", self.installed / "AWQM_Manager.exe")
        for alias in aliases:
            alias.unlink()
        with self.assertRaises(ValueError):
            _validate_program(self.installed)
        (self.installed / "VERSION.txt").write_text("0.45.0\n")
        self.assertEqual(_validate_program(self.installed), "0.45.0")
        aliases[0].touch()
        with self.assertRaises(ValueError):
            _validate_program(self.installed)
    def test_prepare_and_non_overwriting_recovery(self):
        result=self.prepare()
        self.assertEqual(result["status"],"prepared_not_installed")
        self.assertEqual(result["old_version"],"0.23.0-dev")
        self.assertEqual(result["new_version"],"0.24.0-dev")
        self.assertGreaterEqual(len(self.guard_calls),3)
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"old")
        m=verify(self.dest)
        self.assertIn("config/operations.db",m["old_state"]["files"])
        self.assertIn("secrets/fixture.dpapi",m["old_state"]["files"])
        self.assertIn("data/core.db",m["old_state"]["files"])
        self.assertFalse((self.dest/"old-state/backup").exists())
        out=self.state/"backup/recovery1"
        self.assertEqual(restore_copy(self.dest,out)["status"],"verified_recovery_copy_not_applied")
        self.assertEqual((out/"old-program/AWQM.exe").read_bytes(),b"old")
        self.assertEqual(Store(out/"old-state/data",mode="diagnostic").snapshot()["radios"][0]["radio_id"],"r:0")
        self.assertEqual((self.state/"config/service.json").read_text(),'{"fixture":true}')
        with self.assertRaises(ValueError):restore_copy(self.dest,out)
    def test_corruption_and_extra_file(self):
        self.prepare()
        p=self.dest/"old-state/secrets/fixture.dpapi"
        content=p.read_bytes();p.write_bytes(b"tampered")
        with self.assertRaises(ValueError):verify(self.dest)
        with self.assertRaises(ValueError):restore_copy(self.dest,self.state/"backup/recovery")
        p.write_bytes(content)
        (self.dest/"candidate/extra.txt").write_text("extra")
        with self.assertRaises(ValueError):verify(self.dest)
    def test_wal_and_association_guard(self):
        wal=self.state/"data/core.db-wal";wal.write_bytes(b"unclean")
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertFalse(self.dest.exists());wal.unlink()
        with closing(sqlite3.connect(self.state/"data/association.db")) as c, c:c.execute("CREATE TABLE fixture(id INTEGER)")
        with self.assertRaises(ValueError):self.prepare()
        self.assertFalse(self.dest.exists())
        self.prepare(include_association=True)
        self.assertTrue(verify(self.dest)["include_association_history"])
    def test_source_changes_and_pending_spool_reject(self):
        original=self.guard
        def changed():
            original()
            if len(self.guard_calls)==3:(self.state/"config/service.json").write_text('{"changed":true}')
        self.guard=changed
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertFalse(self.dest.exists())
    def test_guard_failure_and_wrong_checksum(self):
        def stopped():raise RuntimeError("not stopped")
        with self.assertRaises(RuntimeError):
            prepare(self.installed,self.state,self.archive,digest(self.archive),self.dest,stopped)
        with self.assertRaises(ValueError):
            prepare(self.installed,self.state,self.archive,"0"*64,self.dest,self.guard)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list((self.state/"backup").iterdir()),[self.state/"backup/excluded"])
