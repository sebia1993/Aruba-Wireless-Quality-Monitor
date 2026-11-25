# Optional WLC resource observation (v0.31)

References:
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-memory.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-datapath.htm
- https://arubanetworking.hpe.com/techdocs/ArubaOS/oxygen/AOS-8%20Consolidated%20User%20Guide/about-aruba-mobility-conductor-virtual-appliance-and-mobility-controller-virtual-appliance/datapath-debug-commands.html

The AOS8 memory command reference describes a summary on Mobility Conductor and recommends technical-support supervision. Actual managed-device support, permissions and layout on the installed 7240XM/AOS8 version must be checked locally before enabling. The Datapath command reports CPU IDs and separate 1s/4s/64s utilization. The AOS8 virtual-appliance guide notes a 64s counter limitation on some processors; do not use 64s values for live conclusions. Both parsers are documentation-shaped, not captured production outputs. The untyped layout is documented for AOS8; the typed layout is only an additional parser candidate requiring actual local confirmation.

Offline probe: AWQM.exe --validate-cli memory local-output.txt or --validate-cli datapath local-output.txt. No SSH. Do not upload site output.

Default disabled. After verification and internal approval create protected config/resource-validation.json with version=1, enabled=true, site=<configured site>, contract="aos8-wlc-resources-v1", controllers=[records]. Each record requires controller, radio_tag (exact Radio approval SHA-256), approved_at and valid_until within the Radio approval lifetime, reviewed_by, cpu_ids (complete verified list of integer datapath CPU IDs), checks (all true only after local verification): memory_layout_and_unit, datapath_layout_and_cpu_inventory, datapath_1s_4s_percent, read_only_support_permission. Absence or {"version":1,"enabled":false} disables the feature. An arbitrary CPU list is not approval.

Only the fixed summary commands show memory and show datapath utilization are permitted; no per-process memory dump, command modifiers or arbitrary CLI. Memory targets 180s and datapath 120s within the existing single command/CPU/CLI budget. Both require a fresh normal CPU protection state, CPU below 70%, and the current same-generation version/Host Key and resource attestation. They are withheld when prerequisites are unavailable. Memory shows reported total/used/free Kb and used fraction, not operating-system available memory. Datapath shows individual 1s/4s percentages and maximum observed CPU values, not a sum or overall device utilization. A complete expected CPU ID set must match; stale, invalid, mismatched or expired data is hidden. No new device failure or RF cause threshold is inferred. The dashboard exposes bounded numeric observations only, no raw CLI or controller management IP. This increment does not persist resource trend history.

Actual hardware support and field semantics remain user-owned; CI checks only fixtures.
