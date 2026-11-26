"""Synthetic documentation-shaped ARM output, no production device contact."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from awqm.aos8 import ParseError
from awqm.arm_history import arm_history, LEGACY_LEGEND, MODERN_LEGEND
from awqm.collector import command
from awqm.jobs import DETAIL_COMMANDS, Jobs

TABLE = """ARM History
-----------
Reason Old channel New channel Old Power New Power Last change
------ ----------- ----------- --------- --------- -----------
"""
ROWS = """I 36+ 44- 18 18 0d:0h:1m:2s
P- 44- 44- 18 15 0d:0h:0m:0s
OFF(R) 44- 0 N/A 0 1d:2h:3m:4s
"""
LEGACY = "Interface :wifi0\n"+TABLE+ROWS+"Interface :wifi1\n"+TABLE+"P+ 6 6 9 12 0d:0h:0m:9s\n"+LEGACY_LEGEND
MODERN = "Interface :wifi0\nPhy-Type :5GHz\nInterface :wifi1\nPhy-Type :2.4GHz\n"+MODERN_LEGEND

class ArmHistoryTests(unittest.TestCase):
    def test_legacy_rows_keep_source_and_age(self):
        r=arm_history(LEGACY,"DEMO-AP")
        self.assertEqual(r["event_count"],4)
        self.assertEqual(r["interface_count"],2)
        a,b=r["interfaces"]
        self.assertIsNone(a["phy_type"])
        self.assertTrue(a["table_present"])
        self.assertEqual(a["events"][0],dict(reason_code="I",old_channel="36+",
            new_channel="44-",old_power="18",new_power="18",reported_age_seconds=62))
        self.assertEqual(a["events"][1]["reported_age_seconds"],0)
        self.assertIsNone(a["events"][2]["old_power"])
        self.assertEqual(a["events"][2]["new_power"],"0")
        self.assertEqual(b["interface"],"wifi1")
        for flag in ("field_validated","target_verified","radio_binding_verified",
                     "completeness_verified","sample_time_verified","event_time_verified"):
            self.assertIs(r[flag],False)
        for key in ("cause","ap_status","event_time","airtime"):
            self.assertNotIn(key,r)

    def test_modern_header_only_is_not_complete_zero_history(self):
        r=arm_history("(host) [mynode] (config) #show ap arm history ap-name DEMO-AP\n"+MODERN,"DEMO-AP")
        self.assertEqual(r["event_count"],0)
        self.assertEqual([x["phy_type"] for x in r["interfaces"]],["5GHz","2.4GHz"])
        self.assertFalse(any(x["table_present"] for x in r["interfaces"]))
        self.assertFalse(r["completeness_verified"])
        mixed=MODERN.replace("Interface :wifi1",TABLE+ROWS+"Interface :wifi1")
        self.assertEqual(arm_history(mixed,"DEMO-AP")["event_count"],3)

    def test_bad_layout_and_bounds(self):
        for raw in (LEGACY.replace("Interface :wifi1","Interface :wifi0"),
                    LEGACY.replace("Reason Old channel","Reason Unknown"),
                    LEGACY.replace("0d:0h:1m:2s","0d:24h:1m:2s"),
                    LEGACY.replace("0d:0h:1m:2s","0d:0h:60m:2s"),
                    LEGACY.replace("36+","999+"),
                    LEGACY.replace("18 18","nan 18"),
                    LEGACY.replace("I 36+","UNKNOWN 36+"),
                    LEGACY.replace("P- 44- 44- 18 15","P- 44- 44-"),
                    LEGACY.replace(LEGACY_LEGEND,""),
                    LEGACY+"\nPermission denied",LEGACY+"\n--More--",
                    LEGACY+"\nextra row",MODERN.replace("Phy-Type :2.4GHz","Phy-Type :8GHz"),
                    MODERN.replace("Phy-Type :2.4GHz",""),
                    "show ap arm history ap-name OTHER\n"+LEGACY):
            with self.subTest(raw=raw[:100]), self.assertRaises(ParseError):
                arm_history(raw,"DEMO-AP")
        with self.assertRaises(ParseError):
            arm_history(LEGACY,"bad;show version")

    def test_probe_privacy_and_live_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"capture.txt"
            p.write_text(LEGACY,encoding="utf-8")
            args=[sys.executable,"-m","awqm.probe","arm-history",str(p),"--ap-name","DEMO-AP"]
            proc=subprocess.run(args,capture_output=True,text=True,check=True)
            r=json.loads(proc.stdout)
            self.assertEqual(r["event_count"],4)
            self.assertFalse(r["target_verified"])
            self.assertNotIn("DEMO-AP",proc.stdout)
            self.assertNotIn("interfaces",r)
            self.assertNotIn("36+",proc.stdout)
            detail=json.loads(subprocess.run(args+["--detail"],capture_output=True,text=True,check=True).stdout)
            self.assertEqual(detail["observation"]["interfaces"][0]["events"][0]["reason_code"],"I")
            self.assertEqual(subprocess.run(args[:-2],capture_output=True).returncode,2)
        self.assertEqual(command("show ap arm history","DEMO-AP"),"show ap arm history ap-name DEMO-AP")
        self.assertNotIn("show ap arm history",DETAIL_COMMANDS)
        with self.assertRaises(ValueError):
            Jobs().submit("request","show ap arm history","DEMO-AP",1)

if __name__=="__main__":
    unittest.main()
