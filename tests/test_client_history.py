import tempfile,unittest,json
from pathlib import Path
from awqm.client_history import ClientHistory,map_samples
from awqm.client import client_table,bss_table
from awqm.aos8 import associations,radio_summary,inventory,active_details,ControllerSnapshot,merge_radios
from awqm.fixture_session import fixtures
from dataclasses import replace

def data(n,at):
    name,f=fixtures(1,n)
    s=ControllerSnapshot("WLC-1",at,inventory(f["show ap database long"]),active_details(f["show ap active details"]),radio_summary(f["show ap radio-summary"]),associations(f["show ap association"]))
    rows,issues=merge_radios([s])
    radios=[replace(r,source="synthetic") for r in rows]
    a={x["client_mac"]:x for x in s.associations}
    return map_samples(client_table(f["show ap debug client-table"]),"WLC-1",name,"session-1",at,a,bss_table(f["show ap bss-table"]),radios)

class ClientHistoryTests(unittest.TestCase):
    def test_samples_and_counter_epochs(self):
        with tempfile.TemporaryDirectory() as root:
            h=ClientHistory(Path(root)/"clients.db",simulation=True)
            first=data(1,1000);self.assertEqual(len(first),8)
            self.assertEqual(h.ingest(first,set()),8)
            self.assertEqual(h.ingest(first,set()),0)
            second=data(2,1060)
            self.assertEqual(h.ingest(second,{x[0].radio_id for x in second}),8)
            rows=h.latest(1060)
            self.assertEqual(len(rows),8)
            self.assertTrue(any(r["quality_status"]=="partial" for r in rows))
            self.assertEqual(sum(r["activity"]=="idle" for r in rows),2)
            self.assertTrue(all(r["airtime"] is None and r["retry"] is None and r["candidate"] is None for r in rows))
            s,r,raw=second[0]
            self.assertEqual(h.ingest([(replace(s,at=1120,epoch="new"),r,raw)],set()),1)
            row=next(x for x in h.latest(1120) if x["client_mac"]==s.client_mac)
            self.assertEqual(row["activity"],"unknown")
            self.assertEqual(h.latest(1500),[])
    def test_station_independent(self):
        with tempfile.TemporaryDirectory() as root:
            h=ClientHistory(Path(root)/"clients.db",simulation=True);m=data(1,1000)
            h.ingest(m,set())
            s,_,_=m[0]
            ctx=json.dumps([s.epoch,s.radio_id,s.bssid,s.ssid],separators=(",",":"))
            h.station(s.client_mac,1000,ctx,{"sent":1000,"retried":100})
            h.station(s.client_mac,1060,ctx,{"sent":1200,"retried":170})
            r=next(x for x in h.latest(1060) if x["client_mac"]==s.client_mac)
            self.assertEqual(r["station_retry"],35)
            self.assertIsNone(r["retry"])
            self.assertIsNone(r["airtime"])
            with self.assertRaises(ValueError):ClientHistory(Path(root)/"real.db")
