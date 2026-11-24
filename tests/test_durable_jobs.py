import tempfile,unittest
from pathlib import Path
from awqm.durable_jobs import DurableJobs

class DurableTests(unittest.TestCase):
    def test_recover_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"jobs.db"
            q=DurableJobs(p,"w1",now=100)
            a=q.submit("one","show ap debug client-table","AP1",100)
            self.assertEqual(q.submit("alias","show ap debug client-table","AP1",101).job_id,a.job_id)
            b=q.submit("two","show ap debug client-table","AP2",101,ttl=10)
            c=q.submit("three","show ap debug client-table","AP3",101)
            q.start(a.job_id,102)
            q=DurableJobs(p,"w1",now=112)
            self.assertEqual(q.by_request("alias").status,"failed")
            self.assertEqual(q.get(a.job_id).reason,"interrupted_outcome_unknown")
            self.assertEqual(q.get(b.job_id).status,"expired")
            self.assertEqual(q.get(c.job_id).reason,"restart_revalidate")
            self.assertEqual(q.submit("three","show ap debug client-table","AP3",113).job_id,c.job_id)
            self.assertEqual(q.cancel(c.job_id,114).status,"cancelled")
            self.assertEqual(DurableJobs(p,"w2",now=115).list(115),[])
    def test_persist_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            q=DurableJobs(Path(root)/"j.db","w",now=0,limit=1)
            a=q.submit("r","show ap debug client-table","AP",0)
            with self.assertRaises(ValueError):q.submit("r","show ap debug client-table","Other",0)
            with self.assertRaises(RuntimeError):q.submit("new","show ap debug client-table","Other",0)
            self.assertEqual(q.by_request("r").job_id,a.job_id)
            self.assertIsNone(q.by_request("new"))
