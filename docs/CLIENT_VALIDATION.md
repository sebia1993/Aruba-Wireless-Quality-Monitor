# v0.13.0-dev optional live Client Table

Default disabled. A local operator must verify the actual controller and
AP model's Client Table layout, Health percentage, ACK SNR in dB, last
PHY rate in Mbps, active association and BSS-to-physical-Radio mapping,
read-only permission and approved privacy handling. No developer performed
these checks. Link quality is explicitly partial and not a cause.

Create protected config/client-validation.json only after local validation.
Absent or {"version":1,"enabled":false} disables it. Enabled document:
version=1, enabled=true, site (configured site), contract="aos8-client-table-v1",
controllers (list of records). Each record requires controller, radio_tag
(full SHA-256 tag from the exact Radio approval record), approved_at,
valid_until (Unix seconds within that Radio approval lifetime), automatic
(boolean), reviewed_by (local reference), checks (all eight true after
actual verification): client_table_layout, active_association_mapping,
bss_radio_mapping, client_health_percent, ack_snr_db, last_phy_rate_mbps,
privacy_approval, read_only_permission. No guessed approvals. The initial
implementation supports automatic=true only for actual scheduling; false
does not issue detail commands. Manual live reservation is pending.

Automatic selection uses persistent locally attested Radio congestion,
fresh observations, max 5 APs per WLC and 2 per selection round, 180s
cooldown. Worker independently rechecks current version/key, approval
lifetime, Radio/association/BSS freshness, anchor, CPU and CLI budgets.
Only the allowlisted Client Table command is enabled; no real Station
Stats or Airtime. Safe-profile polls can leave insufficient aligned
metadata; the system defers or hides data rather than inventing values.

Raw detailed CLI is transient in worker/coordinator processing. The client
view retains process-local HMAC pseudonyms and link observations for at
most 180s and 5000 rows, not a durable client database. Pseudonyms are
not anonymous data. Current association, Radio and validation context
must remain valid. No client MAC, BSSID, SSID, counters or username are
returned by the client API. Access is authenticated and audited.
No cross-restart identity or client-history export. Actual live
operation, client session continuity, counter semantics, Airtime and
causal analysis, privacy/retention controls and full field compatibility
remain outstanding.
