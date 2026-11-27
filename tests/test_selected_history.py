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
                                   load_selected_history, load_selected_history_policy)
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
        # Synthetic capacity is controlled; explicit critical-disk cases
        # override this fixture rather than depending on the runner's volume.
        space = patch("awqm.engine.shutil.disk_usage",
                      return_value=SimpleNamespace(free=80*10**9, total=100*10**9))
        space.start()
        self.addCleanup(space.stop)

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

    def test_selected_disclosure_policy_is_optional_and_strictly_bounded(self):
        cfg = SimpleNamespace(root=Path(self.root.name))
        (cfg.root / "config").mkdir()
        path = cfg.root / "config/selected-history.json"
        self.assertEqual(load_selected_history_policy(cfg), (False, 30))
        for doc, expected in (({"version": 1, "enabled": False}, (False, 30)),
                              (POLICY, (True, 30)),
                              ({**POLICY, "disclosure_days": 7}, (True, 7)),
                              ({**POLICY, "disclosure_days": 1}, (True, 1))):
            path.write_text(json.dumps(doc), encoding="utf-8")
            self.assertEqual(load_selected_history_policy(cfg), expected)
            self.assertIs(load_selected_history(cfg), expected[0])
        for value in (0, -1, 31, True, False, 7.0, "7", None, [], {}):
            with self.subTest(value=value):
                path.write_text(json.dumps({**POLICY, "disclosure_days": value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_selected_history_policy(cfg)
        for doc in ({"version": 1, "enabled": False, "disclosure_days": 7},
                    {"version": 1, "enabled": True, "disclosure_days": 7},
                    {**POLICY, "disclosure_days": 7, "other": 1}):
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_selected_history_policy(cfg)

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

    @staticmethod
    def selected_path(worker, command):
        return {PORT: worker.ap_ports, RF: worker.rf_summary,
                ARM: worker.arm_history, STATS: worker.radio_stats}[command]

    def test_selected_approval_change_during_cli_withholds_receipt(self):
        for cmd in sorted(COMMANDS):
            with self.subTest(command=cmd):
                w, engine, session = self.worker_engine(cmd)
                path = self.selected_path(w, cmd)
                original_run = session.run
                original_approval = path.approval

                def renew_during_cli(name, *args, **kwargs):
                    raw = original_run(name, *args, **kwargs)
                    if name == cmd:
                        # Same Radio tag, AP identity and still valid now;
                        # only the selected approval lifetime changes.
                        path.approval = replace(original_approval, valid_until=1800)
                    return raw

                with patch.object(session, "run", side_effect=renew_during_cli):
                    outcome = Engine._step(w)
                self.assertEqual(outcome[0].status, "withheld")
                self.assertIsNone(outcome[0].selected_receipt)
                self.assertEqual(path.project(w, KEY, self.now)["rows"], [])
                self.assertIsNone(self.harvest(engine, outcome)["storage_error"])
                self.assertEqual(self.rows(engine.store), [])
                self.assertEqual(len([c for c in session.calls if c[0] == cmd]), 1)
                engine.close()

    def test_selected_approval_renewal_withdraws_live_not_archived_evidence(self):
        for cmd in sorted(COMMANDS):
            with self.subTest(command=cmd):
                w, engine, session = self.worker_engine(cmd)
                path = self.selected_path(w, cmd)
                outcome = Engine._step(w)
                self.assertEqual(outcome[0].status, "success")
                self.assertIsNone(self.harvest(engine, outcome)["storage_error"])
                old_rows = self.rows(engine.store)
                self.assertEqual(len(old_rows), 1)
                self.assertTrue(path.project(w, KEY, self.now)["rows"])
                original_radio = w.field_approval
                # Still valid for the selected approval and current cached
                # evidence, but not identical to the approval at collection.
                w.field_approval = replace(original_radio, valid_until=1950)
                self.assertEqual(path.project(w, KEY, self.now)["rows"], [])
                w.field_approval = original_radio
                self.assertEqual(path.project(w, KEY, self.now)["rows"], [])
                self.assertEqual(self.rows(engine.store), old_rows)
                receipt = json.loads(old_rows[0][1])
                self.assertIsNone(receipt["counter_epoch"])
                self.assertIsNone(receipt["measurement_start"])
                self.assertFalse(receipt["observation"]["field_validated"])
                self.assertEqual(len([c for c in session.calls if c[0] == cmd]), 1)
                engine.close()

    def test_selected_target_withdrawal_cannot_resurrect_cached_result(self):
        for cmd in sorted(COMMANDS):
            with self.subTest(command=cmd):
                w, engine, session = self.worker_engine(cmd)
                path = self.selected_path(w, cmd)
                outcome = Engine._step(w)
                self.assertEqual(outcome[0].status, "success")
                self.assertIsNone(self.harvest(engine, outcome)["storage_error"])
                saved = self.rows(engine.store)
                original = path.approval
                self.assertTrue(path.project(w, KEY, self.now)["rows"])
                # A different valid target scope, not a cache-only filter.
                path.approval = replace(original,
                                        targets=(("OTHER-AP", "f" * 64),))
                self.assertEqual(path.project(w, KEY, self.now)["rows"], [])
                self.assertEqual(path.records, {})
                if cmd == ARM:
                    self.assertEqual(path.previous, {})
                    self.assertEqual(path.changed, {})
                path.approval = original
                self.assertEqual(path.project(w, KEY, self.now)["rows"], [])
                self.assertEqual(self.rows(engine.store), saved)
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

    def test_staged_receipt_recovers_on_engine_restart_without_ssh(self):
        w, engine, session = self.worker_engine(PORT)
        outcome = Engine._step(w)
        receipt = outcome[0].selected_receipt
        with patch.object(engine.selected_history, "save", side_effect=sqlite3.OperationalError):
            self.assertEqual(self.harvest(engine, outcome)["storage_error"], "OperationalError")
        self.assertEqual(engine.selected_history.pending(), {"w1": receipt})
        self.assertEqual(self.rows(engine.store), [])
        before = list(session.calls)
        with self.assertRaisesRegex(RuntimeError, "selected history shutdown incomplete"):
            engine.close()
        # A new Store and Engine (same protected data directory), no old
        # in-memory pending map or fresh collection.
        reopened = Store(engine.store.root, mode="diagnostic")
        restarted = Engine([w], reopened, clock=lambda: self.now, wall=lambda: self.now,
                           selected_history=True)
        self.addCleanup(restarted.close)
        restarted.paused = True
        self.assertEqual(restarted.selected_pending, {"w1": receipt})
        self.assertIsNone(restarted.pulse()["storage_error"])
        self.assertEqual(self.rows(reopened), [(receipt.receipt_id, receipt.payload)])
        self.assertEqual(restarted.selected_history.pending(), {})
        self.assertEqual(restarted.selected_pending, {})
        self.assertEqual(session.calls, before)
        self.assertEqual(restarted.published_radios, ())

    def test_early_handoff_stages_before_slow_snapshot_finishes(self):
        w, engine, session = self.worker_engine(PORT)
        entered, release = Event(), Event()
        snapshot = w.snapshot

        def delayed_snapshot():
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test snapshot release timed out")
            return snapshot()

        with patch.object(w, "snapshot", side_effect=delayed_snapshot):
            engine.next_tick["w1"] = self.now
            engine.pulse()  # Use the production executor dispatch path.
            future = engine.inflight["w1"]
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(future.done())
                self.assertEqual(engine.operation_status()["selected_history"]["pending"], 1)
                with patch.object(engine.selected_history, "save",
                                  side_effect=sqlite3.OperationalError):
                    self.assertEqual(engine.pulse()["storage_error"], "OperationalError")
                staged = engine.selected_history.pending()
                self.assertEqual(set(staged), {"w1"})
                self.assertEqual(engine.selected_pending, staged)
                self.assertEqual(engine.selected_handoff, {})
                self.assertEqual(self.rows(engine.store), [])
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(timeout=10)
            self.assertIsNone(result[0].selected_receipt)
        engine.paused = True
        engine.recover_storage()
        self.assertIsNone(engine.pulse()["storage_error"])
        receipt = staged["w1"]
        self.assertEqual(self.rows(engine.store), [(receipt.receipt_id, receipt.payload)])
        self.assertEqual(engine.selected_history.pending(), {})
        self.assertEqual(engine.selected_pending, {})
        self.assertEqual(len([c for c in session.calls if c[0] == PORT]), 1)

    def test_snapshot_failure_does_not_discard_accepted_receipt(self):
        w, engine, session = self.worker_engine(PORT)
        engine.next_tick["w1"] = self.now
        with patch.object(w, "snapshot", side_effect=RuntimeError("private snapshot detail")):
            engine.pulse()
            future = engine.inflight["w1"]
            with self.assertRaises(RuntimeError):
                future.result(timeout=10)
            engine.paused = True
            result = engine.pulse()
        self.assertIsNone(result["storage_error"])
        self.assertEqual(result["controllers"]["w1"]["status"], "worker_error")
        self.assertNotIn("private snapshot detail", json.dumps(result))
        self.assertIsNone(engine.snapshots["w1"])
        self.assertEqual(engine.published_radios, ())
        self.assertEqual(len(self.rows(engine.store)), 1)
        self.assertEqual(engine.selected_history.pending(), {})
        self.assertEqual(engine.selected_handoff, {})
        self.assertEqual(len([c for c in session.calls if c[0] == PORT]), 1)

    def test_close_after_snapshot_failure_still_archives_receipt(self):
        w, engine, session = self.worker_engine(PORT)
        with patch.object(w, "snapshot", side_effect=RuntimeError("private snapshot detail")):
            future = engine.pool.submit(Engine._step, w, False, engine._offer_selected)
            engine.inflight["w1"] = future
            with self.assertRaises(RuntimeError):
                future.result(timeout=10)
        before = list(session.calls)
        with self.assertRaisesRegex(RuntimeError, "^selected history shutdown incomplete$"):
            engine.close()
        self.assertEqual(len(self.rows(engine.store)), 1)
        self.assertEqual(engine.selected_handoff, {})
        self.assertEqual(engine.selected_history.pending(), {})
        self.assertEqual(engine.published_radios, ())
        self.assertEqual(session.calls, before)
        self.assertFalse(w.retain_selected_history)

    def test_archive_capacity_reports_restricted_and_durable_pending_without_deletion(self):
        from awqm.diagnostic_view import DiagnosticView
        w, engine, session = self.worker_engine(PORT)
        receipt = Engine._step(w)[0].selected_receipt
        archive = engine.selected_history
        self.assertTrue(archive.stage(receipt))
        view = DiagnosticView(engine.store, lambda: self.now, selected_history=archive,
                              selected_controllers=("w1",), selected_disclosure_days=1)
        status = view.selected_history_capacity()
        self.assertTrue(status["available"])
        self.assertEqual(status["archive_rows"], 0)
        self.assertEqual(status["durable_pending_rows"], 1)
        archive.save(receipt)
        self.now += 86401
        status = view.selected_history_capacity()
        self.assertEqual(status["archive_rows"], 1)
        self.assertEqual(status["archive_payload_bytes"], len(receipt.payload.encode("utf-8")))
        self.assertEqual(status["restricted_archive_rows"], 1)
        self.assertEqual(status["durable_pending_rows"], 0)
        self.assertEqual(status["archive_capacity"], "within_limits")
        self.assertNotIn(receipt.receipt_id, json.dumps(status))
        self.assertNotIn(AP, json.dumps(status))
        with patch("awqm.selected_history.MAX_ROWS", 2):
            self.assertEqual(view.selected_history_capacity()["archive_capacity"], "limited")
        with patch("awqm.selected_history.MAX_ROWS", 1):
            self.assertEqual(view.selected_history_capacity()["archive_capacity"], "full")
        with patch("awqm.selected_history.MAX_BYTES", status["archive_payload_bytes"]):
            self.assertEqual(view.selected_history_capacity()["archive_capacity"], "full")
        self.assertEqual(self.rows(engine.store), [(receipt.receipt_id, receipt.payload)])
        self.assertEqual(len([c for c in session.calls if c[0] == PORT]), 1)

    def test_archive_capacity_invalid_metadata_and_disabled_are_not_empty(self):
        from awqm.diagnostic_view import DiagnosticView
        w, engine, _ = self.worker_engine(PORT)
        archive = engine.selected_history
        receipt = Engine._step(w)[0].selected_receipt
        archive.save(receipt)
        view = DiagnosticView(engine.store, lambda: self.now, selected_history=archive,
                              selected_controllers=("w1",))
        with patch.object(archive, "capacity", side_effect=sqlite3.OperationalError("private")):
            self.assertEqual(view.selected_history_capacity(), {"enabled":True,"available":False})
        with engine.store.lock, engine.store._db(engine.store.root / "core.db") as db:
            db.execute("UPDATE selected_detail_receipts SET payload_bytes=-1")
        self.assertEqual(view.selected_history_capacity(), {"enabled":True,"available":False})
        self.assertEqual(self.rows(engine.store), [(receipt.receipt_id, receipt.payload)])
        view.selected_history=None
        self.assertEqual(view.selected_history_capacity(), {"enabled":False,"available":False})

    def test_pending_conflict_capacity_and_atomic_ack(self):
        w, engine, _ = self.worker_engine(PORT)
        receipt = Engine._step(w)[0].selected_receipt
        ledger = engine.selected_history
        self.assertTrue(ledger.stage(receipt))
        self.assertFalse(ledger.stage(receipt))
        self.assertEqual(ledger.query("w1", PORT, 1000, 1001), ((), None))
        other = replace(receipt, receipt_id="f"*32 if receipt.receipt_id != "f"*32 else "0"*32)
        with self.assertRaises(ValueError):
            ledger.stage(other)
        with self.assertRaises(ValueError):
            ledger.save(other)
        with patch("awqm.selected_history.MAX_ROWS", 0):
            with self.assertRaises(SelectedHistoryFull):
                ledger.save(receipt)
        self.assertEqual(ledger.pending(), {"w1": receipt})
        self.assertEqual(self.rows(engine.store), [])
        backup = engine.store.backup(Path(self.root.name) / "pending-backup")
        restored = SelectedHistory(Store(backup, mode="diagnostic"))
        self.assertEqual(restored.pending(), {"w1": receipt})
        self.assertTrue(ledger.save(receipt))
        self.assertEqual(ledger.pending(), {})
        self.assertTrue(ledger.stage(receipt))
        self.assertFalse(ledger.save(receipt))
        self.assertEqual(ledger.pending(), {})
        self.assertEqual(self.rows(engine.store), [(receipt.receipt_id, receipt.payload)])

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
                             "extension_channel_utilization":1,"quality":95},
                         "coverage_index":{"components":["7","1"],"reported_total":"8","total_consistent":True},
                         "interference_index":{"components":["18","11","4","1"],"reported_total":"34","total_consistent":True}},
                        {"channel":44, "reported_band":None, "noise_raw":None,
                         "split_scan_reported":None, "retry_raw":None,
                         "phy_error_raw":"0", "mac_error_raw":None, "utilization":None,
                         "coverage_index":None,
                         "interference_index":{"components":["2","3","4","5"],"reported_total":"9","total_consistent":False}}])
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

    def test_d03_boundary_hint_archived_from_actual_worker_and_legacy_safe(self):
        from awqm.selected_history import receipt_summary
        from awqm.diagnostic_view import DiagnosticView
        w, engine, session = self.worker_engine(STATS)
        session.responses[STATS]=FIXTURE.replace("TX Timeouts 4","TX Timeouts 18446744073709551615")
        for elapsed in (0,60,60):
            if elapsed:
                self.now += elapsed
                for cmd, reading in list(w.latest.items()):
                    w.latest[cmd]=w.outcomes[cmd]=replace(reading,observed_at=self.now)
                w.schedule.due={k:self.now+1000 for k in w.schedule.polls}
                w.schedule.protection.cpu(20,self.now)
                cmd="show ap radio-summary"
                reading=w.latest[cmd]
                w.latest[cmd]=w.outcomes[cmd]=replace(reading,data=[{**x,"utilization":90} for x in reading.data])
            if self.now==1120:
                session.responses[STATS]=FIXTURE.replace("TX Timeouts 4","TX Timeouts 18446744073709551614")
            outcome=Engine._step(w)
            self.assertEqual(outcome[0].status,"success")
            receipt=outcome[0].selected_receipt
            self.assertIsNotNone(receipt)
            self.assertIsNone(self.harvest(engine,outcome)["storage_error"])
        self.assertEqual([c[1] for c in session.calls if c[0]==STATS],[(AP,0),(AP,1),(AP,0)])
        view=DiagnosticView(engine.store,lambda:self.now,key=b"fixture",
                            selected_history=engine.selected_history,selected_controllers=("w1",))
        rows=view.selected_history_page("w1","D03",1000,1121)["rows"]
        self.assertEqual([r["counter_boundary_hint"] for r in rows],
                         ["not_assessed","not_assessed","reported_decrease"])
        self.assertTrue(all(r["counter_epoch"] is None and r["measurement_start"] is None
                            and r["sample_time_verified"] is False for r in rows))
        data=json.loads(receipt.payload)
        self.assertEqual(data["observation"].pop("counter_boundary_hint"),"reported_decrease")
        legacy=replace(receipt,payload=json.dumps(data))
        self.assertEqual(receipt_summary(legacy,lambda _:"masked")["counter_boundary_hint"],"not_assessed")
        for bad in (None,True,[],{},"verified_epoch","PRIVATE-HINT"):
            data["observation"]["counter_boundary_hint"]=bad
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError,"selected history record unavailable"):
                receipt_summary(replace(receipt,payload=json.dumps(data)),lambda _:"masked")

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
        idx = row["coverage_index"]
        idx.update(components=["18446744073709551615","1"], reported_total="0",
                   total_consistent=False, private="DO-NOT-DISCLOSE")

        def project():
            return receipt_summary(replace(original, payload=json.dumps(data)), lambda _: "masked")

        detail = project()["rf_detail"]
        self.assertEqual(detail[0]["noise_raw"], "-92*")
        self.assertIs(detail[0]["split_scan_reported"], True)
        self.assertNotIn("DO-NOT-DISCLOSE", json.dumps(detail))
        self.assertIsNone(detail[1]["coverage_index"])
        self.assertEqual(detail[1]["interference_index"], {"components":["2","3","4","5"],
                         "reported_total":"9","total_consistent":False})
        self.assertEqual(detail[0]["coverage_index"], {"components":["18446744073709551615","1"],
                         "reported_total":"0","total_consistent":False})
        for target, key, invalid in (
            (row, "coverage_index", [[], True, {}]),
            (idx, "components", [[], ["1"], ["1","2","3"], ["18446744073709551616","0"], [0,"0"], [None,"1"]]),
            (idx, "reported_total", [None, True, "18446744073709551616", "-1"]),
            (idx, "total_consistent", [True, 0, None]),
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
        del row["interference_index"]
        with self.assertRaisesRegex(RuntimeError, "^selected history record unavailable$"):
            project()
        row["interference_index"] = None
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

    def test_admin_disclosure_floor_rolls_forward_without_deleting_receipts(self):
        from awqm.diagnostic_view import DiagnosticView
        from awqm.selected_history import MAX_QUERY_SECONDS
        w, engine, session = self.worker_engine(PORT)
        outcome = Engine._step(w)
        receipt = outcome[0].selected_receipt
        self.harvest(engine, outcome)
        ledger = engine.selected_history
        # A later, distinct receipt, with matching private provenance.
        data = json.loads(receipt.payload)
        data["received_at"] = data["observation"]["received_at"] = 1001
        later = replace(receipt, receipt_id="f"*32 if receipt.receipt_id != "f"*32 else "0"*32,
                        received_at=1001, payload=json.dumps(data))
        ledger.save(later)
        now = [1000 + MAX_QUERY_SECONDS]
        view = DiagnosticView(engine.store, lambda: now[0], key=b"fixture",
                              selected_history=ledger, selected_controllers=("w1",))
        before = list(session.calls)
        first = view.selected_history_page("w1", "D07", 999, 1002, limit=1)
        self.assertEqual(first["disclosure_floor"], 1000)
        self.assertTrue(first["disclosure_limited"])
        self.assertEqual([r["received_at"] for r in first["rows"]], [1000])
        cursor = first["next_cursor"]
        now[0] += 1
        tail = view.selected_history_page("w1", "D07", 999, 1002, limit=1,
                                after=(cursor["received_at"], cursor["receipt_id"]))
        self.assertEqual([r["received_at"] for r in tail["rows"]], [1001])
        self.assertIsNone(tail["next_cursor"])
        now[0] += 1
        old = view.selected_history_page("w1", "D07", 999, 1002)
        self.assertEqual(old["rows"], [])
        self.assertTrue(old["disclosure_limited"])
        self.assertIsNone(old["next_cursor"])
        # A wholly expired query still validates its original cursor.
        with self.assertRaises(ValueError):
            view.selected_history_page("w1", "D07", 999, 1002, after=(998,"a"*32))
        self.assertEqual(ledger.query("w1", PORT, 999, 1002)[0], (receipt, later))
        self.assertEqual(session.calls, before)
        self.assertEqual(len(self.rows(engine.store)), 2)
        # Restored core history is subject to the same disclosure floor.
        backup = engine.store.backup(Path(self.root.name) / "history-age-backup")
        restored = Store(backup, mode="diagnostic")
        restored_view = DiagnosticView(restored, lambda: now[0],
            selected_history=SelectedHistory(restored), selected_controllers=("w1",))
        self.assertEqual(restored_view.selected_history_page("w1", "D07", 999, 1002)["rows"], [])
        now[0] = float("nan")
        with self.assertRaisesRegex(RuntimeError, "selected history clock unavailable"):
            view.selected_history_page("w1", "D07", 999, 1002)

    def test_shorter_site_disclosure_policy_with_cursor_and_restore(self):
        from awqm.diagnostic_view import DiagnosticView
        w, engine, session = self.worker_engine(PORT)
        outcome = Engine._step(w)
        receipt = outcome[0].selected_receipt
        self.harvest(engine, outcome)
        ledger = engine.selected_history
        data = json.loads(receipt.payload)
        data["received_at"] = data["observation"]["received_at"] = 1001
        later = replace(receipt, receipt_id="f"*32 if receipt.receipt_id != "f"*32 else "0"*32,
                        received_at=1001, payload=json.dumps(data))
        ledger.save(later)
        cfg = SimpleNamespace(root=Path(self.root.name))
        (cfg.root / "config").mkdir()
        (cfg.root / "config/selected-history.json").write_text(
            json.dumps({**POLICY, "disclosure_days": 7}), encoding="utf-8")
        enabled, days = load_selected_history_policy(cfg)
        self.assertTrue(enabled)
        now = [1000 + days*86400]
        def read_model(store, archive):
            return DiagnosticView(store, lambda: now[0], selected_history=archive,
                                  selected_controllers=("w1",), selected_disclosure_days=days)
        view = read_model(engine.store, ledger)
        before = list(session.calls)
        first = view.selected_history_page("w1", "D07", 999, 1002, limit=1)
        self.assertEqual(first["disclosure_days"], 7)
        self.assertEqual(first["disclosure_floor"], 1000)
        self.assertTrue(first["disclosure_limited"])
        self.assertEqual([r["received_at"] for r in first["rows"]], [1000])
        cursor = first["next_cursor"]
        now[0] += 1
        tail = view.selected_history_page("w1", "D07", 999, 1002, limit=1,
                            after=(cursor["received_at"], cursor["receipt_id"]))
        self.assertEqual([r["received_at"] for r in tail["rows"]], [1001])
        self.assertIsNone(tail["next_cursor"])
        now[0] += 1
        self.assertEqual(view.selected_history_page("w1", "D07", 999, 1002)["rows"], [])
        self.assertEqual(ledger.query("w1", PORT, 999, 1002)[0], (receipt, later))
        backup = engine.store.backup(Path(self.root.name) / "shorter-history-backup")
        restored = Store(backup, mode="diagnostic")
        restored_ledger = SelectedHistory(restored)
        self.assertEqual(read_model(restored, restored_ledger).selected_history_page(
            "w1", "D07", 999, 1002)["rows"], [])
        self.assertEqual(restored_ledger.query("w1", PORT, 999, 1002)[0], (receipt, later))
        self.assertEqual(session.calls, before)
        with self.assertRaises(ValueError):
            DiagnosticView(engine.store, lambda: now[0], selected_history=ledger,
                           selected_disclosure_days=31)

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
            (("w1", PORT, 0, 1), {"not_before": True}),
            (("w1", PORT, 0, 1), {"not_before": -1}),
            (("w1", PORT, 0, 1), {"not_before": 10**20}),
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
