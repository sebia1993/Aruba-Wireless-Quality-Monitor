"""Opt-in expiration of individually registered scheduled SQLite backups."""
import json
import os
import re
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from threading import Lock

from .service_config import safe_path
from .storage import MONTH


NAME = re.compile(r"(core|aggregate)-([1-9][0-9]{0,11})-[0-9a-f]{8}", re.ASCII)
HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)


def load_archive_retention(cfg):
    path = safe_path(cfg.root, "config/archive-retention.json")
    if not path.exists():
        return None
    _directory(path.parent)
    if _regular_file(path).st_size > 4096:
        raise ValueError("archive policy too large")
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or type(policy.get("version")) is not int or policy["version"] != 1:
        raise ValueError("unsupported archive policy")
    if set(policy) == {"version", "enabled"} and policy["enabled"] is False:
        return None
    if (set(policy) != {"version", "enabled", "authorized", "policy", "days"}
            or policy["enabled"] is not True or policy["authorized"] is not True
            or policy["policy"] != "scheduled-archives-v1" or type(policy["days"]) is not int
            or policy["days"] < 1):
        raise ValueError("explicit scheduled archive expiration policy required")
    return policy["days"]


def _directory(path):
    if ".." in path.parts:
        raise ValueError("archive directory traversal prohibited")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("archive directory is not a plain directory")


def _regular_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("archive member is not a plain file")
    return info


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _members(path, hashes, partial=False):
    _directory(path)
    members = set()
    with os.scandir(path) as entries:
        for item in entries:
            if item.name not in hashes:
                raise ValueError("unexpected archive member")
            _regular_file(path / item.name)
            members.add(item.name)
    if not partial and members != set(hashes):
        raise ValueError("missing archive member")
    return members


def _safe_hashes(hashes, kind):
    if not isinstance(hashes, dict) or len(hashes) > 10001 or not {"core.db", "manifest.json"} <= hashes.keys():
        raise ValueError("invalid scheduled archive inventory")
    for name, value in hashes.items():
        if (name not in ("manifest.json", "core.db", "web-audit.db")
                and (kind != "aggregate" or not MONTH.fullmatch(name))):
            raise ValueError("invalid scheduled archive file")
        if not isinstance(value, str) or not HASH.fullmatch(value):
            raise ValueError("invalid scheduled archive hash")
    return hashes


class ArchiveRetention:
    def __init__(self, store, root, days):
        if store.mode != "diagnostic" or type(days) is not int or days < 1:
            raise ValueError("diagnostic archive policy with positive days required")
        self.store = store
        self.root = Path(root).absolute()
        _directory(self.root)
        if self.root == store.root.resolve() or store.root.resolve() in self.root.parents:
            raise ValueError("archive root inside data root")
        self.days = days
        self.lock = Lock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS scheduled_archives(
                name TEXT PRIMARY KEY, kind TEXT NOT NULL, stamp INTEGER NOT NULL, created INTEGER NOT NULL,
                hashes TEXT NOT NULL, deleting INTEGER NOT NULL DEFAULT 0, attempted INTEGER NOT NULL DEFAULT 0,
                directory_dev TEXT NOT NULL, directory_ino TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS archive_successor ON scheduled_archives(kind,stamp DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS archive_attempt ON scheduled_archives(attempted,deleting DESC,stamp)")

    def _db(self):
        return self.store._db(self.store.root / "core.db")

    def register(self, name):
        """Called only after this scheduler creates a new backup under the opt-in."""
        from .maintenance import digest
        match = NAME.fullmatch(name)
        if not match:
            raise ValueError("invalid scheduled archive name")
        kind, stamp = match.groups()
        path = self.root / name
        _directory(path)
        manifest = path / "manifest.json"
        if _regular_file(manifest).st_size > 2_000_000:
            raise ValueError("archive manifest too large")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (data.get("mode") != self.store.mode or data.get("include_raw") is not False
                or data.get("include_aggregates") is not (kind == "aggregate")
                or not isinstance(data.get("files"), list) or not isinstance(data.get("sha256"), dict)
                or len(data["files"]) != len(data["sha256"]) or set(data["files"]) != set(data["sha256"])
                or "manifest.json" in data["files"]):
            raise ValueError("scheduled archive manifest mismatch")
        created = datetime.fromisoformat(data["created_at"])
        if created.utcoffset() is None:
            raise ValueError("scheduled archive requires a timezone")
        hashes = _safe_hashes({**data["sha256"], "manifest.json": digest(manifest)}, kind)
        _members(path, hashes)
        identity = path.stat(follow_symlinks=False)
        if not identity.st_ino:
            raise ValueError("archive directory identity unavailable")
        with self._db() as db:
            db.execute("""INSERT INTO scheduled_archives(name,kind,stamp,created,hashes,directory_dev,directory_ino)
                          VALUES(?,?,?,?,?,?,?)""",
                       (name, kind, int(stamp), int(created.timestamp()), json.dumps(hashes),
                        str(identity.st_dev), str(identity.st_ino)))

    def _check(self, name, hashes):
        from .maintenance import digest, verify
        path = self.root / name
        _members(path, hashes)
        if digest(path / "manifest.json") != hashes["manifest.json"]:
            raise ValueError("registered manifest changed")
        verify(path)

    def run(self, now, protected=()):
        """Examine at most one eligible archive and its newer rollback copy."""
        protected = tuple(name for name in protected if name is not None)
        if any(not isinstance(name, str) or not NAME.fullmatch(name) for name in protected):
            raise ValueError("invalid current scheduled backup reference")
        cutoff = max(0, now - self.days * 86400)
        with self.lock, self._db() as db:
            excluded = "".join(",?" for _ in protected)
            candidate = db.execute(f"""SELECT name,kind,created,hashes,deleting,directory_dev,directory_ino
                FROM scheduled_archives AS a
                WHERE stamp<? AND created<? AND name NOT IN (''{excluded})
                  AND EXISTS(SELECT 1 FROM scheduled_archives b
                             WHERE b.kind=a.kind AND b.stamp>a.stamp AND b.created>=a.created
                               AND b.stamp<=? AND b.created<=? AND b.deleting=0)
                ORDER BY attempted,deleting DESC,stamp,name LIMIT 1""", (cutoff, cutoff, *protected, now, now)).fetchone()
            if not candidate:
                return {"status": "idle", "deleted": None}
            name, kind, created, encoded, deleting, device, inode = candidate
            db.execute("UPDATE scheduled_archives SET attempted=? WHERE name=?", (now, name))
            db.commit()
            try:
                match = NAME.fullmatch(name)
                if not match or match[1] != kind:
                    raise ValueError("invalid catalog entry")
                hashes = _safe_hashes(json.loads(encoded), kind)
                newer, saved = db.execute("""SELECT name,hashes FROM scheduled_archives WHERE kind=?
                    AND stamp>? AND created>=? AND stamp<=? AND created<=? AND deleting=0
                    ORDER BY stamp DESC,name DESC LIMIT 1""", (kind, int(match[2]), created, now, now)).fetchone()
                replacement = NAME.fullmatch(newer)
                if not replacement or replacement[1] != kind or int(replacement[2]) <= int(match[2]):
                    raise ValueError("no newer rollback copy")
                self._check(newer, _safe_hashes(json.loads(saved), kind))
                if not deleting:
                    self._check(name, hashes)
                    db.execute("UPDATE scheduled_archives SET deleting=1 WHERE name=?", (name,))
                # Persist authorization and inventory before rename or unlink.
                db.commit()
                self._finish(name, hashes, (int(device), int(inode)), previously_checked=not deleting)
                db.execute("DELETE FROM scheduled_archives WHERE name=?", (name,))
                return {"status": "ok", "deleted": name}
            except (OSError, ValueError, TypeError, sqlite3.Error):
                return {"status": "held", "archive": name, "deleted": None}

    def _finish(self, name, hashes, identity, previously_checked):
        from .maintenance import digest
        _directory(self.root)
        source = self.root / name
        pending = self.root / (".awqm-expiring-" + name)
        if _present(source):
            if _present(pending):
                raise ValueError("archive quarantine occupied")
            _directory(source)
            info = source.lstat()
            if (info.st_dev, info.st_ino) != identity:
                raise ValueError("registered archive directory changed")
            if previously_checked:
                _members(source, hashes)
            else:
                self._check(name, hashes)
            source.rename(pending)
        if not _present(pending):
            return
        _directory(pending)
        info = pending.lstat()
        if (info.st_dev, info.st_ino) != identity:
            raise ValueError("archive quarantine directory changed")
        members = _members(pending, hashes, partial=True)
        identities = {}
        for member in members:
            path = pending / member
            identities[member] = _regular_file(path)
            if digest(path) != hashes[member]:
                raise ValueError("quarantined archive member changed")
        # Leave the manifest until all its payloads have been removed.
        for member in sorted(members, key=lambda item: (item == "manifest.json", item)):
            _directory(pending)
            if not os.path.samestat(info, pending.lstat()):
                raise ValueError("archive quarantine directory replaced")
            path = pending / member
            current = _regular_file(path)
            old = identities[member]
            if not os.path.samestat(old, current) or (old.st_size, old.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise ValueError("quarantined file replaced")
            path.unlink()
        _directory(pending)
        if not os.path.samestat(info, pending.lstat()):
            raise ValueError("archive quarantine directory replaced")
        pending.rmdir()
