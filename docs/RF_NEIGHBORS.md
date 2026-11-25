# Offline RF neighbor contract and correlation foundation

AOS 8.9+ documents phy-type, discovered BSSID, channel, SNR, transmitter
power, path loss, discovery method and last OTA update in ARM Neighbors:
https://arubanetworking.hpe.com/techdocs/ArubaOS/oxygen/AOS-8%20Command%20Line%20Interface%20Guide/s/show-ap-arm-neighbors.html

Locally use AWQM.exe --validate-cli neighbors <saved-output.txt>. Default
prints count only. --detail displays parsed BSSIDs and measurements in the
local console; do not upload corporate output. The parser does not claim
complete row count or verified update time. It drops ESSID rather than
guessing whether it contains the source BSSID. Actual AOS 8.10.0.24 output
and device permissions remain unverified.

ChannelBlock requires verified explicit contiguous 20 MHz constituent
channels. It compares nominal spectral spans for 20/40/80/160 MHz,
including partial 2.4 GHz overlap. It does not infer blocks from suffixes,
80+80, primary channel alone, or country-specific regulatory availability.

The offline correlation primitive requires separately verified direct
receiving-Radio/discovered-BSSID mapping, current device update timestamp,
both aligned valid Radio observations and persistent congestion. Equal
channel blocks may yield RF05, partially overlapping blocks RF06; these
remain medium-confidence candidates, never actual cause or service impact.
Passive/indirect/old or ambiguous evidence does not produce candidates.
There is no RF07 external-interference classification from this parser.
Live neighbor polling, exact block attestation, BSSID mapping and incident
integration are outstanding. No additional real SSH commands enabled.

## v0.18.0-dev optional live investigation

AOS 8 supports the BSSID-scoped ARM neighbor query. It does not by itself prove receiving physical Radio scope. The default remains disabled. Only after independently confirming that exact scope, discovered BSSID mapping, parser layout, discovery semantics, exact channel blocks, device clock and timezone, and read-only permission may the operator configure protected config/neighbor-validation.json. Schema: version=1, enabled=true, site, contract="aos8-bssid-neighbors-v1", controllers (list). Each record requires controller, radio_tag (exact Radio approval SHA-256), approved_at/valid_until (within Radio approval), automatic (boolean), timezone_minutes (verified fixed offset), reviewed_by, checks (all of neighbor_layout, bssid_query_receiving_radio_scope, discovered_bssid_mapping, channel_block_semantics, ota_update_timezone, device_clock_sync, discovery_semantics, read_only_permission), and blocks (list of {"band":"5GHz","raw":"44+","subchannels":[44,48]}-shaped entries verified for the actual deployment). Do not copy example blocks as field approval. Absent or {"version":1,"enabled":false} disables it.

Target BSSID must match an active AP and unique physical Radio and consistent BSS width. Persistent attested risk, fresh Radio, current version/key, target scope, CPU and time budget are rechecked. Max 5 queued/running neighbor jobs per controller, 2 per selection round, 60s round interval and 300s per-Radio cooldown. This shares the existing command budget. Only explicitly scoped read-only commands are used. Source and target require separately valid attested channel blocks before a current RF05/RF06 candidate; passive, stale, conflicting or non-congested observations cannot prove a cause. Live view is bounded/ephemeral, raw BSSID/SSID not stored or exported. Candidate evidence revisions store logical AP/channel and OTA timestamp, not raw BSSID; prior context changes cannot imply service recovery. Neighbor view is authenticated/audited and only reads stored observations. Actual WLC output, load and field semantics remain user verification requirements.
