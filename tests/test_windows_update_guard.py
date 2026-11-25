"""Mocked SCM preflight, no actual service operations."""
import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
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
