import unittest
from awqm.ssh_validation import validate

class SSHValidationTests(unittest.TestCase):
    def test_actual_loopback_ssh_and_host_pinning(self):
        r=validate()
        self.assertEqual(r["result"],"pass")
        self.assertEqual(len(r["checks"]),5)
        self.assertTrue(all(x=="pass" for x in r["checks"].values()))
        self.assertFalse(r["real_wlc_validated"])
        self.assertFalse(r["enterprise_aaa_validated"])
