"use strict";
const $=id=>document.getElementById(id);
const names={overview:"종합 현황",radios:"AP 품질 관측",clients:"사용자 분석",history:"장애 및 이력",admin:"운영 관리",results:"통합 검색 결과"};
let view="overview",page=1,snapshot=null,selected=null,busy=false,revision=0,expired=false;
let updateSocket=null,updateTimer=null,lastSignal=0,lastAutomatic=0,lastPublication=null;
const detailRoute=/^#(radio|incident|client)\/([0-9a-f]{32})$/.exec(location.hash);
const kst=t=>t==null?"미확인":new Date(t*1000).toLocaleString("ko-KR",{timeZone:"Asia/Seoul"});
function el(tag,value,cls){const n=document.createElement(tag);if(value!=null)n.textContent=String(value);if(cls)n.className=cls;return n;}
function table(headers,rows){const wrap=el("div",null,"table-wrap"),t=el("table"),head=el("thead"),tr=el("tr");for(const h of headers)tr.append(el("th",h));head.append(tr);t.append(head);const b=el("tbody");for(const row of rows){const r=el("tr");for(const value of row){const c=el("td");if(value instanceof Node)c.append(value);else c.textContent=value??"미확인";r.append(c);}b.append(r);}t.append(b);wrap.append(t);return wrap;}
function metric(n,v){const b=el("div",null,"kpi");b.append(el("span",n),el("strong",v??"미확인"));return b;}
function notice(s){$("notice").textContent=s;}
function invalidate(){expired=true;closeUpdates();snapshot=null;selected=null;incidentPanel=null;submittedSearch="";refreshSearchClients=null;for(const id of ["search","global-query"]){$(id).value="";$(id).disabled=true;}document.body.classList.remove("detail-page");$("content").replaceChildren(el("p","인증 만료. 새로고침 후 다시 로그인하세요."));$("detail").hidden=true;$("detailBody").replaceChildren();notice("인증 세션 만료");}
async function request(path){const r=await fetch(path,{cache:"no-store"});if(r.status===401){invalidate();throw Error("expired");}if(!r.ok)throw Error("unavailable");return r.json();}
function neighborPanel(r){
  const panel=el("section");panel.append(el("h3","RF Neighbor 관측"),el("p","논리적 RF 관계 · 물리 위치 또는 실제 간섭 원인 확정 아님"));
  const b=el("button","저장된 Neighbor 관측 조회"),out=el("div");b.addEventListener("click",async()=>{
    const wanted=selected;if(!wanted||expired)return;b.disabled=true;
    try{
      const d=await request("/api/neighbors?"+new URLSearchParams({id:r.id}));
      if(selected!==wanted||expired)return;
      out.replaceChildren(el("p",!d.enabled?"RF Neighbor 수집 비활성 · 별도 현장 검증 필요":!d.available?"현재 유효한 Neighbor 관측 없음":"관측 "+kst(d.observed_at)+" · "+d.rows.length+"건"+(d.truncated?" · 조회 상한 도달":"")));
      if(d.rows.length){
        const labels={same_block:"동일 블록",overlapping_block:"중첩",disjoint:"비중첩",different_band:"다른 대역",unknown:"미확인"};
        out.append(table(["이웃 AP","대역","채널","폭 MHz","SNR","탐지 방식","OTA 관측 시각","채널 관계","원인 후보"],d.rows.map(x=>[x.ap_name||"미등록 관측 "+x.neighbor_id.slice(0,8),x.band,x.channel,x.width,x.snr,x.discovery,kst(x.neighbor_at),labels[x.relation]||x.relation,candidateLabels[x.candidate]||"미확인"])));
      }
    }catch(e){if(!expired&&selected===wanted)out.textContent="현재 Neighbor 조회 불가";}
    finally{b.disabled=false;}
  });panel.append(b,out);return panel;
}


const manualAttempts=new Map();
const jobLabels={queued:"접수 완료 · 대기",deferred:"보호 정책 대기",running:"CLI 조회 중",completed:"CLI 조회 완료",failed:"조회 실패",cancelled:"예약 취소",expired:"예약 만료",skipped:"조회 불가",receipt_unavailable:"결과 확인 불가"};
const jobReasons={collection_paused:"수집 일시정지",maintenance_context_changed:"점검 전 예약 종료",service_instance_changed:"서비스 인스턴스 변경 · 새로고침 필요",manual_detail_disabled:"수동 조사 비활성",current_attested_congestion_required:"현재 검증된 지속 혼잡 Radio 필요",observation_unavailable:"현재 관측 확인 불가",client_attestation_unavailable:"단말 지표 검증 필요",command_unavailable:"지원하지 않는 조회 명령",controller_ap_queue_full:"WLC 대기열 가득 참",ap_cooldown:"동일 AP 재조회 대기",queue_not_accepted:"예약 접수 불가",shared_job_not_cancellable:"공유 예약은 취소할 수 없음",receipt_capacity_reached:"접수 이력 상한 도달",service_stopping:"서비스 종료 중"};
async function jobPost(action,data){
  const csrf=document.querySelector("input[name=csrf]").value;
  const r=await fetch("/api/manual-jobs/"+action,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams({...data,csrf}),cache:"no-store"});
  if(r.status===401){invalidate();throw Error("expired");}
  if(r.status===409){const d=await r.json();return {rejected:true,error:d.error};}
  if(!r.ok)throw Error("unconfirmed");
  return r.json();
}
function jobsPanel(r=null){
  const panel=el("section"),out=el("div"),message=el("p"),refresh=el("button","내 예약 현황 조회"),reserve=el("button","Client Table 정밀 조회 예약");
  let epoch=null,busy=false;
  function show(result){return (jobLabels[result.status]||result.status)+(result.joined?" · 기존 작업 연결":"")+(result.queue_position?" · 상세 대기 순서 "+result.queue_position:"")+(result.reason?" · "+(jobReasons[result.reason]||result.reason):"");}
  async function load(){
    if(busy||expired)return;
    busy=true;refresh.disabled=true;reserve.disabled=true;
    try{
      const d=await request("/api/manual-jobs");
      if(expired||!panel.isConnected)return;
      epoch=d.epoch;
      out.replaceChildren(el("p",d.enabled?"이번 서비스 실행 중 내 예약 "+d.total+"건 · CLI 완료는 분석 성공과 별개":"수동 예약 비활성"));
      if(d.enabled&&d.rows.length){
        out.append(table(["AP","WLC","예약 ID","상태","접수 시각","취소"],d.rows.map(x=>{
          const b=el("button","예약 취소");b.disabled=!x.can_cancel;
          b.addEventListener("click",async()=>{b.disabled=true;
            try{const result=await jobPost("cancel",{epoch:d.epoch,receipt_id:x.receipt_id});
              message.textContent=result.rejected?(jobReasons[result.error]||result.error):show(result);await load();}
            catch(e){if(!expired)message.textContent="취소 결과 미확인 · 예약 현황 재조회";}
            finally{b.disabled=!x.can_cancel;}
          });return [x.ap_name,x.controller,x.receipt_id,show(x),kst(x.accepted_at),b];
        })));
      }
      reserve.disabled=!d.enabled||!r;
    }catch(e){if(!expired&&panel.isConnected)out.textContent="예약 현황 확인 불가";}
    finally{busy=false;refresh.disabled=false;}
  }
  reserve.disabled=true;
  reserve.addEventListener("click",async()=>{
    if(!r||!epoch||busy||expired)return;
    let attempt=manualAttempts.get(r.id);
    if(attempt&&attempt.epoch!==epoch){
      message.textContent="서비스가 재시작되었습니다. 이전 예약은 재실행하지 않습니다. 새로고침 후 새 예약을 선택하세요.";return;
    }
    if(!attempt){
      if(manualAttempts.size>=100){message.textContent="미확인 요청 상한 · 예약 현황 확인 필요";return;}
      attempt={epoch,request_id:crypto.randomUUID()};manualAttempts.set(r.id,attempt);
    }
    reserve.disabled=true;refresh.disabled=true;busy=true;
    try{
      const d=await jobPost("reserve",{...attempt,id:r.id});
      if(expired||!panel.isConnected)return;
      if(d.rejected){message.textContent=jobReasons[d.error]||d.error;if(d.error!=="service_instance_changed")manualAttempts.delete(r.id);}
      else{message.textContent=show(d)+" · 접수 ID "+d.receipt_id;manualAttempts.delete(r.id);}
    }catch(e){if(!expired)message.textContent="접수 여부 미확인 · 같은 버튼 재시도는 동일 요청 ID 사용. 현황을 확인하세요.";}
    finally{busy=false;refresh.disabled=false;if(!expired){reserve.disabled=false;await load();}}
  });
  refresh.addEventListener("click",load);
  panel.append(el("h3","정밀 조사 예약"),el("p","현재 검증된 지속 혼잡 AP 대상 · 기존 수집과 동일한 보호 정책 적용 · 재시작 시 재실행하지 않음"));
  if(r)panel.append(reserve);
  panel.append(refresh,message,out);
  // The initial GET only reads receipts, never submits an investigation.
  queueMicrotask(load);
  return panel;
}

function historyChart(report){
  const box=el("section",null,"history-chart");box.append(el("h3","채널 사용률·간섭률 추세"));
  const ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","0 0 800 220");svg.setAttribute("role","img");svg.setAttribute("aria-label","관측 시각별 사용률과 간섭률. 결측 구간은 선을 잇지 않습니다.");
  function shape(tag,attrs,label){const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(label!=null)n.textContent=label;svg.append(n);return n;}
  for(const p of [0,25,50,75,100]){
    const y=190-p*1.7;shape("line",{x1:44,x2:788,y1:y,y2:y,class:"chart-grid"});
    shape("text",{x:34,y:y+4,"text-anchor":"end",class:"chart-text"},p+"%");
  }
  const raw=report.resolution==="raw",tf=raw?"observed_at":"bucket";
  const gap=raw?360:report.resolution==="5m"?300:3600;
  for(const [key,cls] of [[raw?"utilization":"utilization_avg","chart-util"],[raw?"interference":"interference_avg","chart-interf"]]){
    let path="",dots="",prev=null;
    for(const row of report.rows){
      const t=row[tf],value=row[key];
      if(!Number.isFinite(t)||!Number.isFinite(value)||value<0||value>100){prev=null;continue;}
      const x=44+744*(t-report.start)/(report.end-report.start),y=190-1.7*value;
      const join=prev&&t>prev.t&&t-prev.t<=gap&&prev.context===row.context_id;
      path+=(join?"L":"M")+x.toFixed(2)+","+y.toFixed(2)+" ";
      if(!join)dots+="M"+x.toFixed(2)+","+y.toFixed(2)+" l0.6,0 ";
      prev={t,context:row.context_id};
    }
    shape("path",{d:path,fill:"none",class:cls,"stroke-width":1.6,"stroke-linejoin":"round"});
    // Render all isolated observations in a single SVG path.
    shape("path",{d:dots,fill:"none",class:cls,"stroke-width":3,"stroke-linecap":"round"});
  }
  shape("text",{x:44,y:213,class:"chart-text"},kst(report.start));
  shape("text",{x:788,y:213,"text-anchor":"end",class:"chart-text"},kst(report.end));
  const legend=el("div",null,"history-legend");legend.append(el("span","● 사용률","legend-util"),el("span","● 간섭률","legend-interf"));
  box.append(svg,legend,el("p","조회 범위의 실제 저장 관측만 표시합니다. 빈 구간은 미수집·미집계·보존기간 만료 등일 수 있으며 정상 상태로 해석하지 않습니다."));
  return box;
}
function detailLink(kind,id){
  const link=el("a",detailRoute?"목록으로 돌아가기":"전체 페이지에서 열기");
  link.href=detailRoute?"/":"/#"+kind+"/"+id;
  if(!detailRoute){link.target="_blank";link.rel="noopener";}
  return link;
}
async function openRadio(id){
  const ticket={kind:"radio",id},out=$("detailBody");
  selected=ticket;incidentPanel=null;$("detail").hidden=false;
  out.replaceChildren(el("p","Radio 관측 조회 중"));
  try{
    const row=await request("/api/radio?"+new URLSearchParams({id}));
    if(!expired&&selected===ticket)openHistory(row);
  }catch(e){
    if(!expired&&selected===ticket)out.replaceChildren(el("p","Radio 정보를 찾을 수 없습니다. 목록에서 다시 선택하세요."));
  }
}
function physicalApPanel(r){
  const panel=el("section",null,"physical-ap-panel"),out=el("div"),button=el("button","동일 물리 AP Radio 조회");
  panel.append(el("h3","동일 물리 AP의 Radio"),el("p","저장된 식별 관계 기준 · 이름/IP로 합치지 않음 · AP Up/Down 및 전체 Radio 수 확정 아님"),button,out);
  let serial=0;
  async function load(page){
    const wanted=selected,attempt=++serial;
    if(!wanted||wanted.id!==r.id||expired)return;
    button.disabled=true;out.replaceChildren(el("p","조회 중"));
    try{
      const value=await request("/api/ap-radios?"+new URLSearchParams({id:r.id,page:String(page)}));
      if(expired||selected!==wanted||attempt!==serial)return;
      out.replaceChildren();
      if(!value.identity_available){
        out.append(el("p","물리 AP 식별 관계 미확인 · 다른 Radio와 통합하지 않습니다."));
        return;
      }
      out.append(el("p",value.total+"개 저장 Radio · 조회 시점 기준"+(value.available?"":" · 현재 수집 확인 불가")));
      if(value.rows.length)out.append(table(["AP 관측 이름","WLC","대역","채널","관측 시각 KST","수집 상태","품질 점수","Radio 상세"],value.rows.map(row=>{
        let control;
        if(row.id===r.id)control=el("span","선택한 Radio");
        else if(detailRoute){control=el("a","Radio 상세");control.href="/#radio/"+row.id;}
        else{control=el("button","Radio 상세");control.addEventListener("click",()=>openRadio(row.id));}
        return [row.ap_name,row.controller,row.band,row.channel,kst(row.observed_at),
          row.freshness==="observed"?"최근 관측":"오래됨 / 확인 불가",row.quality_score??"미검증",control];
      })));
      const pages=Math.max(1,Math.ceil(value.total/value.size));
      if(pages>1||page>1){
        const controls=el("div",null,"pager"),prev=el("button","이전"),next=el("button","다음");
        prev.disabled=page<=1;next.disabled=page>=pages;
        prev.onclick=()=>load(page-1);next.onclick=()=>load(page+1);
        controls.append(prev,el("span",page+" / "+pages),next);out.append(controls);
      }
    }catch(e){if(!expired&&selected===wanted&&attempt===serial)out.replaceChildren(el("p","동일 AP 관측을 조회할 수 없습니다."));}
    finally{if(attempt===serial)button.disabled=false;}
  }
  button.addEventListener("click",()=>load(1));return panel;
}
function openHistory(r){
  incidentPanel=null;selected={id:r.id,name:r.ap_name,band:r.band};$("detail").hidden=false;
  const d=$("detailBody");d.replaceChildren(el("h3",r.ap_name+" · "+r.band),el("p","원본·집계 이력 · 실제 서비스 영향 및 원인 미확인"));
  d.append(detailLink("radio",r.id),el("p","AP 앵커 관측 (상세 화면을 연 시점의 자료, 현재 상태 보장 아님)"));
  if(r.ap_anchor?.observations?.length)d.append(table(["관측 WLC","자료 유형","보고 역할","관측 시각"],r.ap_anchor.observations.map(x=>[x.source,x.kind==="standby"?"Standby AP":"UAC AP DB",x.roles.join(", ")||"미확인",kst(x.observed_at)])));
  else d.append(el("p","현재 검증된 추가 앵커 관측 없음"));
  const end=el("input");end.type="datetime-local";end.setAttribute("aria-label","종료 시각 KST");
  end.value=new Date((Math.ceil(Date.now()/60000)*60+60+32400)*1000).toISOString().slice(0,16);
  const resolution=el("select"),hours=el("select"),button=el("button","이력 조회"),output=el("div");
  resolution.setAttribute("aria-label","관측 해상도");hours.setAttribute("aria-label","조회 기간");
  for(const [value,label] of [["raw","원본 관측 (최대 7일)"],["5m","5분 집계 (최대 30일)"],["1h","1시간 집계 (최대 365일)"]]){
    const o=el("option",label);o.value=value;resolution.append(o);
  }
  const periods={raw:[1,24,168],"5m":[24,168,720],"1h":[168,720,8760]};
  function resetPeriods(){
    hours.replaceChildren();for(const v of periods[resolution.value]){const o=el("option",v<24?"최근 "+v+"시간":"최근 "+(v/24)+"일");o.value=String(v);hours.append(o);}
    hours.value=resolution.value==="raw"?"24":resolution.value==="5m"?"720":"720";
  }
  resolution.addEventListener("change",resetPeriods);resetPeriods();
  button.addEventListener("click",async()=>{
    const wanted=selected;if(!wanted||expired)return;
    const e=Date.parse(end.value+":00+09:00")/1000,start=e-Number(hours.value)*3600;
    if(!Number.isFinite(e)||start<0){output.textContent="유효한 시간 범위를 입력하세요.";return;}
    const q=new URLSearchParams({id:wanted.id,start:String(start),end:String(e),resolution:resolution.value});
    button.disabled=end.disabled=resolution.disabled=hours.disabled=true;
    try{
      const h=await request("/api/history?"+q);if(selected!==wanted||expired)return;
      const raw=h.resolution==="raw",coverage=h.coverage||{};
      output.replaceChildren(el("p",kst(h.start)+" ~ "+kst(h.end)+" · "+h.resolution+" · "+h.count+"행"+(h.truncated?" · 조회 상한 도달, 기간을 줄이세요":"")),
        el("p","저장 관측 "+(coverage.observed_samples??"미확인")+"개"+(raw?"":" · 유효 관측 "+(coverage.valid_samples??"미확인")+"개 · 완료된 과거 일자 집계만 조회")),
        el("p","자료 범위: "+kst(coverage.first_at)+" ~ "+kst(coverage.last_at)));
      if(!h.rows.length)output.append(el("p","저장 관측 없음. 미수집·미집계 또는 보존기간 만료일 수 있습니다."));
      else {
        output.append(historyChart(h));
        const show=h.rows.slice(-300);
        if(h.rows.length>show.length)output.append(el("p","표에는 반환된 "+h.rows.length+"행 중 최근 "+show.length+"행을 표시합니다. 다운로드에는 전체 반환 행을 포함합니다."));
        const fmt=x=>x==null?"미확인":Number.isFinite(x)?Math.round(x*10)/10:x;
        output.append(raw?table(["시각 KST","사용률 %","간섭률 %","NF dBm","채널"],show.map(x=>[kst(x.observed_at),fmt(x.utilization),fmt(x.interference),fmt(x.noise),x.channel])):
          table(["구간 시작 KST","평균 사용률 %","최소 / 최대 %","평균 간섭률 %","평균 NF dBm","관측 / 유효","위험 기준 관측","채널"],show.map(x=>[kst(x.bucket),fmt(x.utilization_avg),fmt(x.utilization_min)+" / "+fmt(x.utilization_max),fmt(x.interference_avg),fmt(x.noise_avg),x.observed+" / "+x.valid,x.risky,x.channel])));
        const links=el("div",null,"pager");
        for(const ext of ["csv","html"]){const a=el("a",ext.toUpperCase()+" 다운로드");a.href="/api/history."+ext+"?"+q;a.download="awqm-diagnostic-history."+ext;links.append(a);}
        output.append(links);
      }
    }catch(e){if(!expired&&selected===wanted)output.textContent="조회 불가 · 데이터 유지보수 중이거나 조회 조건을 확인하세요.";}
    finally{button.disabled=end.disabled=resolution.disabled=hours.disabled=false;}
  });
  const controls=el("div",null,"pager");controls.append(end,resolution,hours,button);d.append(controls,output,physicalApPanel(r),neighborPanel(r),jobsPanel(r));
}

const incidentLabels={DETECTED:"최초 위험",PERSISTENT:"지속 위험",RECOVERING:"회복 관측 중",RESOLVED:"RF 정책 정상화",TRANSIENT:"일시 위험",UNCONFIRMED:"현재 확인 불가",CONTEXT_LOST:"관측 조건 변경 · 정상화 아님"};
const candidateLabels={RF03:"재시도·동시 저하 후보(과거 근거)",RF04:"Radio 혼잡 관측",RF05:"동일 채널 경합 후보",RF06:"중첩 채널 경합 후보",UNKNOWN:"원인 미확인"};
const verdictLabels={supported:"근거 지지",not_supported:"근거 불충분/반대",inconclusive:"판단 보류"};
let incidentPage=1,incidentFilter="all",incidentVersion=0,incidentPanel=null;
const reviewAttempts=new Map();
function selectOptions(values,labels,initial){
  const s=el("select");for(const value of values){const o=el("option",labels[value]||value);o.value=value;s.append(o);}s.value=initial;return s;
}
async function loadIncidents(){
  const rev=++incidentVersion,out=$("content");if(view!=="history"||expired)return;
  const end=Math.floor(Date.now()/1000)+1,start=Math.max(0,end-365*86400);
  const filters=el("div",null,"pager"),status=selectOptions(["all","open","closed"],{all:"전체",open:"관측 진행/확인 불가",closed:"종료 이력(정상화 아님 포함)"},incidentFilter),reload=el("button","이력 갱신");
  status.addEventListener("change",()=>{incidentFilter=status.value;incidentPage=1;loadIncidents();});
  reload.addEventListener("click",loadIncidents);
  filters.append(status,reload);
  out.replaceChildren(el("p","현장 승인된 Radio의 정책 이상 관측입니다. 실제 서비스 장애나 특정 단말 원인을 확정하지 않습니다."),filters,el("p","최근 365일 이력 조회 중"));
  try{
    const q=new URLSearchParams({page:String(incidentPage),status:incidentFilter,start:String(start),end:String(end)});
    const data=await request("/api/incidents?"+q);
    if(rev!==incidentVersion||view!=="history"||expired)return;
    out.replaceChildren(el("p","관측 이력 · 원인 후보 및 운영자 검증"),filters);
    if(!data.rows.length)out.append(el("p","저장된 정책 위험 이력 없음. 실제 무선 서비스가 정상이라는 뜻은 아닙니다."));
    out.append(table(["AP","WLC","대역","최초 관측","마지막 관측","관측 상태","후보","검증"],data.rows.map(r=>{
      const b=el("button",r.ap_name);b.addEventListener("click",()=>openIncident(r.incident_id));
      return [b,r.controller,r.band,kst(r.first_at),kst(r.last_at),incidentLabels[r.display_state]||r.display_state,
        r.evidence.candidates.map(x=>candidateLabels[x.code]||x.code).join(" / "),
        r.review?(verdictLabels[r.review.verdict]||r.review.verdict)+(r.review_current?"":" · 이전 근거"):"미검증"];
    })));
    const pager=el("div",null,"pager"),prev=el("button","이전"),next=el("button","다음");
    prev.disabled=incidentPage<=1;next.disabled=incidentPage*data.size>=data.total;
    prev.addEventListener("click",()=>{incidentPage--;loadIncidents();});next.addEventListener("click",()=>{incidentPage++;loadIncidents();});
    pager.append(prev,el("span",incidentPage+" / "+Math.max(1,Math.ceil(data.total/data.size))+" · "+data.total+"건"),next);out.append(pager);
  }catch(e){if(!expired&&rev===incidentVersion&&view==="history")out.append(el("p","이력을 불러오지 못했습니다."));}
}
async function openIncident(id){
  const ticket={id};incidentPanel=ticket;selected=null;$("detail").hidden=false;
  const out=$("detailBody");out.replaceChildren(el("p","이력 조회 중"));
  try{
    const d=await request("/api/incident?"+new URLSearchParams({id}));
    if(incidentPanel!==ticket||expired)return;
    const reload=el("button","상세 다시 조회");
    reload.addEventListener("click",()=>openIncident(id));
    out.replaceChildren(el("h3",d.ap_name+" · "+d.band+" · "+id.slice(0,12)),
      el("p","조회 당시 상태 · "+(incidentLabels[d.display_state]||d.display_state)+" · "+kst(Date.now()/1000)),reload,
      el("p","서비스 영향: 미검증 · 실제 원인 확정 아님"),
      el("p","최초 "+kst(d.first_at)+" · 마지막 "+kst(d.last_at)),
      table(["근거 기록 시각","Radio 관측 시각","상태","사용률","간섭률","NF","후보"],d.timeline.map(x=>[
        kst(x.at),kst(x.evidence.observed_at),incidentLabels[x.state]||x.state,x.evidence.utilization,
        x.evidence.interference,x.evidence.noise,x.evidence.candidates.map(c=>candidateLabels[c.code]||c.code).join(" / ")])));
    const exports=el("div",null,"pager"),copy=el("button","주간보고 요약 복사"),copyText=el("textarea");
    copyText.hidden=true;copyText.readOnly=true;copyText.rows=10;copyText.setAttribute("aria-label","주간보고 요약");
    copy.addEventListener("click",async()=>{
      try{
        const r=await request("/api/incident/summary?"+new URLSearchParams({id}));
        if(incidentPanel!==ticket||expired)return;
        copyText.value=r.text;copyText.hidden=false;
        try{await navigator.clipboard.writeText(r.text);copy.textContent="요약 복사 완료";}
        catch(e){copyText.select();copy.textContent="텍스트 선택 · 직접 복사";}
      }catch(e){if(!expired)copy.textContent="요약 조회 실패";}
    });
    exports.append(detailLink("incident",id),copy);
    for(const fmt of ["csv","html"]){
      const a=el("a",fmt.toUpperCase()+" 보고서");a.href="/api/incident/report."+fmt+"?"+new URLSearchParams({id});
      a.download="awqm-incident-report."+fmt;exports.append(a);
    }
    out.append(exports,copyText);
    if(d.timeline_truncated||d.reviews_truncated)out.append(el("p","최근 이력만 표시합니다. 일부 과거 근거/검증 이력은 조회 상한으로 생략됩니다.","warn"));
    const revision=selectOptions(d.timeline.map(x=>String(x.revision)),Object.fromEntries(d.timeline.map(x=>[String(x.revision),"근거 #"+x.revision+" · "+kst(x.at)+" · "+(incidentLabels[x.state]||x.state)])),String(d.revision));
    const candidate=el("select"),basis=el("p"),verdict=selectOptions(["inconclusive","supported","not_supported"],verdictLabels,"inconclusive"),reason=selectOptions(["insufficient_evidence","radio_evidence","other_evidence","conflicting_evidence"],{insufficient_evidence:"자료 부족",radio_evidence:"Radio 관측 근거",other_evidence:"별도 운영자 확인",conflicting_evidence:"상충 자료"},"insufficient_evidence");
    function updateCandidates(){const ev=d.timeline.find(x=>String(x.revision)===revision.value);candidate.replaceChildren();for(const c of ev.evidence.candidates){const o=el("option",candidateLabels[c.code]||c.code);o.value=c.code;candidate.append(o);}basis.textContent=ev.evidence.candidates.map(c=>(candidateLabels[c.code]||c.code)+" · "+c.basis.join(", ")+" · "+c.limit).join(" / ")+(ev.evidence.client_correlation?" · RF03 과거 집계 "+kst(ev.evidence.client_correlation.interval_end)+" / 후보 "+ev.evidence.client_correlation.candidate_count+"대 / 비교 최소 "+ev.evidence.client_correlation.min_comparable+"대, 저하 최소 "+ev.evidence.client_correlation.min_degraded+"대":"")+(ev.evidence.neighbor_evidence?" · 인접 후보 "+ev.evidence.neighbor_evidence.map(n=>(n.ap_name||"미확인")+" "+n.channel+"CH / "+n.overlap_mhz+"MHz").join(", "):"");}
    revision.addEventListener("change",updateCandidates);updateCandidates();
    const save=el("button","운영자 검증 기록"),message=el("p");let attempt=null;
    save.addEventListener("click",async()=>{
      if(expired||incidentPanel!==ticket||save.disabled)return;
      const csrf=document.querySelector("input[name=csrf]").value;
      if(candidate.value==="UNKNOWN"&&verdict.value==="supported"){message.textContent="미확인 원인은 지지 확정할 수 없습니다.";return;}
      const data={id,revision:revision.value,csrf,verdict:verdict.value,reason:reason.value,candidate:candidate.value};
      const sig=JSON.stringify(data);if(!reviewAttempts.has(sig)){if(reviewAttempts.size>=100){message.textContent="미확인 요청이 너무 많습니다. 기존 검증 이력을 확인하세요.";return;}reviewAttempts.set(sig,"review-"+crypto.randomUUID());}attempt={sig,id:reviewAttempts.get(sig)};
      save.disabled=revision.disabled=candidate.disabled=verdict.disabled=reason.disabled=true;
      try{const r=await fetch("/api/incident/review",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams({...data,request_id:attempt.id}),cache:"no-store"});
        if(r.status===401){invalidate();return;}if(!r.ok)throw Error("review failed");const result=await r.json();
        if(!result.recorded)throw Error("unconfirmed");if(incidentPanel!==ticket||expired)return;
        message.textContent="검증 이력 저장 완료";reviewAttempts.delete(sig);attempt=null;await openIncident(id);if(view==="history")loadIncidents();
      }catch(e){if(!expired&&incidentPanel===ticket)message.textContent="저장 여부 미확인 · 같은 선택으로 다시 시도하면 중복 저장을 방지합니다.";}
      finally{save.disabled=revision.disabled=candidate.disabled=verdict.disabled=reason.disabled=false;}
    });
    out.append(el("h3","운영자 검증"),el("p","원본 근거 버전과 검증 결과만 기록합니다. 원인 확정 또는 장비 조치를 실행하지 않습니다."),revision,candidate,basis,verdict,reason,save,message,
      el("h3","검증 이력"),table(["시각","근거 버전","후보","검증","사유"],d.reviews.map(r=>[kst(r.at),r.revision,candidateLabels[r.candidate]||r.candidate,verdictLabels[r.verdict]||r.verdict,r.reason])));
  }catch(e){if(!expired&&incidentPanel===ticket)out.replaceChildren(el("p","상세 이력을 조회하지 못했습니다."));}
}

let clientPage=1,clientVersion=0;
function clientButton(row){
  const button=el("button",row.id.slice(0,16));
  button.addEventListener("click",()=>{openClient(row.id);$("detail").scrollIntoView({block:"start"});});
  return button;
}
async function openClient(id){
  const ticket={kind:"client",id};
  selected=ticket;incidentPanel=null;$("detail").hidden=false;
  const out=$("detailBody");
  out.replaceChildren(el("p","단말 관측 조회 중"));
  const reload=el("button","상세 다시 조회");
  reload.addEventListener("click",()=>openClient(id));
  try{
    const row=await request("/api/client?"+new URLSearchParams({id}));
    if(expired||selected!==ticket)return;
    const withUnit=(value,unit)=>value==null?"미확인":value+unit;
    const activity={successful_tx_observed:"성공 프레임 증가 관측",no_successful_tx_increase:"성공 프레임 증가 없음"};
    const fields=[
      ["AP",row.ap_name],["WLC",row.controller],["대역",row.band],["관측 시각 KST",kst(row.observed_at)],
      ["Health",withUnit(row.health,"%")],["ACK SNR",withUnit(row.snr," dB")],
      ["최근 PHY 속도",withUnit(row.last_phy_rate," Mbps")],["부분 품질 점수",row.quality_score],
      ["성공 프레임 재시도율",withUnit(row.station_retry,"%")],["성공 Tx 관측",activity[row.station_tx_activity]],
      ["Station Stats 구간 KST",row.station_start==null?"미확인":kst(row.station_start)+" ~ "+kst(row.station_end)],
      ["Airtime","미확인"],["원인 후보",candidateLabels[row.candidate]||"미확인"],
      ["비교 단말 중 저하",row.peer_comparable==null?"미확인":row.peer_degraded+" / "+row.peer_comparable]
    ];
    out.replaceChildren(el("h3","임시 단말 "+row.id),detailLink("client",id),reload,
      el("p","Health와 ACK SNR로 계산한 부분 점수입니다. 성공 프레임 재시도율은 승인된 Station Stats 구간의 관측값입니다."),
      table(["항목","관측값"],fields));
    setTimeout(()=>{if(!expired&&selected===ticket)openClient(id);},row.refresh_after*1000);
  }catch(e){
    if(!expired&&selected===ticket)out.replaceChildren(el("p","현재 승인된 단말 관측을 조회할 수 없습니다."),reload,detailLink("client",id));
  }
}
async function loadClients(){
  if(view!=="clients"||expired)return;
  const rev=++clientVersion,out=$("content");
  out.replaceChildren(el("p","단말 상세 관측 조회 중"));
  try{
    const q=new URLSearchParams({q:$("search").value,page:String(clientPage)});
    const d=await request("/api/clients?"+q);
    if(rev!==clientVersion||view!=="clients"||expired)return;
    out.replaceChildren(el("p",d.enabled?"현장 승인된 AP 정밀 조회의 임시 단말 관측입니다. MAC·SSID 원문 미표시, DB 미저장.":"단말 상세 수집 비활성 · 별도 현장 검증 필요"),
      el("p","품질은 Health·ACK SNR 기반 부분 점수입니다. 별도 검증된 Station Stats는 성공 프레임 중 재시도 비율만 표시합니다. Airtime은 미확인. RF03은 별도 승인된 동시간대 재시도·비교 단말 저하 후보이며 원인 확정이 아닙니다."));
    const refreshBtn=el("button","관측 갱신");refreshBtn.addEventListener("click",loadClients);out.append(refreshBtn);
    if(!d.rows.length)out.append(el("p","현재 조건을 충족하는 상세 관측이 없습니다. 단말이 없거나 정상이라는 의미는 아닙니다."));
    else out.append(table(["임시 단말 ID","AP","WLC","대역","Health %","ACK SNR dB","최근 PHY Mbps","부분 품질 점수","성공 Tx 관측","성공 프레임 재시도 %","통계 구간","Airtime","간접 후보","비교 단말 저하","관측 시각"],d.rows.map(r=>[clientButton(r),r.ap_name,r.controller,r.band,r.health,r.snr,r.last_phy_rate,r.quality_score==null?"미확인":r.quality_score+" (부분)",({successful_tx_observed:"증가 관측",no_successful_tx_increase:"성공 Tx 증가 없음"})[r.station_tx_activity]||"미확인",r.station_retry==null?"미확인":r.station_retry.toFixed(1),r.station_start==null?"미확인":kst(r.station_start)+" ~ "+kst(r.station_end),"미확인",r.candidate==="RF03"?"RF03 · 낮은 신뢰도":"미확인",r.peer_comparable==null?"미확인":r.peer_degraded+" / "+r.peer_comparable,kst(r.observed_at)])));
    if(d.refresh_after>0)setTimeout(()=>{if(view==="clients"&&!expired&&clientVersion===rev)loadClients();},d.refresh_after*1000);
    const controls=el("div",null,"pager"),prev=el("button","이전"),next=el("button","다음");
    prev.disabled=clientPage<=1;next.disabled=clientPage*d.size>=d.total;
    prev.addEventListener("click",()=>{clientPage--;loadClients();});next.addEventListener("click",()=>{clientPage++;loadClients();});
    controls.append(prev,el("span",clientPage+" / "+Math.max(1,Math.ceil(d.total/d.size))+" · "+d.total+"건"),next);out.append(controls);
  }catch(e){if(!expired&&rev===clientVersion&&view==="clients")out.replaceChildren(el("p","현재 상세 관측 확인 불가"));}
}

function clusterPanel(d){
  const c=el("section");c.append(el("h3","WLC 클러스터 관측"));
  if(!d){c.append(el("p","클러스터 관측 자료 미확인"));return c;}
  c.append(el("p","대표 조회 "+d.representative+" · 멤버십 수집 "+d.membership_sources+" / "+d.expected+" · 현장 검증 전 · 장비 Down/Hang 판단 아님"));
  const labels={connected_observed:"연결 교차 관측",self_only:"자체 관측",peer_disagreement:"Peer 상태 상충",peer_issue_observed:"Peer 이상 관측",self_issue_observed:"자체 이상 관측",peer_only:"Peer 관측만 있음",unknown:"미확인"};
  const m=Object.fromEntries(["ap","client"].map(k=>[k,new Map(d.load[k].rows.map(r=>[r.controller,r]))]));
  c.append(table(["WLC","CPU %","멤버십","Role","Active AP","Standby AP","Active Client","Standby Client"],d.nodes.map(r=>[r.controller,r.cpu,labels[r.state]||r.state,r.role,m.ap.get(r.controller)?.active,m.ap.get(r.controller)?.standby,m.client.get(r.controller)?.active,m.client.get(r.controller)?.standby])));
  c.append(el("h3","AP Standby / UAC DB 관측"),el("p","현재 AP와 일치하는 양성 관측 수입니다. 미관측은 장애·Standby 미구성으로 확정하지 않습니다."),table(["WLC","Standby 관측/일치","UAC AP DB 관측/일치","Standby 시각","UAC DB 시각"],d.nodes.map(r=>{const a=r.ap_anchors?.standby,u=r.ap_anchors?.uac_database;const count=x=>x?.status==="observed"?x.total+" / "+x.matched:"미확인";return [r.controller,count(a),count(u),kst(a?.observed_at),kst(u?.observed_at)];})));
  c.append(el("h3","WLC 자원 관측"),el("p","메모리 보고 사용률과 Datapath CPU별 1초·4초 값은 서로 다른 지표입니다. 장비 장애나 RF 원인으로 확정하지 않습니다."));
  const resourceLabels={disabled:"비활성",unavailable:"미확인",withheld:"보호/검증 대기",stale:"오래된 관측",attestation_unavailable:"검증 만료/불일치",cpu_inventory_mismatch:"CPU 목록 불일치",parse_error:"출력 형식 불일치",permission_denied:"권한 없음",unsupported:"명령 미지원",transport_error:"통신 오류",observed:"최근 관측"};
  c.append(table(["WLC","메모리 보고 사용률 %","Free (Kb)","메모리 관측 시각","Datapath CPU 수","1초 최대 %","4초 최대 %","Datapath 관측 시각","수집 상태"],d.nodes.map(r=>{
    const m=r.resources?.memory,p=r.resources?.datapath;
    return [r.controller,m?.data?.reported_used_percent,m?.data?.free_kb,kst(m?.observed_at),p?.data?.cpu_count,p?.data?.max_1s_percent,p?.data?.max_4s_percent,kst(p?.observed_at),
      "Mem "+(resourceLabels[m?.status]||"미확인")+" / DP "+(resourceLabels[p?.status]||"미확인")];
  })));
  const cores=d.nodes.flatMap(r=>(r.resources?.datapath?.data?.rows||[]).map(x=>[r.controller,x.id,x.type,x.percent_1s,x.percent_4s]));
  if(cores.length){const detail=el("details");detail.append(el("summary","Datapath CPU별 상세 (64초 값 제외)"),table(["WLC","CPU ID","유형","1초 %","4초 %"],cores));c.append(detail);}
  for(const [key,title] of [["ap","AP"],["client","Client"]]){
    const l=d.load[key],status={unknown:"미확인",single_source:"단일 출처",crosschecked:"교차 확인",conflicting_observations:"관측값 상충"}[l.status]||l.status;
    c.append(el("p",title+" 전체 Active "+(l.active_total??"미확인")+" / Standby "+(l.standby_total??"미확인")+" · "+status+" · "+(l.source||"—")+" · "+kst(l.observed_at)));
  }
  if(d.links.length)c.append(table(["관측 WLC","대상 WLC","연결 상태","연결 유형","시각"],d.links.map(r=>[r.from,r.to,r.state,r.link,kst(r.observed_at)])));
  return c;
}

function associationPanel(){
  const panel=el("section"),out=el("div"),input=el("input"),button=el("button","접속 이력 조회"),pager=el("div",null,"pager");
  let p=1;
  input.placeholder="보호된 단말 ID (전체 조회는 공란)";input.maxLength=32;input.setAttribute("aria-label","보호된 단말 ID");
  async function load(){
    button.disabled=true;
    try{
      const d=await request("/api/association-history?"+new URLSearchParams({id:input.value.trim(),page:String(p)}));
      if(expired||!panel.isConnected)return;
      out.replaceChildren(el("p",d.enabled?"최근 30일 양성 관측 이력 · 관측 공백은 접속 종료가 아님":"이력 수집 비활성 또는 현장 승인 만료"));
      if(d.rows.length)out.append(table(["보호 ID","AP","대역","AAC","A-UAC 관측","S-UAC 관측","관측 유형","첫 관측","최근 관측"],d.rows.map(r=>[r.id,r.ap_name,r.band,r.aac,r.auac,r.suac,({first_observation:"첫 관측",after_observation_gap:"관측 공백 후",attachment_changed:"접속 위치 변경 관측",anchor_context_changed:"앵커 컨텍스트 변경",user_anchor_observation_changed:"UAC 관측 변경",metadata_changed:"메타데이터 변경",physical_mapping_unverified:"물리 AP·Radio 연결 미검증"})[r.event]||r.event,kst(r.first_at),kst(r.last_at)])));
      const prev=el("button","이전"),next=el("button","다음");
      prev.disabled=p<=1;next.disabled=p*d.size>=d.total;
      prev.addEventListener("click",()=>{p--;load();});next.addEventListener("click",()=>{p++;load();});
      pager.replaceChildren(prev,el("span",p+" / "+Math.max(1,Math.ceil(d.total/d.size))+" · "+d.total+"건"),next);
    }catch(e){if(!expired)out.textContent="관리자 권한 필요 또는 이력 조회 불가";}
    finally{button.disabled=false;}
  }
  button.addEventListener("click",()=>{p=1;load();});
  panel.append(el("h3","보호된 단말 접속 이력"),el("p","관리자 전용 · MAC/IP/사용자명/SSID/BSSID 원문 미저장 · UAC는 테이블 관측"),input,button,out,pager);
  return panel;
}

function operationsPanel(){
  const panel=el("section"),out=el("div"),msg=el("p"),refresh=el("button","수집 보호·점검 상태 조회");
  let state=null,loading=false;
  const phase={enabled:"수집 허용",paused:"일시정지 적용",draining:"실행 중 조회 종료 대기",applying:"변경 적용 중"};
  const protection={normal:"정상",limited:"제한",critical:"긴급 보호",unknown:"미확인"};
  async function load(){
    if(loading||expired)return;
    loading=true;refresh.disabled=true;
    try{
      const d=await request("/api/admin/collection");
      if(expired||!panel.isConnected)return;
      state=d.enabled?d.desired:null;
      if(!state){out.replaceChildren(el("p","수집 제어 비활성"));return;}
      const applied=d.applied.current?d.applied.state:null;
      const rows=new Map((applied?.controllers||[]).map(x=>[x.controller,x]));
      const controls=el("div",null,"pager");
      for(const [action,label] of [["pause","전체 수집 일시정지"],["resume","전체 수집 재개"]]){
        const b=el("button",label);b.addEventListener("click",()=>change("all",action,b));controls.append(b);
      }
      out.replaceChildren(el("p","요청 상태와 적용 상태는 별개입니다. 설정 변경이나 장비 재부팅을 수행하지 않습니다."),controls,
        table(["WLC","요청","적용","CPU %","보호","CLI 5분 사용 초","상세 1분 사용 초","예약 대기/실행","제어"],Object.entries(state.controllers).map(([id,w])=>{
          const a=rows.get(id),b=el("button",w.requested_paused?"재개":"일시정지");
          b.addEventListener("click",()=>change(id,w.requested_paused?"resume":"pause",b));
          const phaseLabel=a&&a.revision===w.revision?phase[a.phase]||a.phase:"적용 미확인";
          return [id,w.requested_paused?"정지 요청":"수집 허용 요청",phaseLabel,a?.cpu,protection[a?.protection]||"미확인",
            a?.cli_seconds_300,a?.detail_seconds_60,a?.jobs?(a.jobs.queued+a.jobs.deferred)+" / "+a.jobs.running:"미확인",b];
        })));
      if(applied?.error)out.append(el("p","운영 상태 저장소 오류 · 신규 수집 차단","warn"));
      if(d.recent.length)out.append(el("h3","최근 요청 이력"),table(["시각","대상","요청"],d.recent.slice(0,20).map(x=>[kst(x.at),x.target,x.requested_paused?"일시정지":"재개"])));
    }catch(e){state=null;if(!expired)out.textContent="관리자 권한 필요 또는 상태 조회 실패";}
    finally{loading=false;refresh.disabled=false;}
  }
  async function change(target,action,button){
    if(!state||loading||expired)return;
    button.disabled=true;
    const revision=state.revision;loading=true;
    try{
      const csrf=document.querySelector("input[name=csrf]").value;
      const r=await fetch("/api/admin/collection",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
        body:new URLSearchParams({csrf,target,action,revision:String(revision)}),cache:"no-store"});
      if(r.status===401){invalidate();return;}
      if(r.status===409){msg.textContent="상태가 변경됐거나 요청을 적용할 수 없습니다. 최신 상태를 확인하세요.";return;}
      if(!r.ok)throw Error("unconfirmed");
      const d=await r.json();msg.textContent=d.accepted?"요청 저장 완료 · 실행 중 조회 종료 및 적용 상태를 확인하세요.":"접수 여부 미확인";
    }catch(e){if(!expired)msg.textContent="요청 결과 미확인 · 상태를 다시 조회합니다.";}
    finally{loading=false;button.disabled=false;if(!expired)await load();}
  }
  refresh.addEventListener("click",load);
  panel.append(el("h3","수집 보호 및 점검 제어"),el("p","관리자 전용 · 전체 정지 후 새 웹 로그인 불가 시 로컬 AWQM의 서비스 관리 사용 · 자동 재개 없음"),refresh,msg,out);
  return panel;
}

let submittedSearch="",refreshSearchClients=null;
function searchGroup(parent,kind,title,query,end){
  const panel=el("section",null,"search-group"),body=el("div");
  panel.setAttribute("aria-label",title);
  panel.append(el("h3",title),body);parent.append(panel);
  let serial=0,currentPage=1;
  if(kind==="clients")refreshSearchClients=()=>{if(panel.isConnected)load(currentPage);};
  async function load(number){
    currentPage=number;
    const attempt=++serial,params=new URLSearchParams({q:query,page:String(number),size:"8"});
    if(kind==="incidents"){
      params.set("start",String(Math.max(0,end-365*86400)));params.set("end",String(end));params.set("status","all");
    }
    body.replaceChildren(el("p","조회 중"));
    try{
      const result=await request("/api/"+kind+"?"+params);
      if(expired||!panel.isConnected||attempt!==serial)return;
      body.replaceChildren();
      if(!result.rows.length){
        const message=kind==="clients"&&!result.enabled?"단말 상세 수집 비활성":
          kind==="clients"&&!result.available?"현재 단말 관측을 확인할 수 없습니다.":"검색 결과 없음";
        body.append(el("p",message));
      }else{
        body.append(table(["AP","WLC","대역",kind==="clients"?"임시 단말 ID":kind==="incidents"?"최근 관측 KST":"관측 시각 KST","상세"],
          result.rows.map(row=>{
            const open=el("button","상세 보기");
            const identifier=kind==="clients"?row.id.slice(0,16):kind==="incidents"?row.incident_id.slice(0,12):row.band;
            open.setAttribute("aria-label",row.ap_name+" "+identifier+" 상세 보기");
            open.addEventListener("click",()=>{
              if(kind==="clients")openClient(row.id);
              else if(kind==="incidents")openIncident(row.incident_id);
              else openRadio(row.id);
              $("detail").scrollIntoView({block:"start"});
            });
            return [row.ap_name,row.controller,row.band,kind==="clients"?row.id.slice(0,16):
              kst(kind==="incidents"?row.last_at:row.observed_at),open];
          })));
      }
      const pages=Math.max(1,Math.ceil(result.total/result.size));
      if(pages>1||number>1){
        const controls=el("div",null,"pager"),prev=el("button","이전"),next=el("button","다음");
        prev.disabled=number===1;next.disabled=number>=pages;
        prev.onclick=()=>load(number-1);next.onclick=()=>load(number+1);
        controls.append(prev,el("span",number+" / "+pages+" · "+result.total+"건"),next);body.append(controls);
      }
      if(kind==="clients"&&result.refresh_after>0)setTimeout(()=>{
        if(!expired&&panel.isConnected&&attempt===serial)load(number);
      },result.refresh_after*1000);
    }catch(e){
      if(expired||!panel.isConnected||attempt!==serial)return;
      const retry=el("button","다시 조회");retry.onclick=()=>load(number);
      body.replaceChildren(el("p","조회할 수 없습니다."),retry);
    }
  }
  load(1);
}
function maintenancePanel(value){
  const root=el("div");
  root.append(el("h3","DB 백업 및 데이터 보존"));
  if(!value?.available){root.append(el("p","유지보수 상태를 확인할 수 없습니다."));return root;}
  const statuses={ok:"정상",running:"처리 중",failed:"실패",pending_writes:"저장 대기 중",
    disk_protection:"디스크 보호",backup_deferred_disk:"디스크 부족으로 백업 보류",
    revision_changed:"다음 주기에 재시도",cancelled:"중단",unknown:"미확인"};
  const levels={normal:"정상",warning:"주의",limited:"제한",critical:"위험",unknown:"미확인"};
  const days=value.retention_days||{};
  root.append(el("p","유지보수 상태: "+(statuses[value.status]||"미확인")+" · 최근 시도 "+kst(value.last_attempt)),
    el("p","핵심 DB 백업: "+kst(value.last_core_backup)+" · 매일 / 집계 DB 백업: "+kst(value.last_aggregate_backup)+" · 매주"),
    el("p","데이터 디스크: "+(levels[value.disk_level]||"미확인")+" · 백업 디스크: "+(levels[value.backup_disk_level]||"미확인")),
    el("p","데이터 보존: "+(value.retention_enabled?"보호된 정책 승인됨":"자동 삭제 비활성")+" · 최근 시도 "+kst(value.last_retention_attempt)),
    el("p","정책 기준: 원본 "+(days.raw??"—")+"일 · 5분 "+(days.five_minute??"—")+"일 · 1시간 "+(days.hourly??"—")+"일 · 종료된 장애 및 감사 "+(days.audit??"—")+"일"),
    el("p","백업 보관 만료: "+(value.archive_expiration_days==null?"자동 삭제 비활성":value.archive_expiration_days+"일")+" · 최근 시도 "+kst(value.last_archive_retention_attempt)),
    el("p","위 값은 서비스에서 승인된 적용 상태입니다. 삭제 기준을 충족하지 못한 데이터는 더 오래 남을 수 있습니다. 경로 및 자격 증명은 표시하지 않습니다. 보존 정책 변경은 보호된 로컬 관리자 절차로만 수행합니다."));
  return root;
}
function render(){
  if(expired)return;
  const c=$("content");c.replaceChildren();$("heading").textContent=names[view];
  if(view==="results"){
    c.append(el("p","검색어 “"+submittedSearch+"” · 장애 이력은 최근 365일 기준"));
    const end=Math.floor(Date.now()/1000)+1;
    for(const [kind,title] of [["radios","AP / Radio"],["clients","사용자 단말"],["incidents","장애 이력"]]){
      searchGroup(c,kind,title,submittedSearch,end);
    }
    return;
  }
  if(view==="clients"){loadClients();return;}
  if(view==="history"){loadIncidents();return;}
  if(view==="admin"){const b=el("button","관리자 상태 조회"),out=el("div");b.addEventListener("click",async()=>{try{const a=await request("/api/admin/status");out.replaceChildren(el("p","인증서 잔여 "+a.certificate_days+"일"+(a.certificate_warning?" · 갱신 확인 필요":"")),el("p","감사 기록 "+a.audit.events+"건 · 최근 "+kst(a.audit.last_event_at)),maintenancePanel(a.maintenance));}catch(e){if(!expired)out.textContent="관리자 권한 필요 또는 조회 불가";}});c.append(el("p","조회 전용 서비스 상태입니다. 장비 설정 및 자동 정밀 분석은 이 화면에서 변경할 수 없습니다."),b,out,operationsPanel(),jobsPanel(),associationPanel());return;}
  if(!snapshot){c.append(el("p","자료 확인 중"));return;}
  if(view==="overview"){const k=el("div",null,"kpis");k.append(metric("관측 WLC",snapshot.coherent_controllers==null?null:snapshot.coherent_controllers+" / "+snapshot.expected_controllers),metric("저장된 Radio",snapshot.inventory_radios),metric("현재 매핑 Radio",snapshot.mapped_radios),metric("활성 단말 관측",snapshot.active_clients));c.append(k,el("p","최근 게시: "+kst(snapshot.published_at)+" · "+(snapshot.available?"관측 자료 제공":"현재 관측 확인 불가")),clusterPanel(snapshot.cluster));}
  c.append(table(["AP","WLC","대역","채널","사용률 %","간섭률 %","NF dBm","품질 점수","위험 관측","관측 시각 KST","수집 상태"],snapshot.rows.map(r=>{const b=el("button",r.ap_name);b.addEventListener("click",()=>openHistory(r));const cell=el("div");cell.append(b);if(r.incident_id){const i=el("button","위험 이력");i.addEventListener("click",()=>openIncident(r.incident_id));cell.append(i);}return [cell,r.controller,r.band,r.channel,r.utilization,r.interference,r.noise,r.quality_score==null?"미검증":r.quality_score,r.risk_status==="UNKNOWN"?"미확인":r.risk_status,kst(r.observed_at),r.freshness==="observed"?(r.quality==="local_operator_attested"?"최근 관측 · 현장 승인 지표":"최근 관측 · 미검증"):"오래됨 / 확인 불가"];})));
  if(!snapshot.rows.length)c.append(el("p","저장 자료 또는 검색 결과 없음"));
  const pager=el("div",null,"pager"),prev=el("button","이전"),next=el("button","다음");
  prev.disabled=page<=1;next.disabled=page*snapshot.size>=snapshot.total;
  prev.addEventListener("click",()=>{page--;refresh(true);});next.addEventListener("click",()=>{page++;refresh(true);});
  pager.append(prev,el("span",page+" / "+Math.max(1,Math.ceil(snapshot.total/snapshot.size))+" · "+snapshot.total+"건"),next);c.append(pager);
}
async function refresh(force=false){if(expired||busy&&!force)return;const serial=++revision;busy=true;try{const q=new URLSearchParams({q:$("search").value,page:String(page)});const r=await request("/api/radios?"+q);if(serial!==revision||expired)return;snapshot=r;notice(r.available?"진단 관측 · 현장 승인된 Radio 지표만 정책 점수 제공 · 원인/서비스 장애 확정 금지":"수집 또는 저장 상태 확인 불가 · 마지막 관측을 현재 상태로 해석하지 마십시오.");if(view==="overview"||view==="radios"){const offsets=force?[]:[...$("content").querySelectorAll(".table-wrap")].map(n=>[n.scrollLeft,n.scrollTop]);render();if(offsets.length)[...$("content").querySelectorAll(".table-wrap")].forEach((n,i)=>{if(offsets[i]){n.scrollLeft=offsets[i][0];n.scrollTop=offsets[i][1];}});}}catch(e){if(!expired&&serial===revision){notice("갱신 실패 · 현재 상태 확인 불가");snapshot=null;if(view==="overview"||view==="radios")render();}}finally{if(serial===revision)busy=false;}}
function clearDetail(){
  selected=null;incidentPanel=null;$("detail").hidden=true;$("detailBody").replaceChildren();
}
function navigate(v){
  if(expired||!Object.hasOwn(names,v)||view===v)return;
  // Invalidate in-flight callbacks before switching screens. Never display
  // the previous AP/incident detail or a page from another search context.
  revision++;clientVersion++;incidentVersion++;busy=false;clearDetail();
  refreshSearchClients=null;
  view=v;page=1;clientPage=1;incidentPage=1;
  for(const b of $("nav").querySelectorAll("button")){
    if(b.dataset.view===v)b.setAttribute("aria-current","page");
    else b.removeAttribute("aria-current");
  }
  $("search").disabled=v==="history"||v==="admin"||v==="results";
  $("list-filter").hidden=$("search").disabled;
  if(v==="overview"||v==="radios"){
    snapshot=null;render();refresh(true);
  }else render();
}
$("nav").addEventListener("click",e=>{const v=e.target.dataset.view;if(v)navigate(v);});
$("global-search").addEventListener("submit",event=>{
  event.preventDefault();
  const query=$("global-query").value.trim();
  if(!query||expired)return;
  submittedSearch=query;
  if(view==="results"){clearDetail();render();}
  else navigate("results");
});
$("search").addEventListener("input",()=>{
  if(expired||$("search").disabled)return;
  clearDetail();page=1;clientPage=1;
  if(view==="clients")loadClients();
  else if(view==="overview"||view==="radios"){snapshot=null;render();refresh(true);}
});
$("close").addEventListener("click",clearDetail);
async function showDetailPage(kind,id){
  view="detail";
  document.body.classList.add("detail-page");
  const title={radio:"Radio 관측 상세",client:"단말 관측 상세",incident:"위험 이력 상세"}[kind];
  $("heading").textContent=title;
  document.title=title+" · AWQM";
  $("detail").hidden=false;
  const out=$("detailBody");
  out.replaceChildren(el("p","상세 정보 조회 중"));
  if(kind==="incident"){
    await openIncident(id);
  }else if(kind==="client"){
    await openClient(id);
  }else{
    await openRadio(id);
  }
  if(!expired&&!out.querySelector('a[href="/"]'))out.append(detailLink(kind,id));
}
if(detailRoute)showDetailPage(detailRoute[1],detailRoute[2]);
else{render();refresh();}
window.addEventListener("hashchange",()=>location.reload());
function closeUpdates(){
  if(updateTimer!==null){clearTimeout(updateTimer);updateTimer=null;}
  if(updateSocket){const s=updateSocket;updateSocket=null;try{s.close();}catch(e){}}
}
function automaticRefresh(){
  if(expired||document.hidden||Date.now()-lastAutomatic<4000)return;
  lastAutomatic=Date.now();
  if(selected?.kind==="client")openClient(selected.id);
  if(detailRoute){
    if(detailRoute[1]==="client")return;
    request("/api/publication").then(state=>{
      if(!expired)notice(state.available?"저장된 상세 정보 · 조회 시점 기준":"현재 수집 상태 확인 불가 · 저장된 상세 정보");
    }).catch(()=>{if(!expired)notice("현재 수집 상태 확인 불가");});
  }else if(view==="clients")loadClients();
  else if(view==="results"){if(refreshSearchClients)refreshSearchClients();}
  else refresh();
}
function connectUpdates(){
  // A session has one WSS slot. Detail tabs retain the HTTPS fallback.
  if(detailRoute||expired||document.hidden||updateSocket||location.protocol!=="https:"||!("WebSocket" in window))return;
  let s;
  try{s=new WebSocket("wss://"+location.host+"/ws/updates");}catch(e){return;}
  updateSocket=s;lastPublication=null;
  s.addEventListener("message",e=>{
    if(updateSocket!==s||expired||typeof e.data!=="string"||e.data.length>256)return;
    try{
      const d=JSON.parse(e.data);
      if(d.type!=="publication"||!Number.isSafeInteger(d.revision)||d.revision<0||typeof d.available!=="boolean")return;
      lastSignal=Date.now();const key=d.revision+":"+d.available;
      if(key!==lastPublication){lastPublication=key;automaticRefresh();}
    }catch(e){}
  });
  s.addEventListener("close",()=>{
    if(updateSocket!==s)return;
    updateSocket=null;lastSignal=0;
    if(!expired&&!document.hidden)updateTimer=setTimeout(()=>{updateTimer=null;connectUpdates();},8000);
  });
  s.addEventListener("error",()=>{try{s.close();}catch(e){}});
}
document.addEventListener("visibilitychange",()=>{
  if(document.hidden)closeUpdates();
  else{lastSignal=0;automaticRefresh();connectUpdates();}
});
window.addEventListener("pagehide",closeUpdates);
connectUpdates();
// Independent fallback also enforces expiry/staleness during a stalled WS.
setInterval(()=>{
  if(expired||document.hidden)return;
  if(Date.now()-lastSignal>12000)automaticRefresh();
  if(!updateSocket&&updateTimer===null)connectUpdates();
},10000);

