const { chromium } = require('playwright');

const batches = [
  [
    ['通风后没有气味，甲醛就安全了吗？', '从常见误区切入，适合新房家庭。'],
    ['99%除醛率，到底应该看哪些检测条件？', '数字有冲突感，也能自然引出证据核验。'],
    ['入住前看检测报告，最容易漏掉哪三项？', '实用清单型，便于收藏和转发。'],
  ],
  [
    ['测醛前为什么要先确认封闭时间？', '从检测流程入手，避免只看一个数字。'],
    ['新房通风多久才够，为什么不能只凭气味？', '把高频疑问拆成可核验的问题。'],
    ['室内空气检测报告应该先看什么？', '用报告阅读框架代替简单结论。'],
  ],
];

(async () => {
  const executablePath = process.env.CODEX_UI_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  const browser = await chromium.launch({ headless: true, executablePath });
  const baseUrl = process.env.CONTENT_FACTORY_URL || 'http://127.0.0.1:8765';
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 }, deviceScaleFactor: 1 });
  const errors = [];
  let topicCalls = 0;
  let providerVerified = false;
  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  page.on('pageerror', err => errors.push(err.message));
  await page.route('**/api/status', async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({
      response,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        ...data,
        provider_ready: true,
        provider_configured: true,
        provider_connection_verified: providerVerified,
        provider_connection_verified_at: providerVerified ? '2026-08-01T12:00:00+08:00' : null,
        provider_state: providerVerified ? 'verified' : 'configured',
      }),
    });
  });
  await page.route('**/api/provider/test', async route => {
    providerVerified = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ ok: true, models: ['deepseek-v4-flash'], configured_model_available: true, connection_verified: true }),
    });
  });
  await page.route('**/api/jobs', async route => {
    const response = await route.fetch();
    const data = await response.json();
    const jobs = (data.jobs || []).filter(job => ['complete', 'legacy_read_only'].includes(job.status));
    await route.fulfill({ response, contentType: 'application/json; charset=utf-8', body: JSON.stringify({ jobs }) });
  });
  await page.route('**/api/agent/topics', async route => {
    const batch = batches[Math.min(topicCalls, batches.length - 1)];
    topicCalls += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        selection_bundle_id: `selection-smoke-ui-${topicCalls}`,
        source: 'deepseek',
        notice: `预任务 Provider 请求 ${topicCalls}/3，剩余 ${3 - topicCalls}；来源：DeepSeek。`,
        screening: `已排除越域、夸大承诺和重复选题；公开依据将在研究阶段逐条核验；预任务 Provider 请求 ${topicCalls}/3，剩余 ${3 - topicCalls}；来源：DeepSeek。`,
        capability_review: {
          status: 'passed',
          issues: ['门店资料仍待核验'],
          safe_scope: ['仅可作为研究问题'],
          candidate_verdicts: batch.map((_, index) => ({
            candidate_id: `topic-${index + 1}`,
            verdict: index === 0 ? 'needs_evidence' : 'usable_limited',
            reasons: ['不得提前断言门店事实'],
            safe_scope: '进入研究后逐条取证',
          })),
        },
        topic_provider_budget: { limit: 3, attempted: topicCalls, succeeded: topicCalls, failed: 0, remaining: 3 - topicCalls, events: [] },
        pretask_provider_budget: { limit: 3, attempted: topicCalls, succeeded: topicCalls, failed: 0, remaining: 3 - topicCalls, events: [] },
        candidates: batch.map(([title, reason], index) => ({ id: `topic-${index + 1}`, title, reason, audience: '新房家庭' })),
      }),
    });
  });

  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await page.locator('#topicCandidates .topic-option').first().waitFor();
  const providerBadge = await page.locator('#providerBadge').innerText();
  const providerConfiguredClass = await page.locator('#providerQuickButton').evaluate(node => node.classList.contains('configured'));
  const initialScreening = await page.locator('#topicScreening').innerText();
  const initialTopics = await page.locator('#topicCandidates .topic-option').count();
  const initialSelected = await page.locator('#topicCandidates .topic-option.selected').count();
  const homeDecisionSelects = await page.locator('#view-workbench select').count();
  const homeButtonLabels = await page.locator('#view-workbench button').allInnerTexts();
  const homeHasApproveRejectPair = homeButtonLabels.some(label => /批准|拒绝|退回/.test(label));
  const detailedRejectOptions = await page.locator('#view-jobs option[value="rejected"]').count();
  await page.locator('#topicCandidates .topic-option').nth(1).click();
  const screeningAfterSelection = await page.locator('#topicScreening').innerText();
  await page.locator('#latestVideo').waitFor({ state: 'visible' }).catch(() => {});
  await page.waitForTimeout(500);
  await page.screenshot({ path: process.env.UI_SMOKE_OUTPUT || 'runtime/ui-smoke.png', fullPage: false });

  await page.locator('#topicCandidates .topic-option').nth(2).click();
  const selectedTitle = await page.locator('#topicCandidates .topic-option.selected strong').innerText();
  await page.locator('#refreshTopics').click();
  await page.getByText('测醛前为什么要先确认封闭时间？').waitFor();
  const refreshedTitle = await page.locator('#topicCandidates .topic-option').first().innerText();
  const refreshedScreening = await page.locator('#topicScreening').innerText();
  await page.locator('#writeOwnTopic').click();
  const composerFocused = await page.locator('#goalInput').evaluate(node => document.activeElement === node);

  await page.locator('#appMenuButton').click();
  await page.locator('#appMenu:not([hidden])').waitFor();
  await page.locator('#appMenu [data-view="catalog"]').click();
  await page.locator('#view-catalog.active').waitFor();
  await page.screenshot({ path: 'runtime/catalog-smoke.png', fullPage: true });
  const installRoot = await page.locator('#hardwareFacts .wide strong').innerText();

  await page.locator('#appMenuButton').click();
  await page.locator('#appMenu [data-view="settings"]').click();
  await page.locator('#view-settings.active').waitFor();
  const storageRoot = await page.locator('#storageRoot').inputValue();
  const storageDirectories = await page.locator('#storageDirectories > div').count();
  await page.locator('#testProvider').click();
  await page.waitForFunction(() => document.querySelector('#providerBadge')?.textContent === 'DeepSeek · 本次连接已验证');
  const providerVerifiedBadge = await page.locator('#providerBadge').innerText();
  const providerVerifiedClass = await page.locator('#providerQuickButton').evaluate(node => node.classList.contains('verified'));
  await page.screenshot({ path: 'runtime/storage-settings-smoke.png', fullPage: true });

  await page.setViewportSize({ width: 720, height: 900 });
  await page.locator('#view-settings.active .back-home').click();
  await page.locator('#view-workbench.active').waitFor();
  await page.waitForTimeout(250);
  const mobileNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  await page.screenshot({ path: 'runtime/ui-narrow-smoke.png', fullPage: false });

  const result = {
    title: await page.title(),
    heading: await page.locator('h1').innerText(),
    providerBadge,
    providerConfiguredClass,
    providerVerifiedBadge,
    providerVerifiedClass,
    initialScreening,
    screeningAfterSelection,
    refreshedScreening,
    initialTopics,
    initialSelected,
    homeDecisionSelects,
    homeHasApproveRejectPair,
    detailedRejectOptions,
    selectedTitle,
    refreshedTitle,
    composerFocused,
    stageButtons: await page.locator('.stage-tab').count(),
    persistentSideRails: await page.locator('.context-rail, .utility-nav').count(),
    packageCards: await page.locator('#packageGrid .package-card').count(),
    hardwareProfile: await page.locator('#hardwareProfile').innerText(),
    installPolicy: await page.locator('#installPolicyBadge').innerText(),
    installRoot,
    storageRoot,
    storageDirectories,
    mobileNoOverflow,
    topicCalls,
    errors,
  };
  process.stdout.write(JSON.stringify(result));
  await browser.close();
  if (errors.length || providerBadge !== 'DeepSeek · Key 已就绪' || !providerConfiguredClass || providerVerifiedBadge !== 'DeepSeek · 本次连接已验证' || !providerVerifiedClass || !initialScreening.includes('1/3') || screeningAfterSelection !== initialScreening || !initialScreening.includes('研究阶段逐条核验') || !initialScreening.includes('反证审核通过') || !initialScreening.includes('原因：门店资料仍待核验') || !initialScreening.includes('允许范围：仅可作为研究问题') || initialScreening.includes('needs_evidence') || !refreshedScreening.includes('2/3') || initialTopics !== 3 || initialSelected !== 1 || homeDecisionSelects !== 0 || homeHasApproveRejectPair || detailedRejectOptions < 2 || result.stageButtons !== 0 || result.persistentSideRails !== 0 || !composerFocused || !mobileNoOverflow) process.exit(1);
})();
