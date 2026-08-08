const { chromium } = require('playwright');

(async () => {
  const executablePath = process.env.CODEX_UI_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  const browser = await chromium.launch({ headless: true, executablePath });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 }, deviceScaleFactor: 1 });
  const baseUrl = process.env.CONTENT_FACTORY_URL || 'http://127.0.0.1:8765';
  const errors = [];
  const calls = [];
  let job = null;
  let runCount = 0;
  let runRequests = 0;
  const runKeys = [];
  let createRequests = 0;
  const createKeys = [];
  let authorizeRequests = 0;
  let researchApprovalPayload = null;

  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  page.on('pageerror', err => errors.push(err.message));

  await page.route('**/api/agent/topics', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({
      source: 'deepseek',
      selection_bundle_id: 'selection-smoke-agent-flow-000000000001',
      screening: '已排除越域、夸大承诺和重复选题；公开依据将在研究阶段逐条核验。',
      candidates: [
        { id: 'topic-1', title: '通风后没有气味，甲醛就安全了吗？', reason: '误区切入。', audience: '新房家庭' },
        { id: 'topic-2', title: '除醛率为什么要看检测条件？', reason: '证据切入。', audience: '新房家庭' },
        { id: 'topic-3', title: '检测报告应该先看哪些信息？', reason: '报告切入。', audience: '新房家庭' },
      ],
    }),
  }));
  await page.route('**/api/jobs', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({ jobs: job ? [job] : [] }),
  }));
  await page.route('**/api/demo-job', async route => {
    createRequests += 1;
    createKeys.push(route.request().headers()['idempotency-key']);
    const requestPayload = await route.request().postDataJSON();
    const selected = {
      'topic-1': { title: '通风后没有气味，甲醛就安全了吗？', audience: '新房家庭' },
      'topic-2': { title: '除醛率为什么要看检测条件？', audience: '新房家庭' },
      'topic-3': { title: '检测报告应该先看哪些信息？', audience: '新房家庭' },
    }[requestPayload.candidate_id];
    const input = {
      topic: selected.title,
      audience: selected.audience,
      ...requestPayload.production_options,
      selection_bundle_id: requestPayload.selection_bundle_id,
      candidate_id: requestPayload.candidate_id,
    };
    if (!job) {
      job = {
        schema_version: 2,
        id: 'fake-job',
        status: 'planned',
        created_at: '2026-08-01T15:00:00+08:00',
        production_input: input,
        approvals: { research: { status: 'pending' }, compliance: { status: 'pending' } },
        budget: { limit: 7, attempted: 0, succeeded: 0, failed: 0 },
        runs: [],
        artifacts: [],
        current_run_id: null,
      };
    }
    if (createRequests === 1) {
      calls.push('create-network-uncertain');
      return route.abort('connectionfailed');
    }
    calls.push('create-replay');
    await route.fulfill({ status: 201, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });
  await page.route('**/api/jobs/fake-job', route => route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) }));
  await page.route('**/api/jobs/fake-job/approve', route => {
    authorizeRequests += 1;
    calls.push('authorize-network-uncertain');
    job = { ...job, status: 'authorized' };
    return route.abort('connectionfailed');
  });
  await page.route('**/api/jobs/fake-job/run', route => {
    runRequests += 1;
    runKeys.push(route.request().headers()['idempotency-key']);
    if (runRequests === 1) {
      calls.push('run-network-uncertain');
      return route.abort('connectionfailed');
    }
    if (runRequests === 3) {
      calls.push('run-2-failed');
      job = { ...job, status: 'failed', last_error: '模拟的已知服务端失败' };
      return route.fulfill({
        status: 500,
        contentType: 'application/json; charset=utf-8',
        body: JSON.stringify({ error: { code: 'simulated_failure', message: '模拟的已知服务端失败' } }),
      });
    }
    runCount += 1;
    calls.push(`run-${runCount}`);
    const statuses = ['awaiting_research_approval', 'awaiting_compliance_approval', 'complete'];
    job = { ...job, status: statuses[runCount - 1], budget: { ...job.budget, attempted: runCount + 1 } };
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });
  await page.route('**/api/jobs/fake-job/review-artifacts/research.json', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({
      status: 'offline',
      summary: '未配置API Key，跳过联网研究并使用本地范式',
      findings: [],
      sources: [],
    }),
  }));
  await page.route('**/api/jobs/fake-job/review-artifacts/approved_script.json', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({ script: '这是经过证据约束的安全测试脚本。' }),
  }));
  await page.route('**/api/jobs/fake-job/review-artifacts/review.json', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({ status: 'passed', blocked: false, warnings: [] }),
  }));
  await page.route('**/api/jobs/fake-job/approvals/research', async route => {
    calls.push('research-approval');
    researchApprovalPayload = await route.request().postDataJSON();
    job = { ...job, status: 'research_approved', approvals: { ...job.approvals, research: { status: 'approved' } } };
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });
  await page.route('**/api/jobs/fake-job/approvals/compliance', route => {
    calls.push('compliance-approval');
    job = { ...job, status: 'compliance_approved', approvals: { ...job.approvals, compliance: { status: 'approved' } } };
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });

  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await page.locator('#topicCandidates .topic-option').nth(1).click();
  await page.locator('#startSelectedTopic').click();
  await page.getByRole('heading', { name: '本次没有可采信的外部证据' }).waitFor();
  await page.getByRole('button', { name: '确认边界并继续' }).click();
  const retryButton = page.getByRole('button', { name: '重试当前步骤' });
  await retryButton.waitFor();
  await retryButton.evaluate(button => { button.click(); button.click(); });
  await page.getByRole('heading', { name: '最终脚本已通过自动合规检查' }).waitFor();
  await page.getByRole('button', { name: '确认脚本并渲染' }).click();
  await page.getByRole('heading', { name: '成片已经完成' }).waitFor();

  const expectedCalls = ['create-network-uncertain', 'create-replay', 'authorize-network-uncertain', 'run-network-uncertain', 'run-1', 'research-approval', 'run-2-failed', 'run-2', 'compliance-approval', 'run-3'];
  const createReplayReusedKey = createKeys[0] && createKeys[0] === createKeys[1];
  const automaticReplayReusedKey = runKeys[0] && runKeys[0] === runKeys[1];
  const explicitRetryUsedNewKey = runKeys[2] && runKeys[3] && runKeys[2] !== runKeys[3];
  const logicalAttemptKeysAreUnique = new Set([runKeys[0], runKeys[2], runKeys[3], runKeys[4]]).size === 4;
  const emptyResearchBoundaryConfirmed = researchApprovalPayload
    && Array.isArray(researchApprovalPayload.findings)
    && researchApprovalPayload.findings.length === 0
    && researchApprovalPayload.note === '本人确认本次无可采信 finding；后续仅允许使用不含行业事实主张的本地安全模板';
  const unexpectedErrors = errors.filter(message => !/net::ERR_CONNECTION_FAILED|status of 500 \(Internal Server Error\)/.test(message));
  const result = { status: job.status, calls, expectedCalls, createKeys, createReplayReusedKey, authorizeRequests, runKeys, automaticReplayReusedKey, explicitRetryUsedNewKey, logicalAttemptKeysAreUnique, emptyResearchBoundaryConfirmed, expectedFailureSignals: errors, unexpectedErrors };
  process.stdout.write(JSON.stringify(result));
  await browser.close();
  if (unexpectedErrors.length || job.status !== 'complete' || JSON.stringify(calls) !== JSON.stringify(expectedCalls) || createRequests !== 2 || !createReplayReusedKey || authorizeRequests !== 1 || runRequests !== 5 || !automaticReplayReusedKey || !explicitRetryUsedNewKey || !logicalAttemptKeysAreUnique || !emptyResearchBoundaryConfirmed) process.exit(1);
})();
