"""D07 AP wired-port evidence: offline, documented AOS 8 layout only.

AOS 8 CLI guide, show ap port status: the live CLI has one long table.
No documented row-count footer or device sample timestamp. A successful
parse therefore cannot establish completeness, physical uplink identity,
link freshness, AP availability, or a root cause. Do not enable live jobs
until the local field/anchor/permission contracts have been implemented.
"""
import re
from .aos8 import cleaned, mac, norm, ParseError
from .collector import AP

COMMAND = "show ap port status"
CONTRACT = "aos8-ap-port-status-v1"
HEADER = ("port mac type forward mode admin oper speed duplex 802.3az poe "
          "stp tx-packets tx-bytes rx-packets rx-bytes")
TITLE = re.compile(r'AP "([A-Za-z0-9_.-]{1,128})" Port Status \(updated every 60 seconds\)')
ROW = re.compile(
    r"(\d{1,3})\s+(\S+)\s+([A-Za-z0-9-]{1,16})\s+(\S+)\s+"
    r"(enabled|disabled)\s+(up|down)\s+"
    r"(N/A|\d{1,5}(?:\.\d{1,3})? (?:Mb/s|Gb/s))\s+"
    r"(full|half|N/A)\s+(\S+)\s+(\S+)\s+(\S+)\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", re.ASCII)
COUNTERS = ("tx_packets", "tx_bytes", "rx_packets", "rx_bytes")

def _counter(value):
    if value == "N/A":
        return None
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) > 2**64-1:
        raise ParseError("invalid port counter")
    return int(value)

def _label(value):
    if value == "N/A":
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", value, re.ASCII):
        raise ParseError("unsupported port label")
    return value

def port_status(text, expected_ap):
    """Return unvalidated observations bound to the requested display name.

    The caller must separately verify the physical AP/anchor and device
    sample age. Counters are reported values, not deltas or rates.
    """
    if not isinstance(expected_ap, str) or not AP.fullmatch(expected_ap):
        raise ParseError("valid expected AP name required")
    if not isinstance(text, str) or len(text) > 128_000:
        raise ParseError("invalid port output size")
    lines = [line.strip() for line in cleaned(text).splitlines() if line.strip()]
    if len(lines) < 5 or len(lines) > 36:
        raise ParseError("missing or oversized port table")
    title = TITLE.fullmatch(lines[0])
    if title is None or title[1] != expected_ap:
        raise ParseError("port table target mismatch")
    if (not re.fullmatch(r"-{5,}", lines[1])
            or norm(lines[2]) != HEADER
            or not re.fullmatch(r"-+(?:\s+-+){14}", lines[3])):
        raise ParseError("unsupported or wrapped port table header")
    seen_ports = set()
    seen_macs = set()
    rows = []
    for line in lines[4:]:
        m = ROW.fullmatch(line)
        if m is None:
            raise ParseError("unsupported or incomplete port row")
        port, address, kind, forward, admin, oper, speed, duplex, az, poe, stp, *counts = m.groups()
        port = int(port)
        address = mac(address)
        if port in seen_ports or address in seen_macs:
            raise ParseError("duplicate port identity")
        seen_ports.add(port)
        seen_macs.add(address)
        speed_mbps = None
        if speed != "N/A":
            number, unit = speed.split()
            # Decimal arithmetic preserves exact integral Mbps; no rounding.
            from decimal import Decimal
            value = Decimal(number) * (1000 if unit == "Gb/s" else 1)
            if not 0 < value <= 100_000 or value != value.to_integral_value():
                raise ParseError("unsupported port speed")
            speed_mbps = int(value)
        rows.append(dict(port=port, port_mac=address, port_type=kind,
                         forward_mode=_label(forward), admin=admin, oper=oper,
                         speed_mbps=speed_mbps, speed_raw=None if speed=="N/A" else speed,
                         duplex=None if duplex=="N/A" else duplex,
                         energy_efficient_ethernet=_label(az), poe=_label(poe),
                         stp=_label(stp), **dict(zip(COUNTERS, map(_counter, counts)))))
    return dict(command_id="D07", parser_contract=CONTRACT, ap_name=expected_ap,
                rows=rows, row_count=len(rows), row_count_verified=False,
                field_validated=False, sample_time_verified=False,
                physical_uplink_verified=False, reported_update_interval_seconds=60)
