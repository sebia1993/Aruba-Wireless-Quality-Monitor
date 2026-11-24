"""Offline parser validation entry point. Does not connect to any device."""
import argparse
import json
from pathlib import Path
from .aos8 import radio_summary,inventory,active_details,associations,cpu_load,ParseError

PARSERS={"radio":radio_summary,"inventory":inventory,"active":active_details,"association":associations,"cpu":cpu_load}

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
    else:
        report={"status":"parsed","kind":a.kind,"cpu_percent":result,"field_validated":False}
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":
    main()
