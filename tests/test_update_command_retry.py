import unittest
from types import SimpleNamespace

from awqm import windows_update
from awqm.package_stage import digest
import test_update_snapshot


class ServiceCommandRetryTests(unittest.TestCase):
    setUp = test_update_snapshot.SnapshotTests.setUp
    prepare = test_update_snapshot.SnapshotTests.prepare
    guard = test_update_snapshot.SnapshotTests.guard

    def test_repeat_after_completed_directory_move_validates_the_same_candidate(self):
        self.prepare()
        args = SimpleNamespace(id="binding", archive=self.archive, sha256=digest(self.archive))

        def switch():
            return windows_update.switch_candidate(self.installed, self.state, self.dest,
                                                   args, self.guard, None, None)

        self.assertEqual(switch()["status"], "candidate_installed_service_not_started")
        self.assertEqual(switch()["status"], "candidate_already_installed_service_not_started")
        args.sha256 = "a" * 64
        with self.assertRaises(ValueError):
            switch()
        args.sha256 = digest(self.archive)
        (self.installed / "AWQM.exe").write_bytes(b"changed")
        with self.assertRaises(RuntimeError):
            switch()
