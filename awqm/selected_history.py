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


def _radio_history_detail(obs):
    def counter(value):
        if value is None:
            return None
        if (type(value) is not str or re.fullmatch(r"[0-9]{1,20}", value) is None
                or int(value) > 2**64-1):
            raise ValueError("invalid historical Radio counter")
        return value

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
    return {"reported_counters": {key: counter(counters[key])
                                 for key in D03_HISTORY_COUNTERS},
            "reported_busy_windows": {key: percentage(busy[key])
                                      for key in D03_HISTORY_BUSY}}


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
            summary = {"retained_port_rows": len(sequence(obs["ports"], 32))}
        elif receipt.command == RF:
            retained = len(sequence(obs["channels"], 64))
            summary = {"retained_channel_rows": retained,
                       "reported_channel_rows": integer(obs["row_count"], retained, 768)}
        elif receipt.command == ARM:
            interfaces = sequence(obs["interfaces"], 3)
            retained = sum(len(sequence(item["events"], 64)) for item in interfaces)
            summary = {"retained_event_rows": integer(retained, 0, 64),
                       "reported_event_rows": integer(obs["event_count"], retained, 4096),
                       "retained_interfaces": len(interfaces)}
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
            CREATE INDEX IF NOT EXISTS selected_detail_receipt_time
                ON selected_detail_receipts(controller,received_at);
            CREATE INDEX IF NOT EXISTS selected_detail_receipt_size
                ON selected_detail_receipts(payload_bytes);
            CREATE INDEX IF NOT EXISTS selected_detail_receipt_query
                ON selected_detail_receipts(controller,command,received_at,receipt_id);
            """)

    def query(self, controller, command, start, end, *, limit=20, after=None):
        """Private bounded receipt-time query; caller must authorize disclosure.

        Not an observation-time query, a coverage statement or a public API.
        Keyset order is ascending (received_at, receipt_id), with [start,end).
        Separate requests are not a database snapshot: concurrent late inserts
        before the cursor may be missed. No count, raw CLI or device access.
        """
        if (type(controller) is not str or not 1 <= len(controller) <= 128
                or type(command) is not str or command not in COMMANDS
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= MAX_TIMESTAMP
                or end - start > MAX_QUERY_SECONDS
                or type(limit) is not int or not 1 <= limit <= MAX_QUERY_ROWS):
            raise ValueError("invalid selected history query")
        params = [controller, command, start, end]
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

    def save(self, receipt):
        """Idempotent exact replay, serialized with other core DB writes."""
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
        values = (receipt.controller, receipt.command, receipt.received_at,
                  receipt.payload, size)
        with self.store.lock, self.store._db(self.store.root / "core.db") as db:
            old = db.execute("""SELECT controller,command,received_at,payload,payload_bytes
                FROM selected_detail_receipts WHERE receipt_id=?""",
                             (receipt.receipt_id,)).fetchone()
            if old is not None:
                if old != values:
                    raise ValueError("selected receipt ID conflict")
                return False
            count, total = db.execute("""SELECT COUNT(*),COALESCE(SUM(payload_bytes),0)
                FROM selected_detail_receipts""").fetchone()
            if count >= MAX_ROWS or total + size > MAX_BYTES:
                raise SelectedHistoryFull("selected history capacity")
            db.execute("INSERT INTO selected_detail_receipts VALUES(?,?,?,?,?,?)",
                       (receipt.receipt_id, *values))
        return True
