"use strict";
const $=id=>document.getElementById(id);
let snapshot=null, view="overview", selected=null, clientMode="candidate";
const names={overview:"종합 현황",ap:"AP 품질 분석",client:"사용자 분석",events:"장애 및 이력",admin:"운영 관리"};
const methods={measured:"모의 실측",estimated:"모의 추정",indirect:"간접"};
const candidateNames={RF01:"채널 과점유 기여 의심",RF03:"재전송 기여 의심"};
const confidence={medium:"중간",low:"낮음",unavailable:"판정 불가"};
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
function clientRows(rows){return rows.map(c=>[clientButton(c),c.ap_name,c.band,c.ssid,c.activity==="active"?"활성":c.activity==="idle"?"유휴":"미확인",c.quality,c.retry===null?null:c.retry.toFixed(1)+"%",airtime(c),c.impact_score,candidateNames[c.candidate]||"—",confidence[c.confidence]]);}
const clientHeaders=["단말","AP","대역","SSID","활동","품질","재전송","전송시간","영향 점수","원인 후보","신뢰도"];
function detail(){
  const d=$("detailBody");$("detail").hidden=!selected;if(!selected)return;
  if(selected.type==="client"){
    const c=snapshot?.clients.find(x=>x.client_mac===selected.id);
    if(!c){$("detail").hidden=true;return;}
    const go=el("button","접속 Radio 분석");go.addEventListener("click",()=>choose({type:"radio",id:c.radio_id}));
    d.replaceChildren(el("h3",c.client_name+" · "+c.ap_name),el("p",c.ssid+" · "+c.band+" · "+c.client_mac),
      el("p","SNR "+c.snr+"dB · 마지막 PHY Rate "+c.rate+"Mbps · Health "+c.health+"%"),
      el("p","전송시간: "+airtime(c)+" · 품질 점수: "+(c.quality??"산정 불가")),
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
    for(const [key,label] of [["candidate","원인 후보"],["poor","품질 저하"],["all","전체 접속"]]){
      const b=el("button",label);b.setAttribute("aria-pressed",String(clientMode===key));b.addEventListener("click",()=>{clientMode=key;render();});controls.append(b);
    }
    c.append(controls);
    const list=cs.filter(x=>clientMode==="all"||clientMode==="candidate"&&x.candidate||clientMode==="poor"&&x.quality!==null&&x.quality<50);
    // Separate measured/estimated/indirect groups rather than a single ranking.
    for(const method of ["measured","estimated","indirect"]){
      const group=list.filter(x=>x.airtime_method===method);
      if(group.length)c.append(el("h3",methods[method]+" · "+group.length+"대"),table(clientHeaders,clientRows(group)));
    }
    if(!list.length)c.append(el("p","해당 조건에 맞는 단말 없음"));
  }else if(view==="admin"){
    c.append(el("p","모의 전용 · 실제 SSH 비활성 · 사내망 HTTPS/Aruba 인증/Windows Service 미구현"));
  }
  detail();
}
let refreshing=false;
async function refresh(){if(refreshing)return;refreshing=true;try{const r=await fetch("/api/snapshot",{cache:"no-store"});if(!r.ok)throw Error();snapshot=await r.json();document.querySelector(".notice").textContent="개발용 모의 화면 · 모든 수치와 분석 근거는 실제 사내 데이터가 아닙니다.";render();}catch(e){document.querySelector(".notice").textContent="갱신 실패 · 마지막 모의 데이터 표시";}finally{refreshing=false;}}
$("nav").addEventListener("click",e=>{const v=e.target.dataset.view;if(!v)return;view=v;for(const b of $("nav").querySelectorAll("button"))b.removeAttribute("aria-current");e.target.setAttribute("aria-current","page");selected=null;render();});
$("search").addEventListener("input",render);$("close").addEventListener("click",()=>{selected=null;detail();});refresh();setInterval(refresh,10000);
