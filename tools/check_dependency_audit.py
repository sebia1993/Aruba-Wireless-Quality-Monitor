"""Check the exact freeze against a pip-audit JSON result.

No network activity here. Missing, skipped, duplicate, or mismatched
packages block the gate. No severity or fixability exception.
"""
import argparse,json,re
from pathlib import Path

PIN=re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)$")
def normalize(name):
    return re.sub(r"[-_.]+","-",name).lower()

def evaluate(freeze,report,exit_code):
    pins={};issues=[]
    for line in freeze.splitlines():
        line=line.strip()
        if not line or line.startswith("#"):continue
        m=PIN.fullmatch(line)
        if not m:raise ValueError("exact package version required")
        name=normalize(m[1])
        if name in pins:raise ValueError("duplicate frozen dependency")
        pins[name]=m[2]
    if not pins:raise ValueError("empty package inventory")
    entries=report.get("dependencies") if isinstance(report,dict) else None
    if not isinstance(entries,list):raise ValueError("invalid audit report")
    checked={};skipped=[];vulnerabilities=[]
    for row in entries:
        if not isinstance(row,dict) or not isinstance(row.get("name"),str):raise ValueError("invalid dependency result")
        name=normalize(row["name"])
        if name in checked:raise ValueError("duplicate audited dependency")
        version=row.get("version")
        checked[name]=version
        if row.get("skip_reason") or row.get("skipped"):
            skipped.append(name);continue
        if not isinstance(row.get("vulns"),list):raise ValueError("missing vulnerability results")
        for vuln in row["vulns"]:
            if not isinstance(vuln,dict) or not isinstance(vuln.get("id"),str):raise ValueError("invalid advisory")
            vulnerabilities.append({"package":name,"version":version,"id":vuln["id"],
                "fix_versions":vuln.get("fix_versions",[])})
    missing=sorted(set(pins)-set(checked));unexpected=sorted(set(checked)-set(pins))
    mismatches=sorted(name for name in pins.keys()&checked.keys() if pins[name]!=checked[name])
    if missing:issues.append("missing_packages")
    if unexpected:issues.append("unexpected_packages")
    if mismatches:issues.append("version_mismatch")
    if skipped:issues.append("skipped_packages")
    if vulnerabilities:issues.append("known_vulnerabilities")
    if exit_code!=0:issues.append("scanner_nonzero_exit")
    return {"result":"pass" if not issues else "blocked","frozen_count":len(pins),
        "reported_count":len(checked),"scanner_exit":exit_code,"issues":issues,
        "missing":missing,"unexpected":unexpected,"version_mismatch":mismatches,
        "skipped":sorted(skipped),"vulnerabilities":vulnerabilities,
        "scope":"Python build-environment distributions, known advisories only",
        "binary_native_os_or_runtime_security_validated":False}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("freeze",type=Path);p.add_argument("report",type=Path);p.add_argument("exit_code",type=int);p.add_argument("output",type=Path)
    a=p.parse_args()
    try:
        summary=evaluate(a.freeze.read_text(encoding="utf-8-sig"),
            json.loads(a.report.read_text(encoding="utf-8-sig")),a.exit_code)
    except (OSError,UnicodeError,ValueError,TypeError,KeyError) as exc:
        summary={"result":"blocked","issues":["audit_evidence_unavailable"],"error_type":type(exc).__name__}
    a.output.write_text(json.dumps(summary,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    if summary["result"]!="pass":raise SystemExit(1)
if __name__=="__main__":main()
