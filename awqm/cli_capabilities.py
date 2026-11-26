"""Read-only approved CLI capability inventory and worker evidence.

Catalog membership is not device support, authorization, or semantic
validation. The runtime projection uses only bounded categorical outcomes
from the existing worker, never issues a command or returns a target.
"""
# IDs and templates from approved v1 section 3.1/3.2.
# Entries not implemented in the worker are not a new command allowlist.
CATALOG = (
    ("C01", "show version", "base"),
    ("C02", "show cpuload", "base"),
    ("C03", "show lc-cluster group-membership", "base"),
    ("R01", "show ap radio-summary", "base"),
    ("C04", "show lc-cluster load distribution ap", "base"),
    ("C05", "show lc-cluster load distribution client", "base"),
    ("A01", "show ap active details", "base"),
    ("U01", "show ap association", "base"),
    ("C06", "show datapath utilization", "base"),
    ("C07", "show memory", "base"),
    ("U02", "show user-table", "base"),
    ("B01", "show ap bss-table", "base"),
    ("A02", "show ap standby", "base"),
    ("U03", "show user-table standby", "base"),
    ("A03", "show ap database long", "base"),
    ("D01", "show ap debug client-table", "detail"),
    ("D02", "show ap debug client-stats", "detail"),
    ("D03", "show ap debug radio-stats", "offline"),
    ("D04", "show ap arm rf-summary", "offline"),
    ("D05", "show ap arm neighbors bssid", "detail_variant"),
    ("D06", "show ap monitor ap-list", "pending"),
    ("D07", "show ap port status", "offline"),
    ("D08", "show ap arm history", "offline"),
    ("D09", "show ap client trail-info", "pending"),
    ("D10", "show aaa cluster essid", "pending"),
    ("D11", "show ap debug client-info", "offline"),
    ("D12", "show ap arm rf-summary verbose", "pending"),
    ("C08", "show lc-cluster history", "pending"),
    ("C09", "show lc-cluster heartbeat counters", "pending"),
    ("C10", "show lc-cluster global-events", "pending"),
    ("C11", "show lc-cluster vlan-probe status", "pending"),
    ("V01", "show ap uac-database", "base"),
    ("V02", "show ap bss-table standby", "pending"),
)

# A successful command is a parsed response, not acceptance of metrics.
OUTCOMES = {"success": "parsed_response", "permission_denied": "permission_denied",
            "unsupported": "unsupported", "parse_error": "parse_error",
            "transport_error": "transport_error", "withheld": "withheld"}

def matrix(worker):
    """Called with the worker lock held; no IO, commands or new admission.

    A connection change invalidates previous observation evidence. A
    persistent denial latch is represented separately from that evidence.
    Configuration alone never claims the command is authorized.
    """
    rows = []
    now = int(worker.wall())
    connected = worker.session is not None and not worker.blocked
    for command_id, name, implementation in CATALOG:
        configured = False
        latched = False
        reading = None
        if implementation == "base":
            configured = name in worker.schedule.polls
            latched = name in worker.disabled
            r = worker.outcomes.get(name)
            if r is not None:
                reading = (r.status, r.observed_at, r.generation)
        elif command_id == "D07" and getattr(worker,"ap_ports",None) is not None:
            implementation = "protected_detail"
            configured = True
            latched = name in worker.detail_disabled
            reading = worker.detail_outcomes.get(name)
        elif implementation in ("detail", "detail_variant"):
            latched = name in worker.detail_disabled
            if worker.live_detail:
                if command_id == "D05":
                    configured = bool(worker.neighbor_approval and worker.neighbor_approval.automatic and worker.field_approval)
                else:
                    a = worker.client_approval
                    configured = bool(a and worker.field_approval and
                        (a.automatic or command_id == "D01" and worker.manual_detail) and
                        (command_id != "D02" or worker.station_approval))
            reading = worker.detail_outcomes.get(name)
        outcome = "unobserved"
        observed_at = None
        if (connected and reading is not None and reading[2] == worker.generation
                and 0 <= now - reading[1] <= 660):
            outcome = OUTCOMES.get(reading[0], "unobserved")
            # Existing detail worker reports a single unsupported category
            # for denied, unknown command and station binding failures.
            if implementation in ("detail", "detail_variant") and outcome == "unsupported":
                outcome = "detail_unavailable"
            if outcome != "unobserved":
                observed_at = reading[1]
        rows.append(dict(id=command_id, command=name, implementation=implementation,
                         configured=configured, disabled_latched=latched,
                         observation=outcome, observed_at=observed_at,
                         field_support_verified=False))
    return rows
