# Optional AP anchor observations (v0.33)

References:
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-standby.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-uac-db.htm

AOS 8 documents both commands, but the published examples are empty, and the standby header is wrapped in the manual. The parser accepts only a complete aligned single-line layout with the documented required columns and count footer. Nonempty real outputs and alternate layouts require actual field verification. Offline: AWQM.exe --validate-cli standby FILE or --validate-cli uac-ap FILE. The optional --detail may contain internal data; do not share it externally.

Default disabled. In protected config/ap-anchor-validation.json, set version=1, enabled=true, site=<configured site>, contract="aos8-ap-anchor-observations-v1", controllers=[records]. Each record requires controller, radio_tag (exact local Radio approval hash), approved_at, valid_until (within Radio approval lifetime), reviewed_by, and checks (all true only after local verification): standby_layout, cluster_role_semantics, uac_ap_database_layout, uac_mac_matches_wired_identity, inventory_identity_mapping, read_only_permission. Absent or {"version":1,"enabled":false} disables collection.

The two read-only commands are scheduled at a 300s target interval under existing CPU, CLI-time, authorization and connection guards. The parser checks row counts and unique identities; UAC DB BSSIDs and status are discarded. A positive observation requires same-generation local inventory and a unique currently mapped physical AP, including name/IP and wired-MAC agreement where provided. Unknown and ambiguous mappings remain unmatched. No Standby readiness, actual failover, client A-UAC/S-UAC, AP Down, or service impact is inferred. Multiple WLC observations are kept separately, not summed as distinct APs. The dashboard shows per-WLC table counts and matching counts, and selected Radio detail shows AP-level observations. Data expires with the approval/observation/inventory freshness. No raw addresses or MACs are published in these views. No new persistent telemetry table.

These are development fixtures, not a substitute for site CLI output, identity/role verification, or actual failover readiness assessment.
