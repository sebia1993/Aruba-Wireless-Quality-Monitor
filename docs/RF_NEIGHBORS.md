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
