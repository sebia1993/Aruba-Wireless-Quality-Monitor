import json,unittest,sqlite3
from contextlib import closing
from awqm.data_rollback import rollback
from awqm.program_switch import apply
from awqm.package_stage import digest
from awqm.storage import Store
from awqm.model import Radio
import test_update_snapshot

class DataRollbackTests(unittest.TestCase):
    setUp=test_update_snapshot.SnapshotTests.setUp
    guard=test_update_snapshot.SnapshotTests.guard
    prepare=test_update_snapshot.SnapshotTests.prepare

    def switch_and_write(self):
        self.prepare()
        apply(self.installed,self.state,self.dest,"test",self.archive,digest(self.archive),self.guard)
        self.store.save(Radio("r:0","r","DEMO","w1","5GHz",44,2000,30,5,-94,source="aos8-cli-unvalidated"))
        (self.state/"logs/service-status.json").write_text('{"state":"new_version_stopped"}')

    def undo(self,**kw):
        return rollback(self.installed,self.state,self.dest,"test",self.guard,acknowledge=True,**kw)

    def test_restore_preserves_newer_data_and_logs(self):
        self.switch_and_write()
        result=self.undo()
        self.assertEqual(result["status"],"previous_program_and_data_restored_service_not_started")
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"old")
        displaced=self.dest.parent/(self.dest.name+".data-rollback")/"displaced"
        self.assertEqual(Store(displaced/"data",mode="diagnostic").snapshot()["radios"][0]["observed_at"],2000)
        self.assertEqual(json.loads((displaced/"logs/service-status.json").read_text())["state"],"new_version_stopped")
        self.assertEqual(Store(self.state/"data",mode="diagnostic").snapshot()["radios"][0]["observed_at"],1000)
        # Do not reopen the two DB trees before idempotent replay tests:
        # even a read with Store can change SQLite metadata.

    def test_resume_interrupted_directory_rename(self):
        self.switch_and_write()
        tx=self.dest.parent/(self.dest.name+".data-rollback")
        def fail(a,b):
            if a.name=="data" and a.parent.name=="staged" and b.name=="data":raise OSError("simulated rename failure")
            a.rename(b)
        with self.assertRaises(OSError):self.undo(rename=fail)
        self.assertFalse((self.state/"data").exists())
        self.assertTrue((tx/"displaced/data/core.db").exists())
        self.assertTrue(self.undo()["newer_data_preserved"])
        self.assertEqual(self.undo()["program_status"],"previous_program_already_installed")

    def test_resume_interrupted_program_reverse(self):
        self.switch_and_write()
        def fail(a,b):
            if a.name=="program.switch-test.old":raise OSError("simulated program rename failure")
            a.rename(b)
        with self.assertRaises(OSError):self.undo(rename=fail)
        self.assertFalse(self.installed.exists())
        self.assertEqual(self.undo()["status"],"previous_program_and_data_restored_service_not_started")

    def test_changed_configuration_or_secret_not_replaced(self):
        self.switch_and_write()
        for path in (self.state/"config/service.json",self.state/"secrets/fixture.dpapi"):
            old=path.read_bytes()
            path.write_bytes(b"changed")
            with self.assertRaises(RuntimeError):self.undo()
            self.assertEqual(path.read_bytes(),b"changed")
            path.write_bytes(old)
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"new")

    def test_explicit_acknowledgement_and_journal_integrity(self):
        self.switch_and_write()
        with self.assertRaises(ValueError):
            rollback(self.installed,self.state,self.dest,"test",self.guard)
        tx=self.dest.parent/(self.dest.name+".data-rollback")
        def fail(a,b):raise OSError("first rename interrupted")
        with self.assertRaises(OSError):self.undo(rename=fail)
        (tx/"journal.json").write_text("{}")
        with self.assertRaises(ValueError):self.undo()
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"new")

    def test_new_association_history_requires_separate_approval(self):
        self.switch_and_write()
        with closing(sqlite3.connect(self.state/"data/association.db")) as c,c:
            c.execute("CREATE TABLE fixture(id INTEGER)");c.execute("INSERT INTO fixture VALUES(1)")
        with self.assertRaises(ValueError):self.undo()
        self.assertTrue(self.undo(include_association=True)["newer_data_preserved"])
        self.assertFalse((self.state/"data/association.db").exists())
        tx=self.dest.parent/(self.dest.name+".data-rollback")
        self.assertTrue((tx/"displaced/data/association.db").exists())

    def test_modified_displaced_files_block_resume(self):
        self.switch_and_write()
        tx=self.dest.parent/(self.dest.name+".data-rollback")
        def fail(a,b):
            if a.name=="data" and a.parent.name=="staged" and b.name=="data":raise OSError("simulated interruption")
            a.rename(b)
        with self.assertRaises(OSError):self.undo(rename=fail)
        (tx/"displaced/data/extra").write_bytes(b"changed")
        with self.assertRaises(RuntimeError):self.undo()
        self.assertFalse((self.state/"data").exists())
        self.assertTrue((tx/"staged/data/core.db").exists())
