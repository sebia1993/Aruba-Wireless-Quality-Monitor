import json
import tempfile
import unittest
from dataclasses import replace

from awqm.diagnostic_view import DiagnosticView
from awqm.storage import Store
from awqm.model import Radio


class PhysicalAPViewTests(unittest.TestCase):
    def setUp(self):
        root=tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.store=Store(root.name,mode="diagnostic")
        self.now=1000
        self.view=DiagnosticView(self.store,lambda:self.now,key=b"fixture")
        self.ap="site:02:00:00:00:00:01"

    def row(self,ap,rid,name="Same-name",**kw):
        return replace(Radio(ap+":"+str(rid),ap,name,"w1","5GHz",44,1000,80,5,-94,
                             source="aos8-cli-unvalidated"),**kw)

    def test_rename_same_band_and_distinct_physical_ap_or_site(self):
        a=self.row(self.ap,0)
        b=self.row(self.ap,1,"Renamed",controller="w2",observed_at=990)
        other=self.row("site:02:00:00:00:00:02",0)
        other_site=self.row("other:02:00:00:00:00:01",0)
        inconsistent=self.row(self.ap,2,radio_id="site:02:00:00:00:00:03:2")
        legacy=self.row("legacy",0)
        rows=[a,b,other,other_site,inconsistent,legacy]
        self.store.save_batch(rows)
        self.view.publish({},[(r.radio_id,r.observed_at) for r in (a,other,other_site)])
        result=self.view.ap_radios(self.view.token(a.radio_id))
        self.assertTrue(result["identity_available"])
        self.assertEqual(result["total"],2)
        by_id={r["id"]:r for r in result["rows"]}
        self.assertEqual(set(by_id),{self.view.token(a.radio_id),self.view.token(b.radio_id)})
        self.assertEqual(by_id[self.view.token(b.radio_id)]["freshness"],"stale_or_unconfirmed")
        self.assertEqual(by_id[self.view.token(b.radio_id)]["ap_name"],"Renamed")
        self.assertNotIn(self.ap,json.dumps(result))
        for r in (inconsistent,legacy):
            self.assertFalse(self.view.ap_radios(self.view.token(r.radio_id))["identity_available"])
        self.assertFalse(self.view.ap_radios("f"*32)["identity_available"])
        for page in (1,2):
            self.assertEqual(len(self.view.ap_radios(self.view.token(a.radio_id),page,size=1)["rows"]),1)
        for token,page in ((self.ap,1),(None,1),(self.view.token(a.radio_id),0)):
            with self.assertRaises(ValueError):
                self.view.ap_radios(token,page)

    def test_attestation_expires_and_unavailable_publication_masks_quality(self):
        row=self.row(self.ap,0)
        self.store.save(row)
        quality={row.radio_id:dict(observed_at=1000,valid_until=1005,score=50,status="PERSISTENT",
                                  approval="fixture",incident_id="a"*32)}
        self.view.publish({},[(row.radio_id,1000)],quality=quality)
        token=self.view.token(row.radio_id)
        self.assertEqual(self.view.ap_radios(token)["rows"][0]["quality_score"],50)
        self.now=1005
        self.assertIsNone(self.view.ap_radios(token)["rows"][0]["quality_score"])
        self.view.publish({},available=False)
        result=self.view.ap_radios(token)
        self.assertFalse(result["available"])
        self.assertEqual(result["rows"][0]["freshness"],"stale_or_unconfirmed")
        self.assertEqual(result["rows"][0]["risk_status"],"UNKNOWN")
        self.assertIsNone(result["rows"][0]["incident_id"])

    def test_list_and_search_pagination_independent_of_private_identity_order(self):
        # The physical AP rows sort last by internal ID but first by display
        # name. Changing identity representation must not shift search pages.
        a=self.row(self.ap,0,"DEMO-0001")
        b=self.row(self.ap,1,"DEMO-0002")
        legacy=[self.row("legacy",n,f"DEMO-{n:04d}") for n in range(3,19)]
        rows=[a,b,*legacy]
        self.store.save_batch(rows)
        self.view.publish({},[(r.radio_id,r.observed_at) for r in rows])
        for query in ("","demo-"):
            first=self.view.page(query,page=1,size=8)
            second=self.view.page(query,page=2,size=8)
            third=self.view.page(query,page=3,size=8)
            self.assertEqual([p["total"] for p in (first,second,third)],[18,18,18])
            self.assertEqual([r["ap_name"] for p in (first,second,third) for r in p["rows"]],
                             [f"DEMO-{n:04d}" for n in range(1,19)])
        self.assertEqual(self.view.page(self.view.token(a.radio_id))["total"],1)
        self.assertEqual(self.view.page(self.ap)["total"],0)
        self.assertEqual(self.view.ap_radios(self.view.token(a.radio_id))["total"],2)
