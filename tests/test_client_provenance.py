import tempfile
import unittest
from dataclasses import replace

from awqm.aos8 import active_details, inventory, merge_radios, radio_summary
from awqm.client import Airtime, analyze, estimate_payload
from awqm.diagnostic_view import DiagnosticView
from awqm.live_clients import ClientApproval, LiveClients
from awqm.station_validation import AssociationStamp, StationApproval
from awqm.storage import Store
from awqm.worker import Reading
from test_aos8 import sample
from test_client import peers, rad, sam
from test_live_clients import BSS, MAC, SSID, fixture


class ClientProvenanceTests(unittest.TestCase):
    def test_synthetic_airtime_requires_explicit_boolean_validation(self):
        target = sam("target", 120, 1200, 20, 70)
        previous = sam("target", 60, 1000, 30)
        for flag in ("false", 1):
            with self.subTest(flag=flag):
                measured = Airtime(40, "measured", "whole_interval", 60, 120, target.epoch, flag)
                result = analyze(previous, target, rad(), True, peers(), measured)
                self.assertIsNone(result["airtime"])
                self.assertNotEqual(result["candidate"], "RF01")
                self.assertIsNone(estimate_payload([(3_000_000, 24)], 60, flag))

    def test_reused_bssid_on_new_ap_cannot_inherit_previous_station_interval(self):
        r, s, q, a, assoc, bss, detail = fixture()
        clients = LiveClients({"w1": ClientApproval("tag", 900, 1900, True, "ct")}, key=b"fixture",
                              station_approvals={"w1": StationApproval("ct", 900, 1800, "station")})
        clients.ingest(detail, "w1", s, assoc, bss, [r], q, {MAC:a}, 1010)
        for t, age, sent, retry in ((1010, 100, 1000, 100), (1040, 130, 1200, 170)):
            stamp = AssociationStamp(r.ap_name, BSS, SSID, 1, 1, age, t)
            reading = Reading("show ap debug client-stats", "success", t, {"before":stamp, "after":stamp,
                              "counters":{"sent":sent, "retried":retry}}, generation=1, target=MAC)
            clients.ingest(reading, "w1", s, assoc, bss, [r], q, {MAC:a}, t)
        self.assertEqual(clients.publish([r], [s], q, {MAC:a}, 1040)[0]["station_retry"], 35)
        new = replace(r, ap_id="new-ap", ap_name="new-name", radio_id="new-radio", observed_at=1041)
        claim = dict(a, ap_name=new.ap_name)
        quality = {new.radio_id: dict(q[r.radio_id], observed_at=1041)}
        assoc = replace(assoc, data=[claim], observed_at=1041)
        bss = replace(bss, data=[dict(bss.data[0], ap_name=new.ap_name)], observed_at=1041)
        detail = replace(detail, target=new.ap_name, observed_at=1041)
        snapshot = replace(s, observed_at=1041)
        self.assertEqual(clients.ingest(detail, "w1", snapshot, assoc, bss, [new], quality, {MAC:claim}, 1041), 1)
        row = clients.publish([new], [snapshot], quality, {MAC:claim}, 1041)[0]
        self.assertIsNone(row["station_retry"])
        self.assertIsNone(row["station_start"])
        self.assertIsNone(row["candidate"])

    def test_other_network_or_epoch_cannot_supply_peer_evidence(self):
        target = sam("target", 120, 1200, 20, 70)
        previous = sam("target", 60, 1000, 30)
        airtime = Airtime(40, "measured", "whole_interval", 60, 120, target.epoch, True)
        for changed in ({"ssid": "other-network"}, {"epoch": "other-configuration"}):
            cohort = [tuple(replace(s, **changed) for s in history) for history in peers()]
            for occupancy in (None, airtime):
                with self.subTest(changed=changed, occupancy=bool(occupancy)):
                    result = analyze(previous, target, rad(), True, cohort, occupancy)
                    self.assertEqual(result["peer_comparable"], 0)
                    self.assertIsNone(result["candidate"])
                    self.assertIsNone(result["impact_score"])

    def test_ambiguous_ap_name_invalidates_link_station_and_browser_view(self):
        _, snapshot, _, claim, assoc, bss, detail = fixture()
        raw_inventory, raw_anchors, raw_rf, _ = sample()
        inv, anchors, summaries = inventory(raw_inventory), active_details(raw_anchors), radio_summary(raw_rf)
        initial = replace(snapshot, inventory=inv, active=anchors, radios=summaries)
        original, _ = merge_radios([initial])
        target = next(r for r in original if r.band == "5GHz")
        quality = {target.radio_id: {"observed_at": target.observed_at, "status": "PERSISTENT", "score": 50}}
        # Two actual physical APs on this WLC advertise the same logical name and channel.
        second_ip = "192.0.2.11"
        expanded = replace(initial, inventory=inv + [dict(inv[0], ip=second_ip, mac="02:00:00:00:00:11")],
                           active=anchors + [dict(anchors[0], ip=second_ip)],
                           radios=summaries + [dict(r, ip=second_ip) for r in summaries])
        ambiguous, unresolved = merge_radios([expanded])
        self.assertEqual((len(ambiguous), len(unresolved)), (4, 0))
        clients = LiveClients({"w1": ClientApproval("tag", 900, 1900, True, "ct")}, key=b"fixture",
                              station_approvals={"w1": StationApproval("ct", 900, 1800, "station")})
        claims = {MAC: claim}
        def stats(t, age, sent, retries):
            stamp = AssociationStamp(claim["ap_name"], BSS, SSID, 2, 1, age, t)
            return Reading("show ap debug client-stats", "success", t,
                           {"before": stamp, "after": stamp, "counters": {"sent": sent, "retried": retries}},
                           generation=1, target=MAC)
        self.assertEqual(clients.ingest(detail, "w1", initial, assoc, bss, original, quality, claims, 1010), 1)
        for args in ((1010, 471, 1000, 100), (1040, 501, 1200, 170)):
            self.assertEqual(clients.ingest(stats(*args), "w1", initial, assoc, bss, original, quality, claims, args[0]), 1)
        self.assertEqual(clients.publish(original, [initial], quality, claims, 1040)[0]["station_retry"], 35)
        with self.subTest(path="station"):
            self.assertEqual(clients.ingest(stats(1070, 531, 1400, 240), "w1", expanded, assoc, bss,
                                            ambiguous, quality, claims, 1070), 0)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, mode="diagnostic")
            view = DiagnosticView(store, lambda: 1070)
            published = clients.publish(ambiguous, [expanded], quality, claims, 1070)
            view.publish({}, client_views=published, client_enabled=True)
            with self.subTest(path="published_client_api"):
                self.assertEqual(view.clients_page()["total"], 0)
        # Removing the duplicate cannot revive the old attachment without new detail.
        self.assertEqual(clients.publish(original, [initial], quality, claims, 1071), [])
        refresh = replace(detail, observed_at=1072)
        self.assertEqual(clients.ingest(refresh, "w1", initial, assoc, bss, original, quality, claims, 1072), 1)
        row = clients.publish(original, [initial], quality, claims, 1072)[0]
        self.assertIsNone(row["station_retry"])
        self.assertIsNone(row["candidate"])
