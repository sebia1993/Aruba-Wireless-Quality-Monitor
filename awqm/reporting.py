"""Bounded synthetic Radio history and safe, offline exports.

No operational client identity or raw CLI output is exported. Never infer
service impact from radio samples. Numeric cells retain numeric semantics;
untrusted text is escaped against spreadsheet formula execution and HTML.
"""
import csv,io,html
from datetime import datetime,timezone,timedelta

KST=timezone(timedelta(hours=9))
LIMIT=2000
WINDOWS={"raw":7*86400,"5m":30*86400,"1h":365*86400}
FIELDS=("radio_id","ap_name","band","channel","observed_at","utilization","interference","noise","source")
AGG_FIELDS=("radio_id","band","channel","bucket","observed","valid","risky","utilization_avg","utilization_min","utilization_max","interference_avg","noise_avg","source")

def history_report(store,radio_id,start,end,resolution="raw"):
    if store.mode!="demo":raise ValueError("demo export only")
    if not isinstance(radio_id,str) or not 1<=len(radio_id)<=160:
        raise ValueError("invalid radio id")
    if resolution not in WINDOWS or not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end or end-start>WINDOWS[resolution]:
        raise ValueError("invalid history range")
    if not any(r["radio_id"]==radio_id for r in store.snapshot()["radios"]):
        raise ValueError("unobserved Radio")
    rows=store.history(radio_id,start,end,resolution,LIMIT+1)
    truncated=len(rows)>LIMIT
    rows=rows[:LIMIT]
    if any(r.get("source")!="synthetic" for r in rows):raise ValueError("synthetic observations required")
    fields=FIELDS if resolution=="raw" else AGG_FIELDS
    return {"mode":"demo","field_validated":False,"radio_id":radio_id,"start":start,"end":end,
            "resolution":resolution,"count":len(rows),"limit":LIMIT,"truncated":truncated,
            "fields":list(fields),"rows":[{k:r.get(k) for k in fields} for r in rows],
            "note":"Synthetic observations only. Missing intervals do not indicate recovery or downtime. Service impact and cause not verified."}

def cell(value):
    if value is None:return ""
    if type(value) in (int,float):return value
    s=str(value)
    # Leading whitespace/control characters cannot hide a formula prefix.
    if s.lstrip().startswith(("=","+","-","@")) or s.startswith(("\t","\r","\n")):
        return "'"+s
    return s

def timestamp(t):
    return datetime.fromtimestamp(t,KST).isoformat(timespec="seconds")

def csv_report(report):
    out=io.StringIO(newline="")
    writer=csv.writer(out)
    # Each row retains explicit source, filter metadata, and truncated flag.
    writer.writerow(["mode","field_validated","range_start_kst","range_end_kst","resolution","truncated"]+report["fields"])
    for r in report["rows"]:
        writer.writerow(["demo",False,timestamp(report["start"]),timestamp(report["end"]),report["resolution"],report["truncated"]]+[cell(r[k]) for k in report["fields"]])
    return ("\ufeff"+out.getvalue()).encode("utf-8")

def html_report(report):
    esc=lambda x:html.escape("" if x is None else str(x),quote=True)
    rows="".join("<tr>"+"".join("<td>"+esc(r[k])+"</td>" for k in report["fields"])+"</tr>" for r in report["rows"])
    headers="".join("<th>"+esc(k)+"</th>" for k in report["fields"])
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>AWQM 모의 이력 보고서</title><h1>AWQM 모의 Radio 이력 보고서</h1>"
        "<p>모의 데이터 · 현장 검증 전 · 실제 서비스 영향 및 원인 미확인</p>"
        "<p>Radio: "+esc(report["radio_id"])+"</p><p>조회 범위(KST): "+esc(timestamp(report["start"]))+
        " ~ "+esc(timestamp(report["end"]))+" (종료 시각 제외)</p><p>해상도: "+esc(report["resolution"])+
        " · 관측 행: "+esc(report["count"])+" · 제한 초과: "+esc(report["truncated"])+"</p>"
        "<p>"+esc(report["note"])+"</p><table border='1'><thead><tr>"+headers+"</tr></thead><tbody>"+rows+"</tbody></table></html>").encode("utf-8")
