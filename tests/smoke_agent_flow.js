const { chromium } = require('playwright');
const { createHash } = require('node:crypto');

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
  const context = await browser.newContext({ viewport: { width: 1440, height: 1024 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
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
  let mechanicalRevisionJobReads = 0;
  let mechanicalRevisionArtifactRequests = 0;
  let retryRequests = 0;
  let retryRunRequests = 0;
  const retryKeys = [];
  let retriedJob = null;
  let legacyCorrectionRequests = 0;
  let revisionPatchRequests = 0;
  const revisionPatchPayloads = [];
  const revisionPatchKeys = [];
  const baseApprovedScript = '这是经过证据约束的安全测试脚本。';
  const revisedApprovedScript = '这是用户直接修改后的完整安全测试脚本，明确说明检测条件、适用边界和行动建议。';
  const baseApprovedArtifactBody = JSON.stringify({ script: baseApprovedScript });
  const revisedApprovedArtifactBody = JSON.stringify({
    script: revisedApprovedScript,
    selected_by: 'browser_editor',
  });
  const baseApprovedArtifactSha256 = createHash('sha256').update(baseApprovedArtifactBody).digest('hex');
  let evidenceReportRequests = 0;
  let publicEvidenceRequests = 0;
  const expectedZipFilename = 'shiyi-public-evidence-fake-job-render-run-5-0123456789ab.zip';
  let detailJob = {
    schema_version: 2,
    id: 'running-detail-job',
    status: 'research_running',
    created_at: '2026-08-01T14:00:00+08:00',
    production_input: { topic: '运行详情轮询测试', audience: '测试用户' },
    review_policy: { ...AGENT_TEST_POLICY },
    approvals: { research: { status: 'pending' }, compliance: { status: 'pending' } },
    budget: { limit: 7, attempted: 1, succeeded: 1, failed: 0 },
    runs: [{ run_id: 'detail-run', stage: 'research', status: 'running' }],
    artifacts: [],
    active_run_id: 'detail-run',
    current_run_id: null,
  };
  const mechanicalRevisionJob = {
    schema_version: 2,
    id: 'mechanical-revision-job',
    status: 'failed',
    created_at: '2026-08-01T16:00:00+08:00',
    updated_at: '2026-08-01T16:01:00+08:00',
    production_input: { topic: '默认目标的脚本自动生成失败回归', audience: '潜在客户' },
    review_policy: {
      stage_review_mode: 'mechanical',
      final_human_acceptance_required: true,
    },
    provider_provenance: {
      created_at: '2026-08-01T16:00:00+08:00',
      provider_state: 'unconfigured',
      provider_name: 'DeepSeek',
      model: 'deepseek-v4-pro',
      connection_verified_at: null,
      topic_source: 'local_safe_agent',
      selection_bundle_id: 'selection-mechanical-fixture',
      pretask_budget: { limit: 3, attempted: 0, succeeded: 0, failed: 0, remaining: 3, events: [] },
    },
    approvals: {
      research: {
        status: 'approved',
        reviewer: '反向机械审核器',
        actor_type: 'mechanical_reviewer',
        review_mode: 'mechanical',
        human_approval_claimed: false,
      },
      compliance: { status: 'pending' },
    },
    budget: { limit: 7, attempted: 0, succeeded: 0, failed: 0 },
    runs: [
      { run_id: 'mechanical-research-ok', stage: 'research', status: 'complete' },
      { run_id: 'mechanical-content-failed-1', stage: 'content', status: 'failed', error: '脚本长度未通过自动门禁' },
      { run_id: 'mechanical-content-failed-2', stage: 'content', status: 'failed', error: '脚本长度未通过自动门禁' },
      { run_id: 'mechanical-content-failed-3', stage: 'content', status: 'failed', error: '脚本长度未通过自动门禁' },
    ],
    artifacts: [],
    active_run_id: null,
    current_run_id: null,
    last_error: '脚本 Agent 及本地安全兜底均未生成符合自然节奏门禁的脚本',
    automatic_controller: {
      mode: 'mechanical',
      status: 'retry_limit_reached',
      stage_attempts: 4,
      maximum_stage_attempts: 4,
      human_intervention_required_during_generation: false,
    },
    automatic_controller_failure: {
      code: 'automatic_stage_attempts_exhausted',
      reason: '脚本自动生成已达到重试上限，本次未发布不合格脚本。',
      source_error: '场景3与上一幕重复同一信息结构',
      stage: 'content',
      stage_attempts: 4,
      maximum_stage_attempts: 4,
    },
  };

  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  page.on('pageerror', err => errors.push(err.message));

  await page.context().route('**/api/jobs/fake-job/evidence-report.html', route => {
    evidenceReportRequests += 1;
    return route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      headers: { 'Content-Disposition': 'inline; filename="evidence-report.html"' },
      body: '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>成片验收报告</title></head><body><main><h1>给运营与负责人看的成片说明 · 不是动画代码</h1><p>请先播放成片，再核对文案、声音、字幕和证据边界。</p><p>这份报告使用普通人能看懂的说明；技术记录另行折叠保存。</p></main></body></html>',
    });
  });
  await page.context().route('**/api/jobs/fake-job/public-evidence.zip', route => {
    publicEvidenceRequests += 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/zip',
      headers: { 'Content-Disposition': `attachment; filename="${expectedZipFilename}"` },
      body: 'PK\u0005\u0006\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000\u0000',
    });
  });

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
    body: JSON.stringify({ jobs: [job, retriedJob, detailJob, mechanicalRevisionJob].filter(Boolean) }),
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
  await page.route('**/api/jobs/mechanical-revision-job', route => {
    mechanicalRevisionJobReads += 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify(mechanicalRevisionJob),
    });
  });
  await page.route('**/api/jobs/mechanical-revision-job/retry', async route => {
    retryRequests += 1;
    retryKeys.push(route.request().headers()['idempotency-key']);
    if (!retriedJob) {
      retriedJob = {
        ...mechanicalRevisionJob,
        id: 'mechanical-retry-job',
        status: 'authorized',
        retry_of_job_id: mechanicalRevisionJob.id,
        budget: { limit: 7, attempted: 0, succeeded: 0, failed: 0, events: [] },
        runs: [],
        last_error: null,
        automatic_controller: undefined,
        automatic_controller_failure: undefined,
      };
    }
    if (retryRequests === 1) return route.abort('connectionfailed');
    return route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify(retriedJob),
    });
  });
  await page.route('**/api/jobs/mechanical-retry-job', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(retriedJob),
  }));
  await page.route('**/api/jobs/mechanical-retry-job/run', route => {
    retryRunRequests += 1;
    retriedJob = {
      ...retriedJob,
      status: 'complete',
      current_run_id: 'mechanical-retry-render',
      artifacts: ['final.mp4'],
      runs: [{ run_id: 'mechanical-retry-render', stage: 'render', status: 'complete' }],
      automatic_controller: {
        mode: 'mechanical',
        status: 'complete',
        human_intervention_required_during_generation: false,
      },
    };
    return route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify(retriedJob),
    });
  });
  await page.route('**/api/jobs/mechanical-revision-job/review-artifacts/approved_script.json', route => {
    mechanicalRevisionArtifactRequests += 1;
    return route.fulfill({
      status: 404,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ error: { message: '内容阶段失败，没有可发布脚本' } }),
    });
  });
  await page.route('**/api/jobs/mechanical-revision-job/review-artifacts/review.json', route => {
    mechanicalRevisionArtifactRequests += 1;
    return route.fulfill({
      status: 404,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ error: { message: '内容阶段失败，没有可发布审核结果' } }),
    });
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
      const completedRunId = runRequests >= 5 ? 'render-run-5' : 'render-run-4';
      job = {
        ...job,
        status,
        ...(status === 'complete' ? {
          // The shipped workbench uses mechanical review.  The earlier
          // agent_test stages above exercise the explicit approval UI; switch
          // this completed fixture to the shipped policy before exercising
          // the customer-facing exact full-text revision flow.
          review_policy: {
            stage_review_mode: 'mechanical',
            final_human_acceptance_required: true,
          },
        } : {}),
        active_run_id: null,
        last_error: null,
        budget: { ...job.budget, attempted: runRequests },
        ...(status === 'complete' ? {
          current_run_id: completedRunId,
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
    body: baseApprovedArtifactBody,
  }));
  await page.route('**/api/jobs/fake-job/artifacts/approved_script.json', route => route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: job?.current_run_id === 'render-run-5'
      ? revisedApprovedArtifactBody
      : baseApprovedArtifactBody,
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
  await page.route('**/api/jobs/fake-job/script', async route => {
    revisionPatchRequests += 1;
    const payload = await route.request().postDataJSON();
    const requestKey = route.request().headers()['idempotency-key'];
    revisionPatchPayloads.push(payload);
    revisionPatchKeys.push(requestKey);
    job = {
      ...job,
      status: 'compliance_approved',
      last_error: null,
      approvals: {
        ...job.approvals,
        compliance: {
          status: 'approved',
          reviewer: '反向机械审核器',
          actor_type: 'mechanical_reviewer',
          review_mode: 'mechanical',
          human_approval_claimed: false,
        },
      },
      script_revision: {
        status: 'accepted_pending_render',
        editor: '本地浏览器用户',
        base_run_id: 'render-run-4',
        previous_success_run_preserved: 'render-run-4',
      },
    };
    if (revisionPatchRequests === 1) return route.abort('connectionfailed');
    return route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ ...job, replayed: true }),
    });
  });
  await page.route('**/api/agent/corrections', route => {
    legacyCorrectionRequests += 1;
    return route.fulfill({
      status: 500,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ error: { message: '完整文案改稿不应再调用学习纠错接口' } }),
    });
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
  const technicalArtifacts = page.locator('#artifactLinks details.technical-artifacts');
  const technicalArtifactsCollapsed = await technicalArtifacts.count() === 1
    && !(await technicalArtifacts.evaluate(node => node.open));
  const rawManifestOnlyInTechnicalAttachments = await page.locator('#artifactLinks').evaluate(node => {
    const manifestLinks = [...node.querySelectorAll('a')].filter(link => link.getAttribute('href')?.endsWith('/manifest.json'));
    return manifestLinks.length === 1
      && Boolean(manifestLinks[0].closest('details.technical-artifacts'))
      && !manifestLinks[0].closest('details.technical-artifacts').open;
  });
  const rawManifestHiddenByDefault = await technicalArtifacts.locator('a[href$="/manifest.json"]').count() === 1
    && !await technicalArtifacts.locator('a[href$="/manifest.json"]').isVisible();
  await page.locator('#homeButton').click();
  await page.getByRole('heading', { name: '自动成片已经完成' }).waitFor();
  const explicitComplianceAdvance = runRequests === 4;
  const latestDownloadAvailable = await page.locator('#latestArtifact a[download][href$="/final.mp4"]').count() === 1;
  const completedHumanActionsVisible = await page.locator('#latestArtifact #latestEditButton', { hasText: '修改文案并重新生成' }).isVisible()
    && await page.locator('#latestArtifact a[href$="/evidence-report.html"]', { hasText: '查看验收报告' }).isVisible()
    && await page.locator('#latestArtifact a[href$="/public-evidence.zip"]', { hasText: '下载交付材料（ZIP）' }).isVisible();
  const deliveryHint = await page.locator('#latestArtifact .delivery-hint').innerText();
  const deliveryHintIsOperatorFriendly = deliveryHint.includes('给运营和审核人员使用')
    && deliveryHint.includes('00-验收报告.html');

  await page.locator('#latestEditButton').click();
  await page.locator('#revisionCurrentScript').waitFor();
  const revisionCurrentScriptVisible = await page.locator('#revisionCurrentScript').isVisible()
    && await page.locator('#revisionCurrentScript').inputValue() === baseApprovedScript
    && !await page.locator('#revisionCurrentScript').evaluate(node => node.readOnly);
  const revisionExactEditGuidanceVisible = (await page.locator('#activeJobPanel').innerText()).includes('当前成片实际使用的完整文案')
    && (await page.locator('#activeJobPanel').innerText()).includes('直接改正文')
    && (await page.locator('#activeJobPanel').innerText()).includes('上一版成片');
  await page.locator('#revisionCurrentScript').fill(revisedApprovedScript);
  const runRequestsBeforeRevision = runRequests;
  await page.locator('button[data-home-action="submit-script-revision"]').click();
  await page.getByRole('heading', { name: '自动成片已经完成' }).waitFor();
  const revisionRunRequests = runRequests - runRequestsBeforeRevision;
  const revisionExactContract = revisionPatchRequests === 2
    && legacyCorrectionRequests === 0
    && revisionPatchKeys[0]
    && revisionPatchKeys[0] === revisionPatchKeys[1]
    && JSON.stringify(revisionPatchPayloads[0]) === JSON.stringify(revisionPatchPayloads[1])
    && revisionPatchPayloads[0]?.script === revisedApprovedScript
    && revisionPatchPayloads[0]?.base_run_id === 'render-run-4'
    && revisionPatchPayloads[0]?.base_approved_script_sha256 === baseApprovedArtifactSha256;
  const revisionTriggeredExactlyOneRun = revisionRunRequests === 1;

  const reportLink = page.locator('#latestArtifact a[href$="/evidence-report.html"]');
  const reportHref = await reportLink.getAttribute('href');
  const reportPage = await page.context().newPage();
  const reportResponse = await reportPage.goto(new URL(reportHref, baseUrl).href, { waitUntil: 'domcontentloaded' });
  const reportContentType = reportResponse?.headers()['content-type'] || '';
  const reportBodyText = await reportPage.locator('body').innerText();
  const evidenceReportOpenedAsHumanHtml = evidenceReportRequests === 1
    && reportContentType.toLowerCase().startsWith('text/html')
    && reportBodyText.includes('给运营与负责人看的成片说明')
    && reportBodyText.includes('不是动画代码')
    && reportBodyText.includes('普通人能看懂')
    && !reportBodyText.trim().startsWith('{');
  await reportPage.close();

  const publicEvidenceLink = page.locator('#latestArtifact a[href$="/public-evidence.zip"]');
  const [publicEvidenceDownload] = await Promise.all([
    page.waitForEvent('download'),
    publicEvidenceLink.click(),
  ]);
  const zipDownloadNamingCorrect = publicEvidenceRequests === 1
    && publicEvidenceDownload.suggestedFilename() === expectedZipFilename;
  await publicEvidenceDownload.cancel().catch(() => {});
  const completedOpenEnabled = await page.locator('#jobList button[data-action="open"][data-job-id="fake-job"]').isEnabled();

  await page.locator('#appMenuButton').click();
  await page.locator('#appMenu [data-view="jobs"]').click();
  job = { ...job, review_policy: { ...AGENT_TEST_POLICY } };
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: '测试成片已经完成，等待用户最终验收' }).waitFor();
  const nonMechanicalCompletionHidesExactRevision = await page.locator('#activeJobPanel [data-home-action="edit-script"]').count() === 0
    && await page.locator('#latestArtifact #latestEditButton').count() === 0;
  job = {
    ...job,
    review_policy: {
      stage_review_mode: 'mechanical',
      final_human_acceptance_required: true,
    },
  };
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

  // Reproduce the real packaged-product path: a fully mechanical task exhausts
  // bounded content retries before either approved_script.json or review.json
  // exists, and the user opens its details.  This state is terminal until the
  // Agent starts a new automatic attempt; a 404 here is not "still publishing".
  const listRetryVisible = await page.locator('#jobList button[data-action="retry"][data-job-id="mechanical-revision-job"]').isVisible();
  await page.locator('#jobList button[data-action="open"][data-job-id="mechanical-revision-job"]').click();
  await page.waitForFunction(() => document.querySelector('#jobDetailBadge')?.textContent === '本次生成未完成');
  const detailRetryButtonVisible = await page.locator('#retryFailedJobBtn').evaluate(node => node.checkVisibility());
  const detailRetryButtonText = await page.locator('#retryFailedJobBtn').innerText();
  const detailRetryButtonState = await page.locator('#retryFailedJobBtn').evaluate(node => ({
    hidden: node.hidden,
    disabled: node.disabled,
    display: getComputedStyle(node).display,
    width: node.getBoundingClientRect().width,
    height: node.getBoundingClientRect().height,
    visibility: getComputedStyle(node).visibility,
    opacity: getComputedStyle(node).opacity,
    checkVisibility: node.checkVisibility(),
    parentDisplay: getComputedStyle(node.parentElement).display,
    parentVisibility: getComputedStyle(node.parentElement).visibility,
    parentWidth: node.parentElement.getBoundingClientRect().width,
    parentHeight: node.parentElement.getBoundingClientRect().height,
    detailHidden: document.querySelector('#jobDetailPanel')?.hidden,
    viewActive: document.querySelector('#view-jobs')?.classList.contains('active'),
  }));
  const detailJobSummaryText = await page.locator('#jobSummary').innerText();
  const detailRetryVisible = detailRetryButtonVisible
    && detailRetryButtonText.trim() === '重新生成这个选题'
    && detailJobSummaryText.includes('选题没有被否决')
    && detailJobSummaryText.includes('分镜阶段生成了相邻重复的画面结构');
  await page.screenshot({ path: process.env.FLOW_SMOKE_OUTPUT || 'runtime/agent-flow-smoke.png', fullPage: false });
  const mechanicalTaskHasNoManualRun = await page.locator('#jobList button[data-action="run"][data-job-id="mechanical-revision-job"]').count() === 0;
  const mechanicalReviewLabel = await page.locator('#jobSummary').innerText();
  const mechanicalReviewIsAutomatic = mechanicalReviewLabel.includes('反向机械审核（全自动）')
    && !mechanicalReviewLabel.includes('用户本人审查');
  const mechanicalProviderSourceIsExplicit = mechanicalReviewLabel.includes('本地安全 Agent（创建时未配置 DeepSeek）');
  const mechanicalHumanControlsHidden = !await page.locator('#researchApprovalPanel').isVisible()
    && !await page.locator('#complianceApprovalPanel').isVisible()
    && !await page.locator('#approvedScriptInput').isVisible()
    && !await page.locator('#saveScriptBtn').isVisible()
    && !await page.locator('#submitResearchBtn').isVisible()
    && !await page.locator('#submitComplianceBtn').isVisible();
  const diagnosticHistory = page.locator('#runHistory details.diagnostic-history');
  const diagnosticHistoryPresent = await diagnosticHistory.count() === 1;
  const diagnosticHistoryCollapsed = diagnosticHistoryPresent
    && !(await diagnosticHistory.evaluate(node => node.open));
  const diagnosticHistorySummary = diagnosticHistoryPresent
    ? await diagnosticHistory.locator('summary').innerText()
    : '';
  const diagnosticHistoryExplainsFailures = diagnosticHistorySummary.includes('内部诊断记录')
    && diagnosticHistorySummary.includes('3')
    && /失败|未通过/.test(diagnosticHistorySummary);
  const failedRunDetailsHidden = await page.locator('#runHistory .run-row').count() === 3
    && await page.locator('#runHistory .run-row:visible').count() === 0;

  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await page.waitForFunction(() => window.scrollY > 0);
  const mechanicalScrollBefore = await page.evaluate(() => window.scrollY);
  const mechanicalReadsBeforeWait = mechanicalRevisionJobReads;
  const mechanicalArtifactsBeforeWait = mechanicalRevisionArtifactRequests;
  await page.waitForTimeout(2500);
  const mechanicalScrollAfter = await page.evaluate(() => window.scrollY);
  const mechanicalScrollStable = Math.abs(mechanicalScrollAfter - mechanicalScrollBefore) <= 1;
  const mechanicalTerminalPollingStopped = mechanicalRevisionJobReads === mechanicalReadsBeforeWait
    && mechanicalRevisionArtifactRequests === mechanicalArtifactsBeforeWait;

  await page.evaluate(() => sessionStorage.setItem('shiyi_home_job_id', 'mechanical-revision-job'));
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: '选题没有被否决，这次生成没成功' }).waitFor();
  const retryFailureReasonVisible = (await page.locator('#activeJobPanel').innerText())
    .includes('分镜阶段生成了相邻重复的画面结构');
  const retryButton = page.locator('#activeJobPanel [data-home-action="retry-failed"]');
  const sameTopicRetryVisible = await retryButton.isVisible()
    && (await retryButton.innerText()) === '重新生成这个选题';
  await retryButton.click();
  await page.getByRole('heading', { name: '自动成片已经完成' }).waitFor();
  const retryReplayReusedKey = retryKeys.length === 2 && retryKeys[0] === retryKeys[1];
  const retryCreatedFreshJob = retriedJob?.id === 'mechanical-retry-job'
    && retriedJob?.retry_of_job_id === mechanicalRevisionJob.id
    && retriedJob?.production_input?.topic === mechanicalRevisionJob.production_input.topic
    && retriedJob?.budget?.attempted === 0;
  const retryTriggeredExactlyOneRun = retryRunRequests === 1;

  const expectedCalls = ['create-network-uncertain', 'create-replay', 'authorize-network-uncertain', 'run-long', 'research-approval', 'run-2-failed', 'run-3', 'compliance-approval', 'run-4', 'run-5'];
  const createReplayReusedKey = createKeys[0] && createKeys[0] === createKeys[1];
  const noAutomaticRunReplay = runRequestsBeforeRevision === 4;
  const logicalAttemptKeysAreUnique = runKeys.length === 5 && new Set(runKeys).size === 5;
  const emptyResearchBoundaryConfirmed = researchApprovalPayload
    && Array.isArray(researchApprovalPayload.findings)
    && researchApprovalPayload.findings.length === 0
    && researchApprovalPayload.note === EMPTY_RESEARCH_REVIEW_NOTE;
  const humanImpersonationAbsent = ![researchApprovalPayload, complianceApprovalPayload]
    .some(payload => payload?.reviewer === '本机会话用户')
    && job.approvals.research?.human_approval_claimed === false
    && job.approvals.compliance?.human_approval_claimed === false;
  const unexpectedErrors = errors.filter(message => !/net::ERR_CONNECTION_FAILED|status of 404 \(Not Found\)|status of 500 \(Internal Server Error\)/.test(message));
  const result = { status: job.status, calls, expectedCalls, createKeys, createReplayReusedKey, createProductionMode, latestDownloadAvailable, completedHumanActionsVisible, deliveryHintIsOperatorFriendly, technicalArtifactsCollapsed, rawManifestOnlyInTechnicalAttachments, rawManifestHiddenByDefault, revisionCurrentScriptVisible, revisionExactEditGuidanceVisible, revisionExactContract, revisionTriggeredExactlyOneRun, revisionPatchRequests, legacyCorrectionRequests, revisionRunRequests, nonMechanicalCompletionHidesExactRevision, evidenceReportRequests, evidenceReportOpenedAsHumanHtml, reportContentType, publicEvidenceRequests, zipDownloadNamingCorrect, expectedZipFilename, authorizeRequests, runKeys, noAutomaticRunReplay, logicalAttemptKeysAreUnique, maxConcurrentRunRequests, pollsDuringLongRun, injectedPollFailures, recoveredPolls, researchArtifactRequests, detailJobReads, detailArtifactRequests, detailRunRequests, staleFindingClearedDuringPending, staleFindingAbsentAfterRecovery, mechanicalRevisionJobReads, mechanicalRevisionArtifactRequests, mechanicalTaskHasNoManualRun, mechanicalReviewIsAutomatic, mechanicalProviderSourceIsExplicit, mechanicalHumanControlsHidden, listRetryVisible, detailRetryButtonVisible, detailRetryButtonText, detailRetryButtonState, detailJobSummaryText, detailRetryVisible, diagnosticHistoryPresent, diagnosticHistoryCollapsed, diagnosticHistorySummary, diagnosticHistoryExplainsFailures, failedRunDetailsHidden, mechanicalScrollBefore, mechanicalScrollAfter, mechanicalScrollStable, mechanicalTerminalPollingStopped, retryRequests, retryReplayReusedKey, retryFailureReasonVisible, sameTopicRetryVisible, retryCreatedFreshJob, retryTriggeredExactlyOneRun, completedOpenEnabled, completedDetailUpdated, emptyResearchBoundaryConfirmed, researchHomeRequiresDetails, researchHomeDidNotApprove, researchReviewerLocked, researchNotePrefilled, researchApprovalDidNotAutoRun, researchApprovalIsAgentTest, explicitResearchAdvance, complianceHomeRequiresDetails, complianceHomeDidNotApprove, complianceReviewerLocked, complianceNotePrefilled, complianceApprovalDidNotAutoRun, complianceApprovalIsAgentTest, explicitComplianceAdvance, humanImpersonationAbsent, expectedFailureSignals: errors, unexpectedErrors };
  process.stdout.write(JSON.stringify(result));
  await context.close();
  await browser.close();
  if (unexpectedErrors.length || job.status !== 'complete' || JSON.stringify(calls) !== JSON.stringify(expectedCalls) || createRequests !== 2 || !createReplayReusedKey || createProductionMode !== 'motion' || !latestDownloadAvailable || !completedHumanActionsVisible || !deliveryHintIsOperatorFriendly || !technicalArtifactsCollapsed || !rawManifestOnlyInTechnicalAttachments || !rawManifestHiddenByDefault || !revisionCurrentScriptVisible || !revisionExactEditGuidanceVisible || !revisionExactContract || !revisionTriggeredExactlyOneRun || !nonMechanicalCompletionHidesExactRevision || !evidenceReportOpenedAsHumanHtml || !zipDownloadNamingCorrect || authorizeRequests !== 1 || !noAutomaticRunReplay || !logicalAttemptKeysAreUnique || maxConcurrentRunRequests !== 1 || pollsDuringLongRun < 2 || injectedPollFailures !== 1 || recoveredPolls < 1 || researchArtifactRequests < 2 || detailJobReads < 3 || detailArtifactRequests < 2 || detailRunRequests !== 0 || !staleFindingClearedDuringPending || !staleFindingAbsentAfterRecovery || !mechanicalTaskHasNoManualRun || !mechanicalReviewIsAutomatic || !mechanicalProviderSourceIsExplicit || !mechanicalHumanControlsHidden || !listRetryVisible || !detailRetryVisible || !diagnosticHistoryPresent || !diagnosticHistoryCollapsed || !diagnosticHistoryExplainsFailures || !failedRunDetailsHidden || !mechanicalScrollStable || !mechanicalTerminalPollingStopped || retryRequests !== 2 || !retryReplayReusedKey || !retryFailureReasonVisible || !sameTopicRetryVisible || !retryCreatedFreshJob || !retryTriggeredExactlyOneRun || !completedOpenEnabled || !completedDetailUpdated || !emptyResearchBoundaryConfirmed || !researchHomeRequiresDetails || !researchHomeDidNotApprove || !researchReviewerLocked || !researchNotePrefilled || !researchApprovalDidNotAutoRun || !researchApprovalIsAgentTest || !explicitResearchAdvance || !complianceHomeRequiresDetails || !complianceHomeDidNotApprove || !complianceReviewerLocked || !complianceNotePrefilled || !complianceApprovalDidNotAutoRun || !complianceApprovalIsAgentTest || !explicitComplianceAdvance || !humanImpersonationAbsent) process.exit(1);
})();
