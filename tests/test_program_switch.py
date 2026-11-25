import tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
from awqm.program_switch import apply,inspect,reverse,locations
from awqm.package_stage import digest
import test_update_snapshot

# Reuse the offline fixture setup, without inheriting its test cases.
class ProgramSwitchTests(unittest.TestCase):
    setUp=test_update_snapshot.SnapshotTests.setUp
    guard=test_update_snapshot.SnapshotTests.guard
    prepare=test_update_snapshot.SnapshotTests.prepare

    def switch(self,**kw):
        return apply(self.installed,self.state,self.dest,"test",self.archive,digest(self.archive),self.guard,**kw)
    def state_view(self):
        return inspect(self.installed,self.state,self.dest,"test")
    def undo(self,**kw):
        return reverse(self.installed,self.state,self.dest,"test",self.guard,**kw)

    def test_switch_and_reverse_without_state_changes(self):
        self.prepare()
        self.assertEqual(self.switch()["status"],"candidate_installed_service_not_started")
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"new")
        self.assertTrue(self.state_view()["pre_start_reverse_allowed"])
        self.assertEqual(self.undo()["status"],"previous_program_restored_service_not_started")
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"old")
        self.assertEqual(self.undo()["status"],"previous_program_already_installed")
        self.assertEqual((self.state/"config/service.json").read_text(),'{"fixture":true}')
        with self.assertRaises(ValueError):self.switch()
    def test_changed_state_blocks_switch_and_reverse(self):
        self.prepare()
        config=self.state/"config/service.json"
        old=config.read_bytes();config.write_bytes(b"changed")
        with self.assertRaises(RuntimeError):self.switch()
        config.write_bytes(old)
        self.switch()
        config.write_bytes(b"new state")
        self.assertFalse(self.state_view()["pre_start_reverse_allowed"])
        with self.assertRaises(RuntimeError):self.undo()
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"new")
    def test_second_rename_error_recovers_previous_program(self):
        self.prepare();_,new,old,j=locations(self.installed,"test")
        def rename(a,b):
            if a==new:raise OSError("simulated rename failure")
            a.rename(b)
        with self.assertRaises(OSError):self.switch(rename=rename)
        self.assertEqual(self.state_view()["layout"],"previous_installed_candidate_staged")
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"old")
    def test_interrupted_reversal_can_resume(self):
        self.prepare();self.switch()
        _,new,old,j=locations(self.installed,"test")
        def rename(a,b):
            if a==old:raise OSError("simulated interruption")
            a.rename(b)
        with self.assertRaises(OSError):self.undo(rename=rename)
        self.assertEqual(self.state_view()["layout"],"switch_interrupted")
        self.assertEqual(self.undo()["status"],"previous_program_restored_service_not_started")
    def test_tampered_candidate_or_guard_fails_closed(self):
        self.prepare()
        with self.assertRaises(ValueError):
            apply(self.installed,self.state,self.dest,"test",self.archive,"0"*64,self.guard)
        self.assertEqual((self.installed/"AWQM.exe").read_bytes(),b"old")
        self.switch()
        (self.installed/"AWQM.exe").write_bytes(b"tampered")
        self.assertEqual(self.state_view()["layout"],"ambiguous")
        with self.assertRaises(RuntimeError):self.undo()
