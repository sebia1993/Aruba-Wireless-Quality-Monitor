from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from awqm import storage
from awqm.model import Radio


DAY = "20261101"
START = storage.day_start(DAY)
END = START + 86400


class AggregationConcurrencyTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = storage.Store(temp.name)
        self.row = Radio("r:0", "r", "AP", "controller", "5GHz", 44, START, 35, 4, -94, 2)
        self.store.save(self.row)

    @contextmanager
    def paused_rebuild(self):
        entered = Event()
        resume = Event()
        original = self.store._aggregate_resolution

        def pause(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("test aggregation was never resumed")
            return original(*args, **kwargs)

        with patch.object(self.store, "_aggregate_resolution", side_effect=pause), ThreadPoolExecutor(4) as pool:
            first = pool.submit(self.store.aggregate_day, DAY, END)
            try:
                self.assertTrue(entered.wait(5))
                yield first, pool, resume
            finally:
                resume.set()

    def test_foreground_read_write_and_safe_retirement_during_rebuild(self):
        with self.paused_rebuild() as (first, pool, resume):
            def foreground():
                current = replace(self.row, observed_at=END)
                self.assertTrue(self.store.save(current))
                self.assertEqual(self.store.history("r:0", END, END + 1)[0]["observed_at"], END)
                self.assertEqual(self.store.history("r:0", START, END, "5m"), [])
                self.assertEqual(self.store.prune_raw(END + 10 * 86400, [DAY]), [])
            pool.submit(foreground).result(timeout=1)
            resume.set()
            self.assertEqual(first.result(5), {"5m": 1, "1h": 1})

    def test_late_arrival_stays_unpublished_until_next_serial_rebuild(self):
        with TemporaryDirectory() as archive_root:
            backup = Path(archive_root) / "snapshot"
            with self.paused_rebuild() as (first, pool, resume):
                late = replace(self.row, observed_at=START + 60)
                pool.submit(self.store.save, late).result(timeout=1)
                starting = Event()
                def rebuild_again():
                    starting.set()
                    return self.store.aggregate_day(DAY, END)
                second = pool.submit(rebuild_again)
                self.assertTrue(starting.wait(2))
                saved = pool.submit(self.store.backup, backup, True, True)
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.2)
                resume.set()
                with self.assertRaises(storage.AggregateRevisionChanged):
                    first.result(5)
                self.assertEqual(second.result(5), {"5m": 2, "1h": 2})
                self.assertEqual(saved.result(5), backup)
            self.assertEqual(self.store.history("r:0", START, END, "1h")[0]["observed"], 2)

    def test_request_cancelled_while_waiting_for_another_rebuild(self):
        with self.paused_rebuild() as (first, pool, resume):
            cancel = Event()
            queued = pool.submit(self.store.aggregate_day, DAY, END, cancel=cancel)
            cancel.set()
            with self.assertRaises(storage.AggregateCancelled):
                queued.result(2)
            resume.set()
            self.assertEqual(first.result(5), {"5m": 1, "1h": 1})

    def test_cancelled_partial_rebuild_stays_hidden_and_restarts(self):
        self.store.aggregate_day(DAY, END)
        cancel = Event()
        original = self.store._aggregate_resolution
        def stop_after_first_resolution(*args, **kwargs):
            result = original(*args, **kwargs)
            cancel.set()
            return result
        with patch.object(self.store, "_aggregate_resolution", side_effect=stop_after_first_resolution):
            with self.assertRaises(storage.AggregateCancelled):
                self.store.aggregate_day(DAY, END, cancel=cancel)
        self.assertEqual(self.store.history("r:0", START, END, "5m"), [])
        self.assertEqual(self.store.prune_raw(END + 10 * 86400, [DAY]), [])
        self.assertEqual(self.store.aggregate_day(DAY, END), {"5m": 1, "1h": 1})
