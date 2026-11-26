"""D03 offline evidence subset, not an attested advanced-output contract.

The AOS 8 reference documents a RADIO Stats parameter/value table and the
advanced field names. It does not establish a complete AP-535/8.10 layout,
the counter epoch, or a target echo. No live scheduler or cause integration.
"""
import re
from .aos8 import cleaned, norm, ParseError
from .collector import AP

CONTRACT = "aos8-radio-stats-offline-subset-v1"
SECTIONS = ("general per-radio statistics", "transmit specific statistics",
            "received statistics")
# Exact normalized labels, scoped to their documented section. Do not
# combine similarly named basic and advanced counters or infer missing ones.
COUNTERS = (
    {"total radio resets": "radio_resets", "resets beacon fail": "beacon_fail_resets",
     "resets tx timeouts": "tx_timeout_resets", "resets fatal intr": "fatal_intr_resets",
     "tx power changes": "tx_power_changes", "channel changes": "channel_changes",
     "radio band changes": "radio_band_changes"},
    {"tx timeouts": "tx_timeouts", "lost carrier events": "lost_carrier_events",
     "frames transmitted": "frames_transmitted_basic",
     "tx frames transmitted": "tx_frames_transmitted", "tx frames dropped": "tx_frames_dropped",
     "tx success with retry": "tx_success_with_retry", "tx mgmt frames": "tx_mgmt_frames",
     "tx data frames": "tx_data_frames", "tx mgmt bytes": "tx_mgmt_bytes",
     "tx data bytes": "tx_data_bytes", "tx missed acks": "tx_missed_acks"},
    {"rx frames received": "rx_frames_received", "rx good frames": "rx_good_frames",
     "rx bad frames": "rx_bad_frames", "rx total data frames recvd": "rx_total_data_frames",
     "rx total mgmt frames recvd": "rx_total_mgmt_frames", "rx total data bytes recvd": "rx_total_data_bytes",
     "rx data frames": "rx_data_frames", "rx mgmt frames": "rx_mgmt_frames",
     "rx crc errors": "rx_crc_errors", "rx plcp errors": "rx_plcp_errors",
     "rx radar events": "rx_radar_events", "rx frames dropped": "rx_frames_dropped"},
)
PERCENTAGES = (
    {},
    {"tx time perct @ beacon intvl": "tx_beacon_interval_percent"},
    {"channel busy 1s": "channel_busy_1s_percent", "channel busy 4s": "channel_busy_4s_percent",
     "channel busy 64s": "channel_busy_64s_percent",
     "ch busy perct @ beacon intvl": "channel_busy_beacon_interval_percent",
     "rx time perct @ beacon intvl": "rx_beacon_interval_percent"},
)
SECTION = re.compile(r"-{5,}\s+(.+)")
NUMBER = re.compile(r"[+-]?[0-9]{1,20}(?:\.[0-9]{1,6})?", re.ASCII)
ROW = re.compile(r"([A-Za-z0-9][A-Za-z0-9 ._/@+\-()[\]*%:]{0,159})\s+(\S+)", re.ASCII)
ALL_LABELS = set().union(*COUNTERS, *PERCENTAGES)

def radio_stats(text, expected_ap, expected_radio):
    """Extract a bounded subset from a local capture.

    The expected target is caller-supplied metadata, never proven by the
    payload. Percentages retain their own windows, not client Airtime.
    """
    if not isinstance(expected_ap, str) or not AP.fullmatch(expected_ap):
        raise ParseError("valid expected AP name required")
    if type(expected_radio) is not int or expected_radio not in (0, 1, 2):
        raise ParseError("valid expected Radio ID required")
    if not isinstance(text, str) or len(text) > 512_000:
        raise ParseError("invalid radio statistics size")
    lines = [line.strip() for line in cleaned(text).splitlines() if line.strip()]
    if not 7 <= len(lines) <= 4096 or any(len(line) > 256 or not line.isascii() or
                                         any(ord(c) < 32 and c != "\t" for c in line) for line in lines):
        raise ParseError("invalid radio statistics layout")
    if (norm(lines[0]) != "radio stats" or not re.fullmatch(r"-{5,}", lines[1])
            or norm(lines[2]) != "parameter value" or not re.fullmatch(r"-+\s+-+", lines[3])):
        raise ParseError("unsupported radio statistics header")
    section = -1
    seen = set()
    counts = {v: None for group in COUNTERS for v in group.values()}
    percentages = {v: None for group in PERCENTAGES for v in group.values()}
    ignored = 0
    observed = 0
    for line in lines[4:]:
        match = SECTION.fullmatch(line)
        if match:
            section += 1
            if section >= len(SECTIONS) or norm(match[1]) != SECTIONS[section]:
                raise ParseError("unsupported or duplicate radio statistics section")
            continue
        if section < 0:
            raise ParseError("radio statistics row outside section")
        if norm(line) in ALL_LABELS:
            raise ParseError("missing radio statistics value")
        row = ROW.fullmatch(line)
        if row is None:
            raise ParseError("unsupported radio statistics row")
        label, value = norm(row[1]), row[2]
        if label not in ALL_LABELS:
            # Unmapped numeric rows are not evidence; reject non-numeric
            # layouts rather than swallowing error strings or new tables.
            if NUMBER.fullmatch(value) is None and value != "N/A":
                raise ParseError("unsupported unmapped radio statistics row")
            ignored += 1
            continue
        if label in seen or label not in COUNTERS[section] and label not in PERCENTAGES[section]:
            raise ParseError("duplicate or misplaced radio statistics field")
        seen.add(label)
        observed += 1
        if label in COUNTERS[section]:
            if value != "N/A":
                if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) > 2**64-1:
                    raise ParseError("invalid radio counter")
                counts[COUNTERS[section][label]] = int(value)
        elif value != "N/A":
            if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,6})?", value) or not 0 <= float(value) <= 100:
                raise ParseError("invalid radio percentage")
            percentages[PERCENTAGES[section][label]] = float(value)
    if section != len(SECTIONS)-1 or counts["radio_resets"] is None or not any(
            counts[v] is not None or percentages.get(v) is not None
            for v in COUNTERS[1].values()) or not (
            any(counts[v] is not None for v in COUNTERS[2].values()) or
            any(percentages[v] is not None for v in PERCENTAGES[2].values())):
        raise ParseError("insufficient radio statistics evidence")
    return dict(command_id="D03", parser_contract=CONTRACT,
                expected_ap=expected_ap, expected_radio=expected_radio,
                counters=counts, radio_percentages=percentages,
                observed_field_count=observed, ignored_row_count=ignored,
                field_validated=False, completeness_verified=False,
                target_verified=False, advanced_layout_verified=False,
                sample_time_verified=False, counter_epoch_verified=False)
