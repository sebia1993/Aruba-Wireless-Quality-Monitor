import tempfile,unittest,json
from pathlib import Path
from awqm.retained_validation import retained_test,expected,FULL

class RetainedTests(unittest.TestCase):
    def test_cardinality_and_full_year_query(self):
        self.assertEqual(sum(expected(FULL).values()),109920000)
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"retained"
            r=retained_test(p,radios=2)
            self.assertEqual(r["fixture_rows"],expected(2))
            self.assertEqual(r["queries"]["raw"]["rows_per_request"],1440)
            self.assertEqual(r["queries"]["5m"]["rows_per_request"],8640)
            self.assertEqual(r["queries"]["1h"]["rows_per_request"],8760)
            self.assertEqual(r["result"],"pass")
            self.assertFalse(r["full_scale_read_benchmark_passed"])
            self.assertFalse(r["ingest_aggregation_validated"])
            self.assertEqual(json.loads((p/"retained-result.json").read_text(encoding="utf-8"))["result"],"pass")
            with self.assertRaises(FileExistsError):retained_test(p,radios=2)
    def test_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"retained"
            for n in (0,4001,True):
                with self.assertRaises(ValueError):retained_test(p,n)
            self.assertFalse(p.exists())
