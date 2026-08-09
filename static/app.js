const DEFAULT_GOAL = "帮我为一家本地服务企业制作一条面向潜在客户的竖屏短视频。";

const state = {
  config: null,
  status: null,
  tools: [],
  jobs: [],
  catalog: null,
  hardware: null,
  csrf: "",
  topicCandidates: [],
  topicResponse: null,
  selectedTopicIndex: 0,
  seenTopics: [],
  currentGoal: DEFAULT_GOAL,
  homeJobId: sessionStorage.getItem("shiyi_home_job_id"),
  selectedJob: null,
  reviewFiles: {},
  busyJobs: new Set(),
  pollTimer: null,
  pollJobId: null,
  pollFailures: 0,
  pollInFlight: false,
  auxiliaryRefresh: null,
};

const statusLabels = {
  planned: "待执行授权",
  authorized: "已授权，待研究",
  research_running: "研究执行中",
  awaiting_research_approval: "待审查研究证据",
  awaiting_research_revision: "研究已退回",
  research_approved: "研究已确认",
  content_running: "脚本生成中",
  blocked_compliance: "合规阻断",
  awaiting_compliance_approval: "待审查最终脚本",
  awaiting_script_revision: "待人工改稿",
  compliance_approved: "脚本已确认",
  rendering: "成片装配中",
  complete: "已完成",
  failed: "运行失败",
  legacy_read_only: "旧任务（只读）",
  needs_attention: "等待适配器",
};

const runnableStates = new Set(["authorized", "research_approved", "compliance_approved", "failed", "awaiting_research_revision"]);
const runningStates = new Set(["research_running", "content_running", "rendering"]);
const statusProgress = {
  planned: 6, authorized: 12, research_running: 26, awaiting_research_approval: 38,
  awaiting_research_revision: 34, research_approved: 43, content_running: 58,
  blocked_compliance: 65, awaiting_compliance_approval: 72, awaiting_script_revision: 68,
  compliance_approved: 78, rendering: 90, complete: 100, failed: 52, needs_attention: 52,
};
const artifactLabels = {
  "research.json": "研究证据", "insight.json": "内容洞察", "script_variants.json": "脚本方案",
  "approved_script.json": "最终脚本", "review.json": "合规检查", "voice.wav": "配音",
  "captions.srt": "字幕", "motion_plan.json": "动态导演计划", "final.mp4": "成片",
  "run_report.json": "运行报告", "manifest.json": "哈希清单", "approvals.json": "审批记录",
};
const PENDING_CREATE_STORAGE = "shiyi_pending_agent_create";
const EMPTY_RESEARCH_APPROVAL_NOTE = "本次确认无可采信 finding；后续仅允许使用不含行业事实主张的本地安全模板";
const AGENT_TEST_REVIEWER = "Codex 测试代理";
const AGENT_RESEARCH_NOTE = "Codex 测试代理已逐项核对来源、严格审核结论与允许使用范围；仅用于受控测试。";
const AGENT_COMPLIANCE_NOTE = "Codex 测试代理已核对最终脚本、合规结果与审批哈希；仅用于受控测试。";
const POLL_BASE_DELAY_MS = 1000;
const POLL_MAX_DELAY_MS = 8000;

function isEmptyLocalResearch(research) {
  const findings = Array.isArray(research?.findings) ? research.findings : [];
  return ["offline", "disabled"].includes(research?.status) && findings.length === 0;
}

function reviewPolicyForJob(job = null) {
  const formalDefault = {
    stage_review_mode: "human",
    final_human_acceptance_required: false,
  };
  if (job) return job.review_policy || formalDefault;
  return state.status?.review_policy || formalDefault;
}

function isAgentTestReview(job = null) {
  return reviewPolicyForJob(job).stage_review_mode === "agent_test";
}

function reviewerForJob(job = null) {
  return isAgentTestReview(job) ? AGENT_TEST_REVIEWER : "本机会话用户";
}

async function bootstrapSession() {
  const response = await fetch("/api/session", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error("无法建立本机会话");
  state.csrf = (await response.json()).csrf_token;
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD"].includes(method)) {
    headers.set("Content-Type", "application/json");
    headers.set("X-Shiyi-CSRF", state.csrf);
  }
  let response;
  try {
    response = await fetch(path, { ...options, method, headers, credentials: "same-origin", cache: "no-store" });
  } catch (cause) {
    const error = new Error("网络连接中断，暂时无法确认请求是否已经送达");
    error.networkUncertain = true;
    error.cause = cause;
    throw error;
  }
  const data = await response.json().catch(() => ({ error: { message: `HTTP ${response.status}` } }));
  if (!response.ok) {
    const seconds = data.error?.details?.estimated_seconds;
    const error = new Error(`${data.error?.message || `HTTP ${response.status}`}${seconds ? `（预计 ${seconds} 秒）` : ""}`);
    error.httpStatus = response.status;
    error.networkUncertain = false;
    throw error;
  }
  return data;
}

async function readJsonArtifact(url) {
  let response;
  try {
    response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
  } catch (cause) {
    const error = new Error("产物暂时无法读取，稍后会自动重试");
    error.networkUncertain = true;
    error.cause = cause;
    throw error;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.error?.message || `HTTP ${response.status}`);
    error.httpStatus = response.status;
    error.networkUncertain = false;
    throw error;
  }
  const bytes = await response.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  return { data: JSON.parse(new TextDecoder().decode(bytes)), sha256 };
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function toast(message, isError = false) {
  const target = document.getElementById("toast");
  target.textContent = message;
  target.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(target._timer);
  target._timer = setTimeout(() => { target.className = "toast"; }, 3800);
}

function setBusy(button, busy, text) {
  if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
  button.disabled = busy;
  button.innerHTML = busy ? escapeHtml(text) : button.dataset.originalHtml;
}

function syncSelectedJobBusyState() {
  const button = document.getElementById("rerunJobBtn");
  if (!button || !state.selectedJob) return;
  button.disabled = state.selectedJob.legacy_read_only
    || !runnableStates.has(state.selectedJob.status)
    || state.busyJobs.has(state.selectedJob.id);
}

function beginJobBusy(jobId) {
  if (state.busyJobs.has(jobId)) return false;
  state.busyJobs.add(jobId);
  renderJobs();
  syncSelectedJobBusyState();
  return true;
}

function endJobBusy(jobId) {
  state.busyJobs.delete(jobId);
  // A refresh may have rendered disabled controls while the request was still
  // in flight. Render once more after finally so those controls cannot remain
  // visually stuck.
  renderJobs();
  syncSelectedJobBusyState();
}

function statusClass(status) {
  if (["complete", "research_approved", "compliance_approved"].includes(status)) return "status-ok";
  if (["failed", "blocked_compliance", "awaiting_script_revision", "awaiting_research_revision"].includes(status)) return "status-danger";
  if (runningStates.has(status)) return "status-running";
  return "status-waiting";
}

function newIdempotencyKey() {
  return `ui-${crypto.randomUUID()}`;
}

function pendingCreateKey(fingerprint) {
  try {
    const pending = JSON.parse(sessionStorage.getItem(PENDING_CREATE_STORAGE) || "null");
    if (pending?.fingerprint === fingerprint && typeof pending.key === "string") return pending.key;
  } catch (_) { /* replace malformed local state */ }
  const key = newIdempotencyKey();
  sessionStorage.setItem(PENDING_CREATE_STORAGE, JSON.stringify({ fingerprint, key }));
  return key;
}

async function approveWithRecovery(job) {
  try {
    return await api(`/api/jobs/${job.id}/approve`, { method: "POST", body: "{}" });
  } catch (error) {
    if (!error.networkUncertain) throw error;
    return api(`/api/jobs/${job.id}`);
  }
}

function nowLabel() {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date());
}

function setHomeJobId(value) {
  state.homeJobId = value || null;
  if (state.homeJobId) sessionStorage.setItem("shiyi_home_job_id", state.homeJobId);
  else sessionStorage.removeItem("shiyi_home_job_id");
}

function switchView(viewName) {
  const selected = document.getElementById(`view-${viewName}`) ? viewName : "workbench";
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${selected}`));
  document.getElementById("pageTitle").textContent = selected === "workbench" ? "Agent 内容工作台" : document.querySelector(`#view-${selected} h2`)?.textContent || "时宜 Agent 内容工厂";
  document.body.dataset.view = selected;
  closeMenu();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openMenu() {
  const button = document.getElementById("appMenuButton");
  const menu = document.getElementById("appMenu");
  const opening = menu.hidden;
  menu.hidden = !opening;
  button.setAttribute("aria-expanded", String(opening));
}

function closeMenu() {
  document.getElementById("appMenu").hidden = true;
  document.getElementById("appMenuButton").setAttribute("aria-expanded", "false");
}

function refreshAuxiliary() {
  if (state.auxiliaryRefresh) return state.auxiliaryRefresh;
  const pending = Promise.allSettled([
    api("/api/config"), api("/api/tools"), api("/api/catalog"), api("/api/hardware"),
  ]).then(([config, toolsData, catalog, hardware]) => {
    if (config.status === "fulfilled") {
      state.config = config.value;
      renderSettings();
    }
    if (toolsData.status === "fulfilled") {
      state.tools = toolsData.value.tools || [];
      renderTools(toolsData.value);
    }
    if (catalog.status === "fulfilled") {
      state.catalog = catalog.value;
      renderCatalog();
    }
    if (hardware.status === "fulfilled") {
      state.hardware = hardware.value;
      if (state.catalog) renderCatalog();
    }
  });
  state.auxiliaryRefresh = pending.finally(() => {
    state.auxiliaryRefresh = null;
  });
  return state.auxiliaryRefresh;
}

async function refresh({ syncHomeView = true } = {}) {
  // Jobs and status drive every visible transition. Auxiliary settings and
  // discovery cards update independently and may not hold the workbench hostage.
  const auxiliary = refreshAuxiliary();
  const [status, jobsData] = await Promise.all([api("/api/status"), api("/api/jobs")]);
  Object.assign(state, { status, jobs: jobsData.jobs || [] });
  renderStatus();
  renderJobs();
  renderLatestArtifact();
  if (syncHomeView) await syncHome();
  auxiliary.catch(() => {});
}

function renderStatus() {
  const providerState = state.status?.provider_state
    || (state.status?.provider_connection_verified ? "verified" : state.status?.provider_configured || state.status?.provider_ready ? "configured" : "unconfigured");
  const labels = {
    unconfigured: "DeepSeek · 未配置",
    configured: "DeepSeek · Key 已就绪",
    verified: "DeepSeek · 本次连接已验证",
  };
  const button = document.getElementById("providerQuickButton");
  document.getElementById("providerBadge").textContent = labels[providerState] || labels.unconfigured;
  button.classList.toggle("configured", providerState === "configured");
  button.classList.toggle("verified", providerState === "verified");
  button.title = providerState === "verified"
    ? "当前本机会话已完成 Provider 连通性测试"
    : providerState === "configured"
      ? "已检测到 Key，尚未验证当前会话连接"
      : "尚未配置 DeepSeek API Key";
  const releaseVersion = state.status?.version || "0.3.0";
  const engines = state.status?.production_engines || {};
  const motion = engines.motion || state.status?.production_engine || {};
  const footage = engines.footage || {};
  const healthLabels = {
    ready: "可用",
    configured_unverified: "已配置，未通过启动校验",
    unavailable: "不可用",
    disabled: "未启用",
    misconfigured: "配置异常",
  };
  const engineLabel = (summary, fallbackName) => {
    const name = summary?.name || fallbackName;
    const version = summary?.version ? ` ${summary.version}` : "";
    return `${name}${version} · ${healthLabels[summary?.health] || summary?.health || "状态未知"}`;
  };
  const motionLabel = engineLabel(motion, "HyperFrames");
  const footageLabel = engineLabel(footage, "MoneyPrinterTurbo");
  const footageReady = footage?.enabled === true && footage?.health === "ready";
  const footageInput = document.getElementById("productionModeFootage");
  footageInput.disabled = !footageReady;
  if (!footageReady && footageInput.checked) document.getElementById("productionModeMotion").checked = true;
  document.getElementById("motionEngineStatus").textContent = motionLabel;
  document.getElementById("footageEngineStatus").textContent = footageReady ? footageLabel : `${footageLabel}，暂不可选`;
  document.getElementById("releaseVersionState").textContent = `v${releaseVersion}`;
  document.getElementById("motionVersionState").textContent = motionLabel;
  document.getElementById("footageVersionState").textContent = footageLabel;
  document.getElementById("editionBadge").textContent = `v${releaseVersion}`;
  document.getElementById("portBadge").textContent = `${location.hostname}:${location.port || "80"} · v${releaseVersion}`;
  document.getElementById("metricTools").textContent = state.status?.tool_count || 0;
  document.getElementById("metricCaps").textContent = state.status?.capabilities?.length || 0;
  document.getElementById("metricJobs").textContent = state.status?.job_count || 0;
  document.getElementById("metricModel").textContent = state.status?.model || "deepseek";
}

async function loadTopics(goal = state.currentGoal, { resetSeen = false } = {}) {
  const normalized = String(goal || "").trim();
  const reusablePack = !resetSeen && state.topicResponse?.goal === normalized
    ? state.topicResponse.capability_pack
    : null;
  if (resetSeen) {
    state.seenTopics = [];
    state.topicResponse = null;
  }
  state.currentGoal = normalized;
  setHomeJobId(null);
  document.getElementById("userGoalText").textContent = normalized;
  document.getElementById("userMessageTime").textContent = nowLabel();
  document.getElementById("goalInput").value = normalized;
  document.getElementById("topicChoicePanel").hidden = true;
  document.getElementById("activeJobPanel").hidden = true;
  document.getElementById("topicLoading").hidden = false;
  try {
    const result = await api("/api/agent/topics", {
      method: "POST",
      body: JSON.stringify({
        goal: normalized,
        excluded_topics: state.seenTopics.slice(-24),
        ...(reusablePack ? { capability_pack: reusablePack } : {}),
      }),
    });
    state.topicCandidates = result.candidates || [];
    state.topicResponse = result;
    state.selectedTopicIndex = 0;
    state.seenTopics.push(...state.topicCandidates.map(item => item.title));
    state.seenTopics = [...new Set(state.seenTopics)].slice(-24);
    renderTopicChoices(result);
    if (result.notice) toast(result.notice);
  } finally {
    document.getElementById("topicLoading").hidden = true;
  }
}

function renderTopicChoices(result = state.topicResponse || {}) {
  const panel = document.getElementById("topicChoicePanel");
  panel.hidden = false;
  document.getElementById("activeJobPanel").hidden = true;
  const source = result.source === "deepseek_bootstrap"
    ? "DeepSeek 动态能力包 Agent"
    : result.source === "deepseek"
    ? "DeepSeek Agent"
    : result.source === "deepseek_filtered_with_local_fallback"
      ? "DeepSeek + 本地安全 Agent"
      : "本地安全 Agent";
  const review = result.capability_review;
  const reviewLabels = { passed: "反证审核通过，仅允许进入研究，不代表事实已证实", needs_revision: "反证审核需要修改，当前动态能力包未放行", blocked: "反证审核已阻止，当前内容不得进入研究" };
  const verdictLabels = { usable_limited: "可有限使用", needs_evidence: "需要补充证据", rejected: "已拒绝" };
  const reviewParts = [];
  if (review && reviewLabels[review.status]) {
    reviewParts.push(reviewLabels[review.status]);
    const issues = Array.isArray(review.issues) ? review.issues.filter(item => typeof item === "string" && item.trim()).slice(0, 2) : [];
    const scopes = Array.isArray(review.safe_scope) ? review.safe_scope.filter(item => typeof item === "string" && item.trim()).slice(0, 2) : [];
    if (issues.length) reviewParts.push(`原因：${issues.join("；")}`);
    if (scopes.length) reviewParts.push(`允许范围：${scopes.join("；")}`);
    // A non-passed review describes the rejected Provider subjects, while the
    // buttons below are new local replacements. Keep those explanations global
    // so identical topic IDs cannot visually bind them to the replacement list.
    const candidateNotes = review.status === "passed" && Array.isArray(review.candidate_verdicts) ? review.candidate_verdicts.slice(0, 2).map(item => {
      const reasons = Array.isArray(item?.reasons) ? item.reasons.filter(reason => typeof reason === "string" && reason.trim()).slice(0, 1) : [];
      const scope = typeof item?.safe_scope === "string" ? item.safe_scope.trim() : "";
      const title = typeof item?.candidate_title === "string" ? item.candidate_title.trim() : "";
      const detail = [reasons[0], scope ? `范围：${scope}` : ""].filter(Boolean).join("；");
      return detail && title ? `被审核候选“${title}”${verdictLabels[item?.verdict] || "待核验"}：${detail}` : "";
    }).filter(Boolean) : [];
    if (candidateNotes.length) reviewParts.push(candidateNotes.join("；"));
  }
  reviewParts.push(result.screening || "已排除危险目标、无依据承诺和重复选题；公开依据将在研究阶段逐条核验。");
  document.getElementById("topicScreening").innerHTML = `<img class="ui-icon" src="/icons/shield-check.svg" alt="">${escapeHtml(reviewParts.join("。"))}<span class="sr-only">${source}</span>`;
  document.getElementById("topicCandidates").innerHTML = state.topicCandidates.map((item, index) => `
    <button class="topic-option${index === state.selectedTopicIndex ? " selected" : ""}" type="button" role="radio" aria-checked="${index === state.selectedTopicIndex}" data-topic-index="${index}">
      <span class="topic-index">${String(index + 1).padStart(2, "0")}</span>
      <span class="topic-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.reason)}</span></span>
      <span class="radio-mark" aria-hidden="true"></span>
    </button>`).join("");
  document.getElementById("processLabel").textContent = "选题 → 证据 → 脚本 → 合规 → 成片";
  document.getElementById("processBudget").textContent = "待选择";
  const packLabel = result.context?.industry_pack_label || "动态行业能力包";
  document.getElementById("processDetails").innerHTML = `<p>${source} 已现场生成“${escapeHtml(packLabel)}”并给出三个候选角度。选中后，这一次确认同时记录为任务执行授权。</p>`;
}

async function startSelectedTopic() {
  const candidate = state.topicCandidates[state.selectedTopicIndex];
  if (!candidate) throw new Error("请先选择一个角度");
  const button = document.getElementById("startSelectedTopic");
  setBusy(button, true, "正在建立任务…");
  document.getElementById("topicChoicePanel").hidden = true;
  const active = document.getElementById("activeJobPanel");
  active.hidden = false;
  active.innerHTML = renderRunningCard(candidate.title, "正在建立任务并记录执行授权…", 10);
  let job = null;
  try {
    const createPayload = {
      selection_bundle_id: state.topicResponse?.selection_bundle_id,
      candidate_id: candidate.id,
      production_options: {
        target_duration_seconds: 52,
        pattern_card_ids: [],
        voice_engine: "voxcpm2",
        production_mode: document.querySelector('input[name="productionMode"]:checked')?.value === "footage"
          && !document.getElementById("productionModeFootage").disabled ? "footage" : "motion",
        enable_web_research: true,
      },
    };
    if (!createPayload.selection_bundle_id) throw new Error("选题凭证已失效，请重新生成三个候选");
    const body = JSON.stringify(createPayload);
    const fingerprint = JSON.stringify(createPayload);
    const createKey = pendingCreateKey(fingerprint);
    const createOnce = () => api("/api/demo-job", {
      method: "POST",
      headers: { "Idempotency-Key": createKey },
      body,
    });
    try {
      job = await createOnce();
    } catch (error) {
      if (!error.networkUncertain) throw error;
      job = await createOnce();
    }
    sessionStorage.removeItem(PENDING_CREATE_STORAGE);
    setHomeJobId(job.id);
    job = await approveWithRecovery(job);
    await renderHomeJob(job);
    if (job.status === "planned") {
      toast("任务已保留，但暂时无法确认授权结果；请点“授权并开始”继续。", true);
      return;
    }
    if (job.status === "authorized") await advanceJob(job.id);
  } catch (error) {
    if (job?.id) {
      setHomeJobId(job.id);
      const recovered = await api(`/api/jobs/${job.id}`).catch(() => job);
      await refresh({ syncHomeView: false }).catch(() => {});
      await renderHomeJob(recovered);
    } else {
      document.getElementById("topicChoicePanel").hidden = false;
      active.hidden = true;
    }
    throw error;
  } finally {
    setBusy(button, false);
  }
}

function renderRunningCard(title, message, progress, job = null) {
  const pauseText = isAgentTestReview(job)
    ? "Agent 会在两道测试审查门禁停下，由 Codex 通过浏览器检查并推进。"
    : "Agent 会在需要你本人确认时停下来。";
  return `<div class="job-chat-head"><div><h2>${escapeHtml(title)}</h2><p>${pauseText}</p></div><span class="status-pill status-running">处理中</span></div>
    <div class="progress-block"><div class="progress-copy"><span>${escapeHtml(message)}</span><b class="mono">${progress}%</b></div><div class="progress-track"><span class="progress-value progress-${progress}"></span></div></div>`;
}

async function advanceJob(id, { busyAlready = false } = {}) {
  const ownsBusy = !busyAlready;
  if (ownsBusy && !beginJobBusy(id)) return;
  // A new user action is a new logical attempt. Only an automatic replay of
  // job creation is allowed. A long /run is posted exactly once and observed
  // through GET polling, even if its response becomes network-uncertain.
  const requestKey = newIdempotencyKey();
  try {
    const current = state.jobs.find(item => item.id === id) || await api(`/api/jobs/${id}`);
    document.getElementById("activeJobPanel").innerHTML = renderRunningCard(current.production_input?.topic || current.id, "Agent 正在推进当前阶段…", statusProgress[current.status] || 20);
    schedulePoll(id, { immediate: true, reset: true });
    const runOnce = () => api(`/api/jobs/${id}/run`, {
      method: "POST",
      headers: { "Idempotency-Key": requestKey },
      body: "{}",
    });
    let job;
    try {
      job = await runOnce();
    } catch (error) {
      if (!error.networkUncertain) throw error;
      // Do not emit a second POST. The server may already be working; recover
      // current truth through the read-only job endpoint while polling remains active.
      job = await api(`/api/jobs/${id}`);
    }
    await refresh({ syncHomeView: false });
    await renderHomeJob(job);
    if (state.selectedJob?.id === id && document.body.dataset.view === "jobs") {
      await openJob(id, { job, scroll: false });
    }
  } catch (error) {
    await refresh({ syncHomeView: false }).catch(() => {});
    const job = state.jobs.find(item => item.id === id);
    if (job) {
      await renderHomeJob(job);
      if (state.selectedJob?.id === id && document.body.dataset.view === "jobs") {
        await openJob(id, { job, scroll: false });
      }
    }
    throw error;
  } finally {
    if (ownsBusy) endJobBusy(id);
    else {
      renderJobs();
      syncSelectedJobBusyState();
    }
  }
}

function findHomeJob() {
  if (state.homeJobId) {
    const selected = state.jobs.find(item => item.id === state.homeJobId);
    if (selected) return selected;
    setHomeJobId(null);
  }
  return null;
}

async function syncHome() {
  const job = findHomeJob();
  if (job) {
    setHomeJobId(job.id);
    await renderHomeJob(job);
    return;
  }
  if (!state.topicCandidates.length) await loadTopics(state.currentGoal, { resetSeen: false });
  else renderTopicChoices();
}

function stopPoll(jobId = null) {
  if (jobId && state.pollJobId && state.pollJobId !== jobId) return;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.pollJobId = null;
  state.pollFailures = 0;
}

function schedulePoll(jobId, { immediate = false, reset = false } = {}) {
  if (reset || state.pollJobId !== jobId) {
    state.pollFailures = 0;
    state.pollJobId = jobId;
  }
  clearTimeout(state.pollTimer);
  const delay = immediate
    ? 0
    : Math.min(POLL_BASE_DELAY_MS * (2 ** Math.max(0, state.pollFailures - 1)), POLL_MAX_DELAY_MS);
  state.pollTimer = setTimeout(async () => {
    if (state.pollJobId !== jobId) return;
    if (state.pollInFlight) {
      schedulePoll(jobId);
      return;
    }
    state.pollInFlight = true;
    let keepPolling = true;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      const index = state.jobs.findIndex(item => item.id === jobId);
      if (index >= 0) state.jobs[index] = job;
      else state.jobs.unshift(job);
      renderJobs();
      const artifactPending = state.homeJobId === jobId
        ? await renderHomeJob(job, { managePolling: false })
        : false;
      const detailArtifactPending = state.selectedJob?.id === jobId && document.body.dataset.view === "jobs"
        ? await openJob(jobId, { job, scroll: false, managePolling: false })
        : false;
      state.pollFailures = 0;
      keepPolling = artifactPending || detailArtifactPending || runningStates.has(job.status) || state.busyJobs.has(jobId);
    } catch (error) {
      state.pollFailures = Math.min(state.pollFailures + 1, 4);
      if (state.pollFailures === 1) toast("状态刷新暂时中断，正在自动重试", true);
      keepPolling = true;
    } finally {
      state.pollInFlight = false;
      if (state.pollJobId !== jobId) return;
      if (keepPolling) schedulePoll(jobId);
      else stopPoll(jobId);
    }
  }, delay);
}

async function renderHomeJob(job, { managePolling = true } = {}) {
  const storedIndex = state.jobs.findIndex(item => item.id === job.id);
  if (storedIndex >= 0) state.jobs[storedIndex] = job;
  else state.jobs.unshift(job);
  setHomeJobId(job.id);
  document.getElementById("topicChoicePanel").hidden = true;
  document.getElementById("topicLoading").hidden = true;
  const panel = document.getElementById("activeJobPanel");
  panel.hidden = false;
  const title = job.production_input?.topic || job.plan?.goal || job.id;
  const status = job.status;
  const agentTestReview = isAgentTestReview(job);
  const progress = statusProgress[status] ?? 18;
  document.getElementById("processBudget").textContent = `请求 ${job.budget?.attempted || 0} / ${job.budget?.limit || 7}`;
  document.getElementById("processDetails").innerHTML = `<p>当前状态：${escapeHtml(statusLabels[status] || status)}。失败重跑不会覆盖上一份成功产物；详细证据、脚本和哈希仍可在任务记录中查看。</p>`;

  if (runningStates.has(status)) {
    panel.innerHTML = renderRunningCard(title, statusLabels[status], progress, job);
    if (managePolling) schedulePoll(job.id);
    return true;
  }

  const head = `<div class="job-chat-head"><div><h2>${escapeHtml(title)}</h2><p>其余步骤由 Agent 自动完成。</p></div><span class="status-pill ${statusClass(status)}">${escapeHtml(statusLabels[status] || status)}</span></div>`;
  if (status === "awaiting_research_approval") {
    let research;
    try {
      research = await readJsonArtifact(`/api/jobs/${job.id}/review-artifacts/research.json`);
    } catch (_) {
      panel.innerHTML = `${head}<div class="gate-card"><h3>研究证据正在发布</h3><p>任务状态已经更新，证据文件尚未就绪；页面会自动重试，无需再次点击。</p></div>`;
      if (managePolling) schedulePoll(job.id);
      return true;
    }
    state.reviewFiles.research = research;
    const findings = research.data.findings || [];
    const eligible = findings.filter(item => item.auto_review_status === "eligible").length;
    const rejected = findings.length - eligible;
    if (isEmptyLocalResearch(research.data)) {
      panel.innerHTML = `${head}<div class="gate-card"><h3>本次没有可采信的外部证据</h3><div class="gate-stats"><span><b>0</b> 条可用 finding</span><span><b>本地安全模板</b></span></div><p>${escapeHtml(EMPTY_RESEARCH_APPROVAL_NOTE)}</p><div class="gate-actions"><button class="primary" type="button" data-home-action="${agentTestReview ? "show-details" : "approve-research"}">${agentTestReview ? "进入代理测试审查" : "确认边界并继续"}</button><button class="quiet-link" type="button" data-home-action="show-details">查看研究记录</button></div>${agentTestReview ? '<p class="reply-hint">测试代理必须先查看记录再提交，不会静默批准。</p>' : '<p class="reply-hint">也可以直接回复：继续</p>'}</div>`;
    } else if (!eligible) {
      panel.innerHTML = `${head}<div class="gate-card"><h3>研究结果不能进入下一步</h3><div class="gate-stats"><span><b>0</b> 条可用</span><span><b>${rejected}</b> 条已否决</span></div><p>本次不是明确的离线/禁用调研，不能用空审批绕过研究门禁。请查看原因并退回研究。</p><div class="gate-actions"><button class="quiet-link" type="button" data-home-action="show-details">查看原因</button></div></div>`;
    } else {
      panel.innerHTML = `${head}<div class="gate-card"><h3>研究证据已反向核验</h3><div class="gate-stats"><span><b>${eligible}</b> 条可用</span><span><b>${rejected}</b> 条已否决</span></div><p>脚本只会使用本次阶段审查确认、且能够回到原始来源的内容。</p><div class="gate-actions"><button class="primary" type="button" data-home-action="${agentTestReview ? "show-details" : "approve-research"}">${agentTestReview ? "进入代理测试审查" : "继续制作"}</button><button class="quiet-link" type="button" data-home-action="show-details">查看依据</button></div>${agentTestReview ? '<p class="reply-hint">测试代理必须逐项查看后提交，不会静默批准。</p>' : '<p class="reply-hint">也可以直接回复：继续</p>'}</div>`;
    }
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (["awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision"].includes(status)) {
    let review = null;
    let script = null;
    try {
      review = await readJsonArtifact(`/api/jobs/${job.id}/review-artifacts/review.json`);
      script = await readJsonArtifact(`/api/jobs/${job.id}/review-artifacts/approved_script.json`);
      state.reviewFiles.review = review;
      state.reviewFiles.script = script;
    } catch (_) {
      if (status === "awaiting_compliance_approval") {
        panel.innerHTML = `${head}<div class="gate-card"><h3>脚本与合规结果正在发布</h3><p>任务状态已经更新，审核文件尚未就绪；页面会自动重试，无需再次点击。</p></div>`;
        if (managePolling) schedulePoll(job.id);
        return true;
      }
      /* detailed view will surface a missing review artifact */
    }
    const blocked = !review || review.data.status === "blocked" || review.data.blocked || status !== "awaiting_compliance_approval";
    const warnings = review?.data?.warnings?.map(item => item.message).filter(Boolean).join("；") || "脚本仍需人工修改或重新检查。";
    const approvalAction = agentTestReview ? "show-details" : "approve-compliance";
    const approvalLabel = agentTestReview ? "进入代理测试审查" : "确认脚本并渲染";
    const readyText = agentTestReview
      ? "没有发现阻断项。Codex 测试代理查看脚本与合规依据后，才可提交测试审查。"
      : "没有发现阻断项。你确认后，Agent 将直接开始配音和成片装配。";
    panel.innerHTML = `${head}<div class="gate-card"><h3>${blocked ? "最终脚本暂时不能放行" : "最终脚本已通过自动合规检查"}</h3><p>${escapeHtml(blocked ? warnings : readyText)}</p><div class="gate-actions">${blocked ? "" : `<button class="primary" type="button" data-home-action="${approvalAction}">${approvalLabel}</button>`}<button class="quiet-link" type="button" data-home-action="show-details">${blocked ? "打开脚本修改" : "查看脚本与合规依据"}</button></div>${blocked || agentTestReview ? "" : '<p class="reply-hint">也可以直接回复：继续</p>'}</div>`;
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (status === "complete") {
    panel.innerHTML = `${head}<div class="gate-card"><h3>${agentTestReview ? "测试成片已经完成，等待用户最终验收" : "成片已经完成"}</h3><p>视频、证据清单、审查记录和哈希都已发布到本次成功运行；失败尝试没有覆盖它。${agentTestReview ? " 两道阶段门禁记录为代理测试审查，不冒充用户签署。" : ""}</p><div class="gate-actions"><button class="primary" type="button" data-home-action="play-latest">播放最新成片</button><button class="quiet-link" type="button" data-home-action="new-task">再做一条</button></div></div>`;
    renderLatestArtifact();
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (status === "planned") {
    panel.innerHTML = `${head}<div class="gate-card"><h3>已选好角度</h3><p>点击一次即可同时记录任务执行授权，并开始研究。</p><div class="gate-actions"><button class="primary" type="button" data-home-action="authorize">授权并开始</button></div></div>`;
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (runnableStates.has(status)) {
    panel.innerHTML = `${head}${job.last_error ? `<div class="error-card">${escapeHtml(job.last_error)}</div>` : ""}<div class="gate-actions"><button class="primary" type="button" data-home-action="advance">${status === "failed" ? "重试当前步骤" : "继续处理"}</button><button class="quiet-link" type="button" data-home-action="show-details">查看详情</button></div>`;
    if (managePolling) stopPoll(job.id);
    return false;
  }
  panel.innerHTML = `${head}${job.last_error ? `<div class="error-card">${escapeHtml(job.last_error)}</div>` : ""}<div class="gate-actions"><button class="quiet-link" type="button" data-home-action="show-details">打开任务记录</button><button class="quiet-link" type="button" data-home-action="new-task">返回选题</button></div>`;
  if (managePolling) stopPoll(job.id);
  return false;
}

async function authorizeHomeJob(jobId, { busyAlready = false } = {}) {
  const ownsBusy = !busyAlready;
  if (ownsBusy && !beginJobBusy(jobId)) return;
  try {
    const job = await approveWithRecovery({ id: jobId });
    await renderHomeJob(job);
    if (job.status === "planned") {
      toast("暂时无法确认授权结果，任务已保留；请稍后再点一次。", true);
      return;
    }
    if (job.status === "authorized") await advanceJob(jobId, { busyAlready: true });
  } finally {
    if (ownsBusy) endJobBusy(jobId);
  }
}

async function approveHomeResearch(jobId) {
  const job = state.jobs.find(item => item.id === jobId) || state.selectedJob;
  if (isAgentTestReview(job)) {
    await openJob(jobId);
    toast("请由 Codex 测试代理查看逐项依据后提交审查");
    return;
  }
  const research = state.reviewFiles.research || await readJsonArtifact(`/api/jobs/${jobId}/review-artifacts/research.json`);
  const findings = (research.data.findings || []).filter(item => item.auto_review_status === "eligible").map(item => ({
    finding_id: String(item.finding_id), decision: "approved", evidence_type: "paraphrase", note: "通过首页单一确认采用安全转述",
  }));
  const emptyLocalResearch = isEmptyLocalResearch(research.data);
  if (!findings.length && !emptyLocalResearch) throw new Error("没有可批准的证据，请打开任务记录查看严格审核结果");
  await api(`/api/jobs/${jobId}/approvals/research`, { method: "POST", body: JSON.stringify({
    decision: "approved", reviewer: reviewerForJob(job), note: emptyLocalResearch ? EMPTY_RESEARCH_APPROVAL_NOTE : "通过 Agent 首页确认继续制作", artifact_sha256: research.sha256, findings,
  }) });
  toast("研究证据已确认，Agent 继续写稿");
  await refresh({ syncHomeView: false });
  await advanceJob(jobId);
}

async function approveHomeCompliance(jobId) {
  const job = state.jobs.find(item => item.id === jobId) || state.selectedJob;
  if (isAgentTestReview(job)) {
    await openJob(jobId);
    toast("请由 Codex 测试代理查看脚本和合规依据后提交审查");
    return;
  }
  const review = state.reviewFiles.review || await readJsonArtifact(`/api/jobs/${jobId}/review-artifacts/review.json`);
  const script = state.reviewFiles.script || await readJsonArtifact(`/api/jobs/${jobId}/review-artifacts/approved_script.json`);
  await api(`/api/jobs/${jobId}/approvals/compliance`, { method: "POST", body: JSON.stringify({
    decision: "approved", reviewer: reviewerForJob(job), note: "通过 Agent 首页确认开始渲染", artifact_sha256: review.sha256, script_sha256: script.sha256,
  }) });
  toast("最终脚本已确认，Agent 开始装配成片");
  await refresh({ syncHomeView: false });
  await advanceJob(jobId);
}

function renderLatestArtifact() {
  const latest = state.jobs.find(job => job.current_run_id && (job.artifacts || []).includes("final.mp4"));
  const target = document.getElementById("latestArtifact");
  if (!latest) {
    target.innerHTML = `<div class="video-empty"><img class="ui-icon" src="/icons/play.svg" alt=""><strong>还没有成片</strong><span>任务完成后，视频会自动出现在这里。</span></div>`;
    return;
  }
  target.innerHTML = `<video id="latestVideo" class="latest-video" src="/api/jobs/${latest.id}/artifacts/final.mp4" preload="metadata" playsinline></video>
    <div class="latest-meta"><h3>${escapeHtml(latest.production_input?.topic || latest.id)}</h3><div class="latest-status"><img class="ui-icon" src="/icons/check.svg" alt="">${isAgentTestReview(latest) ? "代理测试审查完成 · 待用户最终验收" : "已完成 · 合规已确认"} · <span id="latestDuration">00:--</span></div>
    <div class="latest-actions"><button id="latestPlayButton" class="primary latest-play" type="button"><img class="ui-icon" src="/icons/play.svg" alt="">播放</button><a href="/api/jobs/${latest.id}/artifacts/final.mp4" download="shiyi-${escapeHtml(latest.id)}-final.mp4"><img class="ui-icon" src="/icons/download.svg" alt="">下载成片</a><a href="/api/jobs/${latest.id}/artifacts/manifest.json" target="_blank" rel="noreferrer">查看证据清单</a></div></div>`;
  const video = document.getElementById("latestVideo");
  video.addEventListener("loadedmetadata", () => {
    const duration = Number(video.duration || 0);
    const minutes = Math.floor(duration / 60);
    const seconds = Math.round(duration % 60);
    document.getElementById("latestDuration").textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    if (duration > 4 && video.currentTime === 0) video.currentTime = Math.min(3, duration / 12);
  });
  document.getElementById("latestPlayButton").addEventListener("click", () => toggleLatestVideo());
}

async function toggleLatestVideo() {
  const video = document.getElementById("latestVideo");
  if (!video) return;
  if (video.paused) await video.play();
  else video.pause();
}

function renderSettings() {
  const config = state.config;
  if (!config) return;
  document.getElementById("providerName").value = "DeepSeek";
  document.getElementById("baseUrl").value = config.provider.base_url || "https://api.deepseek.com";
  document.getElementById("modelName").value = config.provider.model || "";
  document.getElementById("researchEnabled").checked = config.research?.enabled !== false;
  document.getElementById("mediaParserRoot").value = config.research?.media_parser_root || "";
  document.getElementById("rootsInput").value = (config.discovery.roots || []).join("\n");
  const storageText = config.provider.persisted_api_key ? "Key 已用 Windows DPAPI 加密保存" : (config.provider.has_api_key ? "已检测到会话或环境变量 Key" : "尚未检测到 Key");
  const connectionText = state.status?.provider_connection_verified
    ? "当前本机会话连接已验证"
    : config.provider.has_api_key
      ? "当前会话尚未测试连接"
      : "配置 Key 后可测试连接";
  document.getElementById("providerResult").textContent = `${storageText}；${connectionText}；界面与日志均不回显密钥`;
  const warning = document.getElementById("secretWarning");
  warning.hidden = !config.provider.secret_warning;
  warning.textContent = config.provider.secret_warning || "";
  document.getElementById("reviewModeState").textContent = isAgentTestReview()
    ? "Codex 浏览器审查（仅测试）"
    : "用户本人审查";
  document.getElementById("storageRoot").value = config.storage.root || "";
  const names = { tools: "工具", models: "模型", downloads: "下载暂存", cache: "缓存", temp: "临时文件", logs: "日志", projects: "剪辑与生成项目" };
  document.getElementById("storageDirectories").innerHTML = Object.entries(config.storage.directories || {}).filter(([key]) => key !== "root").map(([key, path]) => `<div><span>${escapeHtml(names[key] || key)}</span><code>${escapeHtml(path)}</code></div>`).join("");
}

function renderTools(data = {}) {
  const grid = document.getElementById("toolGrid");
  grid.innerHTML = state.tools.length ? state.tools.map(tool => `<article class="tool-card"><span class="confidence">置信度 ${Math.round(tool.confidence * 100)}%</span><h3>${escapeHtml(tool.name)}</h3><div class="path mono">${escapeHtml(tool.path)}</div><div class="tags">${(tool.capabilities || []).map(cap => `<span class="tag">${escapeHtml(cap)}</span>`).join("")}</div><p class="hint">入口：${tool.entrypoints?.length ? tool.entrypoints.map(escapeHtml).join("、") : "需人工配置"}</p></article>`).join("") : `<article class="panel"><h2>尚未扫描</h2><p class="lead">填写目录后点击“开始只读扫描”。</p></article>`;
  const report = data.report || {};
  document.getElementById("scanMeta").textContent = data.last_scan ? `上次扫描：${data.last_scan}；访问目录 ${report.visited_directories || 0}；发现 ${state.tools.length} 个候选项目` : "";
}

function renderCatalog() {
  const catalog = state.catalog || { packages: [], policy: {} };
  const result = state.hardware || {};
  const hardware = result.hardware || { gpu: {}, memory: {}, disks: [] };
  const recommended = new Set(result.recommended_package_ids || []);
  document.getElementById("hardwareProfile").textContent = result.profile?.label || "未匹配硬件档位";
  document.getElementById("installPolicyBadge").textContent = result.auto_install_enabled ? "自动安装已开放" : "安装尚未开放";
  document.getElementById("catalogCount").textContent = `${catalog.packages.length} 个组件`;
  document.getElementById("hardwareFacts").innerHTML = `<div><span>显卡</span><strong>${escapeHtml(hardware.gpu?.name || "未检测到")}</strong></div><div><span>显存</span><strong>${escapeHtml(hardware.gpu?.vram_gb ?? 0)} GB</strong></div><div><span>内存</span><strong>${escapeHtml(hardware.memory?.total_gb ?? "未知")} GB</strong></div><div><span>建议路线</span><strong>${escapeHtml(result.profile?.default_route || "待判断")}</strong></div><div class="wide"><span>默认安装位置</span><strong>${escapeHtml(result.storage?.root || "未设置")}</strong></div>`;
  const threshold = Number(catalog.policy?.blocked_install_drives_when_free_gb_below || 20);
  const blocked = (hardware.disks || []).filter(disk => Number(disk.free_gb) < threshold);
  const warning = document.getElementById("diskWarnings");
  warning.hidden = !blocked.length;
  warning.textContent = blocked.length ? `禁止安装到空间不足的磁盘：${blocked.map(disk => `${disk.root} 仅剩 ${disk.free_gb}GB`).join("；")}` : "";
  document.getElementById("packageGrid").innerHTML = (catalog.packages || []).map(pkg => {
    const sources = (pkg.sources || []).map(source => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.role)}</a>`).join(" · ") || "仅限当前内置运行时";
    return `<article class="tool-card package-card ${recommended.has(pkg.id) ? "recommended" : ""}"><span class="confidence">${recommended.has(pkg.id) ? "适合本机" : "其他档位"}</span><h3>${escapeHtml(pkg.name)}</h3><div class="path mono">${escapeHtml(pkg.id)}</div><div class="tags">${(pkg.capabilities || []).slice(0, 4).map(cap => `<span class="tag">${escapeHtml(cap)}</span>`).join("")}</div><p class="hint">许可：${escapeHtml(pkg.license)}<br>状态：${escapeHtml(pkg.install_status)}</p><div class="source-links">${sources}</div><button class="secondary package-install" disabled>等待版本固定与校验</button></article>`;
  }).join("");
}

function renderJobs() {
  const list = document.getElementById("jobList");
  if (!state.jobs.length) {
    list.innerHTML = `<p class="lead">还没有任务。回到 Agent 工作台选择一个角度即可开始。</p>`;
    return;
  }
  list.innerHTML = state.jobs.map(job => {
    const title = job.production_input?.topic || job.plan?.goal || job.id;
    const busy = state.busyJobs.has(job.id);
    const busyAttribute = busy ? 'disabled aria-busy="true"' : "";
    const runButton = runnableStates.has(job.status) && job.production_input && !job.legacy_read_only ? `<button class="primary" data-action="run" data-job-id="${job.id}" ${busyAttribute}>${busy ? "执行中…" : "推进下一阶段"}</button>` : "";
    return `<div class="job"><div class="job-head"><div><h3>${escapeHtml(title)}</h3><small>${escapeHtml(job.id)} · ${escapeHtml(job.created_at)}</small></div><span class="pill ${statusClass(job.status)}">${escapeHtml(statusLabels[job.status] || job.status)}</span></div>${job.last_error ? `<div class="notice">${escapeHtml(job.last_error)}</div>` : ""}<div class="job-facts"><span>预算 ${job.budget?.attempted || 0}/${job.budget?.limit || 7}</span><span>尝试 ${job.runs?.length || 0}</span><span>当前成功 ${escapeHtml(job.current_run_id || "无")}</span></div><div class="action-row">${job.status === "planned" && !job.legacy_read_only ? `<button class="secondary" data-action="authorize" data-job-id="${job.id}" ${busyAttribute}>${busy ? "处理中…" : "批准任务执行"}</button>` : ""}${runButton}${job.production_input || job.legacy_read_only ? `<button class="secondary" data-action="open" data-job-id="${job.id}" ${busyAttribute}>查看/精修</button>` : ""}</div></div>`;
  }).join("");
}

function findingReviewGuide(item) {
  return {
    summary: item.review_summary || item.claim || "未生成易懂解释。",
    allowed: item.allowed_use || "只按这条证据原意做保守转述。",
    prohibited: item.prohibited_use || (item.limitations || []).join("；") || "不能超出证据原意。",
    source: item.source_label || "已抓取公开来源",
  };
}

function renderResearchFindings(findings, strictAudit = null, researchStatus = "") {
  const eligibleCount = findings.filter(item => item.auto_review_status === "eligible").length;
  const excludedCount = findings.length - eligibleCount;
  if (!findings.length && ["offline", "disabled"].includes(researchStatus)) {
    return `<p class="lead">本次没有可采信 finding。${escapeHtml(EMPTY_RESEARCH_APPROVAL_NOTE)}</p>`;
  }
  const summary = strictAudit ? `严格审核 Agent 已按“所有内容默认虚假”完成反向举证：${strictAudit.passed_count || 0} 条限定通过，${strictAudit.rejected_count || 0} 条被否决。` : `${eligibleCount} 条可安全转述，${excludedCount} 条已自动排除。`;
  const rows = findings.map(item => {
    const guide = findingReviewGuide(item);
    const sources = (item.source_urls || []).join(" · ") || "无可回溯来源";
    const excerpts = (item.evidence || []).map(entry => `<li><q>${escapeHtml(entry.excerpt || "")}</q><small>${escapeHtml(entry.url || "")}</small></li>`).join("");
    if (item.auto_review_status === "eligible") {
      return `<div class="finding" data-finding-id="${escapeHtml(item.finding_id)}" data-evidence-type="paraphrase" data-decision="approved"><span class="review-recommendation">严格审核 Agent：仅限安全转述</span><strong>${escapeHtml(guide.summary)}</strong><dl class="review-guide"><dt>可以怎么说</dt><dd>${escapeHtml(guide.allowed)}</dd><dt>绝对不能怎么说</dt><dd>${escapeHtml(guide.prohibited)}</dd></dl><div class="finding-control"><label>审查决定<select data-field="decision"><option value="approved">采用安全转述</option><option value="rejected">这条不用</option></select></label></div><details><summary>查看原文和来源</summary><p>${escapeHtml(guide.source)}</p>${excerpts ? `<ul class="evidence-list">${excerpts}</ul>` : ""}<small>${escapeHtml(sources)}</small></details></div>`;
    }
    return `<div class="finding finding-excluded"><span class="finding-status">系统建议：不采用</span><strong>${escapeHtml(guide.summary)}</strong><dl class="review-guide"><dt>原因</dt><dd>${escapeHtml(guide.prohibited)}</dd></dl><details><summary>查看原始机器结论</summary><p>${escapeHtml(item.claim || "未命名结论")}</p><small>${escapeHtml(sources)}</small></details></div>`;
  }).join("");
  return `<p class="lead">${summary}</p>${rows || '<p class="lead">研究未产生可审批 finding；本次状态不允许空审批，请退回研究。</p>'}`;
}

function renderRunHistory(job) {
  const target = document.getElementById("runHistory");
  if (!job.runs?.length) { target.innerHTML = `<p class="lead">暂无运行尝试。</p>`; return; }
  target.innerHTML = [...job.runs].reverse().map(run => {
    const current = run.run_id === job.current_run_id;
    const links = run.stage === "render" && run.status === "complete" ? `<a href="/api/jobs/${job.id}/runs/${run.run_id}/artifacts/manifest.json" target="_blank" rel="noreferrer">查看清单</a> <a href="/api/jobs/${job.id}/runs/${run.run_id}/artifacts/final.mp4" download="shiyi-${escapeHtml(job.id)}-${escapeHtml(run.run_id)}-final.mp4">下载成片</a>` : "不可公开";
    return `<div class="run-row ${current ? "current" : ""}"><div><strong>${escapeHtml(run.stage)} · ${escapeHtml(run.status)}</strong><small>${escapeHtml(run.run_id)}${current ? " · 当前成功" : ""}</small></div><div>${links}</div></div>`;
  }).join("");
}

function syncApprovalButton(kind) {
  const select = document.getElementById(`${kind}Decision`);
  const button = document.getElementById(`submit${kind === "research" ? "Research" : "Compliance"}Btn`);
  const approved = select.value === "approved";
  button.className = approved ? "primary" : "danger";
  button.textContent = kind === "research" ? (approved ? "提交审核并进入下一步" : "提交审核并退回研究") : (approved ? "提交审核并进入渲染" : "提交审核并退回改稿");
}

async function openJob(id, { job: suppliedJob = null, scroll = true, managePolling = true } = {}) {
  const job = suppliedJob || await api(`/api/jobs/${id}`);
  state.selectedJob = job;
  state.reviewFiles = {};
  let artifactPending = false;
  switchView("jobs");
  const panel = document.getElementById("jobDetailPanel");
  panel.hidden = false;
  const badge = document.getElementById("jobDetailBadge");
  badge.textContent = statusLabels[job.status] || job.status;
  badge.className = `pill ${statusClass(job.status)}`;
  const agentTestReview = isAgentTestReview(job);
  const reviewModeLabel = agentTestReview ? "Codex 代理测试审查" : "用户本人审查";
  document.getElementById("jobSummary").innerHTML = `<div><span>选题</span><strong>${escapeHtml(job.production_input?.topic || "旧任务")}</strong></div><div><span>预算</span><strong>${job.budget?.attempted || 0}/${job.budget?.limit || 7}</strong></div><div><span>阶段审查</span><strong>${escapeHtml(reviewModeLabel)}</strong></div><div><span>当前成功运行</span><strong>${escapeHtml(job.current_run_id || "无")}</strong></div>`;
  const researchPanel = document.getElementById("researchApprovalPanel");
  const compliancePanel = document.getElementById("complianceApprovalPanel");
  researchPanel.hidden = job.status !== "awaiting_research_approval";
  compliancePanel.hidden = !["awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved"].includes(job.status);
  const researchFindings = document.getElementById("researchFindings");
  const complianceSummary = document.getElementById("complianceSummary");
  const researchSubmit = document.getElementById("submitResearchBtn");
  const complianceSubmit = document.getElementById("submitComplianceBtn");
  researchFindings.innerHTML = '<p class="lead">研究产物尚未加载。</p>';
  complianceSummary.textContent = "合规产物尚未加载。";
  const researchReviewer = document.getElementById("researchReviewer");
  const complianceReviewer = document.getElementById("complianceReviewer");
  researchReviewer.value = reviewerForJob(job);
  complianceReviewer.value = reviewerForJob(job);
  researchReviewer.readOnly = agentTestReview;
  complianceReviewer.readOnly = agentTestReview;
  document.getElementById("researchNote").value = agentTestReview ? AGENT_RESEARCH_NOTE : "";
  document.getElementById("complianceNote").value = agentTestReview ? AGENT_COMPLIANCE_NOTE : "";
  document.getElementById("approvedScriptInput").value = "";
  document.getElementById("durationEstimate").textContent = "内容阶段完成后才能人工改稿。";
  document.getElementById("artifactLinks").innerHTML = "";
  document.getElementById("runHistory").innerHTML = '<p class="lead">运行记录加载中。</p>';
  const staleVideo = document.getElementById("artifactVideo");
  staleVideo.hidden = true;
  staleVideo.removeAttribute("src");
  researchSubmit.disabled = true;
  complianceSubmit.disabled = true;
  let script = "";
  const researchMayExist = !["planned", "authorized", "research_running"].includes(job.status);
  try {
    if (!researchMayExist) throw new Error("research_not_ready");
    state.reviewFiles.research = await readJsonArtifact(`/api/jobs/${id}/review-artifacts/research.json`);
    if (!researchPanel.hidden) {
      const findings = state.reviewFiles.research.data.findings || [];
      const emptyLocalResearch = isEmptyLocalResearch(state.reviewFiles.research.data);
      document.getElementById("researchFindings").innerHTML = renderResearchFindings(findings, state.reviewFiles.research.data.strict_audit || null, state.reviewFiles.research.data.status || "");
      document.getElementById("researchDecision").value = findings.some(item => item.auto_review_status === "eligible") || emptyLocalResearch ? "approved" : "rejected";
      if (emptyLocalResearch) document.getElementById("researchNote").value = EMPTY_RESEARCH_APPROVAL_NOTE;
      syncApprovalButton("research");
      researchSubmit.disabled = false;
    }
  } catch (error) {
    if (researchMayExist && (error.networkUncertain || error.httpStatus === 404)) {
      artifactPending = true;
      if (!researchPanel.hidden) researchFindings.innerHTML = '<p class="lead">产物发布中，页面会自动重试。</p>';
    } else if (researchMayExist && !researchPanel.hidden) {
      researchFindings.textContent = `研究产物读取失败：${error.message}`;
    }
  }
  const contentMayExist = !["planned", "authorized", "research_running", "awaiting_research_approval", "awaiting_research_revision", "research_approved", "content_running"].includes(job.status);
  try {
    if (!contentMayExist) throw new Error("content_not_ready");
    state.reviewFiles.script = await readJsonArtifact(`/api/jobs/${id}/review-artifacts/approved_script.json`);
    script = state.reviewFiles.script.data.script || "";
    state.reviewFiles.review = await readJsonArtifact(`/api/jobs/${id}/review-artifacts/review.json`);
    const review = state.reviewFiles.review.data;
    document.getElementById("complianceSummary").textContent = review.status === "blocked"
      ? `自动检查：阻断。${(review.warnings || []).map(item => item.message).join("；")}`
      : agentTestReview
        ? "严格检查未发现阻断项；Codex 测试代理仍需查看脚本后提交测试审查。"
        : "严格检查未发现阻断项；最终仍由你亲自确认。";
    document.getElementById("complianceDecision").value = review.status === "blocked" || review.blocked ? "rejected" : "approved";
    syncApprovalButton("compliance");
    complianceSubmit.disabled = false;
  } catch (error) {
    if (contentMayExist && (error.networkUncertain || error.httpStatus === 404)) {
      artifactPending = true;
      if (!compliancePanel.hidden) complianceSummary.textContent = "产物发布中，页面会自动重试。";
    } else if (contentMayExist && !compliancePanel.hidden) {
      complianceSummary.textContent = `合规产物读取失败：${error.message}`;
    }
  }
  document.getElementById("approvedScriptInput").value = script;
  document.getElementById("approvedScriptInput").disabled = job.legacy_read_only || !script;
  document.getElementById("saveScriptBtn").disabled = job.legacy_read_only || !script;
  document.getElementById("rerunJobBtn").disabled = job.legacy_read_only || !runnableStates.has(job.status) || state.busyJobs.has(job.id);
  document.getElementById("durationEstimate").textContent = script ? `当前 ${script.length} 字；保存时按标点加权校验 35–75 秒，配音后只允许 0.75–1.5 倍安全变速。` : "内容阶段完成后才能人工改稿。";
  renderRunHistory(job);
  document.getElementById("artifactLinks").innerHTML = (job.artifacts || []).map(name => name === "final.mp4"
    ? `<a href="/api/jobs/${id}/artifacts/final.mp4" download="shiyi-${escapeHtml(id)}-final.mp4">下载成片</a>`
    : `<a href="/api/jobs/${id}/artifacts/${name}" target="_blank" rel="noreferrer">${escapeHtml(artifactLabels[name] || name)}</a>`).join("");
  const video = document.getElementById("artifactVideo");
  if ((job.artifacts || []).includes("final.mp4")) { video.src = `/api/jobs/${id}/artifacts/final.mp4`; video.hidden = false; }
  else { video.hidden = true; video.removeAttribute("src"); }
  if (managePolling && (runningStates.has(job.status) || artifactPending)) {
    schedulePoll(job.id, { immediate: runningStates.has(job.status) });
  } else if (managePolling) stopPoll(job.id);
  if (scroll) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  return artifactPending;
}

async function submitResearch(decision) {
  const job = state.selectedJob;
  if (!job || !state.reviewFiles.research) return;
  const findings = [...document.querySelectorAll("#researchFindings .finding[data-finding-id]")].map(row => ({ finding_id: row.dataset.findingId, decision: row.querySelector('[data-field="decision"]')?.value || row.dataset.decision || "rejected", evidence_type: row.dataset.evidenceType || "paraphrase" }));
  const emptyLocalResearch = isEmptyLocalResearch(state.reviewFiles.research.data);
  if (decision === "approved" && !findings.length && !emptyLocalResearch) throw new Error("本次研究状态不允许空审批");
  const note = decision === "approved" && emptyLocalResearch ? EMPTY_RESEARCH_APPROVAL_NOTE : document.getElementById("researchNote").value.trim();
  await api(`/api/jobs/${job.id}/approvals/research`, { method: "POST", body: JSON.stringify({ decision, reviewer: document.getElementById("researchReviewer").value.trim(), note, artifact_sha256: state.reviewFiles.research.sha256, findings }) });
  toast(decision === "approved" ? (isAgentTestReview(job) ? "Codex 测试代理已完成研究审查" : "研究证据已由你批准") : "研究已退回");
  await refresh({ syncHomeView: false });
  await openJob(job.id);
}

async function submitCompliance(decision) {
  const job = state.selectedJob;
  if (!job || !state.reviewFiles.review || !state.reviewFiles.script) return;
  await api(`/api/jobs/${job.id}/approvals/compliance`, { method: "POST", body: JSON.stringify({ decision, reviewer: document.getElementById("complianceReviewer").value.trim(), note: document.getElementById("complianceNote").value.trim(), artifact_sha256: state.reviewFiles.review.sha256, script_sha256: state.reviewFiles.script.sha256 }) });
  toast(decision === "approved" ? (isAgentTestReview(job) ? "Codex 测试代理已完成脚本审查" : "最终脚本已由你放行") : "脚本已退回改稿");
  await refresh({ syncHomeView: false });
  await openJob(job.id);
}

document.getElementById("homeButton").addEventListener("click", () => switchView("workbench"));
document.querySelectorAll(".back-home").forEach(button => button.addEventListener("click", () => switchView("workbench")));
document.getElementById("appMenuButton").addEventListener("click", event => { event.stopPropagation(); openMenu(); });
document.getElementById("appMenu").addEventListener("click", event => {
  const button = event.target.closest("button[data-view]");
  if (button) switchView(button.dataset.view);
});
document.getElementById("providerQuickButton").addEventListener("click", () => switchView("settings"));
document.addEventListener("click", event => { if (!event.target.closest(".menu-wrap")) closeMenu(); });
document.addEventListener("keydown", event => { if (event.key === "Escape") closeMenu(); });

document.getElementById("topicCandidates").addEventListener("click", event => {
  const row = event.target.closest("button[data-topic-index]");
  if (!row) return;
  state.selectedTopicIndex = Number(row.dataset.topicIndex);
  renderTopicChoices();
});
document.getElementById("startSelectedTopic").addEventListener("click", () => startSelectedTopic().catch(error => toast(error.message, true)));
document.getElementById("refreshTopics").addEventListener("click", event => {
  const button = event.currentTarget;
  setBusy(button, true, "正在换一批…");
  loadTopics(state.currentGoal).catch(error => toast(error.message, true)).finally(() => setBusy(button, false));
});
document.getElementById("writeOwnTopic").addEventListener("click", () => {
  const input = document.getElementById("goalInput");
  input.focus();
  input.select();
});

document.getElementById("agentComposer").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("goalInput");
  const message = input.value.trim();
  if (!message) return;
  const job = findHomeJob();
  try {
    if (job && ["awaiting_research_approval", "awaiting_compliance_approval"].includes(job.status) && /^(继续|确认|可以|通过)[。！!]?$/u.test(message)) {
      if (job.status === "awaiting_research_approval") await approveHomeResearch(job.id);
      else await approveHomeCompliance(job.id);
      return;
    }
    if (job) {
      const correctionBody = JSON.stringify({
        job_id: job.id,
        message,
        actor: "本机会话用户",
        mode: "defer",
      });
      const correctionKey = newIdempotencyKey();
      const recordOnce = () => api("/api/agent/corrections", {
        method: "POST",
        headers: { "Idempotency-Key": correctionKey },
        body: correctionBody,
      });
      let learned;
      try {
        learned = await recordOnce();
      } catch (error) {
        if (!error.networkUncertain) throw error;
        learned = await recordOnce();
      }
      input.value = "";
      if (learned.job) {
        const index = state.jobs.findIndex(item => item.id === learned.job.id);
        if (index >= 0) state.jobs[index] = learned.job;
        else state.jobs.unshift(learned.job);
        await renderHomeJob(learned.job);
      }
      toast(learned.queued_for_next_stage
        ? "纠错已记住，将在当前阶段结束后的安全边界应用。"
        : `纠错已记住并应用到当前任务（${learned.effective_scope || "task"}）；旧的成功产物仍然保留。`);
      return;
    }
    if (!job && /^[123]$/u.test(message) && state.topicCandidates.length) {
      state.selectedTopicIndex = Number(message) - 1;
      renderTopicChoices();
      return;
    }
    if (!job && /换一批/u.test(message)) {
      await loadTopics(state.currentGoal);
      return;
    }
    await loadTopics(message, { resetSeen: true });
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("activeJobPanel").addEventListener("click", async event => {
  const button = event.target.closest("button[data-home-action]");
  if (!button) return;
  const job = findHomeJob();
  const action = button.dataset.homeAction;
  try {
    setBusy(button, true, "处理中…");
    if (action === "authorize" && job) await authorizeHomeJob(job.id);
    else if (action === "advance" && job) await advanceJob(job.id);
    else if (action === "approve-research" && job) await approveHomeResearch(job.id);
    else if (action === "approve-compliance" && job) await approveHomeCompliance(job.id);
    else if (action === "show-details" && job) await openJob(job.id);
    else if (action === "play-latest") await toggleLatestVideo();
    else if (action === "new-task") {
      setHomeJobId(null);
      state.topicCandidates = [];
      state.topicResponse = null;
      await loadTopics(DEFAULT_GOAL, { resetSeen: true });
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button.isConnected) setBusy(button, false);
  }
});

document.getElementById("jobList").addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.jobId;
  const task = button.dataset.action === "authorize"
    ? authorizeHomeJob(id)
    : button.dataset.action === "run"
      ? advanceJob(id)
      : (async () => {
          if (!beginJobBusy(id)) return;
          try { await openJob(id); }
          finally { endJobBusy(id); }
        })();
  Promise.resolve(task).catch(error => toast(error.message, true));
});
document.getElementById("reloadJobs").addEventListener("click", () => refresh({ syncHomeView: false }).catch(error => toast(error.message, true)));
document.getElementById("rerunJobBtn").addEventListener("click", () => state.selectedJob && advanceJob(state.selectedJob.id).catch(error => toast(error.message, true)));
document.getElementById("researchDecision").addEventListener("change", () => syncApprovalButton("research"));
document.getElementById("complianceDecision").addEventListener("change", () => syncApprovalButton("compliance"));
document.getElementById("submitResearchBtn").addEventListener("click", () => submitResearch(document.getElementById("researchDecision").value).catch(error => toast(error.message, true)));
document.getElementById("submitComplianceBtn").addEventListener("click", () => submitCompliance(document.getElementById("complianceDecision").value).catch(error => toast(error.message, true)));
document.getElementById("saveScriptBtn").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  try {
    await api(`/api/jobs/${state.selectedJob.id}/script`, { method: "PATCH", body: JSON.stringify({ script: document.getElementById("approvedScriptInput").value }) });
    toast("改稿已保存，旧合规审批已失效");
    await refresh({ syncHomeView: false });
    await openJob(state.selectedJob.id);
  } catch (error) { toast(error.message, true); }
});

document.getElementById("scanBtn").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "扫描中…");
  try {
    const roots = document.getElementById("rootsInput").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    await api("/api/config", { method: "POST", body: JSON.stringify({ discovery: { ...state.config.discovery, roots } }) });
    const result = await api("/api/discover", { method: "POST", body: JSON.stringify({ roots }) });
    toast(`发现 ${result.count} 个候选项目`);
    await refresh({ syncHomeView: false });
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("saveSettings").addEventListener("click", async () => {
  try {
    const body = {
      provider: { model: document.getElementById("modelName").value.trim(), base_url: "https://api.deepseek.com", api_key: document.getElementById("apiKey").value.trim(), persist_api_key: document.getElementById("persistKey").checked },
      research: { ...state.config.research, enabled: document.getElementById("researchEnabled").checked, media_parser_root: document.getElementById("mediaParserRoot").value.trim() },
      storage: { root: document.getElementById("storageRoot").value.trim() },
    };
    await api("/api/config", { method: "POST", body: JSON.stringify(body) });
    document.getElementById("apiKey").value = "";
    toast("设置已保存");
    await refresh({ syncHomeView: false });
  } catch (error) { toast(error.message, true); }
});

document.getElementById("clearApiKey").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "正在清除…");
  try {
    state.config = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ provider: { clear_api_key: true } }),
    });
    document.getElementById("apiKey").value = "";
    await refresh({ syncHomeView: false });
    renderSettings();
    toast("已清除本机会话与 DPAPI 保存的 Key；环境变量中的 Key 不会被修改");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("testProvider").addEventListener("click", async () => {
  const target = document.getElementById("providerResult");
  target.textContent = "正在测试（不计任务预算）…";
  try {
    const result = await api("/api/provider/test", { method: "POST", body: "{}" });
    target.textContent = `连接成功；可用模型 ${result.models.length} 个；本次测试不计任务预算`;
    await refresh({ syncHomeView: false });
    target.textContent = `本次连接已验证；可用模型 ${result.models.length} 个；本次测试不计任务预算`;
  } catch (error) { target.textContent = error.message; toast(error.message, true); }
});

const composer = document.getElementById("goalInput");
composer.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.getElementById("agentComposer").requestSubmit();
  }
});

bootstrapSession().then(() => refresh()).catch(error => toast(error.message, true));
