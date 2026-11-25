import unittest
from dataclasses import replace
from awqm.rf_neighbors import arm_neighbors,ChannelBlock,overlap,candidate
from awqm.aos8 import ParseError
from awqm.model import Radio
from test_aos8 import table

def fixture(rows=None):
    fields=["bssid","essid","phy-type","channel","snr","tx-power","PL (dB)","AP Flags","Last Update (Total updates)"]
    if rows is None:rows=[["02:00:00:00:00:01","demo","5GHz","44E",35,18,75,"Active","2026-11-24 14:00:00(10)"],
            ["02:00:00:00:00:02","other","5GHz","48",10,"N/A","N/A","Passive",""]]
    return table("ARM Neighbors",fields,rows,"")

class RFTests(unittest.TestCase):
    def test_parse_without_false_freshness(self):
        rows=arm_neighbors(fixture())
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["width_candidate"],80)
        self.assertEqual(rows[0]["total_updates"],10)
        self.assertFalse(rows[0]["timestamp_verified"])
        self.assertFalse(rows[0]["row_count_verified"])
        self.assertNotIn("essid",rows[0])
        self.assertIsNone(rows[1]["tx_power"])
        with self.assertRaises(ParseError):arm_neighbors(fixture()+"\n--More--")
        with self.assertRaises(ParseError):arm_neighbors(fixture().replace("phy-type","bad-type"))
    def test_channel_overlap_and_unknown(self):
        a=ChannelBlock("5GHz",36,(36,40,44,48),True)
        b=ChannelBlock("5GHz",44,(36,40,44,48),True)
        c=ChannelBlock("5GHz",44,(44,48),True)
        d=ChannelBlock("5GHz",52,(52,56),True)
        self.assertEqual(overlap(a,b),{"relation":"same_block","overlap_mhz":80})
        self.assertEqual(overlap(a,c),{"relation":"overlapping_block","overlap_mhz":40})
        self.assertEqual(overlap(a,d)["overlap_mhz"],0)
        self.assertEqual(overlap(a,replace(b,verified=False))["relation"],"unknown")
        self.assertEqual(overlap(a,replace(b,subchannels=(36,40,149,153)))["relation"],"unknown")
        self.assertEqual(overlap(ChannelBlock("2.4GHz",1,(1,),True),ChannelBlock("2.4GHz",3,(3,),True))["overlap_mhz"],10)
        self.assertEqual(overlap(a,ChannelBlock("6GHz",5,(5,),True))["relation"],"different_band")
    def test_correlation_requires_verified_direct_aligned_evidence(self):
        a=Radio("a:0","a","AP-A","w1","5GHz",36,1000,90,5,-94)
        b=Radio("b:0","b","AP-B","w2","5GHz",44,1010,85,5,-94)
        x=ChannelBlock("5GHz",36,(36,40,44,48),True)
        y=ChannelBlock("5GHz",44,(36,40,44,48),True)
        n=arm_neighbors(fixture())[0]
        kwargs=dict(neighbor_at=1000,mapping_verified=True,field_validated=True,first_persistent=True,second_persistent=True)
        self.assertIsNone(candidate(a,b,x,y,n,1010)["candidate"])
        r=candidate(a,b,x,y,n,1010,**kwargs)
        self.assertEqual(r["candidate"],"RF05");self.assertFalse(r["verified"])
        self.assertIsNone(candidate(a,b,x,replace(y,subchannels=(44,48)),n,1010,**kwargs)["candidate"])
        self.assertEqual(candidate(a,b,x,replace(y,subchannels=(44,48)),{**n,"width_candidate":40,"channel_raw":"44+"},1010,**kwargs)["candidate"],"RF06")
        for bad in ({**kwargs,"neighbor_at":800},{**kwargs,"field_validated":False},{**kwargs,"second_persistent":False}):
            self.assertIsNone(candidate(a,b,x,y,n,1010,**bad)["candidate"])
        self.assertIsNone(candidate(a,b,x,y,{**n,"discovery":"indirect"},1010,**kwargs)["candidate"])
        self.assertIsNone(candidate(a,replace(b,ap_id="a"),x,y,n,1010,**kwargs)["candidate"])
        self.assertIsNone(candidate(a,replace(b,utilization=30),x,y,n,1010,**kwargs)["candidate"])
