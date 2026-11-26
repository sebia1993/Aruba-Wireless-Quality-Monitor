"""Documentation-shaped synthetic D04 fixtures, no device contact."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from awqm.aos8 import ParseError
from awqm.arm_rf_summary import rf_summary, COMMAND
from awqm.collector import command
from awqm.jobs import DETAIL_COMMANDS
from awqm.worker import DETAIL_PARSERS

HEADER="channel retry phy-err mac-err noise util(Qual) cov-idx(Total) intf_idx(Total)"
LEGEND="Columns:util(Qual): ch-util/rx/tx/ext-ch-util/quality"
ROW="36 0 0 0 92 8/3/2/1/95 7/1(8) 18/11//4/1(34)"
def capture(*rows,header=HEADER,tail=""):
    return "\n".join(("Channel Summary","-"*20,header,
                      " ".join(["----"]*len(header.split())),*rows,LEGEND,tail))

class RFSummaryTests(unittest.TestCase):
    def test_legacy_and_reported_indices(self):
        r=rf_summary(capture(ROW),"DEMO-AP")
        row=r["rows"][0]
        self.assertEqual(row["channel"],36)
        self.assertIsNone(row["reported_band"])
        self.assertEqual(row["noise_raw"],"92")
        self.assertEqual(row["retry_raw"],0)
        self.assertEqual(row["utilization"]["channel_utilization"],8)
        self.assertEqual(row["interference_index"]["components"],[18,11,4,1])
        self.assertEqual(row["interference_index"]["reported_total"],34)
        self.assertTrue(row["interference_index"]["total_consistent"])
        self.assertTrue(all(v is False for k,v in r.items() if k.endswith("_verified")))
        self.assertFalse(r["field_validated"])
        self.assertNotIn("airtime",row)
        self.assertNotIn("cause",r)

    def test_explicit_band_layouts_no_channel_only_merge(self):
        for header,rows in [
            ("phy-type "+HEADER,["2.4GHz "+ROW.replace("36 ","5 ",1),"6GHz "+ROW.replace("36 ","5 ",1)]),
            (HEADER.replace("channel ","channel band ",1),[ROW.replace("36 ","5 2.4GHz ",1),ROW.replace("36 ","5 6GHz ",1)])]:
            r=rf_summary(capture(*rows,header=header),"DEMO-AP")
            self.assertEqual([(x["reported_band"],x["channel"]) for x in r["rows"]],[("2.4GHz",5),("6GHz",5)])
            with self.assertRaises(ParseError):
                rf_summary(capture(rows[0],rows[0],header=header),"DEMO-AP")

    def test_missing_and_inconsistent_not_zero_or_repaired(self):
        row=rf_summary(capture("44 N/A 0 N/A N/A N/A N/A 2/3//4/5(9)"),"DEMO-AP")["rows"][0]
        self.assertIsNone(row["retry_raw"])
        self.assertEqual(row["phy_error_raw"],0)
        self.assertIsNone(row["noise_raw"])
        self.assertIsNone(row["utilization"])
        self.assertIsNone(row["coverage_index"])
        self.assertEqual(row["interference_index"]["reported_total"],9)
        self.assertFalse(row["interference_index"]["total_consistent"])

    def test_split_scan_and_unparsed_tail(self):
        note='Noise followed by "*" indicates that the last scan on this channel was a split-scan, which was\ntriggered by high channel noise. Check show ap arm split-scan-history.'
        raw=capture(ROW.replace(" 92 "," 92* "),tail=note+"\nHT/VHT Channel Summary\n----")
        r=rf_summary(raw,"DEMO-AP")
        self.assertTrue(r["rows"][0]["split_scan_reported"])
        self.assertEqual(r["unparsed_tail_lines"],2)
        self.assertFalse(r["secondary_sections_verified"])
        self.assertFalse(r["completeness_verified"])

    def test_reject_invalid_and_ambiguous(self):
        raw=capture(ROW)
        cases=[capture(),capture(ROW,ROW),raw.replace(" 92 "," invalid "),
               raw.replace("8/3/2/1/95","101/3/2/1/95"),
               raw.replace("18/11//4/1(34)","18/11/4/1(34)"),
               raw.replace(LEGEND,""),raw.replace("phy-err","new-err"),
               raw+"\nPermission denied",raw+"\n--More--",raw+"\nunknown tail",
               raw+"\nHT Channel Summary\nChannel Summary",
               "(host)[node] #show ap arm rf-summary ap-name OTHER\n"+raw,
               capture(ROW.replace("36 ","0 ",1))]
        for bad in cases:
            with self.subTest(bad=bad[-80:]),self.assertRaises(ParseError):
                rf_summary(bad,"DEMO-AP")
        with self.assertRaises(ParseError):
            rf_summary(raw,"DEMO-AP;show version")
        self.assertFalse(rf_summary("(host)[node] #show ap arm rf-summary ap-name DEMO-AP\n"+raw,"DEMO-AP")["target_verified"])

    def test_probe_privacy_and_unchanged_live_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"rf.txt"
            path.write_text(capture(ROW),encoding="utf-8")
            args=[sys.executable,"-m","awqm.probe","rf-summary",str(path),"--ap-name","DEMO-AP"]
            run=subprocess.run(args,capture_output=True,text=True,check=True)
            report=json.loads(run.stdout)
            self.assertEqual(report["row_count"],1)
            self.assertFalse(report["field_validated"])
            self.assertNotIn("DEMO-AP",run.stdout)
            self.assertNotIn("18/11",run.stdout)
            self.assertNotIn("observation",report)
            detail=json.loads(subprocess.run(args+["--detail"],capture_output=True,text=True,check=True).stdout)
            self.assertEqual(detail["observation"]["rows"][0]["channel"],36)
            self.assertEqual(subprocess.run(args[:-2],capture_output=True).returncode,2)
        self.assertEqual(command(COMMAND,"DEMO-AP"),COMMAND+" ap-name DEMO-AP")
        self.assertNotIn(COMMAND,DETAIL_COMMANDS)
        self.assertNotIn(COMMAND,DETAIL_PARSERS)
