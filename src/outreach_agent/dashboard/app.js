const state = { filters: {}, lastFilterKey: "", companies: [], totalCompanies: 0, page: 1, pageSize: 50, selectedId: null, detail: null, summary: null, participationFormUrl: "", formConfiguration: {}, draft: null, delivered: false, responseRecorded: false };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const compact = (value) => String(value || "UNKNOWN").replaceAll("_", " ");
const websiteDomain = (value) => {
  try { return new URL(value).hostname.replace(/^www\./i, ""); }
  catch { return String(value || "Website not recorded"); }
};
const pillClass = (value) => ["VERIFIED","ELIGIBLE","HIGH","APPROVED","HAS_SUITABLE_CHANNEL"].includes(String(value).toUpperCase()) ? "good" : ["UNKNOWN","NEEDS REVIEW","NEEDS_REVIEW","PENDING","BASELINE_NOT_RECHECKED","MEDIUM"].includes(String(value).toUpperCase()) ? "warn" : ["REJECTED_MISMATCH","REJECTED","LOW","NO_SUITABLE_CHANNEL"].includes(String(value).toUpperCase()) ? "bad" : "neutral";

async function api(path) {
  const response = await fetch(path, {headers: {"Accept":"application/json"}, cache:"no-store"});
  if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
  return response.json();
}
async function apiWrite(path, payload) {
  const response = await fetch(path, {method:"POST", headers:{"Accept":"application/json","Content-Type":"application/json"}, body:JSON.stringify(payload), cache:"no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Dashboard request failed (${response.status})`);
  return result;
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
  $("result-count").textContent = `${state.totalCompanies} MATCHING COMPANIES`;
  const first = state.totalCompanies ? (state.page - 1) * state.pageSize + 1 : 0;
  const last = Math.min(state.totalCompanies, state.page * state.pageSize);
  $("company-page").textContent = state.totalCompanies ? `Showing ${first}–${last} of ${state.totalCompanies} · page ${state.page}` : "No matching companies";
  $("company-prev").disabled = state.page <= 1;
  $("company-next").disabled = last >= state.totalCompanies;
  $("company-rows").innerHTML = rows.map((row) => `<button type="button" role="option" data-id="${row.id}" aria-selected="${row.id === state.selectedId}" class="company-row${row.id === state.selectedId ? " selected" : ""}"><span class="company-main"><span class="company-name">${esc(row.company_name)}</span><span class="company-url">${esc(websiteDomain(row.website))}</span></span><span class="company-status"><span class="company-status-label">Priority</span>${pill(row.research_priority)}<span class="company-status-label">Website</span>${pill(row.identity_verification_status)}</span></button>`).join("") || `<div class="no-results" role="status">No companies match the selected filters.</div>`;
  document.querySelectorAll("#company-rows .company-row[data-id]").forEach((row) => {
    row.addEventListener("click", () => selectCompany(Number(row.dataset.id)));
  });
}
async function reloadCompanies() {
  const filterKey = JSON.stringify(state.filters);
  const filtersChanged = filterKey !== state.lastFilterKey;
  state.lastFilterKey = filterKey;
  const query = new URLSearchParams();
  Object.entries(state.filters).forEach(([key,value]) => { if (value) query.set(key,value); });
  query.set("page", String(state.page)); query.set("page_size", String(state.pageSize));
  const result = await api(`/api/companies?${query.toString()}`);
  state.totalCompanies = result.total;
  renderCompanies(result.companies);
  if (filtersChanged && state.selectedId && !result.companies.some((company) => company.id === state.selectedId)) {
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
  $("company-detail").innerHTML = `<div class="detail-title"><div><h2>${esc(company.company_name)}</h2><div class="detail-url">${esc(company.website)}</div></div>${pill(company.identity_verification_status)}</div><div class="detail-grid"><div class="detail-stat"><span>Eligibility</span><strong>${esc(compact(company.eligibility || "UNKNOWN"))}</strong></div><div class="detail-stat"><span>GEO opportunity</span><strong>${esc(compact(company.geo_opportunity || "UNKNOWN"))}</strong></div><div class="detail-stat"><span>Research priority</span><strong>${esc(compact(company.priority_tier || "NEEDS_REVIEW"))} · ${esc(company.priority_score ?? "n/a")}</strong></div><div class="detail-stat"><span>Company review</span><strong>${esc(company.review_status || "PENDING")}</strong></div></div><div class="subsection"><h4>Qualification reasons</h4>${listItems(company.qualification_reasons, "No qualification reason recorded.")}</div><div class="subsection"><h4>GEO opportunity rationale</h4>${listItems(company.geo_reasons, "No GEO rationale recorded.")}</div><div class="subsection"><h4>Recorded evidence</h4><div>${evidence}</div></div><div class="subsection"><h4>Missing evidence</h4>${listItems(company.missing_evidence, "No missing evidence identified by this view.")}</div><div class="subsection"><h4>Discovery provenance</h4>${sources}</div><div class="subsection"><h4>Contact channels</h4>${contacts}</div><div class="subsection"><h4>Human review state</h4><div class="review-line"><span>Company</span>${pill(company.review_status || "PENDING")}</div><div class="review-line"><span>Suitable channels pending review</span><strong>${company.contact_channels.filter((item)=>item.is_suitable_first_party && item.review_status === "PENDING").length}</strong></div><p>Machine qualification and saved channel evidence do not equal human approval.</p></div><div class="subsection"><h4>Outreach history · demo records</h4><div id="company-outreach-history" class="history-list">Loading isolated campaign history…</div></div>`;
}
async function loadOutreachHistory(companyId) {
  const host = $("company-outreach-history");
  if (!host) return;
  const result = await api(`/api/companies/${companyId}/outreach-history`);
  host.innerHTML = result.history.length ? result.history.map((item) => `<div class="history-row"><strong>DEMO · ${esc(item.campaign_name)}</strong><br>${esc(item.channel_type || "No channel")} · ${esc(item.delivery_status)} · ${esc(item.response_status || "No response recorded")} · ${esc(item.interest_status || "No interest form recorded")}</div>`).join("") : "<p>No saved campaign history for this company.</p>";
}
async function selectCompany(id) {
  state.selectedId = id;
  state.draft = null; state.delivered = false; state.responseRecorded = false;
  $("draft-preview").classList.add("hidden"); $("simulate-controls").classList.add("hidden"); $("simulation-log").innerHTML = "";
  const company = await api(`/api/companies/${id}`);
  state.detail = company;
  renderDetail(company);
  loadOutreachHistory(id).catch(showError);
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
  const warnings = result.missing_configuration?.length ? `<div class="draft-warnings"><strong>Missing configuration — this is a demo placeholder</strong><ul>${result.missing_configuration.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : "";
  const participation = `<div class="participation-cta"><span>PRIMARY CALL TO ACTION · DEMO PLACEHOLDER ONLY</span><strong>${esc(result.participation_interest_form_url)}</strong><small>Recipients would use this form to express interest. The reserved .invalid link is not a real form.</small></div>`;
  const researchContact = `<div class="draft-meta">Research questions only: ${esc(result.research_contact_email)}</div>`;
  container.innerHTML = `<div class="draft-banner">DEMO DRAFT — NOT SENT</div><div class="draft-heading">${esc(result.subject)}</div><div class="draft-meta">${esc(destination)}<br>Company review: ${esc(result.company_review_status)} · Channel review: ${esc(result.channel_review_status)} · Approval: ${esc(result.approval_status)}<br>Evidence: ${esc(result.evidence_url)}</div>${participation}${researchContact}${warnings}${formNotes}<pre class="draft-body">${esc(result.body)}</pre>`;
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
    addEvent("SIMULATED DECLINED INVITATION", "Fixture action only. No actual company response or human review state is recorded.");
    return;
  }
  addEvent("SIMULATED FORM OPENED", `${state.participationFormUrl || "https://research.example.invalid/participation-interest"}. No external page or network request was opened.`);
  addEvent("SIMULATED INTEREST SUBMITTED", "Demo event only—not a Google Form submission. It records interest in learning more, not research consent or a commitment to participate.");
  const review = document.createElement("button"); review.className = "button outline"; review.textContent = "Review simulated interest submission";
  review.addEventListener("click", () => {
    addEvent("SIMULATED RESEARCH-TEAM REVIEW", "The interest submission is awaiting human review. No participant approval or consent is recorded.");
    review.disabled = true;
    const followup = document.createElement("button"); followup.className = "button outline"; followup.textContent = "Begin simulated follow-up research process";
    followup.addEventListener("click", () => {
      addEvent("SIMULATED FOLLOW-UP PROCESS BEGINS", "The research team would provide study information for a separate decision. No message is sent and consent remains NOT ESTABLISHED.");
      followup.disabled = true;
    });
    $("simulation-log").append(followup);
  });
  $("simulation-log").append(review);
}
function wireFilters() {
  const bindings = [["search","q"],["source","source"],["verification","verification"],["eligibility","eligibility"],["geo","geo"],["priority","priority"],["channel","channel"],["outreach-status","outreach_status"]];
  bindings.forEach(([element,key]) => $(element).addEventListener(element === "search" ? "input" : "change", () => { state.filters[key] = $(element).value; state.page = 1; reloadCompanies().catch(showError); }));
  $("company-prev").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); reloadCompanies().catch(showError); });
  $("company-next").addEventListener("click", () => { state.page += 1; reloadCompanies().catch(showError); });
  $("preview-button").addEventListener("click", () => previewDraft().catch(showError));
  $("deliver-button").addEventListener("click", simulateDelivery);
  $("response-button").addEventListener("click", recordResponse);
  $("create-campaign-button").addEventListener("click", createCampaign);
}
let selectedCampaignId = null;
let campaignMetricFilter = "all";
async function loadCampaignWorkspace() {
  const [campaignResult, analytics] = await Promise.all([api("/api/campaigns"), api("/api/campaign-analytics")]);
  $("campaign-analytics").innerHTML = [
    ["Saved campaigns", analytics.campaigns], ["Company history rows", analytics.company_items],
    ["Drafts", analytics.drafts], ["Contact-form review queue", analytics.form_channel_review_queue],
    ["Simulated delivery events", analytics.simulated_delivery_events], ["Simulated submissions", analytics.simulated_submissions],
    ["Simulated responses", Object.values(analytics.responses).reduce((sum,value)=>sum+value,0)],
    ["Real sends", analytics.real_sends],
  ].map(([label,value],index) => `<button type="button" class="campaign-stat${campaignMetricFilter === ["all","items","drafts","forms","deliveries","submissions","responses","real"][index] ? " selected" : ""}" data-metric="${["all","items","drafts","forms","deliveries","submissions","responses","real"][index]}"><strong>${esc(value)}</strong><span>${esc(label)} · click to filter</span></button>`).join("");
  $("global-stop-button").textContent = analytics.global_emergency_stopped ? "Clear global DEMO stop (campaigns stay paused)" : "Stop all DEMO campaign processing";
  $("global-stop-state").textContent = analytics.global_emergency_stopped ? "Global emergency stop is active. Clearing it will not resume any campaign automatically." : "No live delivery exists. This control pauses only local simulated batches.";
  $("global-stop-button").onclick = () => apiWrite("/api/demo/emergency-stop", {enabled:!analytics.global_emergency_stopped}).then(() => loadCampaignWorkspace()).catch(showError);
  let campaigns = campaignResult.campaigns;
  if (campaignMetricFilter === "drafts") campaigns = campaigns.filter((campaign) => campaign.draft_count > 0);
  if (campaignMetricFilter === "forms") campaigns = campaigns.filter((campaign) => campaign.contact_form_review_count > 0);
  if (campaignMetricFilter === "deliveries") campaigns = campaigns.filter((campaign) => campaign.simulated_deliveries > 0 || campaign.simulated_failures > 0);
  if (campaignMetricFilter === "submissions") campaigns = campaigns.filter((campaign) => campaign.interest_submission_count > 0);
  if (campaignMetricFilter === "responses") campaigns = campaigns.filter((campaign) => campaign.simulated_response_count > 0);
  if (campaignMetricFilter === "real") campaigns = campaigns.filter((campaign) => false);
  $("campaign-list").innerHTML = campaigns.length ? campaigns.map((campaign) => `<div class="campaign-card${campaign.id === selectedCampaignId ? " selected" : ""}"><button type="button" data-campaign="${esc(campaign.id)}"><strong>${esc(campaign.name)}</strong><small>${esc(campaign.status)} · ${campaign.company_count} companies · ${campaign.simulated_deliveries} simulated deliveries</small></button></div>`).join("") : "<p>No saved campaigns match this metric. Real sends remain disabled.</p>";
  document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => { campaignMetricFilter = button.dataset.metric; loadCampaignWorkspace().catch(showError); }));
  document.querySelectorAll("[data-campaign]").forEach((button) => button.addEventListener("click", () => showCampaign(button.dataset.campaign).catch(showError)));
}
async function createCampaign() {
  if (!state.totalCompanies) { showError(new Error("No companies match the filters.")); return; }
  const result = await apiWrite("/api/campaigns", {name:$("campaign-name").value, all_matching:true, filters:state.filters});
  selectedCampaignId = result.id;
  await loadCampaignWorkspace();
  await showCampaign(result.id);
}
function campaignCounts(campaign) {
  const status = campaign.item_counts || {};
  return `<div class="campaign-counts"><span>Selected: ${campaign.company_count}</span><span>Eligible: ${campaign.eligible_count}</span><span>Ineligible: ${campaign.ineligible_count}</span><span>Unresolved: ${campaign.unresolved_count}</span><span>Excluded total: ${campaign.excluded_count}</span><span>Draftable email: ${status.DRAFT_REVIEW || 0}</span><span>Contact-form review: ${status.CONTACT_FORM_REVIEW || 0}</span><span>Unverified excluded: ${status.EXCLUDED_IDENTITY_UNVERIFIED || 0}</span><span>Not eligible excluded: ${status.EXCLUDED_NOT_ELIGIBLE || 0}</span><span>No channel excluded: ${status.EXCLUDED_NO_SUITABLE_CHANNEL || 0}</span><span>Previously campaigned excluded: ${status.EXCLUDED_ALREADY_CAMPAIGNED || 0}</span><span>Simulated delivered: ${campaign.simulated_deliveries}</span><span>Failed attempts (including recovered): ${campaign.simulated_failures}</span></div>`;
}
async function showCampaign(campaignId) {
  selectedCampaignId = campaignId;
  const campaign = await api(`/api/campaigns/${encodeURIComponent(campaignId)}`);
  await loadCampaignWorkspace();
  const active = ["DEMO_APPROVED","RUNNING"].includes(campaign.status);
  const toolbar = `<div class="campaign-toolbar"><strong>${esc(campaign.status)} · DEMO ONLY</strong>${campaign.status === "DRAFT" || campaign.status === "PAUSED" ? `<button class="button primary" data-action="approve">Approve DEMO campaign</button>` : ""}${active ? `<button class="button dark" data-action="batch">Simulate next batch (max 10)</button><button class="button outline" data-action="pause">Pause</button>` : ""}${campaign.status === "PAUSED" ? `<button class="button outline" data-action="resume">Resume</button>` : ""}${!['CANCELLED','COMPLETED'].includes(campaign.status) ? `<button class="button outline" data-action="cancel">Cancel and revoke demo form tokens</button><button class="button outline" data-action="stop">Emergency stop</button>` : ""}</div>`;
  const items = campaign.items.map((item) => {
    const draft = item.draft;
    const preview = draft ? `<details><summary>Inspect ${item.channel_type === "CONTACT_FORM" ? "contact-form draft" : "email draft"} · NOT SENT</summary><p><strong>${esc(draft.recipient || item.form_url || "No recipient")}</strong></p><p><strong>${esc(draft.subject || "Contact-form message")}</strong></p><pre>${esc(draft.body || "")}</pre><p>Participation URL: ${esc(item.form_url)} · token expires ${esc(item.token_expires_at || "")}</p><p>Source contact review remains ${esc(item.contact_review_status)}. Campaign approval is DEMO-only.</p></details>` : "";
    const response = item.delivery_status === "SIMULATED_DELIVERED" ? `<div class="campaign-toolbar"><label>Simulated response<select data-response="${item.company_id}"><option value="INTERESTED">Interested</option><option value="DECLINED">Declined</option><option value="UNANSWERED">No response</option></select></label><button class="button outline" data-record-response="${item.company_id}">Record DEMO response</button></div>` : "";
    const form = item.delivery_status === "SIMULATED_DELIVERED" && item.demo_token ? `<details><summary>Open safe local demo participation form</summary><p>SIMULATED ONLY. Interest is not consent; data stays in the local campaign sidecar. No public form endpoint exists.</p><p>Study description: ${esc(state.formConfiguration.study_description || "Not configured")}<br>Privacy notice: ${esc(state.formConfiguration.privacy_notice || "Not configured")}</p><button class="button outline" data-form-open="${item.company_id}" data-token="${esc(item.demo_token)}">Record SIMULATED form open</button><div class="field-row"><input aria-label="Company name" data-form="company_name" value="${esc(item.company_name)}"><input aria-label="Website" data-form="website" value="${esc(item.website)}"><input aria-label="Contact name" data-form="contact_name" placeholder="Contact name"><input aria-label="Contact role" data-form="contact_role" placeholder="Role"><input aria-label="Business email" data-form="business_email" placeholder="you@company.com"><select aria-label="Interest choice" data-form="interest"><option value="INTERESTED">Interested</option><option value="MORE_INFO">More information</option><option value="NOT_INTERESTED">Not interested</option></select></div><textarea aria-label="Questions" data-form="questions" placeholder="Optional questions"></textarea><button class="button outline" data-submit-interest="${item.company_id}" data-token="${esc(item.demo_token)}">Submit SIMULATED interest</button></details>` : "";
    return `<article class="campaign-item"><div class="campaign-item-head"><strong>${esc(item.company_name)} · #${item.company_id}</strong>${pill(item.delivery_status)}</div><div class="campaign-item-meta">${esc(item.website)} · ${esc(item.channel_type || "No channel")} · ${esc(item.recipient || item.form_url || "")}</div><div class="campaign-item-meta">Queue: ${esc(item.item_status)} · Identity: ${esc(item.identity_status)} · Eligibility: ${esc(item.qualification_status)} · Contact review: ${esc(item.contact_review_status)}</div>${preview}${response}${form}${item.last_error ? `<div class="campaign-event">${esc(item.last_error)}</div>` : ""}</article>`;
  }).join("");
  const submissions = campaign.interest_submissions.map((submission) => `<div class="history-row">SIMULATED · ${esc(submission.interest)} · ${esc(submission.review_status)} · follow-up: ${esc(submission.handoff_status)} · formal consent: NO ${submission.review_status === "PENDING_RESEARCH_TEAM_REVIEW" ? `<button class="button outline" data-submission-review="${submission.id}">Record DEMO review</button>` : ""}${submission.review_status === "DEMO_RESEARCH_TEAM_REVIEWED" && submission.handoff_status === "NOT_STARTED" && submission.interest !== "NOT_INTERESTED" ? `<button class="button outline" data-submission-handoff="${submission.id}">Begin SIMULATED follow-up</button>` : ""}</div>`).join("") || "No simulated form submissions.";
  $("campaign-detail").innerHTML = `<h3>${esc(campaign.name)}</h3>${campaignCounts(campaign)}${toolbar}<p class="campaign-safety">Separate review states: campaign DEMO approval does not approve a company or contact, create real outreach authorization, or establish participant consent. Contact forms remain a manual review queue and are never submitted.</p><div class="campaign-items">${items || "<p>No company items are stored for this campaign.</p>"}</div><details><summary>Campaign audit history</summary>${campaign.audit.map((event) => `<div class="history-row">DEMO · ${esc(event.event_type)} · ${esc(event.created_at)}</div>`).join("") || "No events."}</details><details><summary>Simulated interest submissions</summary>${submissions}</details>`;
  $("campaign-detail").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runCampaignAction(campaignId, button.dataset.action).catch(showError)));
  $("campaign-detail").querySelectorAll("[data-record-response]").forEach((button) => button.addEventListener("click", () => apiWrite(`/api/campaigns/${campaignId}/responses`, {company_id:Number(button.dataset.recordResponse),response:$("campaign-detail").querySelector(`[data-response="${button.dataset.recordResponse}"]`).value}).then(() => showCampaign(campaignId)).catch(showError)));
  $("campaign-detail").querySelectorAll("[data-submit-interest]").forEach((button) => button.addEventListener("click", () => {
    const card = button.closest(".campaign-item"); const values = {};
    card.querySelectorAll("[data-form]").forEach((control) => { values[control.dataset.form] = control.value; });
    apiWrite("/api/demo/interest", {...values,form_token:button.dataset.token}).then((result) => { alert(`${result.status}: saved as SIMULATED and pending research-team review. Formal consent: NO.`); return showCampaign(campaignId); }).catch(showError);
  }));
  $("campaign-detail").querySelectorAll("[data-form-open]").forEach((button) => button.addEventListener("click", () => apiWrite("/api/demo/form-open", {form_token:button.dataset.token}).then((result) => alert(`${result.status}: no external page was opened.`)).catch(showError)));
  $("campaign-detail").querySelectorAll("[data-submission-review]").forEach((button) => button.addEventListener("click", () => apiWrite(`/api/demo/submissions/${button.dataset.submissionReview}/review`, {}).then(() => showCampaign(campaignId)).catch(showError)));
  $("campaign-detail").querySelectorAll("[data-submission-handoff]").forEach((button) => button.addEventListener("click", () => apiWrite(`/api/demo/submissions/${button.dataset.submissionHandoff}/handoff`, {}).then(() => showCampaign(campaignId)).catch(showError)));
}
async function runCampaignAction(campaignId, action) {
  if (action === "approve") await apiWrite(`/api/campaigns/${campaignId}/approve`, {demo_only:true});
  if (action === "batch") await apiWrite(`/api/campaigns/${campaignId}/simulate-batch`, {limit:10});
  if (action === "pause") await apiWrite(`/api/campaigns/${campaignId}/status`, {status:"PAUSED"});
  if (action === "resume") await apiWrite(`/api/campaigns/${campaignId}/status`, {status:"RESUMED"});
  if (action === "cancel") await apiWrite(`/api/campaigns/${campaignId}/status`, {status:"CANCELLED"});
  if (action === "stop") await apiWrite(`/api/campaigns/${campaignId}/emergency-stop`, {demo_only:true});
  await loadCampaignWorkspace(); await showCampaign(campaignId);
}
function showError(error) { $("result-count").textContent = "DATA LOAD ERROR"; $("result-count").title = error.message; }
async function start() {
  wireFilters();
  const result = await api("/api/summary");
  state.summary = result.summary; state.participationFormUrl = result.simulated_signup_url || "https://research.example.invalid/participation-interest";
  state.formConfiguration = result.participation_form_configuration || {};
  renderSummary({...result.summary, filters: result.filters});
  await reloadCompanies();
  await loadCampaignWorkspace();
}
start().catch(showError);
