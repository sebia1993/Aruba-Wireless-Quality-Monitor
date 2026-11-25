import unittest
from dataclasses import replace
from awqm.aos8 import (radio_summary,inventory,active_details,associations,cpu_load,
    merge_radios,merge_associations,ControllerSnapshot,ParseError)

def table(title,fields,rows,footer):
    widths=[max(len(k),*(len(str(r[i])) for r in rows),1)+3 for i,k in enumerate(fields)]
    def line(values):return "".join(str(v).ljust(w) for v,w in zip(values,widths)).rstrip()
    return "\n".join([title,"-"*len(title),line(fields),line(["-"*(w-3) for w in widths]),*[line(r) for r in rows],footer])

def sample():
    ip="192.0.2.10"
    inv=table("AP Database",["Name","Group","AP Type","IP Address","Status","Switch IP","Standby IP","Wired MAC Address","Serial #"],
              [["DEMO-AP","demo","535",ip,"Up 1d","192.0.2.1","192.0.2.2","02:00:00:00:00:10","DEMO"]],"Total APs:1")
    act=table("Active AP Table",["Name","IP Address","Cluster Role","Radio 0 Band Ch/EIRP/MaxEIRP/Clients","Radio 1 Band Ch/EIRP/MaxEIRP/Clients"],
              [["DEMO-AP",ip,"A","AP:5GHz-HE:44+/18/24/12","AP:2.4GHz-HE:6/12/20/4"]],"Num APs:1")
    rf=table("APs Radios information",["Name","Group","AP Type","IP Address","Band","Mode","EIRP/MaxEIRP","NF/U/I","TD","TM","TC"],
             [["DEMO-AP","demo","535",ip,"5","AP:44+","18/24","-94/ 91/ 5","1/2/3/4/5/6","0/0/0/0/0/0","0/0/0/0/0/0"],
              ["DEMO-AP","demo","535",ip,"2.4","AP:6","12/20","-96/ 24/ 2","1/2/3/4/5/6","0/0/0/0/0/0","0/0/0/0/0/0"]],"Total Radios:2")
    assoc=table("Association Table",["Name","bssid","mac","auth","assoc","essid","Flags"],
                [["DEMO-AP","02:00:00:00:00:11","02:00:00:00:01:01","y","y","Demo SSID","WAB"],
                 ["DEMO-AP","02:00:00:00:00:12","02:00:00:00:01:02","n","n","Demo SSID","WB"]],"Num Clients:2")
    return inv,act,rf,assoc

class ParserTests(unittest.TestCase):
    def test_parse_and_merge(self):
        inv,act,rf,assoc=sample()
        s=ControllerSnapshot("WLC-1",120,inventory(inv),active_details(act),radio_summary(rf),associations(assoc))
        rows,unresolved=merge_radios([s])
        self.assertEqual(len(rows),2);self.assertEqual(unresolved,[])
        self.assertEqual(rows[0].radio_id,"default:02:00:00:00:00:10:0")
        self.assertEqual(rows[0].utilization,91)
        self.assertEqual(rows[0].clients,12)
        self.assertEqual(len(merge_associations([s])[0]),1)
    def test_conflicts_and_stale(self):
        inv,act,rf,assoc=sample()
        s=ControllerSnapshot("WLC-1",120,inventory(inv),active_details(act),radio_summary(rf),associations(assoc))
        rows,issues=merge_radios([s,replace(s,controller="WLC-2")])
        self.assertEqual(rows,[]);self.assertTrue(issues)
        self.assertEqual(len(merge_radios([s,replace(s,controller="WLC-2",observed_at=30)])[0]),2)
        other=replace(s,controller="WLC-2",associations=[{**s.associations[0],"bssid":"02:00:00:00:00:22"}])
        self.assertEqual(len(merge_associations([s,other])[1]),1)
    def test_missing_and_malformed(self):
        _,_,rf,_=sample()
        for bad in (rf.replace("Total Radios:2","Total Radios:3"),rf.replace("Total Radios:2",""),
                    rf.replace("-94/ 91/ 5","-94/ 191/5"),rf+"\n--More--"):
            with self.assertRaises(ParseError):radio_summary(bad)
        # Missing markers are formatted in a new aligned table, not inserted into
        # an existing fixed-width output where they could shift following fields.
        fields=["Name","Group","AP Type","IP Address","Band","Mode","EIRP/MaxEIRP","NF/U/I","TD","TM","TC"]
        for raw,expected in (("N/A/ N/A/ N/A",(None,None,None)),
                             ("-94/ N/A/ 5",(-94,None,5)),
                             ("-94/ 0/ 0",(-94,0,0)),
                             ("-94/ -/ NA",(-94,None,None))):
            fixture=table("APs Radios information",fields,
                [["DEMO-AP","demo","535","192.0.2.10","5","AP:44+","18/24",raw,
                  "1/2/3/4/5/6","0/0/0/0/0/0","0/0/0/0/0/0"]],"Total Radios:1")
            row=radio_summary(fixture)[0]
            self.assertEqual((row["noise"],row["utilization"],row["interference"]),expected)
            self.assertEqual(row["td"],"1/2/3/4/5/6")
        for malformed in ("-94/ N/ 5","-94/ 191/ 5","-94/ 10/ 5/ 1","-94/ N/A"):
            fixture=table("APs Radios information",fields,
                [["DEMO-AP","demo","535","192.0.2.10","5","AP:44+","18/24",malformed,
                  "0","0","0"]],"Total Radios:1")
            with self.assertRaises(ParseError):radio_summary(fixture)
    def test_cpu(self):
        self.assertEqual(cpu_load("user 6.9%, system 7.7%, idle 85.4%"),14.6)
        with self.assertRaises(ParseError):cpu_load("user 50%, system 50%, idle 85%")
    def test_empty(self):
        txt=table("Association Table",["Name","bssid","mac","auth","assoc","essid","Flags"],[],"Num Clients:0")
        self.assertEqual(associations(txt),[])
    def test_radio_ambiguity(self):
        inv,act,rf,assoc=sample()
        a=active_details(act);a[0]["radios"].append({**a[0]["radios"][0],"radio_id":2})
        s=ControllerSnapshot("WLC-1",120,inventory(inv),a,radio_summary(rf),associations(assoc))
        rows,issues=merge_radios([s])
        self.assertEqual(len(rows),1);self.assertEqual(len(issues),1)

    def test_independently_fresh_radios_and_physical_owner_conflict(self):
        inv,act,rf,assoc=sample()
        s=ControllerSnapshot("WLC-1",120,inventory(inv),active_details(act),radio_summary(rf),associations(assoc))
        inv2=inv.replace("DEMO-AP","DEMO-BB").replace("192.0.2.10","192.0.2.11").replace("02:00:00:00:00:10","02:00:00:00:00:20")
        act2=act.replace("DEMO-AP","DEMO-BB").replace("192.0.2.10","192.0.2.11")
        rf2=rf.replace("DEMO-AP","DEMO-BB").replace("192.0.2.10","192.0.2.11")
        t=ControllerSnapshot("WLC-2",30,inventory(inv2),active_details(act2),radio_summary(rf2))
        self.assertEqual(len(merge_radios([s,t])[0]),2)
        self.assertEqual(len(merge_radios([s,t],max_skew=None)[0]),4)
        t=replace(t,inventory=inventory(inv2.replace("02:00:00:00:00:20","02:00:00:00:00:10")))
        rows,issues=merge_radios([s,t],max_skew=None)
        self.assertEqual(rows,[]);self.assertTrue(issues)
