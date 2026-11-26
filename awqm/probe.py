"""Offline parser validation entry point. Does not connect to any device."""
import argparse
import json
from pathlib import Path
from .aos8 import radio_summary,inventory,active_details,associations,cpu_load,ParseError

from .client import client_table,station_stats,bss_table
from .field_validation import version_identity
from .rf_neighbors import arm_neighbors
from .cluster import group_membership,ap_load,client_load
from .association_history import user_table
from .controller_resources import memory,datapath
from .ap_anchors import standby,uac_database
from .ap_port_status import port_status
from .radio_stats import radio_stats
from .client_info_airtime import client_info,parse as parse_client_info,inspect as inspect_client_info
PARSERS={"radio":radio_summary,"inventory":inventory,"active":active_details,"association":associations,"cpu":cpu_load,
         "client":client_table,"stats":station_stats,"bss":bss_table,"version":version_identity,"neighbors":arm_neighbors,"cluster":group_membership,"ap-load":ap_load,"client-load":client_load,"users":user_table,"memory":memory,"datapath":datapath,"standby":standby,"uac-ap":uac_database,"client-info-airtime":client_info,"ap-port":port_status,"radio-stats":radio_stats}

def main():
    p=argparse.ArgumentParser(description="Local AOS 8 CLI parser validation (no SSH connection)")
    p.add_argument("kind",choices=PARSERS)
    p.add_argument("file",type=Path)
    p.add_argument("--detail",action="store_true",help="show parsed fields locally; may include sensitive data")
    p.add_argument("--compare-file",type=Path,help="second local client-info-airtime output; no ratio calculation")
    p.add_argument("--ap-name",help="expected AP name for offline ap-port/radio-stats validation")
    p.add_argument("--radio-id",type=int,choices=(0,1,2),help="expected Radio ID for offline radio-stats validation")
    a=p.parse_args()
    if (a.kind in ("ap-port","radio-stats")) != (a.ap_name is not None):p.error("--ap-name is required only for ap-port/radio-stats")
    if (a.kind=="radio-stats") != (a.radio_id is not None):p.error("--radio-id is required only for radio-stats")
    if a.compare_file and a.kind!="client-info-airtime":p.error("--compare-file is only for client-info-airtime")
    try:
        if a.file.stat().st_size>16_000_000:
            raise ParseError("input too large")
        raw=a.file.read_text(encoding="utf-8-sig")
        if a.kind=="client-info-airtime":
            first=parse_client_info(raw)
            second=None
            if a.compare_file:
                if a.compare_file.stat().st_size>16_000_000:raise ParseError("input too large")
                second=parse_client_info(a.compare_file.read_text(encoding="utf-8-sig"))
            # Safe summary even when --detail is supplied: no raw fields,
            # peer IDs, MACs, driver interfaces or counter values.
            print(json.dumps({"kind":a.kind,**inspect_client_info(first,second)},ensure_ascii=False,indent=2))
            return
        if a.kind=="radio-stats":
            result=radio_stats(raw,a.ap_name,a.radio_id)
            report={k:v for k,v in result.items() if k.endswith("_verified") or k in ("field_validated","observed_field_count","ignored_row_count")}
            report.update(status="parsed",kind=a.kind)
            if a.detail:report["observation"]=result
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return
        if a.kind=="ap-port":
            result=port_status(raw,a.ap_name)
            report={"status":"parsed","kind":a.kind,"count":result["row_count"],"field_validated":False,"row_count_verified":False,"physical_uplink_verified":False,"sample_time_verified":False}
            if a.detail:report["observation"]=result
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return
        result=PARSERS[a.kind](raw)
    except (OSError,UnicodeError,ParseError) as exc:
        p.exit(2,"Parser validation failed: "+str(exc)+"\n")
    if isinstance(result,list):
        report={"status":"parsed","kind":a.kind,"count":len(result),"field_validated":False}
        if a.detail:report["rows"]=result
    elif isinstance(result,tuple):
        report={"status":"parsed","kind":a.kind,"model":result[0],"firmware":result[1],"field_validated":False}
    elif isinstance(result,dict):
        report={"status":"parsed","kind":a.kind,"fields":sorted(result),"field_validated":False}
        if a.detail:report["counters"]=result
    else:
        report={"status":"parsed","kind":a.kind,"cpu_percent":result,"field_validated":False}
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":
    main()
