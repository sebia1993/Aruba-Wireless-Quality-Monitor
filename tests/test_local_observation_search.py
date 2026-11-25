import unittest
from tempfile import TemporaryDirectory

from awqm.device_profiles import DeviceProfile, Profiles
from awqm.device_web import DeviceWeb


class ObservedMonitor:
    def __init__(self):
        self.states = {}
        self.calls = []

    def state(self, slot):
        self.calls.append(slot)
        return self.states.get(slot, {})

    def close(self):
        pass


class LocalObservationSearchTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.monitor = ObservedMonitor()
        self.web = DeviceWeb(Profiles(root.name), self.monitor)

    def observe(self, number, rows, *, stale=False, running=True, table="AP"):
        id = f"WLC-{number}"
        columns = ("AP 이름", "관리 IP") if table == "AP" else ("AP 이름", "SSID", "BSSID")
        self.monitor.states[id] = {
            "profile": DeviceProfile(id, f"192.0.2.{number}", "readonly"),
            "running": running, "stale": stale,
            "tables": {table: (columns, rows)},
        }

    def search(self, query, table="AP", page=0):
        return self.web.dispatch({"action":"search", "table":table, "query":query, "page":page})["table"]

    def test_duplicate_claims_keep_source_and_stale_status(self):
        self.observe(1, [("AP-001", "192.0.2.10"), ("AP-002", "192.0.2.11")])
        self.observe(2, [("AP-001", "192.0.2.10")], stale=True)
        self.observe(3, [("AP-001", "192.0.2.10")], running=False)
        table = self.search(" ap-001 ")
        self.assertEqual(table["scope"], "all")
        self.assertEqual(table["total"], 3)
        self.assertEqual([r[0] for r in table["rows"]], ["WLC-1", "WLC-2", "WLC-3"])
        self.assertEqual([r[2] for r in table["rows"]], ["수집 중", "갱신 지연 / 이전 관측", "수집 중 아님"])
        self.assertEqual(self.monitor.calls, ["WLC-1", "WLC-2", "WLC-3", "WLC-4"])
        self.assertEqual(self.search("192.0.2.10")["total"], 3)
        self.assertEqual(self.search("WLC-2")["total"], 1)

    def test_bssid_normalization_and_ssid(self):
        self.observe(1, [("AP-001", "Staff", "02:00:00:00:00:01")], table="SSID / BSSID")
        for query in ("020000000001", "02-00-00-00-00-01", "0200.0000.0001", "staff"):
            with self.subTest(query=query):
                self.assertEqual(self.search(query, "SSID / BSSID")["total"], 1)
        self.assertEqual(self.search("readonly", "SSID / BSSID")["total"], 0)

    def test_pagination_preserves_total_and_single_slot_read(self):
        self.observe(1, [(f"AP-{i}", "192.0.2.10") for i in range(101)])
        self.observe(2, [("AP-101", "192.0.2.11")])
        a, b = self.search("AP-"), self.search("AP-", page=1)
        self.assertEqual((a["total"], len(a["rows"]), b["total"], len(b["rows"])), (102, 100, 102, 2))
        single = self.web.dispatch({"action":"read","id":"WLC-2","table":"AP","page":0})["table"]
        self.assertEqual(single["scope"], "slot")
        self.assertEqual(single["total"], 1)
        self.assertEqual(single["rows"][0], ("AP-101", "192.0.2.11"))

    def test_reject_invalid_search_inputs(self):
        for patch in ({"query":""}, {"query":" "}, {"query":"x"*129}, {"query":42},
                      {"query":"ap\nother"}, {"table":"명령 결과"}, {"table":[]},
                      {"page":-1}, {"page":True}, {"extra":"value"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.web.dispatch({"action":"search","table":"AP","query":"ap","page":0, **patch})
        self.assertEqual(self.monitor.calls, [])
