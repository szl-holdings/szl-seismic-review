/* SZL Seismic Review UI. No simulated records and no persisted operator token. */
const state = {meta:null, summary:null, offset:0, limit:20, catalogue:"", selected:null, reviewer:""};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const cleanNumber = (value, digits=2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const token = () => $("operator-token").value.trim();
const headers = () => token() ? {Authorization:`Bearer ${token()}`} : {};

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
  $("storage-state").textContent = meta.storage_state === "READ_ONLY" ? "Verified source · read only" : "Operator writes enabled";
  $("write-state").textContent = meta.review_write_enabled ? "Operator mode" : "Read only";
  $("train-count").textContent = meta.model.n_train;
  $("count-auc").textContent = cleanNumber(meta.model.evaluation.roc_auc_oof,3);
  $("brier-value").textContent = cleanNumber(meta.model.evaluation.brier_oof,3);
  $("paper-link").href = meta.paper_url;
  $("source-link").href = meta.source_url;
  $("import-button").disabled = !meta.import_enabled;
  if (!meta.import_enabled) $("import-feedback").textContent = "Import and fresh review are disabled on this read-only deployment.";
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
  const params = new URLSearchParams({limit:String(state.limit), offset:String(state.offset)});
  if (state.catalogue) params.set("catalogue",state.catalogue);
  const data = await api(`/api/detections?${params}`);
  $("detection-rows").innerHTML = data.items.length ? data.items.map(recordRow).join("") : `<tr><td colspan="7" class="empty">No records in this view.</td></tr>`;
  $("page-status").textContent = data.total ? `Showing ${state.offset+1}–${Math.min(state.offset+data.items.length,data.total)} of ${data.total} records` : "No records";
  $("prev-button").disabled = state.offset === 0;
  $("next-button").disabled = state.offset + state.limit >= data.total;
  document.querySelectorAll("[data-record]").forEach(button => button.addEventListener("click",() => openDetail(button.dataset.record)));
}

async function loadFilters() {
  const data = await api("/api/detections?limit=200&offset=0");
  const catalogues = [...new Set(data.items.filter(x => x.origin === "PUBLISHED_PANEL").map(x => x.catalogue))].sort();
  $("catalogue-filter").innerHTML = `<option value="">All catalogues</option>` + catalogues.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
}

function field(name,value){return `<div class="detail-field"><small>${esc(name.replaceAll("_"," "))}</small><strong>${esc(typeof value === "number" ? cleanNumber(value,3) : value)}</strong></div>`;}
function publishedDetail(data) {
  const votes = data.published_verdicts.map(v => `<li><strong>${esc(v.reviewer)}</strong>${badge(v.verdict)}</li>`).join("");
  const meta = Object.entries(data.metadata).map(([name,value]) => field(name,value)).join("");
  const features = Object.entries(data.features).map(([name,value]) => field(name,value)).join("");
  return `<h2>Published case ${esc(data.event_id)}</h2><p>${badge(data.published_consensus)} &nbsp; Four source-panel verdicts · ${esc(data.catalogue)}</p><p class="warning">The pinned archive does not include continuous waveforms. This record can be inspected, but it cannot receive a new blind verdict here. The displayed model score is a full-panel fit; the evaluation AUC uses out-of-fold predictions.</p><h3>Published votes</h3><ul class="review-list">${votes}</ul><h3>Catalogue metadata</h3><div class="detail-grid">${meta}</div><h3>Seven association features</h3><div class="detail-grid">${features}</div><h3>Model readout</h3><p>${data.confirmability_score == null ? esc(data.score_status) : `${cleanNumber(data.confirmability_score,3)} · ${esc(data.score_status)} · Japan panel only`}</p><a href="${esc(data.source_url)}" target="_blank" rel="noopener noreferrer">View pinned source ↗</a>`;
}

function plotTrace(samples){
  const width=480,height=72,low=Math.min(...samples),high=Math.max(...samples),span=high-low||1;
  const points=samples.map((value,index)=>`${(index/(samples.length-1)*width).toFixed(1)},${(height-6-(value-low)/span*(height-12)).toFixed(1)}`).join(" ");
  return `<svg class="trace" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="Waveform trace"><polyline points="${points}" fill="none" stroke="#3d70dd" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>`;
}

function importedDetail(data){
  let body=`<h2>Blind detection</h2><p>Imported case · event metadata hidden during review.</p>`;
  if(!data.waveform){return body+`<p class="warning">Waveforms unavailable. A reviewer cannot judge this detection until an authorized operator attaches five real station traces with a source reference through the API.</p><p>Reference: <code>${esc(data.id)}</code></p>`;}
  body+=`<p class="warning">Waveform set · SHA-256 ${esc(data.waveform_sha256)}. ${data.blind ? "The source reference and event metadata stay hidden until this reviewer locks a verdict." : `Source reference: ${esc(data.waveform.source_uri)}.`} Trace authenticity is not independently verified by this service.</p>`;
  data.waveform.stations.forEach(station=>{body+=`<h3>Station ${esc(station.index)}</h3>${plotTrace(station.samples)}<p>P offset: ${esc(station.p_offset_s ?? "unavailable")} s · S offset: ${esc(station.s_offset_s ?? "unavailable")} s</p>`;});
  if(data.blind && state.meta.review_write_enabled){body+=`<h3>Lock a blind verdict</h3><label for="reviewer-id">Reviewer ID</label><input id="reviewer-id" value="${esc(state.reviewer)}" placeholder="Your reviewer ID" autocomplete="off"><label for="review-note">Evidence note</label><textarea id="review-note" placeholder="What in the traces supports your judgement?"></textarea><div class="review-actions"><button data-verdict="confirmed">Confirmed</button><button data-verdict="unresolved">Unresolved</button><button data-verdict="rejected">Rejected</button></div><p id="review-feedback" role="status"></p>`;}
  if(!data.blind){body+=`<h3>Locked reviews</h3><ul class="review-list">${(data.reviews||[]).map(v=>`<li><strong>${esc(v.reviewer_id)}</strong>${badge(v.verdict)}</li>`).join("")}</ul><h3>Revealed features</h3><div class="detail-grid">${Object.entries(data.features).map(([k,v])=>field(k,v)).join("")}</div><p>Research score: ${data.confirmability_score == null ? esc(data.score_status) : cleanNumber(data.confirmability_score,3)}</p>`;}
  return body;
}

async function openDetail(id,reviewer=""){
  state.selected=id;
  $("drawer-backdrop").hidden=false;
  $("detail-drawer").classList.add("open");
  $("detail-drawer").setAttribute("aria-hidden","false");
  $("detail-content").innerHTML=`<p>Loading record…</p>`;
  try{
    const query=reviewer ? `?reviewer_id=${encodeURIComponent(reviewer)}` : "";
    const data=await api(`/api/detections/${encodeURIComponent(id)}${query}`,{headers:headers()});
    $("detail-content").innerHTML=data.origin==="PUBLISHED_PANEL" ? publishedDetail(data) : importedDetail(data);
    document.querySelectorAll("[data-verdict]").forEach(button=>button.addEventListener("click",()=>submitReview(button.dataset.verdict)));
  }catch(error){$("detail-content").textContent=`Could not load record: ${error.message}`;}
}

function closeDetail(){state.selected=null;$("detail-drawer").classList.remove("open");$("detail-drawer").setAttribute("aria-hidden","true");$("drawer-backdrop").hidden=true;}

async function submitReview(verdict){
  const reviewer=$("reviewer-id").value.trim(),note=$("review-note").value.trim();
  if(!reviewer){$("review-feedback").textContent="Enter a reviewer ID.";return;}
  state.reviewer=reviewer;
  try{
    await api("/api/reviews",{method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({detection_id:state.selected,reviewer_id:reviewer,verdict,note})});
    await openDetail(state.selected,reviewer);await loadCatalogue();await loadSummary();
  }catch(error){$("review-feedback").textContent=error.message;}
}

async function importCatalogue(){
  const file=$("import-file").files[0],catalogue=$("import-catalogue").value.trim(),region=$("import-region").value.trim(),feedback=$("import-feedback");
  if(!file||!catalogue||!region){feedback.textContent="Choose a CSV and enter catalogue and region.";feedback.classList.add("error");return;}
  try{
    const query=new URLSearchParams({catalogue,region});
    const result=await api(`/api/catalogues/import?${query}`,{method:"POST",headers:{...headers(),"Content-Type":"text/csv; charset=utf-8"},body:await file.text()});
    feedback.textContent=`Imported ${result.imported} records. Source SHA-256: ${result.source_sha256}. Waveform attachment remains required.`;
    feedback.classList.remove("error");state.offset=0;await loadSummary();await loadCatalogue();
  }catch(error){feedback.textContent=error.message;feedback.classList.add("error");}
}

async function start(){
  $("close-drawer").addEventListener("click",closeDetail);$("drawer-backdrop").addEventListener("click",closeDetail);
  document.addEventListener("keydown",event=>{if(event.key==="Escape")closeDetail();});
  $("refresh-button").addEventListener("click",()=>Promise.all([loadSummary(),loadCatalogue()]));
  $("catalogue-filter").addEventListener("change",event=>{state.catalogue=event.target.value;state.offset=0;loadCatalogue();});
  $("prev-button").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-state.limit);loadCatalogue();});
  $("next-button").addEventListener("click",()=>{state.offset+=state.limit;loadCatalogue();});
  $("import-button").addEventListener("click",importCatalogue);
  try{await Promise.all([loadMeta(),loadSummary(),loadFilters()]);await loadCatalogue();}
  catch(error){$("detection-rows").innerHTML=`<tr><td colspan="7" class="empty">Source unavailable: ${esc(error.message)}</td></tr>`;$("storage-state").textContent="Source unavailable";}
}
start();
