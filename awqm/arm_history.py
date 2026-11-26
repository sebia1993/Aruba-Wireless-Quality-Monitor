"""D08 offline ARM history evidence; no live command or incident integration.

The AOS 8 reference shows the older six-column table and >=8.9 PHY
headers. Neither establishes a complete AP-535/8.10 capture contract.
Keep CLI-reported relative age and raw channel notation, not event times.
"""
import re
from .aos8 import cleaned, norm, ParseError
from .collector import AP

CONTRACT = "aos8-arm-history-offline-subset-v1"
INTERFACE = re.compile(r"Interface\s*:\s*wifi([012])", re.ASCII)
PHY = re.compile(r"Phy-Type\s*:\s*(2\.4GHz|5GHz|6GHz)", re.ASCII)
ECHO = re.compile(r"(?:\([^()\r\n]{1,128}\)(?:\s*\[[^\[\]\r\n]{1,128}\])?(?:\s*\(config\))?\s*#\s*)?show ap arm history ap-name ([A-Za-z0-9_.-]{1,128})", re.ASCII)
REASONS = frozenset(("I","R","N","Q","E","INV","G","M","P+","P-",
                     "40INT","NO40INT","OFF","OFF(R)","OFF(MA)","ON","D",
                     "AIRMATCH","I*","C","NC","Random","RMC","RCP","OFF(C)"))
LEGACY_LEGEND = (
    "I: Interference, R: Radar detection, N: Noise exceeded, E: Error threshold exceeded, "
    "INV: Invalid Channel, G: Rogue AP Containment, M: Empty Channel, "
    "P+: Increase Power, P-: Decrease Power, OFF: Turn off Radio, ON: Turn on Radio"
)
MODERN_LEGEND = (
    "I: Interference, R: Radar detection, N: Noise exceeded, Q: Bad Channel Quality E: Error threshold exceeded, "
    "INV: Invalid Channel, G: Rogue AP Containment, M: Empty Channel, P+: Increase Power, P-: Decrease Power, "
    "40INT: 40MHZ intol detected on 2.4G, NO40INT: 40MHz intol cleared on 2.4G, "
    "OFF(R): Turn off Radio due to Radar, OFF(MA): Turn off Radio due to Mode Aware, ON: Turn on Radio, "
    "D: Dynamic Bandwidth Switch, AIRMATCH: AirMatch Event, I*: CCA Interference, C: Radar cleared, "
    "NC: Noise Cleared, Random: Random Channel, RMC: Radio Mode Change, "
    "RCP: Radio Client Preference Change, OFF(C) : Turn off Radio due to config"
)
def _legend(s):
    return re.sub(r"\s*([,:])\s*", r"\1", norm(s))

def _channel(token):
    if token in ("N/A", "-"):
        return None
    # Preserve reported syntax, including zero and bandwidth suffix; not
    # proof of a valid channel or operating band.
    if not re.fullmatch(r"[0-9]{1,3}[+\-ES]?", token, re.ASCII) or int(re.match(r"[0-9]+", token)[0]) > 233:
        raise ParseError("invalid ARM channel token")
    return token

def _power(token):
    if token in ("N/A", "-"):
        return None
    if not re.fullmatch(r"-?[0-9]{1,3}(?:\.[0-9]{1,3})?", token, re.ASCII):
        raise ParseError("invalid ARM power token")
    # Preserve source value; do not infer a unit or measured RF power.
    return token

def _age(token):
    m = re.fullmatch(r"([0-9]{1,5})d:([0-9]{1,2})h:([0-9]{1,2})m:([0-9]{1,2})s", token, re.ASCII)
    if not m:
        raise ParseError("invalid ARM relative age")
    d,h,mn,s = map(int,m.groups())
    if h >= 24 or mn >= 60 or s >= 60:
        raise ParseError("invalid ARM relative age")
    return ((d*24+h)*60+mn)*60+s

def arm_history(text, expected_ap):
    if not isinstance(expected_ap,str) or not AP.fullmatch(expected_ap):
        raise ParseError("valid expected AP name required")
    if not isinstance(text,str) or len(text)>512_000:
        raise ParseError("invalid ARM history size")
    lines=[line.strip() for line in cleaned(text).splitlines() if line.strip()]
    if not 2<=len(lines)<=4096 or any(len(line)>1024 or not line.isascii() or
                                    any(ord(c)<32 and c!="\t" for c in line) for line in lines):
        raise ParseError("invalid ARM history layout")
    echo=ECHO.fullmatch(lines[0])
    if echo:
        if echo[1]!=expected_ap:
            raise ParseError("ARM history command target mismatch")
        lines=lines[1:]
    # Match the documented reason legend as terminal layout marker. A
    # recognized legend is not a count footer or proof of full history.
    tail=next((i for i,line in enumerate(lines) if line.startswith("I: Interference,")),None)
    if tail is None or _legend(" ".join(lines[tail:])) not in {
            _legend(LEGACY_LEGEND), _legend(MODERN_LEGEND)}:
        raise ParseError("missing or unsupported ARM reason legend")
    body=lines[:tail]
    interfaces=[]
    seen=set()
    i=0
    while i<len(body):
        match=INTERFACE.fullmatch(body[i])
        if not match or match[1] in seen:
            raise ParseError("missing or duplicate ARM interface")
        seen.add(match[1])
        interface={"interface":"wifi"+match[1],"phy_type":None,"table_present":False,"events":[]}
        i+=1
        if i<len(body):
            phy=PHY.fullmatch(body[i])
            if phy:
                interface["phy_type"]=phy[1]
                i+=1
        if i<len(body) and norm(body[i])=="arm history":
            if (i+3>=len(body) or not re.fullmatch(r"-{5,}",body[i+1]) or
                norm(body[i+2])!="reason old channel new channel old power new power last change" or
                not re.fullmatch(r"-+(?:\s+-+){5}",body[i+3])):
                raise ParseError("unsupported ARM history table")
            interface["table_present"]=True
            i+=4
            while i<len(body) and not INTERFACE.fullmatch(body[i]):
                fields=body[i].split()
                if len(fields)!=6 or fields[0] not in REASONS:
                    raise ParseError("unsupported ARM history row")
                reason,old_ch,new_ch,old_power,new_power,age=fields
                interface["events"].append(dict(reason_code=reason,
                    old_channel=_channel(old_ch),new_channel=_channel(new_ch),
                    old_power=_power(old_power),new_power=_power(new_power),
                    reported_age_seconds=_age(age)))
                i+=1
        elif interface["phy_type"] is None:
            raise ParseError("ARM interface has neither PHY nor table")
        interfaces.append(interface)
    if not interfaces or len({x["phy_type"] is None for x in interfaces})!=1:
        raise ParseError("missing or mixed ARM interface layout")
    return dict(command_id="D08",parser_contract=CONTRACT,expected_ap=expected_ap,
                interfaces=interfaces,interface_count=len(interfaces),
                event_count=sum(len(x["events"]) for x in interfaces),
                field_validated=False,target_verified=False,radio_binding_verified=False,
                completeness_verified=False,sample_time_verified=False,event_time_verified=False)
