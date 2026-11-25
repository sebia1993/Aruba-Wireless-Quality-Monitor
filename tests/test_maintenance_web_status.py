import unittest
from types import SimpleNamespace

from awqm.maintenance import BackgroundMaintenance


class MaintenanceStatusTests(unittest.TestCase):
    def test_summary_excludes_protected_information(self):
        runner = BackgroundMaintenance(SimpleNamespace())
        self.addCleanup(runner.pool.shutdown)
        self.assertEqual(runner.web_status(False,None)["status"],"unknown")
        runner.last = {
            "status":"ok", "last_attempt":1700000000, "last_core_backup":1699999999,
            "last_aggregate_backup":1699999998, "last_backup":r"C:\\private\\archive",
            "disk_level":"normal", "backup_disk_level":"limited", "error":"secret",
            "retention":{"raw_held":{"20260101":"private"}},
            "last_retention_attempt":1699999997,
        }
        result = runner.web_status(True,90)
        self.assertEqual(result["status"],"ok")
        self.assertEqual(result["last_core_backup"],1699999999)
        self.assertEqual(result["archive_expiration_days"],90)
        self.assertEqual(result["retention_days"]["hourly"],365)
        self.assertFalse(result["policy_editable"])
        self.assertNotIn("secret",str(result))
        self.assertNotIn("private",str(result))
        runner.pending = object()
        self.assertEqual(runner.web_status(True,90)["status"],"running")
