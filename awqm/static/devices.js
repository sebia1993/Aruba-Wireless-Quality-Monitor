"use strict";
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get("settings");
const slotIds = ["WLC-1","WLC-2","WLC-3","WLC-4"];
let snapshot = null, selected = slotIds[0], page = 0, view = "overview", busy = false, loading = false, sequence = 0, intervalEdited = false, alertsEdited = false;
const headings = {overview:"종합 현황",settings:"장비 설정",observations:"실시간 관측",operations:"운영 설정"};
const fields = ["host","username","fingerprint","known_hosts"];
function status(message) { $("notice").textContent = message; }
function el(tag, value) {const node=document.createElement(tag);if(value!==undefined)node.textContent=String(value??"—");return node;}
function options(node) {
  for(const id of slotIds){const option=el("option",id);option.value=id;node.append(option);}
}
options($("slot"));options($("observed-slot"));
async function api(data) {
  if(!token) throw Error("Windows AWQM에서 웹 화면을 다시 여세요.");
  const response=await fetch("/api/desktop/devices",{method:"POST",cache:"no-store",
    headers:{"Content-Type":"application/json","X-AWQM-Settings":token},body:JSON.stringify(data)});
  const value=await response.json();
  if(!response.ok) throw Error(value.error||"조회 실패");
  return value;
}
function active() {return snapshot?.slots.find(s=>s.id===selected);}
function show(name) {
  view=name;
  for(const key of Object.keys(headings)) $(key).hidden=key!==name;
  $("heading").textContent=headings[name];
  for(const b of $("device-nav").querySelectorAll("[data-view]")) {
    if(b.dataset.view===name)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");
  }
  if(name==="observations")refresh();
}
function fill() {
  const s=active(), p=s?.profile;
  for(const k of fields)$(k).value=p?.[k]??(k==="username"?snapshot?.default_username??"":"");
  $("password").value="";$("remember").checked=!!s?.remember;
  $("credential-note").textContent=s?.has_password?"설정을 변경하지 않으면 기존 비밀번호를 사용할 수 있습니다.":"장비별 비밀번호를 입력하세요.";
}
function select(id) {
  selected=id;page=0;$("slot").value=id;$("observed-slot").value=id;fill();refresh();
}
function render() {
  if(!snapshot)return;
  const cards=$("cards");cards.replaceChildren();
  for(const s of snapshot.slots) {
    const card=el("div");card.className="kpi";
    card.append(el("span",s.id),el("strong",s.profile?.host??"미등록"),el("p",s.status+(s.stale?" · 이전 관측":"")));
    const b=el("button","장비 설정");b.type="button";b.addEventListener("click",()=>{select(s.id);show("settings");});card.append(b);
    cards.append(card);
  }
  for(const node of [$("slot"),$("observed-slot")])
    for(const option of node.options){const s=snapshot.slots.find(x=>x.id===option.value);option.textContent=option.value+" · "+(s?.profile?.host??"미등록");}
  if(!intervalEdited)$("interval").value=snapshot.interval;
  if(!alertsEdited)$("alerts-enabled").checked=!!snapshot.alerts_enabled;
  const history=$("alert-history");
  history.replaceChildren();
  if(!snapshot.alerts?.length)history.append(el("p","현재 실행 중 기록된 수집 오류 없음"));
  else {
    const list=el("ul");
    for(const alert of snapshot.alerts)list.append(el("li",new Date(alert.at*1000).toLocaleString("ko-KR")+" · "+alert.id+" · "+alert.status));
    history.append(list);
  }
  const s=active();
  $("remove").disabled=busy||!s?.profile;
  const data=snapshot.table;
  $("observed").textContent=[s?.observed_host??"관측 없음",s?.status,s?.connection,s?.cli,
    s?.observed_at?new Date(s.observed_at*1000).toLocaleString("ko-KR"):"시각 없음",s?.stale?"오래된 관측":""].filter(Boolean).join(" · ");
  const table=$("readings");table.replaceChildren();
  const thead=el("thead"), h=el("tr");for(const title of data.headers)h.append(el("th",title));thead.append(h);table.append(thead);
  const tbody=el("tbody");for(const row of data.rows){const tr=el("tr");for(const value of row)tr.append(el("td",value));tbody.append(tr);}table.append(tbody);
  $("page").textContent=data.total+"행 · "+(page+1)+"/"+Math.max(1,Math.ceil(data.total/data.page_size));
  $("previous").disabled=page===0;$("next").disabled=(page+1)*data.page_size>=data.total;
}
async function refresh() {
  const id=selected, table=$("table").value, p=page, order=++sequence;
  try {
    const data=await api({action:"read",id,table,page:p});
    if(order!==sequence)return;
    const initial=snapshot===null;snapshot=data;render();if(initial)fill();
    if(initial||loading)status("현재 PC의 실제 관측 · 미수집 데이터는 정상으로 간주하지 않습니다.");
    loading=false;
  } catch(error) {if(order===sequence){loading=true;status(error.message);}}
}
function form(action) {
  return {action,id:selected,host:$("host").value.trim(),username:$("username").value.trim(),
    password:$("password").value,remember:$("remember").checked,fingerprint:$("fingerprint").value.trim(),
    known_hosts:$("known_hosts").value.trim()};
}
async function act(data, message, after) {
  if(busy)return;
  busy=true;
  const controls=["test","save","stop","remove","default-account","save-interval","save-alerts","slot","observed-slot"];
  for(const id of controls)$(id).disabled=true;
  try {
    await api(data);status(message);
    if(after)after();
    await refresh();
    if(data.action==="save"||data.action==="remove")fill();
  }catch(error){status(error.message);}
  finally{busy=false;for(const id of controls)$(id).disabled=false;render();}
}
$("device-nav").addEventListener("click",e=>{if(e.target.dataset.view)show(e.target.dataset.view);});
$("sample").addEventListener("click",()=>{location.href="/#settings="+encodeURIComponent(token||"");});
$("slot").addEventListener("change",()=>select($("slot").value));
$("observed-slot").addEventListener("change",()=>select($("observed-slot").value));
$("table").addEventListener("change",()=>{page=0;refresh();});
$("previous").addEventListener("click",()=>{page=Math.max(0,page-1);refresh();});
$("next").addEventListener("click",()=>{page++;refresh();});
$("refresh").addEventListener("click",refresh);
$("test").addEventListener("click",()=>act(form("test"),"연결 테스트를 요청했습니다.",()=>{page=0;show("observations");}));
$("save").addEventListener("click",()=>act(form("save"),"장비를 저장하고 수집을 요청했습니다.",()=>{page=0;show("observations");}));
$("stop").addEventListener("click",()=>act({action:"stop",id:selected},"조회 중지를 요청했습니다."));
$("remove").addEventListener("click",()=>{if(confirm(selected+" 등록 정보와 저장된 비밀번호를 삭제할까요?"))act({action:"remove",id:selected},"등록을 삭제했습니다.");});
$("default-account").addEventListener("click",()=>act({action:"default_account",username:$("username").value.trim()},"새 장비 기본 계정을 변경했습니다."));
$("interval").addEventListener("input",()=>{intervalEdited=true;});
$("alerts-enabled").addEventListener("change",()=>{alertsEdited=true;});
$("save-alerts").addEventListener("click",()=>act({action:"alerts",enabled:$("alerts-enabled").checked},"수집 오류 알림 설정을 저장했습니다.",()=>{alertsEdited=false;}));
$("save-interval").addEventListener("click",()=>act({action:"interval",seconds:Number($("interval").value)},"수집 주기를 저장했습니다.",()=>{intervalEdited=false;}));
for(const id of ["host","username"])$(id).addEventListener("input",()=>{$("password").value="";$("remember").checked=false;});
refresh();setInterval(()=>{if(!busy)refresh();},5000);
