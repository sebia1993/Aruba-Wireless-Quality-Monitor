import tempfile
import unittest
from pathlib import Path

from tools.installed_app_validation import verified_uninstaller


class RegisteredUninstallerTests(unittest.TestCase):
    def test_numbered_uninstaller_requires_existing_file_in_expected_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            app = local / "Programs" / "AWQM" / "AWQM.exe"
            app.parent.mkdir(parents=True)
            app.touch()
            target = local / "Programs" / "AWQM Setup" / "Uninstall"
            target.mkdir(parents=True)
            for name in ("unins000.exe", "unins001.exe", "unins999.exe"):
                entry = target / name
                entry.touch()
                self.assertEqual(verified_uninstaller(app.parent, entry, app, local), entry)
            bad_name = target / "other.exe"
            bad_name.touch()
            wrong_directory = local / "unins001.exe"
            wrong_directory.touch()
            for entry in (target / "unins002.exe", bad_name, wrong_directory, Path("unins001.exe")):
                with self.subTest(entry=entry), self.assertRaises(RuntimeError):
                    verified_uninstaller(app.parent, entry, app, local)
            with self.assertRaises(RuntimeError):
                verified_uninstaller(target, target / "unins001.exe", app, local)
