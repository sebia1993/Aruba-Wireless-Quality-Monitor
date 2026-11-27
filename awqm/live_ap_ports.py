"""D07 selected AP port observations using the existing protected worker.

No new generic job admission, web input, connection or inferred uplink.
Local operator attestation is mandatory; absent configuration disables D07.
"""
from dataclasses import dataclass
import json
import time

from .ap_port_status import COMMAND, COUNTERS
from .ap_detail_target import current_ap_binding
from .collector import AP, MAC
from .manual_jobs import bind_ap
from .service_config import safe_path

CONTRACT = "aos8-ap-port-observation-v1"
CHECKS = frozenset(("documented_port_layout", "ap_name_target_scope",
                    "wired_identity_and_active_anchor", "switch_ip_owner",
                    "read_only_permission", "selected_uplink_investigation"))
INTERVAL = 120
ROUND_INTERVAL = 60
PUBLIC_FIELDS = ("port", "admin", "oper", "speed_mbps", "duplex", "stp",
                 "tx_packets", "tx_bytes", "rx_packets", "rx_bytes")
PORT_BOUNDARY_HINTS = frozenset(("not_assessed", "reported_decrease",
                                 "reported_port_context_changed", "no_boundary_observed"))


def _port_context(row):
    # Private, exact reported port identity and link/configuration context.
    # The binding and labels are never added to the public receipt projection.
    return (bind_ap(row["port_mac"]), *(row[k] for k in
            ("port_type", "forward_mode", "admin", "oper", "speed_mbps",
             "duplex", "energy_efficient_ethernet", "poe", "stp")))


def port_boundary_hint(old, current, old_context, context):
    """Conservative comparison of reported uint64 values, never rates/epochs."""
    if old_context != context:
        return "reported_port_context_changed"
    # Do not imply continuity for an inactive or ambiguous link.
    if (current["admin"] != "enabled" or current["oper"] != "up"
            or current["speed_mbps"] is None or current["duplex"] is None):
        return "not_assessed"
    pairs = [(int(old[k]), current[k]) for k in COUNTERS
             if old[k] is not None and current[k] is not None]
    if any(new < prior for prior, new in pairs):
        return "reported_decrease"
    return "no_boundary_observed" if len(pairs) == len(COUNTERS) else "not_assessed"


@dataclass(frozen=True)
class PortApproval:
    radio_tag: str
    approved_at: int
    valid_until: int
    targets: tuple

    def permits(self, tag, at, now):
        return self.radio_tag == tag and self.approved_at <= at <= now < self.valid_until


def load_port_approvals(cfg, radios, now=None):
    return _load_selected_approvals(cfg, radios, now, "config/ap-port-validation.json", CONTRACT, CHECKS)


def _load_selected_approvals(cfg, radios, now, relative, contract, required_checks):
    path = safe_path(cfg.root, relative)
    if not path.exists():
        return {}
    if path.stat().st_size > 64000:
        raise ValueError("AP port approval too large")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc == {"version": 1, "enabled": False}:
        return {}
    if (not isinstance(doc, dict) or set(doc) != {"version", "enabled", "site", "contract", "controllers"}
            or type(doc["version"]) is not int or doc["version"] != 1 or doc["enabled"] is not True
            or doc["site"] != cfg.site or doc["contract"] != contract):
        raise ValueError("invalid AP port approval")
    now = int(time.time()) if now is None else now
    rows = doc["controllers"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= min(4, len(radios)):
        raise ValueError("invalid AP port controllers")
    out, identities = {}, set()
    for row in rows:
        keys = {"controller", "radio_tag", "approved_at", "valid_until", "reviewed_by", "checks", "targets"}
        if not isinstance(row, dict) or set(row) != keys or type(row["controller"]) is not str:
            raise ValueError("invalid AP port controller")
        controller = row["controller"]
        radio = radios.get(controller)
        if not radio or controller in out or row["radio_tag"] != radio.tag:
            raise ValueError("exact Radio approval required")
        at, until = row["approved_at"], row["valid_until"]
        if (type(at) is not int or type(until) is not int
                or not radio.approved_at <= at <= now < until <= radio.valid_until):
            raise ValueError("invalid AP port approval lifetime")
        reviewer = row["reviewed_by"]
        if (type(reviewer) is not str or not 1 <= len(reviewer) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in reviewer)):
            raise ValueError("local reviewer reference required")
        checks = row["checks"]
        if type(checks) is not dict or set(checks) != required_checks or any(v is not True for v in checks.values()):
            raise ValueError("all AP port checks required")
        targets = row["targets"]
        if type(targets) is not list or not 1 <= len(targets) <= 5:
            raise ValueError("select one to five APs per controller")
        selected, names = [], set()
        for target in targets:
            if type(target) is not dict or set(target) != {"ap_name", "wired_mac"}:
                raise ValueError("invalid selected AP")
            name, wired = target["ap_name"], target["wired_mac"]
            if (type(name) is not str or not AP.fullmatch(name) or type(wired) is not str
                    or not MAC.fullmatch(wired)):
                raise ValueError("invalid selected AP identity")
            binding = bind_ap(wired)
            if binding in identities or name in names:
                raise ValueError("duplicate AP port selection")
            identities.add(binding)
            names.add(name)
            selected.append((name, binding))
        out[controller] = PortApproval(radio.tag, at, until, tuple(selected))
    return out


class ApPorts:
    """Called under the worker lock; no independent thread or SSH session."""
    command = COMMAND
    interval = INTERVAL

    def __init__(self, approval):
        self.approval = approval
        self.next_round = float("-inf")
        self.next_ap = {}
        self.records = {}
        self._port_previous = None

    def context(self, worker, target, key, now):
        a, radio = self.approval, worker.field_approval
        if (not worker.live_detail or worker.schedule.mode != "standard" or not radio
                or radio.controller != worker.controller or target not in a.targets
                or not radio.approved_at <= a.approved_at <= now < a.valid_until <= radio.valid_until
                or not a.permits(radio.tag, now, now)):
            return None
        name, binding = target
        if current_ap_binding(worker, name, now) != binding:
            return None
        version = worker.latest.get("show version")
        if (version is None or worker.outcomes.get("show version") is not version
                or version.status != "success" or version.generation != worker.generation
                or not radio.permits(version.data, key, now, now, version.observed_at)):
            return None
        inventory = worker.latest["show ap database long"]
        active = worker.latest["show ap active details"]
        if not all(a.permits(radio.tag, r.observed_at, now) for r in (version, inventory, active)):
            return None
        row = next(r for r in inventory.data if r["name"] == name)
        # Require positive local owner evidence; do not transfer a target
        # automatically to a different controller on failover or rename.
        if row["switch_ip"] != radio.ip:
            return None
        # Private context, never projected or persisted verbatim. Keep both
        # exact immutable approvals and the worker instance/generation; a
        # renewed approval with the same Radio tag is not the old context.
        # Subclasses preserve the first two tuple slots and append Radio
        # descriptors. This is NOT an attested measurement/counter epoch.
        return ((worker.instance_id, worker.generation, radio, a), row["ip"])

    def _prune_unapproved(self, *mappings):
        # RadioStats uses (AP name, binding, Radio ID), other paths use
        # (AP name, binding). Remove withdrawn evidence without changing
        # collection cooldowns or the private archived receipts.
        for mapping in (self.records, *mappings):
            for target in tuple(mapping):
                if target[:2] not in self.approval.targets:
                    mapping.pop(target, None)

    def select(self, worker, key, now, monotonic):
        if monotonic < self.next_round or self.command in worker.detail_disabled:
            return None
        for target in sorted(self.approval.targets, key=lambda t: (self.next_ap.get(t, float("-inf")), t)):
            if monotonic < self.next_ap.get(target, float("-inf")):
                continue
            context = self.context(worker, target, key, now)
            if context is not None:
                return target, context
        return None

    def begin(self, target, monotonic):
        self.next_round = monotonic + ROUND_INTERVAL
        self.next_ap[target] = monotonic + self.interval
        if self.command == COMMAND:
            self._port_previous = (target, self.records.get(target))
        self.records.pop(target, None)

    def record(self, target, context, received, result):
        # One preceding still-retained D07 response; failed attempts consume
        # the old live record, and next begin replaces this private slot.
        previous, self._port_previous = self._port_previous, None
        old_rows, old_contexts = {}, {}
        if previous is not None and previous[0] == target and previous[1] is not None:
            old_ap, old_at, old, contexts = previous[1]
            if old_ap == context and 0 < received-old_at <= INTERVAL:
                old_rows = {row["port"]: row for row in old}
                old_contexts = contexts
        rows, identities = [], {}
        for row in result["rows"]:
            number = row["port"]
            identities[number] = _port_context(row)
            hint = "not_assessed"
            if number in old_rows:
                hint = port_boundary_hint(old_rows[number], row,
                                          old_contexts[number], identities[number])
            clean = {k: str(row[k]) if k in COUNTERS and row[k] is not None else row[k]
                     for k in PUBLIC_FIELDS}
            clean["counter_boundary_hint"] = hint
            rows.append(clean)
        # Port identities are private memory only, not archived or displayed.
        self.records[target] = (context, received, tuple(rows), identities)

    def clear(self):
        self.records.clear()
        self._port_previous = None

    def project(self, worker, key, now):
        self._prune_unapproved()
        rows = []
        for target in self.approval.targets:
            record = self.records.get(target)
            if not record:
                continue
            context = self.context(worker, target, key, now)
            if (COMMAND in worker.detail_disabled or context is None
                    or record[0] != context or not 0 <= now-record[1] <= INTERVAL):
                self.records.pop(target, None)
                continue
            rows.append({"ap_name": target[0], "received_at": record[1],
                         "ports": [dict(r) for r in record[2]],
                         "row_count_verified": False, "sample_time_verified": False,
                         "physical_uplink_verified": False, "field_validated": False})
        return {"configured": True, "selected_count": len(self.approval.targets),
                "rows": rows, "field_validated": False}
