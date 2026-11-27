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
# Fixed reported-value subset. Neither an increasing sequence nor a reported
# reset counter establishes physical measurement time or counter epoch.
BOUNDARY_COUNTERS = ("radio_resets", "tx_timeouts", "rx_crc_errors")
BOUNDARY_HINTS = frozenset(("not_assessed", "reported_decrease",
                            "reported_reset_increase", "no_boundary_observed"))


def counter_boundary_hint(previous, current):
    """Categorical hint only, using exact uint64 values, never deltas/rates."""
    pairs = []
    for name in BOUNDARY_COUNTERS:
        old, new = previous[name], current[name]
        if old is None or new is None:
            continue
        pairs.append((name, old, new))
    if any(new < old for _, old, new in pairs):
        return "reported_decrease"
    if any(name == "radio_resets" and new > old for name, old, new in pairs):
        return "reported_reset_increase"
    if len(pairs) == len(BOUNDARY_COUNTERS):
        return "no_boundary_observed"
    return "not_assessed"


def load_stats_approvals(cfg, radios, now=None):
    return _load_selected_approvals(cfg, radios, now,
                                    "config/radio-stats-validation.json", CONTRACT, CHECKS)


class RadioStats(RFSummary):
    command = COMMAND
    interval = INTERVAL

    def __init__(self, approval):
        super().__init__(approval)
        self._previous = None

    def begin(self, target, monotonic):
        # Capture only the prior live record of this exact requested Radio.
        # A failed attempt clears the live record and the next begin replaces
        # this one-slot private comparison input. No retained second history.
        self._previous = (target, self.records.get(target))
        super().begin(target, monotonic)

    def clear(self):
        super().clear()
        self._previous = None

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
        previous, self._previous = self._previous, None
        hint = "not_assessed"
        if previous is not None and previous[0] == target and previous[1] is not None:
            old_context, old_received, old = previous[1]
            if old_context == context and 0 < received-old_received <= INTERVAL:
                old_counts = {k: None if old["counters"][k] is None
                              else int(old["counters"][k]) for k in BOUNDARY_COUNTERS}
                hint = counter_boundary_hint(old_counts, result["counters"])
        self.records[target] = (context, received, {
            "counter_boundary_hint": hint,
            "counters": {k: None if v is None else str(v)
                         for k, v in result["counters"].items()},
            "radio_percentages": dict(result["radio_percentages"]),
            "observed_field_count": result["observed_field_count"],
            "ignored_row_count": result["ignored_row_count"]})

    def project(self, worker, key, now):
        self._prune_unapproved()
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
