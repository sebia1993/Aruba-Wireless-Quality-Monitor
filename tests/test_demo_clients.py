import tempfile,unittest
from awqm.storage import Store
from awqm.demo import seed,synthetic_clients

class DemoClientTests(unittest.TestCase):
    def test_demo_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);seed(store,180)
            rows=synthetic_clients(store.snapshot()["radios"])
            self.assertEqual(len(rows),48)
            self.assertTrue(all(r["source"]=="synthetic" and not r["verified"] for r in rows))
            self.assertEqual(sum(r["candidate"]=="RF01" for r in rows),3)
            self.assertTrue(all(r["candidate"] is None for r in rows if r["activity"]=="idle"))
            self.assertTrue(all(r["airtime"] is None for r in rows if r["activity"]=="idle"))
