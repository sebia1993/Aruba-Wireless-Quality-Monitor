"""D04 protected selected AP channel observations, not RF cause evidence."""
from copy import deepcopy

from .arm_rf_summary import COMMAND
from .live_ap_ports import ApPorts, _load_selected_approvals

CONTRACT = "aos8-rf-summary-observation-v1"
CHECKS = frozenset(("documented_channel_layout", "ap_name_target_scope",
                    "wired_identity_and_active_anchor", "switch_ip_owner",
                    "read_only_permission", "selected_rf_investigation"))
INTERVAL = 180
MAX_PUBLIC_ROWS = 64


def load_rf_approvals(cfg, radios, now=None):
    return _load_selected_approvals(cfg, radios, now,
                                    "config/rf-summary-validation.json", CONTRACT, CHECKS)


class RFSummary(ApPorts):
    """Reuse the selected physical AP preflight without sharing approvals."""
    command = COMMAND
    interval = INTERVAL

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
        # Require a unique local physical Radio match; no inference from
        # duplicate display rows, missing metrics, or unknown Radio mapping.
        matched = []
        seen = set()
        for r in candidates:
            claims = [a for a in active["radios"] if a["band"] == r["band"] and a["channel"] == r["channel"]]
            if len(claims) != 1 or claims[0]["radio_id"] in seen:
                return None
            rid = claims[0]["radio_id"]
            seen.add(rid)
            if (all(r[k] is not None for k in ("utilization", "interference", "noise"))
                    and (r["utilization"] >= 75 or r["interference"] >= 30 or r["noise"] > -80)):
                matched.append((rid, r["band"], r["channel"], r["mode"]))
        # This threshold is a dispatch trigger, not a quality/cause decision.
        return (*base, tuple(sorted(matched))) if matched else None

    def record(self, target, context, received, result):
        rows = []
        for row in result["rows"][:MAX_PUBLIC_ROWS]:
            # Numeric fields without verified rate semantics remain raw
            # decimal strings. Never convert a noise magnitude into dBm.
            clean = {k: row[k] for k in ("channel", "reported_band", "noise_raw", "split_scan_reported")}
            for k in ("retry_raw", "phy_error_raw", "mac_error_raw"):
                clean[k] = None if row[k] is None else str(row[k])
            clean["utilization"] = deepcopy(row["utilization"])
            for k in ("coverage_index", "interference_index"):
                idx = row[k]
                clean[k] = None if idx is None else {
                    "components": [str(v) for v in idx["components"]],
                    "reported_total": str(idx["reported_total"]),
                    "total_consistent": idx["total_consistent"]}
            rows.append(clean)
        self.records[target] = (context, received, {
            "layout": result["layout"], "row_count": result["row_count"],
            "truncated": result["row_count"] > len(rows),
            "unparsed_tail_lines": result["unparsed_tail_lines"],
            "channels": rows})

    def project(self, worker, key, now):
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
                         "radio_binding_verified": False, "target_verified": False,
                         "completeness_verified": False, "sample_time_verified": False,
                         "noise_unit_verified": False, "utilization_window_verified": False,
                         "secondary_sections_verified": False})
        return {"configured": True, "selected_count": len(self.approval.targets),
                "rows": rows, "field_validated": False}
