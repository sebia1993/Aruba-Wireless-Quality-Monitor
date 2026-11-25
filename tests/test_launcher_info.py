import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest

from cryptography.hazmat.backends.openssl.backend import backend

from awqm import __version__


LAUNCHER = Path(__file__).resolve().parents[1] / "launcher.py"


class LauncherInfoTests(unittest.TestCase):
    def launch(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *args], cwd=cwd,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )

    def test_help_lists_routed_commands(self):
        result = self.launch("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--demo", "--engine-demo", "--manager", "--stage-package",
                     "--validate-scale", "--validate-cli", "--validate-ssh",
                     "--dpapi-smoke-test", "--collect-once", "--auth-preview",
                     "--version", "--runtime-info"):
            with self.subTest(flag=flag):
                self.assertIn(flag, result.stdout)

    def test_version_reports_application_version(self):
        result = self.launch("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_runtime_info_reports_loaded_libraries_in_an_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.launch("--runtime-info", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(json.loads(result.stdout), {
            "python": ".".join(map(str, sys.version_info[:3])),
            "sqlite": sqlite3.sqlite_version,
            "openssl": ssl.OPENSSL_VERSION,
            "cryptography_openssl": backend.openssl_version_text(),
        })

    def test_command_help_still_reaches_scale_parser(self):
        result = self.launch("--validate-scale", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained-full", result.stdout)
        self.assertIn("--output", result.stdout)
