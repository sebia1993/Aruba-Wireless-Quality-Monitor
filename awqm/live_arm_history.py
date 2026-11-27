"""D08 bounded selected ARM observations, not verified events or causes."""
from copy import deepcopy

from .live_ap_ports import ApPorts, _load_selected_approvals

COMMAND = "show ap arm history"
CONTRACT = "aos8-arm-history-observation-v1"
CHECKS = frozenset(("documented_history_layout", "ap_name_target_scope",
                    "wired_identity_and_active_anchor", "switch_ip_owner",
                    "read_only_permission", "selected_arm_investigation"))
# Conservative developer-side cooldown for the event-triggered spec.
INTERVAL = 180
MAX_EVENTS_PER_AP = 64
CHANGE_WINDOW = 180


def load_arm_approvals(cfg, radios, now=None):
    return _load_selected_approvals(cfg, radios, now,
                                    "config/arm-history-validation.json", CONTRACT, CHECKS)


class ARMHistory(ApPorts):
    command = COMMAND
    interval = INTERVAL

    def __init__(self, approval):
        super().__init__(approval)
        self.previous = {}
        self.changed = {}

    def context(self, worker, target, key, now):
        base = super().context(worker, target, key, now)
        if base is None:
            return None
        radio = worker.latest.get("show ap radio-summary")
        if (radio is None or worker.outcomes.get("show ap radio-summary") is not radio
                or radio.status != "success" or radio.generation != worker.generation
                or not 0 <= now-radio.observed_at <= 90
                or not self.approval.permits(worker.field_approval.tag, radio.observed_at, now)):
            return None
        name, _ = target
        _, ip = base
        active = next(a for a in worker.latest["show ap active details"].data
                      if a["name"] == name and a["ip"] == ip)
        candidates = [r for r in radio.data if r["name"] == name and r["ip"] == ip and r["service"]]
        matched = []
        seen = set()
        for r in candidates:
            claims = [a for a in active["radios"] if a["band"] == r["band"] and a["channel"] == r["channel"]]
            if len(claims) != 1 or claims[0]["radio_id"] in seen:
                return None
            rid = claims[0]["radio_id"]
            if rid not in (0, 1, 2) or r["channel"] is None:
                return None
            seen.add(rid)
            matched.append((rid, r["band"], r["channel"], r["mode"]))
        return (*base, tuple(sorted(matched))) if matched else None

    def observe(self, worker, key, now):
        """Compare only fresh, coherent, distinct local observations.

        An incomplete or conflicting anchor breaks continuity; a missing
        Radio, a band change, and a reconnect do not imply a channel change.
        """
        self._prune_unapproved(self.previous, self.changed)
        for target in self.approval.targets:
            context = self.context(worker, target, key, now)
            if context is None:
                self.previous.pop(target, None)
                self.changed.pop(target, None)
                self.records.pop(target, None)
                continue
            stamp = worker.latest["show ap radio-summary"].observed_at
            old = self.previous.get(target)
            if old is not None:
                prev, previous_stamp = old
                if (prev[:2] != context[:2] or stamp < previous_stamp
                        or stamp-previous_stamp > CHANGE_WINDOW
                        or (stamp == previous_stamp and prev != context)):
                    self.changed.pop(target, None)
                elif stamp > previous_stamp:
                    before = {(rid, band): ch for rid, band, ch, _ in prev[2]}
                    after = {(rid, band): ch for rid, band, ch, _ in context[2]}
                    # Only compare an unchanged set of physical Radio IDs.
                    if before.keys() == after.keys() and any(before[k] != after[k] for k in before):
                        self.changed[target] = (context, stamp)
                    elif prev != context:
                        self.changed.pop(target, None)
            self.previous[target] = (context, stamp)
            change = self.changed.get(target)
            if change and (change[0] != context or not 0 <= now-change[1] <= CHANGE_WINDOW):
                self.changed.pop(target, None)

    def select(self, worker, key, now, monotonic):
        self.observe(worker, key, now)
        if monotonic < self.next_round or COMMAND in worker.detail_disabled:
            return None
        for target in sorted(self.approval.targets, key=lambda t: (self.next_ap.get(t, float("-inf")), t)):
            if monotonic < self.next_ap.get(target, float("-inf")):
                continue
            context = self.context(worker, target, key, now)
            if context is None:
                continue
            radio = worker.latest["show ap radio-summary"]
            name, _ = target
            anomaly = any(r["name"] == name and r["ip"] == context[1] and r["service"]
                          and all(r[k] is not None for k in ("utilization", "interference", "noise"))
                          and (r["utilization"] >= 75 or r["interference"] >= 30 or r["noise"] > -80)
                          for r in radio.data)
            if anomaly or target in self.changed:
                return target, context
        return None

    def begin(self, target, monotonic):
        super().begin(target, monotonic)
        # One attempt consumes a change trigger, even on a failed response.
        self.changed.pop(target, None)

    def record(self, target, context, received, result):
        remaining = MAX_EVENTS_PER_AP
        interfaces = []
        for item in result["interfaces"]:
            events = item["events"][:remaining]
            remaining -= len(events)
            interfaces.append({"interface": item["interface"], "phy_type": item["phy_type"],
                               "table_present": item["table_present"],
                               "event_count": len(item["events"]), "events": deepcopy(events)})
        self.records[target] = (context, received, {
            "interfaces": interfaces, "interface_count": result["interface_count"],
            "event_count": result["event_count"],
            "truncated": result["event_count"] > MAX_EVENTS_PER_AP})

    def clear(self):
        super().clear()
        self.previous.clear()
        self.changed.clear()

    def project(self, worker, key, now):
        self._prune_unapproved(self.previous, self.changed)
        rows = []
        for target in self.approval.targets:
            record = self.records.get(target)
            if not record:
                continue
            if (COMMAND in worker.detail_disabled or self.context(worker, target, key, now) != record[0]
                    or not 0 <= now-record[1] <= INTERVAL):
                self.records.pop(target, None)
                continue
            rows.append({"ap_name": target[0], "received_at": record[1],
                         **deepcopy(record[2]), "field_validated": False,
                         "target_verified": False, "radio_binding_verified": False,
                         "completeness_verified": False, "sample_time_verified": False,
                         "event_time_verified": False})
        return {"configured": True, "selected_count": len(self.approval.targets),
                "rows": rows, "field_validated": False}
