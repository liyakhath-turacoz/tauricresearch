/* Offline DOM regressions: no real server, market requests, credentials or AI. */
const fs=require("node:fs"),path=require("node:path"),os=require("node:os"),assert=require("node:assert/strict");
const {JSDOM}=require(process.env.TA_UI_AUDIT_DEPS||path.join(os.tmpdir(),"TradingAgentsUiAudit","node_modules","jsdom"));
const root=path.resolve(__dirname,".."),copy=x=>JSON.parse(JSON.stringify(x)),tick=()=>new Promise(r=>setImmediate(r));
const answer=(value,status=200)=>({ok:status<400,status,json:async()=>copy(value)});
const asset=symbol=>({symbol,name:symbol+" company",asset_type:"stock",currency:"USD",price:100,volume:1000,change_percent:1,tags:[]});
const row={...asset("NVDA"),date:"2026-10-20",status:"done",rating:"Underweight",recommendation:"SELL",summary:"**Decision** <img src=x onerror=bad()>",report_id:"saved-fixture"};
const base={id:"20261020_160000_1234abcd",state:"stopped",running:false,mode:"bulk",date:"2026-10-20",created_at:new Date(Date.now()-16*3600000).toISOString(),expires_at:new Date(Date.now()+8*3600000).toISOString(),total:2,completed:1,results:[row],log:[],message:"Queue stopped"};
let checks=0;
const check=(condition,message)=>{assert.ok(condition,message);checks++;};
async function main(){
  const html=fs.readFileSync(path.join(root,"LOCAL_UI.html"),"utf8");
  const dom=new JSDOM(html,{url:"http://127.0.0.1:8765",runScripts:"outside-only",pretendToBeVisual:true});
  const w=dom.window,d=w.document,calls=[],jobs=new Map([[base.id,copy(base)]]);
  let status=copy(base),settings={enabled:false,interval_minutes:15},schedules={schedules:[],occurrences:[],background:{message:"Fixture background status"}},sequence=0;
  const holds=new Map();
  w.confirm=()=>true;w.Element.prototype.scrollIntoView=()=>{};w.setInterval=()=>0;
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  w.fetch=async(url,options={})=>{
    const method=options.method||"GET",body=options.body?JSON.parse(options.body):null;
    calls.push({url,method,body});
    if(holds.has(method+" "+url))return holds.get(method+" "+url);
    if(url==="/api/status")return answer({...status,ui_version:5});
    if(url==="/api/discovery")return answer({state:"ready",groups:{global:[asset("NVDA"),asset("AAPL")],india:[],crypto:[]},warnings:[],coverage:[],updated_at:"2026-10-20T16:00:00Z"});
    if(url==="/api/reports")return answer({reports:[]});
    if(url==="/api/jobs")return answer({jobs:[...jobs.values()]});
    if(url==="/api/ranges/settings"){if(method==="POST")settings=copy(body);return answer(settings);}
    if(url.startsWith("/api/jobs/")){
      const parts=url.split("/"),id=parts[3],job=jobs.get(id);
      if(parts[4]==="ranges")return answer({status:"not_recorded",records:{}});
      if(parts[4]==="delete"){
        if(job?.running)return answer({error:"Stop first"},409);
        jobs.delete(id);if(status.id===id)status={state:"idle",running:false,results:[],log:[]};
        return answer({ok:true});
      }
      if(parts[4]==="stop"){
        if(!job?.running||status.id!==id)return answer({error:"Not active"},409);
        job.running=false;job.state="stopped";status=copy(job);return answer({ok:true});
      }
      return job?answer(job):answer({error:"Not found"},400);
    }
    if(url==="/api/schedules"){
      if(method==="POST"){
        const item={...body,id:body.id||(++sequence).toString(16).padStart(16,"0"),revision:(body.revision||0)+1,next_run:"2030-10-21T04:00:00Z",last_status:"never"};
        schedules.schedules=[...schedules.schedules.filter(s=>s.id!==item.id),item];return answer(item);
      }
      return answer(schedules);
    }
    if(url.startsWith("/api/schedules/occurrences/")){schedules.occurrences[0].status="cancelled";return answer(schedules);}
    if(url.startsWith("/api/schedules/")){
      const parts=url.split("/"),id=parts[3],item=schedules.schedules.find(s=>s.id===id);
      if(parts[4]==="delete")schedules.schedules=schedules.schedules.filter(s=>s.id!==id);
      if(parts[4]==="stop")item.enabled=false;
      return answer(schedules);
    }
    throw Error("Unexpected offline request: "+method+" "+url);
  };
  const scripts=["local_ui.js","scheduling_ui.js","price_ranges_ui.js"].map(p=>fs.readFileSync(path.join(root,p),"utf8")).join("\n");
  w.eval(scripts+"\nglobalThis.audit={selected,switchTab,startRun,refreshStatus,refreshHistory,renderResults,openSavedJob,renderSavedHistory,updateTimers,runAction,rangeUI,showRangeSettings,renderSchedules,editSchedule,refreshSchedules,getDisplay:()=>displayJob};");
  await tick();await tick();await tick();
  const a=w.audit;
  const ids=[...d.querySelectorAll("[id]")].map(n=>n.id);
  check(ids.length===new Set(ids).size,"HTML IDs are unique");
  check(!d.querySelector("#paperForm,#paperControls")&&!/rollback|cash_budget|paper_ui/i.test(html),"Position planner and rollback controls are removed");
  check(d.querySelector(".results-card").hidden,"Stopped results are not forced into live panels on startup");
  check(d.querySelectorAll(".results-table > thead th").length===6,"Feature off restores six standard columns");
  check(d.querySelectorAll("[data-run-view]").length===4,"Running/stopped/completed/all views exist");
  a.switchTab("reports");
  d.querySelector('[data-run-view="running"]').click();
  check(!d.querySelector("[data-saved-job]"),"Stopped run is absent from Running view");
  d.querySelector('[data-run-view="stopped"]').click();
  check(d.querySelectorAll("[data-saved-job]").length===1,"Stopped run is available in its own view");
  a.updateTimers();
  check(/[78]h /.test(d.querySelector("#savedRunRows .expiry-timer").textContent),"Countdown reflects remaining eight hours, not a fresh 24 hours");
  const timer=d.querySelector("#savedRunRows .expiry-timer").dataset.expiresAt;
  a.renderSavedHistory();
  check(d.querySelector("#savedRunRows .expiry-timer").dataset.expiresAt===timer,"Rendering never changes original expiry");
  await a.openSavedJob(base.id);
  check(!d.querySelector(".results-card").hidden&&d.getElementById("savedResults").contains(d.querySelector(".results-card")),"Stopped details open only on request in Saved tab");
  check(d.getElementById("deleteRun").hidden===false,"Stopped details have a delete control");
  check(!d.querySelector("#resultRows img")&&d.querySelector("#resultRows strong"),"Decision summary escapes HTML and preserves emphasis");
  check(!d.querySelector("#analysisSettings,#analysisDate,#consent,[name=analysts],#rescheduleAck,[name=scheduleDay]"),"Manual date, analyst and consent controls are removed");
  check(!d.getElementById("priceRangeControls").open,"Optional price controls start collapsed");
  d.getElementById("priceRangeControls").open=true;
  check(d.getElementById("priceRangeControls").open,"Price controls can expand");
  check([...d.getElementById("rangeInterval").options].map(x=>x.value).join(",")==="15,30,60","Only intraday candle sizes are offered");
  check(!d.querySelector("#scheduleFrequency option[value=weekly],#rescheduleFrequency option[value=weekly]"),"No weekday/weekend selectors remain");
  a.showRangeSettings({enabled:true,interval_minutes:15});
  a.rangeUI.jobId=base.id;a.rangeUI.lastFetch=Date.now();a.rangeUI.data={records:{NVDA:{status:"ready",low:95,high:108,currency:"USD",interval_minutes:15,method:"remaining_session_v1",session_close:"2026-10-20T20:00:00Z",candles:[{start:"2026-10-20T16:00:00Z",end:"2026-10-20T16:15:00Z",low:95,high:108}],reference_at:"2026-10-20T16:00:00Z",reference_price:100,sample_windows:30,note:"Fixture estimate"}}};a.rangeUI.revision++;a.renderResults(a.getDisplay());
  check(d.querySelectorAll(".results-table > thead > tr > th").length===8,"Only low/high columns are added when enabled");
  check(d.querySelector(".range-low").textContent.includes("95")&&d.querySelector(".range-high").textContent.includes("108"),"Estimated low and high are displayed");
  check(d.getElementById("resultRows").textContent.includes("Underweight")&&d.getElementById("resultRows").textContent.includes("SELL"),"Range feature preserves final recommendation");
  a.showRangeSettings({enabled:false,interval_minutes:15});
  check(d.querySelectorAll(".results-table > thead > tr > th").length===6&&!d.querySelector(".range-low"),"Disabling removes range columns and values");
  holds.set("POST /api/ranges/settings",Promise.resolve(answer({error:"Fixture save failure"},503)));
  d.getElementById("rangesEnabled").checked=true;d.getElementById("rangesEnabled").dispatchEvent(new w.Event("change"));await tick();await tick();
  check(!d.getElementById("rangesEnabled").checked&&d.getElementById("rangeSettingsStatus").textContent.includes("Not saved"),"Failed toggle save restores the actual saved state");
  holds.delete("POST /api/ranges/settings");
  d.getElementById("closeRun").click();await a.refreshStatus();
  check(d.querySelector(".results-card").hidden,"Polling does not reopen closed stopped results");
  a.switchTab("bulk");a.selected.set("NVDA",asset("NVDA"));a.selected.set("AAPL",asset("AAPL"));
  d.getElementById("scheduleBulk").checked=true;d.getElementById("scheduleBulk").dispatchEvent(new w.Event("change"));
  check(!d.getElementById("scheduleBanner").hidden&&d.getElementById("runBulk").textContent.includes("Schedule"),"Schedule mode is explicit");
  d.getElementById("scheduleDate").value="2030-10-21";
  await a.startRun(true);
  const bulkSaved=calls.find(c=>c.method==="POST"&&c.url==="/api/schedules").body;
  check(bulkSaved.analysts.length===4&&!bulkSaved.date,"Schedules have all analysts and no stale analysis date");
  check(bulkSaved.selection.symbols.length===2,"Bulk schedule uses exact current selection");
  check(!calls.some(c=>c.url==="/api/run"||c.url==="/api/bulk"),"Saving a schedule does not run paid analyses immediately");
  check(!d.querySelector("#panel-schedules form"),"Schedules remains a management list");
  a.switchTab("individual");d.getElementById("ticker").value="NVDA";
  d.getElementById("scheduleIndividual").checked=true;d.getElementById("scheduleIndividual").dispatchEvent(new w.Event("change"));
  await a.startRun(false);
  check(calls.filter(c=>c.method==="POST"&&c.url==="/api/schedules").at(-1).body.mode==="individual","Individual scheduling also works");
  const original=copy(schedules.schedules[0]);a.editSchedule(original);
  d.getElementById("rescheduleTime").value="10:45";
  d.getElementById("rescheduleForm").dispatchEvent(new w.Event("submit",{cancelable:true}));await tick();await tick();
  const changed=calls.filter(c=>c.method==="POST"&&c.url==="/api/schedules").at(-1).body;
  check(changed.time==="10:45"&&JSON.stringify(changed.selection)===JSON.stringify(original.selection),"Rescheduling changes timing but preserves assets");
  check(JSON.stringify(changed.analysts)===JSON.stringify(original.analysts),"Rescheduling preserves analysts");
  d.querySelector('[data-schedule="stop"][data-id="'+original.id+'"]').click();await tick();await tick();
  check(!schedules.schedules.find(s=>s.id===original.id).enabled,"Stop schedule disables future runs");
  d.querySelector('[data-schedule="delete"][data-id="'+original.id+'"]').click();await tick();await tick();
  check(!schedules.schedules.some(s=>s.id===original.id),"Delete schedule removes its list entry");
  check(jobs.has(base.id),"Deleting schedule preserves saved run history");
  schedules.occurrences=[{id:"waiting",schedule_id:schedules.schedules[0].id,name:"Waiting",status:"queued",scheduled_at:"2030-10-21T04:00:00Z",run_date:"2030-10-21",message:"Waiting",expires_at:new Date(Date.now()+3600000).toISOString()}];
  a.renderSchedules(schedules);d.querySelector("[data-cancel]").click();await tick();
  check(schedules.occurrences[0].status==="cancelled"&&schedules.schedules[0].enabled,"Cancel waiting occurrence leaves future schedule enabled");
  const running={...copy(base),id:"20261020_170000_1234abcd",state:"running",running:true,created_at:new Date().toISOString(),expires_at:new Date(Date.now()+24*3600000).toISOString()};
  jobs.set(running.id,running);status=copy(running);a.switchTab("bulk");await a.refreshStatus();
  check(!d.querySelector(".results-card").hidden&&!d.getElementById("stopAnalysis").hidden,"Only active run is shown in live panel");
  await a.runAction(running.id,"stop");
  check(calls.some(c=>c.url==="/api/jobs/"+running.id+"/stop"),"Stop targets the selected job ID");
  check(d.querySelector(".results-card").hidden,"Stopped run leaves live panel");
  await a.openSavedJob(running.id);await a.runAction(running.id,"delete");
  check(!jobs.has(running.id)&&d.querySelector(".results-card").hidden,"Delete removes stopped history and closes its details");
  check(jobs.has(base.id),"Deleting one job does not remove another job");
  await a.runAction(base.id,"delete");await a.refreshHistory();
  check(d.querySelectorAll("[data-saved-job]").length===0,"Deleted runs disappear from the history list");
  const style=d.createElement("style");style.textContent=fs.readFileSync(path.join(root,"local_ui.css"),"utf8");d.head.append(style);
  check(style.sheet.cssRules.length>0,"Responsive CSS parses");
  check(style.textContent.includes("@media(max-width:600px)"),"Small-screen layout rules are present");
  holds.set("GET /api/status",Promise.resolve(answer({state:"idle",running:false,results:[],log:[]})));
  await a.refreshStatus();
  check(d.getElementById("notice").textContent.includes("Restart")&&d.getElementById("runIndividual").disabled,"Old server code produces an explicit restart instruction and blocks new work");
  const beforeCalls=calls.length;await a.startRun(false);
  check(calls.length===beforeCalls,"Server-version mismatch cannot accidentally submit a paid run");
  dom.window.close();
  console.log(checks+" offline UI checks passed. No market/AI calls.");
}
main().catch(e=>{console.error(e);process.exitCode=1;});
