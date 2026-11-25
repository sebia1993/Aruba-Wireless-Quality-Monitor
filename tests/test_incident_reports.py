import csv,io,json,tempfile,unittest
from dataclasses import replace
from awqm.storage import Store
from awqm.quality_ledger import QualityLedger
from awqm.incident_reports import summary,incident_csv,incident_html
from test_incidents import observation,snap

class IncidentReportTests(unittest.TestCase):
    def test_report_provenance_and_escape(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");l=QualityLedger(s)
            for t in (1000,1180,1360):
                r=replace(observation(t),ap_name="=FORMULA<script>alert(1)</script>")
                s.save(r);q=l.evaluate([r],[snap(t)],t,{r.radio_id:t})
            ident=q[r.radio_id]["incident_id"];d=l.incidents.detail(ident)
            l.incidents.review(ident,d["revision"],"review","fixture-private-id","supported","radio_evidence","RF04")
            d=l.incidents.detail(ident);d["display_state"]="UNCONFIRMED"
            result=summary(d)
            self.assertIn("□ ",result);self.assertIn("- 관측시간",result);self.assertIn("현재 확인 불가",result)
            self.assertIn("실제 서비스 영향 미확인",result);self.assertIn("실제 장비 조치",result)
            self.assertNotIn("fixture-private-id",result)
            raw=incident_csv(d).decode("utf-8-sig")
            values=list(csv.DictReader(io.StringIO(raw)))
            self.assertEqual(values[0]["state"],"UNCONFIRMED")
            self.assertEqual(values[0]["service_impact"],"unverified")
            self.assertTrue(values[0]["ap_name"].startswith("'=FORMULA"))
            self.assertEqual(values[-1]["record_type"],"review")
            rendered=incident_html(d).decode("utf-8")
            self.assertNotIn("<script>",rendered);self.assertIn("&lt;script&gt;",rendered)
            for forbidden in ("fixture-private-id","02:00:00:00:00:01"):
                self.assertNotIn(forbidden,raw);self.assertNotIn(forbidden,rendered)
