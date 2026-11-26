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
