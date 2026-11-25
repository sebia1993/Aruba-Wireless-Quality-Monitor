import unittest
from dataclasses import replace
from awqm.client import client_table,station_stats,bss_table,match_bss,Sample,Airtime,analyze,estimate_payload
from awqm.aos8 import ParseError
from awqm.model import Radio
from test_aos8 import table

def fixture():
    cols=["MAC","ESSID","BSSID","Assoc_State","Tx_Pkts","Rx_Pkts","Tx_Retries","Tx_Rate","Rx_Rate","Last_ACK_SNR","Last_Rx_SNR","Idle time","Client health (C/R)"]
    rows=[["02:00:00:00:01:01","Demo SSID","02:00:00:00:00:11","Associated",1000,500,200,24,54,13,15,0,"22/88"]]
    return table("Client Table",cols,rows,"UAPSD:(VO,VI,BK,BE,Max SP,Q Len)\nHT_State shows client's original capabilities (not operational capabilities)")

def sam(mac,at,tx=1000,health=90,retried=0):
    return Sample(mac,"ap:0","02:00:00:00:00:11","demo","epoch",at,tx,tx,health,13 if health<50 else 35,24,
                  sent=tx,retried=retried)
def rad(t=120):
    return Radio("ap:0","ap","AP","w1","5GHz",44,t,92,5,-94)
def peers():
    return [(sam(m,0,1000),sam(m,60,1200),sam(m,120,1400,40,80)) for m in ("peer1","peer2")]

class ClientTests(unittest.TestCase):
    def test_table(self):
        r=client_table(fixture())[0]
        self.assertEqual((r["health"],r["radio_health"]),(22,88))
        self.assertEqual(r["tx_rate"],24)
        self.assertFalse(r["row_count_verified"])
        self.assertIsNone(r["tx_bytes"])
        with self.assertRaises(ParseError):client_table(fixture().split("UAPSD:")[0])
    def test_stats(self):
        f="Station Stats\nParameter Value\nFrames Transmitted 200\nSuccess With Retry 70\nMissed ACKs 8"
        self.assertEqual(station_stats(f)["retried"],70)
        with self.assertRaises(ParseError):station_stats(f+"\nFrames Transmitted 20")
    def test_bss(self):
        fields=["bss","ess","ip","band/ht-mode/bandwidth","ch/EIRP/max-EIRP","type","cur-cl","ap name","fm","cluster"]
        data=table("Aruba AP BSS Table",fields,[["02:00:00:00:00:11","demo","192.0.2.10","5GHz/HE/20MHz","44/18/24","ap",4,"AP","T","A"]],"Num APs:1")
        b=bss_table(data)
        self.assertEqual(match_bss(b[0]["bssid"],"demo",b,[rad()]),"ap:0")
        self.assertIsNone(match_bss(b[0]["bssid"],"demo",b,[rad(),replace(rad(),radio_id="ap:2")]))
    def test_measured_not_auto_confirmed(self):
        p=sam("client",60,1000,30);c=sam("client",120,1200,20,70)
        a=Airtime(42,"measured","whole_interval",60,120,"epoch",True)
        r=analyze(p,c,rad(),True,peers(),a)
        self.assertEqual(r["candidate"],"RF01")
        self.assertEqual(r["impact_score"],90)
        self.assertEqual(r["peer_degraded"],2)
        self.assertFalse(r["verified"])
        self.assertEqual(r["confidence"],"medium")
    def test_missing_and_idle(self):
        p=sam("client",60,1000,30);c=sam("client",120,1200,20,70)
        a=Airtime(42,"measured","whole_interval",60,120,"epoch",False)
        r=analyze(p,c,rad(),True,peers(),a)
        self.assertIsNone(r["airtime"]);self.assertEqual(r["candidate"],"RF03")
        self.assertEqual(r["confidence"],"low")
        self.assertIsNone(analyze(p,c,rad(),True)["candidate"])
        self.assertIsNone(analyze(p,replace(c,epoch="new"),rad(),True,peers(),a)["candidate"])
        self.assertEqual(analyze(p,replace(c,tx=p.tx,rx=p.rx),rad(),True,peers(),a)["activity"],"idle")
    def test_low_airtime_is_contrary_evidence(self):
        p=sam("client",60,1000,30);c=sam("client",120,1200,20,70)
        a=Airtime(4,"measured","whole_interval",60,120,"epoch",True)
        self.assertIsNone(analyze(p,c,rad(),True,peers(),a)["candidate"])
    def test_alignment(self):
        p=sam("client",60,1000,30);c=sam("client",120,1200,20,70)
        self.assertIsNone(analyze(p,c,rad(180),True,peers())["candidate"])
        self.assertIsNone(analyze(p,c,rad(),False,peers())["candidate"])
        self.assertIsNone(estimate_payload([(1000,24)],60))
        self.assertAlmostEqual(estimate_payload([(3_000_000,24)],60,True),100/60)
