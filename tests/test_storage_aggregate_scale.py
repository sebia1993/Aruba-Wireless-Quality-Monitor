import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from awqm.model import Radio
from awqm.storage import RAW_SCHEMA, Store, day_start


DAY = "20261101"
START = day_start(DAY)
END = START + 86400


class DailyAggregationTests(unittest.TestCase):
    def test_empty_day_and_out_of_day_record(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with store._db(store._raw_path(DAY)) as raw:
                raw.execute(RAW_SCHEMA)
            self.assertEqual(store.aggregate_day(DAY, END), {"5m": 0, "1h": 0})
            with store._db(store._raw_path(DAY)) as raw:
                raw.execute("INSERT INTO radio_raw VALUES(?, ?, ?)", ("other", END, "{}"))
            with self.assertRaisesRegex(ValueError, "out-of-day"):
                store.aggregate_day(DAY, END)
            with store._db(store.root / "core.db") as core:
                manifest = core.execute(
                    "SELECT revision, completed_revision FROM aggregate_manifest WHERE day=?", (DAY,)
                ).fetchone()
            self.assertEqual(manifest, (0, None))

    def test_raw_grouping_never_sorts_the_entire_partition(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            sample = Radio("alpha", "ap", "Demo", "ctrl", "5GHz", 44, START, 75, 5, -94, 2)
            store.save_batch(
                replace(sample, radio_id=radio, observed_at=START + offset)
                for radio in ("alpha", "beta", "gamma")
                for offset in (0, 300, 3600, 86399)
            )
            raw_path = store._raw_path(DAY)
            raw_queries = []
            open_db = store._db

            @contextmanager
            def trace_raw_queries(path):
                with open_db(path) as connection:
                    if path == raw_path:
                        connection.set_trace_callback(raw_queries.append)
                    yield connection

            with patch.object(store, "_db", trace_raw_queries):
                self.assertEqual(store.aggregate_day(DAY, END), {"5m": 12, "1h": 12})

            with open_db(raw_path) as raw:
                for query in raw_queries:
                    if "group by" not in query.lower():
                        continue
                    plan = [row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + query)]
                    self.assertFalse(
                        any("SCAN radio_raw" in line for line in plan)
                        and any("USE TEMP B-TREE FOR GROUP BY" in line for line in plan),
                        f"Daily aggregation sorted across radios: {plan}",
                    )

    def test_rebuild_preserves_nulls_and_configuration_groups(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            sample = Radio("alpha", "ap", "Demo", "ctrl", "5GHz", 44, START, 20.5, 5, -94, 2)
            store.save_batch([
                sample,
                replace(sample, observed_at=START + 1, utilization=True),
                replace(sample, observed_at=START + 301, utilization=80, clients=7),
                replace(sample, observed_at=START + 303, epoch="2", complete=False),
                replace(sample, observed_at=START + 304, channel=48, clients=-1),
            ])
            for run in range(2):
                with self.subTest(run=run):
                    self.assertEqual(store.aggregate_day(DAY, END), {"5m": 5, "1h": 5})
                    rows = store.history("alpha", START, END, "1h")
                    metric_keys = ("epoch", "channel", "observed", "valid", "risky",
                                   "utilization_sum", "utilization_min", "utilization_max", "clients_max")
                    self.assertEqual([tuple(row[k] for k in metric_keys) for row in rows], [
                        ("1", 44, 3, 2, 1, 100.5, 20.5, 80, 7),
                        ("1", 48, 1, 1, 0, 20.5, 20.5, 20.5, None),
                        ("2", 44, 1, 0, 0, None, None, None, None),
                    ])
                    self.assertEqual(len(store.history("alpha", START, END, "5m")), 4)
