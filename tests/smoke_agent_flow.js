const { chromium } = require('playwright');

const AGENT_TEST_POLICY = {
  stage_review_mode: 'agent_test',
  final_human_acceptance_required: true,
};
const AGENT_TEST_REVIEWER = 'Codex 测试代理';
const EMPTY_RESEARCH_REVIEW_NOTE = '本次确认无可采信 finding；后续仅允许使用不含行业事实主张的本地安全模板';
const AGENT_COMPLIANCE_NOTE = 'Codex 测试代理已核对最终脚本、合规结果与审批哈希；仅用于受控测试。';
const AGENT_TEST_IDENTITY = {
  reviewer: AGENT_TEST_REVIEWER,
  actor_type: 'agent',
  review_mode: 'test',
  interaction_mode: 'browser_operated',
  authority: 'test_progress_only',
  human_approval_claimed: false,
  test_only: true,
};

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
  let runInFlight = false;
  let concurrentRunRequests = 0;
  let maxConcurrentRunRequests = 0;
  let pollsDuringLongRun = 0;
  let injectedPollFailures = 0;
  let recoveredPolls = 0;
  let researchArtifactRequests = 0;
  let createRequests = 0;
  const createKeys = [];
  let createProductionMode = null;
  let authorizeRequests = 0;
  let researchApprovalPayload = null;
  let complianceApprovalPayload = null;
  let detailJobReads = 0;
  let detailArtifactRequests = 0;
  let detailRunRequests = 0;
  let detailJob = {
    schema_version: 2,
    id: 'running-detail-job',
    status: 'research_running',
    created_at: '2026-08-01T14:00:00+08:00',
    production_input: { topic: '运行详情轮询测试', audience: '测试用户' },
    approvals: { research: { status: 'pending' }, compliance: { status: 'pending' } },
    budget: { limit: 7, attempted: 1, succeeded: 1, failed: 0 },
    runs: [{ run_id: 'detail-run', stage: 'research', status: 'running' }],
    artifacts: [],
    active_run_id: 'detail-run',
    current_run_id: null,
  };

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
    body: JSON.stringify({ jobs: job ? [job, detailJob] : [detailJob] }),
  }));
  await page.route('**/api/demo-job', async route => {
    createRequests += 1;
    createKeys.push(route.request().headers()['idempotency-key']);
    const requestPayload = await route.request().postDataJSON();
    createProductionMode = requestPayload.production_options?.production_mode || null;
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
        review_policy: { ...AGENT_TEST_POLICY },
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
  await page.route('**/api/jobs/fake-job', route => {
    if (runInFlight) {
      pollsDuringLongRun += 1;
      if (!injectedPollFailures) {
        injectedPollFailures += 1;
        return route.abort('connectionfailed');
      }
      recoveredPolls += 1;
    }
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });
  await page.route('**/api/jobs/running-detail-job', route => {
    detailJobReads += 1;
    if (detailJobReads >= 2) {
      detailJob = { ...detailJob, status: 'awaiting_research_approval', active_run_id: null, runs: [{ ...detailJob.runs[0], status: 'complete' }] };
    }
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(detailJob) });
  });
  await page.route('**/api/jobs/running-detail-job/review-artifacts/research.json', route => {
    detailArtifactRequests += 1;
    if (detailArtifactRequests === 1) {
      return route.fulfill({ status: 404, contentType: 'application/json; charset=utf-8', body: JSON.stringify({ error: { message: '产物尚未发布' } }) });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        status: 'partial',
        summary: '详情研究已发布',
        findings: [{
          finding_id: 'detail-new-finding',
          claim: '新的详情 finding',
          auto_review_status: 'eligible',
          review_summary: '新的详情 finding',
          allowed_use: '仅按证据原意转述',
          prohibited_use: '不得外推',
          source_label: '测试来源',
          source_urls: ['https://example.com/source'],
          evidence: [],
        }],
        sources: [],
      }),
    });
  });
  await page.route('**/api/jobs/running-detail-job/run', route => {
    detailRunRequests += 1;
    return route.fulfill({ status: 500, contentType: 'application/json; charset=utf-8', body: JSON.stringify({ error: { message: '不应该重放 /run' } }) });
  });
  await page.route('**/api/jobs/fake-job/approve', route => {
    authorizeRequests += 1;
    calls.push('authorize-network-uncertain');
    job = { ...job, status: 'authorized' };
    return route.abort('connectionfailed');
  });
  await page.route('**/api/jobs/fake-job/run', async route => {
    runRequests += 1;
    concurrentRunRequests += 1;
    maxConcurrentRunRequests = Math.max(maxConcurrentRunRequests, concurrentRunRequests);
    runKeys.push(route.request().headers()['idempotency-key']);
    try {
      if (runRequests === 1) {
        calls.push('run-long');
        runInFlight = true;
        job = { ...job, status: 'research_running', active_run_id: 'run-long' };
        await new Promise(resolve => setTimeout(resolve, 1400));
        runCount += 1;
        job = { ...job, status: 'awaiting_research_approval', active_run_id: null, budget: { ...job.budget, attempted: 1 } };
        return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
      }
      if (runRequests === 2) {
        calls.push('run-2-failed');
        job = { ...job, status: 'failed', last_error: '模拟的已知服务端失败' };
        return route.fulfill({
          status: 500,
          contentType: 'application/json; charset=utf-8',
          body: JSON.stringify({ error: { code: 'simulated_failure', message: '模拟的已知服务端失败' } }),
        });
      }
      runCount += 1;
      calls.push(`run-${runRequests}`);
      if (runRequests === 4) {
        job = {
          ...job,
          status: 'rendering',
          active_run_id: 'render-run-4',
          runs: [...(job.runs || []), { run_id: 'render-run-4', stage: 'render', status: 'running' }],
        };
        await new Promise(resolve => setTimeout(resolve, 1400));
      }
      const status = runRequests === 3 ? 'awaiting_compliance_approval' : 'complete';
      job = {
        ...job,
        status,
        active_run_id: null,
        last_error: null,
        budget: { ...job.budget, attempted: runRequests },
        ...(status === 'complete' ? {
          current_run_id: 'render-run-4',
          artifacts: ['final.mp4', 'manifest.json'],
          runs: (job.runs || []).map(run => run.run_id === 'render-run-4' ? { ...run, status: 'complete' } : run),
        } : {}),
      };
      return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
    } finally {
      runInFlight = false;
      concurrentRunRequests -= 1;
    }
  });
  await page.route('**/api/jobs/fake-job/review-artifacts/research.json', route => {
    researchArtifactRequests += 1;
    if (researchArtifactRequests === 1) {
      return route.fulfill({ status: 404, contentType: 'application/json; charset=utf-8', body: JSON.stringify({ error: { message: '产物尚未发布' } }) });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        status: 'offline',
        summary: '未配置API Key，跳过联网研究并使用本地范式',
        findings: [],
        sources: [],
      }),
    });
  });
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
    job = {
      ...job,
      status: 'research_approved',
      approvals: {
        ...job.approvals,
        research: { status: 'approved', ...AGENT_TEST_IDENTITY, note: researchApprovalPayload.note },
      },
    };
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });
  await page.route('**/api/jobs/fake-job/approvals/compliance', async route => {
    calls.push('compliance-approval');
    complianceApprovalPayload = await route.request().postDataJSON();
    job = {
      ...job,
      status: 'compliance_approved',
      approvals: {
        ...job.approvals,
        compliance: { status: 'approved', ...AGENT_TEST_IDENTITY, note: complianceApprovalPayload.note },
      },
    };
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(job) });
  });

  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await page.locator('#topicCandidates .topic-option').nth(1).click();
  await page.locator('#startSelectedTopic').click();
  await page.getByRole('heading', { name: '本次没有可采信的外部证据' }).waitFor();
  const researchCallsBeforeDetails = calls.length;
  const researchRunsBeforeDetails = runRequests;
  const researchHomeRequiresDetails = await page.getByRole('button', { name: '进入代理测试审查' }).isVisible()
    && await page.getByRole('button', { name: '确认边界并继续' }).count() === 0;
  await page.getByRole('button', { name: '进入代理测试审查' }).click();
  await page.locator('#view-jobs.active #researchApprovalPanel:not([hidden])').waitFor();
  const researchHomeDidNotApprove = calls.length === researchCallsBeforeDetails
    && runRequests === researchRunsBeforeDetails
    && researchApprovalPayload === null;
  const researchReviewerLocked = await page.locator('#researchReviewer').inputValue() === AGENT_TEST_REVIEWER
    && await page.locator('#researchReviewer').evaluate(node => node.readOnly);
  const researchNotePrefilled = await page.locator('#researchNote').inputValue() === EMPTY_RESEARCH_REVIEW_NOTE;
  await page.locator('#submitResearchBtn').click();
  await page.waitForFunction(() => document.querySelector('#jobDetailBadge')?.textContent === '研究已确认');
  const researchApprovalDidNotAutoRun = runRequests === researchRunsBeforeDetails;
  const researchApprovalIsAgentTest = researchApprovalPayload?.reviewer === AGENT_TEST_REVIEWER
    && researchApprovalPayload?.note === EMPTY_RESEARCH_REVIEW_NOTE
    && job.approvals.research?.review_mode === 'test'
    && job.approvals.research?.human_approval_claimed === false
    && job.approvals.research?.test_only === true;
  await page.locator('#rerunJobBtn').click();
  const listRetryButton = page.locator('#jobList button[data-action="run"][data-job-id="fake-job"]');
  await listRetryButton.waitFor();
  await page.waitForFunction(() => {
    const button = document.querySelector('#jobList button[data-action="run"][data-job-id="fake-job"]');
    return button && !button.disabled;
  });
  const explicitResearchAdvance = runRequests === 2;
  await listRetryButton.evaluate(button => { button.click(); button.click(); });
  await page.waitForFunction(() => [...document.querySelectorAll('#jobList .pill')].some(node => node.textContent.includes('待审查最终脚本')));
  await page.locator('#homeButton').click();
  await page.getByRole('heading', { name: '最终脚本已通过自动合规检查' }).waitFor();
  const complianceCallsBeforeDetails = calls.length;
  const complianceRunsBeforeDetails = runRequests;
  const complianceHomeRequiresDetails = await page.getByRole('button', { name: '进入代理测试审查' }).isVisible()
    && await page.getByRole('button', { name: '确认脚本并渲染' }).count() === 0;
  await page.getByRole('button', { name: '进入代理测试审查' }).click();
  await page.locator('#view-jobs.active #complianceApprovalPanel:not([hidden])').waitFor();
  const complianceHomeDidNotApprove = calls.length === complianceCallsBeforeDetails
    && runRequests === complianceRunsBeforeDetails
    && complianceApprovalPayload === null;
  const complianceReviewerLocked = await page.locator('#complianceReviewer').inputValue() === AGENT_TEST_REVIEWER
    && await page.locator('#complianceReviewer').evaluate(node => node.readOnly);
  const complianceNotePrefilled = await page.locator('#complianceNote').inputValue() === AGENT_COMPLIANCE_NOTE;
  await page.locator('#submitComplianceBtn').click();
  await page.waitForFunction(() => document.querySelector('#jobDetailBadge')?.textContent === '脚本已确认');
  const complianceApprovalDidNotAutoRun = runRequests === complianceRunsBeforeDetails;
  const complianceApprovalIsAgentTest = complianceApprovalPayload?.reviewer === AGENT_TEST_REVIEWER
    && complianceApprovalPayload?.note === AGENT_COMPLIANCE_NOTE
    && job.approvals.compliance?.review_mode === 'test'
    && job.approvals.compliance?.human_approval_claimed === false
    && job.approvals.compliance?.test_only === true;
  await page.locator('#rerunJobBtn').click();
  await page.waitForFunction(() => document.querySelector('#jobDetailBadge')?.textContent === '成片装配中');
  await page.waitForFunction(() => document.querySelector('#jobDetailBadge')?.textContent === '已完成'
    && !document.querySelector('#artifactVideo')?.hidden
    && document.querySelector('#artifactVideo')?.getAttribute('src')?.endsWith('/final.mp4'));
  const completedDetailUpdated = await page.locator('#artifactVideo:not([hidden])[src$="/final.mp4"]').count() === 1
    && await page.locator('#artifactLinks a[download][href$="/final.mp4"]').count() === 1;
  await page.locator('#homeButton').click();
  await page.getByRole('heading', { name: '测试成片已经完成，等待用户最终验收' }).waitFor();
  const explicitComplianceAdvance = runRequests === 4;
  const latestDownloadAvailable = await page.locator('#latestArtifact a[download][href$="/final.mp4"]').count() === 1;
  const completedOpenEnabled = await page.locator('#jobList button[data-action="open"][data-job-id="fake-job"]').isEnabled();

  await page.locator('#appMenuButton').click();
  await page.locator('#appMenu [data-view="jobs"]').click();
  await page.locator('#researchFindings').evaluate(node => {
    node.innerHTML = '<div class="finding" data-finding-id="stale">旧任务 finding 不应保留</div>';
  });
  await page.locator('#jobList button[data-action="open"][data-job-id="running-detail-job"]').click();
  await page.waitForFunction(() => document.querySelector('#researchFindings')?.textContent.includes('产物发布中'));
  const staleFindingClearedDuringPending = !(await page.locator('#researchFindings').innerText()).includes('旧任务 finding 不应保留');
  await page.waitForFunction(() => document.querySelector('#jobDetailBadge')?.textContent === '待审查研究证据'
    && document.querySelector('#researchFindings')?.textContent.includes('新的详情 finding'));
  const staleFindingAbsentAfterRecovery = !(await page.locator('#researchFindings').innerText()).includes('旧任务 finding 不应保留');

  const expectedCalls = ['create-network-uncertain', 'create-replay', 'authorize-network-uncertain', 'run-long', 'research-approval', 'run-2-failed', 'run-3', 'compliance-approval', 'run-4'];
  const createReplayReusedKey = createKeys[0] && createKeys[0] === createKeys[1];
  const noAutomaticRunReplay = runRequests === 4;
  const logicalAttemptKeysAreUnique = runKeys.length === 4 && new Set(runKeys).size === 4;
  const emptyResearchBoundaryConfirmed = researchApprovalPayload
    && Array.isArray(researchApprovalPayload.findings)
    && researchApprovalPayload.findings.length === 0
    && researchApprovalPayload.note === EMPTY_RESEARCH_REVIEW_NOTE;
  const humanImpersonationAbsent = ![researchApprovalPayload, complianceApprovalPayload]
    .some(payload => payload?.reviewer === '本机会话用户')
    && job.approvals.research?.human_approval_claimed === false
    && job.approvals.compliance?.human_approval_claimed === false;
  const unexpectedErrors = errors.filter(message => !/net::ERR_CONNECTION_FAILED|status of 404 \(Not Found\)|status of 500 \(Internal Server Error\)/.test(message));
  const result = { status: job.status, calls, expectedCalls, createKeys, createReplayReusedKey, createProductionMode, latestDownloadAvailable, authorizeRequests, runKeys, noAutomaticRunReplay, logicalAttemptKeysAreUnique, maxConcurrentRunRequests, pollsDuringLongRun, injectedPollFailures, recoveredPolls, researchArtifactRequests, detailJobReads, detailArtifactRequests, detailRunRequests, staleFindingClearedDuringPending, staleFindingAbsentAfterRecovery, completedOpenEnabled, completedDetailUpdated, emptyResearchBoundaryConfirmed, researchHomeRequiresDetails, researchHomeDidNotApprove, researchReviewerLocked, researchNotePrefilled, researchApprovalDidNotAutoRun, researchApprovalIsAgentTest, explicitResearchAdvance, complianceHomeRequiresDetails, complianceHomeDidNotApprove, complianceReviewerLocked, complianceNotePrefilled, complianceApprovalDidNotAutoRun, complianceApprovalIsAgentTest, explicitComplianceAdvance, humanImpersonationAbsent, expectedFailureSignals: errors, unexpectedErrors };
  process.stdout.write(JSON.stringify(result));
  await browser.close();
  if (unexpectedErrors.length || job.status !== 'complete' || JSON.stringify(calls) !== JSON.stringify(expectedCalls) || createRequests !== 2 || !createReplayReusedKey || createProductionMode !== 'motion' || !latestDownloadAvailable || authorizeRequests !== 1 || !noAutomaticRunReplay || !logicalAttemptKeysAreUnique || maxConcurrentRunRequests !== 1 || pollsDuringLongRun < 2 || injectedPollFailures !== 1 || recoveredPolls < 1 || researchArtifactRequests < 2 || detailJobReads < 3 || detailArtifactRequests < 2 || detailRunRequests !== 0 || !staleFindingClearedDuringPending || !staleFindingAbsentAfterRecovery || !completedOpenEnabled || !completedDetailUpdated || !emptyResearchBoundaryConfirmed || !researchHomeRequiresDetails || !researchHomeDidNotApprove || !researchReviewerLocked || !researchNotePrefilled || !researchApprovalDidNotAutoRun || !researchApprovalIsAgentTest || !explicitResearchAdvance || !complianceHomeRequiresDetails || !complianceHomeDidNotApprove || !complianceReviewerLocked || !complianceNotePrefilled || !complianceApprovalDidNotAutoRun || !complianceApprovalIsAgentTest || !explicitComplianceAdvance || !humanImpersonationAbsent) process.exit(1);
})();
