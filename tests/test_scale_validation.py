import tempfile,unittest
from pathlib import Path
from awqm.scale_validation import scale_fixtures,scale_test,soak
from awqm.aos8 import radio_summary,associations,inventory,active_details
from awqm.client import bss_table

class ScaleValidationTests(unittest.TestCase):
    def test_full_fixture_shape(self):
        f=scale_fixtures(4)
        self.assertEqual(len(inventory(f["show ap database long"])),500)
        self.assertEqual(len(active_details(f["show ap active details"])),500)
        self.assertEqual(len(radio_summary(f["show ap radio-summary"])),1000)
        self.assertEqual(len(associations(f["show ap association"])),5000)
        self.assertEqual(len(bss_table(f["show ap bss-table"])),1000)
    def test_small_actual_engine_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"validation"
            result=scale_test(p,aps=3,clients=4)
            self.assertEqual((result["radio_count"],result["client_count"]),(24,48))
            self.assertEqual(result["result"],"pass")
            self.assertTrue((p/"capacity-result.json").is_file())
            self.assertFalse(result["soak_validated"])
            with self.assertRaises(FileExistsError):scale_test(p,aps=3,clients=4)
    def test_shortcut_not_qualifying(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):soak(Path(root)/"soak",hours=1)
