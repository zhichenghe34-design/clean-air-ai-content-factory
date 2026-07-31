const state = {
  config: null, status: null, tools: [], jobs: [], plan: null, catalog: null,
  hardware: null, selectedJob: null, csrf: "", reviewFiles: {}, runKeys: {}, busyJobs: new Set(),
};

const pipelineLabels = [
  ["web_extraction", "联网调研", "普通网页、动态页面、公开视频平台"],
  ["content_insight", "内容洞察", "转写、OCR、范式提炼"],
  ["script_generation", "脚本生成", "品牌语气与多版本"],
  ["compliance_review", "证据与合规", "事实、功效、广告边界"],
  ["video_generation", "画面与语音", "视频、图片、配音"],
  ["video_editing", "自动合成", "时间线、字幕、MG"],
  ["human_refinement", "人工精修", "人工审定与确认"],
];

const statusLabels = {
  planned: "待执行授权", authorized: "已授权，待研究", research_running: "研究执行中",
  awaiting_research_approval: "待研究审定", awaiting_research_revision: "研究已退回",
  research_approved: "研究已批准", content_running: "脚本生成中", blocked_compliance: "合规阻断",
  awaiting_compliance_approval: "待合规放行", awaiting_script_revision: "待人工改稿",
  compliance_approved: "合规已批准", rendering: "渲染中", complete: "已完成",
  failed: "运行失败", legacy_read_only: "旧任务（只读）", needs_attention: "等待适配器",
};

const runnableStates = new Set(["authorized", "research_approved", "compliance_approved", "failed", "awaiting_research_revision"]);
const artifactLabels = {
  "research.json": "研究证据", "insight.json": "内容洞察", "script_variants.json": "脚本方案",
  "approved_script.json": "最终脚本", "review.json": "合规检查", "voice.wav": "配音",
  "captions.srt": "字幕", "motion_plan.json": "动态导演计划", "final.mp4": "成片",
  "run_report.json": "运行报告", "manifest.json": "哈希清单", "approvals.json": "审批记录",
};

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
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin", cache: "no-store" });
  const data = await response.json().catch(() => ({ error: { message: `HTTP ${response.status}` } }));
  if (!response.ok) {
    const details = data.error?.details?.estimated_seconds ? `（预计 ${data.error.details.estimated_seconds} 秒）` : "";
    throw new Error(`${data.error?.message || `HTTP ${response.status}`}${details}`);
  }
  return data;
}

async function readJsonArtifact(url) {
  const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error?.message || `HTTP ${response.status}`);
  }
  const bytes = await response.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  const data = JSON.parse(new TextDecoder().decode(bytes));
  return { data, sha256 };
}

function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = "toast"; }, 3600);
}

function setBusy(button, busy, text) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? text : button.dataset.label;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function statusClass(status) {
  if (["complete", "research_approved", "compliance_approved"].includes(status)) return "status-ok";
  if (["failed", "blocked_compliance", "awaiting_script_revision", "awaiting_research_revision"].includes(status)) return "status-danger";
  if (String(status).includes("running") || status === "rendering") return "status-running";
  return "status-waiting";
}

function idempotencyKey(jobId) {
  if (!state.runKeys[jobId]) state.runKeys[jobId] = `ui-${crypto.randomUUID()}`;
  return state.runKeys[jobId];
}

async function refresh() {
  const [status, config, toolsData, jobsData, catalog, hardware] = await Promise.all([
    api("/api/status"), api("/api/config"), api("/api/tools"), api("/api/jobs"), api("/api/catalog"), api("/api/hardware"),
  ]);
  Object.assign(state, { status, config, tools: toolsData.tools || [], jobs: jobsData.jobs || [], catalog, hardware });
  renderStatus(); renderSettings(); renderTools(toolsData); renderPipeline(); renderJobs(); renderCatalog();
}

function renderStatus() {
  const current = state.status;
  document.getElementById("providerBadge").textContent = current.provider_ready ? `${current.provider} · ${current.model}` : `${current.provider} · 未配置 Key`;
  document.getElementById("portBadge").textContent = `${location.hostname}:${location.port || "80"} · v${current.version}`;
  document.getElementById("metricTools").textContent = current.tool_count;
  document.getElementById("metricCaps").textContent = current.capabilities.length;
  document.getElementById("metricJobs").textContent = current.job_count;
  document.getElementById("metricModel").textContent = current.model;
}

function renderSettings() {
  const config = state.config;
  document.getElementById("providerName").value = "DeepSeek";
  document.getElementById("baseUrl").value = config.provider.base_url || "https://api.deepseek.com";
  document.getElementById("modelName").value = config.provider.model || "";
  document.getElementById("researchEnabled").checked = config.research?.enabled !== false;
  document.getElementById("mediaParserRoot").value = config.research?.media_parser_root || "";
  document.getElementById("rootsInput").value = (config.discovery.roots || []).join("\n");
  const storageText = config.provider.persisted_api_key ? "Key 已用 Windows DPAPI 加密保存" : (config.provider.has_api_key ? "已检测到会话或环境变量 Key" : "尚未检测到 Key");
  document.getElementById("providerResult").textContent = `${storageText}；界面与日志均不回显密钥`;
  const warning = document.getElementById("secretWarning");
  warning.hidden = !config.provider.secret_warning;
  warning.textContent = config.provider.secret_warning || "";
  document.getElementById("storageRoot").value = config.storage.root || "";
  const names = { tools: "工具", models: "模型", downloads: "下载暂存", cache: "缓存", temp: "临时文件", logs: "日志", projects: "剪辑与生成项目" };
  document.getElementById("storageDirectories").innerHTML = Object.entries(config.storage.directories || {})
    .filter(([key]) => key !== "root")
    .map(([key, path]) => `<div><span>${escapeHtml(names[key] || key)}</span><code>${escapeHtml(path)}</code></div>`).join("");
}

function renderPipeline() {
  const caps = new Set(state.status?.capabilities || []);
  document.getElementById("pipeline").innerHTML = pipelineLabels.map(([key, name, desc], index) => `
    <div class="pipe-node ${caps.has(key) ? "ready" : ""}"><b>${String(index + 1).padStart(2, "0")}</b>
    <strong>${name}</strong><span>${caps.has(key) ? "已有受控能力" : desc}</span></div>`).join("");
}

function renderTools(data = {}) {
  const grid = document.getElementById("toolGrid");
  grid.innerHTML = state.tools.length ? state.tools.map(tool => `
    <article class="tool-card"><span class="confidence">置信度 ${Math.round(tool.confidence * 100)}%</span>
    <h3>${escapeHtml(tool.name)}</h3><div class="path mono">${escapeHtml(tool.path)}</div>
    <div class="tags">${(tool.capabilities || []).map(cap => `<span class="tag">${escapeHtml(cap)}</span>`).join("")}</div>
    <p class="hint">入口：${tool.entrypoints?.length ? tool.entrypoints.map(escapeHtml).join("、") : "需人工配置"}</p></article>`).join("")
    : `<article class="panel"><h2>尚未扫描</h2><p class="lead">填写目录后点击“开始只读扫描”。</p></article>`;
  const report = data.report || {};
  document.getElementById("scanMeta").textContent = data.last_scan ? `上次扫描：${data.last_scan}；访问目录 ${report.visited_directories || 0}；发现 ${state.tools.length} 个候选项目` : "";
}

function renderPlan(result) {
  state.plan = result.plan;
  const plan = result.plan;
  document.getElementById("planPanel").hidden = false;
  document.getElementById("plannerBadge").textContent = result.fallback ? "本地规则计划" : "DeepSeek API 计划";
  document.getElementById("planSummary").textContent = plan.summary || "";
  document.getElementById("planSteps").innerHTML = (plan.steps || []).map((step, index) => `
    <div class="plan-step"><span class="step-index">${index + 1}</span><div><strong>${escapeHtml(step.name || step.capability)}</strong>
    <small>${escapeHtml(step.input || "")} → ${escapeHtml(step.output || "")}</small></div><span class="risk">${escapeHtml(step.risk || "")}</span></div>`).join("");
  const missing = document.getElementById("missingBox");
  missing.hidden = !(plan.missing || []).length;
  missing.textContent = missing.hidden ? "" : `尚缺能力：${plan.missing.join("、")}`;
  document.getElementById("saveJobBtn").disabled = false;
}

function renderJobs() {
  const list = document.getElementById("jobList");
  if (!state.jobs.length) {
    list.innerHTML = `<p class="lead">还没有任务。先在工作台创建 v2 样片任务。</p>`;
    renderLatestArtifact();
    return;
  }
  list.innerHTML = state.jobs.map(job => {
    const title = job.plan?.goal || job.production_input?.topic || job.id;
    const busy = state.busyJobs.has(job.id);
    const runButton = runnableStates.has(job.status) && job.production_input && !job.legacy_read_only
      ? `<button class="primary" data-action="run" data-job-id="${job.id}" ${busy ? "disabled" : ""}>${busy ? "执行中…" : "推进下一阶段"}</button>` : "";
    return `<div class="job"><div class="job-head"><div><h3>${escapeHtml(title)}</h3><small>${escapeHtml(job.id)} · ${escapeHtml(job.created_at)}</small></div>
      <span class="pill ${statusClass(job.status)}">${escapeHtml(statusLabels[job.status] || job.status)}</span></div>
      ${job.last_error ? `<div class="notice">${escapeHtml(job.last_error)}</div>` : ""}
      <div class="job-facts"><span>预算 ${job.budget?.attempted || 0}/${job.budget?.limit || 7}</span><span>尝试 ${job.runs?.length || 0}</span><span>当前成功 ${escapeHtml(job.current_run_id || "无")}</span></div>
      <div class="action-row">${job.status === "planned" && !job.legacy_read_only ? `<button class="secondary" data-action="authorize" data-job-id="${job.id}">批准任务执行</button>` : ""}${runButton}
      ${job.production_input || job.legacy_read_only ? `<button class="secondary" data-action="open" data-job-id="${job.id}">查看/精修</button>` : ""}</div></div>`;
  }).join("");
  renderLatestArtifact();
}

function renderLatestArtifact() {
  const latest = state.jobs.find(job => job.current_run_id && (job.artifacts || []).includes("final.mp4"));
  const target = document.getElementById("latestArtifact");
  if (!latest) {
    target.innerHTML = `<strong>尚无 v2 成片</strong><span>失败尝试不会覆盖上一份成功产物。</span>`;
    return;
  }
  target.innerHTML = `<strong>当前成功成片</strong><span>${escapeHtml(latest.production_input?.topic || latest.id)}<br>${escapeHtml(latest.current_run_id)}</span>
    <a href="/api/jobs/${latest.id}/artifacts/final.mp4" target="_blank" rel="noreferrer">打开 final.mp4 →</a>`;
}

function renderCatalog() {
  const catalog = state.catalog || { packages: [], policy: {} };
  const result = state.hardware || {};
  const hardware = result.hardware || { gpu: {}, memory: {}, disks: [] };
  const recommended = new Set(result.recommended_package_ids || []);
  document.getElementById("hardwareProfile").textContent = result.profile?.label || "未匹配硬件档位";
  document.getElementById("installPolicyBadge").textContent = result.auto_install_enabled ? "自动安装已开放" : "安装尚未开放";
  document.getElementById("catalogCount").textContent = `${catalog.packages.length} 个能力包`;
  document.getElementById("hardwareFacts").innerHTML = `
    <div><span>显卡</span><strong>${escapeHtml(hardware.gpu?.name || "未检测到")}</strong></div>
    <div><span>显存</span><strong>${escapeHtml(hardware.gpu?.vram_gb ?? 0)} GB</strong></div>
    <div><span>内存</span><strong>${escapeHtml(hardware.memory?.total_gb ?? "未知")} GB</strong></div>
    <div><span>建议路线</span><strong>${escapeHtml(result.profile?.default_route || "待判断")}</strong></div>
    <div class="wide"><span>默认安装位置</span><strong>${escapeHtml(result.storage?.root || "未设置")}</strong></div>`;
  const threshold = Number(catalog.policy?.blocked_install_drives_when_free_gb_below || 20);
  const blocked = (hardware.disks || []).filter(disk => Number(disk.free_gb) < threshold);
  const warning = document.getElementById("diskWarnings");
  warning.hidden = !blocked.length;
  warning.textContent = blocked.length ? `禁止安装到空间不足的磁盘：${blocked.map(disk => `${disk.root} 仅剩 ${disk.free_gb}GB`).join("；")}` : "";
  document.getElementById("packageGrid").innerHTML = (catalog.packages || []).map(pkg => {
    const sources = (pkg.sources || []).map(source => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.role)}</a>`).join(" · ") || "仅限当前内置运行时";
    return `<article class="tool-card package-card ${recommended.has(pkg.id) ? "recommended" : ""}"><span class="confidence">${recommended.has(pkg.id) ? "适合本机" : "其他档位"}</span>
      <h3>${escapeHtml(pkg.name)}</h3><div class="path mono">${escapeHtml(pkg.id)}</div><div class="tags">${(pkg.capabilities || []).slice(0, 4).map(cap => `<span class="tag">${escapeHtml(cap)}</span>`).join("")}</div>
      <p class="hint">许可：${escapeHtml(pkg.license)}<br>状态：${escapeHtml(pkg.install_status)}</p><div class="source-links">${sources}</div><button class="secondary package-install" disabled>等待版本固定与校验</button></article>`;
  }).join("");
}

async function authorizeJob(id) {
  await api(`/api/jobs/${id}/approve`, { method: "POST", body: "{}" });
  toast("任务执行授权已记录");
  await refresh();
}

async function runJob(id) {
  if (state.busyJobs.has(id)) return;
  state.busyJobs.add(id); renderJobs();
  try {
    const job = await api(`/api/jobs/${id}/run`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey(id) }, body: "{}" });
    delete state.runKeys[id];
    toast(`阶段完成：${statusLabels[job.status] || job.status}`);
    await refresh(); await openJob(id);
  } catch (error) {
    toast(error.message, true); await refresh();
  } finally {
    state.busyJobs.delete(id); renderJobs();
  }
}

function renderRunHistory(job) {
  const target = document.getElementById("runHistory");
  if (!job.runs?.length) { target.innerHTML = `<p class="lead">暂无运行尝试。</p>`; return; }
  target.innerHTML = [...job.runs].reverse().map(run => {
    const current = run.run_id === job.current_run_id;
    const publicLinks = run.stage === "render" && run.status === "complete" ? `<a href="/api/jobs/${job.id}/runs/${run.run_id}/artifacts/manifest.json" target="_blank" rel="noreferrer">清单</a> <a href="/api/jobs/${job.id}/runs/${run.run_id}/artifacts/final.mp4" target="_blank" rel="noreferrer">成片</a>` : "不可公开";
    return `<div class="run-row ${current ? "current" : ""}"><div><strong>${escapeHtml(run.stage)} · ${escapeHtml(run.status)}</strong><small>${escapeHtml(run.run_id)}${current ? " · 当前成功" : ""}</small></div><div>${publicLinks}</div></div>`;
  }).join("");
}

function renderResearchFindings(findings) {
  const eligibleCount = findings.filter(item => item.auto_review_status === "eligible").length;
  const excludedCount = findings.length - eligibleCount;
  const summary = eligibleCount
    ? `共 ${findings.length} 条：${eligibleCount} 条等待你的逐项决定，${excludedCount} 条因缺少有效证据被自动排除。`
    : `共 ${findings.length} 条，但没有可进入脚本的证据。批准只会形成空证据集，建议退回研究。`;
  const rows = findings.map(item => {
    const sources = (item.source_urls || []).join(" · ") || "无可回溯来源";
    const excerpts = (item.evidence || []).map(entry => `<li><q>${escapeHtml(entry.excerpt || "")}</q><small>${escapeHtml(entry.url || "")}</small></li>`).join("");
    if (item.auto_review_status === "eligible") {
      return `<div class="finding" data-finding-id="${escapeHtml(item.finding_id)}"><strong>${escapeHtml(item.claim || "未命名结论")}</strong><small>${escapeHtml(sources)}</small>
        ${excerpts ? `<ul class="evidence-list">${excerpts}</ul>` : ""}
        <div><label>决定<select data-field="decision"><option value="approved">允许进入脚本</option><option value="rejected">拒绝</option></select></label>
        <label>证据类型<select data-field="evidence_type"><option value="paraphrase">转述 paraphrase</option><option value="verbatim">逐字 verbatim</option></select></label></div></div>`;
    }
    const limitations = (item.limitations || []).join("；") || "缺少满足要求的原文摘录";
    return `<div class="finding finding-excluded"><strong>${escapeHtml(item.claim || "未命名结论")}</strong><span class="finding-status">自动排除 · 不会进入脚本</span><small>${escapeHtml(sources)}</small>
      ${excerpts ? `<ul class="evidence-list">${excerpts}</ul>` : ""}<p>${escapeHtml(limitations)}</p></div>`;
  }).join("");
  return `<p class="lead">${summary}</p>${rows || `<p class="lead">研究未产生 finding，建议退回研究。</p>`}`;
}

async function openJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    state.selectedJob = job; state.reviewFiles = {};
    const panel = document.getElementById("jobDetailPanel"); panel.hidden = false;
    const badge = document.getElementById("jobDetailBadge"); badge.textContent = statusLabels[job.status] || job.status; badge.className = `pill ${statusClass(job.status)}`;
    document.getElementById("jobSummary").innerHTML = `<div><span>选题</span><strong>${escapeHtml(job.production_input?.topic || "旧任务")}</strong></div><div><span>预算</span><strong>${job.budget?.attempted || 0}/${job.budget?.limit || 7}</strong></div><div><span>当前成功运行</span><strong>${escapeHtml(job.current_run_id || "无")}</strong></div>`;
    const researchPanel = document.getElementById("researchApprovalPanel");
    const compliancePanel = document.getElementById("complianceApprovalPanel");
    researchPanel.hidden = job.status !== "awaiting_research_approval";
    compliancePanel.hidden = !["awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved"].includes(job.status);
    let script = "";
    try {
      state.reviewFiles.research = await readJsonArtifact(`/api/jobs/${id}/review-artifacts/research.json`);
      if (!researchPanel.hidden) {
        const findings = state.reviewFiles.research.data.findings || [];
        document.getElementById("researchFindings").innerHTML = renderResearchFindings(findings);
      }
    } catch (_) { /* research may not exist before the first stage */ }
    try {
      state.reviewFiles.script = await readJsonArtifact(`/api/jobs/${id}/review-artifacts/approved_script.json`);
      script = state.reviewFiles.script.data.script || "";
      state.reviewFiles.review = await readJsonArtifact(`/api/jobs/${id}/review-artifacts/review.json`);
      const review = state.reviewFiles.review.data;
      document.getElementById("complianceSummary").textContent = review.status === "blocked" ? `自动检查：阻断。${(review.warnings || []).map(item => item.message).join("；")}` : `自动检查：${review.status === "passed" ? "通过" : "需人工确认"}。即使自动通过，也必须由你亲自放行。`;
    } catch (_) { /* content may not exist yet */ }
    document.getElementById("approvedScriptInput").value = script;
    document.getElementById("approvedScriptInput").disabled = job.legacy_read_only || !script;
    document.getElementById("saveScriptBtn").disabled = job.legacy_read_only || !script;
    document.getElementById("rerunJobBtn").disabled = job.legacy_read_only || !runnableStates.has(job.status);
    document.getElementById("durationEstimate").textContent = script ? `当前 ${script.length} 字；保存时将按标点加权校验 35–75 秒，配音后只允许 0.75–1.5 倍安全变速。` : "内容阶段完成后才能人工改稿。";
    renderRunHistory(job);
    document.getElementById("artifactLinks").innerHTML = (job.artifacts || []).map(name => `<a href="/api/jobs/${id}/artifacts/${name}" target="_blank" rel="noreferrer">${escapeHtml(artifactLabels[name] || name)}</a>`).join("");
    const video = document.getElementById("artifactVideo");
    if ((job.artifacts || []).includes("final.mp4")) { video.src = `/api/jobs/${id}/artifacts/final.mp4`; video.hidden = false; }
    else { video.hidden = true; video.removeAttribute("src"); }
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message, true); }
}

async function submitResearch(decision) {
  const job = state.selectedJob;
  if (!job || !state.reviewFiles.research) return;
  const findings = [...document.querySelectorAll("#researchFindings .finding[data-finding-id]")].map(row => ({
    finding_id: row.dataset.findingId,
    decision: row.querySelector('[data-field="decision"]').value,
    evidence_type: row.querySelector('[data-field="evidence_type"]').value,
  }));
  await api(`/api/jobs/${job.id}/approvals/research`, { method: "POST", body: JSON.stringify({
    decision, reviewer: document.getElementById("researchReviewer").value.trim(), note: document.getElementById("researchNote").value.trim(),
    artifact_sha256: state.reviewFiles.research.sha256, findings,
  }) });
  toast(decision === "approved" ? "研究证据已由你批准" : "研究已退回"); await refresh(); await openJob(job.id);
}

async function submitCompliance(decision) {
  const job = state.selectedJob;
  if (!job || !state.reviewFiles.review || !state.reviewFiles.script) return;
  await api(`/api/jobs/${job.id}/approvals/compliance`, { method: "POST", body: JSON.stringify({
    decision, reviewer: document.getElementById("complianceReviewer").value.trim(), note: document.getElementById("complianceNote").value.trim(),
    artifact_sha256: state.reviewFiles.review.sha256, script_sha256: state.reviewFiles.script.sha256,
  }) });
  toast(decision === "approved" ? "最终脚本已由你放行" : "脚本已退回改稿"); await refresh(); await openJob(job.id);
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  document.getElementById("pageTitle").textContent = ({ workbench: "内容生产工作台", discovery: "本地能力发现", catalog: "可信能力商店", settings: "接口与安全设置", jobs: "运行记录" })[button.dataset.view];
}));

document.getElementById("jobList").addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const id = button.dataset.jobId;
  const operation = action === "authorize" ? authorizeJob(id) : action === "run" ? runJob(id) : openJob(id);
  Promise.resolve(operation).catch(error => toast(error.message, true));
});

document.getElementById("planBtn").addEventListener("click", async () => {
  const button = document.getElementById("planBtn"); setBusy(button, true, "规划中…");
  try { renderPlan(await api("/api/agent/plan", { method: "POST", body: JSON.stringify({ goal: document.getElementById("goalInput").value }) })); toast("执行计划已生成"); }
  catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
});

document.getElementById("saveJobBtn").addEventListener("click", async () => {
  try { await api("/api/jobs", { method: "POST", body: JSON.stringify({ plan: state.plan }) }); toast("任务已保存，等待执行授权"); await refresh(); }
  catch (error) { toast(error.message, true); }
});

document.getElementById("createDemoBtn").addEventListener("click", async () => {
  const button = document.getElementById("createDemoBtn"); setBusy(button, true, "创建中…");
  try {
    const production_input = { topic: document.getElementById("demoTopic").value.trim(), audience: document.getElementById("demoAudience").value.trim(), target_duration_seconds: 52, pattern_card_ids: ["03", "06"], voice_engine: "voxcpm2", enable_web_research: true };
    const job = await api("/api/demo-job", { method: "POST", body: JSON.stringify({ production_input }) });
    toast("v2 任务已创建，请先批准执行"); await refresh(); document.querySelector('[data-view="jobs"]').click(); await openJob(job.id);
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
});

document.getElementById("saveScriptBtn").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  try { await api(`/api/jobs/${state.selectedJob.id}/script`, { method: "PATCH", body: JSON.stringify({ script: document.getElementById("approvedScriptInput").value }) }); toast("改稿已保存，旧合规审批已失效"); await refresh(); await openJob(state.selectedJob.id); }
  catch (error) { toast(error.message, true); }
});
document.getElementById("rerunJobBtn").addEventListener("click", () => state.selectedJob && runJob(state.selectedJob.id));
document.getElementById("approveResearchBtn").addEventListener("click", () => submitResearch("approved").catch(error => toast(error.message, true)));
document.getElementById("rejectResearchBtn").addEventListener("click", () => submitResearch("rejected").catch(error => toast(error.message, true)));
document.getElementById("approveComplianceBtn").addEventListener("click", () => submitCompliance("approved").catch(error => toast(error.message, true)));
document.getElementById("rejectComplianceBtn").addEventListener("click", () => submitCompliance("rejected").catch(error => toast(error.message, true)));

document.getElementById("scanBtn").addEventListener("click", async () => {
  const button = document.getElementById("scanBtn"); setBusy(button, true, "扫描中…");
  try {
    const roots = document.getElementById("rootsInput").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    await api("/api/config", { method: "POST", body: JSON.stringify({ discovery: { ...state.config.discovery, roots } }) });
    const result = await api("/api/discover", { method: "POST", body: JSON.stringify({ roots }) });
    toast(`发现 ${result.count} 个候选项目`); await refresh();
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
});

document.getElementById("saveSettings").addEventListener("click", async () => {
  try {
    const body = {
      provider: { model: document.getElementById("modelName").value.trim(), base_url: "https://api.deepseek.com", api_key: document.getElementById("apiKey").value.trim(), persist_api_key: document.getElementById("persistKey").checked },
      research: { ...state.config.research, enabled: document.getElementById("researchEnabled").checked, media_parser_root: document.getElementById("mediaParserRoot").value.trim() },
      storage: { root: document.getElementById("storageRoot").value.trim() },
    };
    await api("/api/config", { method: "POST", body: JSON.stringify(body) }); document.getElementById("apiKey").value = ""; toast("设置已保存"); await refresh();
  } catch (error) { toast(error.message, true); }
});

document.getElementById("testProvider").addEventListener("click", async () => {
  const target = document.getElementById("providerResult"); target.textContent = "正在测试（不计任务预算）…";
  try { const result = await api("/api/provider/test", { method: "POST", body: "{}" }); target.textContent = `连接成功；可用模型 ${result.models.length} 个；本次测试不计任务预算`; }
  catch (error) { target.textContent = error.message; toast(error.message, true); }
});

document.getElementById("reloadJobs").addEventListener("click", () => refresh().catch(error => toast(error.message, true)));
document.getElementById("refreshAll").addEventListener("click", () => refresh().catch(error => toast(error.message, true)));

bootstrapSession().then(refresh).catch(error => toast(error.message, true));
