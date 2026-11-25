import tempfile,unittest,json,csv,io
from pathlib import Path
from awqm.storage import Store,day_start
from awqm.model import Radio
from awqm.diagnostic_view import DiagnosticView
from awqm.reporting import csv_report,html_report

class DiagnosticHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/"data",mode="diagnostic")
        self.base=day_start("20261030")
        self.now=self.base+4*86400
        self.rid="site:02:00:00:00:00:01:0"
        for offset in (0,86400,2*86400):
            for minute in (0,60,120):
                self.store.save(self.radio(self.base+offset+minute,80))
            from awqm.storage import day_for
            self.store.aggregate_day(day_for(self.base+offset),self.now)
        self.view=DiagnosticView(self.store,lambda:self.now,key=b"fixture")
        self.view.publish({},[])
        self.token=self.view.token(self.rid)
    def radio(self,t,u):
        return Radio(self.rid,"site:02:00:00:00:00:01","DEMO-AP","w1","5GHz",44,t,u,10,-94,
                     source="aos8-cli-unvalidated",epoch="w1:5GHz:AP:44")
    def test_aggregate_windows_and_exports(self):
        for resolution in ("5m","1h"):
            h=self.view.history(self.token,self.base,self.now,resolution)
            self.assertEqual(h["count"],3)
            self.assertEqual(h["coverage"]["observed_samples"],9)
            self.assertEqual(h["coverage"]["valid_samples"],9)
            self.assertEqual(h["rows"][0]["utilization_avg"],80)
            self.assertEqual(h["rows"][0]["risky"],3)
            self.assertTrue(h["coverage"]["complete_days_only"])
            for data in (json.dumps(h),csv_report(h).decode("utf-8-sig"),html_report(h).decode("utf-8")):
                self.assertNotIn(self.rid,data)
                self.assertNotIn("02:00:00:00:00:01",data)
            self.assertIn("+09:00",h["rows"][0]["bucket_kst"])
        raw=self.view.history(self.token,self.base,self.now)
        self.assertEqual(raw["count"],9)
        self.assertFalse(raw["coverage"]["complete_days_only"])
        self.assertEqual(raw["rows"][0]["context_id"],h["rows"][0]["context_id"])
        for resolution,days in (("raw",8),("5m",31),("1h",366)):
            with self.assertRaises(ValueError):self.view.history(self.token,self.base,self.base+days*86400,resolution)
        with self.assertRaises(ValueError):self.view.history(self.token,self.base,self.now,"invalid")
    def test_invalidated_day_never_bridged(self):
        self.store.save(self.radio(self.base+86400+180,30))
        h=self.view.history(self.token,self.base,self.now,"5m")
        self.assertEqual([r["bucket"] for r in h["rows"]],[self.base,self.base+2*86400])
        self.store.aggregate_day("20261031",self.now)
        h=self.view.history(self.token,self.base,self.now,"5m")
        self.assertEqual(h["count"],3)
        self.assertEqual(h["coverage"]["observed_samples"],10)
