import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm import manager, windows_security


@unittest.skipUnless(sys.platform == "win32", "Windows manager")
class InstallLayoutTests(unittest.TestCase):
    def test_program_tree_checked_before_password_prompt_or_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "AWQM"
            runtime = program / "_internal"
            runtime.mkdir(parents=True)
            library = runtime / "example.dll"
            library.touch()
            (program / "AWQM_Service.exe").touch()

            def inspect(path):
                if path == library:
                    raise ValueError("unprotected fixture library")

            with (patch.dict(os.environ, {"ProgramFiles": directory}),
                  patch.object(sys, "frozen", True, create=True),
                  patch.object(sys, "executable", str(program / "AWQM_Manager.exe")),
                  patch("awqm.windows_update.require_admin"),
                  patch.object(manager, "account_sid"), patch.object(manager, "check_layout"),
                  patch.object(windows_security, "check_program_acl", side_effect=inspect),
                  patch.object(manager, "prompt", side_effect=AssertionError("unexpected password prompt"))):
                with self.assertRaisesRegex(ValueError, "unprotected fixture library"):
                    manager.install(SimpleNamespace(account="fixture"))
