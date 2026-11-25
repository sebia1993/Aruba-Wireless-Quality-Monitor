"use strict";
const $=id=>document.getElementById(id);
let snapshot=null, view="overview", selected=null, clientMode="candidate";
const names={overview:"종합 현황",ap:"AP 품질 분석",client:"사용자 분석",events:"장애 및 이력",admin:"운영 관리"};
const methods={measured:"모의 실측",estimated:"모의 추정",indirect:"간접"};
const candidateNames={RF01:"채널 과점유 기여 의심",RF03:"재전송 기여 의심"};
const confidence={medium:"중간",low:"낮음",unavailable:"판정 불가"};
const historyState=new Map();
const kst=t=>new Date(t*1000).toLocaleString("ko-KR",{timeZone:"Asia/Seoul"});
function historyPanel(r){
  let state=historyState.get(r.radio_id);
  if(!state){state={hours:"1",resolution:"raw",end:Math.ceil(Date.now()/60000)*60+60,result:null,query:null,error:null,loading:false};historyState.set(r.radio_id,state);}
  const section=el("section",undefined,"history-panel");
  section.append(el("h3","Radio 과거 이력 · KST"));
  const controls=el("div",undefined,"client-tabs"),range=el("select"),resolution=el("select");
  for(const [value,label] of [["1","최근 1시간"],["24","최근 24시간"],["168","최근 7일"],["720","최근 30일"],["8760","최근 365일"]]){
    const o=el("option",label);o.value=value;range.append(o);
  }
  for(const [value,label] of [["raw","원본"],["5m","5분 집계"],["1h","1시간 집계"]]){const o=el("option",label);o.value=value;resolution.append(o);}
  range.value=state.hours;range.setAttribute("aria-label","조회 기간");resolution.value=state.resolution;resolution.setAttribute("aria-label","데이터 해상도");
  const end=el("input");end.type="datetime-local";end.setAttribute("aria-label","종료 시각 KST");end.value=new Date((state.end+9*3600)*1000).toISOString().slice(0,16);
  function resetHistory(){state.result=null;state.query=null;state.error=null;detail();}
  range.addEventListener("change",()=>{state.hours=range.value;resetHistory();});
  resolution.addEventListener("change",()=>{state.resolution=resolution.value;resetHistory();});
  end.addEventListener("change",()=>{const t=Date.parse(end.value+":00+09:00")/1000;if(Number.isFinite(t)){state.end=Math.floor(t);resetHistory();}});
  const load=el("button",state.loading?"조회 중":"이력 조회");load.disabled=state.loading;
  range.disabled=resolution.disabled=end.disabled=state.loading;
  load.addEventListener("click",async()=>{
    if(state.loading)return;
    const max={raw:168,"5m":720,"1h":8760};
    if(Number(state.hours)>max[state.resolution]){state.error="선택한 해상도의 보존 기간을 초과했습니다.";state.result=null;state.query=null;detail();return;}
    const e=state.end,s=e-Number(state.hours)*3600;
    const q=new URLSearchParams({radio_id:r.radio_id,start:String(s),end:String(e),resolution:state.resolution}).toString();
    state.loading=true;state.error=null;state.result=null;state.query=null;detail();
    try{const response=await fetch("/api/history?"+q,{cache:"no-store"});if(!response.ok)throw Error();state.result=await response.json();state.query=q;}
    catch(err){state.error="이력을 조회하지 못했습니다.";}
    finally{state.loading=false;detail();}
  });
  controls.append(range,resolution,end,load);section.append(controls,el("p","원본 7일 · 5분 30일 · 1시간 365일 조회 범위. 집계가 생성되지 않은 구간은 미확인."));
  if(state.error)section.append(el("p",state.error,"warn"));
  if(state.result){
    const h=state.result;section.append(el("p",kst(h.start)+" ~ "+kst(h.end)+" · "+h.count+"건"+(h.truncated?" · 조회 상한 도달":"")+" · 모의 관측"));
    if(!h.rows.length)section.append(el("p","해당 구간에 저장된 자료가 없습니다. 정상 또는 장애 해제를 의미하지 않습니다."));
    else {
      const cols=h.resolution==="raw"?["observed_at","utilization","interference","noise","channel"]:["bucket","observed","valid","utilization_avg","utilization_max"];
      const names=h.resolution==="raw"?["관측 시각","사용률 %","간섭률 %","NF dBm","채널"]:["집계 시각","전체 표본","유효 표본","평균 사용률 %","최대 사용률 %"];
      section.append(table(names,h.rows.map(x=>cols.map((k,i)=>i===0?kst(x[k]):x[k]))));
      for(const format of ["csv","html"]){const a=el("a",format.toUpperCase()+" 다운로드");a.href="/api/history."+format+"?"+state.query;a.download="awqm-demo-history."+format;controls.append(a);}
    }
  }
  return section;
}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;}
function table(headers,rows){const wrap=el("div",undefined,"table-wrap"),t=el("table"),head=el("thead"),tr=el("tr");headers.forEach(x=>tr.append(el("th",x)));head.append(tr);t.append(head);const b=el("tbody");for(const row of rows){const r=el("tr");for(const v of row){const td=el("td");if(v instanceof Node)td.append(v);else td.textContent=v??"미수집";r.append(td);}b.append(r);}t.append(b);wrap.append(t);return wrap;}
function matches(values){const q=$("search").value.toLowerCase().trim();return values.some(x=>String(x??"").toLowerCase().includes(q));}
function filtered(){return (snapshot?.radios||[]).filter(r=>matches([r.ap_name,r.radio_id,r.controller,r.band])||(snapshot.clients||[]).some(c=>c.radio_id===r.radio_id&&matches([c.client_mac,c.client_name,c.ssid])));}
function clients(){return (snapshot?.clients||[]).filter(c=>matches([c.client_mac,c.client_name,c.radio_id,c.ap_name,c.ssid,c.band]));}
function kpi(name,value){const div=el("div",undefined,"kpi");div.append(el("span",name),el("strong",value));return div;}
function status(r){return el("span",r.status,r.status==="PERSISTENT"?"bad":r.status==="NORMAL"?"good":"warn");}
function choose(r){selected=r;detail();}
function button(r){const b=el("button",r.ap_name);b.addEventListener("click",()=>choose({type:"radio",id:r.radio_id}));return b;}
function clientButton(c){const b=el("button",c.client_name);b.addEventListener("click",()=>choose({type:"client",id:c.client_mac}));return b;}
function airtime(c){return c.airtime===null?"미확인":c.airtime.toFixed(1)+"% · "+methods[c.airtime_method];}
function clientRows(rows){return rows.map(c=>[clientButton(c),c.ap_name,c.band,c.ssid,c.activity==="active"?"활성":c.activity==="idle"?"유휴":"미확인",c.quality===null?null:c.quality+(c.quality_status==="partial"?" (부분)":""),
    c.retry===null?(c.station_retry==null?null:c.station_retry.toFixed(1)+"% (별도 구간)"):c.retry.toFixed(1)+"%",airtime(c),c.impact_score,candidateNames[c.candidate]||"—",confidence[c.confidence]]);}
const clientHeaders=["단말","AP","대역","SSID","활동","품질","재전송","전송시간","영향 점수","원인 후보","신뢰도"];
const pendingRequests=new Map();
let sendingJob=false;
const jobNames={queued:"대기",deferred:"보류",running:"실행 중",completed:"완료",failed:"실패",cancelled:"취소",expired:"만료",skipped:"생략"};
const jobReasons={target_anchor_unverified:"최신 앵커 확인 대기",controller_protection:"장비 보호 정책",restart_revalidate:"재시작 후 재검증",interrupted_outcome_unknown:"이전 실행 결과 미확인",field_validation_pending:"모의 파싱 완료",operator_cancel:"예약 취소",deadline_elapsed:"대기 만료"};
async function jobAction(path,data){
  if(sendingJob)return null;sendingJob=true;
  try{
    const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-AWQM-Demo":snapshot.demo_token},body:JSON.stringify(data)});
    if(!r.ok)throw Error("요청 거절 또는 접수 상태 미확인");
    return await r.json();
  }catch(e){
    document.querySelector(".notice").textContent="모의 작업 응답 미확인 · 같은 요청 ID로 재시도할 수 있습니다.";
    return null;
  }finally{sendingJob=false;}
}
function reserveButton(r){
  const b=el("button","모의 정밀 분석 예약");
  b.addEventListener("click",async()=>{
    if(sendingJob)return;
    const key=r.controller+"|"+r.ap_name;
    const id=pendingRequests.get(key)||("manual-"+crypto.randomUUID());
    pendingRequests.set(key,id);b.disabled=true;
    const job=await jobAction("/api/demo/jobs",{controller:r.controller,request_id:id,ap_name:r.ap_name});
    if(job){pendingRequests.delete(key);await refresh();}
    else {b.disabled=false;b.textContent="같은 요청으로 다시 확인";}
  });return b;
}
function jobRows(jobs){
  return jobs.map(j=>{
    const action=el("span","—");
    if(["queued","deferred"].includes(j.status)){
      const b=el("button","취소");b.addEventListener("click",async()=>{
        if(await jobAction("/api/demo/jobs/cancel",{controller:j.controller,job_id:j.job_id}))await refresh();
      });action.replaceChildren(b);
    }
    return [j.controller,j.target,j.job_id.slice(0,12),j.request_id.startsWith("auto-")?"자동":"수동",jobNames[j.status]||j.status,jobReasons[j.reason]||j.reason||"—",action];
  });
}
function detail(){
  const d=$("detailBody");$("detail").hidden=!selected;if(!selected)return;
  if(selected.type==="client"){
    const c=snapshot?.clients.find(x=>x.client_mac===selected.id);
    if(!c){$("detail").hidden=true;return;}
    const go=el("button","접속 Radio 분석");go.addEventListener("click",()=>choose({type:"radio",id:c.radio_id}));
    d.replaceChildren(el("h3",c.client_name+" · "+c.ap_name),el("p",c.ssid+" · "+c.band+" · "+c.client_mac),
      el("p","SNR "+c.snr+"dB · 마지막 PHY Rate "+c.rate+"Mbps · Health "+c.health+"%"),
      el("p","전송시간: "+airtime(c)+" · 품질 점수: "+(c.quality??"산정 불가")+(c.quality_status==="partial"?" (부분)":"")),
      el("p","별도 Station Stats 재전송 비율: "+(c.station_retry==null?"미확인":c.station_retry.toFixed(1)+"% · "+kst(c.station_observed_at))),
      el("p","원인 후보: "+(candidateNames[c.candidate]||"미확인")+" · 신뢰도 "+confidence[c.confidence]),
      el("p","비교 가능 주변 단말 "+c.peer_comparable+"대 · 품질 저하 "+(c.peer_degraded??"미확인")+"대"),
      el("p","판단 근거: "+(c.evidence.join(" / ")||"충분하지 않음")),el("p","운영자 검증 전 · 모든 지표는 모의 데이터"),go);
    return;
  }
  const r=snapshot?.radios.find(x=>x.radio_id===selected.id);
  if(!r){$("detail").hidden=true;return;}
  const cs=(snapshot.clients||[]).filter(c=>c.radio_id===r.radio_id);
  d.replaceChildren(el("h3",r.radio_id+" · "+r.controller+" · "+r.band+" · CH "+r.channel),
    el("p","채널 사용률 "+(r.utilization??"미수집")+"% · 간섭률 "+(r.interference??"미수집")+"% · NF "+(r.noise??"미수집")+" dBm"),
    el("p","품질 점수 "+(r.score??"산정 불가")+" · "+r.status),
    el("p","수집 시각: "+new Date(r.observed_at*1000).toLocaleString("ko-KR")),
    el("h3","모의 원인 후보 및 접속 단말"),table(clientHeaders,clientRows(cs)));
  d.append(historyPanel(r));
  if(snapshot.engine_demo){
    d.append(el("p","가상 CLI 수집 엔진 · 확보된 상세 관측만 표시 · Airtime 미검증"),reserveButton(r),
      table(["WLC","대상","작업 ID","유형","상태","사유","관리"],jobRows((snapshot.jobs||[]).filter(j=>j.controller===r.controller&&j.target===r.ap_name))));
  }
}
function render(){
  const c=$("content");c.replaceChildren();$("heading").textContent=names[view];
  if(!snapshot){c.append(el("p","데이터 연결 확인 중"));return;}
  const rows=filtered(),bad=rows.filter(r=>r.open_incident),cs=clients();
  if(view==="overview"){
    const ks=el("div",undefined,"kpis");
    ks.append(kpi("모의 AP",new Set(rows.map(r=>r.ap_id)).size),kpi("Radio",rows.length),kpi("지속 이상 Radio",bad.length),
      kpi("모의 원인 후보",cs.filter(r=>r.candidate).length),kpi("데이터 확보",rows.filter(r=>r.score!==null).length+"/"+rows.length));
    c.append(ks,el("h3","우선 조사 Radio"));
  }
  if(view==="ap"||view==="overview"){
    const rs=view==="overview"?bad:rows;
    c.append(table(["AP","대역","채널","사용률","간섭률","NF","품질","AAC(모의)","상태"],rs.map(r=>[button(r),r.band,r.channel,r.utilization===null?null:r.utilization+"%",r.interference===null?null:r.interference+"%",r.noise,r.score,r.controller,status(r)])));
    if(!rs.length)c.append(el("p","표시할 대상 없음"));
  }else if(view==="events"){
    c.append(table(["Radio","관측 시각","상태"],snapshot.events.filter(e=>rows.some(r=>r.radio_id===e.radio_id)).map(e=>[e.radio_id,new Date(e.observed_at*1000).toLocaleString("ko-KR"),e.status])));
  }else if(view==="client"){
    const controls=el("div",undefined,"client-tabs");
    for(const [key,label] of [["candidate","원인 후보"],["poor","품질 저하"],["all",snapshot.engine_demo?"상세 수집 단말":"전체 접속"]]){
      const b=el("button",label);b.setAttribute("aria-pressed",String(clientMode===key));b.addEventListener("click",()=>{clientMode=key;render();});controls.append(b);
    }
    c.append(controls);
    const list=cs.filter(x=>clientMode==="all"||clientMode==="candidate"&&x.candidate||clientMode==="poor"&&x.quality!==null&&x.quality<50);
    // Separate measured/estimated/indirect groups rather than a single ranking.
    for(const method of ["measured","estimated","indirect"]){
      const group=list.filter(x=>x.airtime_method===method);
      if(group.length)c.append(el("h3",methods[method]+" · "+group.length+"대"),table(clientHeaders,clientRows(group)));
    }
    if(!list.length)c.append(el("p",snapshot.engine_demo?"해당 조건의 유효한 상세 관측 없음. 전체 접속 수와 상세 분석 수는 다를 수 있습니다.":"해당 조건에 맞는 단말 없음"));
  }else if(view==="admin"){
    c.append(el("p","모의 전용 · 실제 SSH 비활성 · 사내망 HTTPS/Aruba 인증/Windows Service 미구현"));
    if(snapshot.engine_demo){
      const e=snapshot.engine||{};
      c.append(el("h3","가상 수집 엔진"),
        el("p","유효 WLC "+(e.coherent_controllers??0)+" / "+(e.expected_controllers??4)+" · AP Radio "+(e.mapped_radios??0)+" · 활성 단말 "+(e.active_clients??0)),
        el("p","DB 저장 상태: "+(e.storage_error||"정상")),
        el("p","자동 유지보수: "+(snapshot.maintenance?.status||"대기")+" · 디스크 "+(snapshot.maintenance?.disk_level||"미확인")),
        el("p","최근 백업: "+(snapshot.maintenance?.last_backup||"없음")+" · 원본 자동 삭제 보류"),
        el("h3","자동·수동 작업 대기열"),table(["WLC","대상","작업 ID","유형","상태","사유","관리"],jobRows(snapshot.jobs||[])));
    }
  }
  detail();
}
let refreshing=false;
async function refresh(){if(refreshing)return;refreshing=true;try{const r=await fetch("/api/snapshot",{cache:"no-store"});if(r.status===401){document.querySelector(".notice").textContent="인증 세션 만료 · 새로고침 후 다시 로그인하세요.";snapshot=null;selected=null;render();return;}if(!r.ok)throw Error();snapshot=await r.json();document.querySelector(".notice").textContent="개발용 모의 화면 · 모든 수치와 분석 근거는 실제 사내 데이터가 아닙니다.";render();}catch(e){document.querySelector(".notice").textContent="갱신 실패 · 마지막 모의 데이터 표시";}finally{refreshing=false;}}
$("nav").addEventListener("click",e=>{const v=e.target.dataset.view;if(!v)return;view=v;for(const b of $("nav").querySelectorAll("button"))b.removeAttribute("aria-current");e.target.setAttribute("aria-current","page");selected=null;render();});
$("search").addEventListener("input",render);$("close").addEventListener("click",()=>{selected=null;detail();});refresh();setInterval(refresh,10000);
