const state = { config: null, status: null, tools: [], jobs: [], plan: null, catalog: null, hardware: null, selectedJob: null };
const pipelineLabels = [
  ["content_insight", "内容洞察", "转写、OCR、范式提炼"],
  ["script_generation", "脚本生成", "品牌语气与多版本"],
  ["compliance_review", "证据与合规", "事实、功效、广告边界"],
  ["video_generation", "画面与语音", "视频、图片、配音"],
  ["video_editing", "自动合成", "时间线、字幕、MG"],
  ["human_refinement", "人工精修", "可编辑工程与确认"],
];

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = "toast"; }, 3200);
}

function setBusy(button, busy, text) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? text : button.dataset.label;
}

async function refresh() {
  const [status, config, toolsData, jobsData, catalog, hardware] = await Promise.all([
    api("/api/status"), api("/api/config"), api("/api/tools"), api("/api/jobs"), api("/api/catalog"), api("/api/hardware")
  ]);
  state.status = status; state.config = config; state.tools = toolsData.tools || []; state.jobs = jobsData.jobs || [];
  state.catalog = catalog; state.hardware = hardware;
  renderStatus(); renderSettings(); renderTools(toolsData); renderPipeline(); renderJobs(); renderCatalog();
}

function renderStatus() {
  const s = state.status;
  document.getElementById("providerBadge").textContent = s.provider_ready ? `${s.provider} · ${s.model}` : `${s.provider} · 未配置Key`;
  document.getElementById("metricTools").textContent = s.tool_count;
  document.getElementById("metricCaps").textContent = s.capabilities.length;
  document.getElementById("metricJobs").textContent = s.job_count;
  document.getElementById("metricModel").textContent = s.model;
}

function renderSettings() {
  const c = state.config;
  document.getElementById("providerName").value = c.provider.name || "DeepSeek";
  document.getElementById("baseUrl").value = c.provider.base_url || "";
  document.getElementById("modelName").value = c.provider.model || "";
  document.getElementById("rootsInput").value = (c.discovery.roots || []).join("\n");
  document.getElementById("providerResult").textContent = c.provider.has_api_key ? "已检测到API Key（不会在界面回显）" : "尚未检测到API Key";
  document.getElementById("storageRoot").value = c.storage.root || "D:\\时宜AIGC内容工厂";
  const names = { tools:"工具", models:"模型", downloads:"下载暂存", cache:"缓存", temp:"临时文件", logs:"日志", projects:"剪辑与生成项目" };
  document.getElementById("storageDirectories").innerHTML = Object.entries(c.storage.directories || {})
    .filter(([key]) => key !== "root")
    .map(([key, path]) => `<div><span>${escapeHtml(names[key] || key)}</span><code>${escapeHtml(path)}</code></div>`).join("");
}

function renderPipeline() {
  const caps = new Set((state.status && state.status.capabilities) || []);
  document.getElementById("pipeline").innerHTML = pipelineLabels.map(([key, name, desc], i) => `
    <div class="pipe-node ${caps.has(key) ? "ready" : ""}">
      <b>${String(i + 1).padStart(2, "0")}</b><strong>${name}</strong><span>${caps.has(key) ? "已发现候选工具" : desc}</span>
    </div>`).join("");
}

function renderTools(data = {}) {
  const grid = document.getElementById("toolGrid");
  if (!state.tools.length) {
    grid.innerHTML = `<article class="panel"><h2>尚未扫描</h2><p class="lead">填写目录后点击“开始只读扫描”。</p></article>`;
  } else {
    grid.innerHTML = state.tools.map(tool => `
      <article class="tool-card">
        <span class="confidence">置信度 ${Math.round(tool.confidence * 100)}%</span>
        <h3>${escapeHtml(tool.name)}</h3><div class="path mono">${escapeHtml(tool.path)}</div>
        <div class="tags">${tool.capabilities.map(cap => `<span class="tag">${escapeHtml(cap)}</span>`).join("")}</div>
        <p class="hint">入口：${tool.entrypoints.length ? tool.entrypoints.map(escapeHtml).join("、") : "需人工配置"}</p>
      </article>`).join("");
  }
  const report = data.report || {};
  document.getElementById("scanMeta").textContent = data.last_scan ? `上次扫描：${data.last_scan}；访问目录 ${report.visited_directories || 0}；发现 ${state.tools.length} 个候选项目` : "";
}

function renderPlan(result) {
  state.plan = result.plan;
  const plan = result.plan;
  document.getElementById("planPanel").hidden = false;
  document.getElementById("plannerBadge").textContent = result.fallback ? "本地规则计划" : "DeepSeek API计划";
  document.getElementById("planSummary").textContent = plan.summary || "";
  document.getElementById("planSteps").innerHTML = (plan.steps || []).map((step, i) => `
    <div class="plan-step"><span class="step-index">${i + 1}</span><div><strong>${escapeHtml(step.name || step.capability)}</strong><small>${escapeHtml(step.input || "")} → ${escapeHtml(step.output || "")}</small></div><span class="risk">${escapeHtml(step.risk || "")}</span></div>`).join("");
  const missing = document.getElementById("missingBox");
  missing.hidden = !(plan.missing || []).length;
  missing.textContent = (plan.missing || []).length ? `尚缺能力：${plan.missing.join("、")}` : "";
  document.getElementById("saveJobBtn").disabled = false;
  document.getElementById("planPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderJobs() {
  const list = document.getElementById("jobList");
  if (!state.jobs.length) { list.innerHTML = `<p class="lead">还没有任务。先在工作台生成计划并保存。</p>`; return; }
  list.innerHTML = state.jobs.map(job => `
    <div class="job"><div class="job-head"><div><h3>${escapeHtml(job.plan.goal || job.id)}</h3><small>${escapeHtml(job.id)} · ${escapeHtml(job.created_at)}</small></div><span class="pill">${escapeHtml(job.status)}</span></div>
      ${job.last_error ? `<div class="notice">${escapeHtml(job.last_error)}</div>` : ""}
      <div class="action-row">${job.status === "planned" ? `<button class="secondary" onclick="approveJob('${job.id}')">人工批准</button>` : ""}${["approved","failed","complete"].includes(job.status) && job.production_input ? `<button class="primary" onclick="runJob('${job.id}')">${job.status === "complete" ? "重新生成" : "运行生产线"}</button>` : ""}${job.production_input ? `<button class="secondary" onclick="openJob('${job.id}')">查看/精修</button>` : ""}</div>
    </div>`).join("");
  renderLatestArtifact();
}

function renderLatestArtifact() {
  const latest = state.jobs.find(job => job.status === "complete" && (job.artifacts || []).includes("final.mp4"));
  const el = document.getElementById("latestArtifact");
  if (!el) return;
  if (!latest) { el.innerHTML = `<strong>尚无成片</strong><span>完成任务后可在这里直接预览MP4和下载中间产物。</span>`; return; }
  const url = `/api/jobs/${latest.id}/artifacts/final.mp4`;
  el.innerHTML = `<strong>首条成片已完成</strong><span>${escapeHtml(latest.plan.goal || latest.id)}</span><a href="${url}" target="_blank">打开 final.mp4 →</a>`;
}

function renderCatalog() {
  const catalog = state.catalog || { packages: [], policy: {} };
  const result = state.hardware || {};
  const hardware = result.hardware || { gpu: {}, memory: {}, disks: [] };
  const recommended = new Set(result.recommended_package_ids || []);
  document.getElementById("hardwareProfile").textContent = result.profile ? result.profile.label : "未匹配硬件档位";
  document.getElementById("installPolicyBadge").textContent = result.auto_install_enabled ? "自动安装已开放" : "安装尚未开放";
  document.getElementById("catalogCount").textContent = `${catalog.packages.length} 个能力包`;
  const gpu = hardware.gpu || {};
  const memory = hardware.memory || {};
  document.getElementById("hardwareFacts").innerHTML = `
    <div><span>显卡</span><strong>${escapeHtml(gpu.name || "未检测到")}</strong></div>
    <div><span>显存</span><strong>${escapeHtml(gpu.vram_gb ?? 0)} GB</strong></div>
    <div><span>内存</span><strong>${escapeHtml(memory.total_gb ?? "未知")} GB</strong></div>
    <div><span>建议路线</span><strong>${escapeHtml((result.profile && result.profile.default_route) || "待判断")}</strong></div>
    <div class="wide"><span>默认安装位置</span><strong>${escapeHtml((result.storage && result.storage.root) || "未设置")}</strong></div>`;
  const blockedBelow = Number(catalog.policy.blocked_install_drives_when_free_gb_below || 20);
  const blocked = (hardware.disks || []).filter(disk => Number(disk.free_gb) < blockedBelow);
  const warning = document.getElementById("diskWarnings");
  warning.hidden = !blocked.length;
  warning.textContent = blocked.length ? `禁止安装到空间不足的磁盘：${blocked.map(d => `${d.root} 仅剩 ${d.free_gb}GB`).join("；")}` : "";

  document.getElementById("packageGrid").innerHTML = catalog.packages.map(pkg => {
    const sources = (pkg.sources || []).map(source => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.role)}</a>`).join(" · ") || "仅限当前内置运行时";
    return `<article class="tool-card package-card ${recommended.has(pkg.id) ? "recommended" : ""}">
      <span class="confidence">${recommended.has(pkg.id) ? "适合本机" : "其他档位"}</span>
      <h3>${escapeHtml(pkg.name)}</h3>
      <div class="path mono">${escapeHtml(pkg.id)}</div>
      <div class="tags">${(pkg.capabilities || []).slice(0, 4).map(cap => `<span class="tag">${escapeHtml(cap)}</span>`).join("")}</div>
      <p class="hint">许可：${escapeHtml(pkg.license)}<br>状态：${escapeHtml(pkg.install_status)}</p>
      <div class="source-links">${sources}</div>
      <button class="secondary package-install" disabled>等待版本固定与校验</button>
    </article>`;
  }).join("");
}

async function approveJob(id) { try { await api(`/api/jobs/${id}/approve`, { method: "POST", body: "{}" }); toast("任务已批准"); await refresh(); } catch (e) { toast(e.message, true); } }
async function runJob(id) {
  try {
    toast("生产线已启动：配音和合成可能需要数分钟");
    const job = await api(`/api/jobs/${id}/run`, { method: "POST", body: "{}" });
    toast(job.status === "complete" ? "端到端成片已完成" : "任务已检查");
    await refresh(); await openJob(id);
  } catch (e) { toast(e.message, true); await refresh(); }
}

async function openJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`); state.selectedJob = job;
    const panel = document.getElementById("jobDetailPanel"); panel.hidden = false;
    document.getElementById("jobDetailBadge").textContent = job.status;
    let script = "";
    if ((job.artifacts || []).includes("approved_script.json")) {
      const result = await fetch(`/api/jobs/${id}/artifacts/approved_script.json`).then(r => r.json()); script = result.script || "";
    }
    document.getElementById("approvedScriptInput").value = script;
    const labels = {"insight.json":"洞察","script_variants.json":"4个脚本","approved_script.json":"采用脚本","review.json":"合规审核","voice.wav":"配音","captions.srt":"字幕","final.mp4":"成片","run_report.json":"运行报告"};
    document.getElementById("artifactLinks").innerHTML = (job.artifacts || []).map(name => `<a href="/api/jobs/${id}/artifacts/${name}" target="_blank">${escapeHtml(labels[name] || name)}</a>`).join("");
    const video = document.getElementById("artifactVideo");
    if ((job.artifacts || []).includes("final.mp4")) { video.src = `/api/jobs/${id}/artifacts/final.mp4`; video.hidden = false; } else { video.hidden = true; video.removeAttribute("src"); }
    panel.scrollIntoView({behavior:"smooth", block:"start"});
  } catch (e) { toast(e.message, true); }
}
window.approveJob = approveJob; window.runJob = runJob; window.openJob = openJob;

function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  document.getElementById("pageTitle").textContent = ({workbench:"内容生产工作台", discovery:"本地能力发现", catalog:"可信能力商店", settings:"接口与安全设置", jobs:"运行记录"})[button.dataset.view];
}));

document.getElementById("planBtn").addEventListener("click", async () => {
  const button = document.getElementById("planBtn"); setBusy(button, true, "规划中…");
  try { const result = await api("/api/agent/plan", { method: "POST", body: JSON.stringify({ goal: document.getElementById("goalInput").value }) }); renderPlan(result); toast(result.fallback ? "已生成本地示范计划" : "DeepSeek 已生成执行计划"); }
  catch (e) { toast(e.message, true); } finally { setBusy(button, false); }
});

document.getElementById("saveJobBtn").addEventListener("click", async () => {
  try { await api("/api/jobs", { method: "POST", body: JSON.stringify({ plan: state.plan }) }); toast("任务已保存，等待人工批准"); await refresh(); }
  catch (e) { toast(e.message, true); }
});

document.getElementById("createDemoBtn").addEventListener("click", async () => {
  const button = document.getElementById("createDemoBtn"); setBusy(button, true, "创建中…");
  try {
    const production_input = { topic: document.getElementById("demoTopic").value.trim(), audience: document.getElementById("demoAudience").value.trim(), target_duration_seconds: 52, pattern_card_ids:["03","06"], voice_engine:"voxcpm2" };
    const job = await api("/api/demo-job", { method:"POST", body:JSON.stringify({production_input}) });
    toast("样片任务已创建，请到运行记录人工批准"); await refresh();
    document.querySelector('[data-view="jobs"]').click(); await openJob(job.id);
  } catch (e) { toast(e.message, true); } finally { setBusy(button, false); }
});

document.getElementById("saveScriptBtn").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  try {
    await api(`/api/jobs/${state.selectedJob.id}/script`, { method:"PATCH", body:JSON.stringify({script:document.getElementById("approvedScriptInput").value}) });
    toast("脚本已保存，任务等待重新生成"); await refresh();
  } catch (e) { toast(e.message, true); }
});

document.getElementById("rerunJobBtn").addEventListener("click", async () => {
  if (!state.selectedJob) return; await runJob(state.selectedJob.id);
});

document.getElementById("scanBtn").addEventListener("click", async () => {
  const button = document.getElementById("scanBtn"); setBusy(button, true, "扫描中…");
  try {
    const roots = document.getElementById("rootsInput").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    await api("/api/config", { method: "POST", body: JSON.stringify({ discovery: { ...state.config.discovery, roots } }) });
    const result = await api("/api/discover", { method: "POST", body: JSON.stringify({ roots }) });
    state.tools = result.tools; toast(`发现 ${result.count} 个候选项目`); await refresh();
  } catch (e) { toast(e.message, true); } finally { setBusy(button, false); }
});

document.getElementById("saveSettings").addEventListener("click", async () => {
  try {
    const body = {
      provider: { ...state.config.provider, name: document.getElementById("providerName").value.trim(), base_url: document.getElementById("baseUrl").value.trim(), model: document.getElementById("modelName").value.trim(), api_key: document.getElementById("apiKey").value.trim(), persist_api_key: document.getElementById("persistKey").checked },
      storage: { root: document.getElementById("storageRoot").value.trim() }
    };
    await api("/api/config", { method: "POST", body: JSON.stringify(body) }); document.getElementById("apiKey").value = ""; toast("设置已保存"); await refresh();
  } catch (e) { toast(e.message, true); }
});

document.getElementById("testProvider").addEventListener("click", async () => {
  const el = document.getElementById("providerResult"); el.textContent = "正在测试…";
  try { const result = await api("/api/provider/test", { method: "POST", body: "{}" }); el.textContent = `连接成功，可用模型 ${result.models.length} 个；当前模型${result.configured_model_available ? "可用" : "未在列表中"}`; }
  catch (e) { el.textContent = e.message; toast(e.message, true); }
});

document.getElementById("reloadJobs").addEventListener("click", refresh);
document.getElementById("refreshAll").addEventListener("click", refresh);
refresh().catch(e => toast(e.message, true));
