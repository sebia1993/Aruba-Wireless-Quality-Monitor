import tempfile,unittest
from pathlib import Path
from awqm.https_validation import https_test

class HTTPSValidationTests(unittest.TestCase):
    def test_real_loopback_transport_small_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"check"
            result=https_test(p,aps=3,clients=4)
            self.assertEqual(result["radio_count"],24)
            self.assertEqual(result["https_requests"],50)
            self.assertEqual(result["wss_notifications"],10)
            self.assertEqual(result["admission_and_revocation"],"pass")
            self.assertTrue(result["actual_tls_transport"])
            self.assertFalse(result["browser_rendering_validated"])
            self.assertFalse(result["real_aaa_validated"])
            self.assertTrue((p/"https-result.json").is_file())
            # Latency gates are assessed by the full-size CI step rather
            # than hard-coding a timing assertion in the small unit test.
