/* SZL Seismic Review UI. Credentials live only in this page's input elements. */
const state = {meta:null, summary:null, offset:0, limit:20, catalogue:"", selected:null, selectedOrigin:null, records:new Map()};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const cleanNumber = (value, digits=2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const operatorToken = () => $("operator-token").value.trim();
const reviewerToken = () => $("reviewer-token").value.trim();
const authHeaders = value => value ? {Authorization:`Bearer ${value}`} : {};
const detailHeaders = () => authHeaders(reviewerToken() || operatorToken());

async function api(path, options={}) {
  const response = await fetch(path, {cache:"no-store", ...options});
  let payload;
  try {payload = await response.json();} catch {throw new Error(`Server returned HTTP ${response.status}`);}
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`);
  return payload;
}

function label(value) {return ({confirmed:"Confirmed", rejected:"Rejected", unresolved:"Unresolved", real:"Confirmed", false:"Rejected", uncertain:"Uncertain"})[value] || value || "Awaiting review";}
function badge(value) {return `<span class="badge ${["confirmed","rejected","unresolved"].includes(value) ? value : "neutral"}">${esc(label(value))}</span>`;}

async function loadMeta() {
  const meta = await api("/api/meta"); state.meta = meta;
  $("source-state").textContent = `Zenodo ${meta.source_version} · checksum verified`;
  $("storage-state").textContent = meta.storage_state === "READ_ONLY" ? "Pinned source · read only" : "Local write storage · durability unverified";
  $("write-state").textContent = meta.import_enabled && meta.review_write_enabled ? "Custody + review enabled" : meta.import_enabled ? "Custody enabled" : meta.review_write_enabled ? "Review enabled" : "Read only";
  $("train-count").textContent = meta.model.n_train;
  $("count-auc").textContent = cleanNumber(meta.model.evaluation.roc_auc_oof,3);
  $("brier-value").textContent = cleanNumber(meta.model.evaluation.brier_oof,3);
  $("paper-link").href = meta.paper_url;
  $("source-link").href = meta.source_url;
  $("import-button").disabled = !meta.import_enabled;
  if (!meta.import_enabled) $("import-feedback").textContent = "Catalogue import and waveform attachment are disabled on this deployment.";
}

async function loadSummary() {
  const data = await api("/api/summary"); state.summary = data;
  $("count-panel").textContent = data.published_count;
  $("count-confirmed").textContent = data.confirmed;
  $("count-unresolved").textContent = data.unresolved;
}

function recordRow(item) {
  const score = item.confirmability_score == null ? `<span class="badge neutral">${esc(item.score_status.replaceAll("_"," "))}</span>` : `<span class="badge score">${cleanNumber(item.confirmability_score,3)} · panel fit</span>`;
  return `<tr><td>${esc(item.event_id || "Blind case")}<span class="sub">${esc(item.id)}</span></td><td>${item.origin === "PUBLISHED_PANEL" ? "Published panel" : "Operator import"}</td><td>${esc(item.region)}<span class="sub">${esc(item.catalogue)}</span></td><td>${badge(item.published_consensus)}</td><td>${score}</td><td>${item.waveform_ready ? badge("Ready") : `<span class="badge neutral">Not attached</span>`}</td><td><button class="row-button" data-record="${esc(item.id)}">Inspect →</button></td></tr>`;
}

async function loadCatalogue() {
  const imported = state.catalogue === "__imports__";
  const params = new URLSearchParams({limit:String(state.limit), offset:String(state.offset)});
  if (imported) params.set("origin","imported");
  if (state.catalogue && !imported) params.set("catalogue",state.catalogue);
  const data = await api(`/api/detections?${params}`);
  const visibleTotal = data.total;
  state.records = new Map(data.items.map(item => [item.id,item]));
  $("detection-rows").innerHTML = data.items.length ? data.items.map(recordRow).join("") : `<tr><td colspan="7" class="empty">No records in this view.</td></tr>`;
  $("page-status").textContent = visibleTotal ? `Showing ${state.offset+1}–${Math.min(state.offset+data.items.length,visibleTotal)} of ${visibleTotal} records` : "No records";
  $("prev-button").disabled = state.offset === 0;
  $("next-button").disabled = state.offset + state.limit >= visibleTotal;
  document.querySelectorAll("[data-record]").forEach(button => button.addEventListener("click",() => openDetail(button.dataset.record,state.records.get(button.dataset.record)?.origin)));
}

async function loadFilters() {
  const data = await api("/api/detections?origin=published&limit=200&offset=0");
  const catalogues = [...new Set(data.items.filter(x => x.origin === "PUBLISHED_PANEL").map(x => x.catalogue))].sort();
  $("catalogue-filter").innerHTML = `<option value="">All catalogues</option><option value="__imports__">Imported cases</option>` + catalogues.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
}

function field(name,value){return `<div class="detail-field"><small>${esc(name.replaceAll("_"," "))}</small><strong>${esc(typeof value === "number" ? cleanNumber(value,3) : value)}</strong></div>`;}
function publishedDetail(data) {
  const votes = data.published_verdicts.map(v => `<li><strong>${esc(v.reviewer)}</strong>${badge(v.verdict)}</li>`).join("");
  const meta = Object.entries(data.metadata).map(([name,value]) => field(name,value)).join("");
  const features = Object.entries(data.features).map(([name,value]) => field(name,value)).join("");
  return `<h2>Published case ${esc(data.event_id)}</h2><p>${badge(data.published_consensus)} &nbsp; Four source-panel verdicts · ${esc(data.catalogue)}</p><p class="warning">The pinned archive does not include continuous waveforms. This record can be inspected, but it cannot receive a new blind verdict here. The displayed model score is a full-panel fit; the evaluation AUC uses out-of-fold predictions.</p><h3>Published votes</h3><ul class="review-list">${votes}</ul><h3>Catalogue metadata</h3><div class="detail-grid">${meta}</div><h3>Seven association features</h3><div class="detail-grid">${features}</div><h3>Model readout</h3><p>${data.confirmability_score == null ? esc(data.score_status) : `${cleanNumber(data.confirmability_score,3)} · ${esc(data.score_status)} · Japan panel only`}</p><a href="${esc(data.source_url)}" target="_blank" rel="noopener noreferrer">View pinned source ↗</a>`;
}

function plotTrace(samples){
  if(samples.length>1200){const stride=Math.ceil(samples.length/1200);samples=samples.filter((_,index)=>index%stride===0||index===samples.length-1);}
  const width=480,height=72,low=Math.min(...samples),high=Math.max(...samples),span=high-low||1;
  const points=samples.map((value,index)=>`${(index/(samples.length-1)*width).toFixed(1)},${(height-6-(value-low)/span*(height-12)).toFixed(1)}`).join(" ");
  return `<svg class="trace" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="Waveform trace"><polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>`;
}

function importedDetail(data){
  let body=`<h2>${data.blind ? "Blind detection" : `Reviewed detection ${esc(data.event_id)}`}</h2><p>Imported case · ${data.blind ? "event metadata hidden during review" : "metadata revealed to this reviewer after a locked verdict"}.</p><p class="case-reference">Reference: <code>${esc(data.id)}</code></p>`;
  if(!data.waveform){
    body+=`<p class="warning">No station traces are attached. Blind review remains closed until the source custodian attaches five traces with a sample rate and source reference.</p>`;
    if(state.meta.import_enabled){body+=`<div class="attachment-panel"><h3>Attach waveform evidence</h3><p>Choose a JSON file containing <code>source_uri</code>, <code>sample_rate_hz</code>, and exactly five <code>stations</code> with numeric <code>samples</code>. First attachment is locked. Source authenticity remains unverified by this service.</p><label for="waveform-file">Waveform JSON file</label><input class="input" id="waveform-file" type="file" accept=".json,application/json"><button id="attach-waveform" class="btn btn-secondary btn-sm" type="button">Attach five traces</button><p id="waveform-feedback" class="form-feedback" role="status"></p></div>`;}
    else body+=`<p class="availability">Attachment is disabled on this read-only deployment.</p>`;
    return body;
  }
  body+=`<p class="warning">Five-station waveform attached · SHA-256 <code>${esc(data.waveform_sha256)}</code>. ${data.blind ? "The source reference and event metadata stay hidden until this reviewer locks a verdict." : `Source reference: ${esc(data.waveform.source_uri)}.`} Trace authenticity is unverified by this service.</p><p class="trace-caption">Sample rate: ${cleanNumber(data.waveform.sample_rate_hz,2)} Hz · ${data.waveform.stations.length} stations</p>`;
  data.waveform.stations.forEach(station=>{body+=`<h3>Station ${esc(station.index)}</h3>${plotTrace(station.samples)}<p>P offset: ${esc(station.p_offset_s ?? "unavailable")} s · S offset: ${esc(station.s_offset_s ?? "unavailable")} s</p>`;});
  if(data.blind && state.meta.review_write_enabled && reviewerToken()){body+=`<h3>Lock a blind verdict</h3><p>Reviewer identity is derived from the token above. A verdict cannot be changed after submission.</p><label for="review-note">Evidence note</label><textarea id="review-note" maxlength="2000" placeholder="What in the traces supports your judgement?"></textarea><div class="review-actions"><button data-verdict="confirmed">Confirmed</button><button data-verdict="unresolved">Unresolved</button><button data-verdict="rejected">Rejected</button></div><p id="review-feedback" class="form-feedback" role="status"></p>`;}
  else if(data.blind){body+=`<p class="availability">${state.meta.review_write_enabled ? "Enter a reviewer token above and open the case to submit a blind verdict." : "Reviewer writes are disabled on this deployment."}</p>`;}
  if(!data.blind){body+=`<h3>Locked reviews</h3><ul class="review-list">${(data.reviews||[]).map(v=>`<li><strong>${esc(v.reviewer_id)}</strong>${badge(v.verdict)}</li>`).join("")}</ul><h3>Revealed features</h3><div class="detail-grid">${Object.entries(data.features).map(([k,v])=>field(k,v)).join("")}</div><p>Research score: ${data.confirmability_score == null ? esc(data.score_status) : `${cleanNumber(data.confirmability_score,3)} · ${esc(data.score_status)}`}. Imported-case model readouts are unvalidated.</p>`;}
  return body;
}

async function openDetail(id,origin=state.selectedOrigin,notice=""){
  state.selected=id;
  state.selectedOrigin=origin;
  $("drawer-backdrop").hidden=false;
  $("detail-drawer").classList.add("open");
  $("detail-drawer").setAttribute("aria-hidden","false");
  $("reviewer-access").hidden=origin!=="OPERATOR_IMPORT";
  $("detail-content").innerHTML=`<p>Loading record…</p>`;
  if(origin==="OPERATOR_IMPORT" && !reviewerToken() && !operatorToken()){
    $("detail-content").innerHTML=`<h2>Blind detection</h2><p class="warning">This imported case requires a reviewer token or a source custodian token. Enter a reviewer token above to inspect the blind traces, or add a source custodian token in Operator tools to attach missing waveforms.</p><p>Published records remain available without credentials.</p>`;
    return;
  }
  try{
    const data=await api(`/api/detections/${encodeURIComponent(id)}`,{headers:detailHeaders()});
    state.selectedOrigin=data.origin;
    $("reviewer-access").hidden=data.origin!=="OPERATOR_IMPORT";
    $("detail-content").innerHTML=data.origin==="PUBLISHED_PANEL" ? publishedDetail(data) : importedDetail(data);
    if(notice){const message=document.createElement("p");message.className="success-note";message.textContent=notice;$("detail-content").prepend(message);}
    document.querySelectorAll("[data-verdict]").forEach(button=>button.addEventListener("click",()=>submitReview(button.dataset.verdict)));
    $("attach-waveform")?.addEventListener("click",attachWaveform);
  }catch(error){$("detail-content").textContent=`Could not load record: ${error.message}`;}
}

function closeDetail(){state.selected=null;state.selectedOrigin=null;$("detail-drawer").classList.remove("open");$("detail-drawer").setAttribute("aria-hidden","true");$("drawer-backdrop").hidden=true;}

async function submitReview(verdict){
  const note=$("review-note").value.trim(),feedback=$("review-feedback");
  if(!reviewerToken()){feedback.textContent="Enter a reviewer token and reopen this case.";feedback.classList.add("error");return;}
  const buttons=[...document.querySelectorAll("[data-verdict]")];buttons.forEach(button=>button.disabled=true);
  feedback.textContent="Locking verdict…";feedback.classList.remove("error");
  try{
    await api("/api/reviews",{method:"POST",headers:{...authHeaders(reviewerToken()),"Content-Type":"application/json"},body:JSON.stringify({detection_id:state.selected,verdict,note})});
    await openDetail(state.selected,"OPERATOR_IMPORT","Verdict locked for the reviewer identity bound to this token.");await Promise.all([loadCatalogue(),loadSummary()]);
  }catch(error){feedback.textContent=error.message;feedback.classList.add("error");buttons.forEach(button=>button.disabled=false);}
}

async function attachWaveform(){
  const feedback=$("waveform-feedback"),file=$("waveform-file").files[0];
  feedback.classList.remove("error");
  if(!operatorToken()){feedback.textContent="Enter the source custodian token in Operator tools, then reopen this record.";feedback.classList.add("error");return;}
  if(!file){feedback.textContent="Choose a waveform JSON file.";feedback.classList.add("error");return;}
  if(file.size>1_000_000){feedback.textContent="Waveform JSON exceeds the 1 MB request limit.";feedback.classList.add("error");return;}
  let body;
  try{
    const parsed=JSON.parse(await file.text());
    if(!parsed || typeof parsed!=="object" || Array.isArray(parsed) || typeof parsed.source_uri!=="string" || !parsed.source_uri.trim() || !Number.isFinite(Number(parsed.sample_rate_hz)) || Number(parsed.sample_rate_hz)<=0 || !Array.isArray(parsed.stations) || parsed.stations.length!==5 || parsed.stations.some(station=>!station || !Array.isArray(station.samples) || station.samples.length<2 || station.samples.length>10000 || station.samples.some(value=>!Number.isFinite(Number(value)))))throw new Error("JSON must include a source URI, a positive sample rate, and exactly five numeric station traces.");
    body=JSON.stringify(parsed);
  }catch(error){feedback.textContent=error instanceof SyntaxError ? "The selected file is not valid JSON." : error.message;feedback.classList.add("error");return;}
  const button=$("attach-waveform");button.disabled=true;
  feedback.textContent="Attaching and locking waveform evidence…";
  try{
    const result=await api(`/api/detections/${encodeURIComponent(state.selected)}/waveform`,{method:"POST",headers:{...authHeaders(operatorToken()),"Content-Type":"application/json"},body});
    await openDetail(state.selected,"OPERATOR_IMPORT",`Five traces attached and locked. SHA-256: ${result.waveform_sha256}. Source authenticity is unverified.`);
    await Promise.all([loadCatalogue(),loadSummary()]);
  }catch(error){feedback.textContent=error.message;feedback.classList.add("error");button.disabled=false;}
}

async function importCatalogue(){
  const file=$("import-file").files[0],catalogue=$("import-catalogue").value.trim(),region=$("import-region").value.trim(),feedback=$("import-feedback");
  if(!operatorToken()){feedback.textContent="Enter a source custodian token.";feedback.classList.add("error");return;}
  if(!file||!catalogue||!region){feedback.textContent="Choose a CSV and enter catalogue and region.";feedback.classList.add("error");return;}
  const button=$("import-button");button.disabled=true;feedback.textContent="Importing catalogue…";feedback.classList.remove("error");
  try{
    const query=new URLSearchParams({catalogue,region});
    const result=await api(`/api/catalogues/import?${query}`,{method:"POST",headers:{...authHeaders(operatorToken()),"Content-Type":"text/csv; charset=utf-8"},body:await file.text()});
    feedback.textContent=`Imported ${result.imported} records. Source SHA-256: ${result.source_sha256}. Waveform attachment remains required.`;
    feedback.classList.remove("error");state.offset=0;state.catalogue="__imports__";$("catalogue-filter").value="__imports__";await loadSummary();await loadCatalogue();
  }catch(error){feedback.textContent=error.message;feedback.classList.add("error");}finally{button.disabled=false;}
}

async function start(){
  $("close-drawer").addEventListener("click",closeDetail);$("drawer-backdrop").addEventListener("click",closeDetail);
  document.addEventListener("keydown",event=>{if(event.key==="Escape")closeDetail();});
  $("open-reviewer-case").addEventListener("click",()=>{if(state.selected)openDetail(state.selected,"OPERATOR_IMPORT");});
  $("reviewer-token").addEventListener("keydown",event=>{if(event.key==="Enter" && state.selected)openDetail(state.selected,"OPERATOR_IMPORT");});
  $("refresh-button").addEventListener("click",()=>Promise.all([loadSummary(),loadCatalogue()]));
  $("catalogue-filter").addEventListener("change",event=>{state.catalogue=event.target.value;state.offset=0;loadCatalogue();});
  $("prev-button").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-state.limit);loadCatalogue();});
  $("next-button").addEventListener("click",()=>{state.offset+=state.limit;loadCatalogue();});
  $("import-button").addEventListener("click",importCatalogue);
  try{await Promise.all([loadMeta(),loadSummary(),loadFilters()]);await loadCatalogue();}
  catch(error){$("detection-rows").innerHTML=`<tr><td colspan="7" class="empty">Source unavailable: ${esc(error.message)}</td></tr>`;$("storage-state").textContent="Source unavailable";}
}
start();
