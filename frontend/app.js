/* SZL Seismic Review UI. Credentials live only in this page's input elements. */
const state = {meta:null, summary:null, offset:0, limit:20, catalogue:"", query:"", consensus:"", catalogueRequestSeq:0, catalogueLoaded:false, exportBusy:false, searchTimer:null, evidenceRequestSeq:0, selected:null, selectedOrigin:null, detailRequestSeq:0, returnFocus:null, records:new Map(), reviewerEpoch:0, writeOutcomes:new Map(), importBusy:false, importOutcome:null};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const cleanNumber = (value, digits=2) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const operatorToken = () => $("operator-token").value.trim();
const reviewerToken = () => $("reviewer-token").value.trim();
const authHeaders = value => value ? {Authorization:`Bearer ${value}`} : {};
const activeDetail = (id,requestSeq) => state.selected === id && state.detailRequestSeq === requestSeq;

async function api(path, options={}) {
  const response = await fetch(path, {cache:"no-store", ...options});
  let payload;
  try {payload = await response.json();} catch {throw new Error(`Server returned HTTP ${response.status}`);}
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`);
  return payload;
}

function label(value) {return ({confirmed:"Confirmed", rejected:"Rejected", unresolved:"Unresolved", real:"Confirmed", false:"Rejected", uncertain:"Uncertain"})[value] || value || "Awaiting review";}
function badge(value) {return `<span class="badge ${["confirmed","rejected","unresolved"].includes(value) ? value : "neutral"}">${esc(label(value))}</span>`;}
function safeLink(value) {try {const url=new URL(value);return ["https:","http:"].includes(url.protocol) ? url.href : "";}catch{return "";}}
function externalLink(value,text){const href=safeLink(value);return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text);}

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

async function loadEvidence(){
  const requestSeq=++state.evidenceRequestSeq;
  $("evidence-refresh").disabled=true;
  $("evidence-status").classList.remove("error");
  $("evidence-status").textContent="Loading evidence from this service…";
  try{
    const data=await api("/api/evidence");
    if(requestSeq!==state.evidenceRequestSeq)return;
    const {source={},model={},training_receipt:receipt={},runtime={},limits={}}=data;
    const evaluation=model.evaluation || {}, interval=evaluation.roc_auc_oof_bootstrap_95pct;
    $("model-evidence").innerHTML=`<div class="stat-line"><span>Evidence class</span><strong>${esc(model.evidence_class || "UNKNOWN")}</strong></div><div class="stat-line"><span>AUC · bootstrap 95% interval</span><strong>${Array.isArray(interval)&&interval.length===2 ? `${cleanNumber(interval[0],3)}–${cleanNumber(interval[1],3)}` : "UNAVAILABLE"}</strong></div><div class="stat-line"><span>Calibration error · 5 bins</span><strong>${cleanNumber(evaluation.ece5_oof_unweighted,3)}</strong></div><p class="evidence-description">${esc(evaluation.cv || "Evaluation method unavailable.")}</p><p class="evidence-description">${esc(evaluation.holdout_limit || "Independent evaluation unavailable.")}</p><details class="feature-details"><summary>Model identity and features</summary><p><code>${esc(model.id || "UNKNOWN")}</code></p><ul class="feature-list">${(model.features || []).map(name=>`<li><code>${esc(name)}</code></li>`).join("")}</ul><p>${esc(model.claim || "Model claim unavailable.")}</p></details>`;
    $("train-count").textContent=model.training_cases ?? "UNKNOWN";
    $("brier-value").textContent=cleanNumber(evaluation.brier_oof,3);
    $("source-authors").textContent=`EarthArXiv preprint · ${(source.authors || []).join(", ") || "Attribution unavailable"}.`;
    $("source-version").textContent=`Version ${source.version || "UNKNOWN"} · ${source.license || "License UNKNOWN"}. Derived file checksums are listed below.`;
    $("paper-link").href=safeLink(source.paper_url);
    $("source-link").href=safeLink(source.doi);
    const revision=runtime.build?.revision;
    const revisionUrl=revision && /^[0-9a-f]{40}$/.test(revision) && safeLink(runtime.source_repository) ? `${safeLink(runtime.source_repository).replace(/\/$/,"")}/commit/${revision}` : "";
    $("runtime-evidence").innerHTML=`<p>Evidence class: <strong>${esc(runtime.evidence_class || "UNKNOWN")}</strong></p><p class="revision-line">${revision ? externalLink(revisionUrl,revision) : "Source revision UNKNOWN"}</p><p class="evidence-description">${esc(runtime.binding_basis || "Source binding unavailable.")}</p>`;
    $("evidence-limits").innerHTML=[
      ["Independent replay",model.independent_replay || "UNAVAILABLE","A local fit and its reported evaluation do not establish independent replication."],
      ["Training receipt",receipt.signature_status || "UNKNOWN","Hashes bind artifact bytes; an unsigned receipt does not establish signer identity."],
      ["Trace authenticity",limits.trace_authentication || "UNAVAILABLE","An attachment’s shape can be checked without authenticating its source."],
      ["New-region validation",limits.new_region_validation || "UNAVAILABLE",limits.reference_waveforms_available===false ? "Reference-panel waveforms are unavailable. New regions remain unvalidated." : "Check the evidence record for the supported scope."]
    ].map(([title,status,description])=>`<article class="limit-card"><h3>${esc(title)}</h3><span class="badge neutral">${esc(status)}</span><p>${esc(description)}</p></article>`).join("");
    const hashes=[...Object.entries(source.files_sha256 || {}).map(([name,hash])=>[name,hash]),["Model SHA-256",model.sha256],["Training receipt file SHA-256",receipt.sha256],["Training chain head SHA-256",receipt.chain_head_sha256]];
    $("artifact-evidence").innerHTML=`<p>Training receipt evidence: <strong>${esc(receipt.evidence_class || "UNKNOWN")}</strong>. Values below identify the artifacts served by this application.</p><dl class="hash-list">${hashes.map(([name,hash])=>`<div><dt>${esc(name)}</dt><dd><code>${esc(hash || "UNKNOWN")}</code></dd></div>`).join("")}</dl>`;
    $("evidence-status").textContent="Evidence record loaded. Evaluation, source binding and receipt identity retain their separate evidence classes.";
  }catch(error){
    if(requestSeq!==state.evidenceRequestSeq)return;
    $("evidence-status").textContent=`Evidence unavailable: ${error.message}. Reload to try again.`;
    $("evidence-status").classList.add("error");
    $("model-evidence").innerHTML='<p class="availability">Model evidence UNKNOWN: the evidence endpoint could not be read.</p>';
    $("runtime-evidence").textContent="Source binding UNKNOWN: evidence unavailable.";
    $("source-authors").textContent="Source attribution UNKNOWN: evidence unavailable.";
    $("source-version").textContent="Source version and license UNKNOWN: evidence unavailable.";
    $("evidence-limits").replaceChildren();
    $("artifact-evidence").textContent="Artifact hashes unavailable.";
  }finally{if(requestSeq===state.evidenceRequestSeq)$("evidence-refresh").disabled=false;}
}

function recordRow(item) {
  const score = item.confirmability_score == null ? `<span class="badge neutral">${esc(item.score_status.replaceAll("_"," "))}</span>` : `<span class="badge score">${cleanNumber(item.confirmability_score,3)} · panel fit</span>`;
  return `<tr><td>${esc(item.event_id || "Blind case")}<span class="sub">${esc(item.id)}</span></td><td>${item.origin === "PUBLISHED_PANEL" ? "Published panel" : "Operator import"}</td><td>${esc(item.region)}<span class="sub">${esc(item.catalogue)}</span></td><td>${badge(item.published_consensus)}</td><td>${score}</td><td>${item.waveform_ready ? badge("Ready") : `<span class="badge neutral">Not attached</span>`}</td><td><button class="row-button" data-record="${esc(item.id)}">Inspect →</button></td></tr>`;
}

function catalogueParams(){
  const imported=state.catalogue==="__imports__";
  const params=new URLSearchParams();
  if (imported) params.set("origin","imported");
  if (state.catalogue && !imported) params.set("catalogue",state.catalogue);
  if(state.query)params.set("q",state.query);
  if(state.consensus)params.set("consensus",state.consensus);
  return params;
}

function catalogueLoading(){
  state.catalogueLoaded=false;
  state.records.clear();
  $("catalogue-results").setAttribute("aria-busy","true");
  $("detection-rows").innerHTML='<tr><td colspan="7" class="empty">Loading matching records…</td></tr>';
  $("page-status").textContent="Loading catalogue…";
  $("prev-button").disabled=true;$("next-button").disabled=true;$("export-button").disabled=true;
  $("clear-filters").disabled=!(state.query || state.catalogue || state.consensus);
}

async function loadCatalogue() {
  clearTimeout(state.searchTimer);
  const requestSeq=++state.catalogueRequestSeq,offset=state.offset;
  const params=catalogueParams();
  params.set("limit",String(state.limit));params.set("offset",String(offset));
  catalogueLoading();
  try{
    const data=await api(`/api/detections?${params}`);
    if(requestSeq!==state.catalogueRequestSeq)return;
    const visibleTotal=data.total;
    state.records=new Map(data.items.map(item=>[item.id,item]));
    state.catalogueLoaded=true;
    const filtered=Boolean(state.query || state.catalogue || state.consensus);
    $("detection-rows").innerHTML=data.items.length ? data.items.map(recordRow).join("") : `<tr><td colspan="7" class="empty"><strong>No matching records</strong><p>${state.catalogue==="__imports__" && state.consensus ? "Published verdicts are unavailable for blind imports." : "Try a different query or catalogue."}</p>${filtered ? '<button class="btn btn-secondary btn-sm" id="empty-clear-filters" type="button">Clear filters</button>' : ""}</td></tr>`;
    $("page-status").textContent=visibleTotal ? `Showing ${offset+1}–${Math.min(offset+data.items.length,visibleTotal)} of ${visibleTotal} matching records` : "0 matching records";
    $("prev-button").disabled=offset===0;
    $("next-button").disabled=offset+state.limit>=visibleTotal;
    $("empty-clear-filters")?.addEventListener("click",()=>clearFilters(true));
    document.querySelectorAll("[data-record]").forEach(button=>button.addEventListener("click",()=>openDetail(button.dataset.record,state.records.get(button.dataset.record)?.origin)));
    return true;
  }catch(error){
    if(requestSeq!==state.catalogueRequestSeq)return;
    $("detection-rows").innerHTML=`<tr><td colspan="7" class="empty"><strong>Catalogue unavailable</strong><p>${esc(error.message)}</p><button class="btn btn-secondary btn-sm" id="retry-catalogue" type="button">Try again</button></td></tr>`;
    $("page-status").textContent="Catalogue could not be loaded. Try again or change filters.";
    $("retry-catalogue").addEventListener("click",()=>{$("catalogue-heading").focus();loadCatalogue();});
    return false;
  }finally{
    if(requestSeq===state.catalogueRequestSeq){$("catalogue-results").setAttribute("aria-busy","false");$("export-button").disabled=!state.catalogueLoaded || state.exportBusy;}
  }
}

function clearFilters(focusSearch=false){
  state.query="";state.catalogue="";state.consensus="";state.offset=0;
  $("search-query").value="";$("catalogue-filter").value="";$("verdict-filter").value="";
  if(focusSearch)$("search-query").focus();
  loadCatalogue();
}

async function exportCatalogue(){
  if(state.exportBusy || !state.catalogueLoaded)return;
  // Capture filters once: changes during a download cannot change its query.
  const params=catalogueParams(),filterDescription=params.toString() || "all visible cards";
  const feedback=$("export-feedback");
  state.exportBusy=true;$("export-button").disabled=true;
  feedback.classList.remove("error");feedback.textContent="Preparing JSON export for the selected view…";
  try{
    const data=await api(`/api/export?${params}`);
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+"\n"],{type:"application/json"}));
    const anchor=document.createElement("a");anchor.href=url;anchor.download="szl-seismic-catalogue.json";
    document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    feedback.textContent=data.truncated ? `Downloaded ${data.count} of ${data.total} matching visible cards. Export is partial; the next offset is ${data.next_offset}. Requested view: ${filterDescription}.` : `Downloaded ${data.count} matching visible cards. Requested view: ${filterDescription}.`;
  }catch(error){feedback.textContent=`Export failed: ${error.message}`;feedback.classList.add("error");}
  finally{state.exportBusy=false;$("export-button").disabled=!state.catalogueLoaded;}
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
  const requestSeq=++state.detailRequestSeq;
  const detailCredential=reviewerToken() || operatorToken();
  const reviewerCredential=reviewerToken();
  const alreadyOpen=$("detail-drawer").classList.contains("open");
  if(!alreadyOpen)state.returnFocus=document.activeElement;
  state.selected=id;
  state.selectedOrigin=origin;
  $("drawer-backdrop").hidden=false;
  $("detail-drawer").classList.add("open");
  $("detail-drawer").setAttribute("aria-hidden","false");
  document.querySelector(".shell").inert=true;
  document.body.classList.add("drawer-visible");
  if(!alreadyOpen)$("close-drawer").focus();
  $("reviewer-access").hidden=origin!=="OPERATOR_IMPORT";
  $("detail-content").setAttribute("aria-busy","true");
  $("detail-content").innerHTML=`<p role="status">Loading record…</p>`;
  showAcknowledgedWrites(id,requestSeq);
  if(origin==="OPERATOR_IMPORT" && !detailCredential){
    $("detail-content").innerHTML=`<h2>Blind detection</h2><p class="warning">This imported case requires a reviewer token or a source custodian token. Enter a reviewer token above to inspect the blind traces, or add a source custodian token in Operator tools to attach missing waveforms.</p><p>Published records remain available without credentials.</p>`;
    $("detail-content").setAttribute("aria-busy","false");
    showAcknowledgedWrites(id,requestSeq);
    return;
  }
  try{
    const data=await api(`/api/detections/${encodeURIComponent(id)}`,{headers:authHeaders(detailCredential)});
    if(!activeDetail(id,requestSeq) || (reviewerToken() || operatorToken())!==detailCredential)return;
    state.selectedOrigin=data.origin;
    $("reviewer-access").hidden=data.origin!=="OPERATOR_IMPORT";
    $("detail-content").innerHTML=data.origin==="PUBLISHED_PANEL" ? publishedDetail(data) : importedDetail(data);
    if(notice){const message=document.createElement("p");message.className="success-note";message.textContent=notice;$("detail-content").prepend(message);}
    document.querySelectorAll("[data-verdict]").forEach(button=>button.addEventListener("click",()=>submitReview(button.dataset.verdict,id,reviewerCredential,requestSeq)));
    $("attach-waveform")?.addEventListener("click",()=>attachWaveform(id,requestSeq));
    showAcknowledgedWrites(id,requestSeq);
    return true;
  }catch(error){if(activeDetail(id,requestSeq)){$("detail-content").innerHTML=`<p role="alert">Could not load record: ${esc(error.message)}</p>`;showAcknowledgedWrites(id,requestSeq,"Case detail could not be refreshed.");}return false;}
  finally{if(activeDetail(id,requestSeq))$("detail-content").setAttribute("aria-busy","false");}
}

function closeDetail(){
  if(!$("detail-drawer").classList.contains("open"))return;
  ++state.detailRequestSeq;state.selected=null;state.selectedOrigin=null;
  $("detail-drawer").classList.remove("open");$("detail-drawer").setAttribute("aria-hidden","true");$("drawer-backdrop").hidden=true;
  document.querySelector(".shell").inert=false;document.body.classList.remove("drawer-visible");
  const target=state.returnFocus?.isConnected ? state.returnFocus : $("catalogue-heading");
  target.focus();state.returnFocus=null;
}

function drawerKeyboard(event){
  if(!$("detail-drawer").classList.contains("open"))return;
  if(event.key==="Escape"){event.preventDefault();closeDetail();return;}
  if(event.key!=="Tab")return;
  const focusable=[...$("detail-drawer").querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]')].filter(element=>element.getClientRects().length);
  const first=focusable[0],last=focusable[focusable.length-1];
  if(!first){event.preventDefault();$("detail-drawer").focus();return;}
  if(event.shiftKey && (document.activeElement===first || !$("detail-drawer").contains(document.activeElement))){event.preventDefault();last.focus();}
  else if(!event.shiftKey && (document.activeElement===last || !$("detail-drawer").contains(document.activeElement))){event.preventDefault();first.focus();}
}

function clearDetailOnCredentialChange(event){
  if(event?.target.id==="reviewer-token")++state.reviewerEpoch;
  if(state.selectedOrigin==="OPERATOR_IMPORT" && state.selected){
    ++state.detailRequestSeq;
    $("detail-content").setAttribute("aria-busy","false");
    $("detail-content").textContent="Credentials changed. Open this case again to refresh the blind view.";
  }
}

function reviewOutcomeKey(id,epoch=state.reviewerEpoch){return `review:${epoch}:${id}`;}
function detailWriteOutcomes(id){return [state.writeOutcomes.get(reviewOutcomeKey(id)),state.writeOutcomes.get(`waveform:${id}`)].filter(Boolean);}

function showAcknowledgedWrites(id,requestSeq,detailError=""){
  if(!activeDetail(id,requestSeq))return;
  $("acknowledged-writes")?.remove();
  const outcomes=detailWriteOutcomes(id);
  if(!outcomes.length)return;
  const container=document.createElement("div");container.id="acknowledged-writes";container.setAttribute("role","status");
  outcomes.forEach(outcome=>{const message=document.createElement("p");message.className="success-note";message.textContent=outcome.notice;container.append(message);});
  if(state.writeOutcomes.has(reviewOutcomeKey(id)))document.querySelectorAll("[data-verdict]").forEach(button=>button.disabled=true);
  if(state.writeOutcomes.has(`waveform:${id}`) && $("attach-waveform"))$("attach-waveform").disabled=true;
  const errors=[...new Set([...outcomes.map(outcome=>outcome.refreshError).filter(Boolean),detailError].filter(Boolean))];
  if(errors.length){
    const warning=document.createElement("p");warning.className="warning";warning.textContent=`The write was acknowledged; ${errors.join(" ")} Retry refresh only; do not submit the write again.`;container.append(warning);
    const retry=document.createElement("button");retry.id="retry-write-refresh";retry.type="button";retry.className="btn btn-secondary btn-sm";retry.textContent="Retry refresh";
    retry.addEventListener("click",()=>{if(activeDetail(id,requestSeq)){retry.disabled=true;refreshAfterWrite(outcomes[outcomes.length-1],id,requestSeq);}});container.append(retry);
  }
  $("detail-content").prepend(container);
}

async function refreshOverviewAfterWrite(){
  const results=await Promise.allSettled([loadCatalogue(),loadSummary()]);
  const errors=[];
  if(results[0].status==="rejected" || results[0].value===false)errors.push("Catalogue could not be refreshed.");
  if(results[1].status==="rejected")errors.push("Summary counts could not be refreshed.");
  return errors;
}

async function refreshAfterWrite(outcome,id,requestSeq){
  // POST acknowledgement survives every subsequent GET failure. Reads cannot
  // re-enable a locked write, and the retry callback invokes only these GETs.
  const overview=refreshOverviewAfterWrite();
  const detail=activeDetail(id,requestSeq) ? openDetail(id,"OPERATOR_IMPORT") : Promise.resolve();
  const refreshSeq=state.detailRequestSeq;
  const [errors,detailResult]=await Promise.all([overview,detail]);
  if(detailResult===false)errors.push("Case detail could not be refreshed.");
  outcome.refreshError=errors.join(" ");
  // Clear stale refresh warnings for the same case after a successful retry.
  detailWriteOutcomes(id).forEach(item=>item.refreshError=outcome.refreshError);
  if(activeDetail(id,refreshSeq))showAcknowledgedWrites(id,refreshSeq);
}

async function submitReview(verdict,caseId,credential,requestSeq){
  const outcomeKey=reviewOutcomeKey(caseId);
  if(!activeDetail(caseId,requestSeq) || state.writeOutcomes.has(outcomeKey))return;
  const note=$("review-note").value.trim(),feedback=$("review-feedback");
  if(!credential || reviewerToken()!==credential){feedback.textContent="Reopen this case with the reviewer token shown above.";feedback.classList.add("error");return;}
  const buttons=[...document.querySelectorAll("[data-verdict]")];buttons.forEach(button=>button.disabled=true);
  feedback.textContent="Locking verdict…";feedback.classList.remove("error");
  try{
    await api("/api/reviews",{method:"POST",headers:{...authHeaders(credential),"Content-Type":"application/json"},body:JSON.stringify({detection_id:caseId,verdict,note})});
  }catch(error){if(activeDetail(caseId,requestSeq)){feedback.textContent=error.message;feedback.classList.add("error");buttons.forEach(button=>button.disabled=false);}return;}
  const outcome={notice:"Verdict locked for the reviewer identity bound to this token.",refreshError:""};
  state.writeOutcomes.set(outcomeKey,outcome);
  showAcknowledgedWrites(caseId,requestSeq);
  await refreshAfterWrite(outcome,caseId,requestSeq);
}

async function attachWaveform(caseId,requestSeq){
  if(!activeDetail(caseId,requestSeq) || state.writeOutcomes.has(`waveform:${caseId}`))return;
  const credential=operatorToken();
  const feedback=$("waveform-feedback"),file=$("waveform-file").files[0];
  feedback.classList.remove("error");
  if(!credential){feedback.textContent="Enter the source custodian token in Operator tools, then reopen this record.";feedback.classList.add("error");return;}
  if(!file){feedback.textContent="Choose a waveform JSON file.";feedback.classList.add("error");return;}
  if(file.size>1_000_000){feedback.textContent="Waveform JSON exceeds the 1 MB request limit.";feedback.classList.add("error");return;}
  let body;
  try{
    const parsed=JSON.parse(await file.text());
    if(!parsed || typeof parsed!=="object" || Array.isArray(parsed) || typeof parsed.source_uri!=="string" || !parsed.source_uri.trim() || !Number.isFinite(Number(parsed.sample_rate_hz)) || Number(parsed.sample_rate_hz)<=0 || !Array.isArray(parsed.stations) || parsed.stations.length!==5 || parsed.stations.some(station=>!station || !Array.isArray(station.samples) || station.samples.length<2 || station.samples.length>10000 || station.samples.some(value=>!Number.isFinite(Number(value)))))throw new Error("JSON must include a source URI, a positive sample rate, and exactly five numeric station traces.");
    body=JSON.stringify(parsed);
  }catch(error){feedback.textContent=error instanceof SyntaxError ? "The selected file is not valid JSON." : error.message;feedback.classList.add("error");return;}
  if(!activeDetail(caseId,requestSeq))return;
  const button=$("attach-waveform");button.disabled=true;
  feedback.textContent="Attaching and locking waveform evidence…";
  let result;
  try{result=await api(`/api/detections/${encodeURIComponent(caseId)}/waveform`,{method:"POST",headers:{...authHeaders(credential),"Content-Type":"application/json"},body});}
  catch(error){if(activeDetail(caseId,requestSeq)){feedback.textContent=error.message;feedback.classList.add("error");button.disabled=false;}return;}
  const outcome={notice:`Five traces attached and locked. SHA-256: ${result.waveform_sha256}. Source authenticity is unverified.`,refreshError:""};
  state.writeOutcomes.set(`waveform:${caseId}`,outcome);
  showAcknowledgedWrites(caseId,requestSeq);
  await refreshAfterWrite(outcome,caseId,requestSeq);
}

function showImportOutcome(outcome){
  if(state.importOutcome!==outcome)return;
  const feedback=$("import-feedback");feedback.classList.remove("error");feedback.textContent=outcome.notice;
  if(outcome.refreshError){
    feedback.append(document.createTextNode(` The import was acknowledged; ${outcome.refreshError} Retry refresh only.`));
    const retry=document.createElement("button");retry.id="retry-import-refresh";retry.type="button";retry.className="btn btn-secondary btn-sm";retry.textContent="Retry refresh";
    retry.addEventListener("click",async()=>{retry.disabled=true;outcome.refreshError=(await refreshOverviewAfterWrite()).join(" ");showImportOutcome(outcome);});feedback.append(retry);
  }
}

async function importCatalogue(){
  if(state.importBusy)return;
  const file=$("import-file").files[0],catalogue=$("import-catalogue").value.trim(),region=$("import-region").value.trim(),feedback=$("import-feedback");
  if(!operatorToken()){feedback.textContent="Enter a source custodian token.";feedback.classList.add("error");return;}
  if(!file||!catalogue||!region){feedback.textContent="Choose a CSV and enter catalogue and region.";feedback.classList.add("error");return;}
  const credential=operatorToken(),button=$("import-button");state.importBusy=true;button.disabled=true;feedback.textContent="Importing catalogue…";feedback.classList.remove("error");
  let result;
  try{
    const query=new URLSearchParams({catalogue,region});
    result=await api(`/api/catalogues/import?${query}`,{method:"POST",headers:{...authHeaders(credential),"Content-Type":"text/csv; charset=utf-8"},body:await file.text()});
  }catch(error){feedback.textContent=error.message;feedback.classList.add("error");state.importBusy=false;button.disabled=false;return;}
  const outcome={notice:`Imported ${result.imported} records. Source SHA-256: ${result.source_sha256}. Waveform attachment remains required. Select a new file for another import.`,refreshError:""};
  state.importOutcome=outcome;
  if($("import-file").files[0]===file)$("import-file").value="";
  showImportOutcome(outcome);
  state.offset=0;state.catalogue="__imports__";state.query="";state.consensus="";$("catalogue-filter").value="__imports__";$("search-query").value="";$("verdict-filter").value="";
  outcome.refreshError=(await refreshOverviewAfterWrite()).join(" ");
  showImportOutcome(outcome);state.importBusy=false;
  button.disabled=!state.meta.import_enabled || !$("import-file").files.length;
}

async function start(){
  $("close-drawer").addEventListener("click",closeDetail);$("drawer-backdrop").addEventListener("click",closeDetail);
  document.addEventListener("keydown",drawerKeyboard);
  $("open-reviewer-case").addEventListener("click",()=>{if(state.selected)openDetail(state.selected,"OPERATOR_IMPORT");});
  $("reviewer-token").addEventListener("keydown",event=>{if(event.key==="Enter" && state.selected)openDetail(state.selected,"OPERATOR_IMPORT");});
  $("reviewer-token").addEventListener("input",clearDetailOnCredentialChange);
  $("operator-token").addEventListener("input",clearDetailOnCredentialChange);
  $("refresh-button").addEventListener("click",async()=>{
    $("refresh-button").disabled=true;
    try{await Promise.all([loadSummary(),loadCatalogue()]);}catch(error){$("page-status").textContent=`Counts could not be refreshed: ${error.message}`;}
    finally{$("refresh-button").disabled=false;}
  });
  $("catalogue-search").addEventListener("submit",event=>{event.preventDefault();clearTimeout(state.searchTimer);state.query=$("search-query").value.trim();state.offset=0;loadCatalogue();});
  $("search-query").addEventListener("input",event=>{
    clearTimeout(state.searchTimer);state.query=event.target.value.trim();state.offset=0;
    ++state.catalogueRequestSeq;catalogueLoading();
    state.searchTimer=setTimeout(loadCatalogue,250);
  });
  $("catalogue-filter").addEventListener("change",event=>{state.catalogue=event.target.value;state.offset=0;loadCatalogue();});
  $("verdict-filter").addEventListener("change",event=>{state.consensus=event.target.value;state.offset=0;loadCatalogue();});
  $("clear-filters").addEventListener("click",()=>clearFilters(true));
  $("export-button").addEventListener("click",exportCatalogue);
  $("evidence-refresh").addEventListener("click",loadEvidence);
  $("prev-button").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-state.limit);loadCatalogue();});
  $("next-button").addEventListener("click",()=>{state.offset+=state.limit;loadCatalogue();});
  $("import-button").addEventListener("click",importCatalogue);
  $("import-file").addEventListener("change",()=>{$("import-button").disabled=state.importBusy || !state.meta?.import_enabled || !$("import-file").files.length;});
  const results=await Promise.allSettled([loadMeta(),loadSummary(),loadFilters(),loadEvidence()]);
  if(results.slice(0,3).some(result=>result.status==="rejected")){
    $("storage-state").textContent="Source metadata unavailable";
    $("source-state").textContent="Source metadata UNKNOWN";
  }
  await loadCatalogue();
}
start();
