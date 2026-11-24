"""Offline parser validation entry point. Does not connect to any device."""
import argparse
import json
from pathlib import Path
from .aos8 import radio_summary,inventory,active_details,associations,cpu_load,ParseError

from .client import client_table,station_stats,bss_table
from .field_validation import version_identity
from .rf_neighbors import arm_neighbors
PARSERS={"radio":radio_summary,"inventory":inventory,"active":active_details,"association":associations,"cpu":cpu_load,
         "client":client_table,"stats":station_stats,"bss":bss_table,"version":version_identity,"neighbors":arm_neighbors}

def main():
    p=argparse.ArgumentParser(description="Local AOS 8 CLI parser validation (no SSH connection)")
    p.add_argument("kind",choices=PARSERS)
    p.add_argument("file",type=Path)
    p.add_argument("--detail",action="store_true",help="show parsed fields locally; may include sensitive data")
    a=p.parse_args()
    try:
        if a.file.stat().st_size>16_000_000:
            raise ParseError("input too large")
        result=PARSERS[a.kind](a.file.read_text(encoding="utf-8-sig"))
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
