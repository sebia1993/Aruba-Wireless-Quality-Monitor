import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import launcher

class PortableLauncherTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        self.exe=root/"dist"/"AWQM.exe"
        self.unpack=root/"temp"/"_MEI123"
        self.exe.parent.mkdir()
        self.unpack.mkdir(parents=True)
        for key,value in (("frozen",True),("executable",str(self.exe)),("_MEIPASS",str(self.unpack))):
            p=patch.object(sys,key,value,create=True)
            p.start()
            self.addCleanup(p.stop)

    def run_args(self,*args):
        with patch.object(sys,"argv",[str(self.exe),*args]):
            launcher.main()

    def test_single_file_routes_smoke_and_rejects_privileged_actions(self):
        self.assertTrue(launcher.portable_runtime())
        with patch("awqm.winservice.main") as service, patch("awqm.manager.main") as manager:
            self.run_args("--service","--smoke-test")
            self.run_args("--manager","smoke-test")
            service.assert_called_once()
            manager.assert_called_once()
            for args in (("--service",),("--manager","install"),("--manager","start"),("--manager","prepare-update")):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    self.run_args(*args)
            self.assertEqual(service.call_count,1)
            self.assertEqual(manager.call_count,1)

    def test_legacy_onedir_is_not_portable(self):
        with patch.object(sys,"_MEIPASS",str(self.exe.parent/"_internal")):
            self.assertFalse(launcher.portable_runtime())
            with patch("awqm.manager.main") as manager:
                self.run_args("--manager","status")
                manager.assert_called_once()

    def test_no_arguments_open_desktop_without_console_input(self):
        with patch.object(sys, "platform", "win32"), patch.object(sys, "stdin", None), patch("awqm.desktop_app.run_desktop") as desktop:
            self.run_args()
            desktop.assert_called_once_with(portable=True)
        with patch.object(sys, "platform", "win32"), patch.object(sys, "_MEIPASS", str(self.exe.parent / "_internal")), patch("awqm.desktop_app.run_desktop") as desktop:
            self.run_args()
            desktop.assert_called_once_with(portable=False)

    def test_cli_demo_interrupt_exits_normally(self):
        with patch("awqm.web.serve", side_effect=KeyboardInterrupt):
            self.run_args("--demo")

    def test_explicit_demo_keeps_browser_opt_in(self):
        with patch("awqm.web.serve") as demo:
            self.run_args("--demo","--smoke-test")
            demo.assert_called_once_with(8765,True,open_browser=False)

    def test_new_offline_validation_routes_preserved(self):
        with patch("awqm.ssh_validation.main") as ssh:
            self.run_args("--validate-ssh")
            ssh.assert_called_once()
        with patch("awqm.scale_validation.main") as scale:
            self.run_args("--validate-scale","retained","--output","fixture")
            scale.assert_called_once()
        with patch("awqm.probe.main") as probe:
            self.run_args("--validate-cli","client-info-airtime","fixture.txt")
            probe.assert_called_once()

    def test_renamed_onefile_service_and_manager_are_still_blocked(self):
        for name in ("AWQM_Service.exe","AWQM_Manager.exe"):
            with self.subTest(name=name), patch.object(sys,"executable",str(self.exe.with_name(name))):
                with self.assertRaises(SystemExit):
                    self.run_args()
        with patch("awqm.manager.main") as manager:
            for args in (("rollback-data",),("reverse-program",),("switch-program",)):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    self.run_args("--manager",*args)
            manager.assert_not_called()

    def test_version(self):
        with patch("sys.stdout",new_callable=io.StringIO) as out:
            self.run_args("--version")
            self.assertEqual(out.getvalue().strip(),launcher.__version__)

if __name__=="__main__":unittest.main()
