"""Synthetic documentation-shaped D07 data, never production captures."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from awqm.aos8 import ParseError
from awqm.ap_port_status import port_status
from awqm.collector import command
from awqm.jobs import DETAIL_COMMANDS

HEADER = "Port  MAC  Type  Forward Mode  Admin  Oper  Speed  Duplex  802.3az  PoE  STP  TX-Packets  TX-Bytes  RX-Packets  RX-Bytes"
ROW0 = "0  02:00:00:00:00:01  GE  N/A  enabled  up  2.5 Gb/s  full  N/A  N/A  N/A  12  200  13  300"
ROW1 = "1  02:00:00:00:00:02  GE  tunnel  disabled  down  N/A  N/A  N/A  N/A  Off  0  0  N/A  0"

def fixture(*rows):
    return "\n".join(['AP "DEMO-AP" Port Status (updated every 60 seconds)',
                      "-"*50, HEADER, "  ".join(["----"]*15), *rows])+"\n"

class PortStatusTests(unittest.TestCase):
    def test_observation_contract(self):
        result = port_status(fixture(ROW0, ROW1), "DEMO-AP")
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["reported_update_interval_seconds"], 60)
        for field in ("row_count_verified", "field_validated", "sample_time_verified", "physical_uplink_verified"):
            self.assertIs(result[field], False)
        a,b = result["rows"]
        self.assertEqual((a["speed_mbps"],a["tx_packets"],a["rx_bytes"]),(2500,12,300))
        self.assertEqual((b["admin"],b["oper"],b["tx_packets"]),("disabled","down",0))
        self.assertIsNone(b["rx_packets"])
        self.assertIsNone(b["speed_mbps"])
        self.assertNotIn("ap_status", result)
        self.assertNotIn("cause", result)

    def test_partial_or_invalid_never_returns_rows(self):
        cases = [fixture(), fixture(ROW0,ROW1.rsplit(" ",1)[0]),
                 fixture(ROW0,ROW0), fixture(ROW0,ROW1.replace("00:02","00:01")),
                 fixture(ROW0).replace("RX-Bytes","RX-Other"),
                 fixture(ROW0).replace(" 200 "," -1 "),
                 fixture(ROW0).replace(" 200 "," 18446744073709551616 "),
                 fixture(ROW0).replace("2.5 Gb/s","0 Gb/s"),
                 fixture(ROW0)+"--More--", fixture(ROW0)+"Permission denied\n",
                 fixture(ROW0)+"unexpected trailing line\n", fixture(ROW0)*2]
        for raw in cases:
            with self.subTest(raw=raw[-80:]), self.assertRaises(ParseError):
                port_status(raw,"DEMO-AP")
        with self.assertRaises(ParseError):
            port_status(fixture(ROW0),"OTHER-AP")
        with self.assertRaises(ParseError):
            port_status(fixture(ROW0),"DEMO-AP;show version")

    def test_missing_whole_row_does_not_establish_completeness(self):
        result=port_status(fixture(ROW0),"DEMO-AP")
        self.assertEqual(result["row_count"],1)
        self.assertFalse(result["row_count_verified"])

    def test_offline_probe_and_live_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"fixture.txt"
            p.write_text(fixture(ROW0,ROW1),encoding="utf-8")
            args=[sys.executable,"-m","awqm.probe","ap-port",str(p),"--ap-name","DEMO-AP"]
            result=subprocess.run(args,capture_output=True,text=True,check=True)
            report=json.loads(result.stdout)
            self.assertEqual(report["count"],2)
            self.assertFalse(report["field_validated"])
            self.assertFalse(report["row_count_verified"])
            self.assertNotIn("02:00:00",result.stdout)
            self.assertNotIn("DEMO-AP",result.stdout)
            self.assertEqual(subprocess.run(args[:-2],capture_output=True).returncode,2)
            self.assertEqual(subprocess.run(args[:-1]+["WRONG"],capture_output=True).returncode,2)
        self.assertEqual(command("show ap port status","DEMO-AP"),"show ap port status ap-name DEMO-AP")
        self.assertNotIn("show ap port status",DETAIL_COMMANDS)

if __name__=="__main__":
    unittest.main()
