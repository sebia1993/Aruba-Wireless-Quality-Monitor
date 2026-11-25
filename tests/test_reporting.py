import tempfile,unittest,csv,io
from awqm.reporting import history_report,csv_report,html_report,cell
from awqm.storage import Store
from awqm.model import Radio

class ReportTests(unittest.TestCase):
    def test_history_export(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            s.save_batch([Radio("id","ap","=FORMULA","w","5GHz",44,1000,90,5,-94),
                          Radio("id","ap","<script>alert(1)</script>","w","5GHz",44,1060,None,5,-94)])
            r=history_report(s,"id",900,1100)
            self.assertEqual(r["count"],2)
            self.assertFalse(r["field_validated"])
            self.assertIsNone(r["rows"][1]["utilization"])
            data=list(csv.reader(io.StringIO(csv_report(r).decode("utf-8-sig"))))
            self.assertIn("'=FORMULA",data[1])
            self.assertIn("-94",data[1])
            self.assertNotIn("<script>",html_report(r).decode())
            self.assertIn("&lt;script&gt;",html_report(r).decode())
            self.assertEqual(cell("  =1"),"'  =1")
            with self.assertRaises(ValueError):history_report(s,"id",0,8*86400)
            with self.assertRaises(ValueError):history_report(s,"unknown",900,1100)
            self.assertEqual(history_report(s,"id",0,100)["count"],0)
    def test_reject_real_store(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):history_report(Store(root,mode="diagnostic"),"id",1,2)
