# Optional indirect client retry correlation (v0.27)

Default disabled. This is a low-confidence RF03 investigation candidate,
not causal attribution or a verified victim/cause classification. Existing
Radio, Client Table and Station Stats local field attestations remain
required. Additional privacy approval is required before keeping the
comparison baseline (up to 15 minutes in memory, no database). Only the existing Station Stats command path and shared CLI budget are used.
With the additional approval, a deterministic same-Radio/network cohort of
one poor-link client and two peers is preferred when available. The existing
three-target controller cap and 45-second cooldown remain unchanged. This
can select normal-link peers that were not selected under the old policy.
Sampling remains biased by available AP detail and protection; insufficient
comparable peers suppress the candidate.

Protected config/client-correlation.json:
- absent, or {"version":1,"enabled":false}, disables correlation;
- enabled record: version=1, enabled=true, site, contract="aos8-indirect-retry-correlation-v1", controllers (list).
- each controller record requires controller, station_tag (SHA-256 of
  canonical exact Station Stats attestation, computed by its loader),
  approved_at, valid_until (within station approval lifetime), reviewed_by,
  checks (all explicitly verified true): station_interval_alignment,
  same_radio_peer_comparability, observed_session_continuity,
  baseline_privacy_15_minutes, indirect_candidate_review.

Only current persistent congestion, low partial link quality (<50),
successful-frame retry ratio >=25%, a valid observed station interval
(30–180s, >=100 successful frames) and current aligned Radio/Client Table
samples (within 30 seconds of interval end) are eligible. The two or more
distinct comparison clients must share physical Radio, channel/config
epoch and network, be observed transmitting successfully, and have
at least 80% interval overlap and end times within 30 seconds. Each
must have an observed comparison sample 5–15 minutes earlier in its
same uninterrupted observed station sequence; use the latest eligible
baseline, not a cherry-picked one. At least two peers must exhibit a
Health decline >=20 points or successful-retry rise >=15 points.
The baseline may already be degraded. It is NOT a pre-incident normal
baseline. Inadequate samples, expiry, reconnect/reset/failure, missing
observations and changed Radio context withhold the candidate. These are
observed continuity checks, not proof against unobserved reconnects.

Current candidate and comparison counts only are published through the
existing authenticated client view; no peer identities or internal context
tokens are exposed. Candidate validity is bounded by its constituent
evidence expiration. Baselines are cleared on lost context or approval
and capped per client, retained only for currently observed sequences.
The feature never provides a live Airtime value, impact score, traffic
failure percentage, confirmed cause or service impact. The full cause catalog and live Airtime remain outstanding. Actual
site approval and validation are operator-owned.

## Optional aggregate evidence (v0.28)
The default remains ephemeral only. In an existing approved correlation
controller record, an explicit boolean persist_aggregate=true requires
both the enabled retention policy and additional check
aggregate_evidence_retention_approval=true. This authorizes a Radio-level
historical RF03 evidence revision containing observation time and candidate,
comparison and degradation counts only. The minima describe the observed
candidate set, not necessarily one cohort. No client identifiers, individual
retry/Health/counters or SSID/BSSID values are retained. The first qualifying
summary per Radio observation is retained and existing same-observation
Neighbor evidence is preserved. A new Radio sample does not inherit the
RF03 candidate. Older evidence remains available for explicit review and
reporting, under existing incident/backup retention rules. Review support
means support for the recorded correlation, never confirmed client causality.
