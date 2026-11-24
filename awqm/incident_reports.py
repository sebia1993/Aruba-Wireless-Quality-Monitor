"""Bounded, privacy-minimized incident reports. No service-outage assertion."""
import csv,io,html
from datetime import datetime
from .reporting import KST,timestamp,cell

STATES={"DETECTED":"최초 위험 관측","PERSISTENT":"지속 위험 관측","RECOVERING":"회복 관측 중",
    "RESOLVED":"RF 정책 정상화","TRANSIENT":"일시 위험","UNCONFIRMED":"현재 확인 불가",
    "CONTEXT_LOST":"관측 조건 변경 (정상화 아님)"}
CANDIDATES={"RF04":"Radio 혼잡 관측","UNKNOWN":"원인 미확인","RF05":"동일 채널 경합 후보","RF06":"중첩 채널 경합 후보"}
VERDICTS={"supported":"근거 지지","not_supported":"근거 반대/불충분","inconclusive":"판단 보류"}
REASONS={"radio_evidence":"Radio 관측 근거","other_evidence":"별도 운영자 확인","insufficient_evidence":"자료 부족","conflicting_evidence":"상충 자료"}

def line(value):
    if value is None:return "미확인"
    return " ".join(str(value).split())[:250] or "미확인"

def time_label(value):
    return timestamp(value) if value is not None else "미확인"

def metric(value,unit):
    return "미확인" if value is None else line(value)+unit

def names(evidence):
    return ", ".join(CANDIDATES.get(c["code"],"미확인 후보") for c in evidence.get("candidates",[])) or "원인 미확인"

def summary(detail):
    first=datetime.fromtimestamp(detail["first_at"],KST)
    weekday="월화수목금토일"[first.weekday()]
    e=detail["evidence"]
    state=STATES.get(detail.get("display_state",detail["state"]),"미확인")
    lines=[f'□ {first.month}/{first.day}({weekday}) {line(detail["ap_name"])} {line(detail["band"])} Radio 위험 관측',
        f'- 관측시간 : {time_label(detail["first_at"])} ~ {time_label(detail["last_at"])} (실제 서비스 영향 미확인)',
        '- 내용',f'  * 관측 상태 : {state}',
        f'  * 판단 근거 #{detail["revision"]} ({time_label(e.get("observed_at"))}) : 채널 사용률 {metric(e.get("utilization"),"%")}, 간섭률 {metric(e.get("interference"),"%")}, NF {metric(e.get("noise")," dBm")}',
        f'  * 원인 후보 : {names(e)} (확정 원인 아님)',
        '- 조치내용']
    reviews=detail.get("reviews",[])
    if reviews:
        r=reviews[0]
        current="현재 근거" if r["revision"]==detail["revision"] else "이전 근거"
        lines.append(f'  * 운영자 검증 : {VERDICTS.get(r["verdict"],"미확인")} / {CANDIDATES.get(r["candidate"],"미확인")} ({current} #{r["revision"]}, {time_label(r["at"])})')
    else:lines.append("  * 운영자 검증 기록 없음")
    lines.append("  * 실제 장비 조치 및 서비스 정상화 여부는 기록되지 않음")
    if detail.get("timeline_truncated") or detail.get("reviews_truncated"):
        lines.append("  * 보고서 조회 상한으로 일부 과거 이력은 생략됨")
    return "\n".join(lines)+"\n"

HEADERS=("record_type","incident_id","ap_name","controller","band","revision","observed_at_kst",
    "state","utilization_percent","interference_percent","noise_dbm","quality_score","candidates",
    "review_verdict","review_reason","review_candidate","service_impact","truncated")

def rows(detail):
    common={"incident_id":detail["incident_id"],"ap_name":detail["ap_name"],"controller":detail["controller"],
        "band":detail["band"],"service_impact":"unverified",
        "truncated":bool(detail.get("timeline_truncated") or detail.get("reviews_truncated"))}
    def observation(kind,revision,at,state,e):
        return dict(common,record_type=kind,revision=revision,observed_at_kst=time_label(at),state=state,
            utilization_percent=e.get("utilization"),interference_percent=e.get("interference"),
            noise_dbm=e.get("noise"),quality_score=e.get("quality_score"),
            candidates=",".join(c["code"] for c in e.get("candidates",[])))
    yield observation("summary",detail["revision"],detail["evidence"].get("observed_at"),detail.get("display_state",detail["state"]),detail["evidence"])
    for x in detail["timeline"]:
        yield observation("evidence",x["revision"],x["at"],x["state"],x["evidence"])
    for x in detail["reviews"]:
        yield dict(common,record_type="review",revision=x["revision"],observed_at_kst=time_label(x["at"]),
            review_verdict=x["verdict"],review_reason=x["reason"],review_candidate=x["candidate"])

def incident_csv(detail):
    out=io.StringIO(newline="")
    w=csv.writer(out);w.writerow(HEADERS)
    for row in rows(detail):w.writerow([cell(row.get(k)) for k in HEADERS])
    return ("\ufeff"+out.getvalue()).encode("utf-8")

def incident_html(detail):
    esc=lambda x:html.escape("" if x is None else str(x),quote=True)
    header="".join("<th>"+esc(k)+"</th>" for k in HEADERS)
    body="".join("<tr>"+"".join("<td>"+esc(r.get(k))+"</td>" for k in HEADERS)+"</tr>" for r in rows(detail))
    return ("<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>AWQM Radio 위험 관측 보고서</title><style>body{font:14px system-ui,sans-serif;color:#182331;margin:28px}"
        "pre{white-space:pre-wrap;font:inherit;background:#f5f7fa;padding:16px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #d5dce5;padding:7px;text-align:left}th{background:#edf1f6}.scroll{overflow:auto}</style></head>"
        "<body><h1>Radio 위험 관측 보고서</h1><p>현장 승인된 Radio 지표의 정책 관측 · 실제 서비스 영향 및 원인 미확인</p>"
        "<pre>"+esc(summary(detail))+"</pre><h2>근거 및 검증 이력</h2><div class='scroll'><table><thead><tr>"+header+"</tr></thead><tbody>"+body+
        "</tbody></table></div></body></html>").encode("utf-8")
