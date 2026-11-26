"""Coordinator-level regression for current cross-WLC identity evidence."""
from dataclasses import replace
import tempfile
import unittest

from awqm.engine import Engine
from awqm.fixture_session import FixtureSession
from awqm.storage import Store
from awqm.worker import ControllerWorker


class IdentityConflictEngineTests(unittest.TestCase):
    def test_conflict_withdraws_current_radio_without_deleting_history(self):
        now=1000
        clock=lambda:now
        workers=[ControllerWorker(f"w{i}",lambda i=i:FixtureSession(i),clock,clock,
                                  mode="standard") for i in (1,2)]
        for w in workers:
            self.addCleanup(w.close)
            for _ in w.schedule.polls:
                self.assertEqual(w.tick().status,"success")
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic")
            engine=Engine(workers,store,clock=clock,wall=clock)
            try:
                first,second=(w.snapshot() for w in workers)
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                # Isolate single-writer publication from asynchronous polling.
                engine.next_tick={w.controller:now+100 for w in workers}
                engine.snapshots={"w1":first,"w2":second}
                self.assertEqual(engine.pulse()["mapped_radios"],4)
                initial=set(engine.published_radios)
                self.assertEqual(len(initial),4)
                # A peer inventory reports a new name/IP for the physical
                # AP while the original AAC still claims its previous identity.
                contradiction=dict(first.inventory[0],name="RENAMED",ip="192.0.2.99")
                engine.snapshots["w2"]=replace(second,inventory=[*second.inventory,contradiction])
                result=engine.pulse()
                self.assertEqual(result["mapped_radios"],2)
                self.assertEqual(result["unresolved_radios"],2)
                self.assertFalse(result["field_validated"])
                self.assertEqual(set(engine.published_radios),
                                 {pair for pair in initial if ":02:00:00:00:00:02:" in pair[0]})
                self.assertEqual(len(store.snapshot()["radios"]),4)
                # A fresh agreeing peer record removes the veto, without
                # manufacturing a new sample or historical observation.
                engine.snapshots["w2"]=replace(second,inventory=[*second.inventory,first.inventory[0]])
                self.assertEqual(engine.pulse()["mapped_radios"],4)
                self.assertEqual(set(engine.published_radios),initial)
                self.assertEqual(len(store.snapshot()["radios"]),4)
            finally:
                engine.close()
