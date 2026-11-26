"""Synthetic D03 documentation-shaped outputs; no real AP or SSH."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from awqm.aos8 import ParseError
from awqm.radio_stats import radio_stats
from awqm.collector import command
from awqm.jobs import DETAIL_COMMANDS

FIXTURE = """RADIO Stats
-----------
Parameter Value
--------- -----
------------------- General Per-radio Statistics
Total Radio Resets 0
Resets Beacon Fail 1
Resets TX Timeouts N/A
Current Noise Floor 95
------------------- Transmit specific Statistics
TX Timeouts 4
Tx Frames Transmitted 12
Tx Mgmt Frames 2
Tx Data Frames 10
Tx Data Frames MCS 7 20
Tx Time perct @ beacon intvl 3
------------------- Received Statistics
Rx Total Data Frames Recvd 32
Rx CRC Errors 0
Channel busy 1s 50
Channel busy 4s 42.5
Channel busy 64s 0
Ch Busy perct @ beacon intvl N/A
Rx Last SNR 15
"""

class RadioStatsTests(unittest.TestCase):
    def test_distinct_windows_missing_and_zero(self):
        result=radio_stats(FIXTURE,"DEMO-AP",1)
        counts=result["counters"]
        percentages=result["radio_percentages"]
        self.assertEqual(counts["radio_resets"],0)
        self.assertEqual(counts["tx_timeouts"],4)
        self.assertEqual(counts["tx_data_frames"],10)
        self.assertEqual(counts["rx_total_data_frames"],32)
        self.assertIsNone(counts["tx_timeout_resets"])
        self.assertIsNone(counts["rx_plcp_errors"])
        self.assertEqual(percentages["channel_busy_1s_percent"],50)
        self.assertEqual(percentages["channel_busy_4s_percent"],42.5)
        self.assertEqual(percentages["channel_busy_64s_percent"],0)
        self.assertIsNone(percentages["channel_busy_beacon_interval_percent"])
        self.assertEqual(result["ignored_row_count"],3)
        for flag in ("field_validated","completeness_verified","target_verified",
                     "advanced_layout_verified","sample_time_verified","counter_epoch_verified"):
            self.assertIs(result[flag],False)
        for field in ("airtime","cause","ap_status","quality_score"):
            self.assertNotIn(field,result)

    def test_bad_input(self):
        cases=[FIXTURE.replace("RADIO Stats","CLIENT Stats"),
               FIXTURE.replace("Total Radio Resets 0","Total Radio Resets"),
               FIXTURE.replace("TX Timeouts 4","TX Timeouts 4\nTX Timeouts 4"),
               FIXTURE.replace("TX Timeouts 4","RX CRC Errors 4"),
               FIXTURE.replace("TX Timeouts 4","TX Timeouts -1"),
               FIXTURE.replace("TX Timeouts 4","TX Timeouts 18446744073709551616"),
               FIXTURE.replace("Channel busy 1s 50","Channel busy 1s 101"),
               FIXTURE.replace("Channel busy 4s 42.5","Channel busy 4s NaN"),
               FIXTURE.split("------------------- Received Statistics")[0],
               FIXTURE+"Permission denied\n", FIXTURE+"--More--",
               FIXTURE+"unmapped malformed text\n", FIXTURE*2,
               FIXTURE.replace("Received Statistics","Unexpected Statistics")]
        for raw in cases:
            with self.subTest(raw=raw[-60:]),self.assertRaises(ParseError):
                radio_stats(raw,"DEMO-AP",1)
        for ap,radio in [("bad;show version",1),("DEMO-AP",3),("DEMO-AP",True)]:
            with self.assertRaises(ParseError):
                radio_stats(FIXTURE,ap,radio)

    def test_probe_and_no_live_enable(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"fixture.txt"
            p.write_text(FIXTURE,encoding="utf-8")
            args=[sys.executable,"-m","awqm.probe","radio-stats",str(p),
                  "--ap-name","DEMO-AP","--radio-id","1"]
            proc=subprocess.run(args,capture_output=True,text=True,check=True)
            report=json.loads(proc.stdout)
            self.assertFalse(report["target_verified"])
            self.assertFalse(report["advanced_layout_verified"])
            self.assertNotIn("DEMO-AP",proc.stdout)
            self.assertNotIn("counters",report)
            detail=json.loads(subprocess.run(args+["--detail"],capture_output=True,text=True,check=True).stdout)
            self.assertEqual(detail["observation"]["counters"]["tx_timeouts"],4)
            for bad in (args[:-2],args[:-1]+["3"],args[:-4]):
                self.assertEqual(subprocess.run(bad,capture_output=True).returncode,2)
        self.assertNotIn("show ap debug radio-stats",DETAIL_COMMANDS)
        with self.assertRaises(ValueError):
            command("show ap debug radio-stats","DEMO-AP")

if __name__=="__main__":
    unittest.main()
