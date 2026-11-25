import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm import manager


@unittest.skipUnless(sys.platform == "win32", "Windows SCM registration adapter")
class ServiceRegistrationTests(unittest.TestCase):
    def setUp(self):
        import win32service
        self.w = win32service
        directory = self.enterContext(tempfile.TemporaryDirectory())
        program = Path(directory) / "AWQM"
        program.mkdir()
        (program / "AWQM.exe").touch()
        (program / "_internal").mkdir()
        (program / "VERSION.txt").write_text("0.45.0", encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {"ProgramFiles": directory}))
        self.enterContext(patch.object(sys, "frozen", True, create=True))
        self.enterContext(patch.object(sys, "executable", str(program / "AWQM.exe")))
        for target in ("awqm.windows_update.require_admin", "awqm.windows_security.check_program_acl",
                       "awqm.manager.account_sid", "awqm.manager.check_layout"):
            self.enterContext(patch(target))
        self.publish_endpoint = self.enterContext(patch("awqm.desktop_endpoint.publish_service_endpoint"))
        self.enterContext(patch.object(manager, "prompt", return_value="fixture"))
        self.cfg = SimpleNamespace(account="fixture")
        self.scm, self.service = object(), object()
        self.settings = {}
        self.transitions = []
        self.api = {}
        for name in ("OpenSCManager", "OpenService", "CreateService", "ChangeServiceConfig",
                     "ChangeServiceConfig2", "QueryServiceConfig", "QueryServiceConfig2", "QueryServiceStatus",
                     "CloseServiceHandle", "DeleteService", "StartService"):
            self.api[name] = self.enterContext(patch.object(self.w, name))
        self.api["OpenSCManager"].return_value = self.scm
        self.api["OpenService"].return_value = self.service
        self.api["CreateService"].side_effect = self.create
        self.api["ChangeServiceConfig2"].side_effect = lambda _, key, value: self.settings.update({key: value})
        self.api["QueryServiceConfig2"].side_effect = lambda _, key: self.settings[key]
        self.api["ChangeServiceConfig"].side_effect = lambda _, kind, start, *args: self.transitions.append(start)
        self.api["QueryServiceStatus"].return_value = (0, self.w.SERVICE_STOPPED)

    def create(self, *args):
        self.transitions.append(args[5])
        self.api["QueryServiceConfig"].return_value = (args[4], args[5], args[6], args[7], None, 0, [], self.cfg.account, "AWQM")
        return self.service

    def test_install_registers_only_quoted_unified_binary_with_service_argument(self):
        manager.install(self.cfg)
        self.assertEqual(self.api["CreateService"].call_args.args[7], f'"{Path(sys.executable).resolve(strict=True)}" --service')
        self.publish_endpoint.assert_called_once_with(self.cfg)

    def test_endpoint_configuration_failure_removes_disabled_registration(self):
        self.publish_endpoint.side_effect = OSError("test")
        with self.assertRaises(OSError):
            manager.install(self.cfg, auto=True)
        self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED])
        self.api["DeleteService"].assert_called_once_with(self.service)

    def test_activation_refuses_unreconciled_or_modified_service_command(self):
        manager.install(self.cfg)
        for command in (f'"{sys.executable}"', f'"{sys.executable}" --service unexpected',
                        f'"{Path(sys.executable).parent / "AWQM_Service.exe"}"'):
            self.api["QueryServiceConfig"].return_value = (self.w.SERVICE_WIN32_OWN_PROCESS, self.w.SERVICE_DISABLED, 1,
                                                          command, None, 0, [], self.cfg.account, "AWQM")
            for action in ("start", "enable-auto", "enable-manual"):
                with self.subTest(command=command, action=action), self.assertRaises(ValueError):
                    manager.control(action)
        self.api["StartService"].assert_not_called()

    def test_registration_stays_disabled_until_recovery_is_read_back(self):
        for auto, final in ((False, self.w.SERVICE_DEMAND_START), (True, self.w.SERVICE_AUTO_START)):
            with self.subTest(auto=auto):
                self.transitions.clear()
                def check_settings(_, key):
                    self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED])
                    return self.settings[key]
                self.api["QueryServiceConfig2"].side_effect = check_settings
                manager.install(self.cfg, auto=auto)
                self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED, final])
                recovery = self.settings[self.w.SERVICE_CONFIG_FAILURE_ACTIONS]
                self.assertEqual(recovery["ResetPeriod"], 86400)
                self.assertEqual(tuple(recovery["Actions"]), (
                    (self.w.SC_ACTION_RESTART, 30000), (self.w.SC_ACTION_RESTART, 60000),
                    (self.w.SC_ACTION_RESTART, 300000), (self.w.SC_ACTION_NONE, 0)))
                self.assertTrue(self.settings[self.w.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG])
                self.assertGreaterEqual(self.api["QueryServiceConfig2"].call_count, 2)
                self.api["DeleteService"].assert_not_called()
                self.api["StartService"].assert_not_called()

    def test_verified_policy_permits_requested_start_and_startup_modes(self):
        manager.install(self.cfg)
        self.transitions.clear()
        for action in ("start", "enable-auto", "enable-manual"):
            with self.subTest(action=action):
                manager.control(action)
                access = self.api["OpenService"].call_args.args[2]
                self.assertTrue(access & self.w.SERVICE_QUERY_CONFIG)
        self.api["StartService"].assert_called_once_with(self.service, None)
        self.assertEqual(self.transitions, [self.w.SERVICE_AUTO_START, self.w.SERVICE_DEMAND_START])

    def test_rejected_or_interrupted_configuration_deletes_only_its_own_disabled_service(self):
        for failure in (OSError(), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.transitions.clear()
                self.api["DeleteService"].reset_mock()
                self.api["CloseServiceHandle"].reset_mock()
                self.api["ChangeServiceConfig2"].side_effect = failure
                with self.assertRaises(type(failure)):
                    manager.install(self.cfg, auto=True)
                self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED])
                self.api["DeleteService"].assert_called_once_with(self.service)
                self.assertEqual([c.args[0] for c in self.api["CloseServiceHandle"].call_args_list],
                                 [self.service, self.scm])

    def test_refused_readback_or_final_start_type_rolls_back(self):
        for bad_step in ("action", "flag", "activation"):
            with self.subTest(bad_step=bad_step):
                self.transitions.clear()
                self.api["DeleteService"].reset_mock()
                def readback(_, key):
                    if key == self.w.SERVICE_CONFIG_FAILURE_ACTIONS and bad_step == "action":
                        return dict(self.settings[key], Actions=((self.w.SC_ACTION_RESTART, 1),))
                    if key == self.w.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG and bad_step == "flag":
                        return False
                    return self.settings[key]
                self.api["QueryServiceConfig2"].side_effect = readback
                self.api["ChangeServiceConfig"].side_effect = OSError("activation failed")
                with self.assertRaises((RuntimeError, OSError)):
                    manager.install(self.cfg)
                self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED])
                self.api["DeleteService"].assert_called_once_with(self.service)

    def test_existing_registration_is_never_modified_on_create_failure(self):
        self.api["CreateService"].side_effect = OSError("already exists")
        with self.assertRaises(OSError):
            manager.install(self.cfg)
        self.api["DeleteService"].assert_not_called()
        self.api["ChangeServiceConfig"].assert_not_called()
        self.api["CloseServiceHandle"].assert_called_once_with(self.scm)

    def test_failed_rollback_reports_residual_registration_without_echoing_exception_text(self):
        self.api["ChangeServiceConfig2"].side_effect = OSError("private configuration detail")
        self.api["DeleteService"].side_effect = OSError("private rollback detail")
        with self.assertRaisesRegex(RuntimeError, "removal") as failure:
            manager.install(self.cfg)
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED])
        self.api["DeleteService"].assert_called_once_with(self.service)
        self.assertEqual([c.args[0] for c in self.api["CloseServiceHandle"].call_args_list],
                         [self.service, self.scm])

    def test_activation_rejects_incomplete_policy_but_disabling_remains_available(self):
        self.api["QueryServiceConfig2"].return_value = {}
        self.api["QueryServiceConfig2"].side_effect = None
        for action in ("start", "enable-auto", "enable-manual"):
            with self.subTest(action=action):
                with self.assertRaises(RuntimeError):
                    manager.control(action)
        self.api["StartService"].assert_not_called()
        self.api["ChangeServiceConfig"].assert_not_called()
        manager.control("disable-startup")
        self.assertEqual(self.transitions, [self.w.SERVICE_DISABLED])
