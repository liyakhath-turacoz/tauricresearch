"use strict";
let rangeSettingsLoading=false, rangeEditVersion=0;
let savedRangeSettings={enabled:false,interval_minutes:15};
const rangeUI=globalThis.rangeUI={
  enabled:false,revision:0,jobId:null,data:null,lastFetch:0,loadingId:null,
  observe(job){
    if(!this.enabled||!job?.id)return;
    if(this.jobId!==job.id){this.jobId=job.id;this.data=null;this.lastFetch=0;this.revision++;}
    if(this.loadingId===job.id||Date.now()-this.lastFetch<1800)return;
    const id=job.id;this.loadingId=id;this.lastFetch=Date.now();
    api(`/api/jobs/${encodeURIComponent(id)}/ranges`).then(data=>{
      if(this.jobId!==id)return;
      if(JSON.stringify(this.data)!==JSON.stringify(data)){
        this.data=data;this.revision++;if(displayJob?.id===id)renderResults(displayJob);
      }
    }).catch(e=>{$("rangeSettingsStatus").textContent=e.message;}).finally(()=>{if(this.loadingId===id)this.loadingId=null;});
  },
  cells(item){
    const record=this.data?.records?.[item.symbol];
    if(!record||record.status!=="ready"){
      const note=record?.note||(this.data?.status==="not_recorded"?"Not recorded for this run":"Range unavailable / not generated");
      return `<td colspan="2" class="muted">${esc(note)}</td>`;
    }
    const money=value=>`${new Intl.NumberFormat(undefined,{maximumFractionDigits:value<1?6:2}).format(value)} ${esc(record.currency)}`;
    const current=record.method==="remaining_session_v1";
    const detail=current?`<small class="block">${record.interval_minutes}-minute candles; through ${esc(formattedTime(record.session_close))} IST</small>`:`<small class="block">Original estimate: ${record.horizon_hours} market hour(s), not a remaining-session estimate.</small>`;
    const candles=current?`<table class="candle-ranges"><thead><tr><th>Candle (IST)</th><th>Low</th><th>High</th></tr></thead><tbody>${record.candles.map(c=>`<tr><td>${esc(formattedTime(c.start))} - ${esc(formattedTime(c.end))}</td><td>${money(c.low)}</td><td>${money(c.high)}</td></tr>`).join("")}</tbody></table>`:"";
    return `<td class="range-low">${money(record.low)}${detail}</td><td class="range-high">${money(record.high)}<details class="decision-detail"><summary>${current?"Remaining candles & basis":"Original estimate basis"}</summary>${candles}<p>${esc(record.note)} Reference: ${money(record.reference_price)} at ${esc(formattedTime(record.reference_at))} IST; ${record.sample_windows} historical sessions/windows. This is a saved snapshot, not a live update.</p></details></td>`;
  }
};
function showRangeSettings(config){
  const changed=JSON.stringify(config)!==JSON.stringify(savedRangeSettings);
  savedRangeSettings={...config};rangeUI.enabled=config.enabled;if(changed)rangeUI.revision++;
  $("rangeSummary").textContent=config.enabled?`On - ${config.interval_minutes}-minute candles`:"Off";
  $("rangesEnabled").checked=config.enabled;$("rangeInterval").value=config.interval_minutes;
  $("rangeSettingsStatus").textContent=config.enabled?"On for individual, bulk and scheduled runs at execution time. Estimates cover the remaining current session. Older results are not backfilled.":"Off. Price-range columns are hidden.";
  renderResults(displayJob);
}
async function saveRangeSettings(){
  const version=++rangeEditVersion;
  const payload={enabled:$("rangesEnabled").checked,interval_minutes:Number($("rangeInterval").value)};
  $("rangesEnabled").disabled=true;$("rangeInterval").disabled=true;
  try{const config=await api("/api/ranges/settings",{method:"POST",body:JSON.stringify(payload)});if(version===rangeEditVersion)showRangeSettings(config);}
  catch(e){$("rangesEnabled").checked=savedRangeSettings.enabled;$("rangeInterval").value=savedRangeSettings.interval_minutes;$("rangeSettingsStatus").textContent="Not saved: "+e.message;}
  finally{$("rangesEnabled").disabled=false;$("rangeInterval").disabled=false;}
}
async function refreshRangeSettings(){
  if(rangeSettingsLoading||$("rangesEnabled").disabled)return;
  rangeSettingsLoading=true;const version=rangeEditVersion;
  try{const config=await api("/api/ranges/settings");if(version===rangeEditVersion)showRangeSettings(config);}
  catch(e){$("rangeSettingsStatus").textContent=e.message;}
  finally{rangeSettingsLoading=false;}
}
$("rangesEnabled").addEventListener("change",saveRangeSettings);
$("rangeInterval").addEventListener("change",saveRangeSettings);
refreshRangeSettings();setInterval(refreshRangeSettings,10000);
