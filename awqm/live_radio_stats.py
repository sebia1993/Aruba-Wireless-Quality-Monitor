"""D03 bounded anomalous-Radio observations; not verified counters or cause."""
from copy import deepcopy
from .live_ap_ports import _load_selected_approvals
from .live_rf_summary import RFSummary

COMMAND = "show ap debug radio-stats"
CONTRACT = "aos8-radio-stats-observation-v1"
CHECKS = frozenset(("documented_radio_statistics_subset", "advanced_command_scope",
                    "physical_radio_id_argument", "wired_identity_and_active_anchor",
                    "switch_ip_owner", "read_only_permission", "selected_radio_investigation"))
INTERVAL = 120


def load_stats_approvals(cfg, radios, now=None):
    return _load_selected_approvals(cfg, radios, now,
                                    "config/radio-stats-validation.json", CONTRACT, CHECKS)


class RadioStats(RFSummary):
    command = COMMAND
    interval = INTERVAL

    def candidates(self, worker, ap, key, now):
        base = super().context(worker, ap, key, now)
        if base is None:
            return []
        active = next(a for a in worker.latest["show ap active details"].data
                      if a["name"] == ap[0] and a["ip"] == base[1])
        ids = [r["radio_id"] for r in active["radios"]]
        if (any(type(rid) is not int or rid not in (0, 1, 2) for rid in ids)
                or len(set(ids)) != len(ids)):
            return []
        return [((*ap, r[0]), (*base[:2], r)) for r in base[2]
                if type(r[0]) is int and r[0] in (0, 1, 2)]

    def context(self, worker, target, key, now):
        if target[:2] not in self.approval.targets:
            return None
        return next((ctx for t, ctx in self.candidates(worker, target[:2], key, now)
                     if t == target), None)

    def select(self, worker, key, now, monotonic):
        if monotonic < self.next_round or COMMAND in worker.detail_disabled:
            return None
        choices = [x for ap in self.approval.targets
                   for x in self.candidates(worker, ap, key, now)]
        for target, context in sorted(choices,
                key=lambda x: (self.next_ap.get(x[0], float("-inf")), x[0])):
            if monotonic >= self.next_ap.get(target, float("-inf")):
                return target, context
        return None

    def record(self, target, context, received, result):
        # Response does not echo target. Uint64 strings preserve precision.
        self.records[target] = (context, received, {
            "counters": {k: None if v is None else str(v)
                         for k, v in result["counters"].items()},
            "radio_percentages": dict(result["radio_percentages"]),
            "observed_field_count": result["observed_field_count"],
            "ignored_row_count": result["ignored_row_count"]})

    def project(self, worker, key, now):
        rows = []
        for target, record in sorted(self.records.items()):
            if (COMMAND in worker.detail_disabled or self.context(worker, target, key, now) != record[0]
                    or not 0 <= now-record[1] <= INTERVAL):
                self.records.pop(target, None)
                continue
            rows.append({"ap_name": target[0], "requested_radio": target[2],
                         "received_at": record[1], **deepcopy(record[2]),
                         "field_validated": False, "target_verified": False,
                         "radio_binding_verified": False, "advanced_layout_verified": False,
                         "completeness_verified": False, "sample_time_verified": False,
                         "counter_epoch_verified": False})
        return {"configured": True, "selected_count": len(self.approval.targets),
                "rows": rows, "field_validated": False}
