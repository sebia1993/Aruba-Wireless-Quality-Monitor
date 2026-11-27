"""Opt-in bounded archive of accepted selected-detail response receipts.

Only the existing sanitized projections are retained. Receipt time is not
measurement/event time, and a connection generation is not a counter epoch.
Core SQLite backup/restore includes this table; no automatic deletion here.
"""
from dataclasses import dataclass
import json
import re
from uuid import uuid4

from .service_config import safe_path
from .ap_port_status import COMMAND as PORT
from .arm_rf_summary import COMMAND as RF
from .live_arm_history import COMMAND as ARM
from .live_radio_stats import COMMAND as STATS

COMMAND_IDS = {"D03": STATS, "D04": RF, "D07": PORT, "D08": ARM}
COMMANDS = frozenset(COMMAND_IDS.values())
POLICY = {"version": 1, "enabled": True, "policy": "bounded-selected-receipts-v1"}
MAX_ROWS = 50000
MAX_BYTES = 128 * 1024 * 1024
MAX_RECEIPT = 128 * 1024
MAX_PENDING = 4
MAX_QUERY_ROWS = 20
MAX_QUERY_SECONDS = 30 * 86400
MAX_TIMESTAMP = 253402300799


def load_selected_history(cfg):
    path = safe_path(cfg.root, "config/selected-history.json")
    if not path.exists():
        return False
    if path.stat().st_size > 4096:
        raise ValueError("selected history policy too large")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if type(doc) is not dict or type(doc.get("version")) is not int:
        raise ValueError("invalid selected history policy")
    if doc == {"version": 1, "enabled": False} and doc["enabled"] is False:
        return False
    if doc != POLICY or doc["enabled"] is not True:
        raise ValueError("explicit selected history policy required")
    return True


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    controller: str
    command: str
    received_at: int
    payload: str


def capture(worker, path, target, key, started, received):
    """Called under the worker lock, after successful target revalidation."""
    rows = path.project(worker, key, received)["rows"]
    matches = [row for row in rows if row["ap_name"] == target[0]
               and row["received_at"] == received
               and (path.command != STATS or row.get("requested_radio") == target[2])]
    if len(matches) != 1:
        return None
    # Never copy raw CLI, the AP IP, host key or the private context tuple.
    data = {"schema": 1, "controller": worker.controller, "command": path.command,
            "ap_binding": target[1], "connection": worker.instance_id,
            "generation": worker.generation, "radio_approval": path.approval.radio_tag,
            "approval_at": path.approval.approved_at,
            "approval_until": path.approval.valid_until,
            "started_at": started if started <= received else None,
            "received_at": received, "measurement_start": None,
            "measurement_end": None, "counter_epoch": None,
            "source": "selected-cli-unvalidated", "observation": matches[0]}
    return Receipt(uuid4().hex, worker.controller, path.command, received,
                   json.dumps(data, ensure_ascii=True, sort_keys=True,
                              separators=(",", ":"), allow_nan=False))


# Fixed historical disclosure contract, intentionally independent of future
# parser expansion. These values are as reported, not measured deltas or Airtime.
D03_HISTORY_COUNTERS = ("radio_resets", "tx_timeouts", "rx_crc_errors")
D03_HISTORY_BUSY = ("channel_busy_1s_percent", "channel_busy_4s_percent",
                    "channel_busy_64s_percent")


def _reported_uint64(value):
    if value is None:
        return None
    if (type(value) is not str or re.fullmatch(r"[0-9]{1,20}", value) is None
            or int(value) > 2**64-1):
        raise ValueError("invalid historical counter")
    return value


def _radio_history_detail(obs):
    def percentage(value):
        if value is None:
            return None
        if type(value) not in (int, float) or not 0 <= value <= 100:
            raise ValueError("invalid historical Radio percentage")
        return value

    # Missing fields in an accepted schema-1 capture are corruption, not zero.
    # Unknown fields (including future/private keys) are never projected.
    counters, busy = obs["counters"], obs["radio_percentages"]
    if type(counters) is not dict or type(busy) is not dict:
        raise ValueError("invalid historical Radio detail")
    return {"reported_counters": {key: _reported_uint64(counters[key])
                                 for key in D03_HISTORY_COUNTERS},
            "reported_busy_windows": {key: percentage(busy[key])
                                      for key in D03_HISTORY_BUSY}}


# Only a bounded reported-value subset of the selected D04 channel table.
# Do not expose arbitrary parser keys, infer a band, convert noise to dBm,
# or treat reported utilization fields as aligned samples or client Airtime.
D04_HISTORY_RAW = ("retry_raw", "phy_error_raw", "mac_error_raw")
D04_HISTORY_UTIL = ("channel_utilization", "rx", "tx",
                    "extension_channel_utilization", "quality")


def _rf_history_index(value, count):
    """Keep reported index components and total, not a derived RF metric."""
    if value is None:
        return None
    if type(value) is not dict or type(value["components"]) is not list:
        raise ValueError("invalid historical RF index")
    parts = value["components"]
    if len(parts) != count:
        raise ValueError("invalid historical RF index count")
    parts = [_reported_uint64(part) for part in parts]
    total = _reported_uint64(value["reported_total"])
    if total is None or any(part is None for part in parts):
        raise ValueError("invalid historical RF index value")
    consistent = sum(map(int, parts)) == int(total)
    if value["total_consistent"] is not consistent:
        raise ValueError("invalid historical RF index consistency")
    return {"components": parts, "reported_total": total,
            "total_consistent": consistent}


def _rf_history_detail(obs, channels):
    layout = obs["layout"]
    if layout not in ("legacy", "band", "phy-type") or not channels:
        raise ValueError("invalid historical RF layout")
    rows, seen = [], set()
    for row in channels:
        if type(row) is not dict:
            raise ValueError("invalid historical RF row")
        channel, band = row["channel"], row["reported_band"]
        if (type(channel) is not int or not 1 <= channel <= 233
                or (layout == "legacy" and band is not None)
                or (layout != "legacy" and band not in ("2.4GHz", "5GHz", "6GHz"))
                or (band, channel) in seen):
            raise ValueError("invalid historical RF channel")
        seen.add((band, channel))
        noise = row["noise_raw"]
        if noise is not None and (type(noise) is not str or
                re.fullmatch(r"-?[0-9]{1,3}\*?", noise) is None):
            raise ValueError("invalid historical RF noise")
        split = row["split_scan_reported"]
        if split is not (None if noise is None else noise.endswith("*")):
            raise ValueError("invalid historical RF split-scan marker")
        util = row["utilization"]
        if util is not None:
            if type(util) is not dict:
                raise ValueError("invalid historical RF utilization")
            util = {key: util[key] for key in D04_HISTORY_UTIL}
            if any(type(value) is not int or not 0 <= value <= 100
                   for value in util.values()):
                raise ValueError("invalid historical RF utilization value")
        rows.append({"channel": channel, "reported_band": band,
                     "noise_raw": noise, "split_scan_reported": split,
                     **{key: _reported_uint64(row[key]) for key in D04_HISTORY_RAW},
                     "utilization": util,
                     "coverage_index": _rf_history_index(row["coverage_index"], 2),
                     "interference_index": _rf_history_index(row["interference_index"], 4)})
    return rows


# D07's archived port projection is already stripped of wired MACs.
# Do not expose unconstrained CLI labels (including STP), private fields,
# or convert reported counters to deltas/rates.
D07_HISTORY_COUNTERS = ("tx_packets", "tx_bytes", "rx_packets", "rx_bytes")


def _port_history_detail(obs, ports):
    if not ports or obs["row_count_verified"] is not False or obs["physical_uplink_verified"] is not False:
        raise ValueError("invalid historical port provenance")
    rows, seen = [], set()
    for row in ports:
        if type(row) is not dict:
            raise ValueError("invalid historical port row")
        port, admin, oper = row["port"], row["admin"], row["oper"]
        speed, duplex = row["speed_mbps"], row["duplex"]
        if (type(port) is not int or not 0 <= port <= 999 or port in seen
                or admin not in ("enabled", "disabled")
                or oper not in ("up", "down")
                or (speed is not None and (type(speed) is not int or not 1 <= speed <= 100000))
                or duplex not in (None, "full", "half")):
            raise ValueError("invalid historical port field")
        seen.add(port)
        rows.append({"port": port, "admin": admin, "oper": oper,
                     "speed_mbps": speed, "duplex": duplex,
                     **{key: _reported_uint64(row[key]) for key in D07_HISTORY_COUNTERS}})
    return rows


# Historical D08 disclosure is intentionally fixed to the documented parser
# subset. A future parser expansion must not silently expand disclosure.
D08_HISTORY_REASONS = frozenset((
    "I", "R", "N", "Q", "E", "INV", "G", "M", "P+", "P-", "40INT",
    "NO40INT", "OFF", "OFF(R)", "OFF(MA)", "ON", "D", "AIRMATCH",
    "I*", "C", "NC", "Random", "RMC", "RCP", "OFF(C)",
))


def _arm_history_detail(obs, interfaces):
    if (not interfaces or any(obs[k] is not False for k in
            ("target_verified", "radio_binding_verified", "completeness_verified",
             "event_time_verified"))
            or type(obs["interface_count"]) is not int
            or obs["interface_count"] != len(interfaces)):
        raise ValueError("invalid historical ARM provenance")

    def notation(value, channel):
        if value is None:
            return None
        pattern = r"[0-9]{1,3}[+\-ES]?" if channel else r"-?[0-9]{1,3}(?:\.[0-9]{1,3})?"
        if type(value) is not str or re.fullmatch(pattern, value) is None:
            raise ValueError("invalid historical ARM notation")
        if channel and int(re.match(r"[0-9]+", value)[0]) > 233:
            raise ValueError("invalid historical ARM channel")
        return value

    rows, seen, layouts = [], set(), set()
    remaining, reported = 64, 0
    for item in interfaces:
        if type(item) is not dict:
            raise ValueError("invalid historical ARM interface")
        name, phy, present = item["interface"], item["phy_type"], item["table_present"]
        count, events = item["event_count"], item["events"]
        if (name not in ("wifi0", "wifi1", "wifi2") or name in seen
                or phy not in (None, "2.4GHz", "5GHz", "6GHz")
                or type(present) is not bool or (not present and phy is None)
                or type(count) is not int or not 0 <= count <= 4096
                or (not present and count != 0)
                or type(events) is not list or len(events) != min(count, remaining)):
            raise ValueError("invalid historical ARM interface fields")
        seen.add(name)
        layouts.add(phy is None)
        retained = []
        for event in events:
            if type(event) is not dict:
                raise ValueError("invalid historical ARM row")
            reason, age = event["reason_code"], event["reported_age_seconds"]
            if (type(reason) is not str or reason not in D08_HISTORY_REASONS
                    or type(age) is not int or not 0 <= age <= 8639999999):
                raise ValueError("invalid historical ARM event fields")
            retained.append({"reason_code": reason, "reported_age_seconds": age,
                             **{key: notation(event[key], key.endswith("channel"))
                                for key in ("old_channel", "new_channel",
                                            "old_power", "new_power")}})
        rows.append({"interface": name, "phy_type": phy, "table_present": present,
                     "reported_event_rows": count, "events": retained})
        reported += count
        remaining -= len(events)
    if (len(layouts) != 1 or reported > 4096 or type(obs["event_count"]) is not int
            or obs["event_count"] != reported or obs["truncated"] is not (reported > 64)):
        raise ValueError("invalid historical ARM counts")
    return rows


def receipt_summary(receipt, token):
    """Explicit allowlist for the historical index, not a metric export.

    AP binding is keyed by the view instance before disclosure; no AP name,
    raw payload, connection, approval, or sample/event time is returned.
    Count fields describe the receipt, never completeness or device health.
    """
    def integer(value, lo, hi):
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError("invalid historical count")
        return value

    def sequence(value, bound):
        if type(value) is not list or len(value) > bound:
            raise ValueError("invalid historical rows")
        return value

    try:
        data = json.loads(receipt.payload)
        obs = data["observation"]
        binding = data["ap_binding"]
        if (type(data["schema"]) is not int or data["schema"] != 1
                or data["controller"] != receipt.controller
                or data["command"] != receipt.command
                or data["received_at"] != receipt.received_at
                or data["source"] != "selected-cli-unvalidated"
                or type(binding) is not str or re.fullmatch(r"[0-9a-f]{64}", binding) is None
                or obs["received_at"] != receipt.received_at
                or obs["field_validated"] is not False
                or obs["sample_time_verified"] is not False
                or any(data[k] is not None for k in
                       ("measurement_start", "measurement_end", "counter_epoch"))):
            raise ValueError("invalid historical provenance")
        detail = {}
        if receipt.command == PORT:
            ports = sequence(obs["ports"], 32)
            summary = {"retained_port_rows": len(ports)}
            detail = {"port_detail": _port_history_detail(obs, ports)}
        elif receipt.command == RF:
            channels = sequence(obs["channels"], 64)
            retained = len(channels)
            summary = {"retained_channel_rows": retained,
                       "reported_channel_rows": integer(obs["row_count"], retained, 768)}
            detail = {"rf_detail": _rf_history_detail(obs, channels)}
        elif receipt.command == ARM:
            interfaces = sequence(obs["interfaces"], 3)
            rows = _arm_history_detail(obs, interfaces)
            retained = sum(len(item["events"]) for item in rows)
            summary = {"retained_event_rows": integer(retained, 0, 64),
                       "reported_event_rows": integer(obs["event_count"], retained, 4096),
                       "retained_interfaces": len(interfaces)}
            detail = {"arm_detail": rows}
        elif receipt.command == STATS:
            summary = {"requested_radio": integer(obs["requested_radio"], 0, 2),
                       "observed_field_count": integer(obs["observed_field_count"], 0, 4096),
                       "ignored_row_count": integer(obs["ignored_row_count"], 0, 4096)}
            detail = {"radio_detail": _radio_history_detail(obs)}
        else:
            raise ValueError("invalid historical command")
        return {"ap_token": token("selected-history:" + binding),
                "received_at": integer(receipt.received_at, 0, MAX_TIMESTAMP),
                "source": "selected-cli-unvalidated", "field_validated": False,
                "sample_time_verified": False, "completeness_verified": False,
                "measurement_start": None, "measurement_end": None,
                "counter_epoch": None, "summary": summary, **detail}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        # Corrupt or unsupported archived schema must not become a plausible
        # empty/normal observation or disclose the offending record.
        raise RuntimeError("selected history record unavailable") from None


class SelectedHistoryFull(RuntimeError):
    """Capacity exhausted: retain prior evidence and stop new dispatch."""


class SelectedHistory:
    def __init__(self, store):
        if store.mode != "diagnostic":
            raise ValueError("diagnostic store required")
        self.store = store
        with store.lock, store._db(store.root / "core.db") as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS selected_detail_receipts(
                receipt_id TEXT PRIMARY KEY, controller TEXT NOT NULL,
                command TEXT NOT NULL, received_at INTEGER NOT NULL,
                payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS selected_detail_pending(
                controller TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
                command TEXT NOT NULL, received_at INTEGER NOT NULL,
                payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS selected_detail_receipt_time
                ON selected_detail_receipts(controller,received_at);
            CREATE INDEX IF NOT EXISTS selected_detail_receipt_size
                ON selected_detail_receipts(payload_bytes);
            CREATE INDEX IF NOT EXISTS selected_detail_receipt_query
                ON selected_detail_receipts(controller,command,received_at,receipt_id);
            """)

    def query(self, controller, command, start, end, *, limit=20, after=None, not_before=0):
        """Private bounded receipt-time query; caller must authorize disclosure.

        Not an observation-time query, a coverage statement or a public API.
        Keyset order is ascending (received_at, receipt_id), with [start,end).
        Separate requests are not a database snapshot: concurrent late inserts
        before the cursor may be missed. An internal disclosure floor can further
        restrict the validated requested window without invalidating its cursor.
        No count, raw CLI or device access.
        """
        if (type(controller) is not str or not 1 <= len(controller) <= 128
                or type(command) is not str or command not in COMMANDS
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= MAX_TIMESTAMP
                or end - start > MAX_QUERY_SECONDS
                or type(limit) is not int or not 1 <= limit <= MAX_QUERY_ROWS
                or type(not_before) is not int or not 0 <= not_before <= MAX_TIMESTAMP):
            raise ValueError("invalid selected history query")
        params = [controller, command, max(start, not_before), end]
        where = "controller=? AND command=? AND received_at>=? AND received_at<?"
        if after is not None:
            if (type(after) is not tuple or len(after) != 2
                    or type(after[0]) is not int or not start <= after[0] < end
                    or type(after[1]) is not str
                    or re.fullmatch(r"[0-9a-f]{32}", after[1]) is None):
                raise ValueError("invalid selected history cursor")
            where += " AND (received_at,receipt_id)>(?,?)"
            params.extend(after)
        with self.store.lock, self.store._db(self.store.root / "core.db") as db:
            rows = db.execute(
                "SELECT receipt_id,controller,command,received_at,payload "
                "FROM selected_detail_receipts WHERE " + where +
                " ORDER BY received_at,receipt_id LIMIT ?", (*params, limit+1)
            ).fetchall()
        page = tuple(Receipt(*row) for row in rows[:limit])
        cursor = (page[-1].received_at, page[-1].receipt_id) if len(rows) > limit else None
        return page, cursor

    @staticmethod
    def _values(receipt):
        if not isinstance(receipt, Receipt) or receipt.command not in COMMANDS:
            raise ValueError("invalid selected receipt")
        size = len(receipt.payload.encode("utf-8"))
        if not 0 < size <= MAX_RECEIPT:
            raise ValueError("selected receipt size limit")
        data = json.loads(receipt.payload)
        if (data.get("schema") != 1 or data.get("controller") != receipt.controller
                or data.get("command") != receipt.command
                or data.get("received_at") != receipt.received_at
                or data.get("source") != "selected-cli-unvalidated"
                or any(data.get(k) is not None for k in
                       ("measurement_start", "measurement_end", "counter_epoch"))
                or data["observation"].get("field_validated") is not False
                or data["observation"].get("sample_time_verified") is not False):
            raise ValueError("invalid selected receipt provenance")
        return (receipt.controller, receipt.command, receipt.received_at,
                receipt.payload, size)

    @classmethod
    def _pending_values(cls, receipt):
        values = cls._values(receipt)
        if (type(receipt.receipt_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", receipt.receipt_id) is None
                or type(receipt.controller) is not str
                or not 1 <= len(receipt.controller) <= 128
                or type(receipt.received_at) is not int
                or not 0 <= receipt.received_at <= MAX_TIMESTAMP):
            raise ValueError("invalid pending selected receipt")
        return values

    def stage(self, receipt):
        """Durably stage one accepted receipt per controller; never overwrite."""
        values = self._pending_values(receipt)
        with self.store.lock, self.store._db(self.store.root / "core.db") as db:
            old = db.execute("""SELECT receipt_id,controller,command,received_at,payload,payload_bytes
                FROM selected_detail_pending WHERE controller=? OR receipt_id=?""",
                             (receipt.controller, receipt.receipt_id)).fetchall()
            if old:
                if old != [(receipt.receipt_id, *values)]:
                    raise ValueError("pending selected receipt conflict")
                return False
            if db.execute("SELECT COUNT(*) FROM selected_detail_pending").fetchone()[0] >= MAX_PENDING:
                raise SelectedHistoryFull("selected pending capacity")
            db.execute("""INSERT INTO selected_detail_pending
                (receipt_id,controller,command,received_at,payload,payload_bytes)
                VALUES(?,?,?,?,?,?)""", (receipt.receipt_id, *values))
        return True

    def pending(self):
        """Restore a bounded private queue, rejecting malformed/corrupt rows."""
        with self.store.lock, self.store._db(self.store.root / "core.db") as db:
            count, largest = db.execute("""SELECT COUNT(*),
                COALESCE(MAX(length(CAST(payload AS BLOB))),0)
                FROM selected_detail_pending""").fetchone()
            if count > MAX_PENDING or largest > MAX_RECEIPT:
                raise ValueError("invalid selected pending bounds")
            rows = db.execute("""SELECT receipt_id,controller,command,received_at,payload,payload_bytes
                FROM selected_detail_pending ORDER BY controller LIMIT ?""",
                              (MAX_PENDING + 1,)).fetchall()
        result = {}
        for row in rows:
            receipt = Receipt(*row[:5])
            if self._pending_values(receipt) != row[1:] or receipt.controller in result:
                raise ValueError("invalid selected pending record")
            result[receipt.controller] = receipt
        return result

    def save(self, receipt):
        """Commit archive and acknowledge an exact staged receipt atomically."""
        values = self._values(receipt)
        size = values[-1]
        with self.store.lock, self.store._db(self.store.root / "core.db") as db:
            staged = db.execute("""SELECT receipt_id,controller,command,received_at,payload,payload_bytes
                FROM selected_detail_pending WHERE receipt_id=? OR controller=?""",
                                (receipt.receipt_id, receipt.controller)).fetchall()
            if staged and staged != [(receipt.receipt_id, *values)]:
                raise ValueError("pending selected receipt conflict")
            old = db.execute("""SELECT controller,command,received_at,payload,payload_bytes
                FROM selected_detail_receipts WHERE receipt_id=?""",
                             (receipt.receipt_id,)).fetchone()
            if old is not None:
                if old != values:
                    raise ValueError("selected receipt ID conflict")
            else:
                count, total = db.execute("""SELECT COUNT(*),COALESCE(SUM(payload_bytes),0)
                    FROM selected_detail_receipts""").fetchone()
                if count >= MAX_ROWS or total + size > MAX_BYTES:
                    raise SelectedHistoryFull("selected history capacity")
                db.execute("INSERT INTO selected_detail_receipts VALUES(?,?,?,?,?,?)",
                           (receipt.receipt_id, *values))
            if staged:
                # Only retire the exact queue entry in the same transaction
                # as its archived copy. Failure rolls both changes back.
                db.execute("DELETE FROM selected_detail_pending WHERE receipt_id=?",
                           (receipt.receipt_id,))
        return old is None
