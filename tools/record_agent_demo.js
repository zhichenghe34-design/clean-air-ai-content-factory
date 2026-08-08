const fs = require('fs');
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');
const { chromium } = require('playwright');

const REPO_ROOT = path.resolve(__dirname, '..');
const STATIC_ROOT = path.join(REPO_ROOT, 'static');
const EVIDENCE_VIDEO = path.join(
  REPO_ROOT,
  'evidence',
  'v2-real-deepseek-20260801-022153',
  'final.mp4',
);
const OUTPUT_VIDEO = path.resolve(
  process.env.CONTENT_FACTORY_DEMO_OUTPUT || path.join(REPO_ROOT, 'media', 'agent-workbench-demo.mp4'),
);
const OUTPUT_SCREENSHOT = path.resolve(
  process.env.CONTENT_FACTORY_DEMO_SCREENSHOT || path.join(REPO_ROOT, 'docs', 'assets', 'agent-workbench.png'),
);
const QA_DIR = path.resolve(
  process.env.CONTENT_FACTORY_DEMO_QA || path.join(REPO_ROOT, 'tmp', 'demo-qa'),
);
const TARGET_SECONDS = 78;
const VIEWPORT = { width: 1440, height: 900 };
const PREVIEW_PORT = Number(process.env.CONTENT_FACTORY_DEMO_PORT || 8877);
const PUBLISH_STAGING_DIR = path.join(QA_DIR, `publish-staging-${process.pid}`);
const STAGED_VIDEO = path.join(PUBLISH_STAGING_DIR, 'agent-workbench-demo.mp4');
const STAGED_SCREENSHOT = path.join(PUBLISH_STAGING_DIR, 'agent-workbench.png');
const STAGED_INTRO_FRAME = path.join(PUBLISH_STAGING_DIR, 'demo-intro-frame.png');

const MIME_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

const topics = [
  {
    id: 'topic-1',
    title: '通风后没有气味，甲醛就安全了吗？',
    reason: '从常见误区切入，适合新房家庭。',
    audience: '新房家庭',
  },
  {
    id: 'topic-2',
    title: '除醛率为什么要看检测条件？',
    reason: '数字有冲突感，也能自然引出证据核验。',
    audience: '新房家庭',
  },
  {
    id: 'topic-3',
    title: '检测报告应该先看哪些信息？',
    reason: '实用清单型，便于收藏和转发。',
    audience: '新房家庭',
  },
];

const researchArtifact = {
  findings: [
    {
      finding_id: 'finding-1',
      claim: '检测结论需要结合检测条件理解。',
      auto_review_status: 'eligible',
      review_summary: '除醛率数字必须与检测条件一起阅读。',
      allowed_use: '说明同一个数字离开样品、舱体、时间等条件不能直接横向比较。',
      prohibited_use: '不得把实验条件下的数据改写成真实家庭的效果保证。',
      evidence: [{ excerpt: '检测数据应结合对应条件解读。', url: 'https://example.invalid/evidence/1' }],
      source_urls: ['https://example.invalid/evidence/1'],
    },
    {
      finding_id: 'finding-2',
      claim: '气味不能替代规范检测。',
      auto_review_status: 'eligible',
      review_summary: '闻不到气味不等于检测结果合格。',
      allowed_use: '提醒观众把气味感受与规范检测区分开。',
      prohibited_use: '不得只凭嗅觉宣布室内空气安全或不安全。',
      evidence: [{ excerpt: '感官判断不能替代规范检测。', url: 'https://example.invalid/evidence/2' }],
      source_urls: ['https://example.invalid/evidence/2'],
    },
    {
      finding_id: 'finding-3',
      claim: '检测报告应核对检测对象、方法和条件。',
      auto_review_status: 'eligible',
      review_summary: '先核对检测对象、方法和条件，再阅读结果。',
      allowed_use: '提供不带功效承诺的报告阅读顺序。',
      prohibited_use: '不得据此替任何产品或治理方案背书。',
      evidence: [{ excerpt: '报告结果对应其记录的对象、方法和条件。', url: 'https://example.invalid/evidence/3' }],
      source_urls: ['https://example.invalid/evidence/3'],
    },
    {
      finding_id: 'finding-4',
      claim: '一次治理可以永久解决甲醛问题。',
      auto_review_status: 'rejected',
      review_summary: '“一次治理永久有效”缺少可核验边界。',
      prohibited_use: '绝对化、永久性功效承诺必须排除。',
      evidence: [],
      source_urls: [],
    },
    {
      finding_id: 'finding-5',
      claim: '某方法在所有家庭中都能达到固定除醛率。',
      auto_review_status: 'rejected',
      review_summary: '固定数字被错误外推到所有家庭场景。',
      prohibited_use: '没有对应条件和来源，不得进入脚本。',
      evidence: [],
      source_urls: [],
    },
  ],
  strict_audit: {
    premise: 'all_claims_are_false_until_proven',
    passed_count: 3,
    rejected_count: 2,
  },
};

const approvedScriptArtifact = {
  script: '看到“高除醛率”，先别急着下结论。第一，看检测对象和方法；第二，看舱体、时间等测试条件；第三，确认这个数字有没有被外推成真实家庭里的效果保证。检测报告能说明特定条件下的结果，不能替代对具体居住环境的判断。通风、规范检测和持续观察，应当根据实际情况组合使用。',
};

function createCompleteJob(id, topic, runId) {
  return {
    schema_version: 2,
    id,
    status: 'complete',
    created_at: '2026-08-01T15:00:00+08:00',
    production_input: {
      topic,
      audience: '新房家庭',
      target_duration_seconds: 52,
      enable_web_research: true,
    },
    approvals: {
      research: { status: 'approved' },
      compliance: { status: 'approved' },
    },
    budget: { limit: 7, attempted: 7, succeeded: 7, failed: 0 },
    runs: [{ run_id: runId, stage: 'render', status: 'complete' }],
    artifacts: ['final.mp4', 'manifest.json', 'approvals.json'],
    current_run_id: runId,
  };
}

function createFixture() {
  const calls = [];
  const previousJob = createCompleteJob(
    'evidence-job',
    '除醛率为什么要看检测条件？',
    'run-evidence-20260801-022153',
  );
  let job = null;
  let runCount = 0;

  const reply = (route, body, status = 200) => route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });

  const jobs = () => job ? [job, previousJob] : [previousJob];
  const record = name => { calls.push(name); };

  return {
    calls,
    get job() { return job; },
    async route(route) {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;

      if (pathname.endsWith('/artifacts/final.mp4')) {
        await route.continue();
        return;
      }
      if (pathname === '/api/session') {
        await reply(route, { csrf_token: 'demo-csrf-token' });
        return;
      }
      if (pathname === '/api/status') {
        await reply(route, {
          version: '0.2.0',
          provider_ready: true,
          provider_has_key: true,
          provider_key_saved: true,
          provider_configured: true,
          provider_connection_verified: false,
          provider_state: 'configured',
          provider_verification_status: 'not_tested',
          model: 'deepseek-chat',
          tool_count: 4,
          job_count: jobs().length,
          capabilities: Array.from({ length: 13 }, (_, index) => `cap-${index + 1}`),
        });
        return;
      }
      if (pathname === '/api/config') {
        await reply(route, {
          provider: {
            base_url: 'https://api.deepseek.com',
            model: 'deepseek-chat',
            has_api_key: true,
            persisted_api_key: true,
            secret_warning: '',
          },
          research: { enabled: true, media_parser_root: '' },
          discovery: { roots: [] },
          storage: { root: '本机受控目录', directories: {} },
        });
        return;
      }
      if (pathname === '/api/tools') {
        await reply(route, { tools: [], report: {}, last_scan: null });
        return;
      }
      if (pathname === '/api/catalog') {
        await reply(route, { packages: [], policy: { blocked_install_drives_when_free_gb_below: 20 } });
        return;
      }
      if (pathname === '/api/hardware') {
        await reply(route, {
          hardware: { gpu: {}, memory: {}, disks: [] },
          profile: { label: '固定演示环境', default_route: '受控本地执行' },
          recommended_package_ids: [],
          auto_install_enabled: false,
          storage: { root: '本机受控目录' },
        });
        return;
      }
      if (pathname === '/api/jobs' && request.method() === 'GET') {
        await reply(route, { jobs: jobs() });
        return;
      }
      if (pathname === '/api/agent/topics') {
        record('topics');
        await new Promise(resolve => setTimeout(resolve, 500));
        const pretaskProviderBudget = {
          limit: 3,
          attempted: 1,
          succeeded: 1,
          failed: 0,
          remaining: 2,
          events: [],
        };
        await reply(route, {
          source: 'deepseek',
          selection_bundle_id: 'selection-fixed-agent-demo-0000000001',
          notice: '',
          screening: '排除越域/明显夸大/重复；公开依据将在研究阶段逐条核验；选题 Provider 1/3（固定演示）。',
          topic_provider_budget: pretaskProviderBudget,
          pretask_provider_budget: { ...pretaskProviderBudget },
          candidates: topics,
        });
        return;
      }
      if (pathname === '/api/demo-job') {
        record('create');
        const createPayload = request.postDataJSON();
        const selected = topics.find(item => item.id === createPayload.candidate_id);
        const input = {
          topic: selected.title,
          audience: selected.audience,
          ...createPayload.production_options,
          selection_bundle_id: createPayload.selection_bundle_id,
          candidate_id: createPayload.candidate_id,
        };
        job = {
          schema_version: 2,
          id: 'demo-agent-job',
          status: 'planned',
          created_at: '2026-08-01T15:08:00+08:00',
          production_input: input,
          approvals: { research: { status: 'pending' }, compliance: { status: 'pending' } },
          budget: { limit: 7, attempted: 0, succeeded: 0, failed: 0 },
          runs: [],
          artifacts: [],
          current_run_id: null,
        };
        await new Promise(resolve => setTimeout(resolve, 700));
        await reply(route, job, 201);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/approve') {
        record('authorize');
        job = { ...job, status: 'authorized' };
        await new Promise(resolve => setTimeout(resolve, 600));
        await reply(route, job);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/run') {
        runCount += 1;
        record(`run-${runCount}`);
        const states = [
          {
            status: 'awaiting_research_approval',
            budget: { limit: 7, attempted: 3, succeeded: 3, failed: 0 },
            run: { run_id: 'run-research-demo', stage: 'research', status: 'complete' },
            delay: 6000,
          },
          {
            status: 'awaiting_compliance_approval',
            budget: { limit: 7, attempted: 6, succeeded: 6, failed: 0 },
            run: { run_id: 'run-content-demo', stage: 'content', status: 'complete' },
            delay: 6000,
          },
          {
            status: 'complete',
            budget: { limit: 7, attempted: 7, succeeded: 7, failed: 0 },
            run: { run_id: 'run-render-demo', stage: 'render', status: 'complete' },
            delay: 8000,
          },
        ];
        const next = states[runCount - 1];
        if (!next) {
          await reply(route, { error: { message: '演示夹具不允许额外运行' } }, 409);
          return;
        }
        await new Promise(resolve => setTimeout(resolve, next.delay));
        const nextRuns = [...(job.runs || []), next.run];
        job = {
          ...job,
          status: next.status,
          budget: next.budget,
          runs: nextRuns,
          current_run_id: runCount === 3 ? 'run-render-demo' : null,
          artifacts: runCount === 3 ? ['final.mp4', 'manifest.json', 'approvals.json'] : [],
        };
        await reply(route, job);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/review-artifacts/research.json') {
        await reply(route, researchArtifact);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/review-artifacts/approved_script.json') {
        await reply(route, approvedScriptArtifact);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/review-artifacts/review.json') {
        await reply(route, { status: 'passed', blocked: false, warnings: [] });
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/approvals/research') {
        record('research-approval');
        job = {
          ...job,
          status: 'research_approved',
          approvals: { ...job.approvals, research: { status: 'approved' } },
        };
        await new Promise(resolve => setTimeout(resolve, 600));
        await reply(route, job);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job/approvals/compliance') {
        record('compliance-approval');
        job = {
          ...job,
          status: 'compliance_approved',
          approvals: { ...job.approvals, compliance: { status: 'approved' } },
        };
        await new Promise(resolve => setTimeout(resolve, 600));
        await reply(route, job);
        return;
      }
      if (pathname === '/api/jobs/demo-agent-job') {
        await reply(route, job);
        return;
      }
      await reply(route, { error: { message: `固定演示夹具未定义 ${request.method()} ${pathname}` } }, 404);
    },
  };
}

function sendStaticFile(response, filePath) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('Not found');
    return;
  }
  response.writeHead(200, {
    'Content-Type': MIME_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
    'Cache-Control': 'no-store',
  });
  fs.createReadStream(filePath).pipe(response);
}

function sendVideo(request, response) {
  const stat = fs.statSync(EVIDENCE_VIDEO);
  const range = request.headers.range;
  if (!range) {
    response.writeHead(200, {
      'Content-Type': 'video/mp4',
      'Content-Length': stat.size,
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'no-store',
    });
    fs.createReadStream(EVIDENCE_VIDEO).pipe(response);
    return;
  }
  const match = /^bytes=(\d*)-(\d*)$/.exec(range);
  if (!match) {
    response.writeHead(416, { 'Content-Range': `bytes */${stat.size}` });
    response.end();
    return;
  }
  const start = match[1] ? Number(match[1]) : 0;
  const end = match[2] ? Math.min(Number(match[2]), stat.size - 1) : stat.size - 1;
  response.writeHead(206, {
    'Content-Type': 'video/mp4',
    'Content-Length': end - start + 1,
    'Content-Range': `bytes ${start}-${end}/${stat.size}`,
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'no-store',
  });
  fs.createReadStream(EVIDENCE_VIDEO, { start, end }).pipe(response);
}

function startStaticServer() {
  if (!Number.isInteger(PREVIEW_PORT) || PREVIEW_PORT < 1024 || PREVIEW_PORT > 65535) {
    throw new Error('CONTENT_FACTORY_DEMO_PORT 必须是 1024–65535 之间的整数');
  }
  const server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname);
    if (pathname.endsWith('/artifacts/final.mp4')) {
      sendVideo(request, response);
      return;
    }
    if (pathname === '/favicon.ico') {
      response.writeHead(204);
      response.end();
      return;
    }
    const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
    const resolved = path.resolve(STATIC_ROOT, relative);
    if (!resolved.startsWith(`${STATIC_ROOT}${path.sep}`) && resolved !== path.join(STATIC_ROOT, 'index.html')) {
      response.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
      response.end('Forbidden');
      return;
    }
    sendStaticFile(response, resolved);
  });
  return new Promise((resolve, reject) => {
    server.once('error', error => {
      if (error.code === 'EADDRINUSE') reject(new Error(`独立预览端口 ${PREVIEW_PORT} 已被占用；请通过 CONTENT_FACTORY_DEMO_PORT 指定另一个明确端口`));
      else reject(error);
    });
    server.listen(PREVIEW_PORT, '127.0.0.1', () => resolve({
      server,
      port: PREVIEW_PORT,
      url: `http://127.0.0.1:${PREVIEW_PORT}`,
    }));
  });
}

async function installFixture(page, fixture, errors) {
  await page.addInitScript(fixedTimestamp => {
    const NativeDate = Date;
    class FixedDate extends NativeDate {
      constructor(...args) {
        super(...(args.length ? args : [fixedTimestamp]));
      }
      static now() { return fixedTimestamp; }
    }
    window.Date = FixedDate;
  }, new Date('2026-08-01T15:08:00+08:00').getTime());
  page.on('console', message => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  await page.route('**/api/**', route => fixture.route(route));
}

async function waitForWorkbench(page) {
  await page.locator('#topicCandidates .topic-option').first().waitFor({ state: 'visible' });
  await page.locator('#latestVideo').waitFor({ state: 'visible' });
  await page.locator('#latestVideo').evaluate(async video => {
    if (video.readyState < 1) await new Promise(resolve => video.addEventListener('loadedmetadata', resolve, { once: true }));
    video.currentTime = 3;
    video.pause();
  });
}

async function addDemoOverlay(page) {
  await page.evaluate(() => {
    const style = document.createElement('style');
    style.id = 'demo-overlay-style';
    style.textContent = `
      #demo-proof-ribbon {
        position: fixed; z-index: 10000; top: 24px; left: 50%; transform: translateX(-50%);
        padding: 8px 16px; border-radius: 999px; color: #f7fff0; background: rgba(3, 35, 29, .93);
        box-shadow: 0 8px 24px rgba(3, 35, 29, .18); font: 600 13px/1.2 "Noto Sans SC", sans-serif;
        pointer-events: none; white-space: nowrap;
      }
      #demo-chapter-card {
        position: fixed; z-index: 10000; left: 50%; bottom: 22px; transform: translateX(-50%);
        min-width: 430px; max-width: 760px; padding: 13px 20px; border: 1px solid rgba(189, 246, 59, .55);
        border-radius: 14px; color: #fff; background: rgba(3, 26, 23, .94); box-shadow: 0 14px 40px rgba(3, 26, 23, .25);
        text-align: center; font: 700 17px/1.35 "Noto Sans SC", sans-serif; pointer-events: none;
        opacity: 1; transition: opacity .25s ease, transform .25s ease;
      }
      #demo-chapter-card small { display: block; margin-top: 4px; color: #c9d8d2; font-size: 12px; font-weight: 500; }
      #demo-cursor {
        position: fixed; z-index: 10001; width: 26px; height: 26px; margin: -13px 0 0 -13px;
        border: 3px solid #bdf63b; border-radius: 50%; background: rgba(189, 246, 59, .16);
        box-shadow: 0 0 0 7px rgba(189, 246, 59, .08); pointer-events: none;
        left: 720px; top: 450px; transition: left .65s cubic-bezier(.2,.8,.2,1), top .65s cubic-bezier(.2,.8,.2,1), transform .18s ease;
      }
      #demo-cursor.demo-click { transform: scale(.62); }
    `;
    document.head.appendChild(style);
    const ribbon = document.createElement('div');
    ribbon.id = 'demo-proof-ribbon';
    ribbon.textContent = '界面流程演示，真实结果见公开证据包';
    const chapter = document.createElement('div');
    chapter.id = 'demo-chapter-card';
    chapter.innerHTML = 'Agent 三选一工作台<small>固定 Mock · 不调用 DeepSeek · 不创建真实任务 · 不消耗预算</small>';
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    document.body.append(ribbon, chapter, cursor);
  });
}

async function setChapter(page, title, detail) {
  await page.locator('#demo-chapter-card').evaluate((node, value) => {
    node.style.opacity = '0';
    node.style.transform = 'translateX(-50%) translateY(6px)';
    setTimeout(() => {
      node.innerHTML = `${value.title}<small>${value.detail}</small>`;
      node.style.opacity = '1';
      node.style.transform = 'translateX(-50%) translateY(0)';
    }, 180);
  }, { title, detail });
  await page.waitForTimeout(500);
}

async function pointAt(page, locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error('演示目标当前不可见');
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.locator('#demo-cursor').evaluate((node, point) => {
    node.style.left = `${point.x}px`;
    node.style.top = `${point.y}px`;
  }, { x, y });
  await page.mouse.move(x, y, { steps: 18 });
  await page.waitForTimeout(850);
}

async function clickWithMarker(page, locator) {
  await pointAt(page, locator);
  await page.locator('#demo-cursor').evaluate(node => node.classList.add('demo-click'));
  await page.waitForTimeout(160);
  await locator.click();
  await page.waitForTimeout(220);
  await page.locator('#demo-cursor').evaluate(node => node.classList.remove('demo-click'));
}

function run(command, args, label) {
  const result = spawnSync(command, args, { encoding: 'utf8', windowsHide: true });
  if (result.status !== 0) {
    throw new Error(`${label}失败（退出码 ${result.status}）：\n${result.stderr || result.stdout}`);
  }
  return result.stdout;
}

function resolveExecutable(envName, commandName) {
  const configured = String(process.env[envName] || '').trim();
  if (configured) return path.resolve(configured);
  const result = spawnSync('where.exe', [commandName], { encoding: 'utf8', windowsHide: true });
  const discovered = result.status === 0
    ? String(result.stdout || '').split(/\r?\n/).map(value => value.trim()).find(Boolean)
    : '';
  if (discovered) return discovered;
  throw new Error(`缺少 ${commandName}：请通过 ${envName} 指定现有可执行文件，或将其加入 PATH`);
}

function prepareOutputDirectories() {
  fs.mkdirSync(path.dirname(OUTPUT_VIDEO), { recursive: true });
  fs.mkdirSync(path.dirname(OUTPUT_SCREENSHOT), { recursive: true });
  fs.mkdirSync(QA_DIR, { recursive: true });
  for (const name of fs.readdirSync(QA_DIR)) {
    if (/^(raw-|frame-|contact-sheet|ffprobe|recording-report)/.test(name)) {
      fs.rmSync(path.join(QA_DIR, name), { recursive: true, force: true });
    }
  }
  fs.rmSync(PUBLISH_STAGING_DIR, { recursive: true, force: true });
  fs.mkdirSync(PUBLISH_STAGING_DIR, { recursive: true });
}

async function captureScreenshot(browser, baseUrl, screenshotPath) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
  const page = await context.newPage();
  const fixture = createFixture();
  const errors = [];
  await installFixture(page, fixture, errors);
  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await waitForWorkbench(page);
  await page.locator('#topicCandidates .topic-option').nth(1).click();
  await page.waitForTimeout(500);
  const screening = (await page.locator('#topicScreening').innerText()).trim();
  if (!screening.includes('研究阶段逐条核验') || !screening.includes('1/3') || !screening.includes('固定演示')) {
    throw new Error(`选题后未保留公开依据与预任务预算口径：${screening}`);
  }
  const providerLabel = (await page.locator('#providerBadge').innerText()).trim();
  if (providerLabel !== 'DeepSeek · Key 已就绪') {
    throw new Error(`Provider 状态文案未使用发布口径：${providerLabel}`);
  }
  await page.screenshot({ path: screenshotPath, fullPage: false });
  await context.close();
  if (errors.length) throw new Error(`截图页面出现错误：${errors.join(' | ')}`);
  return { providerLabel, screening, calls: fixture.calls };
}

async function recordBrowserSession(browser, baseUrl) {
  const rawDir = path.join(QA_DIR, 'raw-video');
  fs.mkdirSync(rawDir, { recursive: true });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    recordVideo: { dir: rawDir, size: VIEWPORT },
  });
  const page = await context.newPage();
  const video = page.video();
  const fixture = createFixture();
  const errors = [];
  await installFixture(page, fixture, errors);
  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await waitForWorkbench(page);
  const initialScreening = (await page.locator('#topicScreening').innerText()).trim();
  const providerLabel = (await page.locator('#providerBadge').innerText()).trim();
  if (providerLabel !== 'DeepSeek · Key 已就绪') {
    throw new Error(`Provider 状态文案未使用发布口径：${providerLabel}`);
  }
  await addDemoOverlay(page);
  const proofRibbon = (await page.locator('#demo-proof-ribbon').innerText()).trim();
  const fixedMockNote = (await page.locator('#demo-chapter-card small').innerText()).trim();
  if (proofRibbon !== '界面流程演示，真实结果见公开证据包' || !fixedMockNote.includes('固定 Mock')) {
    throw new Error('首帧真实性标识未完整装载');
  }
  await page.screenshot({ path: STAGED_INTRO_FRAME, fullPage: false });
  const readyAt = Date.now();

  await setChapter(page, '01 · 只说目标，Agent 先给三个角度', '保留选择权，但不把研究、脚本和合规操作全部丢给用户');
  await page.waitForTimeout(4500);

  await setChapter(page, '02 · 从三个可举证角度中选一个', '排除越域、明显夸大和重复选题；公开依据将在研究阶段逐条核验');
  const secondTopic = page.locator('#topicCandidates .topic-option').nth(1);
  await pointAt(page, page.locator('#topicCandidates .topic-option').nth(0));
  await page.waitForTimeout(700);
  await clickWithMarker(page, secondTopic);
  const screeningAfterSelection = (await page.locator('#topicScreening').innerText()).trim();
  if (screeningAfterSelection !== initialScreening || !screeningAfterSelection.includes('1/3')) {
    throw new Error(`选择选题后预任务预算口径发生变化：${screeningAfterSelection}`);
  }
  await page.waitForTimeout(2800);

  await setChapter(page, '03 · 一次确认，同时完成执行授权', '点击“就做这个”后，Agent 自动推进研究阶段');
  await page.waitForTimeout(1800);
  await clickWithMarker(page, page.locator('#startSelectedTopic'));
  await page.getByRole('heading', { name: '研究证据已反向核验' }).waitFor({ timeout: 20000 });

  await setChapter(page, '04 · 第一道人审：研究证据反向核验', '以“所有内容默认虚假”为前提：3 条限定可用，2 条自动否决');
  await page.waitForTimeout(7600);
  await clickWithMarker(page, page.getByRole('button', { name: '继续制作' }));
  await page.getByRole('heading', { name: '最终脚本已通过自动合规检查' }).waitFor({ timeout: 20000 });

  await setChapter(page, '05 · 第二道人审：最终脚本合规放行', '自动检查通过仍不能直接渲染；必须由用户本人确认一次');
  await page.waitForTimeout(7600);
  await clickWithMarker(page, page.getByRole('button', { name: '确认脚本并渲染' }));
  await page.getByRole('heading', { name: '成片已经完成' }).waitFor({ timeout: 25000 });

  await setChapter(page, '06 · 成片、审批和哈希一并交付', '本任务 7/7 次硬预算；失败尝试不会覆盖上一成功运行');
  await page.waitForTimeout(2600);
  await clickWithMarker(page, page.getByRole('button', { name: '播放最新成片' }));
  await page.locator('#latestVideo').evaluate(videoElement => videoElement.play().catch(() => {}));
  await setChapter(page, '真实 52 秒联调成片已回到首页', '画面仅作预览；完整视频、13 项产物和审批哈希均在公开证据包');

  const minReadyDurationMs = 85000;
  const remaining = minReadyDurationMs - (Date.now() - readyAt);
  if (remaining > 0) await page.waitForTimeout(remaining);

  const finalStatus = fixture.job?.status;
  const expectedCalls = [
    'topics', 'create', 'authorize', 'run-1',
    'research-approval', 'run-2', 'compliance-approval', 'run-3',
  ];
  const latestDuration = await page.locator('#latestDuration').innerText();
  await page.close();
  const rawPath = path.join(QA_DIR, 'raw-agent-workbench.webm');
  await video.saveAs(rawPath);
  await context.close();

  if (errors.length) throw new Error(`录制页面出现错误：${errors.join(' | ')}`);
  if (finalStatus !== 'complete') throw new Error(`固定流程未完成：${finalStatus}`);
  if (JSON.stringify(fixture.calls) !== JSON.stringify(expectedCalls)) {
    throw new Error(`固定流程调用顺序异常：${JSON.stringify(fixture.calls)}`);
  }
  if (latestDuration !== '00:52') throw new Error(`真实成片时长未显示为 00:52：${latestDuration}`);
  return {
    rawPath,
    finalStatus,
    calls: fixture.calls,
    latestDuration,
    providerLabel,
    initialScreening,
    screeningAfterSelection,
    proofRibbon,
    fixedMockNote,
    errors,
  };
}

function transcodeAndInspect(rawPath, introFrame, outputPath, ffmpeg, ffprobe) {
  const introSeconds = 2;
  const bodySeconds = TARGET_SECONDS - introSeconds;
  run(ffmpeg, [
    '-y', '-hide_banner', '-loglevel', 'error',
    '-loop', '1', '-framerate', '30', '-i', introFrame,
    '-ss', '5', '-i', rawPath,
    '-filter_complex',
    `[0:v]scale=${VIEWPORT.width}:${VIEWPORT.height}:flags=lanczos,fps=30,format=yuv420p,trim=duration=${introSeconds},setpts=PTS-STARTPTS[intro];`
      + `[1:v]scale=${VIEWPORT.width}:${VIEWPORT.height}:flags=lanczos,fps=30,format=yuv420p,trim=duration=${bodySeconds},setpts=PTS-STARTPTS[body];`
      + '[intro][body]concat=n=2:v=1:a=0,format=yuv420p[outv]',
    '-map', '[outv]',
    '-t', String(TARGET_SECONDS),
    '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
    '-movflags', '+faststart', outputPath,
  ], 'Demo 转码');

  const probeText = run(ffprobe, [
    '-v', 'error',
    '-show_entries', 'format=duration,format_name,size:stream=index,codec_name,codec_type,width,height,r_frame_rate,pix_fmt',
    '-of', 'json', outputPath,
  ], 'Demo ffprobe');
  const probe = JSON.parse(probeText);
  const videoStream = probe.streams.find(stream => stream.codec_type === 'video');
  const audioStreams = probe.streams.filter(stream => stream.codec_type === 'audio');
  const duration = Number(probe.format.duration);
  const valid = videoStream
    && videoStream.codec_name === 'h264'
    && videoStream.width === VIEWPORT.width
    && videoStream.height === VIEWPORT.height
    && videoStream.pix_fmt === 'yuv420p'
    && videoStream.r_frame_rate === '30/1'
    && audioStreams.length === 0
    && duration >= 77.9
    && duration <= 78.1;
  if (!valid) throw new Error(`Demo 媒体规格不合格：${probeText}`);

  fs.writeFileSync(path.join(QA_DIR, 'ffprobe.json'), `${JSON.stringify(probe, null, 2)}\n`, 'utf8');
  return probe;
}

function extractQaFrames(videoPath, ffmpeg) {
  const timestamps = [0, 0.1, 0.5, 5, 14, 24, 34, 45, 55, 66, 75];
  const frames = [];
  for (const second of timestamps) {
    const label = Number.isInteger(second)
      ? String(second).padStart(3, '0')
      : String(second).replace('.', 'p').padStart(4, '0');
    const output = path.join(QA_DIR, `frame-${label}.png`);
    run(ffmpeg, [
      '-y', '-hide_banner', '-loglevel', 'error', '-ss', String(second), '-i', videoPath,
      '-frames:v', '1', '-vf', 'scale=720:450:flags=lanczos', output,
    ], `提取 ${second}s 关键帧`);
    frames.push(output);
  }
  const contactSheet = path.join(QA_DIR, 'contact-sheet.png');
  const layout = frames.map((_, index) => `${(index % 4) * 720}_${Math.floor(index / 4) * 450}`).join('|');
  run(ffmpeg, [
    '-y', '-hide_banner', '-loglevel', 'error',
    ...frames.flatMap(frame => ['-i', frame]),
    '-filter_complex', `xstack=inputs=${frames.length}:layout=${layout}:fill=0x071f1b`,
    '-frames:v', '1', contactSheet,
  ], '生成关键帧接触表');
  return { timestamps, frames, contactSheet };
}

function sha256(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex').toUpperCase();
}

function inspectPng(filePath) {
  const header = fs.readFileSync(filePath).subarray(0, 24);
  const signature = '89504e470d0a1a0a';
  if (header.length < 24 || header.subarray(0, 8).toString('hex') !== signature) {
    throw new Error('截图 staging 不是有效 PNG');
  }
  const width = header.readUInt32BE(16);
  const height = header.readUInt32BE(20);
  if (width !== VIEWPORT.width || height !== VIEWPORT.height) {
    throw new Error(`截图尺寸不合格：${width}x${height}`);
  }
  return { width, height, size: fs.statSync(filePath).size, sha256: sha256(filePath) };
}

function publishPair(entries, { forceFailureAfter = 0 } = {}) {
  const transactionId = `${process.pid}-${Date.now()}`;
  const items = entries.map(entry => ({
    ...entry,
    stagedSha256: sha256(entry.staged),
    originalSha256: fs.existsSync(entry.final) ? sha256(entry.final) : null,
    backup: `${entry.final}.rollback-${transactionId}`,
    originalMoved: false,
    stagedMoved: false,
  }));
  for (const item of items) {
    if (fs.existsSync(item.backup)) throw new Error(`回滚路径已存在：${path.basename(item.backup)}`);
  }

  try {
    for (const item of items) {
      if (fs.existsSync(item.final)) {
        fs.renameSync(item.final, item.backup);
        item.originalMoved = true;
      }
    }
    for (let index = 0; index < items.length; index += 1) {
      const item = items[index];
      fs.renameSync(item.staged, item.final);
      item.stagedMoved = true;
      if (forceFailureAfter === index + 1) throw new Error('发布回滚自检：按计划注入失败');
    }
    for (const item of items) {
      if (!fs.existsSync(item.final) || sha256(item.final) !== item.stagedSha256) {
        throw new Error(`发布后哈希不一致：${path.basename(item.final)}`);
      }
    }
  } catch (error) {
    const rollbackErrors = [];
    for (const item of [...items].reverse()) {
      try {
        if (item.stagedMoved && fs.existsSync(item.final)) {
          fs.renameSync(item.final, `${item.staged}.failed-new`);
        }
        if (item.originalMoved && fs.existsSync(item.backup)) {
          fs.renameSync(item.backup, item.final);
        }
      } catch (rollbackError) {
        rollbackErrors.push(`${path.basename(item.final)}: ${rollbackError.message}`);
      }
    }
    for (const item of items) {
      fs.rmSync(item.staged, { force: true });
      fs.rmSync(`${item.staged}.failed-new`, { force: true });
    }
    if (rollbackErrors.length) {
      throw new Error(`${error.message}；回滚异常：${rollbackErrors.join(' | ')}`);
    }
    throw error;
  }

  const backupCleanupWarnings = [];
  for (const item of items) {
    try { fs.rmSync(item.backup, { force: true }); }
    catch (error) { backupCleanupWarnings.push(`${path.basename(item.backup)}: ${error.message}`); }
  }
  return {
    transactionId,
    pairVerified: true,
    outputs: items.map(item => ({
      path: item.final,
      sha256: item.stagedSha256,
      previous_sha256: item.originalSha256,
    })),
    backup_cleanup_warnings: backupCleanupWarnings,
  };
}

function runPublishRollbackSelfTest() {
  const root = path.join(QA_DIR, 'publish-rollback-self-test');
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });
  const firstFinal = path.join(root, 'first.final');
  const secondFinal = path.join(root, 'second.final');
  const firstStaged = path.join(root, 'first.staged');
  const secondStaged = path.join(root, 'second.staged');
  fs.writeFileSync(firstFinal, 'previous-first', 'utf8');
  fs.writeFileSync(secondFinal, 'previous-second', 'utf8');
  fs.writeFileSync(firstStaged, 'next-first', 'utf8');
  fs.writeFileSync(secondStaged, 'next-second', 'utf8');
  let forcedFailureObserved = false;
  try {
    publishPair([
      { staged: firstStaged, final: firstFinal },
      { staged: secondStaged, final: secondFinal },
    ], { forceFailureAfter: 1 });
  } catch (error) {
    forcedFailureObserved = /按计划注入失败/.test(error.message);
  }
  const previousPairRestored = fs.readFileSync(firstFinal, 'utf8') === 'previous-first'
    && fs.readFileSync(secondFinal, 'utf8') === 'previous-second';
  const leakedRollbackFiles = fs.readdirSync(root).some(name => name.includes('.rollback-') || name.includes('.failed-new'));
  fs.rmSync(root, { recursive: true, force: true });
  if (!forcedFailureObserved || !previousPairRestored || leakedRollbackFiles) {
    throw new Error('截图与 Demo 成对发布的失败回滚自检未通过');
  }
  return { forced_failure_observed: true, previous_pair_restored: true, leaked_rollback_files: false };
}

(async () => {
  prepareOutputDirectories();
  if (!fs.existsSync(EVIDENCE_VIDEO)) throw new Error('缺少已验证的 52 秒公开证据成片');
  const ffmpeg = resolveExecutable('FFMPEG_PATH', 'ffmpeg.exe');
  const ffprobe = resolveExecutable('FFPROBE_PATH', 'ffprobe.exe');
  const browserPath = String(process.env.CODEX_UI_BROWSER || '').trim() || chromium.executablePath();
  for (const required of [ffmpeg, ffprobe]) {
    if (!fs.existsSync(required)) throw new Error(`媒体依赖不存在：${path.basename(required)}`);
  }
  if (!fs.existsSync(browserPath)) {
    throw new Error('缺少可用浏览器：请安装当前 Playwright Chromium，或通过 CODEX_UI_BROWSER 指定现有浏览器');
  }

  const { server, url, port } = await startStaticServer();
  let browser;
  let report;
  try {
    browser = await chromium.launch({
      headless: true,
      executablePath: browserPath,
      args: ['--autoplay-policy=no-user-gesture-required'],
    });
    const screenshot = await captureScreenshot(browser, url, STAGED_SCREENSHOT);
    const recording = await recordBrowserSession(browser, url);
    const probe = transcodeAndInspect(recording.rawPath, STAGED_INTRO_FRAME, STAGED_VIDEO, ffmpeg, ffprobe);
    const qa = extractQaFrames(STAGED_VIDEO, ffmpeg);
    const screenshotInspection = inspectPng(STAGED_SCREENSHOT);
    const rollbackSelfTest = runPublishRollbackSelfTest();
    const publication = publishPair([
      { staged: STAGED_VIDEO, final: OUTPUT_VIDEO },
      { staged: STAGED_SCREENSHOT, final: OUTPUT_SCREENSHOT },
    ]);
    fs.rmSync(PUBLISH_STAGING_DIR, { recursive: true, force: true });
    report = {
      ok: true,
      mode: 'fixed-playwright-fixture',
      preview_service: 'repository-static-read-only',
      preview_port: port,
      preview_service_pid: process.pid,
      external_provider_requests: 0,
      real_jobs_created: 0,
      production_budget_consumed: 0,
      output: OUTPUT_VIDEO,
      screenshot: OUTPUT_SCREENSHOT,
      qa_dir: QA_DIR,
      provider_label: recording.providerLabel,
      latest_duration_label: recording.latestDuration,
      screening_initial: recording.initialScreening,
      screening_after_selection: recording.screeningAfterSelection,
      proof_ribbon: recording.proofRibbon,
      fixed_mock_note: recording.fixedMockNote,
      calls: recording.calls,
      duration_seconds: Number(probe.format.duration),
      screenshot_inspection: screenshotInspection,
      qa_timestamps: qa.timestamps,
      contact_sheet: qa.contactSheet,
      screenshot_calls: screenshot.calls,
      rollback_self_test: rollbackSelfTest,
      publication,
    };
  } finally {
    if (browser) await browser.close().catch(() => {});
    await new Promise(resolve => server.close(resolve));
  }
  report.cleanup = {
    preview_server_listening: server.listening,
    preview_port_released_by_owner: !server.listening,
    browser_closed: true,
  };
  fs.writeFileSync(path.join(QA_DIR, 'recording-report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  process.stdout.write(`${JSON.stringify(report)}\n`);
})().catch(error => {
  fs.rmSync(PUBLISH_STAGING_DIR, { recursive: true, force: true });
  console.error(error.stack || error.message);
  process.exit(1);
});
