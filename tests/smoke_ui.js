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
  let providerConfigured = true;
  let providerVerified = false;
  let mptReady = false;
  let clearKeyPayload = null;
  let configSnapshot = null;
  let auxiliaryToolsSettled = false;
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
        provider_ready: providerConfigured,
        provider_configured: providerConfigured,
        provider_connection_verified: providerConfigured && providerVerified,
        provider_connection_verified_at: providerVerified ? '2026-08-01T12:00:00+08:00' : null,
        provider_state: !providerConfigured ? 'unconfigured' : providerVerified ? 'verified' : 'configured',
        version: '0.3.0',
        production_engines: {
          default_mode: 'motion',
          motion: { name: 'HyperFrames', version: '0.7.86', enabled: true, health: 'ready', role: 'primary', selectable: true },
          footage: { name: 'MoneyPrinterTurbo', version: '1.3.3', enabled: mptReady, health: mptReady ? 'ready' : 'disabled', role: 'secondary', selectable: mptReady },
        },
      }),
    });
  });
  await page.route('**/api/config', async route => {
    const method = route.request().method();
    if (method === 'GET') {
      if (!configSnapshot) {
        const response = await route.fetch();
        configSnapshot = await response.json();
      }
      return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(configSnapshot) });
    }
    const payload = await route.request().postDataJSON();
    if (payload?.provider?.clear_api_key === true) {
      clearKeyPayload = payload;
      providerConfigured = false;
      providerVerified = false;
      configSnapshot = {
        ...configSnapshot,
        provider: { ...configSnapshot.provider, has_api_key: false, persisted_api_key: false },
      };
      return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(configSnapshot) });
    }
    return route.fulfill({ status: 422, contentType: 'application/json; charset=utf-8', body: JSON.stringify({ error: { message: 'unexpected config mutation' } }) });
  });
  await page.route('**/api/provider/test', async route => {
    providerVerified = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ ok: true, models: ['deepseek-v4-flash'], configured_model_available: true, connection_verified: true }),
    });
  });
  await page.route('**/api/tools', async route => {
    const response = await route.fetch();
    await new Promise(resolve => setTimeout(resolve, 2500));
    auxiliaryToolsSettled = true;
    await route.fulfill({ response });
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
        source: topicCalls === 1 ? 'local_safe_agent' : 'deepseek',
        notice: `预任务 Provider 请求 ${topicCalls}/3，剩余 ${3 - topicCalls}；来源：DeepSeek。`,
        screening: topicCalls === 1
          ? '项目启动结构含未知字段；反证审核未执行；已切换到本地安全候选；预任务 Provider 请求 1/3，剩余 2。'
          : `项目启动结构校验通过；已排除越域、夸大承诺和重复选题；公开依据将在研究阶段逐条核验；预任务 Provider 请求 ${topicCalls}/3，剩余 ${3 - topicCalls}；来源：DeepSeek。`,
        bootstrap_failure_kind: topicCalls === 1 ? 'invalid_capability_pack_schema' : 'passed',
        bootstrap_schema_diagnostic: topicCalls === 1 ? {
          missing_fields: [], unknown_fields: ['<redacted-unknown-field>'], field_types: {}, list_element_types: {},
        } : null,
        capability_review_failure_kind: topicCalls === 1 ? 'not_run' : 'passed',
        capability_review: topicCalls === 1 ? null : {
          status: topicCalls === 1 ? 'needs_revision' : 'passed',
          issues: ['门店资料仍待核验'],
          safe_scope: ['仅可作为研究问题'],
          candidate_verdicts: batch.map(([title], index) => ({
            candidate_id: `topic-${index + 1}`,
            candidate_title: topicCalls === 1 ? `ORIGINAL REVIEWED TITLE ${index + 1}` : title,
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

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.locator('#topicCandidates .topic-option').first().waitFor();
  const criticalRenderedBeforeAuxiliary = !auxiliaryToolsSettled;
  const providerBadge = await page.locator('#providerBadge').innerText();
  const providerConfiguredClass = await page.locator('#providerQuickButton').evaluate(node => node.classList.contains('configured'));
  const initialScreening = await page.locator('#topicScreening').innerText();
  const initialTopics = await page.locator('#topicCandidates .topic-option').count();
  const initialSelected = await page.locator('#topicCandidates .topic-option.selected').count();
  const defaultProductionMode = await page.locator('input[name="productionMode"]:checked').inputValue();
  const footageModeDisabled = await page.locator('#productionModeFootage').isDisabled();
  const motionEngineText = await page.locator('#motionEngineStatus').innerText();
  const footageEngineText = await page.locator('#footageEngineStatus').innerText();
  const visibleVersion = await page.locator('#editionBadge').innerText();
  mptReady = true;
  await page.evaluate(() => refresh({ syncHomeView: false }));
  await page.waitForFunction(() => !document.querySelector('#productionModeFootage')?.disabled);
  await page.locator('#productionModeFootage').check();
  const footageSelectableWhenReady = await page.locator('input[name="productionMode"]:checked').inputValue() === 'footage';
  mptReady = false;
  await page.evaluate(() => refresh({ syncHomeView: false }));
  await page.waitForFunction(() => document.querySelector('#productionModeFootage')?.disabled);
  const modeFallsBackToMotion = await page.locator('input[name="productionMode"]:checked').inputValue() === 'motion';
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
  await page.locator('#topicCandidates strong').getByText('测醛前为什么要先确认封闭时间？', { exact: true }).waitFor();
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
  await page.locator('#clearApiKey').click();
  await page.waitForFunction(() => document.querySelector('#providerBadge')?.textContent === 'DeepSeek · 未配置');
  const providerClearedBadge = await page.locator('#providerBadge').innerText();
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
    providerClearedBadge,
    clearKeyPayload,
    initialScreening,
    screeningAfterSelection,
    refreshedScreening,
    initialTopics,
    initialSelected,
    defaultProductionMode,
    footageModeDisabled,
    motionEngineText,
    footageEngineText,
    visibleVersion,
    footageSelectableWhenReady,
    modeFallsBackToMotion,
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
    criticalRenderedBeforeAuxiliary,
    errors,
  };
  process.stdout.write(JSON.stringify(result));
  await browser.close();
  if (errors.length || !criticalRenderedBeforeAuxiliary || providerBadge !== 'DeepSeek · Key 已就绪' || !providerConfiguredClass || providerVerifiedBadge !== 'DeepSeek · 本次连接已验证' || !providerVerifiedClass || providerClearedBadge !== 'DeepSeek · 未配置' || clearKeyPayload?.provider?.clear_api_key !== true || defaultProductionMode !== 'motion' || !footageModeDisabled || !footageSelectableWhenReady || !modeFallsBackToMotion || !motionEngineText.includes('HyperFrames 0.7.86') || !footageEngineText.includes('MoneyPrinterTurbo 1.3.3') || visibleVersion !== 'v0.3.0' || !initialScreening.includes('1/3') || screeningAfterSelection !== initialScreening || !initialScreening.includes('项目启动结构含未知字段') || !initialScreening.includes('反证审核未执行') || initialScreening.includes('needs_evidence') || initialScreening.includes('候选1') || initialScreening.includes('ORIGINAL REVIEWED TITLE') || initialScreening.includes('<redacted-unknown-field>') || !refreshedScreening.includes('2/3') || !refreshedScreening.includes('项目启动结构校验通过') || !refreshedScreening.includes('被审核候选“测醛前为什么要先确认封闭时间？”') || refreshedScreening.includes('候选1') || initialTopics !== 3 || initialSelected !== 1 || homeDecisionSelects !== 0 || homeHasApproveRejectPair || detailedRejectOptions < 2 || result.stageButtons !== 0 || result.persistentSideRails !== 0 || !composerFocused || !mobileNoOverflow) process.exit(1);
})();
