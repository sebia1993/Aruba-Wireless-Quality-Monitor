# Radio observation incidents (v0.12.0-dev)

Only locally attested, committed Radio observations enter the incident ledger.
Initial risk, persistent risk after three valid observations, recovering,
resolved after three clear observations, transient risk, unconfirmed, and
context lost are distinct. Context lost ends an observation context, not a
service outage. A missing, stale or unapproved observation never proves
recovery; an open incident without current evidence is displayed unconfirmed.
Historical pre-v0.12 incidents are not retroactively synthesized.

RF04 records high utilization as a congestion observation. High interference
or noise alone is UNKNOWN, because actual interferer identity and client
contribution are not verified. No RF05/RF06/RF07 or client attribution is
fabricated. Service impact is always unverified in this implementation.

Core SQLite stores incidents and immutable evidence revisions in the same
transaction as the quality transition. A changed state/candidate or worse
score creates a new revision. Previously recorded reviews stay bound to the
reviewed revision, including historical evidence after recovery. Authenticated
operator/admin can record supported/not-supported/inconclusive with a fixed
reason, exact candidate and request ID. UNKNOWN cannot be marked supported.
The review records actor and timestamp in the same serialized transaction.
No raw free text, client identity or device configuration action is accepted.
Incident details access is separately audited. Browser refresh does not issue
SSH commands. APIs use role/session checks, same-origin and CSRF protection.
The incident list supports page and state filters over up to 365 days; full
incident reports, automatic retention, grouping, free-text operational notes
and other cause codes are pending. No field verification or actual outage
testing was performed by the developer.

## Incident export

Authenticated operators can export the current incident detail as HTML, CSV, or a Korean weekly-report summary (□ / - / *). The report labels observation time separately from service-outage time, retains evidence revision and review decision, and explicitly states that actual equipment action and service impact are unverified. Reviewer IDs and internal Radio MACs are excluded; CSV formula prefixes and HTML text are escaped. A truncated timeline/review is labeled. Downloads and summary requests are audited.


## Previous resolved observation (bounded recurrence context)

The authenticated, audited incident detail now shows at most one preceding persistent incident with explicit RF policy resolution in the 30 days before the selected incident's first observation. It requires the same private Radio identity, exact approval/connection/epoch context, controller and band, and a strictly earlier consistent closure time. Transient closures, context loss, unresolved episodes, changed context, overlapping observations and records outside the window do not qualify. The response returns only the previous incident ID and observed times/gap, not private Radio/context values. The browser provides a separate prior-detail action under the existing authenticated endpoint and the weekly summary labels the relation. No matching record is not proof of a first occurrence. This is bounded retrospective observation context, not a recurrence count, common cause, common infrastructure or verified actual service recovery. No historical evidence is changed, no extra SSH or new permissions. Grouping across APs and full recurrence lifecycle remain open. Exact-candidate CI required.


## Related persistent RF observation links (grouping prerequisite)

The existing locally attested direct RF05/RF06 neighbor path can link the exact peer incident and evidence revision inside historical neighbor evidence. Only distinct AP/Radio identities with verified overlapping channel blocks, aligned persistent congestion and direct timestamped neighbor observations qualify. The single SQLite writer rechecks the source context, exact target Radio/context/controller/band, peer persistent/open state, latest observation and approval/time bounds. Unavailable, changed or inconsistent identities leave the RF candidate without a peer link; no display-name or WLC-only match. No raw Radio/BSSID/context leaves the private lookup. The incident panel exposes related-detail buttons for the selected evidence revision through the existing authenticated audited endpoint. Directional historical evidence only, not common cause, infrastructure confirmation, service outage or automatic parent grouping. No child incident is changed or deleted. Actual synthetic LiveNeighbors/QualityLedger/SQLite tests cover valid identity, repeat, stale and wrong peer context. Full grouping, lifecycle and field validation remain open. Exact new CI required.


## Recorded repeat RF risk within 30 minutes

Approved section 6.1 defines a separate repeat-risk event within 30 minutes of resolution. When a new locally attested risk opens, the existing writer transaction records an explicit first-revision repeat assessment, requiring an earlier persistent RF-policy-resolved episode in the exact Radio/approval/connection/epoch/controller/band context, strictly positive gap no greater than 1800 seconds, and no intervening different committed quality context. The distinct incident is retained; the previous incident is never reopened or merged. The first revision retains only prior incident ID, closure time, gap and fixed relation/uncertainty, or null if no qualifying relation is recorded. No backfill for older incidents: absent assessment is unknown. The detail reads revision one independently of recent timeline truncation; subsequent state changes, repeated telemetry and restart do not recalculate it. Authenticated detail and weekly summary/HTML report show the recorded relationship, not actual service recovery, outage, same cause, or cross-AP grouping. CSV schema is unchanged. No additional SSH, permissions or deletion. Actual synthetic ledger lifecycle, 1800/1801-second boundary, intervening context and Chromium fixture coverage added; exact new CI required.
