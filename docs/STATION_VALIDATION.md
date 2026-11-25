# v0.15.0-dev — optional Station Stats interval

Default disabled. Verify actual firmware/target association output and
Station Stats counters independently after Client Table attestation.
Aruba references:
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-assoc.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-dbg-clt-st.htm

Protected config/station-validation.json schema: version=1, enabled=true,
site, contract="aos8-station-success-retry-v1", controllers list. Each
record requires controller, client_tag (SHA-256 of exact Client Table
attestation), approved_at, valid_until (within client approval lifetime),
reviewed_by and checks (all explicitly true after local verification):
station_layout, counter_scope, successful_retry_subset,
association_age_seconds, association_count_continuity, counter_reset_behavior,
targeted_association_permission, privacy_approval.
Absent or {"version":1,"enabled":false} disables it. No generated approval.
Client and Radio approval tags use canonical sorted JSON; calculate locally
from the exact protected configuration records, do not share records.

Automatic low-link-quality investigation has at most three queued/running
Station Stats targets per WLC, 45-second per-target cooldown, and a 60-second
job deadline. Each execution requires current Radio/association/BSS
metadata and CPU/CLI protection. One sequential job uses targeted
association, Station Stats, targeted association under the same <=10s
detail time budget. Unsupported/denied command disables this path for the
worker. Only document-shaped labeled association time is accepted. AID,
association count, BSSID/SSID and age progression must agree across the
bracket and between samples; counter resets and gaps invalidate deltas.
This is observed session continuity, not a guarantee against all
unobserved reconnects or device-counter changes.

Ratio = delta successful-after-retry / delta successfully transmitted *
100, with >=100 successful frames and nonnegative consistent deltas.
This is NOT overall retry attempts, failure rate, full activity, or
Airtime. No successful-Tx increase does not imply an idle client. Separate
Station Stats interval, no mixing with Client Table partial quality.
Temporary counters and pseudonyms stay in memory. Raw target job receipts
are removed after terminal execution; no live client data history or
report exports. Stale or expired values are hidden.

airtime_contract.py adds only an offline numerical contract for already
verified counter units/scope/context. Counter time exceeding the observation
interval is rejected. No actual Aruba Airtime field or source is enabled,
and a last PHY rate cannot substitute for measured occupancy. Client
causality, real deployment validation and full privacy lifecycle remain
outstanding.
