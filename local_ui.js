"use strict";
const $ = id => document.getElementById(id);
const TOKEN = document.querySelector('meta[name="local-ui-token"]').content;
const selected = new Map();
let discovery = {groups:{global:[],india:[],crypto:[]}}, scope = "global", sectorAssets = [];
let displayJob = null, liveJob = null, searchTimer, searchSequence = 0, lastJobSignature = "", sectorSequence = 0;
let polling = false, reportsLoaded = false, startInFlight = false, discoveryPolling = false, historySequence = 0, tickerSequence = 0, selectedStore = "";
let activeTab = "bulk";
let savedJobs=[],runView="all",viewingSaved=false,closedLiveJob=null,savedSignature="";
const savedJobBusy=new Set();
let serverCompatible=true;
let discoverySignature = "", renderedResultsKey = "";
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const inlineMarkdown=value=>esc(value).replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>").replace(/`([^`]+)`/g,"<code>$1</code>").replace(/\n/g,"<br>");
const shortDate = value => /^\d{8}$/.test(value || "") ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6,8)}` : value || "—";
const formattedTime = value => value ? new Date(value).toLocaleString([], {timeZone:"Asia/Kolkata",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const compact = value => value == null ? "—" : new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:1}).format(value);
const price = item => item.price == null ? "—" : `${new Intl.NumberFormat(undefined,{maximumFractionDigits:item.price < 1 ? 6 : 2}).format(item.price)} ${item.currency || ""}`;
const assetMarkup = item => `<div class="asset-cell"><span class="avatar ${item.asset_type === "crypto" ? "crypto" : ""}" aria-hidden="true">${esc(item.symbol.slice(0,2))}</span><span><strong>${esc(item.name || item.symbol)}</strong><small>${esc(item.symbol)}${item.exchange ? " · "+esc(item.exchange) : ""}</small></span></div>`;
const localDate = () => {const parts=new Intl.DateTimeFormat("en",{timeZone:"Asia/Kolkata",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date());const value=kind=>parts.find(p=>p.type===kind).value;return `${value("year")}-${value("month")}-${value("day")}`;};
try { for(const item of JSON.parse(localStorage.getItem("ta-selection") || "[]").slice(0,1000)) if(item && typeof item.symbol==="string" && /^[A-Za-z0-9.^=_&+-]{1,32}$/.test(item.symbol) && typeof item.name==="string") selected.set(item.symbol,item); } catch {}

async function api(path,options={}) {
  const response=await fetch(path,{...options,headers:{"X-Local-UI-Token":TOKEN,...(options.body?{"Content-Type":"application/json"}:{}),...(options.headers||{})}});
  if(response.status===401){location.replace("/");throw new Error("Please sign in again.");}
  let data;try{data=await response.json();}catch{throw new Error(`The local server returned an unreadable response (${response.status}). Check the runner status before retrying a run.`);}
  if(path==="/api/status"&&data.ui_version!==5){serverCompatible=false;selectionChanged();notify("The server is still running older code. Restart Start-LocalUI.ps1, then sign in again. No new analysis can start until the update is loaded.");throw new Error("Server restart required");}
  if(path==="/api/status")serverCompatible=true;
  if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);return data;
}
function notify(message,type="error") {delete $("notice").dataset.jobId;$("notice").textContent=message;$("notice").className="notice "+type;$("notice").hidden=false;}
function hideNotice(){$("notice").hidden=true;}
function switchTab(tab) {
  activeTab=tab;
  document.querySelectorAll("[data-tab]").forEach(button=>{const active=button.dataset.tab===tab;button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;$("panel-"+button.dataset.tab).hidden=!active;});
  const results=document.querySelector(".results-card");
  if(tab==="reports")$("savedResults").append(results);
  else {
    $("resultsHome").after(results);
    viewingSaved=false;$("jobHistory").value="";
    displayJob=liveJob?.running&&closedLiveJob!==liveJob.id?liveJob:null;
  }
  renderResults(displayJob);
  globalThis.scheduleUI?.sync();
  if(tab==="reports"){refreshHistory();if(!reportsLoaded)refreshReports();}
}

document.querySelectorAll("[data-tab]").forEach(button=>{
  button.addEventListener("click",()=>switchTab(button.dataset.tab));
  button.addEventListener("keydown",event=>{if(!["ArrowLeft","ArrowRight"].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll("[data-tab]")];const next=tabs[(tabs.indexOf(button)+(event.key==="ArrowRight"?1:tabs.length-1))%tabs.length];next.focus();switchTab(next.dataset.tab);});
});
function chooseIndividual(item) {
  $("ticker").value=item.symbol;$("individualAsset").innerHTML=assetMarkup(item);switchTab("individual");$("searchResults").hidden=true;$("ticker").focus();
}
function persistSelection(){try{const value=JSON.stringify([...selected.values()]);if(value!==selectedStore){localStorage.setItem("ta-selection",value);selectedStore=value;}}catch{}}
function selectionChanged() {
  const n=selected.size;$("countSelected").textContent=n;$("runCount").textContent=n;$("bulkSelectedBadge").textContent=n;
  $("selectedLabel").textContent=`${n} asset${n===1?"":"s"} selected`;
  $("selectionPreview").textContent=n?[...selected.keys()].slice(0,7).join(", ")+(n>7?` +${n-7} more`:""):"Select rows across any market or sector.";
  const scheduled=$("scheduleBulk").checked;
  $("runBulk").textContent=n?`${scheduled?"Schedule":"Run"} ${n} selected asset${n===1?"":"s"}`:`${scheduled?"Schedule":"Run"} selected assets`;
  $("runBulk").disabled=!serverCompatible||!n||startInFlight||(!scheduled&&!!liveJob?.running);
  $("runIndividual").textContent=$("scheduleIndividual").checked?"Schedule this asset":"Run individual analysis";
  $("runIndividual").disabled=!serverCompatible||startInFlight||(!$("scheduleIndividual").checked&&!!liveJob?.running);
  $("saveInlineSchedule").disabled=!serverCompatible||startInFlight;
  persistSelection();syncVisibleCheckbox();globalThis.scheduleUI?.sync();
}
function currentAssets() {
  const items=scope==="sector"?sectorAssets:discovery.groups[scope]||[];
  const q=$("filterAssets").value.trim().toLowerCase();let result=items.filter(item=>!q||(item.name+" "+item.symbol).toLowerCase().includes(q));
  const sort=$("sortAssets").value;
  if(sort!=="rank")result=[...result].sort((a,b)=>sort==="name"?a.name.localeCompare(b.name):sort==="volume"?(b.volume||0)-(a.volume||0):sort==="gainers"?(b.change_percent??-Infinity)-(a.change_percent??-Infinity):(a.change_percent??Infinity)-(b.change_percent??Infinity));
  return result;
}
function syncVisibleCheckbox(){const items=currentAssets();const n=items.filter(a=>selected.has(a.symbol)).length;$("selectVisible").checked=!!items.length&&n===items.length;$("selectVisible").indeterminate=n>0&&n<items.length;}
function renderAssets() {
  const items=currentAssets();$("visibleCount").textContent=`${items.length} assets`;
  const node=$("assetRows");node.innerHTML="";
  if(!items.length){node.innerHTML=`<tr><td colspan="7" class="empty">${discovery.state==="loading"&&scope!=="sector"?"Fetching Yahoo assets…":"No assets in this view. Review discovery coverage or try search."}</td></tr>`;syncVisibleCheckbox();return;}
  const fragment=document.createDocumentFragment();
  for(const item of items){
    const row=document.createElement("tr");row.classList.toggle("selected",selected.has(item.symbol));
    const change=item.change_percent;
    row.innerHTML=`<td class="check-column"><input type="checkbox" aria-label="Select ${esc(item.name)}" ${selected.has(item.symbol)?"checked":""}></td><td>${assetMarkup(item)}</td><td class="number">${esc(price(item))}</td><td class="number ${change>0?"positive":change<0?"negative":""}">${change==null?"—":`${change>0?"+":""}${change.toFixed(2)}%`}</td><td class="number">${compact(item.volume)}</td><td><span class="tag">${esc((item.tags||[]).join(" · ")||item.sector||"Yahoo")}</span></td><td><button class="text-button" title="Analyse this asset individually">Analyse</button></td>`;
    row.querySelector("input").addEventListener("change",e=>{e.target.checked?selected.set(item.symbol,item):selected.delete(item.symbol);row.classList.toggle("selected",e.target.checked);selectionChanged();});
    row.querySelector("button").addEventListener("click",()=>chooseIndividual(item));fragment.append(row);
  }
  node.append(fragment);syncVisibleCheckbox();
}
function setScope(next){if(next!=="sector"){sectorSequence++;$("loadSector").disabled=false;}scope=next;document.querySelectorAll(".segmented [data-scope]").forEach(b=>{b.classList.toggle("active",b.dataset.scope===scope);b.setAttribute("aria-pressed",String(b.dataset.scope===scope));});$("sectorControls").hidden=scope!=="sector";
  const titles={global:["Global stock discovery","A mix of Yahoo gainers, losers and most active stocks across supported markets."],india:["Indian stock discovery","100 additional symbols, excluding symbols already in the global selection."],crypto:["Cryptocurrency discovery","USD pairs ranked using 24-hour activity and market capitalisation."],sector:["Sector discovery","An additional targeted fetch, including stocks outside the initial 100-stock lists."]};
  $("universeTitle").textContent=titles[scope][0];$("universeSubtitle").textContent=titles[scope][1];$("filterAssets").value="";renderAssets();if(scope==="sector")loadSector();
}
document.querySelectorAll("[data-scope]").forEach(button=>button.addEventListener("click",()=>setScope(button.dataset.scope)));
$("filterAssets").addEventListener("input",renderAssets);$("sortAssets").addEventListener("change",renderAssets);
$("selectVisible").addEventListener("change",event=>{for(const item of currentAssets())event.target.checked?selected.set(item.symbol,item):selected.delete(item.symbol);selectionChanged();renderAssets();});
$("selectTwo").addEventListener("click",()=>{selected.clear();currentAssets().slice(0,2).forEach(a=>selected.set(a.symbol,a));selectionChanged();renderAssets();});
$("selectTwenty").addEventListener("click",()=>{selected.clear();currentAssets().slice(0,20).forEach(a=>selected.set(a.symbol,a));selectionChanged();renderAssets();});
$("clearSelection").addEventListener("click",()=>{selected.clear();selectionChanged();renderAssets();});

async function refreshDiscovery() {
  if(discoveryPolling)return;discoveryPolling=true;
  try{discovery=await api("/api/discovery");for(const [key,id] of [["global","countGlobal"],["india","countIndia"],["crypto","countCrypto"]])$(id).textContent=discovery.groups[key].length;
    $("discoveryStatus").textContent=discovery.state==="loading"?discovery.message:`${discovery.stale?"Some groups are stale · last refresh attempt":"Yahoo snapshot"} ${formattedTime(discovery.updated_at)} · latest available sessions`;
    $("coverageDetails").hidden=!(discovery.warnings?.length||discovery.coverage?.length);$("coverageCount").textContent=discovery.warnings?.length?`(${discovery.warnings.length})`:"";
    $("coverageNotes").replaceChildren(...[...(discovery.warnings||[]),discovery.coverage?.length?"Global exchanges returned: "+discovery.coverage.join(", "):""].filter(Boolean).map(t=>{const p=document.createElement("p");p.textContent=t;return p;}));
    for(const items of Object.values(discovery.groups))for(const item of items)if(selected.has(item.symbol))selected.set(item.symbol,item);
    const signature=`${discovery.state}:${discovery.updated_at}`;if(signature!==discoverySignature){discoverySignature=signature;renderAssets();}selectionChanged();$("refreshDiscovery").disabled=discovery.state==="loading";
  }catch(e){$("discoveryStatus").textContent="Discovery unavailable: "+e.message;$("refreshDiscovery").disabled=false;}finally{discoveryPolling=false;}
}
$("refreshDiscovery").addEventListener("click",async()=>{try{await api("/api/discovery/refresh",{method:"POST",body:"{}"});await refreshDiscovery();}catch(e){notify(e.message);}});
async function loadSector(){const sequence=++sectorSequence;sectorAssets=[];renderAssets();$("loadSector").disabled=true;$("universeSubtitle").textContent="Fetching this sector independently from Yahoo…";try{
  const query=new URLSearchParams({scope:$("sectorScope").value,sector:$("sectorSelect").value,limit:$("sectorLimit").value});const data=await api("/api/sectors?"+query);if(sequence!==sectorSequence||scope!=="sector")return;
  sectorAssets=data.assets;$("universeSubtitle").textContent=`${data.scope==="india"?"India":"Global"} · ${data.sector} · ${data.assets.length} stocks. ${data.warning||data.ranking}`;renderAssets();
}catch(e){if(sequence===sectorSequence&&scope==="sector"){notify(e.message);$("universeSubtitle").textContent="Sector fetch failed. Try again.";}}finally{if(sequence===sectorSequence)$("loadSector").disabled=false;}}
$("loadSector").addEventListener("click",loadSector);["sectorSelect","sectorScope","sectorLimit"].forEach(id=>$(id).addEventListener("change",loadSector));

$("assetSearch").addEventListener("input",()=>{clearTimeout(searchTimer);const q=$("assetSearch").value.trim();const seq=++searchSequence;if(q.length<2){$("searchResults").hidden=true;return;}
  searchTimer=setTimeout(async()=>{$("searchResults").hidden=false;$("searchResults").innerHTML='<div class="search-empty">Searching Yahoo Finance…</div>';try{const data=await api("/api/search?q="+encodeURIComponent(q));if(seq!==searchSequence)return;const node=$("searchResults");node.innerHTML="";
    if(!data.assets.length){node.innerHTML='<div class="search-empty">No stock or cryptocurrency matches.</div>';return;}
    for(const item of data.assets){const row=document.createElement("div");row.className="search-result";row.innerHTML=`<button type="button"><strong>${esc(item.name)}</strong><small>${esc(item.symbol)} · ${esc(item.asset_type)}${item.exchange?" · "+esc(item.exchange):""}</small></button><button type="button" class="secondary small">+ Bulk</button>`;row.children[0].addEventListener("click",()=>chooseIndividual(item));row.children[1].addEventListener("click",()=>{selected.set(item.symbol,item);selectionChanged();renderAssets();notify(`${item.name} added to bulk selection.` ,"success");});node.append(row);}
  }catch(e){if(seq===searchSequence)$("searchResults").innerHTML=`<div class="search-empty">${esc(e.message)}</div>`;}},400);
});
$("assetSearch").addEventListener("keydown",event=>{if(event.key==="Escape")$("searchResults").hidden=true;});
document.addEventListener("click",event=>{if(!event.target.closest(".search-wrap"))$("searchResults").hidden=true;});
$("resolveTicker").addEventListener("click",async()=>{const sequence=++tickerSequence,symbol=$("ticker").value.trim().toUpperCase();$("resolveTicker").disabled=true;try{const item=await api("/api/resolve?symbol="+encodeURIComponent(symbol));if(sequence===tickerSequence&&$("ticker").value.trim().toUpperCase()===symbol)chooseIndividual(item);}catch(e){notify(e.message);}finally{if(sequence===tickerSequence)$("resolveTicker").disabled=false;}});
$("ticker").addEventListener("input",()=>{tickerSequence++;$("resolveTicker").disabled=false;$("individualAsset").textContent="Enter a Yahoo ticker and choose Find asset to resolve its name.";});

function runPayload(){return {date:localDate(),analysts:["market","sentiment","news","fundamentals"]};}
async function startRun(bulk){
  if(startInFlight||!serverCompatible)return;
  if($(bulk?"scheduleBulk":"scheduleIndividual").checked)return globalThis.scheduleUI.save(bulk);
  try {
    hideNotice();const payload=runPayload();
    if(bulk){if(!selected.size)throw new Error("Select assets first.");payload.symbols=[...selected.keys()];}
    else payload.ticker=$("ticker").value.trim();
    startInFlight=true;selectionChanged();$("runIndividual").disabled=true;
    const started=await api(bulk?"/api/bulk":"/api/run",{method:"POST",body:JSON.stringify(payload)});
    $("jobHistory").value="";displayJob=null;viewingSaved=false;closedLiveJob=null;await refreshStatus();await refreshHistory();
    if(liveJob?.id===started.job_id&&!liveJob.running)notify("Run finished. Open Saved runs & reports for its results.","success");
    else{notify(`Started ${bulk?payload.symbols.length:"1"} asset analysis job. Completed decisions save automatically.`,"success");$("notice").dataset.jobId=started.job_id;}
    if(!document.querySelector(".results-card").hidden)$("jobMessage").scrollIntoView({behavior:"smooth",block:"center"});
  } catch(e){notify(e.message);}
  finally{startInFlight=false;selectionChanged();}
}
$("runBulk").addEventListener("click",()=>startRun(true));$("individualForm").addEventListener("submit",event=>{event.preventDefault();startRun(false);});
$("stopAnalysis").addEventListener("click",()=>{if(displayJob?.id)runAction(displayJob.id,"stop");});
$("deleteRun").addEventListener("click",()=>{if(displayJob?.id)runAction(displayJob.id,"delete");});
$("closeRun").addEventListener("click",()=>{closedLiveJob=displayJob?.id;viewingSaved=false;displayJob=null;historySequence++;$("jobHistory").value="";renderResults(null);});
function renderResults(job) {
  const visible=!!job?.id&&(activeTab==="reports"?viewingSaved:["bulk","individual"].includes(activeTab)&&job.running&&closedLiveJob!==job.id);
  document.querySelector(".results-card").hidden=!visible;
  $("deleteRun").hidden=!job?.id||!!job.running;
  $("stopAnalysis").hidden=!job?.running||job.id!==liveJob?.id;
  $("runDeadline").dataset.expiresAt=job?.expires_at||"";
  $("runDeadline").textContent=job?.expires_at?"Auto-removal: "+timeLeft(job.expires_at):"";

  const extension=globalThis.rangeUI;extension?.observe(job);const showRanges=!!extension?.enabled;
  const header=document.querySelector(".results-table thead tr");if(header.dataset.ranges!==String(showRanges)){header.dataset.ranges=String(showRanges);header.innerHTML="<th>Asset / symbol</th><th>Recommendation</th><th>Final rating</th><th>Analysis date</th><th>Status</th><th>Report</th>"+(showRanges?"<th>Estimated low</th><th>Estimated high</th>":"");}
  const empty=!job||!job.results;$("jobState").textContent=empty?"IDLE":job.state.toUpperCase();$("jobState").className="state-badge "+(job?.state||"");$("jobMessage").textContent=job?.message||"Ready for a new analysis.";
  $("jobProgress").max=job?.total||1;$("jobProgress").value=job?.completed||0;$("jobProgressLabel").textContent=`${job?.completed||0} / ${job?.total||0} completed`;
  $("exportCsv").hidden=!job?.id;if(job?.id){$("exportCsv").href=`/api/jobs/${job.id}/csv`;$("exportCsv").download=`TradingAgents-${job.date}-${job.id}.csv`;}
  const filter=$("resultFilter").value;const items=(job?.results||[]).filter(row=>filter==="all"||(filter==="failed"?row.status==="failed":row.recommendation===filter));const node=$("resultRows");
  const rowsKey=JSON.stringify([job?.id,filter,showRanges,extension?.revision,items]);
  if(rowsKey!==renderedResultsKey){const opened=new Set();node.querySelectorAll("tr[data-symbol]").forEach(row=>row.querySelectorAll("details").forEach((d,i)=>{if(d.open)opened.add(row.dataset.symbol+":"+i);}));renderedResultsKey=rowsKey;node.innerHTML="";
  if(!items.length)node.innerHTML=`<tr><td colspan="${showRanges?8:6}" class="empty">No results in this view yet. Completed analyses appear here automatically.</td></tr>`;
  for(const item of items){const row=document.createElement("tr");row.dataset.symbol=item.symbol;row.innerHTML=`<td>${assetMarkup(item)}${item.error?`<small class="row-error">${esc(item.error)}</small>`:""}${item.log_tail?`<details class="decision-detail"><summary>Error output</summary><pre>${esc(item.log_tail)}</pre></details>`:""}${item.summary?`<details class="decision-detail"><summary>Decision summary</summary><p>${inlineMarkdown(item.summary)}</p></details>`:""}</td><td>${item.recommendation?`<span class="signal ${esc(item.recommendation)}">${esc(item.recommendation)}</span>`:'<span class="muted">—</span>'}</td><td>${esc(item.rating||"—")}</td><td class="number" style="text-align:left">${esc(item.date)}</td><td><span class="row-status ${esc(item.status)}">${esc(item.status)}</span></td><td>${item.report_id?`<a href="/report/${encodeURIComponent(item.report_id)}" target="_blank" rel="noopener">Open report ↗</a>`:"—"}</td>`;if(showRanges)row.insertAdjacentHTML("beforeend",extension.cells(item));row.querySelectorAll("details").forEach((d,i)=>{d.open=opened.has(item.symbol+":"+i);});node.append(row);}
  }
  const log=$("jobLog"),content=(job?.log||[]).join("\n");if(log.textContent!==content){const nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<70;log.textContent=content;if(nearBottom)log.scrollTop=log.scrollHeight;}
}
async function refreshStatus(){
  if(polling)return;polling=true;
  try{
    liveJob=await api("/api/status");
    if($("notice").dataset.jobId===liveJob.id&&!liveJob.running)hideNotice();
    $("connectionDot").className="dot online";$("connectionText").textContent="Runner online";selectionChanged();
    if(viewingSaved&&activeTab==="reports"){
      if(displayJob?.id===liveJob.id){displayJob=liveJob;renderResults(displayJob);}
      else globalThis.rangeUI?.observe(displayJob);
    }else if(["bulk","individual"].includes(activeTab)){
      displayJob=liveJob.running&&closedLiveJob!==liveJob.id?liveJob:null;renderResults(displayJob);
    }
    const signature=`${liveJob.id}:${liveJob.state}:${liveJob.completed}`;
    if(signature!==lastJobSignature){lastJobSignature=signature;await refreshHistory();if(!liveJob.running)refreshReports();}
  }catch(e){$("connectionDot").className="dot offline";$("connectionText").textContent="Runner offline";$("jobMessage").textContent=e.message;}
  finally{polling=false;}
}
function runCategory(job){
  return job.running||["running","stopping"].includes(job.state)?"running":["done","done_with_errors"].includes(job.state)?"completed":"stopped";
}
function timeLeft(value){
  const seconds=Math.max(0,Math.floor((new Date(value).getTime()-Date.now())/1000));
  return seconds===0?"Expired - cleanup pending":`${Math.floor(seconds/3600)}h ${Math.floor(seconds%3600/60)}m ${seconds%60}s left`;
}
function updateTimers(){
  document.querySelectorAll("[data-expires-at]").forEach(node=>{if(node.dataset.expiresAt)node.textContent="Auto-removal: "+timeLeft(node.dataset.expiresAt);});
  document.querySelectorAll("[data-starts-at]").forEach(node=>{if(node.dataset.startsAt)node.textContent="Starts in: "+timeLeft(node.dataset.startsAt).replace(" left","").replace("Expired - cleanup pending","due now");});
}
function renderSavedHistory(){
  document.querySelectorAll("[data-run-view]").forEach(button=>{
    const kind=button.dataset.runView;button.setAttribute("aria-pressed",String(kind===runView));
    const count=kind==="all"?savedJobs.length:savedJobs.filter(j=>runCategory(j)===kind).length;
    button.textContent=({all:"All runs",running:"Running",stopped:"Stopped / failed",completed:"Completed"})[kind]+" ("+count+")";
  });
  const signature=JSON.stringify([savedJobs,runView,[...savedJobBusy]]);if(signature===savedSignature){updateTimers();return;}savedSignature=signature;
  const items=savedJobs.filter(j=>runView==="all"||runCategory(j)===runView);
  $("savedRunRows").innerHTML=items.map(j=>`<tr><td>${esc(formattedTime(j.created_at))}</td><td>${esc(j.mode)}<small class="block">${esc(j.source?.type==="schedule"?"Scheduled: "+j.source.schedule_name:"Manual run")}</small></td><td>${esc(j.date)}</td><td>${j.completed||0} / ${j.total}</td><td>${esc(j.state)}${j.expires_at?`<small class="block expiry-timer" data-expires-at="${esc(j.expires_at)}"></small>`:""}</td><td><div class="button-row"><button class="text-button" data-saved-job="${esc(j.id)}">Open run</button><button class="text-button" data-run-action="${runCategory(j)==="running"?"stop":"delete"}" data-id="${esc(j.id)}" ${savedJobBusy.has(j.id)?"disabled":""}>${runCategory(j)==="running"?"Stop":"Delete"}</button></div></td></tr>`).join("")||'<tr><td colspan="6" class="empty">No runs in this category. Stopped runs open only when you choose Open run.</td></tr>';
  updateTimers();
}
$("runViews").addEventListener("click",event=>{const button=event.target.closest("[data-run-view]");if(button){runView=button.dataset.runView;renderSavedHistory();}});
async function runAction(id,action){
  if(savedJobBusy.has(id))return;
  if(!confirm(action==="delete"?"Delete this run and its saved range data? Separately saved report files are kept.":"Stop this run and cancel its remaining assets?"))return;
  savedJobBusy.add(id);renderSavedHistory();
  try{
    await api("/api/jobs/"+encodeURIComponent(id)+"/"+action,{method:"POST"});
    if(displayJob?.id===id){viewingSaved=false;displayJob=null;$("jobHistory").value="";historySequence++;renderResults(null);}
    await refreshHistory();await refreshStatus();
  }catch(e){notify(e.message);}
  finally{savedJobBusy.delete(id);renderSavedHistory();}
}

async function refreshHistory(){
  try{
    const data=await api("/api/jobs");savedJobs=data.jobs;
    const value=$("jobHistory").value;
    $("jobHistory").innerHTML='<option value="">Latest / active run</option>'+savedJobs.map(j=>`<option value="${esc(j.id)}">${esc(j.date)} - ${j.total} assets - ${esc(j.state)}</option>`).join("");
    if(value&&savedJobs.some(j=>j.id===value))$("jobHistory").value=value;
    else if(value){viewingSaved=false;displayJob=null;historySequence++;renderResults(null);}
    renderSavedHistory();
  }catch(e){notify(e.message);}
}
async function openSavedJob(id){
  const sequence=++historySequence;
  try{
    const job=await api("/api/jobs/"+encodeURIComponent(id));if(sequence!==historySequence)return;
    await refreshHistory();if(sequence!==historySequence)return;
    if(!savedJobs.some(j=>j.id===id))throw new Error("This run was deleted or expired.");
    $("jobHistory").value=id;displayJob=job;viewingSaved=true;switchTab("reports");renderResults(job);
  }catch(e){notify(e.message);}
}
$("savedRunRows").addEventListener("click",event=>{
  const action=event.target.closest("[data-run-action]");
  if(action)return runAction(action.dataset.id,action.dataset.runAction);
  const button=event.target.closest("[data-saved-job]");if(button)openSavedJob(button.dataset.savedJob);
});

$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{method:"POST"});location.replace("/");}catch(e){notify(e.message);}});
$("jobHistory").addEventListener("change",()=>{const id=$("jobHistory").value;if(id)openSavedJob(id);else{viewingSaved=false;displayJob=null;historySequence++;renderResults(null);}});
$("resultFilter").addEventListener("change",()=>renderResults(displayJob));
async function refreshReports(){try{const data=await api("/api/reports");reportsLoaded=true;$("reportRows").innerHTML=data.reports.length?data.reports.map(item=>`<tr><td>${assetMarkup({...item,symbol:item.ticker})}</td><td class="number" style="text-align:left">${esc(shortDate(item.date))}</td><td>${esc(item.rating||"See decision")}</td><td>${esc(item.generated)}</td><td><a href="/report/${encodeURIComponent(item.id)}" target="_blank" rel="noopener">Open report ↗</a></td></tr>`).join(""):'<tr><td colspan="5" class="empty">Your saved reports will appear here.</td></tr>';}catch(e){$("reportRows").innerHTML=`<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`;}}
$("refreshReports").addEventListener("click",()=>{refreshReports();refreshHistory();});
$("stopServer").addEventListener("click",async()=>{if(!confirm("Stop the UI server and active analysis? Enabled schedules can restart it within a minute. To keep it stopped, first use Schedules → Pause all schedules."))return;try{await api("/api/shutdown",{method:"POST",body:"{}"});notify("UI server stopped. Enabled Windows scheduling may restart it; otherwise use Start-LocalUI.ps1.","success");}catch(e){notify(e.message);}});
function clock(){updateTimers();$("localClock").textContent=new Date().toLocaleTimeString([],{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit"})+" IST";$("scheduleDate").min=localDate();}
clock();setInterval(clock,1000);selectionChanged();refreshDiscovery();refreshStatus();refreshReports();setInterval(refreshStatus,2000);setInterval(refreshDiscovery,12000);setInterval(refreshHistory,10000);
