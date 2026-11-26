"""Selected-detail durable receipt tests: synthetic SSH only."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm.engine import Engine
from awqm.service_runtime import service_lifetime
from threading import Event
from awqm.storage import Store
from awqm.worker import ControllerWorker
from awqm.selected_history import (COMMANDS, POLICY, SelectedHistory, SelectedHistoryFull,
                                   load_selected_history)
from awqm.ap_port_status import COMMAND as PORT
from awqm.arm_rf_summary import COMMAND as RF
from awqm.live_arm_history import COMMAND as ARM
from awqm.live_radio_stats import COMMAND as STATS
from test_live_ap_ports import AP, MAC, KEY, Session, approval, radio_approval
from test_live_rf_summary import fixture as rf_fixture
from test_arm_history import LEGACY
from test_radio_stats import FIXTURE


class SelectedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.now = 1000
        self.key = patch("awqm.worker.fingerprint", return_value=KEY)
        self.key.start()
        self.addCleanup(self.key.stop)

    def worker_engine(self, cmd, enabled=True):
        session = Session()
        session.responses.update({RF: rf_fixture(), ARM: LEGACY, STATS: FIXTURE})
        option = {PORT: "port_approval", RF: "rf_approval",
                  ARM: "arm_approval", STATS: "stats_approval"}[cmd]
        w = ControllerWorker("w1", lambda: session, lambda: self.now, lambda: self.now,
                             mode="standard", field_approval=radio_approval(),
                             live_detail=True, **{option: approval()})
        for _ in range(len(w.schedule.polls)):
            self.assertEqual(w.tick().status, "success")
        store = Store(Path(self.root.name) / ("data-" + option), mode="diagnostic")
        engine = Engine([w], store, clock=lambda: self.now, wall=lambda: self.now,
                        selected_history=enabled)
        self.addCleanup(engine.close)
        engine.next_tick["w1"] = self.now + 1000
        return w, engine, session

    def harvest(self, engine, outcome):
        future = Future()
        future.set_result(outcome)
        engine.inflight["w1"] = future
        return engine.pulse()

    def rows(self, store):
        with store._db(store.root / "core.db") as db:
            return db.execute("SELECT receipt_id,payload FROM selected_detail_receipts").fetchall()

    def test_strict_default_off_policy(self):
        cfg = SimpleNamespace(root=Path(self.root.name))
        (cfg.root / "config").mkdir()
        path = cfg.root / "config/selected-history.json"
        self.assertFalse(load_selected_history(cfg))
        for doc, expected in (({"version": 1, "enabled": False}, False), (POLICY, True)):
            path.write_text(json.dumps(doc), encoding="utf-8")
            self.assertEqual(load_selected_history(cfg), expected)
        for doc in ({**POLICY, "version": True}, {**POLICY, "enabled": 1},
                    {**POLICY, "extra": True}, {"version": 1, "enabled": True}):
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_selected_history(cfg)

    def test_each_actual_worker_path_persists_once_and_keeps_uncertainty(self):
        for cmd in sorted(COMMANDS):
            with self.subTest(cmd=cmd):
                w, engine, session = self.worker_engine(cmd)
                outcome = Engine._step(w)
                reading = outcome[0]
                self.assertEqual((reading.command, reading.status), (cmd, "success"))
                self.assertIsNone(reading.data)
                self.assertIsNone(reading.target)
                self.assertIsNotNone(reading.selected_receipt)
                self.assertIsNone(self.harvest(engine, outcome)["storage_error"])
                rows = self.rows(engine.store)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], reading.selected_receipt.receipt_id)
                data = json.loads(rows[0][1])
                self.assertEqual(data["command"], cmd)
                self.assertEqual(data["received_at"], 1000)
                self.assertEqual(data["started_at"], 1000)
                self.assertEqual(data["generation"], w.generation)
                self.assertEqual(data["observation"]["ap_name"], AP)
                self.assertFalse(data["observation"]["field_validated"])
                self.assertFalse(data["observation"]["sample_time_verified"])
                self.assertIsNone(data["counter_epoch"])
                self.assertIsNone(data["measurement_start"])
                for private in (MAC, KEY, "192.0.2.11"):
                    self.assertNotIn(private, rows[0][1])
                self.assertEqual(self.harvest(engine, outcome)["storage_error"], None)
                self.assertEqual(self.rows(engine.store), rows)
                self.assertNotIn(reading.selected_receipt.receipt_id,
                                 json.dumps(engine.operation_status()))
                self.assertEqual(len([c for c in session.calls if c[0] == cmd]), 1)
                engine.close()

    def test_disabled_and_denied_do_not_capture(self):
        w, engine, _ = self.worker_engine(PORT, enabled=False)
        self.assertIsNone(Engine._step(w)[0].selected_receipt)
        self.assertNotIn("selected_history", engine.operation_status())
        engine.close()
        w, engine, session = self.worker_engine(RF)
        session.responses[RF] = "Permission denied"
        outcome = Engine._step(w)
        self.assertEqual(outcome[0].status, "permission_denied")
        self.assertIsNone(outcome[0].selected_receipt)
        self.harvest(engine, outcome)
        self.assertEqual(self.rows(engine.store), [])

    def test_storage_failure_keeps_pending_then_retries_exact_receipt(self):
        w, engine, _ = self.worker_engine(PORT)
        outcome = Engine._step(w)
        with patch.object(engine.selected_history, "save", side_effect=sqlite3.OperationalError):
            result = self.harvest(engine, outcome)
            self.assertEqual(result["storage_error"], "OperationalError")
            self.assertEqual(engine.operation_status()["selected_history"]["pending"], 1)
            self.assertEqual(engine.inflight, {})
        self.assertEqual(self.rows(engine.store), [])
        engine.recover_storage()
        self.assertIsNone(engine.pulse()["storage_error"])
        self.assertEqual(engine.operation_status()["selected_history"]["pending"], 0)
        self.assertEqual(len(self.rows(engine.store)), 1)

    def test_close_drains_actual_worker_without_another_tick_or_publication(self):
        w, engine, session = self.worker_engine(PORT)
        # Real executor/worker, not a fabricated receipt. Wait for an accepted
        # result but intentionally do not give pulse a chance to harvest it.
        future = engine.pool.submit(Engine._step, w)
        engine.inflight["w1"] = future
        outcome = future.result(timeout=10)
        receipt = outcome[0].selected_receipt
        self.assertIsNotNone(receipt)
        self.assertEqual(self.rows(engine.store), [])
        before = list(session.calls)
        with patch("awqm.engine.shutil.disk_usage",
                   return_value=SimpleNamespace(free=80*10**9, total=100*10**9)):
            engine.close()
        self.assertEqual(self.rows(engine.store), [(receipt.receipt_id, receipt.payload)])
        self.assertEqual(session.calls, before)
        self.assertEqual(engine.selected_pending, {})
        self.assertEqual(engine.inflight, {})
        self.assertEqual(engine.published_radios, ())
        self.assertFalse(w.retain_selected_history)
        self.assertIsNone(w.session)
        engine.close()  # Idempotent shutdown.
        self.assertEqual(len(self.rows(engine.store)), 1)

    def test_close_cancelled_future_and_disabled_archive_do_not_write(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                # Unique store directories for the two cases.
                cmd = PORT if enabled else RF
                w, engine, _ = self.worker_engine(cmd, enabled=enabled)
                future = Future()
                self.assertTrue(future.cancel())
                engine.inflight["w1"] = future
                with patch("awqm.engine.shutil.disk_usage") as usage:
                    engine.close()
                    usage.assert_not_called()
                self.assertFalse(w.retain_selected_history)
                if enabled:
                    self.assertEqual(self.rows(engine.store), [])

    def test_close_failure_is_not_clean_service_stop_and_preserves_pending(self):
        for reason, cmd in (("disk", PORT), ("sqlite", RF), ("prior", ARM)):
            with self.subTest(reason=reason):
                w, engine, _ = self.worker_engine(cmd)
                outcome = Engine._step(w)
                receipt = outcome[0].selected_receipt
                self.assertIsNotNone(receipt)
                if reason == "prior":
                    # Existing failed commit is not an implicit recovery.
                    with patch.object(engine.selected_history, "save",
                                      side_effect=sqlite3.OperationalError):
                        self.harvest(engine, outcome)
                else:
                    future = Future()
                    future.set_result(outcome)
                    engine.inflight["w1"] = future
                space = SimpleNamespace(free=(4 if reason == "disk" else 80)*10**9,
                                        total=100*10**9)
                status = Path(self.root.name) / ("status-" + reason + ".json")
                stop = Event()
                with patch("awqm.engine.shutil.disk_usage", return_value=space), \
                        patch.object(engine.selected_history, "save",
                                     side_effect=sqlite3.OperationalError) as save:
                    with self.assertRaisesRegex(RuntimeError, "selected history shutdown incomplete"):
                        with service_lifetime(stop, status, lambda: 1000) as resources:
                            resources.callback(engine.close)
                    if reason == "sqlite":
                        save.assert_called_once_with(receipt)
                    else:
                        save.assert_not_called()
                self.assertEqual(json.loads(status.read_text())["state"], "failed")
                self.assertTrue(stop.is_set())
                self.assertEqual(engine.selected_pending, {"w1": receipt})
                self.assertEqual(self.rows(engine.store), [])
                self.assertEqual(engine.error, "disk_protection" if reason == "disk"
                                 else "OperationalError")
                self.assertFalse(w.retain_selected_history)
                self.assertIsNone(w.session)

    def test_close_unavailable_worker_result_fails_without_leaking_exception(self):
        w, engine, _ = self.worker_engine(PORT)
        future = Future()
        future.set_exception(RuntimeError("private diagnostic exception"))
        engine.inflight["w1"] = future
        with self.assertRaisesRegex(RuntimeError, "^selected history shutdown incomplete$"):
            engine.close()
        self.assertEqual(engine.error, "RuntimeError")
        self.assertEqual(self.rows(engine.store), [])
        self.assertFalse(w.retain_selected_history)
        self.assertIsNone(w.session)

    def test_private_receipt_query_pages_actual_archived_worker_results(self):
        w, engine, session = self.worker_engine(PORT)
        outcome = Engine._step(w)
        original = outcome[0].selected_receipt
        self.assertIsNotNone(original)
        self.harvest(engine, outcome)
        ledger = engine.selected_history
        # Distinct synthetic accepted receipt IDs at the same second must
        # remain separately pageable; exact replays must not add rows.
        other = replace(original, receipt_id="f"*32 if original.receipt_id != "f"*32 else "0"*32)
        self.assertTrue(ledger.save(other))
        self.assertFalse(ledger.save(original))
        earlier = json.loads(original.payload)
        earlier["received_at"] = 999
        earlier["started_at"] = 999
        earlier["observation"]["received_at"] = 999
        excluded = replace(original, receipt_id="e"*32, received_at=999,
                           payload=json.dumps(earlier))
        ledger.save(excluded)
        before = list(session.calls)
        ordered = tuple(sorted((original, other), key=lambda r: r.receipt_id))
        first, cursor = ledger.query("w1", PORT, 1000, 1001, limit=1)
        self.assertEqual(first, ordered[:1])
        self.assertEqual(cursor, (first[0].received_at, first[0].receipt_id))
        second, end = ledger.query("w1", PORT, 1000, 1001, limit=1, after=cursor)
        self.assertEqual(second, ordered[1:])
        self.assertIsNone(end)
        self.assertEqual(ledger.query("w1", PORT, 1000, 1001), (ordered, None))
        for controller, command, start, end in (
            ("w2", PORT, 1000, 1001), ("w1", RF, 1000, 1001),
            ("w1", PORT, 1001, 1002),
        ):
            self.assertEqual(ledger.query(controller, command, start, end), ((), None))
        self.assertEqual(ledger.query("w1", PORT, 999, 1000), ((excluded,), None))
        self.assertEqual(session.calls, before)
        self.assertEqual(len(self.rows(engine.store)), 3)
        self.assertNotIn(original.receipt_id, json.dumps(engine.operation_status()))
        backup = engine.store.backup(Path(self.root.name) / "query-backup")
        restored = SelectedHistory(Store(backup, mode="diagnostic"))
        self.assertEqual(restored.query("w1", PORT, 1000, 1001), (ordered, None))

    def test_admin_read_model_summaries_of_each_real_worker_receipt(self):
        from awqm.diagnostic_view import DiagnosticView
        from awqm.selected_history import COMMAND_IDS
        for ident, cmd in COMMAND_IDS.items():
            with self.subTest(command=ident):
                w, engine, session = self.worker_engine(cmd)
                outcome = Engine._step(w)
                original = outcome[0].selected_receipt
                self.assertIsNotNone(original)
                self.harvest(engine, outcome)
                view = DiagnosticView(engine.store, lambda: self.now, key=b"fixture",
                                      selected_history=engine.selected_history,
                                      selected_controllers=("w1",))
                before = list(session.calls)
                page = view.selected_history_page("w1", ident, 1000, 1001)
                self.assertEqual(len(page["rows"]), 1)
                row = page["rows"][0]
                data = json.loads(original.payload)
                self.assertEqual(row["ap_token"], view.token("selected-history:" + data["ap_binding"]))
                self.assertEqual(row["received_at"], 1000)
                self.assertFalse(row["field_validated"])
                self.assertFalse(row["sample_time_verified"])
                self.assertFalse(row["completeness_verified"])
                self.assertIsNone(row["counter_epoch"])
                self.assertEqual(page["time_basis"], "receipt_time")
                self.assertFalse(page["snapshot"])
                self.assertIsNone(page["next_cursor"])
                self.assertTrue(row["summary"])
                if ident == "D07":
                    self.assertEqual(row["summary"]["retained_port_rows"], len(data["observation"]["ports"]))
                    self.assertEqual(row["port_detail"], [
                        {"port":0,"admin":"enabled","oper":"up","speed_mbps":2500,
                         "duplex":"full","tx_packets":"0","tx_bytes":"18446744073709551615",
                         "rx_packets":None,"rx_bytes":"300"}])
                else:
                    self.assertNotIn("port_detail", row)
                if ident == "D03":
                    self.assertEqual(row["radio_detail"], {
                        "reported_counters": {"radio_resets":"0", "tx_timeouts":"4", "rx_crc_errors":"0"},
                        "reported_busy_windows": {"channel_busy_1s_percent":50,
                            "channel_busy_4s_percent":42.5, "channel_busy_64s_percent":0}})
                else:
                    self.assertNotIn("radio_detail", row)
                if ident == "D04":
                    self.assertEqual(row["rf_detail"], [
                        {"channel":36, "reported_band":None, "noise_raw":"92",
                         "split_scan_reported":False, "retry_raw":"18446744073709551615",
                         "phy_error_raw":"0", "mac_error_raw":"0",
                         "utilization":{"channel_utilization":8,"rx":3,"tx":2,
                             "extension_channel_utilization":1,"quality":95}},
                        {"channel":44, "reported_band":None, "noise_raw":None,
                         "split_scan_reported":None, "retry_raw":None,
                         "phy_error_raw":"0", "mac_error_raw":None, "utilization":None}])
                else:
                    self.assertNotIn("rf_detail", row)
                if ident == "D08":
                    self.assertEqual(row["summary"], {"retained_event_rows":4,
                        "reported_event_rows":4, "retained_interfaces":2})
                    self.assertEqual(row["arm_detail"], [
                        {"interface":"wifi0","phy_type":None,"table_present":True,"reported_event_rows":3,
                         "events":[{"reason_code":"I","old_channel":"36+","new_channel":"44-",
                                    "old_power":"18","new_power":"18","reported_age_seconds":62},
                                   {"reason_code":"P-","old_channel":"44-","new_channel":"44-",
                                    "old_power":"18","new_power":"15","reported_age_seconds":0},
                                   {"reason_code":"OFF(R)","old_channel":"44-","new_channel":"0",
                                    "old_power":None,"new_power":"0","reported_age_seconds":93784}]},
                        {"interface":"wifi1","phy_type":None,"table_present":True,"reported_event_rows":1,
                         "events":[{"reason_code":"P+","old_channel":"6","new_channel":"6",
                                    "old_power":"9","new_power":"12","reported_age_seconds":9}]}])
                else:
                    self.assertNotIn("arm_detail", row)
                raw = json.dumps(page)
                for private in (AP, MAC, KEY, data["ap_binding"], original.receipt_id,
                                "radio_approval", "connection", "observation"):
                    self.assertNotIn(private, raw)
                self.assertEqual(before, session.calls)
                with self.assertRaises(ValueError):
                    view.selected_history_page("unconfigured", ident, 1000, 1001)
                with self.assertRaises(ValueError):
                    view.selected_history_page("w1", cmd, 1000, 1001)
                disabled = DiagnosticView(engine.store, lambda: self.now)
                self.assertFalse(disabled.selected_history_page("w1", ident, 1000, 1001)["enabled"])
                # Malformed archived evidence fails closed; no raw payload or
                # fabricated empty/normal receipt. An unknown private field
                # is not copied even if stored in an otherwise valid payload.
                data["observation"]["extra_private"] = "DO-NOT-DISCLOSE"
                altered = replace(original, receipt_id="f"*32 if original.receipt_id != "f"*32 else "0"*32,
                                  payload=json.dumps(data))
                engine.selected_history.save(altered)
                page = view.selected_history_page("w1", ident, 1000, 1001, limit=1)
                cursor = page["next_cursor"]
                self.assertIsNotNone(cursor)
                tail = view.selected_history_page("w1", ident, 1000, 1001,
                            limit=1, after=(cursor["received_at"],cursor["receipt_id"]))
                self.assertIsNone(tail["next_cursor"])
                self.assertNotIn("DO-NOT-DISCLOSE",json.dumps((page,tail)))
                data["ap_binding"] = "invalid"
                invalid = replace(original, receipt_id="1"*32 if original.receipt_id != "1"*32 else "2"*32,
                                  payload=json.dumps(data))
                engine.selected_history.save(invalid)
                with self.assertRaisesRegex(RuntimeError,"selected history record unavailable"):
                    view.selected_history_page("w1", ident, 1000, 1001)
                engine.close()

    def test_d03_history_preserves_uint64_null_and_rejects_corrupt_values(self):
        from awqm.selected_history import receipt_summary
        w, engine, session = self.worker_engine(STATS)
        outcome = Engine._step(w)
        original = outcome[0].selected_receipt
        self.assertIsNotNone(original)
        self.harvest(engine, outcome)
        data = json.loads(original.payload)
        obs = data["observation"]
        obs["counters"]["radio_resets"] = "18446744073709551615"
        obs["counters"]["tx_timeouts"] = None
        obs["counters"]["private_counter"] = "DO-NOT-DISCLOSE"
        obs["radio_percentages"]["channel_busy_64s_percent"] = None
        obs["radio_percentages"]["private_busy"] = "DO-NOT-DISCLOSE"
        def project():
            return receipt_summary(replace(original, payload=json.dumps(data)), lambda _: "masked")
        detail = project()["radio_detail"]
        self.assertEqual(detail["reported_counters"], {
            "radio_resets":"18446744073709551615", "tx_timeouts":None, "rx_crc_errors":"0"})
        self.assertEqual(detail["reported_busy_windows"], {
            "channel_busy_1s_percent":50, "channel_busy_4s_percent":42.5,
            "channel_busy_64s_percent":None})
        self.assertNotIn("DO-NOT-DISCLOSE", json.dumps(detail))
        for group, key, invalid in (
            ("counters","radio_resets",["18446744073709551616", 0, True, "-1", "1e4", "1"*21]),
            ("radio_percentages","channel_busy_1s_percent",[True, "50", -1, 101, float("nan"), float("inf")]),
        ):
            original_value = obs[group][key]
            for bad in invalid:
                with self.subTest(group=group,bad=bad):
                    obs[group][key] = bad
                    with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
                        project()
            obs[group][key] = original_value
        del obs["counters"]["rx_crc_errors"]
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        engine.close()

    def test_d04_history_preserves_reported_values_and_rejects_corruption(self):
        from awqm.selected_history import receipt_summary
        w, engine, _ = self.worker_engine(RF)
        outcome = Engine._step(w)
        original = outcome[0].selected_receipt
        self.assertIsNotNone(original)
        self.harvest(engine, outcome)
        data = json.loads(original.payload)
        obs = data["observation"]
        row = obs["channels"][0]
        row["private_key"] = "DO-NOT-DISCLOSE"
        row["utilization"]["private_key"] = "DO-NOT-DISCLOSE"
        row["noise_raw"] = "-92*"
        row["split_scan_reported"] = True

        def project():
            return receipt_summary(replace(original, payload=json.dumps(data)), lambda _: "masked")

        detail = project()["rf_detail"]
        self.assertEqual(detail[0]["noise_raw"], "-92*")
        self.assertIs(detail[0]["split_scan_reported"], True)
        self.assertNotIn("DO-NOT-DISCLOSE", json.dumps(detail))
        self.assertNotIn("interference_index", detail[1])
        for target, key, invalid in (
            (row, "channel", [True, 0, 234, "36"]),
            (row, "reported_band", ["5GHz", []]),
            (row, "retry_raw", ["18446744073709551616", 0, True, "-1"]),
            (row, "noise_raw", [92, "N/A", "private-name"]),
            (row, "split_scan_reported", [False, 1, None]),
            (row["utilization"], "quality", [True, "95", -1, 101, float("nan")]),
        ):
            previous = target[key]
            for value in invalid:
                with self.subTest(key=key, value=value):
                    target[key] = value
                    with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
                        project()
            target[key] = previous
        obs["channels"].append(dict(row))
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        obs["channels"].pop()
        del row["mac_error_raw"]
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        engine.close()

    def test_d07_history_preserves_reported_values_and_rejects_corruption(self):
        from awqm.selected_history import receipt_summary
        w, engine, _ = self.worker_engine(PORT)
        outcome = Engine._step(w)
        original = outcome[0].selected_receipt
        self.assertIsNotNone(original)
        self.harvest(engine, outcome)
        data = json.loads(original.payload)
        obs = data["observation"]
        row = obs["ports"][0]
        row["private_key"] = "DO-NOT-DISCLOSE"
        row["stp"] = "DO-NOT-DISCLOSE"
        row["port_mac"] = MAC
        second = dict(row, port=1, admin="disabled", oper="down",
                      speed_mbps=None, duplex=None, tx_bytes=None)
        obs["ports"].append(second)

        def project():
            return receipt_summary(replace(original, payload=json.dumps(data)), lambda _: "masked")

        page = project()
        self.assertEqual(page["summary"], {"retained_port_rows":2})
        detail = page["port_detail"]
        self.assertEqual(detail[0]["tx_bytes"], "18446744073709551615")
        self.assertEqual(detail[0]["tx_packets"], "0")
        self.assertIsNone(detail[0]["rx_packets"])
        self.assertEqual(detail[1]["oper"], "down")
        self.assertIsNone(detail[1]["speed_mbps"])
        self.assertIsNone(detail[1]["duplex"])
        self.assertIsNone(detail[1]["tx_bytes"])
        for private in ("DO-NOT-DISCLOSE", MAC, "stp", "port_mac"):
            self.assertNotIn(private, json.dumps(detail))
        for target, key, invalid in (
            (row, "port", [True, -1, 1000, 1, "0"]),
            (row, "admin", ["unknown", None, []]),
            (row, "oper", ["normal", True, None]),
            (row, "speed_mbps", [True, 0, 100001, "2500", 1.5]),
            (row, "duplex", ["auto", False, []]),
            (row, "tx_bytes", ["18446744073709551616", 0, True, "-1", "1e4"]),
            (obs, "physical_uplink_verified", [True, 0, None]),
            (obs, "row_count_verified", [True, 0, None]),
        ):
            previous = target[key]
            for value in invalid:
                with self.subTest(key=key, value=value):
                    target[key] = value
                    with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
                        project()
            target[key] = previous
        valid_ports = obs["ports"]
        for invalid in ([], [dict(row, port=i) for i in range(33)], [row, None]):
            obs["ports"] = invalid
            with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
                project()
        obs["ports"] = valid_ports
        del row["rx_bytes"]
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        engine.close()

    def test_d08_history_bounds_privacy_and_header_only(self):
        from awqm.selected_history import receipt_summary
        w, engine, _ = self.worker_engine(ARM)
        outcome = Engine._step(w)
        original = outcome[0].selected_receipt
        self.assertIsNotNone(original)
        self.harvest(engine, outcome)
        data = json.loads(original.payload)
        obs = data["observation"]
        first, second = obs["interfaces"]
        event = first["events"][0]
        event["private"] = "DO-NOT-DISCLOSE"
        first["private"] = MAC

        def project():
            return receipt_summary(replace(original, payload=json.dumps(data)), lambda _: "masked")

        self.assertNotIn("DO-NOT-DISCLOSE", json.dumps(project()))
        self.assertNotIn(MAC, json.dumps(project()))
        for target, key, invalid in (
            (first, "interface", ["wifi3", "wifi1", None, []]),
            (first, "phy_type", ["8GHz", "5GHz", False]),
            (first, "table_present", [False, 1, None]),
            (first, "event_count", [True, -1, 4, 4097]),
            (event, "reason_code", ["private", None, []]),
            (event, "old_channel", ["234+", "N/A", 36, "36++"]),
            (event, "old_power", ["nan", "1000", 18, "18.0001"]),
            (event, "reported_age_seconds", [True, -1, 8640000000, "62", 1.5]),
            (obs, "event_time_verified", [True, 0, None]),
            (obs, "radio_binding_verified", [True, 0, None]),
            (obs, "completeness_verified", [True, 0, None]),
            (obs, "target_verified", [True, 0, None]),
            (obs, "interface_count", [True, 0, 3]),
            (obs, "event_count", [True, 0, 5]),
            (obs, "truncated", [True, 0, None]),
        ):
            previous = target[key]
            for value in invalid:
                with self.subTest(key=key, value=value):
                    target[key] = value
                    with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
                        project()
            target[key] = previous
        del event["new_channel"]
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        event["new_channel"] = "44-"
        # Repeated identical rows are reported evidence, not unique events.
        first["events"] = [dict(event) for _ in range(64)]
        first["event_count"] = 65
        second["events"] = []
        obs["event_count"] = 66
        obs["truncated"] = True
        self.assertEqual(project()["summary"], {"retained_event_rows":64,
                         "reported_event_rows":66,"retained_interfaces":2})
        self.assertEqual(project()["arm_detail"][1]["reported_event_rows"], 1)
        first["events"].append(dict(event))
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        # A valid PHY-only header is retained as unknown completeness.
        obs.update(interfaces=[{"interface":"wifi0","phy_type":"5GHz",
                   "table_present":False,"events":[],"event_count":0}],
                   interface_count=1,event_count=0,truncated=False)
        page = project()
        self.assertEqual(page["arm_detail"], [{"interface":"wifi0","phy_type":"5GHz",
                         "table_present":False,"reported_event_rows":0,"events":[]}])
        self.assertFalse(page["completeness_verified"])
        self.assertIsNone(page["measurement_start"])
        engine.close()

    def test_admin_default_page_bounds_and_same_second_cursor(self):
        from awqm.diagnostic_view import DiagnosticView
        w, engine, session = self.worker_engine(PORT)
        outcome = Engine._step(w)
        original = outcome[0].selected_receipt
        self.assertIsNotNone(original)
        self.harvest(engine, outcome)
        ids = {original.receipt_id}
        for n in range(1, 22):
            ident = f"{n:032x}"
            if ident not in ids and len(ids) < 21:
                engine.selected_history.save(replace(original, receipt_id=ident))
                ids.add(ident)
        self.assertEqual(len(ids), 21)
        view = DiagnosticView(engine.store, lambda: self.now, key=b"fixture",
                              selected_history=engine.selected_history,
                              selected_controllers=("w1",))
        before = list(session.calls)
        first = view.selected_history_page("w1", "D07", 1000, 1001)
        self.assertEqual(len(first["rows"]), 20)
        self.assertEqual(first["next_cursor"], {"received_at": 1000,
                                               "receipt_id": sorted(ids)[19]})
        cursor = first["next_cursor"]
        tail = view.selected_history_page("w1", "D07", 1000, 1001,
                            after=(cursor["received_at"], cursor["receipt_id"]))
        self.assertEqual(len(tail["rows"]), 1)
        self.assertIsNone(tail["next_cursor"])
        self.assertEqual(tail["rows"][0], first["rows"][0])
        self.assertFalse(first["snapshot"])
        self.assertEqual(view.selected_history_page("w1", "D04", 1000, 1001)["rows"], [])
        self.assertEqual(session.calls, before)

    def test_private_receipt_query_bounds_and_cursor(self):
        _, engine, _ = self.worker_engine(PORT)
        ledger = engine.selected_history
        for args, kw in [
            (("", PORT, 0, 1), {}), (("w"*129, PORT, 0, 1), {}),
            (("w1", "arbitrary command", 0, 1), {}),
            (("w1", [], 0, 1), {}), (("w1", PORT, True, 2), {}),
            (("w1", PORT, 0, 30*86400+1), {}),
            (("w1", PORT, -1, 1), {}), (("w1", PORT, 1, 1), {}),
            (("w1", PORT, 0, 10**20), {}),
            (("w1", PORT, 0, 1), {"limit": True}),
            (("w1", PORT, 0, 1), {"limit": 21}),
            (("w1", PORT, 0, 1), {"limit": 0}),
            (("w1", PORT, 0, 1), {"after": (1, "a"*32)}),
            (("w1", PORT, 0, 1), {"after": (0, "bad")}),
            (("w1", PORT, 0, 1), {"after": (False, "a"*32)}),
            (("w1", PORT, 0, 1), {"after": [0, "a"*32]}),
        ]:
            with self.subTest(args=args, kw=kw), self.assertRaises(ValueError):
                ledger.query(*args, **kw)
        self.assertEqual(ledger.query("w1", PORT, 0, 30*86400), ((), None))

    def test_capacity_replay_conflict_and_core_backup(self):
        w, engine, _ = self.worker_engine(PORT)
        receipt = Engine._step(w)[0].selected_receipt
        ledger = engine.selected_history
        self.assertTrue(ledger.save(receipt))
        with patch("awqm.selected_history.MAX_ROWS", 1):
            self.assertFalse(ledger.save(receipt))
            with self.assertRaises(SelectedHistoryFull):
                ledger.save(replace(receipt, receipt_id="different"))
        with self.assertRaises(ValueError):
            ledger.save(replace(receipt, payload=receipt.payload + " "))
        with patch("awqm.selected_history.MAX_BYTES", 1):
            with self.assertRaises(SelectedHistoryFull):
                ledger.save(replace(receipt, receipt_id="other"))
        before = self.rows(engine.store)
        backup = engine.store.backup(Path(self.root.name) / "backup")
        restored = Store(backup, mode="diagnostic")
        self.assertEqual(self.rows(restored), before)
        self.assertEqual(self.rows(engine.store), before)
        with self.assertRaises(ValueError):
            SelectedHistory(Store(Path(self.root.name) / "demo", mode="demo"))


if __name__ == "__main__":
    unittest.main()
