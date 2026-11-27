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
