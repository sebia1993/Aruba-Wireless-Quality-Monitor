import json
import tempfile
import unittest

from awqm.diagnostic_view import DiagnosticView
from awqm.quality_ledger import QualityLedger
from awqm.storage import Store
from test_incidents import feed


class GlobalSearchTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.store = Store(work.name, mode="diagnostic")
        self.ledger = QualityLedger(self.store)
        self.incident = feed(self.store, self.ledger, 1000)["incident_id"]
        self.now = 1000
        self.view = DiagnosticView(self.store, lambda: self.now, incidents=self.ledger.incidents)
        self.client = dict(id="a"*32, ap_name="Demo", controller="w1", band="5GHz",
                           valid_until=1800, observed_at=999, health=80, snr=20, private="private-marker")
        self.publish()

    def publish(self, enabled=True):
        self.view.publish({}, client_views=[self.client], client_enabled=enabled)

    def test_exact_client_rechecks_approval_and_publication_before_revealing_fields(self):
        token = self.client["id"]
        def lookup():
            return self.view.clients_page(size=1, token=token)["rows"]
        self.assertEqual(lookup(), self.view.clients_page("dem")["rows"])
        self.assertNotIn("private-marker", json.dumps(lookup()))
        self.assertFalse(self.view.clients_page(size=1, token="b"*32)["rows"])
        self.publish(enabled=False)
        self.assertEqual(lookup(), [])
        self.publish()
        self.now = 1021
        self.assertEqual(lookup(), [])
        self.client["valid_until"] = self.now
        self.publish()
        self.assertEqual(lookup(), [])

    def test_incident_search_uses_public_fields_and_literal_substrings(self):
        for query in ("dEm", "W1", "5GHz", self.incident[:12]):
            with self.subTest(query=query):
                result = self.view.incident_list(1, "all", 0, 1200, query=query)
                self.assertEqual([r["incident_id"] for r in result["rows"]], [self.incident])
        for query in ("02:00:00:00", "%", "_", "private-marker"):
            self.assertEqual(self.view.incident_list(1, "all", 0, 1200, query=query)["total"], 0)

    def test_public_radio_token_is_searchable_and_internal_identifiers_are_private(self):
        token = self.view.page()["rows"][0]["id"]
        self.assertEqual(self.view.page(token)["total"], 1)
        for query in ("02:00:00:00", "private-marker"):
            self.assertEqual(self.view.page(query)["total"], 0)
            self.assertEqual(self.view.clients_page(query)["total"], 0)
