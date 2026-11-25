"""Current-user account defaults and v0.46.0 profile migration."""
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from awqm.device_profiles import DeviceProfile, Profiles


class SharedUsernameTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = Profiles(self.root)

    def test_old_profiles_supply_the_first_slot_without_rewriting_credentials(self):
        legacy = [
            ("WLC-3", "192.0.2.3", "regional", "", "", b"three", b"opaque DPAPI blob"),
            ("WLC-1", "192.0.2.1", "operator", "", "", b"one", None),
        ]
        db_path = self.root / "devices.sqlite3"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("""CREATE TABLE profiles (id TEXT PRIMARY KEY, host TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL, fingerprint TEXT NOT NULL, known_hosts TEXT NOT NULL,
                revision BLOB NOT NULL, credential BLOB)""")
            db.executemany("INSERT INTO profiles VALUES(?, ?, ?, ?, ?, ?, ?)", legacy)
        self.assertEqual(self.store.default_username(), "operator")
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM profiles ORDER BY rowid").fetchall(), legacy)
        self.store.remove("WLC-1")
        self.assertEqual(Profiles(self.root).default_username(), "operator")

    def test_per_device_account_override_leaves_the_default_unchanged(self):
        self.assertEqual(self.store.default_username(), "")
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.save(DeviceProfile("WLC-3", "192.0.2.3", "first"), "secret3", False)
        self.store.save(DeviceProfile("WLC-1", "192.0.2.1", "other"), "secret1", False)
        self.assertEqual(Profiles(self.root).default_username(), "first")
        self.assertEqual([p.username for p in self.store.list()], ["other", "first"])
