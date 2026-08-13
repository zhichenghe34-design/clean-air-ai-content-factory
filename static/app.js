const DEFAULT_GOAL = "为除甲醛服务企业制作一条面向新房家庭的竖屏科普短视频，重点讲清检测条件、适用边界和可追溯证据。";

const state = {
  config: null,
  status: null,
  jobs: [],
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
  awaiting_script_revision: "自动生成未通过",
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
const MECHANICAL_REVIEWER = "反向机械审核器";
const AGENT_RESEARCH_NOTE = "Codex 测试代理已逐项核对来源、严格审核结论与允许使用范围；仅用于受控测试。";
const AGENT_COMPLIANCE_NOTE = "Codex 测试代理已核对最终脚本、合规结果与审批哈希；仅用于受控测试。";
const POLL_BASE_DELAY_MS = 1000;
const POLL_MAX_DELAY_MS = 8000;
const SAME_TOPIC_RETRY_FAILURE_CODES = new Set([
  "automatic_script_revision_exhausted",
  "automatic_stage_attempts_exhausted",
]);

function isEmptyLocalResearch(research) {
  const findings = Array.isArray(research?.findings) ? research.findings : [];
  return ["offline", "disabled"].includes(research?.status) && findings.length === 0;
}

function reviewPolicyForJob(job = null) {
  const formalDefault = {
    stage_review_mode: "mechanical",
    final_human_acceptance_required: true,
  };
  if (job) return job.review_policy || formalDefault;
  return state.status?.review_policy || formalDefault;
}

function isAgentTestReview(job = null) {
  return reviewPolicyForJob(job).stage_review_mode === "agent_test";
}

function isMechanicalReview(job = null) {
  return reviewPolicyForJob(job).stage_review_mode === "mechanical";
}

function reviewerForJob(job = null) {
  if (isAgentTestReview(job)) return AGENT_TEST_REVIEWER;
  if (isMechanicalReview(job)) return MECHANICAL_REVIEWER;
  return "本机会话用户";
}

function isTerminalMechanicalGenerationFailure(job) {
  return isMechanicalReview(job)
    && job?.status === "failed"
    && Boolean(job?.automatic_controller_failure);
}

function canRetryFailedJob(job) {
  return isTerminalMechanicalGenerationFailure(job)
    && SAME_TOPIC_RETRY_FAILURE_CODES.has(String(job.automatic_controller_failure?.code || ""));
}

function isPreservedExactScriptFailure(job) {
  return isTerminalMechanicalGenerationFailure(job)
    && job.automatic_controller_failure?.code === "exact_script_render_failed_preserved";
}

function statusLabelForJob(job) {
  if (isPreservedExactScriptFailure(job)) return "改稿成片未完成";
  if (isTerminalMechanicalGenerationFailure(job)) return "本次生成未完成";
  if (job?.status === "awaiting_script_revision" && !isMechanicalReview(job)) return "待浏览器改稿";
  return statusLabels[job?.status] || job?.status || "状态未知";
}

function providerSourceLabelForJob(job) {
  const provenance = job?.provider_provenance;
  if (!provenance) return "旧任务未记录选题来源";
  const source = provenance.topic_source;
  if (source === "deepseek") return `DeepSeek（${provenance.model || "已配置模型"}）`;
  if (source === "deepseek_bootstrap") return `DeepSeek 动态能力包（${provenance.model || "已配置模型"}）`;
  if (source === "deepseek_filtered_with_local_fallback") return "DeepSeek 与本地安全 Agent 混合";
  if (source === "local_safe_agent") {
    if (provenance.provider_state === "unconfigured") return "本地安全 Agent（创建时未配置 DeepSeek）";
    return "本地安全 Agent（本任务未采用 DeepSeek 选题）";
  }
  return provenance.provider_state === "verified"
    ? "用户直接输入（创建时 DeepSeek 已验证）"
    : "用户直接输入（未使用 DeepSeek 选题）";
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
  if (!state.selectedJob) return;
  const button = document.getElementById("rerunJobBtn");
  if (button) button.disabled = state.selectedJob.legacy_read_only
    || !runnableStates.has(state.selectedJob.status)
    || state.busyJobs.has(state.selectedJob.id);
  const retryButton = document.getElementById("retryFailedJobBtn");
  if (retryButton) retryButton.disabled = state.busyJobs.has(state.selectedJob.id)
    || !canRetryFailedJob(state.selectedJob);
  const exactButton = document.getElementById("continueExactScriptBtn");
  if (exactButton) exactButton.disabled = state.busyJobs.has(state.selectedJob.id)
    || !isPreservedExactScriptFailure(state.selectedJob);
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

function switchView(viewName, { scroll = true } = {}) {
  const selected = document.getElementById(`view-${viewName}`) ? viewName : "workbench";
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${selected}`));
  document.getElementById("pageTitle").textContent = selected === "workbench" ? "Agent 内容工作台" : document.querySelector(`#view-${selected} h2`)?.textContent || "时宜 Agent 内容工厂";
  document.body.dataset.view = selected;
  closeMenu();
  if (selected === "workbench") requestAnimationFrame(resizeComposer);
  if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
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
  const pending = api("/api/config").then(config => {
    state.config = config;
    renderSettings();
  });
  state.auxiliaryRefresh = pending.finally(() => {
    state.auxiliaryRefresh = null;
  });
  return state.auxiliaryRefresh;
}

async function refresh({ syncHomeView = true } = {}) {
  // Jobs and status drive every visible transition. Settings update
  // independently and may not hold the workbench hostage.
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
    verified: "DeepSeek · 当前连接已验证",
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
  const motionLabel = motion?.health === "ready" ? "视频生成组件已就绪" : "视频生成组件正在检查";
  const footageLabel = "实拍素材组件";
  const footageReady = footage?.selectable === true && footage?.operator_ready === true;
  const footageInput = document.getElementById("productionModeFootage");
  footageInput.disabled = true;
  if (footageInput.checked) document.getElementById("productionModeMotion").checked = true;
  document.getElementById("motionEngineStatus").textContent = motionLabel;
  document.getElementById("footageEngineStatus").textContent = footageReady
    ? footageLabel
    : footage?.disabled_reason || "当前版本仅支持纯动画";
  document.getElementById("releaseVersionState").textContent = `v${releaseVersion}`;
  document.getElementById("motionVersionState").textContent = motion?.health === "ready" ? "可用" : "正在检查";
  document.getElementById("footageVersionState").textContent = "尚未开放";
  document.getElementById("editionBadge").textContent = `v${releaseVersion}`;
  document.getElementById("portBadge").textContent = `${location.hostname}:${location.port || "80"} · v${releaseVersion}`;
  document.getElementById("metricJobs").textContent = state.status?.job_count || 0;
  document.getElementById("metricModel").textContent = state.status?.model || "deepseek";
}

function setComposerContext(mode) {
  const form = document.getElementById("agentComposer");
  const label = document.getElementById("composerLabel");
  const help = document.getElementById("goalInputHelp");
  const buttonText = document.querySelector("#sendGoalBtn span");
  if (mode === "hidden") {
    form.hidden = true;
    return;
  }
  form.hidden = false;
  if (mode === "topics") {
    label.textContent = "想换选题方向？（可选）";
    help.textContent = "上面任选一个选题后，直接点“就做这个”即可，不用再发送。这里只在你想换方向时使用。";
    buttonText.textContent = "按新要求换选题";
  } else {
    label.textContent = "描述你想做的视频";
    help.textContent = "写清行业、受众和内容重点，Agent 会先推荐 3 个选题。";
    buttonText.textContent = "获取 3 个选题";
  }
  requestAnimationFrame(resizeComposer);
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
  resizeComposer();
  document.getElementById("topicChoicePanel").hidden = true;
  document.getElementById("activeJobPanel").hidden = true;
  setComposerContext("initial");
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
  setComposerContext("topics");
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
  setComposerContext("hidden");
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
        production_mode: "motion",
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
    : isMechanicalReview(job)
      ? "反向机械审核会自动核对证据、脚本与镜头蓝图；通过后直接继续，不要求中途改稿。"
      : "Agent 会在需要你本人确认时停下来。";
  return `<div class="job-chat-head"><div><h2>${escapeHtml(title)}</h2><p>${pauseText}</p></div><span class="status-pill status-running">处理中</span></div>
    <div class="progress-block"><div class="progress-copy"><span>${escapeHtml(message)}</span><b class="mono">${progress}%</b></div><div class="progress-track"><span class="progress-value progress-${progress}"></span></div></div>`;
}

function automaticFailureExplanation(job) {
  const failure = job?.automatic_controller_failure || {};
  const sourceError = String(failure.source_error || "").trim();
  const stage = String(failure.stage || "").trim();
  if (failure.code === "exact_script_render_failed_preserved") {
    return "你修改后的完整文案已经保存，但这次配音或成片没有通过质量检查；上一版成片仍可使用。";
  }
  if (/上一幕重复同一信息结构/.test(sourceError)) {
    return "分镜阶段生成了相邻重复的画面结构，自动质量检查没有放行。";
  }
  if (/自然节奏|口播|字\/秒|长度/.test(sourceError)) {
    return "文案或配音节奏没有达到自然成片要求，自动质量检查没有放行。";
  }
  if (stage === "research") return "资料研究阶段没有得到可安全使用的结果。";
  if (stage === "content") return "文案或分镜阶段没有通过自动质量检查。";
  if (stage === "render") return "配音或成片阶段没有通过自动质量检查。";
  return "这次生成没有通过自动质量检查，因此没有发布不合格视频。";
}

async function retryFailedJob(job, button) {
  if (!canRetryFailedJob(job)) {
    throw new Error(isPreservedExactScriptFailure(job)
      ? "这次必须继续使用已保存的修改文案，不能重新抽稿"
      : "这类失败不能按同一选题重新生成");
  }
  const requestKey = newIdempotencyKey();
  const retryOnce = () => api(`/api/jobs/${job.id}/retry`, {
    method: "POST",
    headers: { "Idempotency-Key": requestKey },
    body: "{}",
  });
  setBusy(button, true, "正在建立新尝试…");
  try {
    let retried;
    try {
      retried = await retryOnce();
    } catch (error) {
      if (!error.networkUncertain) throw error;
      retried = await retryOnce();
    }
    setHomeJobId(retried.id);
    await refresh({ syncHomeView: false });
    await renderHomeJob(retried);
    if (retried.status === "authorized") await advanceJob(retried.id);
  } finally {
    if (button.isConnected) setBusy(button, false);
  }
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
    // Persisted correction memory is an operator-only diagnostic capability.
    // In the customer build this composer keeps its visible promise and starts
    // a fresh three-topic request; it never probes the hidden mutation route.
    if (job && state.status?.internal_diagnostics_enabled === true) {
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
  setComposerContext("hidden");
  document.getElementById("topicChoicePanel").hidden = true;
  document.getElementById("topicLoading").hidden = true;
  const panel = document.getElementById("activeJobPanel");
  panel.hidden = false;
  const title = job.production_input?.topic || job.plan?.goal || job.id;
  const status = job.status;
  const agentTestReview = isAgentTestReview(job);
  const mechanicalReview = isMechanicalReview(job);
  const progress = statusProgress[status] ?? 18;
  document.getElementById("processBudget").textContent = `请求 ${job.budget?.attempted || 0} / ${job.budget?.limit || 7}`;
  document.getElementById("processDetails").innerHTML = `<p>当前状态：${escapeHtml(statusLabelForJob(job))}。失败重跑不会覆盖上一份成功产物；详细证据、脚本和哈希仍可在任务记录中查看。</p>`;

  if (runningStates.has(status)) {
    panel.innerHTML = renderRunningCard(title, statusLabelForJob(job), progress, job);
    if (managePolling) schedulePoll(job.id);
    return true;
  }

  if (mechanicalReview && runnableStates.has(status) && state.busyJobs.has(job.id)) {
    panel.innerHTML = renderRunningCard(title, "反向机械审核已通过当前阶段，正在自动继续…", progress, job);
    if (managePolling) schedulePoll(job.id);
    return true;
  }

  const head = `<div class="job-chat-head"><div><h2>${escapeHtml(title)}</h2><p>其余步骤由 Agent 自动完成。</p></div><span class="status-pill ${statusClass(status)}">${escapeHtml(statusLabelForJob(job))}</span></div>`;
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
    const warnings = review?.data?.warnings?.map(item => item.message).filter(Boolean).join("；") || (mechanicalReview
      ? job.last_error || "Agent 已在安全边界停止，本次没有生成可放行脚本。"
      : "脚本仍需人工修改或重新检查。");
    const approvalAction = agentTestReview ? "show-details" : "approve-compliance";
    const approvalLabel = agentTestReview ? "进入代理测试审查" : "确认脚本并渲染";
    const readyText = agentTestReview
      ? "没有发现阻断项。Codex 测试代理查看脚本与合规依据后，才可提交测试审查。"
      : "没有发现阻断项。你确认后，Agent 将直接开始配音和成片装配。";
    if (mechanicalReview && blocked) {
      panel.innerHTML = `${head}<div class="gate-card"><h3>自动生成未通过，已安全停止</h3><p>${escapeHtml(warnings)}</p><p>反向机械审核没有把不合格脚本交给你修改，也没有冒充人工批准。可以让 Agent 自动重试，或返回选题重新开始。</p><div class="gate-actions"><button class="primary" type="button" data-home-action="advance">让 Agent 自动重试</button><button class="quiet-link" type="button" data-home-action="new-task">返回选题</button><button class="quiet-link" type="button" data-home-action="show-details">查看内部诊断</button></div></div>`;
    } else {
      panel.innerHTML = `${head}<div class="gate-card"><h3>${blocked ? "最终脚本暂时不能放行" : "最终脚本已通过自动合规检查"}</h3><p>${escapeHtml(blocked ? warnings : readyText)}</p><div class="gate-actions">${blocked ? "" : `<button class="primary" type="button" data-home-action="${approvalAction}">${approvalLabel}</button>`}<button class="quiet-link" type="button" data-home-action="show-details">${blocked ? "打开脚本修改" : "查看脚本与合规依据"}</button></div>${blocked || agentTestReview ? "" : '<p class="reply-hint">也可以直接回复：继续</p>'}</div>`;
    }
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (status === "complete") {
    const completionTitle = agentTestReview
      ? "测试成片已经完成，等待用户最终验收"
      : mechanicalReview ? "自动成片已经完成" : "成片已经完成";
    const reviewBoundary = agentTestReview
      ? " 两道阶段门禁记录为代理测试审查，不冒充用户签署。"
      : mechanicalReview ? " 研究、脚本和镜头均已通过反向机械审核；公开发布仍保留最终责任确认。" : "";
    const exactRevisionAction = mechanicalReview
      ? '<button class="secondary" type="button" data-home-action="edit-script">修改文案并重新生成</button>'
      : '';
    panel.innerHTML = `${head}<div class="gate-card"><h3>${completionTitle}</h3><p>视频、验收报告和技术记录都已发布到本次成功运行；失败尝试没有覆盖它。${reviewBoundary}</p><div class="gate-actions"><button class="primary" type="button" data-home-action="play-latest">播放最新成片</button>${exactRevisionAction}<button class="quiet-link" type="button" data-home-action="new-task">再做一条</button></div></div>`;
    renderLatestArtifact();
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (status === "planned") {
    panel.innerHTML = `${head}<div class="gate-card"><h3>已选好角度</h3><p>点击一次即可同时记录任务执行授权，并开始研究。</p><div class="gate-actions"><button class="primary" type="button" data-home-action="authorize">授权并开始</button></div></div>`;
    if (managePolling) stopPoll(job.id);
    return false;
  }
  if (isTerminalMechanicalGenerationFailure(job)) {
    const failureExplanation = automaticFailureExplanation(job);
    if (canRetryFailedJob(job)) {
      panel.innerHTML = `${head}<div class="gate-card"><h3>选题没有被否决，这次生成没成功</h3><p>${escapeHtml(failureExplanation)}</p><p>系统已经停止重复无效尝试。重新生成会保留这个选题，让 Agent 重新调用模型制作；旧失败记录不会被覆盖。</p><div class="gate-actions"><button class="primary" type="button" data-home-action="retry-failed">重新生成这个选题</button><button class="secondary" type="button" data-home-action="new-task">换个选题</button><button class="quiet-link" type="button" data-home-action="show-details">查看详细原因</button></div></div>`;
    } else if (isPreservedExactScriptFailure(job)) {
      panel.innerHTML = `${head}<div class="gate-card"><h3>修改后的文案已保留，这次成片没成功</h3><p>${escapeHtml(failureExplanation)}</p><p>系统不会把你的改稿丢掉，也不会重新抽一篇文案。可以继续修改，或按当前保存的文案再次生成。</p><div class="gate-actions"><button class="primary" type="button" data-home-action="edit-script">继续修改并重新生成</button>${job.current_run_id ? '<button class="secondary" type="button" data-home-action="play-latest">播放上一版成片</button>' : ""}<button class="quiet-link" type="button" data-home-action="show-details">查看详细原因</button></div></div>`;
    } else {
      panel.innerHTML = `${head}<div class="gate-card"><h3>这次生成没有完成</h3><p>${escapeHtml(failureExplanation)}</p><p>这类失败不能安全地直接重开同题任务，请查看详细原因或换一个选题。</p><div class="gate-actions"><button class="secondary" type="button" data-home-action="new-task">换个选题</button><button class="quiet-link" type="button" data-home-action="show-details">查看详细原因</button></div></div>`;
    }
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
  const exactRevisionAction = isMechanicalReview(latest)
    ? '<button id="latestEditButton" class="secondary" type="button">修改文案并重新生成</button>'
    : '';
  target.innerHTML = `<video id="latestVideo" class="latest-video" src="/api/jobs/${latest.id}/artifacts/final.mp4" preload="metadata" playsinline></video>
    <div class="latest-meta"><h3>${escapeHtml(latest.production_input?.topic || latest.id)}</h3><div class="latest-status"><img class="ui-icon" src="/icons/check.svg" alt="">${isAgentTestReview(latest) ? "代理测试审查完成 · 待用户最终验收" : "已生成 · 自动检查未发现阻断项 · 等待负责人验收"} · <span id="latestDuration">00:--</span></div>
    <div class="latest-actions"><button id="latestPlayButton" class="primary latest-play" type="button"><img class="ui-icon" src="/icons/play.svg" alt="">播放</button>${exactRevisionAction}<a href="/api/jobs/${latest.id}/artifacts/final.mp4" download="shiyi-${escapeHtml(latest.id)}-final.mp4"><img class="ui-icon" src="/icons/download.svg" alt="">下载成片</a><a href="/api/jobs/${latest.id}/evidence-report.html">查看验收报告</a><a href="/api/jobs/${latest.id}/public-evidence.zip"><img class="ui-icon" src="/icons/download.svg" alt="">下载交付材料（ZIP）</a></div><p class="delivery-hint">给运营和审核人员使用；ZIP 内先打开“00-验收报告.html”。</p></div>`;
  const video = document.getElementById("latestVideo");
  video.addEventListener("loadedmetadata", () => {
    const duration = Number(video.duration || 0);
    const minutes = Math.floor(duration / 60);
    const seconds = Math.round(duration % 60);
    document.getElementById("latestDuration").textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    if (duration > 4 && video.currentTime === 0) video.currentTime = Math.min(3, duration / 12);
  });
  document.getElementById("latestPlayButton").addEventListener("click", () => toggleLatestVideo());
  document.getElementById("latestEditButton")?.addEventListener("click", () => openContentRevision(latest).catch(error => toast(error.message, true)));
}

async function openContentRevision(job) {
  if (!job.current_run_id) throw new Error("当前任务没有可作为修改基准的成功成片");
  const scriptArtifact = await readJsonArtifact(`/api/jobs/${job.id}/artifacts/approved_script.json`);
  let script = String(scriptArtifact.data?.script || "").trim();
  const continuingPreservedRevision = isPreservedExactScriptFailure(job);
  if (continuingPreservedRevision) {
    const preserved = await readJsonArtifact(`/api/jobs/${job.id}/review-artifacts/approved_script.json`);
    script = String(preserved.data?.script || "").trim();
  }
  if (!script) throw new Error("当前任务没有可修改的文案");
  const panel = document.getElementById("activeJobPanel");
  panel.hidden = false;
  panel.innerHTML = `<div class="job-chat-head"><div><h2>${escapeHtml(job.production_input?.topic || job.id)}</h2><p>${continuingPreservedRevision ? "下面是上次渲染失败后保留下来的完整改稿。可以继续修改，也可以保持正文不变后再次生成；系统不会重新抽稿。" : "下面是当前成片实际使用的完整文案。直接改正文，保存后系统会重新检查、分镜、配音和生成。"}</p></div><span class="status-pill ${continuingPreservedRevision ? "status-danger" : "status-complete"}">${continuingPreservedRevision ? "改稿已保留" : "修改文案"}</span></div>
    <section class="script-revision-card">
      <label>编辑完整文案<textarea id="revisionCurrentScript" rows="12" maxlength="1200">${escapeHtml(script)}</textarea></label>
      <p class="reply-hint">${continuingPreservedRevision ? "可以按当前保存的正文再次生成，也可以继续修改；仍需保持约 45–60 秒口播。" : "必须实际修改正文并保持约 45–60 秒口播。"} 新版成功前，上一版成片仍可播放和下载。</p>
      <div class="gate-actions"><button class="primary" type="button" data-home-action="submit-script-revision">${continuingPreservedRevision ? "按这份文案重新生成" : "保存文案并重新生成"}</button><button class="quiet-link" type="button" data-home-action="cancel-script-revision">取消</button></div>
    </section>`;
  panel.dataset.revisionBaseRunId = String(job.current_run_id);
  panel.dataset.revisionBaseScriptSha256 = scriptArtifact.sha256;
  const editor = document.getElementById("revisionCurrentScript");
  editor.focus();
  editor.setSelectionRange(editor.value.length, editor.value.length);
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function submitContentRevision(job, button) {
  const script = document.getElementById("revisionCurrentScript")?.value.trim();
  if (!script) throw new Error("文案不能为空");
  const panel = document.getElementById("activeJobPanel");
  const body = JSON.stringify({
    script,
    base_run_id: panel.dataset.revisionBaseRunId,
    base_approved_script_sha256: panel.dataset.revisionBaseScriptSha256,
  });
  setBusy(button, true, "正在检查并保存…");
  const key = newIdempotencyKey();
  const submitOnce = () => api(`/api/jobs/${job.id}/script`, {
    method: "PATCH",
    headers: { "Idempotency-Key": key },
    body,
  });
  let result;
  try {
    result = await submitOnce();
  } catch (error) {
    if (!error.networkUncertain) throw error;
    result = await submitOnce();
  } finally {
    if (button.isConnected) setBusy(button, false);
  }
  const updated = result.job || result;
  const index = state.jobs.findIndex(item => item.id === job.id);
  if (index >= 0) state.jobs[index] = updated;
  else state.jobs.unshift(updated);
  toast("新文案已保存并通过检查，正在重新生成；上一版成片会保留到新版成功。");
  await renderHomeJob(updated);
  if (!runningStates.has(updated.status)) await advanceJob(job.id);
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
  const configuredModel = String(config.provider.model || "").trim();
  document.getElementById("modelName").textContent = configuredModel === "deepseek-v4-pro"
    ? "DeepSeek V4 Pro"
    : `沿用已有自定义模型：${configuredModel || "未识别"}`;
  document.getElementById("persistKey").checked = Boolean(config.provider.persisted_api_key);
  const storageText = config.provider.persisted_api_key ? "Key 已用 Windows 加密保存在本机" : (config.provider.has_api_key ? "已检测到本次使用的 Key" : "尚未填入 Key");
  const connectionText = state.status?.provider_connection_verified
    ? "本次连接已验证"
    : config.provider.has_api_key
      ? "尚未测试本次连接"
      : "填入 Key 后可测试本次连接";
  document.getElementById("providerResult").textContent = `${storageText}；${connectionText}。界面和日志不会显示完整 Key。`;
  const warning = document.getElementById("secretWarning");
  warning.hidden = !config.provider.secret_warning;
  warning.textContent = config.provider.secret_warning || "";
  document.getElementById("reviewModeState").textContent = isAgentTestReview()
    ? "Codex 浏览器审查（仅测试）"
    : isMechanicalReview()
      ? "反向机械审核（全自动）"
      : "用户本人审查";
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
    const mechanicalReview = isMechanicalReview(job);
    const busyAttribute = busy ? 'disabled aria-busy="true"' : "";
    const runButton = runnableStates.has(job.status) && !isTerminalMechanicalGenerationFailure(job) && job.production_input && !job.legacy_read_only ? `<button class="primary" data-action="run" data-job-id="${job.id}" ${busyAttribute}>${busy ? "执行中…" : "推进下一阶段"}</button>` : "";
    const retryButton = canRetryFailedJob(job) ? `<button class="primary" data-action="retry" data-job-id="${job.id}" ${busyAttribute}>${busy ? "正在重试…" : "重新生成这个选题"}</button>` : "";
    const exactRevisionButton = isPreservedExactScriptFailure(job) ? `<button class="primary" data-action="revise-exact" data-job-id="${job.id}" ${busyAttribute}>继续修改已保存文案</button>` : "";
    const visibleError = isTerminalMechanicalGenerationFailure(job)
      ? `选题没有被否决。${automaticFailureExplanation(job)}`
      : job.last_error;
    const runFacts = mechanicalReview
      ? `<span>请求 ${job.budget?.attempted || 0}/${job.budget?.limit || 7}</span><span>内部记录 ${job.runs?.length || 0}</span><span>${job.current_run_id ? "已有成片" : "尚无成片"}</span><span>选题来源：${escapeHtml(providerSourceLabelForJob(job))}</span>`
      : `<span>预算 ${job.budget?.attempted || 0}/${job.budget?.limit || 7}</span><span>尝试 ${job.runs?.length || 0}</span><span>当前成功 ${escapeHtml(job.current_run_id || "无")}</span>`;
    return `<div class="job"><div class="job-head"><div><h3>${escapeHtml(title)}</h3><small>${escapeHtml(job.id)} · ${escapeHtml(job.created_at)}</small></div><span class="pill ${statusClass(job.status)}">${escapeHtml(statusLabelForJob(job))}</span></div>${visibleError ? `<div class="notice">${escapeHtml(visibleError)}</div>` : ""}<div class="job-facts">${runFacts}</div><div class="action-row">${job.status === "planned" && !job.legacy_read_only ? `<button class="secondary" data-action="authorize" data-job-id="${job.id}" ${busyAttribute}>${busy ? "处理中…" : "批准任务执行"}</button>` : ""}${runButton}${retryButton}${exactRevisionButton}${job.production_input || job.legacy_read_only ? `<button class="secondary" data-action="open" data-job-id="${job.id}" ${busyAttribute}>${mechanicalReview ? "查看诊断" : "查看/精修"}</button>` : ""}</div></div>`;
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
  const currentRun = job.runs.find(run => run.run_id === job.current_run_id && ["render", "report_rebuild"].includes(run.stage) && run.status === "complete");
  const diagnostics = job.runs.filter(run => run !== currentRun && run.status !== "complete");
  const failedCount = diagnostics.length;
  const currentSummary = currentRun
    ? `<div class="run-summary"><div><strong>当前成片已完成</strong><small>${escapeHtml(currentRun.finished_at || currentRun.run_id)}</small></div><a href="/api/jobs/${job.id}/runs/${currentRun.run_id}/artifacts/final.mp4" download="shiyi-${escapeHtml(job.id)}-${escapeHtml(currentRun.run_id)}-final.mp4">下载成片</a></div>`
    : `<div class="run-summary run-summary-empty"><strong>本次尚未产生成片</strong><small>${failedCount ? `Agent 已记录 ${failedCount} 次未通过诊断，未发布不合格结果。` : "Agent 尚未进入成片阶段。"}</small></div>`;
  if (!diagnostics.length) { target.innerHTML = currentSummary; return; }
  const rows = [...diagnostics].reverse().map(run => {
    const current = run.run_id === job.current_run_id;
    const stageLabels = { research: "资料研究", content: "脚本生成", render: "成片装配", report_rebuild: "报告重建" };
    const statusText = run.status === "complete" ? "完成" : "未通过";
    return `<div class="run-row ${current ? "current" : ""}"><div><strong>${escapeHtml(stageLabels[run.stage] || run.stage)} · ${statusText}</strong><small>${escapeHtml(run.finished_at || run.started_at || run.run_id)}</small></div>${run.error ? `<small class="run-error">${escapeHtml(run.error)}</small>` : ""}</div>`;
  }).join("");
  target.innerHTML = `${currentSummary}<details class="diagnostic-history"><summary>内部诊断记录（${diagnostics.length} 次${failedCount ? `，${failedCount} 次未通过` : ""}）</summary><div class="diagnostic-runs">${rows}</div></details>`;
}

function syncApprovalButton(kind) {
  const select = document.getElementById(`${kind}Decision`);
  const button = document.getElementById(`submit${kind === "research" ? "Research" : "Compliance"}Btn`);
  const approved = select.value === "approved";
  button.className = approved ? "primary" : "danger";
  button.textContent = kind === "research" ? (approved ? "提交审核并进入下一步" : "提交审核并退回研究") : (approved ? "提交审核并进入渲染" : "提交审核并退回改稿");
}

async function openJob(id, { job: suppliedJob = null, scroll = true, managePolling = true } = {}) {
  const preservedScrollY = scroll ? null : window.scrollY;
  const job = suppliedJob || await api(`/api/jobs/${id}`);
  state.selectedJob = job;
  state.reviewFiles = {};
  let artifactPending = false;
  switchView("jobs", { scroll });
  const panel = document.getElementById("jobDetailPanel");
  panel.hidden = false;
  const badge = document.getElementById("jobDetailBadge");
  badge.textContent = statusLabelForJob(job);
  badge.className = `pill ${statusClass(job.status)}`;
  const agentTestReview = isAgentTestReview(job);
  const mechanicalReview = isMechanicalReview(job);
  const retryableFailure = canRetryFailedJob(job);
  const preservedExactFailure = isPreservedExactScriptFailure(job);
  const reviewModeLabel = agentTestReview
    ? "Codex 代理测试审查"
    : mechanicalReview ? "反向机械审核（全自动）" : "用户本人审查";
  const failureSummary = isTerminalMechanicalGenerationFailure(job)
    ? `<div class="failure-summary"><span>为什么没完成</span><strong>选题没有被否决。${escapeHtml(automaticFailureExplanation(job))}</strong></div>`
    : "";
  document.getElementById("jobSummary").innerHTML = `<div><span>选题</span><strong>${escapeHtml(job.production_input?.topic || "旧任务")}</strong></div><div><span>${mechanicalReview ? "请求" : "预算"}</span><strong>${job.budget?.attempted || 0}/${job.budget?.limit || 7}</strong></div><div><span>阶段审查</span><strong>${escapeHtml(reviewModeLabel)}</strong></div><div><span>本任务选题来源</span><strong>${escapeHtml(providerSourceLabelForJob(job))}</strong></div><div><span>${mechanicalReview ? "成片状态" : "当前成功运行"}</span><strong>${mechanicalReview ? (job.current_run_id ? "已完成" : "尚无成片") : escapeHtml(job.current_run_id || "无")}</strong></div>${failureSummary}`;
  const researchPanel = document.getElementById("researchApprovalPanel");
  const compliancePanel = document.getElementById("complianceApprovalPanel");
  researchPanel.hidden = mechanicalReview || job.status !== "awaiting_research_approval";
  compliancePanel.hidden = mechanicalReview || !["awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved"].includes(job.status);
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
  document.getElementById("durationEstimate").textContent = "内容阶段完成后才能在浏览器中改稿。";
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
    const researchIsStillPublishing = job.status === "research_running" || job.status === "awaiting_research_approval";
    if (researchMayExist && (error.networkUncertain || (error.httpStatus === 404 && researchIsStillPublishing))) {
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
    const contentIsStillPublishing = runningStates.has(job.status) || job.status === "awaiting_compliance_approval";
    if (contentMayExist && (error.networkUncertain || (error.httpStatus === 404 && contentIsStillPublishing))) {
      artifactPending = true;
      if (!compliancePanel.hidden) complianceSummary.textContent = "产物发布中，页面会自动重试。";
    } else if (contentMayExist && error.httpStatus === 404 && mechanicalReview) {
      complianceSummary.textContent = job.last_error || "Agent 未生成可供人工修改的脚本；不合格候选已保留为内部诊断。";
    } else if (contentMayExist && !compliancePanel.hidden) {
      complianceSummary.textContent = `合规产物读取失败：${error.message}`;
    }
  }
  const scriptField = document.getElementById("approvedScriptInput");
  const scriptLabel = scriptField.closest("label");
  const durationEstimate = document.getElementById("durationEstimate");
  const saveScriptButton = document.getElementById("saveScriptBtn");
  const rerunJobButton = document.getElementById("rerunJobBtn");
  const retryFailedJobButton = document.getElementById("retryFailedJobBtn");
  const continueExactScriptButton = document.getElementById("continueExactScriptBtn");
  scriptField.value = script;
  scriptLabel.hidden = !script;
  document.getElementById("approvedScriptLabelText").textContent = mechanicalReview ? "当前文案（修改请返回首页）" : "当前待审脚本";
  durationEstimate.hidden = !script;
  saveScriptButton.hidden = mechanicalReview;
  rerunJobButton.hidden = mechanicalReview;
  retryFailedJobButton.hidden = !retryableFailure;
  continueExactScriptButton.hidden = !preservedExactFailure;
  scriptField.disabled = mechanicalReview || job.legacy_read_only || !script;
  saveScriptButton.disabled = mechanicalReview || job.legacy_read_only || !script;
  rerunJobButton.disabled = mechanicalReview || job.legacy_read_only || !runnableStates.has(job.status) || state.busyJobs.has(job.id);
  retryFailedJobButton.disabled = !retryableFailure || state.busyJobs.has(job.id);
  continueExactScriptButton.disabled = !preservedExactFailure || state.busyJobs.has(job.id);
  durationEstimate.textContent = script ? `当前 ${script.length} 字；固定使用普通中文播报声与 -2% 语速，逐幕实测时长和语速，不做整轨变速。` : "内容阶段完成后才能在浏览器中改稿。";
  renderRunHistory(job);
  const technicalLinks = (job.artifacts || []).filter(name => name !== "final.mp4").map(name =>
    `<li><a href="/api/jobs/${id}/artifacts/${name}" target="_blank" rel="noreferrer">${escapeHtml(artifactLabels[name] || name)}</a><span>${escapeHtml(name)}</span></li>`).join("");
  const deliveryLinks = (job.artifacts || []).includes("final.mp4")
    ? `<div class="delivery-actions"><a href="/api/jobs/${id}/artifacts/final.mp4" download="shiyi-${escapeHtml(id)}-final.mp4">下载成片</a><a href="/api/jobs/${id}/evidence-report.html">查看验收报告</a></div>`
    : "";
  const publicEvidenceLink = job.current_run_id
    ? `<a href="/api/jobs/${id}/public-evidence.zip">下载交付材料（ZIP）</a><p class="delivery-hint">给运营和审核人员使用；ZIP 内先打开“00-验收报告.html”。</p>`
    : "";
  document.getElementById("artifactLinks").innerHTML = `${deliveryLinks}${publicEvidenceLink}${technicalLinks ? `<details class="technical-artifacts"><summary>技术附件（开发/复核人员使用）</summary><p>下面是机器可读记录，普通运营人员不需要阅读。</p><ul>${technicalLinks}</ul></details>` : ""}`;
  const video = document.getElementById("artifactVideo");
  if ((job.artifacts || []).includes("final.mp4")) { video.src = `/api/jobs/${id}/artifacts/final.mp4`; video.hidden = false; }
  else { video.hidden = true; video.removeAttribute("src"); }
  if (managePolling && (runningStates.has(job.status) || artifactPending)) {
    schedulePoll(job.id, { immediate: runningStates.has(job.status) });
  } else if (managePolling) stopPoll(job.id);
  if (scroll) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  else if (window.scrollY !== preservedScrollY) window.scrollTo({ top: preservedScrollY, behavior: "auto" });
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
      resizeComposer();
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
    if (!job && state.topicCandidates.length && message === state.currentGoal) {
      toast("这 3 个选题已经按当前要求生成；选中一个后直接点“就做这个”，不用再发送。", true);
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
    else if (action === "edit-script" && job) await openContentRevision(job);
    else if (action === "submit-script-revision" && job) await submitContentRevision(job, button);
    else if (action === "cancel-script-revision" && job) await renderHomeJob(job);
    else if (action === "retry-failed" && job) await retryFailedJob(job, button);
    else if (action === "new-task") {
      const originalGoal = String(
        job?.production_input?.capability_pack?.snapshot?.goal
        || state.currentGoal
        || DEFAULT_GOAL
      ).trim();
      setHomeJobId(null);
      state.topicCandidates = [];
      state.topicResponse = null;
      await loadTopics(originalGoal, { resetSeen: true });
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
      : button.dataset.action === "retry"
        ? (async () => {
            const job = state.jobs.find(item => item.id === id) || await api(`/api/jobs/${id}`);
            await retryFailedJob(job, button);
          })()
        : button.dataset.action === "revise-exact"
          ? (async () => {
              const job = state.jobs.find(item => item.id === id) || await api(`/api/jobs/${id}`);
              setHomeJobId(job.id);
              switchView("workbench");
              await renderHomeJob(job);
              await openContentRevision(job);
            })()
      : (async () => {
          if (!beginJobBusy(id)) return;
          try { await openJob(id); }
          finally { endJobBusy(id); }
        })();
  Promise.resolve(task).catch(error => toast(error.message, true));
});
document.getElementById("reloadJobs").addEventListener("click", () => refresh({ syncHomeView: false }).catch(error => toast(error.message, true)));
document.getElementById("rerunJobBtn").addEventListener("click", () => state.selectedJob && advanceJob(state.selectedJob.id).catch(error => toast(error.message, true)));
document.getElementById("retryFailedJobBtn").addEventListener("click", event => state.selectedJob && retryFailedJob(state.selectedJob, event.currentTarget).catch(error => toast(error.message, true)));
document.getElementById("continueExactScriptBtn").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  try {
    const job = state.selectedJob;
    setHomeJobId(job.id);
    switchView("workbench");
    await renderHomeJob(job);
    await openContentRevision(job);
  } catch (error) { toast(error.message, true); }
});
document.getElementById("researchDecision").addEventListener("change", () => syncApprovalButton("research"));
document.getElementById("complianceDecision").addEventListener("change", () => syncApprovalButton("compliance"));
document.getElementById("submitResearchBtn").addEventListener("click", () => submitResearch(document.getElementById("researchDecision").value).catch(error => toast(error.message, true)));
document.getElementById("submitComplianceBtn").addEventListener("click", () => submitCompliance(document.getElementById("complianceDecision").value).catch(error => toast(error.message, true)));
document.getElementById("saveScriptBtn").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  try {
    await api(`/api/jobs/${state.selectedJob.id}/script`, {
      method: "PATCH",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      body: JSON.stringify({
        script: document.getElementById("approvedScriptInput").value,
        editor: document.getElementById("complianceReviewer").value,
      }),
    });
    toast("改稿已保存，旧合规审批已失效");
    await refresh({ syncHomeView: false });
    await openJob(state.selectedJob.id);
  } catch (error) { toast(error.message, true); }
});

document.getElementById("saveSettings").addEventListener("click", async () => {
  try {
    const body = {
      // This customer form edits only the Key. The model shown above is the
      // model that remains configured; do not silently replace an existing
      // custom model merely because the customer saved a credential.
      provider: { api_key: document.getElementById("apiKey").value.trim(), persist_api_key: document.getElementById("persistKey").checked },
    };
    await api("/api/config", { method: "POST", body: JSON.stringify(body) });
    document.getElementById("apiKey").value = "";
    toast("DeepSeek Key 已保存");
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
    toast("已删除本机会话和 Windows 加密保存的 Key；系统环境变量中的 Key 不会被修改");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("testProvider").addEventListener("click", async () => {
  const target = document.getElementById("providerResult");
  target.textContent = "正在测试本次连接…";
  try {
    const result = await api("/api/provider/test", { method: "POST", body: "{}" });
    const verifiedModel = state.config?.provider?.model === "deepseek-v4-pro"
      ? "DeepSeek V4 Pro"
      : `已有自定义模型 ${state.config?.provider?.model || ""}`;
    target.textContent = `连接成功，${verifiedModel} 可用。`;
    await refresh({ syncHomeView: false });
    target.textContent = `本次连接已验证，${verifiedModel} 可用。`;
  } catch (error) { target.textContent = error.message; toast(error.message, true); }
});

const composer = document.getElementById("goalInput");
function resizeComposer() {
  if (!composer.isConnected || composer.offsetParent === null) return;
  composer.style.height = "auto";
  const styles = getComputedStyle(composer);
  const minHeight = Number.parseFloat(styles.minHeight) || 116;
  const cssMaxHeight = Number.parseFloat(styles.maxHeight) || 240;
  const maxHeight = Math.max(minHeight, Math.min(cssMaxHeight, window.innerHeight * 0.32));
  const contentHeight = Math.max(minHeight, composer.scrollHeight);
  composer.style.height = `${Math.min(contentHeight, maxHeight)}px`;
  composer.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
}
composer.addEventListener("input", resizeComposer);
composer.addEventListener("keydown", event => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.getElementById("agentComposer").requestSubmit();
  }
});
window.addEventListener("resize", resizeComposer);
resizeComposer();

bootstrapSession().then(() => refresh()).catch(error => toast(error.message, true));
