"use strict";
const $=id=>document.getElementById(id);
const names={overview:"종합 현황",radios:"AP 품질 관측",clients:"사용자 분석",history:"장애 및 이력",admin:"운영 관리"};
let view="overview",page=1,snapshot=null,selected=null,busy=false,revision=0,expired=false;
const kst=t=>t==null?"미확인":new Date(t*1000).toLocaleString("ko-KR",{timeZone:"Asia/Seoul"});
function el(tag,value,cls){const n=document.createElement(tag);if(value!=null)n.textContent=String(value);if(cls)n.className=cls;return n;}
function table(headers,rows){const wrap=el("div",null,"table-wrap"),t=el("table"),head=el("thead"),tr=el("tr");for(const h of headers)tr.append(el("th",h));head.append(tr);t.append(head);const b=el("tbody");for(const row of rows){const r=el("tr");for(const value of row){const c=el("td");if(value instanceof Node)c.append(value);else c.textContent=value??"미확인";r.append(c);}b.append(r);}t.append(b);wrap.append(t);return wrap;}
function metric(n,v){const b=el("div",null,"kpi");b.append(el("span",n),el("strong",v??"미확인"));return b;}
function notice(s){$("notice").textContent=s;}
function invalidate(){expired=true;snapshot=null;selected=null;incidentPanel=null;$("content").replaceChildren(el("p","인증 만료. 새로고침 후 다시 로그인하세요."));$("detail").hidden=true;$("detailBody").replaceChildren();notice("인증 세션 만료");}
async function request(path){const r=await fetch(path,{cache:"no-store"});if(r.status===401){invalidate();throw Error("expired");}if(!r.ok)throw Error("unavailable");return r.json();}
function openHistory(r){incidentPanel=null;selected={id:r.id,name:r.ap_name,band:r.band};$("detail").hidden=false;const d=$("detailBody");d.replaceChildren(el("p",r.ap_name+" · "+r.band+" · 측정값 의미 미검증"));const end=el("input");end.type="datetime-local";end.setAttribute("aria-label","종료 시각 KST");end.value=new Date((Math.ceil(Date.now()/60000)*60+60+32400)*1000).toISOString().slice(0,16);const hours=el("select");for(const v of [1,24,168]){const o=el("option","최근 "+v+"시간");o.value=String(v);hours.append(o);}hours.setAttribute("aria-label","조회 기간");const b=el("button","조회"),output=el("div");b.addEventListener("click",async()=>{const wanted=selected;if(!wanted||expired)return;const e=Date.parse(end.value+":00+09:00")/1000;const start=e-Number(hours.value)*3600;if(!Number.isFinite(e)||start<0){output.textContent="유효한 시간 범위를 입력하세요.";return;}const q=new URLSearchParams({id:wanted.id,start:String(start),end:String(e)});b.disabled=end.disabled=hours.disabled=true;try{const h=await request("/api/history?"+q);if(selected!==wanted||expired)return;output.replaceChildren(el("p",kst(h.start)+" ~ "+kst(h.end)+" · "+h.count+"건"+(h.truncated?" · 상한 도달":"")));if(!h.rows.length)output.append(el("p","저장된 관측 없음. 정상 또는 장애 해제를 의미하지 않습니다."));else {output.append(table(["시각 KST","사용률 %","간섭률 %","NF dBm","채널"],h.rows.map(x=>[kst(x.observed_at),x.utilization,x.interference,x.noise,x.channel])));const links=el("div",null,"pager");for(const fmt of ["csv","html"]){const a=el("a",fmt.toUpperCase()+" 다운로드");a.href="/api/history."+fmt+"?"+q;a.download="awqm-diagnostic-history."+fmt;links.append(a);}output.append(links);}}catch(e){if(!expired)output.textContent="조회 불가 · 잠시 후 다시 시도하세요.";}finally{b.disabled=end.disabled=hours.disabled=false;}});d.append(end,hours,b,output);}
const incidentLabels={DETECTED:"최초 위험",PERSISTENT:"지속 위험",RECOVERING:"회복 관측 중",RESOLVED:"RF 정책 정상화",TRANSIENT:"일시 위험",UNCONFIRMED:"현재 확인 불가",CONTEXT_LOST:"관측 조건 변경 · 정상화 아님"};
const candidateLabels={RF04:"Radio 혼잡 관측",UNKNOWN:"원인 미확인"};
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
    out.replaceChildren(el("h3",d.ap_name+" · "+d.band+" · "+id.slice(0,12)),
      el("p",incidentLabels[d.display_state]||d.display_state),el("p","서비스 영향: 미검증 · 실제 원인 확정 아님"),
      el("p","최초 "+kst(d.first_at)+" · 마지막 "+kst(d.last_at)),
      table(["관측 시각","상태","사용률","간섭률","NF","후보"],d.timeline.map(x=>[kst(x.at),incidentLabels[x.state]||x.state,x.evidence.utilization,x.evidence.interference,x.evidence.noise,x.evidence.candidates.map(c=>candidateLabels[c.code]||c.code).join(" / ")])));
    const revision=selectOptions(d.timeline.map(x=>String(x.revision)),Object.fromEntries(d.timeline.map(x=>[String(x.revision),"근거 #"+x.revision+" · "+kst(x.at)+" · "+(incidentLabels[x.state]||x.state)])),String(d.revision));
    const candidate=el("select"),basis=el("p"),verdict=selectOptions(["inconclusive","supported","not_supported"],verdictLabels,"inconclusive"),reason=selectOptions(["insufficient_evidence","radio_evidence","other_evidence","conflicting_evidence"],{insufficient_evidence:"자료 부족",radio_evidence:"Radio 관측 근거",other_evidence:"별도 운영자 확인",conflicting_evidence:"상충 자료"},"insufficient_evidence");
    function updateCandidates(){const ev=d.timeline.find(x=>String(x.revision)===revision.value);candidate.replaceChildren();for(const c of ev.evidence.candidates){const o=el("option",candidateLabels[c.code]||c.code);o.value=c.code;candidate.append(o);}basis.textContent=ev.evidence.candidates.map(c=>(candidateLabels[c.code]||c.code)+" · "+c.basis.join(", ")+" · "+c.limit).join(" / ");}
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
async function loadClients(){
  if(view!=="clients"||expired)return;
  const rev=++clientVersion,out=$("content");
  out.replaceChildren(el("p","단말 상세 관측 조회 중"));
  try{
    const q=new URLSearchParams({q:$("search").value,page:String(clientPage)});
    const d=await request("/api/clients?"+q);
    if(rev!==clientVersion||view!=="clients"||expired)return;
    out.replaceChildren(el("p",d.enabled?"현장 승인된 AP 정밀 조회의 임시 단말 관측입니다. MAC·SSID 원문 미표시, DB 미저장.":"단말 상세 수집 비활성 · 별도 현장 검증 필요"),
      el("p","품질은 Health·ACK SNR 기반 부분 점수입니다. 별도 검증된 Station Stats는 성공 프레임 중 재시도 비율만 표시합니다. Airtime·원인 기여는 미확인."));
    const refreshBtn=el("button","관측 갱신");refreshBtn.addEventListener("click",loadClients);out.append(refreshBtn);
    if(!d.rows.length)out.append(el("p","현재 조건을 충족하는 상세 관측이 없습니다. 단말이 없거나 정상이라는 의미는 아닙니다."));
    else out.append(table(["임시 단말 ID","AP","WLC","대역","Health %","ACK SNR dB","최근 PHY Mbps","부분 품질 점수","성공 Tx 관측","성공 프레임 재시도 %","통계 구간","Airtime","관측 시각"],d.rows.map(r=>[r.id.slice(0,16),r.ap_name,r.controller,r.band,r.health,r.snr,r.last_phy_rate,r.quality_score==null?"미확인":r.quality_score+" (부분)",({successful_tx_observed:"증가 관측",no_successful_tx_increase:"성공 Tx 증가 없음"})[r.station_tx_activity]||"미확인",r.station_retry==null?"미확인":r.station_retry.toFixed(1),r.station_start==null?"미확인":kst(r.station_start)+" ~ "+kst(r.station_end),"미확인",kst(r.observed_at)])));
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
  for(const [key,title] of [["ap","AP"],["client","Client"]]){
    const l=d.load[key],status={unknown:"미확인",single_source:"단일 출처",crosschecked:"교차 확인",conflicting_observations:"관측값 상충"}[l.status]||l.status;
    c.append(el("p",title+" 전체 Active "+(l.active_total??"미확인")+" / Standby "+(l.standby_total??"미확인")+" · "+status+" · "+(l.source||"—")+" · "+kst(l.observed_at)));
  }
  if(d.links.length)c.append(table(["관측 WLC","대상 WLC","연결 상태","연결 유형","시각"],d.links.map(r=>[r.from,r.to,r.state,r.link,kst(r.observed_at)])));
  return c;
}

function render(){
  if(expired)return;
  const c=$("content");c.replaceChildren();$("heading").textContent=names[view];
  if(view==="clients"){loadClients();return;}
  if(view==="history"){loadIncidents();return;}
  if(view==="admin"){const b=el("button","관리자 상태 조회"),out=el("div");b.addEventListener("click",async()=>{try{const a=await request("/api/admin/status");out.replaceChildren(el("p","인증서 잔여 "+a.certificate_days+"일"+(a.certificate_warning?" · 갱신 확인 필요":"")),el("p","감사 기록 "+a.audit.events+"건 · 최근 "+kst(a.audit.last_event_at)));}catch(e){if(!expired)out.textContent="관리자 권한 필요 또는 조회 불가";}});c.append(el("p","조회 전용 서비스 상태입니다. 장비 설정 및 자동 정밀 분석은 이 화면에서 변경할 수 없습니다."),b,out);return;}
  if(!snapshot){c.append(el("p","자료 확인 중"));return;}
  if(view==="overview"){const k=el("div",null,"kpis");k.append(metric("관측 WLC",snapshot.coherent_controllers==null?null:snapshot.coherent_controllers+" / "+snapshot.expected_controllers),metric("저장된 Radio",snapshot.inventory_radios),metric("현재 매핑 Radio",snapshot.mapped_radios),metric("활성 단말 관측",snapshot.active_clients));c.append(k,el("p","최근 게시: "+kst(snapshot.published_at)+" · "+(snapshot.available?"관측 자료 제공":"현재 관측 확인 불가")),clusterPanel(snapshot.cluster));}
  c.append(table(["AP","WLC","대역","채널","사용률 %","간섭률 %","NF dBm","품질 점수","위험 관측","관측 시각 KST","수집 상태"],snapshot.rows.map(r=>{const b=el("button",r.ap_name);b.addEventListener("click",()=>openHistory(r));const cell=el("div");cell.append(b);if(r.incident_id){const i=el("button","위험 이력");i.addEventListener("click",()=>openIncident(r.incident_id));cell.append(i);}return [cell,r.controller,r.band,r.channel,r.utilization,r.interference,r.noise,r.quality_score==null?"미검증":r.quality_score,r.risk_status==="UNKNOWN"?"미확인":r.risk_status,kst(r.observed_at),r.freshness==="observed"?(r.quality==="local_operator_attested"?"최근 관측 · 현장 승인 지표":"최근 관측 · 미검증"):"오래됨 / 확인 불가"];})));
  if(!snapshot.rows.length)c.append(el("p","저장 자료 또는 검색 결과 없음"));
  const pager=el("div",null,"pager"),prev=el("button","이전"),next=el("button","다음");
  prev.disabled=page<=1;next.disabled=page*snapshot.size>=snapshot.total;
  prev.addEventListener("click",()=>{page--;refresh(true);});next.addEventListener("click",()=>{page++;refresh(true);});
  pager.append(prev,el("span",page+" / "+Math.max(1,Math.ceil(snapshot.total/snapshot.size))+" · "+snapshot.total+"건"),next);c.append(pager);
}
async function refresh(force=false){if(expired||busy&&!force)return;const serial=++revision;busy=true;try{const q=new URLSearchParams({q:$("search").value,page:String(page)});const r=await request("/api/radios?"+q);if(serial!==revision||expired)return;snapshot=r;notice(r.available?"진단 관측 · 현장 승인된 Radio 지표만 정책 점수 제공 · 원인/서비스 장애 확정 금지":"수집 또는 저장 상태 확인 불가 · 마지막 관측을 현재 상태로 해석하지 마십시오.");if(view==="overview"||view==="radios")render();}catch(e){if(!expired&&serial===revision){notice("갱신 실패 · 현재 상태 확인 불가");snapshot=null;if(view==="overview"||view==="radios")render();}}finally{if(serial===revision)busy=false;}}
$("nav").addEventListener("click",e=>{const v=e.target.dataset.view;if(!v||expired)return;view=v;for(const b of $("nav").querySelectorAll("button"))b.removeAttribute("aria-current");e.target.setAttribute("aria-current","page");render();});
$("search").addEventListener("input",()=>{page=1;clientPage=1;if(view==="clients")loadClients();else refresh(true);});$("close").addEventListener("click",()=>{selected=null;incidentPanel=null;$("detail").hidden=true;$("detailBody").replaceChildren();});render();refresh();setInterval(()=>{if(view==="clients")loadClients();else refresh();},10000);
