const state = { filters: {}, companies: [], selectedId: null, detail: null, summary: null, signupUrl: "", draft: null, delivered: false, responseRecorded: false };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const compact = (value) => String(value || "UNKNOWN").replaceAll("_", " ");
const pillClass = (value) => ["VERIFIED","ELIGIBLE","HIGH","APPROVED","HAS_SUITABLE_CHANNEL"].includes(String(value).toUpperCase()) ? "good" : ["UNKNOWN","NEEDS REVIEW","NEEDS_REVIEW","PENDING","BASELINE_NOT_RECHECKED","MEDIUM"].includes(String(value).toUpperCase()) ? "warn" : ["REJECTED_MISMATCH","REJECTED","LOW","NO_SUITABLE_CHANNEL"].includes(String(value).toUpperCase()) ? "bad" : "neutral";

async function api(path) {
  const response = await fetch(path, {headers: {"Accept":"application/json"}, cache:"no-store"});
  if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
  return response.json();
}
function setOptions(id, values) {
  const select = $(id), first = select.options[0];
  select.replaceChildren(first);
  values.forEach((value) => { const option = document.createElement("option"); option.value = value; option.textContent = compact(value); select.append(option); });
}
function distCard(title, values, total) {
  const rows = Object.entries(values).sort((a,b) => b[1]-a[1]).map(([name,count]) => `<div class="dist-row"><span>${esc(compact(name))}</span><div class="bar"><i style="width:${total ? Math.round(count*100/total) : 0}%"></i></div><span class="count">${count}</span></div>`).join("");
  return `<div class="distribution"><h3>${esc(title)}</h3>${rows}</div>`;
}
function renderSummary(summary) {
  const coverage = summary.contact_channel_coverage;
  const reviews = summary.pending_human_reviews;
  const outreach = summary.outreach;
  const metrics = [
    ["Companies discovered", summary.total_companies, "companies in the research cohort"],
    ["Verified company websites", summary.website_verification.VERIFIED || 0, "based on saved evidence"],
    ["GEO-eligible companies", summary.eligibility.ELIGIBLE || 0, "current qualification results"],
    ["Suitable contact channels", coverage.with_suitable_first_party_channel, "saved first-party email or form"],
  ];
  $("metrics").innerHTML = metrics.map(([label,value,note]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join("");
  $("secondary-metrics").innerHTML = [
    ["Companies awaiting human review", reviews.companies],
    ["Suitable channels awaiting review", reviews.contact_channels],
    ["Saved simulated deliveries / real sent", `${outreach.simulated_delivery_events} / ${outreach.real_sent_records}`],
    ["Saved simulated responses", outreach.simulated_response_events],
    ["Saved simulated signup handoffs", outreach.simulated_handoff_events],
    ["Real outreach records", outreach.real_outreach_records],
  ].map(([label,value]) => `<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  $("distributions").innerHTML = [
    distCard("WEBSITE IDENTITY", summary.website_verification, summary.total_companies),
    distCard("ELIGIBILITY", summary.eligibility, summary.total_companies),
    distCard("GEO OPPORTUNITY", summary.geo_opportunity, summary.total_companies),
    distCard("RESEARCH PRIORITY", summary.research_priority, summary.total_companies),
  ].join("");
  setOptions("source", summary.filters.sources);
  setOptions("verification", summary.filters.website_verification);
  setOptions("eligibility", summary.filters.eligibility);
  setOptions("geo", summary.filters.geo_opportunity);
  setOptions("priority", summary.filters.research_priority);
}
function pill(value) { return `<span class="pill ${pillClass(value)}">${esc(compact(value))}</span>`; }
function renderCompanies(rows) {
  state.companies = rows;
  $("result-count").textContent = `${rows.length} COMPANIES`;
  $("company-rows").innerHTML = rows.slice(0, 250).map((row) => `<tr data-id="${row.id}" tabindex="0" role="button" aria-pressed="${row.id === state.selectedId}" class="${row.id === state.selectedId ? "selected" : ""}"><td><span class="company-cell">${esc(row.company_name)}</span><span class="company-url">${esc(row.website)}</span></td><td>${esc(row.discovery_sources.join(", ") || "Unknown")}</td><td>${pill(row.identity_verification_status)}</td><td>${pill(row.eligibility)}</td><td>${pill(row.geo_opportunity)}</td><td>${pill(row.research_priority)}</td><td class="${row.has_suitable_channel ? "contact-yes" : "contact-no"}">${row.has_suitable_channel ? "Available" : "None"}</td></tr>`).join("") || `<tr><td colspan="7">No companies match the selected filters.</td></tr>`;
  document.querySelectorAll("#company-rows tr[data-id]").forEach((row) => {
    row.addEventListener("click", () => selectCompany(Number(row.dataset.id)));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectCompany(Number(row.dataset.id)); }
    });
  });
}
async function reloadCompanies() {
  const query = new URLSearchParams();
  Object.entries(state.filters).forEach(([key,value]) => { if (value) query.set(key,value); });
  const result = await api(`/api/companies?${query.toString()}`);
  renderCompanies(result.companies);
  if (state.selectedId && !result.companies.some((company) => company.id === state.selectedId)) {
    state.selectedId = null; state.detail = null; renderEmptyDetail(); resetSimulation();
  }
}
function renderEmptyDetail() {
  $("company-detail").innerHTML = `<div class="empty-state"><div class="empty-icon">↖</div><h3>Choose a company</h3><p>Select a row to inspect its source, qualification rationale, missing evidence, contacts, and review states.</p></div>`;
}
function listItems(items, empty) {
  if (!items || !items.length) return `<p>${esc(empty)}</p>`;
  return `<ul>${items.map((item) => `<li>${esc(typeof item === "string" ? item : JSON.stringify(item))}</li>`).join("")}</ul>`;
}
function renderDetail(company) {
  const sources = (company.provenance || []).map((item) => `<div class="channel-card"><strong>${esc(item.source_name || item.source_type || "Source")}</strong><small>Record: ${esc(item.source_identifier || "not recorded")}</small><small>Source URL: ${esc(item.source_url || "not recorded")}</small></div>`).join("") || "<p>No provenance records.</p>";
  const evidence = (company.qualification_evidence_items || []).map((item) => `<span class="evidence-chip">${esc(typeof item === "string" ? item : `${item.label || "Evidence"}: ${item.value ?? ""}`)}</span>`).join("") || "<p>No structured qualification evidence recorded.</p>";
  const contacts = (company.contact_channels || []).map((contact) => `<div class="channel-card"><strong>${esc(contact.channel_type || "Unclassified contact")}${contact.is_suitable_first_party ? " · SUITABLE FIRST-PARTY" : " · REVIEW / NOT SUITABLE"}</strong><small>${esc(contact.email || contact.channel_url || "No recipient value stored")}</small><small>Evidence: ${esc(contact.source_url || "not recorded")}</small><small>Review: ${esc(contact.review_status || "PENDING")} · Validation: ${esc(contact.validation_status || "UNKNOWN")}</small><small>${esc(contact.evidence_text || contact.purpose || "No excerpt recorded")}</small></div>`).join("") || "<p>No contact records stored.</p>";
  $("company-detail").innerHTML = `<div class="detail-title"><div><h2>${esc(company.company_name)}</h2><div class="detail-url">${esc(company.website)}</div></div>${pill(company.identity_verification_status)}</div><div class="detail-grid"><div class="detail-stat"><span>Eligibility</span><strong>${esc(compact(company.eligibility || "UNKNOWN"))}</strong></div><div class="detail-stat"><span>GEO opportunity</span><strong>${esc(compact(company.geo_opportunity || "UNKNOWN"))}</strong></div><div class="detail-stat"><span>Research priority</span><strong>${esc(compact(company.priority_tier || "NEEDS_REVIEW"))} · ${esc(company.priority_score ?? "n/a")}</strong></div><div class="detail-stat"><span>Company review</span><strong>${esc(company.review_status || "PENDING")}</strong></div></div><div class="subsection"><h4>Qualification reasons</h4>${listItems(company.qualification_reasons, "No qualification reason recorded.")}</div><div class="subsection"><h4>GEO opportunity rationale</h4>${listItems(company.geo_reasons, "No GEO rationale recorded.")}</div><div class="subsection"><h4>Recorded evidence</h4><div>${evidence}</div></div><div class="subsection"><h4>Missing evidence</h4>${listItems(company.missing_evidence, "No missing evidence identified by this view.")}</div><div class="subsection"><h4>Discovery provenance</h4>${sources}</div><div class="subsection"><h4>Contact channels</h4>${contacts}</div><div class="subsection"><h4>Human review state</h4><div class="review-line"><span>Company</span>${pill(company.review_status || "PENDING")}</div><div class="review-line"><span>Suitable channels pending review</span><strong>${company.contact_channels.filter((item)=>item.is_suitable_first_party && item.review_status === "PENDING").length}</strong></div><p>Machine qualification and saved channel evidence do not equal human approval.</p></div>`;
}
async function selectCompany(id) {
  state.selectedId = id;
  state.draft = null; state.delivered = false; state.responseRecorded = false;
  $("draft-preview").classList.add("hidden"); $("simulate-controls").classList.add("hidden"); $("simulation-log").innerHTML = "";
  const company = await api(`/api/companies/${id}`);
  state.detail = company;
  renderDetail(company);
  const suitable = company.contact_channels.filter((item) => item.is_suitable_first_party);
  const selector = $("channel-select");
  selector.replaceChildren();
  if (!suitable.length) {
    selector.add(new Option("No suitable first-party channel stored", ""));
    selector.disabled = true; $("preview-button").disabled = true;
  } else {
    selector.add(new Option("Select a saved evidence channel…", ""));
    suitable.forEach((item) => selector.add(new Option(`${item.channel_type} · ${item.email || item.channel_url}`, item.id)));
    selector.disabled = false;
    const mayDraft = company.eligibility === "ELIGIBLE" && company.identity_verification_status === "VERIFIED";
    $("preview-button").disabled = !mayDraft;
    if (!mayDraft) $("preview-button").textContent = "Requires verified identity and eligibility";
  }
  if (suitable.length && company.eligibility === "ELIGIBLE" && company.identity_verification_status === "VERIFIED") $("preview-button").textContent = "Generate draft preview →";
  if (!suitable.length) $("preview-button").textContent = "No usable contact channel";
  await reloadCompanies();
}
function resetSimulation() {
  state.draft = null; state.delivered = false; state.responseRecorded = false;
  $("draft-preview").classList.add("hidden"); $("simulate-controls").classList.add("hidden"); $("simulation-log").innerHTML = "";
  $("channel-select").replaceChildren(new Option("Select a company first", "")); $("channel-select").disabled = true; $("preview-button").disabled = true;
}
async function previewDraft() {
  if (!state.selectedId || !$("channel-select").value) return;
  const result = await api(`/api/draft/${state.selectedId}?contact_id=${encodeURIComponent($("channel-select").value)}`);
  state.draft = result;
  const container = $("draft-preview");
  container.classList.remove("hidden");
  if (result.status !== "DRAFT_READY") {
    container.innerHTML = `<div class="draft-banner">${esc(result.label)}</div><p>${esc(result.reason)}</p>`;
    $("simulate-controls").classList.add("hidden"); return;
  }
  const destination = result.recipient ? `Recipient: ${result.recipient}` : `Official form: ${result.form_url}`;
  const formNotes = result.form_url ? `<p>Form field map: ${esc(Object.keys(result.form_field_mapping).join(", ") || "no fields confidently mapped")} · ${esc(result.form_safety.submission_status)} · Manual submission is not available.</p>` : "";
  container.innerHTML = `<div class="draft-banner">${esc(result.label)}</div><div class="draft-heading">${esc(result.subject)}</div><div class="draft-meta">${esc(destination)}<br>Company review: ${esc(result.company_review_status)} · Channel review: ${esc(result.channel_review_status)} · Approval: ${esc(result.approval_status)}<br>Evidence: ${esc(result.evidence_url)}</div>${formNotes}<pre class="draft-body">${esc(result.body)}</pre>`;
  $("simulate-controls").classList.remove("hidden"); $("response-controls").classList.add("hidden"); $("deliver-button").disabled = false; $("simulation-log").innerHTML = "";
}
function addEvent(title, detail) {
  const event = document.createElement("div"); event.className = "event";
  event.innerHTML = `<strong>${esc(title)}</strong><br>${esc(detail)}`;
  $("simulation-log").append(event);
}
function simulateDelivery() {
  if (!state.draft || state.draft.status !== "DRAFT_READY" || state.delivered) return;
  state.delivered = true;
  $("deliver-button").disabled = true;
  addEvent("SIMULATED DELIVERY", `Local-only demo event for ${state.draft.company_name}. No provider or network call occurred; actual outreach remains NOT SENT.`);
  $("response-controls").classList.remove("hidden");
}
function recordResponse() {
  if (!state.delivered || state.responseRecorded) return;
  state.responseRecorded = true;
  const response = $("response-select").value;
  if (response === "UNANSWERED") {
    addEvent("SIMULATED — NO RESPONSE", "The demonstration ends without a response. No follow-up is scheduled.");
    return;
  }
  if (response === "DECLINED") {
    addEvent("SIMULATED DECLINED RESPONSE", "Fixture response only. No actual company response or human review state is recorded.");
    return;
  }
  addEvent("SIMULATED INTERESTED RESPONSE", "Fixture response only. Interest is not research consent or participation approval.");
  const handoff = document.createElement("button"); handoff.className = "button outline"; handoff.textContent = "Show simulated research signup handoff";
  handoff.addEventListener("click", () => {
    addEvent("SIMULATED SIGNUP HANDOFF", `${state.signupUrl || "Reserved demo-only URL"}. It is non-operational; signup is not completed and consent is NOT ESTABLISHED.`);
    handoff.disabled = true;
  });
  $("simulation-log").append(handoff);
}
function wireFilters() {
  const bindings = [["search","q"],["source","source"],["verification","verification"],["eligibility","eligibility"],["geo","geo"],["priority","priority"],["channel","channel"]];
  bindings.forEach(([element,key]) => $(element).addEventListener(element === "search" ? "input" : "change", () => { state.filters[key] = $(element).value; reloadCompanies().catch(showError); }));
  $("preview-button").addEventListener("click", () => previewDraft().catch(showError));
  $("deliver-button").addEventListener("click", simulateDelivery);
  $("response-button").addEventListener("click", recordResponse);
}
function showError(error) { $("result-count").textContent = "DATA LOAD ERROR"; $("result-count").title = error.message; }
async function start() {
  wireFilters();
  const result = await api("/api/summary");
  state.summary = result.summary; state.signupUrl = result.simulated_signup_url || "";
  renderSummary({...result.summary, filters: result.filters});
  await reloadCompanies();
}
start().catch(showError);
