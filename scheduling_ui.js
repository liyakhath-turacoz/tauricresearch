"use strict";
let scheduleData={schedules:[],occurrences:[]},schedulePolling=false,rescheduling=null;
const scheduleBusy=new Set();
const istTime=value=>value?new Date(value).toLocaleString("en-IN",{timeZone:"Asia/Kolkata",dateStyle:"medium",timeStyle:"short"}):"Not yet";
$("scheduleDate").value=localDate();
$("scheduleDate").min=localDate();
function timingFields(prefix){
  const frequency=$(prefix+"Frequency").value;
  $(prefix+"DateField").hidden=frequency!=="once";
  $(prefix+"Date").required=frequency==="once";
}
function scheduleFields(){
  timingFields("schedule");
  globalThis.scheduleUI.sync();
}
function scheduleSelection(s){
  const x=s.selection;
  return x.kind==="symbols"?x.symbols.join(", "):x.kind==="group"?
    `${x.group} discovery - ${x.limit||"all"} assets`:`${x.scope} - ${x.sector} - top ${x.limit}`;
}
function renderSchedules(data){
  scheduleData=data;
  $("backgroundStatus").textContent=[data.background?.message,data.error].filter(Boolean).join(" ");
  $("scheduleRows").innerHTML=data.schedules.filter(s=>{const filter=$("scheduleListFilter").value;const active=data.occurrences.some(o=>o.schedule_id===s.id&&["queued","dispatching","running"].includes(o.status));return filter==="all"||(filter==="active"?active:filter==="stopped"?!s.enabled&&!active:s.enabled&&!active);}).map(s=>{
    const active=data.occurrences.find(o=>o.schedule_id===s.id&&["queued","dispatching","running"].includes(o.status));
    const latest=[...data.occurrences].reverse().find(o=>o.schedule_id===s.id);
    const state=active?active.status:s.enabled?(s.next_run?"Scheduled":"Finished"):"Stopped";
    const button=(action,label)=>`<button class="text-button" data-schedule="${action}" data-id="${esc(s.id)}" ${scheduleBusy.has(s.id)?"disabled":""}>${label}</button>`;
    return `<tr><td><strong>${esc(s.name)}</strong><small class="block">${esc(s.mode)} - ${esc(scheduleSelection(s))}</small><small class="block">Analysts: ${esc(s.analysts.join(", "))}</small></td><td>${esc(s.time)} IST<small class="block">${esc(s.frequency==="once"?s.run_on:s.frequency==="daily"?"Every day":s.weekdays.map(d=>["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d]).join(", "))}</small></td><td>${s.enabled?(s.next_run?esc(istTime(s.next_run)):"No future times"):"Stopped"}${s.enabled&&s.next_run?`<small class="block" data-starts-at="${esc(s.next_run)}"></small>`:""}</td><td>${esc(state)}<small class="block">Last due: ${esc(istTime(latest?.scheduled_at))}</small><small class="block">Last outcome: ${esc(latest?.status||s.last_status||"never")}</small></td><td><div class="button-row">${button("reschedule","Reschedule")}${s.enabled||active?button("stop","Stop"):button("resume","Resume")}${button("delete","Delete")}</div></td></tr>`;
  }).join("")||'<tr><td colspan="5" class="empty">No schedules. Use Schedule mode in the Individual or Bulk tab.</td></tr>';
  const active=data.occurrences.filter(o=>["queued","dispatching","running"].includes(o.status)).reverse();
  const finished=data.occurrences.filter(o=>!["queued","dispatching","running"].includes(o.status)).reverse().slice(0,100);
  $("occurrenceRows").innerHTML=[...active,...finished].map(o=>`<tr><td>${esc(o.name)}</td><td>${esc(istTime(o.scheduled_at))}</td><td>${esc(o.run_date)}</td><td>${esc(o.status)}<small class="block">${esc(o.message)}</small>${o.expires_at?`<small class="block expiry-timer" data-expires-at="${esc(o.expires_at)}"></small>`:""}</td><td><div class="button-row">${o.job_id?`<button class="text-button" data-job="${esc(o.job_id)}">View saved run</button>`:""}${["queued","dispatching"].includes(o.status)?`<button class="text-button" data-cancel="${esc(o.id)}">Cancel waiting</button>`:""}</div></td></tr>`).join("")||'<tr><td colspan="5" class="empty">No scheduled activity yet. Launched runs are saved in Saved runs &amp; reports.</td></tr>';
  updateTimers();
}
$("scheduleListFilter").addEventListener("change",()=>renderSchedules(scheduleData));
async function refreshSchedules(){
  if(schedulePolling)return;schedulePolling=true;
  try{renderSchedules(await api("/api/schedules"));}
  catch(e){$("backgroundStatus").textContent=e.message;}
  finally{schedulePolling=false;}
}
function timingPayload(prefix){
  const frequency=$(prefix+"Frequency").value;
  if(!$(prefix+"Time").value)throw new Error("Choose a run time in IST.");
  if(frequency==="once"&&!$(prefix+"Date").value)throw new Error("Choose a future run date.");
  return {frequency,time:$(prefix+"Time").value,run_on:frequency==="once"?$(prefix+"Date").value:null,weekdays:[]};
}
globalThis.scheduleUI={
  sync(){
    const inAnalysis=["bulk","individual"].includes(activeTab);
    const scheduled=inAnalysis&&$(activeTab==="bulk"?"scheduleBulk":"scheduleIndividual").checked;
    $("scheduleBanner").hidden=!scheduled;$("scheduleEditor").hidden=!scheduled;
    $("scheduleSelectionPreview").textContent=activeTab==="bulk"?
      `Bulk: ${selected.size} selected assets - ${[...selected.keys()].join(", ")}`:
      `Individual: ${$("ticker").value.trim().toUpperCase()}`;
  },
  async save(bulk){
    try{
      const {analysts}=runPayload();
      const symbols=bulk?[...selected.keys()]:[$("ticker").value.trim().toUpperCase()];
      if(!symbols.length||!symbols[0])throw new Error("Choose assets before scheduling.");
      const payload={name:$("scheduleName").value.trim()||(bulk?"Bulk assets review":symbols[0]+" review"),mode:bulk?"bulk":"individual",selection:{kind:"symbols",symbols},analysts,enabled:true,acknowledged:true,...timingPayload("schedule")};
      startInFlight=true;selectionChanged();
      await api("/api/schedules",{method:"POST",body:JSON.stringify(payload)});
      $(bulk?"scheduleBulk":"scheduleIndividual").checked=false;
      $("scheduleName").value="";
      await refreshSchedules();switchTab("schedules");
      notify("Schedule saved. Nothing ran now. Review its next run below; future results will appear in Saved runs & reports.","success");
    }catch(e){notify(e.message);}
    finally{startInFlight=false;selectionChanged();}
  }
};
for(const id of ["scheduleBulk","scheduleIndividual"])$(id).addEventListener("change",()=>{timingFields("schedule");selectionChanged();if($(id).checked)$("scheduleEditor").scrollIntoView({behavior:"smooth",block:"center"});});
$("saveInlineSchedule").addEventListener("click",()=>startRun(activeTab==="bulk"));
$("ticker").addEventListener("input",()=>globalThis.scheduleUI.sync());
$("scheduleFrequency").addEventListener("change",scheduleFields);
function editSchedule(s){
  rescheduling=JSON.parse(JSON.stringify(s));
  $("rescheduleSummary").textContent=s.name+" - "+scheduleSelection(s)+". Assets and analysts are read-only.";
  $("rescheduleFrequency").value=s.frequency==="weekly"?"once":s.frequency;$("rescheduleDate").value=s.run_on||localDate();$("rescheduleDate").min=localDate();$("rescheduleTime").value=s.time;
  $("rescheduleError").textContent="";timingFields("reschedule");
  $("rescheduleDialog").showModal();
}
$("rescheduleFrequency").addEventListener("change",()=>timingFields("reschedule"));
$("cancelReschedule").addEventListener("click",()=>$("rescheduleDialog").close());
$("rescheduleForm").addEventListener("submit",async event=>{
  event.preventDefault();if(!rescheduling||$("confirmReschedule").disabled)return;
  try{
    const payload={...rescheduling,...timingPayload("reschedule"),enabled:true,acknowledged:true};
    $("confirmReschedule").disabled=true;
    await api("/api/schedules",{method:"POST",body:JSON.stringify(payload)});
    $("rescheduleDialog").close();await refreshSchedules();notify("New timing saved. No run was started now.","success");
  }catch(e){$("rescheduleError").textContent=e.message;}
  finally{$("confirmReschedule").disabled=false;}
});
$("scheduleRows").addEventListener("click",async event=>{
  const b=event.target.closest("[data-schedule]");if(!b)return;
  const s=scheduleData.schedules.find(x=>x.id===b.dataset.id);if(!s||scheduleBusy.has(s.id))return;
  const action=b.dataset.schedule;
  if(action==="reschedule")return editSchedule(s);
  if(!confirm(action==="delete"?"Delete this schedule? Saved history stays. Running work continues; use Stop first to cancel it.":action==="stop"?"Stop future occurrences and stop this schedule's active analysis?":"Resume future scheduled runs using the configured AI APIs?"))return;
  scheduleBusy.add(s.id);renderSchedules(scheduleData);
  try{
    if(action==="resume")await api("/api/schedules",{method:"POST",body:JSON.stringify({...s,enabled:true,acknowledged:true})});
    else await api(`/api/schedules/${encodeURIComponent(s.id)}/${action}`,{method:"POST"});
    await refreshSchedules();await refreshStatus();
  }catch(e){notify(e.message);}
  finally{scheduleBusy.delete(s.id);renderSchedules(scheduleData);}
});
$("occurrenceRows").addEventListener("click",async event=>{
  const cancel=event.target.closest("[data-cancel]");
  if(cancel){
    cancel.disabled=true;
    try{renderSchedules(await api("/api/schedules/occurrences/"+encodeURIComponent(cancel.dataset.cancel)+"/cancel",{method:"POST"}));}
    catch(e){notify(e.message);}finally{cancel.disabled=false;}
  }
  const button=event.target.closest("[data-job]");if(button)openSavedJob(button.dataset.job);
});
$("pauseAllSchedules").addEventListener("click",async()=>{
  if(!confirm("Pause all future schedules and cancel waiting occurrences? Running analyses continue until stopped."))return;
  try{renderSchedules(await api("/api/schedules/pause-all",{method:"POST"}));}catch(e){notify(e.message);}
});
$("retryBackground").addEventListener("click",async()=>{
  try{renderSchedules(await api("/api/schedules/background",{method:"POST"}));}catch(e){notify(e.message);}
});
scheduleFields();refreshSchedules();setInterval(refreshSchedules,10000);
