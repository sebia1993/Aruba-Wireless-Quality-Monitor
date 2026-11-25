import json,unittest,tempfile,io,sys
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from dataclasses import replace
from awqm.client_info_airtime import parse,inspect
from awqm.airtime_contract import Counter,measured
from awqm.aos8 import ParseError
from awqm.probe import main

MAC="02:ab:00:00:01:01"
def fixture(values=(0,100),target=MAC,peer=6,interface="wifi0",vdev="aruba000",keys=(196614,131078)):
    prefix="wlan: [382:F:DP] "
    rows=["Client Info","-----------","Start time: synthetic fixture",
        f"client-mac {target} at {interface} {vdev}",
        f"cfg80211tool {interface} dp_peer_stats {target}",
        prefix+"HTT_PEER_DETAILS_TLV:",prefix+f"sw_peer_id = {peer}"]
    for k,v in zip(keys,values):
        rows.extend([prefix+"HTT_TX_TID_STATS_V1_TLV:",prefix+f"sw_peer_id__tid_num = {k}",
            prefix+f"tid_tx_airtime = {v}",prefix+"HTT_MSDU_FLOW_STATS_TLV:",prefix+f"sw_peer_id = {peer}"])
    rows.append(f"cfg80211tool {interface} fc_peer_stats {target}")
    return "\n".join(rows)

class ClientInfoTests(unittest.TestCase):
    def test_only_aggregate_layout_and_changes(self):
        a=parse(fixture());b=parse(fixture((5,90)))
        d=inspect(a,b)
        self.assertEqual(d["status"],"field_changes_observed")
        self.assertEqual((d["increased_fields"],d["decreased_fields"],d["unchanged_fields"]),(1,1,0))
        self.assertFalse(d["coverage_verified"]);self.assertFalse(d["unit_verified"])
        self.assertIsNone(d["airtime_percent"])
        for raw in (MAC,"wifi0","aruba000","196614","131078"):
            self.assertNotIn(raw,json.dumps(d))
        self.assertEqual(inspect(a,parse(fixture(peer=8)))["status"],"context_or_layout_changed")
        self.assertEqual(inspect(a,parse(fixture(keys=(196614,999))))["status"],"context_or_layout_changed")
        self.assertIsNone(inspect(a,parse(fixture(target="02:00:00:00:00:99")))["increased_fields"])
    def test_ambiguous_or_incomplete_subset_fails(self):
        for text in (fixture(keys=(1,1)),fixture().replace("tid_tx_airtime = 100","tid_tx_airtime = -1"),
            fixture().replace("tid_tx_airtime = 100","tid_tx_airtime = 18446744073709551616"),
            fixture().replace("tid_tx_airtime = 100","unrelated_field = 100"),
            fixture().replace("dp_peer_stats "+MAC,"dp_peer_stats 02:00:00:00:00:09"),
            fixture()+"\n--More--",fixture().replace("sw_peer_id = 6","sw_peer_id = x",1)):
            with self.subTest(text=text[-100:]):
                with self.assertRaises(ParseError):parse(text)
    def test_cli_pair_summary_has_no_raw_fields(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/"a.txt";b=Path(d)/"b.txt"
            a.write_text(fixture());b.write_text(fixture((5,100)))
            output=io.StringIO()
            with patch.object(sys,"argv",["probe","client-info-airtime",str(a),"--compare-file",str(b),"--detail"]),redirect_stdout(output):
                main()
            r=json.loads(output.getvalue())
            self.assertEqual(r["increased_fields"],1)
            self.assertIsNone(r["airtime_percent"]);self.assertNotIn(MAC,output.getvalue())
    def test_measured_contract_rejects_invalid_values_and_flags(self):
        a=Counter(1000,1000,"us","tx","epoch","metric",True)
        b=replace(a,at=1060,value=30001000)
        self.assertEqual(measured(a,b).value,50)
        for bad in (replace(b,validated=1),replace(b,value=2**64),replace(b,metric_id=""),replace(b,value=True)):
            self.assertIsNone(measured(a,bad))
