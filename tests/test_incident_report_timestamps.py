import csv
import io
import tempfile
import unittest
from html.parser import HTMLParser

from awqm.incident_reports import incident_csv, incident_html
from awqm.quality_ledger import QualityLedger
from awqm.reporting import timestamp
from awqm.storage import Store
from test_incidents import feed


class ReportTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        if tag in ("th", "td"):
            self.rows[-1].append("")
            self.in_cell = True

    def handle_data(self, data):
        if self.in_cell:
            self.rows[-1][-1] += data

    def handle_endtag(self, tag):
        if tag in ("th", "td"):
            self.in_cell = False


class IncidentReportTimestampTests(unittest.TestCase):
    def test_context_change_does_not_redate_prior_radio_measurements(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, mode="diagnostic")
            ledger = QualityLedger(store)
            for observed_at in (1000, 1180, 1360):
                incident = feed(store, ledger, observed_at)["incident_id"]
            revision = ledger.incidents.detail(incident)["revision"]
            feed(store, ledger, 1540, connection="other-session")
            ledger.incidents.clock = lambda: 1600
            ledger.incidents.review(incident, revision, "request", "operator", "supported",
                                    "radio_evidence", "RF04")
            detail = ledger.incidents.detail(incident)
            self.assertEqual(detail["state"], "CONTEXT_LOST")
            self.assertEqual(detail["timeline"][-1]["at"], 1540)
            self.assertEqual(detail["timeline"][-1]["evidence"]["observed_at"], 1360)

            csv_records = list(csv.DictReader(io.StringIO(incident_csv(detail).decode("utf-8-sig"))))
            table = ReportTable()
            table.feed(incident_html(detail).decode("utf-8"))
            html_records = [dict(zip(table.rows[0], row)) for row in table.rows[1:]]
            for format_name, records in (("CSV", csv_records), ("HTML", html_records)):
                with self.subTest(format=format_name):
                    change = next(row for row in records if row["record_type"] == "evidence"
                                  and row["state"] == "CONTEXT_LOST")
                    self.assertEqual(change["observed_at_kst"], timestamp(1360))
                    self.assertEqual(change["event_at_kst"], timestamp(1540))
                    review = next(row for row in records if row["record_type"] == "review")
                    self.assertEqual(review["observed_at_kst"], "")
                    self.assertEqual(review["event_at_kst"], timestamp(1600))
