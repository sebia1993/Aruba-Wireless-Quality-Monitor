"""D04 offline Channel Summary subset of the documented AOS 8 CLI.

This is not an attested live AP-535 contract. The secondary HT/VHT and
interface sections are not interpreted, and D12 verbose is not implemented.
"""
import re
from .aos8 import cleaned, norm, ParseError
from .collector import AP

COMMAND = "show ap arm rf-summary"
CONTRACT = "aos8-arm-rf-summary-channel-subset-v1"
COLUMNS = "channel retry phy-err mac-err noise util(qual) cov-idx(total) intf_idx(total)"
LAYOUTS = {COLUMNS: "legacy", COLUMNS.replace("channel ", "channel band ", 1): "band",
           "phy-type "+COLUMNS: "phy-type"}
BANDS = ("2.4GHz", "5GHz", "6GHz")
LEGEND = "Columns:util(Qual): ch-util/rx/tx/ext-ch-util/quality"
ECHO = re.compile(r"(?:\([^()\r\n]{1,128}\)(?:\s*\[[^\[\]\r\n]{1,128}\])?(?:\s*\(config\))?\s*#\s*)?show ap arm rf-summary ap-name ([A-Za-z0-9_.-]{1,128})", re.ASCII)
SPLIT_NOTE = ('Noise followed by "*" indicates that the last scan on this channel was a split-scan, which was '
              'triggered by high channel noise. Check show ap arm split-scan-history.')

def _uint(value, maximum=2**64-1):
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value)>maximum:
        raise ParseError("invalid RF summary numeric field")
    return int(value)

def _optional(value):
    return None if value=="N/A" else _uint(value)

def _util(value):
    if value=="N/A":
        return None
    fields=value.split("/")
    if len(fields)!=5:
        raise ParseError("unsupported RF utilization layout")
    return dict(zip(("channel_utilization","rx","tx","extension_channel_utilization","quality"),
                    (_uint(x,100) for x in fields)))

def _index(value, interference):
    if value=="N/A":
        return None
    match=re.fullmatch(r"([0-9]+/[0-9]+//[0-9]+/[0-9]+)\(([0-9]+)\)" if interference else
                       r"([0-9]+/[0-9]+)\(([0-9]+)\)", value)
    if not match:
        raise ParseError("unsupported RF index layout")
    components=[_uint(x) for x in match[1].replace("//","/").split("/")]
    total=_uint(match[2])
    # Keep the reported total and components separate. Do not repair or
    # silently use inconsistent data as evidence.
    return dict(components=components,reported_total=total,
                total_consistent=sum(components)==total,raw=value)

def rf_summary(text, expected_ap):
    if not isinstance(expected_ap,str) or not AP.fullmatch(expected_ap):
        raise ParseError("valid expected AP name required")
    if not isinstance(text,str) or len(text)>512_000:
        raise ParseError("invalid RF summary size")
    lines=[line.strip() for line in cleaned(text).splitlines() if line.strip()]
    if not 6<=len(lines)<=4096 or any(len(line)>1024 or not line.isascii() or
                                    any(ord(c)<32 and c!="\t" for c in line) for line in lines):
        raise ParseError("invalid RF summary layout")
    if any(re.match(r"(?i)^(?:%\s*)?(?:not authorized|authorization failed|unrecognized command)",x) for x in lines):
        raise ParseError("RF summary unavailable")
    echo=ECHO.fullmatch(lines[0])
    if echo:
        if echo[1]!=expected_ap:
            raise ParseError("RF summary command target mismatch")
        lines=lines[1:]
    if len(lines)<6 or norm(lines[0])!="channel summary" or not re.fullmatch(r"-{5,}",lines[1]):
        raise ParseError("RF channel summary missing")
    layout=LAYOUTS.get(norm(lines[2]))
    if layout is None:
        raise ParseError("unsupported RF channel header")
    columns=8 if layout=="legacy" else 9
    if not re.fullmatch(r"-+(?:\s+-+){"+str(columns-1)+r"}",lines[3]):
        raise ParseError("unsupported RF channel separator")
    ends=[i for i,x in enumerate(lines) if norm(x)==norm(LEGEND)]
    if len(ends)!=1 or ends[0]<5:
        raise ParseError("missing or ambiguous RF channel legend")
    end=ends[0]
    if end-4>768:
        raise ParseError("invalid RF channel row count")
    tail=lines[end+1:]
    # Explicitly accept a channel-table-only capture. Recognize the
    # documented optional split-scan note, then only known section starts.
    # The remaining sections are *unparsed*, not validated or complete.
    if tail and tail[0].startswith('Noise followed by "*"'):
        n=next((i for i in range(1,min(len(tail),3)+1)
                if norm(" ".join(tail[:i]))==norm(SPLIT_NOTE)),None)
        if n is None:
            raise ParseError("unsupported RF split-scan note")
        tail=tail[n:]
    if tail and not (norm(tail[0]) in ("ht channel summary","ht/vht channel summary") or
                     re.fullmatch(r"Interface Name\s*:\s*wifi[012]",tail[0])):
        raise ParseError("unsupported RF summary trailing section")
    if any(norm(x)=="channel summary" for x in tail):
        raise ParseError("duplicate RF channel summary")
    rows=[];seen=set()
    for line in lines[4:end]:
        fields=line.split()
        if len(fields)!=columns:
            raise ParseError("unsupported or incomplete RF channel row")
        band=None
        if layout=="phy-type":
            band=fields.pop(0)
        elif layout=="band":
            band=fields.pop(1)
        if band is not None and band not in BANDS:
            raise ParseError("unsupported RF band label")
        ch,retry,phy,mac,noise,util,cov,intf=fields
        channel=_uint(ch,233)
        if channel==0 or (band,channel) in seen:
            raise ParseError("invalid or duplicate RF channel")
        seen.add((band,channel))
        # Preserve CLI noise notation (often unsigned magnitude). No sign
        # conversion or assignment of the scan to a specific physical Radio.
        if noise!="N/A" and not re.fullmatch(r"-?[0-9]{1,3}\*?",noise):
            raise ParseError("unsupported RF noise token")
        rows.append(dict(channel=channel,reported_band=band,retry_raw=_optional(retry),
                         phy_error_raw=_optional(phy),mac_error_raw=_optional(mac),
                         noise_raw=None if noise=="N/A" else noise,
                         split_scan_reported=None if noise=="N/A" else noise.endswith("*"),
                         utilization=_util(util),coverage_index=_index(cov,False),
                         interference_index=_index(intf,True)))
    if not rows or len(rows)>768:
        raise ParseError("invalid RF channel row count")
    return dict(command_id="D04",parser_contract=CONTRACT,expected_ap=expected_ap,
                layout=layout,rows=rows,row_count=len(rows),unparsed_tail_lines=len(tail),
                field_validated=False,target_verified=False,radio_binding_verified=False,
                completeness_verified=False,sample_time_verified=False,
                noise_unit_verified=False,utilization_window_verified=False,
                secondary_sections_verified=False)
