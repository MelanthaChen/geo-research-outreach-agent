const state = { filters: {}, lastFilterKey: "", companies: [], totalCompanies: 0, page: 1, pageSize: 50, candidatePage: 1, candidatePageSize: 25, candidateFilters: {}, phase6gPage:1, phase6gPageSize:20, phase6gFilters:{}, selectedId: null, selectedIds: new Set(), detail: null, summary: null, campaignDetails: {}, campaignSummaries: [], participationFormUrl: "", formConfiguration: {}, emailConfiguration: null, testEmailPreview: null, draft: null, delivered: false, responseRecorded: false, responsePage: 1, responsePageSize: 25, reviewToken: "", dashboardToken: "" };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const compact = (value) => String(value || "UNKNOWN").replaceAll("_", " ");
function displayCampaignName(value) {
  const name = String(value || "Research Outreach Campaign").replace(/\s*[—–-]\s*DEMO(?:\s+ONLY)?$/i, "").trim();
  return /^Professor Walkthrough$/i.test(name) || !name ? "Research Outreach Campaign" : name;
}
function displayStatus(value) {
  return compact(value).replace(/^SIMULATED\s+/i, "").replace(/^DEMO\s+/i, "").replaceAll("TEST ", "");
}
const websiteDomain = (value) => {
  try { return new URL(value).hostname.replace(/^www\./i, ""); }
  catch { return String(value || "Website not recorded"); }
};
const pillClass = (value) => ["VERIFIED","ELIGIBLE","HIGH","APPROVED","HAS_SUITABLE_CHANNEL"].includes(String(value).toUpperCase()) ? "good" : ["UNKNOWN","NEEDS REVIEW","NEEDS_REVIEW","PENDING","BASELINE_NOT_RECHECKED","MEDIUM"].includes(String(value).toUpperCase()) ? "warn" : ["REJECTED_MISMATCH","REJECTED","LOW","NO_SUITABLE_CHANNEL"].includes(String(value).toUpperCase()) ? "bad" : "neutral";

function authHeaders(headers={}) {
  const result = {...headers};
  if (state.dashboardToken) result.Authorization = `Bearer ${state.dashboardToken}`;
  return result;
}
function showAuthPrompt(message="This action requires protected operator access.") {
  const host=$("app-error");
  if (host) { host.hidden=false; host.textContent=`Protected operator action: ${message}`; }
}
async function publicApi(path) {
  const response = await fetch(path, {headers:{"Accept":"application/json"}, cache:"no-store"});
  if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
  return response.json();
}
async function api(path) {
  const response = await fetch(path, {headers: authHeaders({"Accept":"application/json"}), cache:"no-store"});
  if (response.status === 401) showAuthPrompt();
  if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
  return response.json();
}
async function apiWrite(path, payload) {
  const headers=authHeaders({"Accept":"application/json","Content-Type":"application/json"});
  if(path==="/api/research/consent-review" || path.startsWith("/api/hotel-discovery/")) { if(state.reviewToken) headers.Authorization=`Bearer ${state.reviewToken}`; }
  const response = await fetch(path, {method:"POST", headers, body:JSON.stringify(payload), cache:"no-store"});
  const result = await response.json();
  if (response.status === 401) showAuthPrompt();
  if (!response.ok) throw new Error(result.error || `Dashboard request failed (${response.status})`);
  return result;
}
async function apiDownload(path, payload) {
  const response = await fetch(path, {method:"POST", headers:authHeaders({"Accept":"text/csv, application/json","Content-Type":"application/json"}), body:JSON.stringify(payload), cache:"no-store"});
  if (response.status === 401) showAuthPrompt();
  if (!response.ok) { const result=await response.json(); const error=new Error(result.error || `Export failed (${response.status})`); error.exportResult=result; throw error; }
  const blob=await response.blob();
  const filename=response.headers.get("Content-Disposition")?.match(/filename="?([^";]+)"?/)?.[1] || "mail-merge.csv";
  const link=document.createElement("a"); link.href=URL.createObjectURL(blob); link.download=filename; link.click(); URL.revokeObjectURL(link.href);
  return {filename};
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
  const pool = summary.candidate_pool || {};
  const channelClasses = summary.contact_channel_classes || {};
  const scale = summary.phase6h || {};
  const smb = summary.phase7_smb_target || {};
  const hotelPool = summary.hotel_candidate_pool || {};
  const hotelJob = summary.hotel_discovery || {};
  const metrics = [
    ["Total candidates", pool.total_candidates ?? summary.total_companies, "existing cohort + separate discovery pool"],
    ["Verified companies", pool.verified_companies ?? summary.website_verification.VERIFIED ?? 0, "based on identity evidence"],
    ["GEO-eligible companies", pool.geo_eligible_companies ?? summary.eligibility.ELIGIBLE ?? 0, "existing qualification rules"],
    ["Companies with suitable contact channels", pool.companies_with_email_contacts ?? 0, "saved first-party email or form"],
    ["Outreach-ready companies", pool.outreach_ready_companies ?? 0, "human approval and readiness required"],
  ];
  $("metrics").innerHTML = metrics.map(([label,value,note]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join("");
  $("secondary-metrics").innerHTML = [
    ["Email only", channelClasses.EMAIL_ONLY || 0],
    ["Contact form only", channelClasses.FORM_ONLY || 0],
    ["Email and form", channelClasses.EMAIL_AND_FORM || 0],
    ["Channel evidence needs review", channelClasses.NEEDS_REVIEW || 0],
    ["No contact channel", channelClasses.NO_CONTACT_CHANNEL || 0],
    ["Companies awaiting human review", reviews.companies],
    ["Suitable channels awaiting review", reviews.contact_channels],
    ["Test deliveries", outreach.simulated_delivery_events],
    ["Live delivery", "Unavailable"],
    ["Test responses", outreach.simulated_response_events],
    ["Test research handoffs", outreach.simulated_handoff_events],
    ["Live outreach records", outreach.real_outreach_records],
  ].map(([label,value]) => `<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  $("candidate-scale-metrics").innerHTML = [
    ["Total candidate pool", pool.total_candidates ?? "—"],
    ["Priority shortlist records", scale.shortlist ?? 0],
    ["Processed in shortlist", scale.processed ?? 0],
    ["Verified identities", scale.verified ?? 0],
    ["GEO eligible", scale.geo_eligible ?? 0],
    ["Companies with business emails", scale.companies_with_business_emails ?? 0],
    ["Companies with confirmed forms", scale.companies_with_contact_forms ?? 0],
    ["Pending contact reviews", scale.pending_contact_reviews ?? 0],
    ["Outreach ready", scale.outreach_ready ?? 0],
    ["Hotel property candidates (separate pool)", hotelPool.properties ?? 0],
    ["Hotel candidates processed", hotelPool.processed ?? 0],
    ["Hotel identities verified", hotelPool.verified ?? 0],
    ["GEO-eligible hotels", hotelPool.geo_eligible ?? 0],
    ["Hotels with business emails", hotelPool.with_business_email ?? 0],
    ["Hotels with confirmed contact forms", hotelPool.with_confirmed_form ?? 0],
    ["Hotel properties awaiting review", hotelPool.pending_review ?? 0],
  ].map(([label,value]) => `<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(Number(value).toLocaleString())}</strong></div>`).join("");
  const smbChannels = smb.contact_channel_breakdown_contactable_smb || {};
  const allChannels = smb.contact_channel_breakdown_contactable || {};
  $("phase7-smb-metrics").innerHTML = [
    ["Phase 7 company records", smb.phase7_company_records ?? 0],
    ["Strict contactable companies · all sectors", smb.strict_contactable_companies ?? 0],
    ["Positive size-evidence records", smb.smb_size_supported_records ?? 0],
    ["Verified SMB candidates", smb.verified_smb_count ?? 0],
    ["GEO-eligible SMBs", smb.geo_eligible_smb_count ?? 0],
    ["Contactable SMBs", smb.contactable_smb_count ?? 0],
    ["Remaining to 5,000", smb.remaining_gap_to_5000 ?? 5000],
    ["Contactable records with unknown size", smb.contactable_unknown_size_nonhotel ?? 0],
    ["Hotel-tagged contacts excluded", smb.hotel_tagged_contactable_excluded ?? 0],
    ["Potential hotel/business domain overlaps · review only", smb.hotel_cross_cohort_domain_overlap?.hotel_records_sharing_business_domain ?? 0],
    ["Large-enterprise size evidence", smb.large_enterprise_supported_records ?? 0],
    ["SMB email only", smbChannels.EMAIL_ONLY ?? 0],
    ["SMB form only", smbChannels.FORM_ONLY ?? 0],
    ["SMB email and form", smbChannels.EMAIL_AND_FORM ?? 0],
    ["All contactable · email only", allChannels.EMAIL_ONLY ?? 0],
    ["All contactable · form only", allChannels.FORM_ONLY ?? 0],
    ["All contactable · email and form", allChannels.EMAIL_AND_FORM ?? 0],
    ["No strict usable channel · full pool", (smb.contact_channel_breakdown_all_candidates||{}).NO_CONTACT ?? 0],
  ].map(([label,value]) => `<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(Number(value).toLocaleString())}</strong></div>`).join("");
  $("hotel-candidate-metrics").innerHTML = [
    ["Hotel property candidates", hotelPool.properties ?? 0],
    ["Actually inspected", hotelPool.processed ?? 0],
    ["Identity verified", hotelPool.verified ?? 0],
    ["GEO eligible", hotelPool.geo_eligible ?? 0],
    ["With public business email", hotelPool.with_business_email ?? 0],
    ["With confirmed contact form", hotelPool.with_confirmed_form ?? 0],
    ["Awaiting human review", hotelPool.pending_review ?? 0],
  ].map(([label,value]) => `<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(Number(value).toLocaleString())}</strong></div>`).join("");
  const jobStatus = hotelJob.status || "NOT_INITIALIZED";
  $("hotel-discovery-metrics").innerHTML = [
    ["Qualified prospects", hotelJob.qualified_prospects ?? 0],
    ["Target", hotelJob.target_count ?? 1000],
    ["Remaining", hotelJob.remaining ?? 1000],
    ["Raw rows screened", hotelJob.scanned_count ?? 0],
    ["Promoted this job", hotelJob.promoted_count ?? 0],
    ["Rejected / duplicate", Number(hotelJob.rejected_count ?? 0) + Number(hotelJob.duplicate_count ?? 0)],
    ["Last successful run", hotelJob.last_success_at || "Not run"],
    ["Job status", jobStatus],
  ].map(([label,value]) => `<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  $("hotel-discovery-status").textContent = hotelJob.last_error ? `Last error: ${hotelJob.last_error}` : `Status: ${jobStatus}. Discovery is disabled by default and never sends outreach or starts GEO research.`;
  $("hotel-discovery-pause").disabled = ["PAUSED","TARGET_REACHED","NOT_INITIALIZED"].includes(jobStatus);
  $("hotel-discovery-resume").disabled = jobStatus !== "PAUSED";
  const distributionHTML = [
    distCard("WEBSITE IDENTITY", summary.website_verification, summary.total_companies),
    distCard("ELIGIBILITY", summary.eligibility, summary.total_companies),
    distCard("GEO OPPORTUNITY", summary.geo_opportunity, summary.total_companies),
    distCard("RESEARCH PRIORITY", summary.research_priority, summary.total_companies),
    distCard("CONTACT-CHANNEL COVERAGE", summary.contact_channel_coverage, summary.total_companies),
  ].join("");
  $("distributions").innerHTML = distributionHTML;
  $("overview-distributions").innerHTML = [
    distCard("ELIGIBILITY", summary.eligibility, summary.total_companies),
    distCard("GEO OPPORTUNITY", summary.geo_opportunity, summary.total_companies),
  ].join("");
  setOptions("source", summary.filters.sources);
  setOptions("verification", summary.filters.website_verification);
  setOptions("eligibility", summary.filters.eligibility);
  setOptions("geo", summary.filters.geo_opportunity);
  setOptions("priority", summary.filters.research_priority);
  renderSettings(state.formConfiguration);
  const requiredFormConfig=["study_description","privacy_notice","research_contact_email","participation_interest_form_url"];
  const missingFormConfig=requiredFormConfig.filter((key)=>!state.formConfiguration[key]||state.formConfiguration[key]==="Not configured");
  $("form-config-status").textContent = `${missingFormConfig.length ? `${missingFormConfig.length} form configuration item(s) incomplete` : "Form configuration complete"}. Study description: ${state.formConfiguration.study_description || "Not configured"} · Research contact: ${state.formConfiguration.research_contact_email || "Not configured"}`;
}
function candidateQuery() {
  const query = new URLSearchParams({page:String(state.candidatePage),page_size:String(state.candidatePageSize)});
  Object.entries(state.candidateFilters).forEach(([key,value])=>{if(value)query.set(key,value);});
  return query;
}
async function reloadCandidates() {
  const result = await api(`/api/candidates?${candidateQuery()}`);
  const p=result.summary||{};
  const screened=Object.values(p.screen_statuses||{}).reduce((sum,count)=>sum+Number(count||0),0);
  const sixf=result.phase6f||{};
  const sixfProgress=sixf.selected?` · Phase 6F ${sixf.processed}/${sixf.selected} processed · ${sixf.verified} verified · ${sixf.geo_eligible} GEO eligible · ${sixf.contact_channels_found} channels · ${sixf.pending_verification} pending · ${sixf.review_required} review`:"";
  const sixh=result.phase6h||{};
  $("candidate-total").textContent=`${Number(p.candidate_count||0).toLocaleString()} candidates · 5,000 prioritized: ${Number(sixh.processed||0).toLocaleString()} processed, ${Number(sixh.remaining||0).toLocaleString()} pending · ${Number(sixh.verified||0).toLocaleString()} verified · ${Number(sixh.geo_eligible||0).toLocaleString()} GEO eligible`;
  setOptions("candidate-source", result.filters?.sources||[]);
  setOptions("candidate-status", result.filters?.statuses||[]);
  setOptions("candidate-screen-status", result.filters?.screen_statuses||[]);
  $("candidate-list").innerHTML=result.candidates.length?result.candidates.map(row=>`<article class="candidate-row"><div class="candidate-main"><strong>${esc(row.company_name)}</strong><span>${esc(row.canonical_domain)}</span></div><div class="candidate-badges"><span class="status-pill">${esc(compact(row.screen_status))}</span><span class="status-pill ${pillClass(row.verification_status)}">Identity ${esc(compact(row.verification_status))}</span><span class="candidate-source-label">${esc(row.category_group)} · ${esc(compact(row.chain_location_concern))} · ${esc(compact(row.website_domain_concern))}</span></div><a class="candidate-website" href="${esc(row.website)}" target="_blank" rel="noreferrer">Website ↗</a></article>`).join(""):`<p class="empty-state">No candidates match these filters.</p>`;
  const maxPage=Math.max(1,Math.ceil(result.total/result.page_size));
  $("candidate-page").textContent=`Page ${result.page} of ${maxPage} · ${result.total.toLocaleString()} matching records`;
  $("candidate-prev").disabled=result.page<=1;
  $("candidate-next").disabled=result.page>=maxPage;
  await reloadPhase6gReview();
}
function phase6gQuery(){const query=new URLSearchParams({page:String(state.phase6gPage),page_size:String(state.phase6gPageSize)});Object.entries(state.phase6gFilters).forEach(([key,value])=>{if(value)query.set(key,value);});return query;}
async function reloadPhase6gReview(){
  const result=await api(`/api/phase6g/review?${phase6gQuery()}`);
  const currentSource=$("phase6g-source").value;setOptions("phase6g-source",result.sources||[]);if((result.sources||[]).includes(currentSource))$("phase6g-source").value=currentSource;
  const filterKey=new URLSearchParams(Object.entries(state.phase6gFilters).filter(([,v])=>v)).toString();
  $("phase6g-export").href=`/api/phase6g/review/export.csv?${filterKey}`;
  $("phase6g-review-total").textContent=`${Number(result.companies||0).toLocaleString()} companies · ${Number(result.total||0).toLocaleString()} channels`;
  $("phase6g-review-list").innerHTML=result.rows.length?result.rows.map(row=>{
    const destination=row.email?`<a href="mailto:${esc(row.email)}">${esc(row.email)}</a>`:`<a href="${esc(row.channel_url)}" target="_blank" rel="noreferrer">${esc(websiteDomain(row.channel_url))} · inspect evidence ↗</a>`;
    const proof=String(row.evidence||row.purpose||"No captured channel excerpt.").slice(0,1600);
    const identity=row.verification_status==="VERIFIED"?"Company identity verified":"Company identity needs review";
    const approveLabel=row.channel_classification==="OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD"?"Confirm official contact page":"Approve channel evidence";
    const channelActions=row.channel_classification.startsWith("INVALID")?`<span class="status-pill bad">Unsupported · approval blocked</span><button class="button outline" data-contact-review="REJECTED" data-candidate="${esc(row.candidate_id)}" data-channel="${esc(row.channel_key)}">Reject evidence</button>`:`<button class="button outline" data-contact-review="APPROVED" data-candidate="${esc(row.candidate_id)}" data-channel="${esc(row.channel_key)}">${approveLabel}</button><button class="button outline" data-contact-review="REJECTED" data-candidate="${esc(row.candidate_id)}" data-channel="${esc(row.channel_key)}">Reject evidence</button>`;
    return `<article class="candidate-row review-contact-row"><div class="candidate-main"><strong>${esc(row.company_name)}</strong><span>${esc(row.canonical_domain)} · ${esc(row.source_name)}</span><span>${esc(identity)} · GEO ${esc(compact(row.geo_eligibility_status))} / ${esc(compact(row.geo_opportunity_status))}</span></div><div class="candidate-badges"><span class="status-pill ${pillClass(row.confidence)}">${esc(row.confidence)} confidence</span><span class="status-pill">${esc(compact(row.channel_classification))}</span><span class="status-pill ${pillClass(row.review_status)}">Channel ${esc(compact(row.review_status))}</span><span class="status-pill ${pillClass(row.company_review_status)}">Company ${esc(compact(row.company_review_status))}</span></div><div class="review-contact-destination">${destination}<small>${esc(row.purpose_suitability)} · Evidence ${esc(row.evidence_freshness||"unknown")}</small></div><details class="review-evidence"><summary>Evidence, provenance & review controls</summary><p><strong>Company source:</strong> <a href="${esc(row.source_url)}" target="_blank" rel="noreferrer">${esc(row.source_name)} record ↗</a> · <strong>Contact evidence:</strong> <a href="${esc(row.evidence_url)}" target="_blank" rel="noreferrer">${esc(row.evidence_url)} ↗</a></p><p><strong>Identity-page evidence:</strong> ${esc(row.identity_evidence||"No saved page metadata.")} · <strong>Qualification rationale:</strong> ${esc(row.qualification_evidence||"Not available in saved record.")}</p><p><strong>First party:</strong> ${esc(row.first_party_provenance)} · <strong>Identity:</strong> ${esc(row.company_identity_match)} · <strong>Contact excerpt:</strong> ${esc(proof)}</p><p><strong>All source excerpts for this deduplicated channel:</strong> ${esc(row.duplicate_evidence||"[]")}</p><p><strong>Assessment:</strong> ${esc(row.confidence_reasons)} · <strong>Repeated source entries merged:</strong> ${Number(row.duplicate_count||0)}</p><label class="review-note-label">Reviewer note<textarea data-review-note maxlength="1000" placeholder="Optional note about the evidence or follow-up">${esc(row.review_reason||row.company_review_note||"")}</textarea></label><div class="review-actions">${channelActions}<button class="button outline" data-company-review="FURTHER_REVIEW" data-candidate="${esc(row.candidate_id)}">Mark company for further review</button><span class="section-help">Channel decisions never approve outreach.</span></div><p class="inline-status">${esc(row.review_reason||row.company_review_note||"No reviewer note saved.")}</p></details></article>`;
  }).join(""):`<p class="empty-state">No contact channels match these review filters.</p>`;
  const maxPage=Math.max(1,Math.ceil(result.total/result.page_size));
  $("phase6g-page").textContent=`Page ${result.page} of ${maxPage} · ${Number(result.total||0).toLocaleString()} matching channels`;
  $("phase6g-prev").disabled=result.page<=1;$("phase6g-next").disabled=result.page>=maxPage;
  $("phase6g-review-list").querySelectorAll("[data-contact-review]").forEach(button=>button.addEventListener("click",async()=>{const note=button.closest(".review-evidence").querySelector("[data-review-note]").value;await apiWrite("/api/phase6g/review/channel",{candidate_id:button.dataset.candidate,channel_key:button.dataset.channel,decision:button.dataset.contactReview,note});await reloadPhase6gReview();}));
  $("phase6g-review-list").querySelectorAll("[data-company-review]").forEach(button=>button.addEventListener("click",async()=>{const note=button.closest(".review-evidence").querySelector("[data-review-note]").value;await apiWrite("/api/phase6g/review/company",{candidate_id:button.dataset.candidate,review_status:button.dataset.companyReview,note});await reloadPhase6gReview();}));
}
function renderSettings(config) {
  const fields = [["Researcher name",config.researcher_name],["Research team",config.research_team],["University affiliation",config.university_affiliation],["Research contact email",config.research_contact_email],["Sender name",config.sender_name],["Sender email",config.sender_email],["Unified interest form URL",config.participation_interest_form_url],["Delivery mode",config.delivery_mode || "TEST"],["Sending limit",config.daily_limit],["Reply-to",config.reply_to]];
  $("settings-grid").innerHTML = fields.map(([label,value]) => `<div class="setting-item"><span>${esc(label)}</span><strong>${esc(value || "Not configured")}</strong></div>`).join("");
  renderTestEmailSettings();
}
function renderTestEmailSettings(){
  const config=state.emailConfiguration;
  if(!config)return;
  const formState=config.participation_form_ready?config.participation_form_url:(config.participation_form_url_valid?"Configured · not verified":"Not configured");
  const entries=[["Preferred sender",config.preferred_sender],["Authorized test recipient 1",config.authorized_test_recipients[0]],["Authorized test recipient 2",config.authorized_test_recipients[1]],["Provider",config.provider],["Authenticated account",config.authenticated_sender||"Not connected"],["Public HTTPS form",formState],["Send status",config.send_enabled?"Ready for explicit confirmation":"Disabled"]];
  $("email-test-settings").innerHTML=entries.map(([label,value])=>`<div class="setting-item"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  $("email-settings-prerequisite").textContent=config.prerequisite;
  $("gmail-connection-status").textContent=config.authenticated?`Connected as ${config.authenticated_sender}.`:config.oauth_prerequisite;
  $("connect-gmail").disabled=!config.oauth_client_configured;
  $("connect-gmail").textContent=config.authenticated?"Reconnect Gmail":"Connect Gmail";
  $("gmail-campaign-status").textContent=config.authenticated?`Gmail connected · ${config.authenticated_sender}`:"Gmail not connected";
  const select=$("test-email-recipient");
  const selected=select.value;
  select.replaceChildren(...config.authorized_test_recipients.map((address)=>new Option(address,address)));
  if(config.authorized_test_recipients.includes(selected))select.value=selected;
  $("test-email-prerequisite").textContent=config.prerequisite;
  updateTestEmailSendButton();
  loadTestEmailPreview().catch(showError);
  loadTestEmailEvents().catch(showError);
}
async function loadSenderAccounts(){
  const result=await api("/api/sender-accounts");
  $("sender-accounts").innerHTML=(result.accounts||[]).map(account=>`<div class="setting-item"><span>${esc(account.email)} · ${esc(account.provider)}${Number(account.is_default)?" · DEFAULT":""}</span><strong>${esc(account.connection_status)} · Reply sync ${esc(account.reply_sync_status)} <button class="button outline" data-sender-action="${esc(account.account_id)}" data-action="connect">Connect</button>${account.connection_status==="CONNECTED"?`<button class="button outline" data-sender-action="${esc(account.account_id)}" data-action="set-default">Set default</button>`:""}<button class="button outline" data-sender-action="${esc(account.account_id)}" data-action="disconnect">Disconnect</button></strong></div>`).join("")||"<p class='section-help'>No recruitment sender accounts configured.</p>";
  $("sender-accounts").querySelectorAll("[data-sender-action]").forEach(button=>button.addEventListener("click",async()=>{try{const result=await apiWrite(`/api/sender-accounts/${encodeURIComponent(button.dataset.senderAction)}`,{action:button.dataset.action});$("sender-account-status").textContent=result.message||result.status||"Saved";await loadSenderAccounts();}catch(error){$("sender-account-status").textContent=error.message;}}));
}
async function loadTestEmailPreview(){
  const recipient=$("test-email-recipient").value;
  if(!recipient)return;
  const preview=await api(`/api/test-email/preview?recipient=${encodeURIComponent(recipient)}`);
  state.testEmailPreview=preview;
  $("test-email-preview").innerHTML=`<div class="draft-banner">${esc(preview.status)}</div><div class="draft-meta"><strong>From:</strong> ${esc(preview.from)}<br><strong>To:</strong> ${esc(preview.to)}<br><strong>Subject:</strong> ${esc(preview.subject)}<br><strong>Participation form:</strong> ${esc(preview.participation_form_url)}</div><pre class="draft-body">${esc(preview.body)}</pre>`;
  $("test-email-status").textContent="Preview only. Dispatch requires explicit human action.";
  $("test-email-confirm").checked=false;
  updateTestEmailSendButton();
}
function updateTestEmailSendButton(){
  $("send-test-email").disabled=!state.emailConfiguration?.send_enabled||!state.testEmailPreview||!$("test-email-confirm").checked;
}
async function loadTestEmailEvents(){
  const result=await api("/api/test-email/events");
  $("test-email-events").innerHTML=result.events.length?result.events.map((item)=>`<div class="activity-row"><div><strong>${esc(item.recipient)} · ${esc(compact(item.status))}</strong><small>${esc(item.created_at)} · ${esc(item.provider_message_id||item.error||"No provider message ID")}</small></div><span class="test-label">API acceptance is not inbox confirmation</span></div>`).join(""):`<div class="response-empty">No Gmail test-send events recorded.</div>`;
}
function pill(value) { return `<span class="pill ${pillClass(value)}">${esc(displayStatus(value))}</span>`; }
function renderCompanies(rows) {
  state.companies = rows;
  $("result-count").textContent = `${state.totalCompanies} MATCHING COMPANIES`;
  const first = state.totalCompanies ? (state.page - 1) * state.pageSize + 1 : 0;
  const last = Math.min(state.totalCompanies, state.page * state.pageSize);
  $("company-page").textContent = state.totalCompanies ? `Showing ${first}–${last} of ${state.totalCompanies} · page ${state.page}` : "No matching companies";
  $("company-prev").disabled = state.page <= 1;
  $("company-next").disabled = last >= state.totalCompanies;
  $("company-rows").innerHTML = rows.map((row) => `<div role="option" data-option="${row.id}" aria-selected="${row.id === state.selectedId}" class="company-row${row.id === state.selectedId ? " selected" : ""}"><input type="checkbox" aria-label="Select ${esc(row.company_name)} for a campaign" data-select-id="${row.id}" ${state.selectedIds.has(row.id) ? "checked" : ""}><button type="button" data-id="${row.id}" class="company-choice"><span class="company-main"><span class="company-name">${esc(row.company_name)}</span><span class="company-url">${esc(websiteDomain(row.website))}</span></span><span class="company-status"><span class="company-status-label">Priority</span>${pill(row.research_priority)}<span class="company-status-label">Website</span>${pill(row.identity_verification_status)}</span></button></div>`).join("") || `<div class="no-results" role="status">No companies match the selected filters.</div>`;
  document.querySelectorAll("#company-rows [data-id]").forEach((row) => row.addEventListener("click", () => selectCompany(Number(row.dataset.id))));
  document.querySelectorAll("#company-rows [data-select-id]").forEach((box) => box.addEventListener("change", () => { const id=Number(box.dataset.selectId); if(box.checked)state.selectedIds.add(id);else state.selectedIds.delete(id); updateSelectedCount(); }));
  updateSelectedCount();
}
function updateSelectedCount(){const host=$("selected-count");if(host)host.textContent=`${state.selectedIds.size} selected`;}
function downloadCompanyCsv(filtered){
  const query=new URLSearchParams();
  if(filtered) Object.entries(state.filters).forEach(([key,value])=>{if(value)query.set(key,value);});
  window.location.href=`/api/companies/export.csv${query.size?`?${query.toString()}`:""}`;
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
  $("company-detail").innerHTML = `<div class="detail-title"><div><h2>${esc(company.company_name)}</h2><div class="detail-url">${esc(company.website)}</div></div>${pill(company.identity_verification_status)}</div><div class="detail-grid"><div class="detail-stat"><span>Eligibility</span><strong>${esc(compact(company.eligibility || "UNKNOWN"))}</strong></div><div class="detail-stat"><span>GEO opportunity</span><strong>${esc(compact(company.geo_opportunity || "UNKNOWN"))}</strong></div><div class="detail-stat"><span>Research priority</span><strong>${esc(compact(company.priority_tier || "NEEDS_REVIEW"))} · ${esc(company.priority_score ?? "n/a")}</strong></div><div class="detail-stat"><span>Company review</span><strong>${esc(company.review_status || "PENDING")}</strong></div></div><div class="subsection"><h4>Qualification reasons</h4>${listItems(company.qualification_reasons, "No qualification reason recorded.")}</div><div class="subsection"><h4>GEO opportunity rationale</h4>${listItems(company.geo_reasons, "No GEO rationale recorded.")}</div><div class="subsection"><h4>Recorded evidence</h4><div>${evidence}</div></div><div class="subsection"><h4>Missing evidence</h4>${listItems(company.missing_evidence, "No missing evidence identified by this view.")}</div><div class="subsection"><h4>Discovery provenance</h4>${sources}</div><div class="subsection"><h4>Contact channels</h4>${contacts}</div><div class="subsection"><h4>Human review state</h4><div class="review-line"><span>Company</span>${pill(company.review_status || "PENDING")}</div><div class="review-line"><span>Suitable channels pending review</span><strong>${company.contact_channels.filter((item)=>item.is_suitable_first_party && item.review_status === "PENDING").length}</strong></div><p>Machine qualification and saved channel evidence do not equal human approval.</p></div><div class="subsection"><h4>Outreach history</h4><div id="company-outreach-history" class="history-list">Loading campaign history…</div></div>`;
}
async function loadOutreachHistory(companyId) {
  const host = $("company-outreach-history");
  if (!host) return;
  const result = await api(`/api/companies/${companyId}/outreach-history`);
  host.innerHTML = result.history.length ? result.history.map((item) => `<div class="history-row"><strong><span class="test-label">Test</span> · ${esc(displayCampaignName(item.campaign_name))}</strong><br>${esc(item.channel_type || "No channel")} · ${esc(displayStatus(item.delivery_status))} · ${esc(displayStatus(item.response_status || "No response recorded"))} · ${esc(displayStatus(item.interest_status || "No interest form recorded"))}</div>`).join("") : "<p>No saved campaign history for this company.</p>";
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
  await reloadCandidates();
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
  const warnings = result.missing_configuration?.length ? `<div class="draft-warnings"><strong>Configuration incomplete — test values are shown</strong><ul>${result.missing_configuration.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : "";
  const participation = `<div class="participation-cta"><span>PARTICIPATION INTEREST FORM · TEST LINK</span><strong>${esc(result.participation_interest_form_url)}</strong><small>Interest is not a commitment. The reserved .invalid link is not active.</small></div>`;
  const researchContact = `<div class="draft-meta">Research questions only: ${esc(result.research_contact_email)}</div>`;
  container.innerHTML = `<div class="draft-banner">DRAFT · NOT SENT <span class="test-label">Test</span></div><div class="draft-heading">${esc(result.subject)}</div><div class="draft-meta">${esc(destination)}<br>Company review: ${esc(result.company_review_status)} · Channel review: ${esc(result.channel_review_status)} · Approval: ${esc(result.approval_status)}<br>Evidence: ${esc(result.evidence_url)}</div>${participation}${researchContact}${warnings}${formNotes}<pre class="draft-body">${esc(result.body)}</pre>`;
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
  addEvent("Test delivery recorded", `Local test event for ${state.draft.company_name}. No provider or network call occurred; no message was sent.`);
  $("response-controls").classList.remove("hidden");
}
function recordResponse() {
  if (!state.delivered || state.responseRecorded) return;
  state.responseRecorded = true;
  const response = $("response-select").value;
  if (response === "UNANSWERED") {
    addEvent("Test outcome · no response", "No response was recorded in this test. No follow-up is scheduled.");
    return;
  }
  if (response === "DECLINED") {
    addEvent("Test outcome · declined", "Test record only. No company response or human review state is recorded.");
    return;
  }
  if (response === "OPTED_OUT") {
    addEvent("Test outcome · opted out", "Test record only. The suppression registry and company records were not changed.");
    return;
  }
  if (response === "MORE_INFO") {
    addEvent("Test outcome · more information", "Test recipient requested more information. No company response or follow-up message exists.");
    return;
  }
  addEvent("Test form opened", `${state.participationFormUrl || "https://research.example.invalid/participation-interest"}. No external page or network request was opened.`);
  addEvent("Test interest submitted", "Local test record only—not an external form submission. Interest is not research consent or a commitment to participate.");
  const review = document.createElement("button"); review.className = "button outline"; review.textContent = "Review test interest";
  review.addEventListener("click", () => {
    addEvent("Test review recorded", "The interest record is awaiting human review. No participant approval or consent is recorded.");
    review.disabled = true;
    const followup = document.createElement("button"); followup.className = "button outline"; followup.textContent = "Continue test handoff";
    followup.addEventListener("click", () => {
      addEvent("Test handoff recorded", "The research team may provide study information for a separate decision. No message is sent and consent remains unestablished.");
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
  $("select-all-matching").addEventListener("change", async (event)=>{if(!event.target.checked){state.selectedIds.clear();await reloadCompanies();return;}const query=new URLSearchParams(state.filters);const result=await api(`/api/company-ids?${query.toString()}`);result.company_ids.forEach((id)=>state.selectedIds.add(id));await reloadCompanies();});
  $("add-selected-link").addEventListener("click",()=>{if(state.selectedIds.size){const name=$("campaign-name");if(name)name.value=`Selected companies · ${state.selectedIds.size}`;}});
  $("form-token-select").addEventListener("change",()=>loadSelectedToken().catch(showError));
  $("submit-local-interest").addEventListener("click",()=>submitLocalInterest().catch(showError));
  $("preview-mail-merge").addEventListener("click",()=>previewMailMerge().catch(showMailMergeError));
  $("export-test-mail-merge").addEventListener("click",()=>exportTestMailMerge().catch(showMailMergeError));
  $("export-selected-mail-merge").addEventListener("click",()=>exportSelectedMailMerge().catch(showMailMergeError));
}
let selectedCampaignId = null;
let campaignMetricFilter = "all";
async function loadCampaignWorkspace() {
  const [campaignResult, analytics] = await Promise.all([api("/api/campaigns"), api("/api/campaign-analytics")]);
  const campaignToolbar = document.querySelector("#campaign-manager .campaign-toolbar");
  if (campaignToolbar && !$("export-form-review-queue")) {
    campaignToolbar.insertAdjacentHTML("beforeend", '<a class="button outline" id="export-form-review-queue" href="/api/campaigns/form-review-queue.csv">Export manual form-review queue</a>');
  }
  state.campaignSummaries=campaignResult.campaigns;
  const mergeSelect=$("mail-merge-campaign");
  const priorMergeCampaign=mergeSelect.value || selectedCampaignId || "";
  mergeSelect.replaceChildren(new Option("Select a campaign",""), ...campaignResult.campaigns.map((campaign)=>new Option(displayCampaignName(campaign.name),campaign.id)));
  if(campaignResult.campaigns.some((campaign)=>campaign.id===priorMergeCampaign)) mergeSelect.value=priorMergeCampaign;
  $("campaign-analytics").innerHTML = [
    ["Saved campaigns", analytics.campaigns], ["Company history rows", analytics.company_items],
    ["Drafts", analytics.drafts], ["Contact-form review queue", analytics.form_channel_review_queue],
    ["Test deliveries", analytics.simulated_delivery_events], ["Test submissions", analytics.simulated_submissions],
    ["Test responses", Object.values(analytics.responses).reduce((sum,value)=>sum+value,0)],
    ["Live sends · disabled", analytics.real_sends],
  ].map(([label,value],index) => `<button type="button" class="campaign-stat${campaignMetricFilter === ["all","items","drafts","forms","deliveries","submissions","responses","real"][index] ? " selected" : ""}" data-metric="${["all","items","drafts","forms","deliveries","submissions","responses","real"][index]}"><strong>${esc(value)}</strong><span>${esc(label)} · click to filter</span></button>`).join("");
  $("global-stop-button").textContent = analytics.global_emergency_stopped ? "Clear test pause (campaigns stay paused)" : "Pause test processing";
  $("global-stop-state").textContent = analytics.global_emergency_stopped ? "The test pause is active. Clearing it will not resume any campaign automatically." : "This control pauses local test batches. Live delivery is unavailable.";
  $("global-stop-button").onclick = () => apiWrite("/api/demo/emergency-stop", {enabled:!analytics.global_emergency_stopped}).then(() => loadCampaignWorkspace()).catch(showError);
  let campaigns = campaignResult.campaigns;
  if (campaignMetricFilter === "drafts") campaigns = campaigns.filter((campaign) => campaign.draft_count > 0);
  if (campaignMetricFilter === "forms") campaigns = campaigns.filter((campaign) => campaign.contact_form_review_count > 0);
  if (campaignMetricFilter === "deliveries") campaigns = campaigns.filter((campaign) => campaign.simulated_deliveries > 0 || campaign.simulated_failures > 0);
  if (campaignMetricFilter === "submissions") campaigns = campaigns.filter((campaign) => campaign.interest_submission_count > 0);
  if (campaignMetricFilter === "responses") campaigns = campaigns.filter((campaign) => campaign.simulated_response_count > 0);
  if (campaignMetricFilter === "real") campaigns = campaigns.filter((campaign) => false);
  $("campaign-list").innerHTML = campaigns.length ? campaigns.map((campaign) => `<div class="campaign-card${campaign.id === selectedCampaignId ? " selected" : ""}"><button type="button" data-campaign="${esc(campaign.id)}"><strong>${esc(displayCampaignName(campaign.name))} <span class="test-label">Test</span></strong><small>${esc(displayStatus(campaign.status))} · ${campaign.company_count} companies · ${campaign.simulated_deliveries} test deliveries</small></button></div>`).join("") : "<p>No saved campaigns match this metric. Live sending is unavailable.</p>";
  document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => { campaignMetricFilter = button.dataset.metric; loadCampaignWorkspace().catch(showError); }));
  document.querySelectorAll("[data-campaign]").forEach((button) => button.addEventListener("click", () => showCampaign(button.dataset.campaign).catch(showError)));
  renderRecentActivity(campaignResult.campaigns);
  renderAnalytics(analytics, campaignResult.campaigns);
}
function mailMergeRequest(){return {campaign_id:$("mail-merge-campaign").value,company_ids:[...state.selectedIds].sort((a,b)=>a-b)};}
function renderMailMergeAssessment(result){
  const reasons=Object.entries(result.exclusion_counts||{}).map(([reason,count])=>`<span class="mail-merge-reason"><strong>${esc(count)}</strong> ${esc(compact(reason))}</span>`).join("") || "<span>No selected companies are excluded.</span>";
  $("mail-merge-status").innerHTML=`<strong>${esc(result.included_count)} included · ${esc(result.excluded_count)} excluded</strong><div class="mail-merge-reasons">${reasons}</div>`;
  $("mail-merge-preview").innerHTML=(result.preview_rows||[]).map((row)=>`<details class="mail-merge-email"><summary>${esc(row.company_name)} · ${esc(row.recipient_email)} · ${esc(row.review_status)}</summary><p><strong>Subject:</strong> ${esc(row.email_subject)}</p><pre>${esc(row.email_body)}</pre><p><strong>Form:</strong> ${esc(row.participation_form_url)}</p></details>`).join("") || `<p class="section-help">No selected real-company email passed every preparation gate. No ready-to-send file is available.</p>`;
}
async function previewMailMerge(){
  const request=mailMergeRequest();
  if(!request.campaign_id){throw new Error("Select a campaign first.");}
  if(!request.company_ids.length){throw new Error("Select real companies on the Companies page first.");}
  const response=await fetch("/api/mail-merge/assessment",{method:"POST",headers:{"Accept":"application/json","Content-Type":"application/json"},body:JSON.stringify(request),cache:"no-store"});
  const result=await response.json(); if(!response.ok)throw new Error(result.error||"Could not assess mail merge.");
  renderMailMergeAssessment(result);
}
async function exportTestMailMerge(){
  const request=mailMergeRequest(); request.mode="TEST";
  const result=await apiDownload("/api/mail-merge/export",request);
  $("mail-merge-status").textContent=`Downloaded ${result.filename} · exactly two synthetic test rows · no messages sent.`;
}
async function exportSelectedMailMerge(){
  const request=mailMergeRequest(); request.mode="SELECTED";
  if(!request.campaign_id)throw new Error("Select a campaign first.");
  if(!request.company_ids.length)throw new Error("Select real companies on the Companies page first.");
  const result=await apiDownload("/api/mail-merge/export",request);
  $("mail-merge-status").textContent=`Downloaded ${result.filename} · prepared only · no messages sent.`;
}
function showMailMergeError(error){
  if(error.exportResult){const result=error.exportResult;$("mail-merge-status").innerHTML=`<strong>${esc(error.message)}</strong><div class="mail-merge-reasons">${Object.entries(result.exclusion_counts||{}).map(([reason,count])=>`<span class="mail-merge-reason"><strong>${esc(count)}</strong> ${esc(compact(reason))}</span>`).join("")}</div>`;}
  else $("mail-merge-status").textContent=error.message;
}
async function renderFormTokens(campaigns){const select=$("form-token-select");if(!select)return;const current=select.value;select.replaceChildren(new Option("Select a delivered test invitation",""));const submissions=[];for(const campaign of campaigns){const detail=await api(`/api/campaigns/${encodeURIComponent(campaign.id)}`);detail.items.filter(item=>item.demo_token && item.delivery_status==="SIMULATED_DELIVERED").forEach(item=>{const option=new Option(`${item.company_name} · ${displayCampaignName(campaign.name)}`,JSON.stringify({token:item.demo_token,campaign_id:campaign.id,company_id:item.company_id,company_name:item.company_name,website:item.website}));select.append(option);});detail.interest_submissions.forEach(submission=>submissions.push({company:detail.items.find((row)=>row.company_id===submission.company_id)?.company_name || `Company #${submission.company_id}`,interest:submission.interest,status:submission.review_status,at:submission.submitted_at}));}if([...select.options].some(option=>option.value===current))select.value=current;$("form-submission-status").innerHTML=submissions.length?submissions.map(item=>`<div class="activity-row"><div><strong>${esc(item.company)}</strong><small>${esc(compact(item.interest))} · ${esc(item.at)}</small></div><span class="test-label">Test · ${esc(displayStatus(item.status))}</span></div>`).join(""):`<div class="response-empty">No test interest submissions recorded.</div>`;}
async function loadSelectedToken(){const option=$("form-token-select").value;if(!option)return;const token=JSON.parse(option);await apiWrite("/api/demo/form-open",{form_token:token.token});$("local-interest-form").dataset.token=token.token;$("local-interest-form").dataset.company=token.company_name;$("local-interest-form").dataset.website=token.website;$("local-form-status").textContent=`Local form opened for ${token.company_name}. Test event only.`;const name=document.querySelector('[data-interest-field="company_name"]');const website=document.querySelector('[data-interest-field="website"]');name.value=token.company_name;website.value=token.website;}
async function submitLocalInterest(){const host=$("local-interest-form");const token=host.dataset.token;if(!token){$("local-form-status").textContent="Select a delivered test invitation first.";return;}const values={};host.querySelectorAll("[data-interest-field]").forEach((field)=>values[field.dataset.interestField]=field.value);await apiWrite("/api/demo/interest",{...values,form_token:token});$("local-form-status").textContent="Test interest saved · pending research-team review · formal consent: no";await loadCampaignWorkspace();}
function renderRecentActivity(campaigns) {
  $("recent-activity").innerHTML = campaigns.slice(0,5).map((campaign) => `<div class="activity-row"><div><strong>${esc(displayCampaignName(campaign.name))} <span class="test-label">Test</span></strong><small>${esc(campaign.company_count)} companies · ${esc(displayStatus(campaign.status))}</small></div><span class="activity-status">${esc(campaign.simulated_deliveries)} test deliveries</span></div>`).join("") || `<div class="response-empty">No campaign activity yet.</div>`;
}
function responseFilters() {
  return {
    q: $("response-search").value.trim(), status: $("response-status").value,
    campaign_id: $("response-campaign").value, source: $("response-source").value,
    date_from: $("response-date-from").value, date_to: $("response-date-to").value,
    environment: $("response-environment").value, sort: $("response-sort").value,
  };
}
function responseStatusLabel(status) {
  return ({INTERESTED:"Interested",MORE_INFO:"More information requested",DECLINED:"Declined",OPTED_OUT:"Opted out",PENDING_VERIFICATION:"Pending verification",NO_RESPONSE:"No response"})[status] || compact(status);
}
function responseSourceLabel(source) {
  return ({TEST_RECIPIENT_OUTCOME:"Test recipient outcome",PARTICIPATION_INTEREST_FORM:"Participation Interest Form"})[source] || "No response recorded";
}
function renderResponseCards(counts) {
  const cards = [["INTERESTED","Interested",counts.INTERESTED||0],["DECLINED","Declined",counts.DECLINED||0],["NEEDS_FOLLOW_UP","Needs follow-up",counts.NEEDS_FOLLOW_UP||0],["NO_RESPONSE","No response",counts.NO_RESPONSE||0]];
  $("response-cards").innerHTML = cards.map(([status,label,count])=>`<button type="button" class="response-card${$("response-status").value===status?" selected":""}" data-response-quick="${status}"><span>${esc(label)}</span><strong>${count}</strong><small>Apply filter</small></button>`).join("");
  document.querySelectorAll("[data-response-quick]").forEach((button)=>button.addEventListener("click",()=>{
    const value=button.dataset.responseQuick;
    $("response-status").value=$("response-status").value===value?"":value;
    state.responsePage=1; renderResponsesFromApi().catch(showError);
  }));
}
function renderResponseRows(records) {
  const host=$("response-table");
  if (!records.length) { host.innerHTML=`<div class="response-empty">No response records match these filters.</div>`; return; }
  host.innerHTML=`<div class="response-row response-row-head" role="row"><span>Company</span><span>Campaign</span><span>Channel</span><span>Status · source</span><span>Last activity</span><span>Data</span></div>${records.map((row)=>`<button type="button" class="response-row response-record" role="row" data-response-id="${esc(row.record_id)}"><span class="response-company"><strong>${esc(row.company_name)}</strong><small>${esc(websiteDomain(row.website))}</small></span><span>${esc(displayCampaignName(row.campaign_name))}</span><span>${esc(compact(row.channel_type||"Unknown"))}</span><span><strong>${esc(responseStatusLabel(row.status))}</strong><small>${esc(responseSourceLabel(row.response_source))}</small></span><time datetime="${esc(row.activity_at||"")}">${esc(row.activity_at||"Date unavailable")}</time><span>${row.environment==="TEST"?'<span class="test-label">Test</span>':esc(row.environment)}</span></button>`).join("")}`;
  host.querySelectorAll("[data-response-id]").forEach((button)=>button.addEventListener("click",()=>loadResponseDetail(button.dataset.responseId).catch(showError)));
}
async function loadResponseDetail(recordId) {
  const detail=await api(`/api/responses/${encodeURIComponent(recordId)}`);
  const history=(detail.company_history||[]).map((row)=>`<div class="response-history-row"><strong>${esc(displayCampaignName(row.campaign_name))}</strong><span>${esc(compact(row.channel_type||"No channel"))} · ${esc(displayStatus(row.delivery_status))}</span><small>${esc(row.updated_at||row.created_at||"")}</small></div>`).join("")||"<p>No other campaign history is stored for this company.</p>";
  const consent=detail.response_source==="PARTICIPATION_INTEREST_FORM"?"A submitted interest form is not formal research consent.":"This event is a locally stored test outcome, not a company communication.";
  const host=$("response-detail"); host.classList.remove("hidden");
  host.innerHTML=`<div class="section-head"><div><p class="eyebrow">RESPONSE DETAILS</p><h2>${esc(detail.company_name)}</h2><p class="section-help">${esc(websiteDomain(detail.website))} · ${esc(displayCampaignName(detail.campaign_name))}</p></div><button type="button" class="button outline" id="response-detail-close">Close</button></div><div class="response-detail-grid"><div><span>Status</span><strong>${esc(responseStatusLabel(detail.status))}</strong></div><div><span>Channel</span><strong>${esc(compact(detail.channel_type||"Unknown"))}</strong></div><div><span>Source</span><strong>${esc(responseSourceLabel(detail.response_source))}</strong></div><div><span>Activity</span><strong>${esc(detail.activity_at||"Date unavailable")}</strong></div><div><span>Research-team review</span><strong>${esc(displayStatus(detail.review_status||"Not applicable"))}</strong></div><div><span>Formal consent</span><strong>No</strong></div></div><p class="response-consent-note">${esc(consent)}</p><p class="response-consent-note">Saved channel evidence: ${esc(detail.source_evidence_url||"Not recorded")}</p><h3>Company outreach history</h3><div class="response-history">${history}</div><button type="button" class="button outline" id="response-open-company">Open company record</button>`;
  $("response-detail-close").addEventListener("click",()=>host.classList.add("hidden"));
  $("response-open-company").addEventListener("click",()=>{location.hash="#companies";selectCompany(Number(detail.company_id)).catch(showError);});
}
async function renderResponsesFromApi(resetPage=false) {
  if (resetPage) state.responsePage=1;
  const query=new URLSearchParams();
  Object.entries(responseFilters()).forEach(([key,value])=>{if(value)query.set(key,value);});
  query.set("page",String(state.responsePage)); query.set("page_size",String(state.responsePageSize));
  const result=await api(`/api/responses?${query.toString()}`);
  const campaignSelect=$("response-campaign"), selectedCampaign=campaignSelect.value;
  campaignSelect.replaceChildren(new Option("All campaigns",""),...result.campaigns.map((campaign)=>new Option(displayCampaignName(campaign.name),campaign.id)));
  campaignSelect.value=selectedCampaign;
  const sourceSelect=$("response-source"), selectedSource=sourceSelect.value;
  const sourceLabels={TEST_RECIPIENT_OUTCOME:"Test recipient outcome",PARTICIPATION_INTEREST_FORM:"Participation Interest Form"};
  const supported=result.sources||[];
  sourceSelect.replaceChildren(new Option("All sources",""),...supported.map((source)=>new Option(sourceLabels[source]||compact(source),source)),
    new Option("Email reply — unavailable","EMAIL_REPLY"),new Option("Manual entry — unavailable","MANUAL_ENTRY"));
  for(const option of sourceSelect.options) if(["EMAIL_REPLY","MANUAL_ENTRY"].includes(option.value))option.disabled=true;
  sourceSelect.value=supported.includes(selectedSource)?selectedSource:"";
  const env=$("response-environment"), envValue=env.value;
  const hasLive=(result.environments||[]).includes("LIVE");
  env.querySelector('[value="LIVE"]').disabled=!hasLive;
  env.value=(result.environments||[]).includes(envValue)?envValue:"";
  renderResponseCards(result.counts||{}); renderResponseRows(result.records||[]);
  $("response-count").textContent=`${result.total} matching record${result.total===1?"":"s"}`;
  const pages=Math.max(1,Math.ceil(result.total/result.page_size));
  $("response-page").textContent=`Page ${result.page} of ${pages}`;
  $("response-prev").disabled=result.page<=1; $("response-next").disabled=result.page>=pages;
  $("response-detail").classList.add("hidden");
}
function renderAnalytics(analytics,campaigns) {
  const selector=$("analytics-campaign-filter");const selected=selector.value;selector.replaceChildren(new Option("All campaigns",""),...campaigns.map(c=>new Option(displayCampaignName(c.name),c.id)));selector.value=selected;
  const scoped=selected?campaigns.filter(c=>c.id===selected):campaigns;
  let drafts=0,queued=0,deliveries=0,failures=0,responses=0,submissions=0,handoffs=0;
  scoped.forEach(c=>{drafts+=c.draft_count;deliveries+=c.simulated_deliveries;failures+=c.simulated_failures;responses+=c.simulated_response_count;submissions+=c.interest_submission_count;handoffs+=c.simulated_followup_count||0;const counts=c.item_counts||{};queued+=counts.DRAFT_REVIEW||0;});
  $("analytics-detail").innerHTML = `<div class="analytics-source-group"><h3>Test activity</h3>${[["Drafts",selected?drafts:analytics.drafts], ["Queued invitations",queued], ["Test deliveries",selected?deliveries:analytics.simulated_delivery_events], ["Test delivery failures",selected?failures:analytics.simulated_failure_events], ["Test responses",selected?responses:Object.values(analytics.responses || {}).reduce((sum,value)=>sum+value,0)], ["Test interest submissions",selected?submissions:analytics.simulated_submissions], ["Test research handoffs",selected?handoffs:analytics.simulated_followups]].map(([name,value])=>`<div class="campaign-stat"><strong>${esc(value ?? 0)}</strong><span>${esc(name)}</span></div>`).join("")}</div><div class="analytics-source-group"><h3>Live activity</h3><div class="campaign-stat"><strong>Unavailable</strong><span>Live sending is disabled</span></div></div>`;
  $("analytics-campaigns").innerHTML = scoped.map((campaign)=>`<div class="campaign-card"><strong>${esc(displayCampaignName(campaign.name))} <span class="test-label">Test</span></strong><small>${esc(campaign.company_count)} companies · ${esc(campaign.simulated_deliveries)} test deliveries · ${esc(campaign.simulated_response_count)} test responses</small></div>`).join("") || `<p class="response-empty">Campaign analytics will appear here when campaigns exist.</p>`;
  const interested=Object.entries(analytics.responses||{}).filter(([key])=>key.includes("INTERESTED")).reduce((total,[,count])=>total+count,0);const declined=Object.entries(analytics.responses||{}).filter(([key])=>key.includes("DECLINED")).reduce((total,[,count])=>total+count,0);
  $("overview-campaign-metrics").innerHTML=[["Campaigns created",analytics.campaigns],["Test invitations sent",analytics.simulated_delivery_events],["Test responses",Object.values(analytics.responses||{}).reduce((a,b)=>a+b,0)],["Interested · test",interested],["Declined · test",declined],["Pending company reviews",state.summary?.pending_human_reviews?.companies||0]].map(([label,value])=>`<div class="secondary-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
}
async function createCampaign() {
  if (!state.totalCompanies) { showError(new Error("No companies match the filters.")); return; }
  const payload=state.selectedIds.size?{name:$("campaign-name").value,company_ids:[...state.selectedIds]}:{name:$("campaign-name").value,all_matching:true,filters:state.filters};
  const result = await apiWrite("/api/campaigns", payload);
  selectedCampaignId = result.id;
  await loadCampaignWorkspace();
  await showCampaign(result.id);
}
function campaignCounts(campaign) {
  const status = campaign.item_counts || {};
  return `<div class="campaign-counts"><span>Selected: ${campaign.company_count}</span><span>Eligible: ${campaign.eligible_count}</span><span>Ineligible: ${campaign.ineligible_count}</span><span>Unresolved: ${campaign.unresolved_count}</span><span>Excluded total: ${campaign.excluded_count}</span><span>Draftable email: ${status.DRAFT_REVIEW || 0}</span><span>Contact-form review: ${status.CONTACT_FORM_REVIEW || 0}</span><span>Unverified excluded: ${status.EXCLUDED_IDENTITY_UNVERIFIED || 0}</span><span>Not eligible excluded: ${status.EXCLUDED_NOT_ELIGIBLE || 0}</span><span>No channel excluded: ${status.EXCLUDED_NO_SUITABLE_CHANNEL || 0}</span><span>Previously campaigned excluded: ${status.EXCLUDED_ALREADY_CAMPAIGNED || 0}</span><span>Test deliveries: ${campaign.simulated_deliveries}</span><span>Test failures (including recovered): ${campaign.simulated_failures}</span></div>`;
}
async function showCampaign(campaignId) {
  selectedCampaignId = campaignId;
  const mergeCampaign=$("mail-merge-campaign");
  if(mergeCampaign && [...mergeCampaign.options].some((option)=>option.value===campaignId)) mergeCampaign.value=campaignId;
  const campaign = await api(`/api/campaigns/${encodeURIComponent(campaignId)}`);
  await loadCampaignWorkspace();
  const active = ["DEMO_APPROVED","RUNNING"].includes(campaign.status);
  const toolbar = `<details class="test-actions"><summary>Test and validation actions · <span class="test-label">Test</span></summary><div class="campaign-toolbar"><strong>${esc(displayStatus(campaign.status))} · Test</strong>${campaign.status === "DRAFT" || campaign.status === "PAUSED" ? `<button class="button primary" data-action="approve">Approve for test processing</button>` : ""}${active ? `<button class="button dark" data-action="batch">Process test batch · max 10</button><button class="button outline" data-action="pause">Pause</button>` : ""}${campaign.status === "PAUSED" ? `<button class="button outline" data-action="resume">Resume test processing</button>` : ""}${!['CANCELLED','COMPLETED'].includes(campaign.status) ? `<button class="button outline" data-action="cancel">Cancel and revoke test form links</button><button class="button outline" data-action="stop">Pause all test processing</button>` : ""}</div></details>`;
  const items = campaign.items.map((item) => {
    const draft = item.draft;
    const preview = draft ? `<details><summary>Inspect ${item.channel_type === "CONTACT_FORM" ? "contact-form draft" : "email draft"} · NOT SENT</summary><p><strong>${esc(draft.recipient || item.form_url || "No recipient")}</strong></p><p><strong>${esc(draft.subject || "Contact-form message")}</strong></p><pre>${esc(draft.body || "")}</pre><p>Participation URL: ${esc(item.form_url)} · token expires ${esc(item.token_expires_at || "")}</p><p>Source contact review remains ${esc(item.contact_review_status)}. This campaign is limited to test processing.</p></details>` : "";
    const response = item.delivery_status === "SIMULATED_DELIVERED" ? `<details class="test-actions"><summary>Record test outcome</summary><div class="campaign-toolbar"><label>Test response<select data-response="${item.company_id}"><option value="INTERESTED">Interested</option><option value="MORE_INFO">More information requested</option><option value="DECLINED">Declined</option><option value="UNANSWERED">No response</option><option value="OPTED_OUT">Opted out (test record)</option></select></label><button class="button outline" data-record-response="${item.company_id}">Add test response</button></div></details>` : "";
    const form = item.delivery_status === "SIMULATED_DELIVERED" && item.demo_token ? `<details class="test-actions"><summary>Participation form test</summary><p>This local test record is isolated from real participant submissions. Interest is not consent; no public form endpoint exists.</p><p>Study description: ${esc(state.formConfiguration.study_description || "Not configured")}<br>Privacy notice: ${esc(state.formConfiguration.privacy_notice || "Not configured")}</p><button class="button outline" data-form-open="${item.company_id}" data-token="${esc(item.demo_token)}">Record test form opening</button><div class="field-row"><input aria-label="Company name" data-form="company_name" value="${esc(item.company_name)}"><input aria-label="Website" data-form="website" value="${esc(item.website)}"><input aria-label="Contact name" data-form="contact_name" placeholder="Contact name"><input aria-label="Contact role" data-form="contact_role" placeholder="Role"><input aria-label="Business email" data-form="business_email" placeholder="you@company.com"><select aria-label="Interest choice" data-form="interest"><option value="INTERESTED">Interested</option><option value="MORE_INFO">More information</option><option value="NOT_INTERESTED">Not interested</option></select></div><textarea aria-label="Questions" data-form="questions" placeholder="Optional questions"></textarea><button class="button outline" data-submit-interest="${item.company_id}" data-token="${esc(item.demo_token)}">Submit test interest</button></details>` : "";
    return `<article class="campaign-item"><div class="campaign-item-head"><strong>${esc(item.company_name)} · #${item.company_id}</strong>${pill(item.delivery_status)}</div><div class="campaign-item-meta">${esc(item.website)} · ${esc(item.channel_type || "No channel")} · ${esc(item.recipient || item.form_url || "")}</div><div class="campaign-item-meta">Queue: ${esc(item.item_status)} · Identity: ${esc(item.identity_status)} · Eligibility: ${esc(item.qualification_status)} · Contact review: ${esc(item.contact_review_status)}</div>${preview}${response}${form}${item.last_error ? `<div class="campaign-event">${esc(item.last_error)}</div>` : ""}</article>`;
  }).join("");
  const submissions = campaign.interest_submissions.map((submission) => `<div class="history-row"><span class="test-label">Test</span> · ${esc(submission.interest)} · ${esc(displayStatus(submission.review_status))} · follow-up: ${esc(displayStatus(submission.handoff_status))} · formal consent: NO ${submission.review_status === "PENDING_RESEARCH_TEAM_REVIEW" ? `<button class="button outline" data-submission-review="${submission.id}">Record test review</button>` : ""}${submission.review_status === "DEMO_RESEARCH_TEAM_REVIEWED" && submission.handoff_status === "NOT_STARTED" && submission.interest !== "NOT_INTERESTED" ? `<button class="button outline" data-submission-handoff="${submission.id}">Continue test handoff</button>` : ""}</div>`).join("") || "No test interest submissions.";
  $("campaign-detail").innerHTML = `<h3>${esc(displayCampaignName(campaign.name))} <span class="test-label">Test</span></h3>${campaignCounts(campaign)}${toolbar}<p class="campaign-safety">Test campaign activity does not approve a company or contact, authorize live outreach, or establish participant consent. Contact forms remain a manual review queue and are never submitted.</p><div class="campaign-items">${items || "<p>No company items are stored for this campaign.</p>"}</div><details><summary>Campaign audit history</summary>${campaign.audit.map((event) => `<div class="history-row"><span class="test-label">Test</span> · ${esc(displayStatus(event.event_type))} · ${esc(event.created_at)}</div>`).join("") || "No events."}</details><details><summary>Test interest submissions</summary>${submissions}</details>`;
  $("campaign-detail").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runCampaignAction(campaignId, button.dataset.action).catch(showError)));
  $("campaign-detail").querySelectorAll("[data-record-response]").forEach((button) => button.addEventListener("click", () => apiWrite(`/api/campaigns/${campaignId}/responses`, {company_id:Number(button.dataset.recordResponse),response:$("campaign-detail").querySelector(`[data-response="${button.dataset.recordResponse}"]`).value}).then(() => showCampaign(campaignId)).catch(showError)));
  $("campaign-detail").querySelectorAll("[data-submit-interest]").forEach((button) => button.addEventListener("click", () => {
    const card = button.closest(".campaign-item"); const values = {};
    card.querySelectorAll("[data-form]").forEach((control) => { values[control.dataset.form] = control.value; });
    apiWrite("/api/demo/interest", {...values,form_token:button.dataset.token}).then((result) => { alert(`Test interest saved and is pending research-team review. Formal consent: no.`); return showCampaign(campaignId); }).catch(showError);
  }));
  $("campaign-detail").querySelectorAll("[data-form-open]").forEach((button) => button.addEventListener("click", () => apiWrite("/api/demo/form-open", {form_token:button.dataset.token}).then(() => alert("Test form opening recorded. No external page was opened.")).catch(showError)));
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
function showError(error) { const host = $("app-error"); host.hidden = false; host.textContent = `Unable to load this view: ${error.message}`; }
const researchState={page:1,pageSize:25,selected:new Set(),total:0,filters:{promotion_state:"OPERATIONAL_WITH_TEST"}};
async function reloadResearchQueue(){const query=new URLSearchParams({page:String(researchState.page),page_size:String(researchState.pageSize),...researchState.filters});const result=await api(`/api/research/records?${query}`);researchState.total=result.total;const host=$("research-table");host.innerHTML=result.records.length?result.records.map(row=>`<article class="company-row ${researchState.selected.has(row.profile_id)?"selected":""}"><input type="checkbox" data-research-select="${esc(row.profile_id)}" ${researchState.selected.has(row.profile_id)?"checked":""}><button class="company-choice" data-research-detail="${esc(row.profile_id)}"><span class="company-main"><span class="company-name">${esc(row.name)}</span><span class="company-url">${esc(row.official_website)}</span></span><span class="company-status"><span class="company-status-label">${esc(row.profile_type)}</span>${pill(row.qualification_status)}<span class="company-status-label">Consent</span>${pill(row.consent_status)}</span><small>${esc(row.industry||"Unknown industry")} · ${esc(row.location||"Unknown location")} · Screen ${esc(row.geo_research_status)}</small></button></article>`).join(""):`<div class="no-results">No research records match these filters.</div>`;host.querySelectorAll("[data-research-select]").forEach(box=>box.addEventListener("change",()=>{box.checked?researchState.selected.add(box.dataset.researchSelect):researchState.selected.delete(box.dataset.researchSelect);updateResearchSelection();}));host.querySelectorAll("[data-research-detail]").forEach(button=>button.addEventListener("click",()=>loadResearchDetail(button.dataset.researchDetail).catch(showError)));$("research-page").textContent=`Page ${result.page} of ${Math.max(1,Math.ceil(result.total/result.page_size))} · ${result.total} records`;$("research-prev").disabled=result.page<=1;$("research-next").disabled=result.page>=Math.max(1,Math.ceil(result.total/result.page_size));updateResearchSelection();}
async function reloadResearchQueue(){
  const publicMode=!state.dashboardToken;
  const filters={...researchState.filters};
  if(publicMode) delete filters.consent_status;
  const query=new URLSearchParams({page:String(researchState.page),page_size:String(researchState.pageSize),...filters});
  const result=publicMode?await publicApi(`/api/public/research/records?${query}`):await api(`/api/research/records?${query}`);
  researchState.total=result.total;
  const host=$("research-table");
  host.innerHTML=result.records.length?result.records.map(row=>publicMode
    ?`<article class="company-row public-company-row"><button class="company-choice" data-research-detail="${esc(row.profile_id)}"><span class="company-main"><span class="company-name">${esc(row.name)}</span><span class="company-url">${esc(row.official_website)}</span></span><span class="company-status"><span class="company-status-label">${esc(row.profile_type)}</span>${pill(row.qualification_status)}<span class="company-status-label">Public read-only</span></span><small>${esc(row.industry||"Unknown industry")} · ${esc(row.location||"Unknown location")} · Screen ${esc(row.screening_status||"NOT_PUBLIC")}</small></button></article>`
    :`<article class="company-row ${researchState.selected.has(row.profile_id)?"selected":""}"><input type="checkbox" data-research-select="${esc(row.profile_id)}" ${researchState.selected.has(row.profile_id)?"checked":""}><button class="company-choice" data-research-detail="${esc(row.profile_id)}"><span class="company-main"><span class="company-name">${esc(row.name)}</span><span class="company-url">${esc(row.official_website)}</span></span><span class="company-status"><span class="company-status-label">${esc(row.profile_type)}</span>${pill(row.qualification_status)}<span class="company-status-label">Consent</span>${pill(row.consent_status)}</span><small>${esc(row.industry||"Unknown industry")} · ${esc(row.location||"Unknown location")} · Screen ${esc(row.geo_research_status)}</small></button></article>`).join(""):`<div class="no-results">No research records match these filters.</div>`;
  host.querySelectorAll("[data-research-select]").forEach(box=>box.addEventListener("change",()=>{box.checked?researchState.selected.add(box.dataset.researchSelect):researchState.selected.delete(box.dataset.researchSelect);updateResearchSelection();}));
  host.querySelectorAll("[data-research-detail]").forEach(button=>button.addEventListener("click",()=>loadResearchDetail(button.dataset.researchDetail).catch(showError)));
  $("research-page").textContent=`Page ${result.page} of ${Math.max(1,Math.ceil(result.total/result.page_size))} · ${result.total} records`;
  $("research-prev").disabled=result.page<=1;$("research-next").disabled=result.page>=Math.max(1,Math.ceil(result.total/result.page_size));updateResearchSelection();
}
function updateResearchSelection(){
  $("research-selected-count").textContent=`${researchState.selected.size} selected`;
}
async function downloadResearchMailMergePackage(){
  if(!researchState.selected.size){showError(new Error("Select at least one approved invitation draft"));return;}
  const response=await fetch("/api/research/mail-merge/package",{method:"POST",headers:{"Content-Type":"application/json",...authHeaders()},body:JSON.stringify({profile_ids:[...researchState.selected]})});
  if(!response.ok){let detail="Mail Merge package export failed";try{detail=(await response.json()).error||detail;}catch(_){ }throw new Error(detail);}
  const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download="geo-research-mail-merge-package.zip";document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);$("research-status").textContent="Downloaded approved Word Mail Merge package (DOCX + XLSX + CSV).";
}
function reviewHeaders(){return `<label class="field-label">Review token (only required off localhost)<input id="consent-review-token" type="password" value="${esc(state.reviewToken)}" autocomplete="off"></label>`;}
function renderConsentReview(row, profileId){
  const submissions=row.questionnaire_submissions||[], conflicts=row.questionnaire_conflicts||[], reviews=row.consent_reviews||[];
  const response=submissions[0];
  const responseHtml=response?`<div class="consent-response"><div><strong>${esc(response.submission_id)}</strong> · ${esc(response.response_source||"UNKNOWN")}</div><div>Invitation: ${esc(response.invitation_reference||response.invitation_id)}</div><div>Interest: ${esc(response.research_interest)} · Authorization: ${esc(response.research_authorization)}</div><div>Respondent: ${esc(response.representative_full_name)} · ${esc(response.representative_job_title)} · ${esc(response.contact_email)}</div><div>Submitted: ${esc(response.submitted_at)} · Consent: ${esc(response.consent_status)} · Review: ${esc(response.review_status)} · Identity: ${esc(response.identity_review_status)}</div></div>`:`<p class="section-help">No questionnaire response is recorded.</p>`;
  const conflictHtml=conflicts.length?`<div class="conflict-warning"><strong>Conflict quarantine</strong>${conflicts.map(item=>`<div>${esc(item.conflict_id)} · ${esc(item.status)} · ${esc(item.reason)} · ${esc(item.created_at)}</div>`).join("")}<p>Approval is disabled until a human resolves this conflict in the lifecycle store.</p></div>`:"";
  const history=reviews.length?reviews.map(item=>`<li>${esc(item.created_at)} · ${esc(item.reviewer)} · ${esc(item.decision)} · ${esc(item.notes||"No notes")}</li>`).join(""):"<li>No review decisions recorded.</li>";
  const actionable=response && !conflicts.length && ["NEEDS_REVIEW","PENDING"].includes(String(response.review_status||""));
  const form=actionable?`<div class="review-checks"><label><input type="checkbox" data-review-check="company_identity_verified"> Company identity verified</label><label><input type="checkbox" data-review-check="representative_authorized"> Representative authorization verified</label><label><input type="checkbox" data-review-check="consent_version_verified"> Consent wording/version verified</label><label><input type="checkbox" data-review-check="website_scope_verified"> Website-analysis scope verified</label><label><input type="checkbox" data-review-check="withdrawal_checked"> Withdrawal status checked</label></div><label class="field-label">Reviewer<input id="consent-reviewer" required placeholder="Reviewer name"></label><label class="field-label">Notes<textarea id="consent-review-notes" placeholder="Review evidence or rejection reason"></textarea></label><div class="review-actions"><button class="button primary" data-consent-decision="approve">Approve verified consent</button><button class="button outline" data-consent-decision="reject">Reject / keep blocked</button></div>`:`<p class="section-help">${response?conflicts.length?"Review controls disabled while a conflicting response is quarantined.":`No pending review action. Current state: ${esc(response.review_status||response.consent_status||"UNKNOWN")}.`:"Review controls appear after a response is synchronized."}</p>`;
  return `<div class="subsection consent-review-card"><div class="section-head"><div><p class="eyebrow">HUMAN CONSENT REVIEW</p><h3>Response and authorization review</h3></div></div>${reviewHeaders()}${responseHtml}${conflictHtml}<h4>Review history</h4><ul class="review-history">${history}</ul>${form}</div>`;
}
async function loadResearchDetail(profileId){const row=await api(`/api/research/records/${encodeURIComponent(profileId)}`);$("research-detail").innerHTML=`<div class="detail-title"><div><h2>${esc(row.name)}</h2><div class="detail-url">${esc(row.official_website)}</div></div>${pill(row.consent_status)}</div><div class="detail-grid"><div class="detail-stat"><span>Type</span><strong>${esc(row.profile_type)}</strong></div><div class="detail-stat"><span>Recruitment</span><strong>${esc(row.recruitment_status)}</strong></div><div class="detail-stat"><span>Questionnaire</span><strong>${esc(row.questionnaire_status)}</strong></div><div class="detail-stat"><span>GEO</span><strong>${esc(row.geo_research_status)}</strong></div></div><div class="subsection"><h4>Screening evidence</h4>${listItems((row.screenings||[]).map(item=>JSON.parse(item.evidence_json||"[]")),"No screening evidence recorded.")}</div><div class="subsection"><h4>Drafts</h4>${listItems((row.email_drafts||[]).map(item=>`${item.draft_id} · ${item.approval_status} · ${item.recipient}`),"No recruitment drafts.")}</div><div class="subsection"><h4>GEO handoffs</h4>${listItems((row.handoffs||[]).map(item=>`${item.handoff_id} · ${item.status}`),"No GEO handoff records.")}</div>${renderConsentReview(row,profileId)}`;
  const tokenButton=$("research-detail").querySelector("#consent-review-token"); if(tokenButton) tokenButton.addEventListener("change",()=>{state.reviewToken=tokenButton.value;});
  $("research-detail").querySelectorAll("[data-consent-decision]").forEach(button=>button.addEventListener("click",async()=>{const reviewer=$("research-detail").querySelector("#consent-reviewer").value.trim();if(!reviewer){showError(new Error("Reviewer name is required"));return;}const checks={};$("research-detail").querySelectorAll("[data-review-check]").forEach(input=>checks[input.dataset.reviewCheck]=input.checked);try{await researchAction("/api/research/consent-review",{profile_id:profileId,submission_id:(row.questionnaire_submissions||[])[0]?.submission_id,reviewer,approve:button.dataset.consentDecision==="approve",notes:$("research-detail").querySelector("#consent-review-notes").value,...checks});await loadResearchDetail(profileId);}catch(error){showError(error);}}));
}
async function researchAction(path,body){const result=await apiWrite(path,body);$("research-status").textContent=JSON.stringify(result);await reloadResearchQueue();return result;}
function focusResearchDetail(){const detail=$("research-detail");if(detail){requestAnimationFrame(()=>{const top=detail.getBoundingClientRect().top+window.scrollY-24;window.scrollTo({top:Math.max(0,top),behavior:"smooth"});});}}
async function loadResearchDetail(profileId){
  if(!state.dashboardToken){
    $("research-detail").innerHTML=`<div class="empty-state"><h3>Operator sign-in required</h3><p>This record's website evidence, screening observations, contact evidence, invitations, and manual controls require protected operator access.</p><form id="research-inline-login" class="dashboard-login-form"><label class="field-label">Operator dashboard token<input id="research-inline-token" type="password" autocomplete="off" required></label><button class="button primary" type="submit">Open protected details</button></form><p id="research-inline-login-status" class="inline-status" role="status"></p></div>`;
    focusResearchDetail();
    const form=$("research-inline-login");
    form.addEventListener("submit",async(event)=>{
      event.preventDefault();
      const token=$("research-inline-token").value.trim();
      const status=$("research-inline-login-status");
      if(!token)return;
      state.dashboardToken=token;
      status.textContent="Authenticating…";
      try{
        await api(`/api/research/records/${encodeURIComponent(profileId)}`);
        sessionStorage.setItem("geo_outreach_dashboard_token",token);
        await reloadResearchQueue();
        await loadResearchDetail(profileId);
      }catch(error){
        state.dashboardToken="";
        sessionStorage.removeItem("geo_outreach_dashboard_token");
        status.textContent="Authentication failed. Check the token and try again.";
        showError(error);
      }
    });
    return;
  }
  const row=await api(`/api/research/records/${encodeURIComponent(profileId)}`);
  const drafts=row.email_drafts||[];
  const official=(row.contact_channels||[]).filter(item=>item.email&&["APPROVED","VERIFIED","CONTACT_VERIFIED"].includes(String(item.review_status||"").toUpperCase()));
  const evidence=(row.verification_evidence_json&&JSON.parse(row.verification_evidence_json||"[]"))||[];
  const draftHtml=drafts.length?drafts.map(item=>`<div class="draft-card"><strong>${esc(item.subject||"Invitation draft")}</strong><div>${esc(item.approval_status)} · delivery: ${esc(item.delivery_recipient||item.recipient)} · intended: ${esc(item.intended_recipient||"Not established")}</div><div>${item.test_override?"OWNER-CONTROLLED TEST OVERRIDE · ":""}${esc(item.invitation_id||"")}</div><details><summary>Preview</summary><pre>${esc(item.body||"")}</pre></details>${item.approval_status==="PENDING"?`<button class="button outline" data-approve-draft="${esc(item.draft_id)}">Approve for manual send</button>`:""}${item.approval_status==="APPROVED"?`<div class="manual-reconcile"><p class="section-help">After the operator sends this exact preview through Word Mail Merge/Outlook, reconcile it with evidence from Sent Items. This does not send.</p><input data-send-message="${esc(item.draft_id)}" placeholder="Outlook Message-ID"><input data-send-evidence="${esc(item.draft_id)}" placeholder="Sent Items evidence reference"><button class="button outline" data-reconcile-send="${esc(item.draft_id)}">Record provider acceptance</button></div>`:""}</div>`).join(""):"<p class=section-help>No invitation drafts.</p>";
  $("research-detail").innerHTML=`<div class="detail-title"><div><h2>${esc(row.name)}</h2><div class="detail-url">${esc(row.official_website)}</div></div>${pill(row.consent_status)}</div><div class="detail-grid"><div class="detail-stat"><span>Type</span><strong>${esc(row.profile_type)}</strong></div><div class="detail-stat"><span>Recruitment</span><strong>${esc(row.recruitment_status)}</strong></div><div class="detail-stat"><span>Questionnaire</span><strong>${esc(row.questionnaire_status)}</strong></div><div class="detail-stat"><span>GEO</span><strong>${esc(row.geo_research_status)}</strong></div></div><div class="subsection"><h4>Verified website/contact evidence</h4>${listItems(evidence.map(item=>`${item.text||item.description||"Evidence recorded"}${item.source_url?` · ${item.source_url}`:""}`),"No verification evidence recorded.")}<div>${official.length?official.map(item=>`<div>${esc(item.channel_type||"OFFICIAL_CONTACT")} · ${esc(item.email)} · ${esc(item.source_url||"source URL not recorded")} · ${esc(item.review_status)}</div>`).join(""):"No verified official company email. Test delivery overrides do not change this."}</div></div><div class="subsection"><h4>Preliminary GEO observations</h4>${listItems((row.screenings||[]).flatMap(item=>{try{const value=JSON.parse(item.opportunities_json||"[]");return (value.opportunities||value||[]).map(x=>x.title||x.observation||JSON.stringify(x));}catch(_){return [];}}),"No preliminary screening evidence recorded.")}</div><div class="subsection"><h4>Prepare manual invitation</h4><p class="section-help">Creates a draft only. Sending remains an explicit Word Mail Merge/Outlook action and is reconciled only from provider evidence.</p><label class="field-label">Delivery recipient<select id="manual-delivery-recipient"><option value="">Select a verified official email</option>${official.map(item=>`<option value="${esc(item.email)}">${esc(item.email)} · official</option>`).join("")}<option value="yuxuan.chen.1031@outlook.com">yuxuan.chen.1031@outlook.com · owner test override</option><option value="yuxaun.chen.1031@outlook.com">yuxaun.chen.1031@outlook.com · owner test override</option></select></label><label><input type="checkbox" id="manual-test-override"> Owner-controlled test override (required for test inboxes)</label><button class="button primary" id="manual-create-draft">Create invitation draft</button></div><div class="subsection"><h4>Invitation drafts</h4>${draftHtml}</div><div class="subsection"><h4>GEO handoffs</h4>${listItems((row.handoffs||[]).map(item=>`${item.handoff_id} · ${item.status}`),"No GEO handoff records.")}</div>${renderConsentReview(row,profileId)}`;
  const createButton=$("manual-create-draft"); if(createButton) createButton.addEventListener("click",async()=>{const recipient=$("manual-delivery-recipient").value;if(!recipient){showError(new Error("Select a delivery recipient"));return;}try{await researchAction("/api/recruitment/manual-draft",{profile_id:profileId,delivery_recipient:recipient,test_override:$("manual-test-override").checked});await loadResearchDetail(profileId);}catch(error){showError(error);}});
  $("research-detail").querySelectorAll("[data-approve-draft]").forEach(button=>button.addEventListener("click",async()=>{const reviewer=window.prompt("Reviewer name for manual-send approval:");if(!reviewer)return;try{await researchAction("/api/recruitment/approve-batch",{draft_ids:[button.dataset.approveDraft],reviewer});await loadResearchDetail(profileId);}catch(error){showError(error);}}));
  $("research-detail").querySelectorAll("[data-reconcile-send]").forEach(button=>button.addEventListener("click",async()=>{const id=button.dataset.reconcileSend;const message=$("research-detail").querySelector(`[data-send-message="${CSS.escape(id)}"]`).value.trim();const evidence=$("research-detail").querySelector(`[data-send-evidence="${CSS.escape(id)}"]`).value.trim();if(!message||!evidence){showError(new Error("Message-ID and Sent Items evidence are required"));return;}try{await researchAction("/api/recruitment/manual-send-reconcile",{draft_id:id,provider:"OUTLOOK_WORD_MAIL_MERGE",provider_message_id:message,evidence_ref:evidence});await loadResearchDetail(profileId);}catch(error){showError(error);}}));
  focusResearchDetail();
}
const pages = ["overview","companies","hotels","research-queue","campaigns","interest-form","responses","analytics","settings"];
function navigate() {
  const requested = location.hash.slice(1) || (state.summary?.cloud_registry ? "research-queue" : "overview");
  const page = pages.includes(requested) ? requested : "overview";
  document.querySelectorAll("[data-view]").forEach((view)=>{view.hidden=view.dataset.view!==page;});
  document.querySelectorAll("[data-page]").forEach((link)=>{const active=link.dataset.page===page;link.classList.toggle("active",active);if(active)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
  document.title = `${page.replaceAll("-"," ").replace(/\b\w/g,(c)=>c.toUpperCase())} · GEO Research Outreach Platform`;
  if(page==="responses") renderResponsesFromApi().catch(showError);
  if(page==="research-queue") reloadResearchQueue().catch(showError);
  if(page==="interest-form") renderFormTokens(state.campaignSummaries).catch(showError);
}
async function start() {
  if (state.dashboardToken) $("dashboard-login").hidden=true;
  wireFilters();
  state.emailConfiguration=await api("/api/test-email/config");
  const result = await api("/api/summary");
  state.summary = result.summary; state.participationFormUrl = result.simulated_signup_url || "https://research.example.invalid/participation-interest";
  state.formConfiguration = result.participation_form_configuration || {};
  renderSettings(state.formConfiguration);
  loadSenderAccounts().catch(showError);
  renderSummary({...result.summary, filters: result.filters});
  await reloadCompanies();
  await loadCampaignWorkspace();
  navigate();
}
async function start() {
  if(!state.dashboardToken){
    const result=await publicApi("/api/public/summary");
    state.summary=result.summary; state.participationFormUrl=""; state.formConfiguration={};
    renderSummary({...result.summary, filters: result.filters||{}});
    const protectedActions=$("research-protected-actions"); if(protectedActions) protectedActions.hidden=true;
    await loadPublicOverview();
    navigate();
    return;
  }
  const result=await api("/api/summary");
  state.summary=result.summary; state.participationFormUrl=result.simulated_signup_url||""; state.formConfiguration=result.participation_form_configuration||{};
  renderSettings(state.formConfiguration); renderSummary({...result.summary, filters: result.filters}); await reloadCompanies(); await loadCampaignWorkspace(); navigate();
}
window.addEventListener("hashchange",navigate);
const loginForm=$("dashboard-login-form"); if(loginForm) loginForm.addEventListener("submit",async(event)=>{
  event.preventDefault();
  const token=$("dashboard-token").value.trim();
  if(!token)return;
  state.dashboardToken=token;
  sessionStorage.setItem("geo_outreach_dashboard_token",token);
  $("dashboard-login-status").textContent="Authenticating…";
  try{
    await start();
    $("dashboard-login").hidden=true;
    $("app-error").textContent="Protected operator access is configured for this browser session.";
    $("app-error").hidden=true;
    $("dashboard-token").value="";
  }catch(error){
    state.dashboardToken="";
    sessionStorage.removeItem("geo_outreach_dashboard_token");
    $("dashboard-login-status").textContent="Authentication failed. Check the token and try again.";
    showError(error);
  }
});
let responseSearchTimer;
$("response-search").addEventListener("input",()=>{clearTimeout(responseSearchTimer);responseSearchTimer=setTimeout(()=>{state.responsePage=1;renderResponsesFromApi().catch(showError);},250);});
["response-status","response-campaign","response-source","response-date-from","response-date-to","response-environment","response-sort"].forEach((id)=>$(id).addEventListener("change",()=>{state.responsePage=1;renderResponsesFromApi().catch(showError);}));
$("response-page-size").addEventListener("change",()=>{state.responsePageSize=Number($("response-page-size").value)||25;state.responsePage=1;renderResponsesFromApi().catch(showError);});
$("response-prev").addEventListener("click",()=>{state.responsePage=Math.max(1,state.responsePage-1);renderResponsesFromApi().catch(showError);});
$("response-next").addEventListener("click",()=>{state.responsePage+=1;renderResponsesFromApi().catch(showError);});
$("response-export").addEventListener("click",()=>{const query=new URLSearchParams();Object.entries(responseFilters()).forEach(([key,value])=>{if(value)query.set(key,value);});window.location.href=`/api/responses/export.csv?${query.toString()}`;});
$("export-all-companies").addEventListener("click",()=>downloadCompanyCsv(false));
$("export-filtered-companies").addEventListener("click",()=>downloadCompanyCsv(true));
let candidateSearchTimer;
$("candidate-search").addEventListener("input",()=>{clearTimeout(candidateSearchTimer);candidateSearchTimer=setTimeout(()=>{state.candidateFilters.q=$("candidate-search").value;state.candidatePage=1;reloadCandidates().catch(showError);},200);});
["candidate-source","candidate-status","candidate-screen-status"].forEach((id)=>$(id).addEventListener("change",()=>{state.candidateFilters[id==="candidate-source"?"source":id==="candidate-screen-status"?"screen_status":"status"]=$(id).value;state.candidatePage=1;reloadCandidates().catch(showError);}));
$("candidate-prev").addEventListener("click",()=>{state.candidatePage=Math.max(1,state.candidatePage-1);reloadCandidates().catch(showError);});
$("candidate-next").addEventListener("click",()=>{state.candidatePage+=1;reloadCandidates().catch(showError);});
["phase6g-source","phase6g-confidence","phase6g-status"].forEach((id)=>$(id).addEventListener("change",()=>{state.phase6gFilters[id==="phase6g-source"?"source":id==="phase6g-confidence"?"confidence":"status"]=$(id).value;state.phase6gPage=1;reloadPhase6gReview().catch(showError);}));
$("phase6g-prev").addEventListener("click",()=>{state.phase6gPage=Math.max(1,state.phase6gPage-1);reloadPhase6gReview().catch(showError);});
$("phase6g-next").addEventListener("click",()=>{state.phase6gPage+=1;reloadPhase6gReview().catch(showError);});
$("test-email-recipient").addEventListener("change",()=>loadTestEmailPreview().catch(showError));
$("refresh-test-email-preview").addEventListener("click",()=>loadTestEmailPreview().catch(showError));
$("test-email-confirm").addEventListener("change",updateTestEmailSendButton);
$("send-test-email").addEventListener("click",async()=>{
  if($("send-test-email").disabled)return;
  const result=await apiWrite("/api/test-email/send",{recipient:$("test-email-recipient").value,preview_hash:state.testEmailPreview.preview_hash,confirm:true});
  $("test-email-status").textContent=result.status==="PROVIDER_ACCEPTED"?`Gmail API accepted message ${result.provider_message_id}. Inbox delivery is not confirmed.`:"No email was sent.";
  $("test-email-confirm").checked=false;
  updateTestEmailSendButton();
  await loadTestEmailEvents();
});
$("connect-gmail").addEventListener("click",async()=>{
  $("connect-gmail").disabled=true;
  $("gmail-connection-status").textContent="Opening Google sign-in. Connecting does not send email.";
  try{
    const result=await apiWrite("/api/test-email/connect",{action:"authorize"});
    state.emailConfiguration=result;
    renderTestEmailSettings();
  }catch(error){$("gmail-connection-status").textContent=error.message;}
  finally{$("connect-gmail").disabled=!state.emailConfiguration?.oauth_client_configured;}
});
$("add-sender-account").addEventListener("click",async()=>{try{const result=await apiWrite("/api/sender-accounts",{email:$('sender-email').value,display_name:$('sender-display-name').value,reply_to:$('sender-reply-to').value,signature:$('sender-signature').value,provider:"GMAIL"});$("sender-account-status").textContent=result.message||"Sender account saved";await loadSenderAccounts();}catch(error){$("sender-account-status").textContent=error.message;}});
$("analytics-campaign-filter").addEventListener("change",()=>api("/api/campaign-analytics").then(result=>renderAnalytics(result,state.campaignSummaries)).catch(showError));
$("hotel-discovery-pause").addEventListener("click",()=>apiWrite("/api/hotel-discovery/pause",{}).then(result=>{renderSummary({...state.summary,hotel_discovery:result,filters:state.summary.filters});}).catch(showError));
$("hotel-discovery-resume").addEventListener("click",()=>apiWrite("/api/hotel-discovery/resume",{}).then(result=>{renderSummary({...state.summary,hotel_discovery:result,filters:state.summary.filters});}).catch(showError));
$("research-search").addEventListener("input",()=>{researchState.filters.q=$("research-search").value;researchState.page=1;reloadResearchQueue().catch(showError);});
["research-type","research-geography","research-consent"].forEach((id)=>$(id).addEventListener("change",()=>{researchState.filters[id==="research-type"?"profile_type":id==="research-geography"?"geographic_status":"consent_status"]=$(id).value;researchState.page=1;reloadResearchQueue().catch(showError);}));
$("research-prev").addEventListener("click",()=>{researchState.page=Math.max(1,researchState.page-1);reloadResearchQueue().catch(showError);});
$("research-next").addEventListener("click",()=>{researchState.page+=1;reloadResearchQueue().catch(showError);});
$("research-select-all").addEventListener("change",()=>{$("research-table").querySelectorAll("[data-research-select]").forEach(box=>{box.checked=$("research-select-all").checked;box.checked?researchState.selected.add(box.dataset.researchSelect):researchState.selected.delete(box.dataset.researchSelect);});updateResearchSelection();});
$("research-create-draft").addEventListener("click",async()=>{const id=[...researchState.selected][0];if(!id)return;const recipient=window.prompt("Approved recipient address for this draft:");if(!recipient)return;try{await researchAction("/api/recruitment/screen-and-draft",{profile_id:id,recipient,mode:"RULE_BASED"});}catch(error){showError(error);}});
$("research-mail-merge-package").addEventListener("click",()=>downloadResearchMailMergePackage().catch(showError));
$("research-invite").addEventListener("click",async()=>{const id=[...researchState.selected][0];if(!id)return;try{await researchAction("/api/recruitment/invite",{profile_id:id});}catch(error){showError(error);}});
$("research-approve").addEventListener("click",async()=>{const reviewer=window.prompt("Reviewer name:");if(!reviewer)return;try{const records=await Promise.all([...researchState.selected].map(id=>api(`/api/research/records/${encodeURIComponent(id)}`)));const drafts=records.flatMap(row=>(row.email_drafts||[]).filter(item=>item.approval_status==="PENDING").map(item=>item.draft_id));if(!drafts.length)throw new Error("No pending drafts selected");await researchAction("/api/recruitment/approve-batch",{draft_ids:drafts,reviewer});}catch(error){showError(error);}});
$("research-send").addEventListener("click",async()=>{try{const records=await Promise.all([...researchState.selected].map(id=>api(`/api/research/records/${encodeURIComponent(id)}`)));const drafts=records.flatMap(row=>(row.email_drafts||[]).filter(item=>item.approval_status==="APPROVED").map(item=>item.draft_id));if(!drafts.length)throw new Error("No approved drafts selected");await researchAction("/api/recruitment/send-simulated",{draft_ids:drafts});}catch(error){showError(error);}});
function publicDirectoryMarkup(title, description, result) {
  const rows = result.records || [];
  const maxPage = Math.max(1, Math.ceil(Number(result.total || 0) / Number(result.page_size || 25)));
  return `<div class="page-heading"><div><p class="eyebrow">PUBLIC READ-ONLY DIRECTORY</p><h1>${esc(title)}</h1><p>${esc(description)}</p></div><span class="mini-tag">${Number(result.total || 0).toLocaleString()} prospects</span></div><div class="public-readonly-notice"><strong>Public read-only view</strong><span>Names, websites, qualification states, and locations are visible. Contacts, invitations, responses, consent, and approvals remain protected.</span></div><div class="filters"><label class="search"><span>⌕</span><input id="public-directory-search" type="search" value="${esc(result.query || "")}" placeholder="Search name, website, or location"></label></div><div class="company-list" id="public-directory-rows">${rows.length ? rows.map(row => `<article class="company-row public-company-row"><a class="company-choice" href="${esc(row.official_website || "#")}" target="_blank" rel="noreferrer"><span class="company-main"><span class="company-name">${esc(row.name)}</span><span class="company-url">${esc(row.official_website || "Website not recorded")}</span></span><span class="company-status"><span class="company-status-label">${esc(compact(row.profile_type))}</span>${pill(row.qualification_status)}<span class="company-status-label">${esc(compact(row.geographic_classification || "COUNTRY_UNKNOWN"))}</span></span><small>${esc(row.industry || "Unknown industry")} · ${esc(row.location || "Unknown location")} · Website ${esc(compact(row.identity_verification_status || "UNKNOWN"))}</small></a></article>`).join("") : `<div class="empty-state"><h3>No public prospects match these filters</h3><p>Try a broader search.</p></div>`}</div><div class="list-pagination"><button class="button outline" id="public-directory-prev" ${result.page <= 1 ? "disabled" : ""}>Previous</button><span>Page ${result.page} of ${maxPage} · ${Number(result.total || 0).toLocaleString()} records</span><button class="button outline" id="public-directory-next" ${result.page >= maxPage ? "disabled" : ""}>Next</button></div>`;
}
async function loadPublicDirectory(page, profileType) {
  const host = $(page === "hotels" ? "page-hotels" : "page-companies");
  const endpoint = profileType === "hotel_property" ? "/api/public/hotels" : "/api/public/companies";
  const query = new URLSearchParams({page:String(state.publicDirectoryPage || 1), page_size:"25"});
  if (state.publicDirectoryQuery) query.set("q", state.publicDirectoryQuery);
  const result = await publicApi(`${endpoint}?${query}`); result.query = state.publicDirectoryQuery || "";
  host.innerHTML = publicDirectoryMarkup(profileType === "hotel_property" ? "Hotels" : "Companies", profileType === "hotel_property" ? "Qualified US hotel properties in the operational prospect registry." : "Eligible operational company prospects in the lean registry.", result);
  $("public-directory-search").addEventListener("input", (event) => { state.publicDirectoryQuery = event.target.value; state.publicDirectoryPage = 1; clearTimeout(state.publicDirectoryTimer); state.publicDirectoryTimer = setTimeout(() => loadPublicDirectory(page, profileType).catch(showError), 250); });
  $("public-directory-prev").addEventListener("click", () => { state.publicDirectoryPage = Math.max(1, (state.publicDirectoryPage || 1) - 1); loadPublicDirectory(page, profileType).catch(showError); });
  $("public-directory-next").addEventListener("click", () => { state.publicDirectoryPage = (state.publicDirectoryPage || 1) + 1; loadPublicDirectory(page, profileType).catch(showError); });
}
function renderPublicRestricted(page, title, reason) {
  const host = $("page-" + page); if (!host) return;
  host.innerHTML = `<div class="page-heading"><div><p class="eyebrow">PROTECTED WORKFLOW</p><h1>${esc(title)}</h1><p>${esc(reason)}</p></div></div><section class="panel restricted-panel"><h2>Restricted operation</h2><p>This view is not available in the public browsing session because it may expose or modify sensitive operational data.</p><p>Operator authentication is required for contacts, invitations, questionnaire responses, consent review, approvals, exports, sender settings, and GEO handoffs.</p></section>`;
}
async function loadPublicResponses() {
  const result = await publicApi("/api/public/responses"); const host = $("page-responses");
  host.innerHTML = `<div class="page-heading"><div><p class="eyebrow">FOLLOW-UP</p><h1>Responses</h1><p>Aggregate response activity only. Personal submissions and contact details are never returned publicly.</p></div></div><section class="panel"><div class="public-readonly-notice"><strong>Aggregate-only view</strong><span>Individual response records, identities, consent evidence, and questionnaire contents require protected operator access.</span></div><div class="metrics">${Object.entries(result.responses.by_status || {}).map(([key,value]) => `<div class="metric"><div class="metric-label">${esc(compact(key))}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">Aggregate count</div></div>`).join("") || `<div class="empty-state"><h3>No public response metrics</h3><p>Response details are protected.</p></div>`}</div></section>`;
}
async function loadPublicAnalytics() {
  const result = await publicApi("/api/public/analytics"); const host = $("page-analytics");
  host.innerHTML = `<div class="page-heading"><div><p class="eyebrow">MEASUREMENT</p><h1>Analytics</h1><p>Public aggregate metrics from the lean operational registry. Test activity is separated from live activity.</p></div></div><section class="panel"><div class="metrics"><div class="metric"><div class="metric-label">Company prospects</div><div class="metric-value">${Number(result.companies || 0).toLocaleString()}</div><div class="metric-note">Operational records</div></div><div class="metric"><div class="metric-label">Hotel prospects</div><div class="metric-value">${Number(result.hotels || 0).toLocaleString()}</div><div class="metric-note">Operational records</div></div><div class="metric"><div class="metric-label">Owner-controlled tests</div><div class="metric-value">${Number(result.owner_controlled_tests || 0).toLocaleString()}</div><div class="metric-note">Excluded from formal cohorts</div></div><div class="metric"><div class="metric-label">Responses</div><div class="metric-value">${Number(result.responses?.total || 0).toLocaleString()}</div><div class="metric-note">Aggregate only</div></div></div></section>`;
}
async function loadPublicOverview() {
  const result = await publicApi("/api/public/analytics");
  const metrics = [
    ["Company prospects", result.companies || 0, "Operational company records"],
    ["Hotel prospects", result.hotels || 0, "Operational hotel records"],
    ["Owner-controlled tests", result.owner_controlled_tests || 0, "Excluded from formal cohorts"],
    ["Aggregate responses", result.responses?.total || 0, "Individual responses remain protected"],
  ];
  const host = $("metrics");
  if (host) host.innerHTML = metrics.map(([label, value, note]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${Number(value).toLocaleString()}</div><div class="metric-note">${esc(note)}</div></div>`).join("");
  document.querySelectorAll('#page-overview a[href^="/api/"]').forEach((link) => link.remove());
  const notice = document.querySelector('#page-overview .candidate-scale-overview');
  if (notice) notice.innerHTML = '<div class="public-readonly-notice"><strong>Operational registry view</strong><span>Use Companies, Hotels, and Research Queue for safe public browsing. Qualification evidence, contacts, invitations, consent, and exports remain protected.</span></div>';
}
async function loadPublicCampaigns() {
  const result = await publicApi("/api/public/campaigns"); const host = $("page-campaigns");
  host.innerHTML = `<div class="page-heading"><div><p class="eyebrow">OUTREACH STATUS</p><h1>Campaigns</h1><p>Public campaign status only. Sending, approvals, recipients, and invitation content remain protected.</p></div></div><section class="panel"><div class="public-readonly-notice"><strong>Protected workflow controls hidden</strong><span>Anonymous visitors can see high-level status but cannot create, approve, export, or send campaigns.</span></div><div class="campaign-list">${(result.campaigns || []).map(c => `<article class="campaign-card"><strong>${esc(displayCampaignName(c.name))}</strong><small>${esc(compact(c.status))} · ${esc(c.company_count || 0)} selected · ${esc(c.simulated_deliveries || 0)} test deliveries</small></article>`).join("") || `<div class="empty-state"><h3>No campaign status to display</h3><p>Live sending is disabled.</p></div>`}</div></section>`;
}
function navigate() {
  const requested = location.hash.slice(1) || (state.summary?.cloud_registry ? "research-queue" : "overview");
  const page = pages.includes(requested) ? requested : "overview";
  document.querySelectorAll("[data-view]").forEach((view) => { view.hidden = view.dataset.view !== page; });
  document.querySelectorAll("[data-page]").forEach((link) => { const active = link.dataset.page === page; link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  document.title = `${page.replaceAll("-", " ").replace(/\b\w/g, (c) => c.toUpperCase())} · GEO Research Outreach Platform`;
  if (state.dashboardToken) {
    if (page === "responses") renderResponsesFromApi().catch(showError);
    if (page === "research-queue") reloadResearchQueue().catch(showError);
    if (page === "interest-form") renderFormTokens(state.campaignSummaries).catch(showError);
    return;
  }
  if (page === "companies") loadPublicDirectory(page, "business_company").catch(showError);
  else if (page === "hotels") loadPublicDirectory(page, "hotel_property").catch(showError);
  else if (page === "research-queue") reloadResearchQueue().catch(showError);
  else if (page === "responses") loadPublicResponses().catch(showError);
  else if (page === "analytics") loadPublicAnalytics().catch(showError);
  else if (page === "campaigns") loadPublicCampaigns().catch(showError);
  else if (page === "interest-form") renderPublicRestricted(page, "Participation", "The actual questionnaire is handled through the secure invitation workflow; local test controls are not public.");
  else if (page === "settings") renderPublicRestricted(page, "Settings", "Configuration and administrative controls require protected operator access.");
  if (page === "research-queue") {
    const exportLink = document.querySelector('#page-research-queue a[href="/api/research/records.csv"]');
    if (exportLink) exportLink.remove();
  }
}
start().catch(showError);
