import sys,unittest
from awqm.dpapi import protect,unprotect,DPAPIError

class DPAPITests(unittest.TestCase):
    def test_invalid_input(self):
        with self.assertRaises(ValueError):protect(b"")
        with self.assertRaises(ValueError):protect("plaintext")
    @unittest.skipUnless(sys.platform=="win32","Windows DPAPI required")
    def test_current_user_roundtrip(self):
        plain=b"fixture-only-not-an-account"
        encrypted=protect(plain)
        self.assertNotEqual(plain,encrypted)
        self.assertNotIn(plain,encrypted)
        self.assertEqual(unprotect(encrypted),plain)
        broken=bytearray(encrypted);broken[len(broken)//2]^=255
        with self.assertRaises(DPAPIError):unprotect(bytes(broken))
    @unittest.skipIf(sys.platform=="win32","non-Windows only")
    def test_non_windows(self):
        with self.assertRaises(DPAPIError):protect(b"fixture")
