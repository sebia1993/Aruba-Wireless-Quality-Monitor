import tempfile
import unittest
from pathlib import Path

from awqm.engine import Engine
from awqm.scale_validation import workers
from awqm.storage import Store


class HistoryObserver:
    def ingest(self, radios, snapshots, frames, claims, now):
        self.claims = set(claims)


class AssociationInventoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 1000
        self.nodes = workers(lambda: self.now, lambda: self.now, 1, 2, offsets=True)
        self.history = HistoryObserver()
        self.engine = Engine(self.nodes, Store(Path(folder.name), mode="diagnostic"),
                             clock=lambda: self.now, wall=lambda: self.now,
                             association_history=self.history)
        self.addCleanup(self.engine.close)

    def step(self):
        self.now += 1
        result = self.engine.pulse()
        for future in self.engine.inflight.values():
            future.result(timeout=5)
        return result

    def advance(self, seconds):
        for _ in range(seconds):
            result = self.step()
        return result

    def test_inventory_survives_radio_skew_but_history_only_gets_aligned_claims(self):
        self.advance(180)
        counts = []
        aligned = []
        for _ in range(120):
            result = self.step()
            self.assertEqual(result["coherent_controllers"], 4)
            self.assertEqual(result["mapped_radios"], 8)
            counts.append(result["active_clients"])
            aligned.append(len(self.history.claims))
        self.assertEqual(set(counts), {8})
        self.assertEqual((min(aligned), max(aligned)), (0, 8))

    def test_unaligned_conflicts_still_exclude_client_and_duplicate_claims_count_once(self):
        self.advance(180)
        first, second = self.nodes[:2]
        first_table = first.session.responses["show ap association"]
        second_table = second.session.responses["show ap association"]
        old_row = second.latest["show ap association"].data[0]
        other_row = first.latest["show ap association"].data[0]
        second.session.responses["show ap association"] = second_table.replace(
            old_row["client_mac"], other_row["client_mac"])
        second.schedule.due["show ap association"] = 0
        self.advance(5)
        for _ in range(90):
            result = self.step()
            self.assertEqual((result["active_clients"], result["client_conflicts"]), (6, 1))
            self.assertNotIn(other_row["client_mac"], self.history.claims)
        second.session.responses["show ap association"] = first_table
        second.schedule.due["show ap association"] = 0
        result = self.advance(5)
        self.assertEqual((result["active_clients"], result["client_conflicts"]), (6, 0))

    def test_failed_expired_paused_and_reconnected_sources_are_withdrawn(self):
        self.advance(180)
        first, second, third, fourth = self.nodes
        first.session.responses["show ap association"] = "incomplete table"
        first.schedule.due["show ap association"] = 0
        second.schedule.due["show ap association"] = float("inf")
        self.advance(155)
        self.assertEqual(self.engine.last_result["active_clients"], 4)
        self.assertNotEqual(first.outcomes["show ap association"].status, "success")
        self.assertGreater(self.now - second.latest["show ap association"].observed_at, 150)
        desired = {"revision": 1, "controllers": {
            w.controller: {"requested_paused": w is third, "revision": int(w is third),
                           "changed_at": self.now if w is third else None} for w in self.nodes}}
        self.engine.apply_operations(desired)
        result = self.advance(2)
        self.assertEqual((result["active_clients"], result["coherent_controllers"]), (2, 3))
        # Drop transport and reconnect before another association poll can succeed.
        fourth.close()
        prior = fourth.generation
        result = self.advance(2)
        self.assertGreater(fourth.generation, prior)
        self.assertEqual(result["active_clients"], 0)
