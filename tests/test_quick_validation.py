import unittest
from unittest.mock import patch
from tools.quick_validation import select,changed_paths

AVAILABLE={"test_foundation","test_client_info_airtime","test_client","test_station_validation",
           "test_quick_validation","test_storage_lifecycle","test_engine_demo","test_diagnostic_web",
           "test_secure_preview","test_portable_launcher"}

class QuickValidationTests(unittest.TestCase):
    def test_leaf_and_baseline(self):
        names,reason=select(["awqm/client_info_airtime.py"],AVAILABLE)
        self.assertEqual(set(names),{"test_foundation","test_client_info_airtime","test_client",
                                    "test_station_validation","test_portable_launcher"})
        self.assertIn("targeted",reason)

    def test_broad_unknown_and_deleted_test_fallback(self):
        for paths in (None,["awqm/storage.py"],["requirements.txt"],["tests/test_removed.py"]):
            with self.subTest(paths=paths):
                self.assertEqual(set(select(paths,AVAILABLE)[0]),AVAILABLE)

    def test_workflow_and_docs(self):
        names,_=select([".github/workflows/development-check.yml","docs/STATUS.md"],AVAILABLE)
        self.assertEqual(set(names),{"test_foundation","test_quick_validation","test_portable_launcher"})

    def test_invalid_range_falls_back(self):
        self.assertIsNone(changed_paths("0"*40,"not-a-sha","refs/heads/develop/awqm-foundation"))
        with patch("tools.quick_validation.git",side_effect=OSError()):
            self.assertIsNone(changed_paths("a"*40,"b"*40,"refs/heads/develop/awqm-foundation"))
