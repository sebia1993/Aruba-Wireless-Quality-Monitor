# D07 selected AP wired-port observation (protected service)

The approved v1 section 3.2 defines D07 as a 120-second targeted
uplink investigation. This implementation uses explicit local selection
as the investigation trigger. It does not establish a physical uplink,
device sample time, outage, causation, full table completeness or
native Windows/device acceptance.

Default: no configuration, no command. Only the protected service,
standard collection profile and exact current Radio attestation may use
it. Existing base polls and reserved detail jobs take priority; one
attempt per controller per 60 seconds and per AP per 120 seconds
(including failures), within existing CPU/CLI budgets. No new generic
reservation API or SSH session. Unsupported/denied responses latch
until worker restart.

After the owner verifies output, scope, permissions and identity, an
authorized local administrator may create
ProgramData/AWQM/config/ap-port-validation.json with the protected
configuration (service stopped). Example uses fictional values;
radio_tag must match the local Radio approval. Do not upload real
configuration, identifiers or CLI captures to GitHub.

    {
      "version": 1, "enabled": true, "site": "fixture",
      "contract": "aos8-ap-port-observation-v1",
      "controllers": [{
        "controller": "w1", "radio_tag": "LOCAL_APPROVAL_TAG",
        "approved_at": 1900000000, "valid_until": 1900003600,
        "reviewed_by": "local-approved-reviewer",
        "checks": {
          "documented_port_layout": true,
          "ap_name_target_scope": true,
          "wired_identity_and_active_anchor": true,
          "switch_ip_owner": true,
          "read_only_permission": true,
          "selected_uplink_investigation": true
        },
        "targets": [{"ap_name": "DEMO-AP-001", "wired_mac": "02:00:00:00:00:01"}]
      }]
    }

Absent or {"version":1,"enabled":false} disables D07 on startup.
Maximum five selected APs per controller and four controllers; no
duplicate selected physical identity across controllers. The worker
reuses the shared current AP binding and checks the version/key,
approval lifetime, fresh local inventory and AAC identity, and
Switch IP against the approved controller before and after query.
No automatic transfer or topology inference on failover/rename.
A stale/failed/ambiguous binding, changed session or expired approval
withdraws the observation.

Admin collection status displays bounded port evidence with receive
time, reported admin/oper and speed, duplex/STP and counters.
Counters are exact decimal strings or null to avoid browser uint64
rounding; no computed deltas, raw CLI, MAC or management IP in D07
projection. No additional retained history or changed deletion policy.
No AP Down, confirmed uplink or cause inference.

Remaining: cross-WLC ownership reconciliation, full evidence retention
and correlation before AP02 cause candidates; do not set field checks
true based solely on CI. Actual Windows 11, WLC/AAA and 72-hour operation
remain owner-owned and are not claimed passed.
