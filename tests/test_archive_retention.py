import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from awqm.archive_retention import ArchiveRetention, load_archive_retention
from awqm.maintenance import Maintenance, verified_backup
from awqm.storage import Store


class ArchiveRetentionTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.archives = self.root / "backup"
        self.archives.mkdir()
        self.store = Store(self.root / "data", mode="diagnostic")
        self.rotation = ArchiveRetention(self.store, self.archives, 2)
        self.now = int(time.time())

    def archive(self, kind, age, register=True):
        stamp = self.now - age * 86400
        name = f"{kind}-{stamp}-01234567"
        path = verified_backup(self.store, self.archives / name, include_aggregates=(kind == "aggregate"))
        manifest = path / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["created_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        manifest.write_text(json.dumps(data), encoding="utf-8")
        if register:
            self.rotation.register(name)
        return path

    def test_separate_explicit_positive_day_policy(self):
        cfg = SimpleNamespace(root=self.root)
        config = self.root / "config"
        config.mkdir()
        (config / "retention.json").write_text('{"version":1,"enabled":true,"authorized":true,"policy":"balanced-7-30-365-v1"}')
        self.assertIsNone(load_archive_retention(cfg))
        policy = config / "archive-retention.json"
        policy.write_text('{"version":1,"enabled":false}')
        self.assertIsNone(load_archive_retention(cfg))
        values = {"version": 1, "enabled": True, "authorized": True, "policy": "scheduled-archives-v1", "days": 2}
        for bad in (0, -1, True, "2"):
            policy.write_text(json.dumps({**values, "days": bad}))
            with self.assertRaises(ValueError):
                load_archive_retention(cfg)
        policy.write_text(json.dumps({**values, "authorized": False}))
        with self.assertRaises(ValueError):
            load_archive_retention(cfg)
        policy.write_text(json.dumps(values))
        self.assertEqual(load_archive_retention(cfg), 2)

    def test_one_per_pass_protected_copies_and_unregistered_archives_survive(self):
        manual = self.archive("core", 40, register=False)
        old_core = self.archive("core", 30)
        old_week = self.archive("aggregate", 26)
        protected = self.archive("core", 20)
        latest_core = self.archive("core", 10)
        latest_week = self.archive("aggregate", 8)
        for removal in (old_core, old_week):
            result = self.rotation.run(self.now, (protected.name,))
            self.assertEqual(result["deleted"], removal.name)
        self.assertIsNone(self.rotation.run(self.now, (protected.name,))["deleted"])
        self.assertEqual(set(self.archives.iterdir()), {manual, protected, latest_week, latest_core})

    def test_unexpected_content_and_corrupt_replacement_hold(self):
        old = self.archive("core", 20)
        newer = self.archive("core", 8)
        extra = old / "other.txt"
        extra.write_text("retain")
        self.assertIsNone(self.rotation.run(self.now)["deleted"])
        self.assertTrue((old / "core.db").is_file())
        self.assertEqual(extra.read_text(), "retain")
        extra.unlink()
        with (newer / "core.db").open("ab") as out:
            out.write(b"corrupt")
        self.assertIsNone(self.rotation.run(self.now + 86400)["deleted"])
        self.assertTrue((old / "core.db").is_file())

    def test_interruption_resumes_only_verified_remaining_files(self):
        old = self.archive("core", 20)
        self.archive("core", 8)
        unlink = Path.unlink

        def interrupt(path, *args, **kwargs):
            if path.name == "manifest.json":
                raise OSError("interrupted")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupt):
            self.assertIsNone(self.rotation.run(self.now)["deleted"])
        self.assertFalse(old.exists())
        quarantine = self.archives / (".awqm-expiring-" + old.name)
        self.assertEqual([p.name for p in quarantine.iterdir()], ["manifest.json"])
        extra = quarantine / "operator-note"
        extra.write_text("survive")
        restarted = ArchiveRetention(self.store, self.archives, 2)
        self.assertIsNone(restarted.run(self.now + 86400)["deleted"])
        self.assertTrue(extra.exists())
        other = self.archive("aggregate", 19)
        self.archive("aggregate", 7)
        self.assertEqual(restarted.run(self.now + 2*86400)["deleted"], other.name)
        self.assertTrue(extra.exists())
        extra.unlink()
        self.assertEqual(restarted.run(self.now + 3*86400)["deleted"], old.name)
        self.assertFalse(quarantine.exists())

    def test_archive_verification_allows_live_core_writes(self):
        self.archive("core", 20)
        self.archive("core", 7)
        entered = Event()
        resume = Event()
        verify = self.rotation._check

        def pause_verification(name, hashes):
            entered.set()
            if not resume.wait(10):
                raise RuntimeError("test verifier timed out")
            verify(name, hashes)

        result = []
        with patch.object(self.rotation, "_check", pause_verification):
            worker = Thread(target=lambda: result.append(self.rotation.run(self.now)))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.store.lock.acquire(timeout=1), "archive holds live store lock")
                try:
                    db = sqlite3.connect(self.store.root / "core.db", timeout=1)
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        db.rollback()
                    finally:
                        db.close()
                finally:
                    self.store.lock.release()
            finally:
                resume.set()
                worker.join(10)
        self.assertEqual(result[0]["status"], "ok")

    def test_hardlink_substitution_is_held(self):
        old = self.archive("core", 30)
        self.archive("core", 8)
        outside = self.root / "outside.db"
        (old / "core.db").rename(outside)
        os.link(outside, old / "core.db")
        self.assertIsNone(self.rotation.run(self.now)["deleted"])
        self.assertTrue((old / "core.db").exists())
        self.assertTrue(outside.exists())

    def test_archive_directory_link_is_held(self):
        old = self.archive("core", 30)
        self.archive("core", 8)
        outside = self.root / "detached-archive"
        old.rename(outside)
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(old))
        else:
            old.symlink_to(outside, target_is_directory=True)
        try:
            self.assertIsNone(self.rotation.run(self.now)["deleted"])
            self.assertTrue((outside / "core.db").exists())
        finally:
            if os.name == "nt":
                old.rmdir()
            else:
                old.unlink()

    def test_manifest_creation_time_must_also_be_old(self):
        old = self.archive("core", 20, register=False)
        manifest = old / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["created_at"] = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.rotation.register(old.name)
        self.archive("core", 8)
        self.assertIsNone(self.rotation.run(self.now)["deleted"])
        self.assertTrue(old.is_dir())
        self.assertIsNone(self.rotation.run(self.now + 2*86400 + 1)["deleted"])
        self.archive("core", 0)
        # The other old core may be selected first.
        deleted = [self.rotation.run(self.now + 2*86400 + 1)["deleted"] for _ in range(2)]
        self.assertIn(old.name, deleted)

    def test_scheduler_registers_only_while_opted_in(self):
        normal_space = lambda _: SimpleNamespace(free=100*10**9, total=200*10**9)
        now = [self.now]
        def tick(days):
            scheduler = Maintenance(self.store, self.archives, clock=lambda: now[0], usage=normal_space, archive_days=days)
            return scheduler.tick(force=True)
        legacy = tick(None)["last_backup"]
        enrolled = []
        for week in range(1, 4):
            now[0] = self.now + week*7*86400
            result = tick(2)
            self.assertEqual(result["status"], "ok")
            enrolled.append(result["last_backup"])
        self.assertTrue((self.archives / legacy).is_dir())
        self.assertFalse((self.archives / enrolled[0]).exists())
        self.assertTrue((self.archives / enrolled[-1]).is_dir())
