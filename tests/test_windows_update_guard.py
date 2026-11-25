"""Mocked SCM preflight, no actual service operations."""
import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from awqm.windows_security import account_sid
from awqm.windows_update import scm_stopped

class SCMGuardTests(unittest.TestCase):
    def test_scm_local_account_resolves_to_machine_sid(self):
        local = "S-1-5-21-1-2-3-1001"
        domain = "S-1-5-21-4-5-6-1001"
        accounts = {r"HOST\service": local, r"DOMAIN\service": domain}

        def lookup(system, name):
            self.assertIsNone(system)
            self.assertIn(name, accounts)
            return accounts[name], name.split("\\")[0], 1

        api = SimpleNamespace(GetComputerName=lambda: "HOST")
        security = SimpleNamespace(LookupAccountName=lookup, ConvertSidToStringSid=str)
        with patch.dict(sys.modules, {"win32api": api, "win32security": security}):
            self.assertEqual(account_sid(r".\service"), local)
            self.assertEqual(account_sid(r"HOST\service"), local)
            self.assertEqual(account_sid(r"DOMAIN\service"), domain)

    def test_stopped_disabled_identity_and_path(self):
        with tempfile.TemporaryDirectory() as root:
            installed=Path(root)/"AWQM"
            cfg=SimpleNamespace(account="DOMAIN\\service")
            state=[1];start=[4];binary=['"'+str(installed/"AWQM_Service.exe")+'"'];account=[cfg.account]
            closed=[]
            def config(_):return (16,start[0],1,binary[0],None,0,[],account[0],"AWQM")
            fake=SimpleNamespace(SC_MANAGER_CONNECT=1,SERVICE_QUERY_CONFIG=1,SERVICE_QUERY_STATUS=4,
                SERVICE_STOPPED=1,SERVICE_WIN32_OWN_PROCESS=16,SERVICE_AUTO_START=2,SERVICE_DEMAND_START=3,SERVICE_DISABLED=4,
                OpenSCManager=lambda *a:"scm",OpenService=lambda *a:"service",
                QueryServiceStatus=lambda *a:(16,state[0],0,0,0,0,0),QueryServiceConfig=config,
                CloseServiceHandle=lambda h:closed.append(h))
            with patch.dict(sys.modules,{"win32service":fake}),patch("awqm.windows_update.account_sid",side_effect=lambda x:x):
                with scm_stopped(cfg,installed,require_disabled=True) as guard:
                    guard()
                    state[0]=4
                    with self.assertRaises(RuntimeError):guard()
                    state[0]=1;start[0]=2
                    with self.assertRaises(RuntimeError):guard()
                    start[0]=4;account[0]="different"
                    with self.assertRaises(ValueError):guard()
                    account[0]=cfg.account;binary[0]=str(installed/"other.exe")
                    with self.assertRaises(ValueError):guard()
                self.assertEqual(closed,["service","scm"])

    def test_unified_binding_is_exact_recoverable_and_restores_legacy_binary(self):
        with tempfile.TemporaryDirectory() as root:
            installed = Path(root) / "Program Files" / "AWQM"
            installed.mkdir(parents=True)
            (installed / "AWQM.exe").touch()
            (installed / "_internal").mkdir()
            (installed / "VERSION.txt").write_text("0.45.0", encoding="utf-8")
            legacy = f'"{installed / "AWQM_Service.exe"}"'
            unified = f'"{installed / "AWQM.exe"}" --service'
            cfg = SimpleNamespace(account="fixture")
            current = [legacy]
            fake = SimpleNamespace(SC_MANAGER_CONNECT=1, SERVICE_QUERY_CONFIG=1, SERVICE_CHANGE_CONFIG=2,
                SERVICE_QUERY_STATUS=4, SERVICE_STOPPED=1, SERVICE_WIN32_OWN_PROCESS=16,
                SERVICE_AUTO_START=2, SERVICE_DEMAND_START=3, SERVICE_DISABLED=4, SERVICE_NO_CHANGE=0xffffffff,
                OpenSCManager=Mock(), OpenService=Mock(), CloseServiceHandle=Mock(),
                QueryServiceStatus=lambda *_: (16,1),
                QueryServiceConfig=lambda *_:(16,4,1,current[0],None,0,[],cfg.account,"AWQM"),
                ChangeServiceConfig=Mock())
            with patch.dict(sys.modules, {"win32service": fake}), patch("awqm.windows_update.account_sid", side_effect=str):
                with scm_stopped(cfg, installed, require_disabled=True, update_command=True) as guard:
                    with self.assertRaisesRegex(RuntimeError, "readback"):
                        guard(sync_command=True)
                    def apply(service, kind, start, error, command, group, tag, deps, user, password, name):
                        self.assertEqual((kind, start, error), (fake.SERVICE_NO_CHANGE,)*3)
                        self.assertEqual((group, tag, deps, user, password, name), (None,0,None,None,None,None))
                        current[0] = command
                    fake.ChangeServiceConfig.side_effect = apply
                    guard(sync_command=True)
                    self.assertEqual(current[0], unified)
                    guard(sync_command=True)
                    self.assertEqual(fake.ChangeServiceConfig.call_count, 2)
                    for malformed in (unified+" extra", unified.upper(), unified.replace('"','')):
                        current[0] = malformed
                        with self.assertRaises(ValueError):
                            guard()
                    current[0] = unified
                    (installed / "VERSION.txt").write_text("0.44.0", encoding="utf-8")
                    (installed / "AWQM_Service.exe").touch()
                    with self.assertRaises(ValueError):
                        guard(sync_command=True)
                    (installed / "AWQM_Manager.exe").touch()
                    guard(sync_command=True)
                    self.assertEqual(current[0], legacy)
                    current[0] = str(installed / "AWQM_Service.exe")
                    guard(sync_command=True)
                    self.assertEqual(current[0], legacy)
                self.assertTrue(fake.OpenService.call_args.args[2] & fake.SERVICE_CHANGE_CONFIG)
