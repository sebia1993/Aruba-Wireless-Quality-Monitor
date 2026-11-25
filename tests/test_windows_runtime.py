import hashlib
import io
import unittest
import zipfile
from unittest.mock import patch

from tools import windows_runtime as runtime


class WindowsRuntimeTests(unittest.TestCase):
    def test_sqlite_vendor_digest_checked_before_reading_archive(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            runtime.sqlite_dll(b"corrupt archive")

    def test_reads_exact_dll_from_verified_vendor_archive(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("sqlite3.dll", b"synthetic dll")
        data = stream.getvalue()
        with patch.object(runtime, "SQLITE_ARCHIVE_DIGEST", hashlib.sha3_256(data).hexdigest()):
            self.assertEqual(runtime.sqlite_dll(data), b"synthetic dll")

    def test_packaged_runtime_must_match_all_reviewed_versions(self):
        for key in runtime.EXPECTED:
            with self.subTest(component=key):
                report = dict(runtime.EXPECTED, **{key: "unexpected"})
                with self.assertRaisesRegex(ValueError, key):
                    runtime.verify_report(report)
        runtime.verify_report(dict(runtime.EXPECTED))
