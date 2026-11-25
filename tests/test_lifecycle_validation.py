import json,tempfile,unittest
from pathlib import Path
from awqm.lifecycle_validation import lifecycle_test


class LifecycleValidationTests(unittest.TestCase):
    def test_actual_lifecycle_representative(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"lifecycle"
            r=lifecycle_test(p,radios=2,minutes=10)
            self.assertEqual(r["result"],"pass")
            self.assertEqual(r["expected_samples"],20)
            self.assertEqual(r["aggregate_sample_counts"],{"5m":20,"1h":20})
            for k in ("interrupted_stage_recovered","duplicate_preserved_aggregate",
                      "backup_restore_verified","raw_retirement_guard_verified"):
                self.assertTrue(r[k],k)
            for k in ("full_cardinality_validated","real_device_validated","scm_validated",
                      "retained_full_validated","soak_validated","concurrent_writer_validated"):
                self.assertFalse(r[k],k)
            self.assertEqual(json.loads((p/"lifecycle-result.json").read_text(encoding="utf-8")),r)
            with self.assertRaises(FileExistsError):
                lifecycle_test(p,radios=2,minutes=10)

    def test_input_bounds_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"lifecycle"
            for radios,minutes in ((0,10),(True,10),(129,10),(2,0),(2,6),(2,1445),(2,False)):
                with self.subTest(radios=radios,minutes=minutes),self.assertRaises(ValueError):
                    lifecycle_test(p,radios,minutes)
            self.assertFalse(p.exists())
