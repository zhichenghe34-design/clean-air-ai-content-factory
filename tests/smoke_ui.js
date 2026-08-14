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
  let settingsSavePayload = null;
  let configSnapshot = null;
  const internalApiRequests = [];
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
          footage: {
            name: 'MoneyPrinterTurbo', version: '1.3.3',
            enabled: mptReady, health: mptReady ? 'ready' : 'disabled', role: 'secondary',
            operator_ready: false, selectable: false,
            disabled_reason: '当前版本尚未开放实拍素材；本版本仅支持纯动画',
          },
        },
      }),
    });
  });
  await page.route('**/api/config', async route => {
    const method = route.request().method();
    if (method === 'GET') {
      if (!configSnapshot) {
        const response = await route.fetch();
        const fetched = await response.json();
        // Customer settings must display and preserve a previously selected
        // custom model when the operator saves only a new API Key.
        configSnapshot = {
          ...fetched,
          provider: { ...fetched.provider, model: 'customer-reviewed-model' },
        };
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
    settingsSavePayload = payload;
    providerConfigured = Boolean(payload?.provider?.api_key) || providerConfigured;
    configSnapshot = {
      ...configSnapshot,
      provider: {
        ...configSnapshot.provider,
        model: payload?.provider?.model || configSnapshot.provider.model,
        has_api_key: providerConfigured,
        persisted_api_key: Boolean(payload?.provider?.api_key && payload?.provider?.persist_api_key) || configSnapshot.provider.persisted_api_key,
      },
    };
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(configSnapshot) });
  });
  await page.route('**/api/provider/test', async route => {
    providerVerified = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ ok: true, models: ['deepseek-v4-pro'], configured_model_available: true, connection_verified: true }),
    });
  });
  await page.route(/\/api\/(?:tools|catalog|hardware)(?:\?|$)/, async route => {
    internalApiRequests.push(route.request().url());
    await route.fulfill({ status: 500, contentType: 'application/json; charset=utf-8', body: JSON.stringify({ error: { message: 'ordinary UI must not request internal endpoints' } }) });
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
  const ordinaryInterfaceLoadedWithoutInternalApi = internalApiRequests.length === 0;
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
  await page.waitForFunction(() => document.querySelector('#footageEngineStatus')?.textContent.includes('尚未开放') || document.querySelector('#footageEngineStatus')?.textContent.includes('仅支持纯动画'));
  const footageRemainsDisabledWhenReady = await page.locator('#productionModeFootage').isDisabled();
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
  const composerSemantics = {
    label: await page.locator('label[for="goalInput"]').innerText(),
    help: await page.locator('#goalInputHelp').innerText(),
    send: await page.locator('#sendGoalBtn').innerText(),
    paperclips: await page.locator('#agentComposer img[src$="paperclip.svg"]').count(),
  };
  const composerIme = await page.locator('#goalInput').evaluate(node => {
    const form = document.getElementById('agentComposer');
    let submits = 0;
    const capture = event => {
      submits += 1;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    form.addEventListener('submit', capture, true);
    node.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }));
    const composingSubmits = submits;
    node.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    const normalSubmits = submits;
    form.removeEventListener('submit', capture, true);
    return { composingSubmits, normalSubmits };
  });
  const originalGoal = await page.locator('#goalInput').inputValue();
  const composerDefault = await page.locator('#goalInput').evaluate(node => ({
    offsetHeight: node.offsetHeight,
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }));
  await page.locator('#goalInput').fill('简短要求');
  const composerShortHeight = await page.locator('#goalInput').evaluate(node => node.clientHeight);
  await page.locator('#goalInput').fill('第一行要求\n第二行要求\n第三行要求\n第四行要求\n第五行要求\n第六行要求\n第七行要求');
  const composerMulti = await page.locator('#goalInput').evaluate(node => ({ clientHeight: node.clientHeight, scrollHeight: node.scrollHeight }));
  await page.locator('#goalInput').fill(Array.from({ length: 20 }, (_, index) => `第${index + 1}行`).join('\n'));
  const composerCapped = await page.locator('#goalInput').evaluate(node => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }));

  await page.locator('#appMenuButton').click();
  await page.locator('#appMenu:not([hidden])').waitFor();
  const menuLabels = await page.locator('#appMenu [role="menuitem"]').allInnerTexts();
  const internalMenuEntries = await page.locator('#appMenu [data-view="discovery"], #appMenu [data-view="catalog"]').count();
  const internalPages = await page.locator('#view-discovery, #view-catalog').count();
  await page.locator('#appMenu [data-view="settings"]').click();
  await page.locator('#view-settings.active').waitFor();
  const modelDisplay = await page.locator('#modelName').innerText();
  const modelDisplayTag = await page.locator('#modelName').evaluate(node => node.tagName);
  const developerFieldCount = await page.locator('#providerName, #baseUrl, #researchEnabled, #mediaParserRoot, #rootsInput, #storageRoot').count();
  const runtimeCopy = await page.locator('.runtime-settings').innerText();
  const dataLocationCopy = await page.locator('.data-location-note').innerText();
  await page.locator('#apiKey').fill('smoke-pro-key');
  await page.locator('#persistKey').check();
  await page.locator('#saveSettings').click();
  await page.waitForFunction(() => document.querySelector('#apiKey')?.value === '');
  const settingsSavePreservesHidden = !Object.hasOwn(settingsSavePayload.provider, 'model')
    && configSnapshot.provider.model === 'customer-reviewed-model'
    && !Object.hasOwn(settingsSavePayload.provider, 'base_url')
    && !Object.hasOwn(settingsSavePayload, 'research')
    && !Object.hasOwn(settingsSavePayload, 'discovery')
    && !Object.hasOwn(settingsSavePayload, 'storage');
  await page.locator('#testProvider').click();
  await page.waitForFunction(() => document.querySelector('#providerBadge')?.textContent === 'DeepSeek · 当前连接已验证');
  const providerVerifiedBadge = await page.locator('#providerBadge').innerText();
  const providerVerifiedClass = await page.locator('#providerQuickButton').evaluate(node => node.classList.contains('verified'));
  await page.locator('#clearApiKey').click();
  await page.waitForFunction(() => document.querySelector('#providerBadge')?.textContent === 'DeepSeek · 未配置');
  const providerClearedBadge = await page.locator('#providerBadge').innerText();
  await page.screenshot({ path: 'runtime/storage-settings-smoke.png', fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#view-settings.active .back-home').click();
  await page.locator('#view-workbench.active').waitFor();
  await page.waitForTimeout(250);
  const mobileNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  const mobileRestoredComposer = await page.locator('#goalInput').evaluate(node => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }));
  await page.locator('#goalInput').fill('第一行\n第二行\n第三行\n第四行\n第五行\n第六行\n第七行');
  const mobileComposer = await page.evaluate(() => {
    const ids = ['agentComposer', 'goalInput', 'sendGoalBtn'];
    const boxes = Object.fromEntries(ids.map(id => {
      const rect = document.getElementById(id).getBoundingClientRect();
      return [id, { left: rect.left, right: rect.right, width: rect.width, height: rect.height }];
    }));
    return {
      boxes,
      viewportWidth: window.innerWidth,
      textareaScrollHeight: document.getElementById('goalInput').scrollHeight,
      textareaClientHeight: document.getElementById('goalInput').clientHeight,
      textareaOffsetHeight: document.getElementById('goalInput').offsetHeight,
    };
  });
  await page.locator('#goalInput').fill(originalGoal);
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
    footageRemainsDisabledWhenReady,
    modeFallsBackToMotion,
    homeDecisionSelects,
    homeHasApproveRejectPair,
    detailedRejectOptions,
    selectedTitle,
    refreshedTitle,
    composerFocused,
    composerSemantics,
    composerIme,
    composerDefault,
    composerShortHeight,
    composerMulti,
    composerCapped,
    mobileRestoredComposer,
    mobileComposer,
    stageButtons: await page.locator('.stage-tab').count(),
    persistentSideRails: await page.locator('.context-rail, .utility-nav').count(),
    menuLabels,
    internalMenuEntries,
    internalPages,
    internalApiRequests,
    modelDisplay,
    modelDisplayTag,
    developerFieldCount,
    runtimeCopy,
    dataLocationCopy,
    settingsSavePreservesHidden,
    mobileNoOverflow,
    topicCalls,
    ordinaryInterfaceLoadedWithoutInternalApi,
    errors,
  };
  process.stdout.write(JSON.stringify(result));
  await browser.close();
  const mobileBoxesFit = Object.values(mobileComposer.boxes).every(box => box.left >= 0 && box.right <= mobileComposer.viewportWidth + 0.5 && box.width > 0);
  const composerContractOk = composerSemantics.label === '想换选题方向？（可选）'
    && composerSemantics.help.includes('直接点“就做这个”')
    && composerSemantics.help.includes('不用再发送')
    && composerSemantics.send === '按新要求换选题'
    && composerSemantics.paperclips === 0
    && composerIme.composingSubmits === 0
    && composerIme.normalSubmits === 1
    && composerDefault.offsetHeight >= 116
    && composerDefault.scrollHeight <= composerDefault.clientHeight + 2
    && composerMulti.clientHeight > composerShortHeight
    && composerMulti.scrollHeight <= composerMulti.clientHeight + 2
    && composerCapped.clientHeight <= 241
    && composerCapped.scrollHeight > composerCapped.clientHeight
    && composerCapped.overflowY === 'auto'
    && (mobileRestoredComposer.scrollHeight <= mobileRestoredComposer.clientHeight + 2 || mobileRestoredComposer.overflowY === 'auto')
    && mobileComposer.textareaOffsetHeight >= 132
    && mobileComposer.textareaScrollHeight <= mobileComposer.textareaClientHeight + 2
    && mobileComposer.boxes.sendGoalBtn.height >= 44
    && mobileBoxesFit;
  const externalSurfaceOk = ordinaryInterfaceLoadedWithoutInternalApi
    && internalApiRequests.length === 0
    && internalMenuEntries === 0
    && internalPages === 0
    && menuLabels.length === 2
    && modelDisplay === '沿用已有自定义模型：customer-reviewed-model'
    && modelDisplayTag === 'OUTPUT'
    && developerFieldCount === 0
    && runtimeCopy.includes('无需另外安装 Python、Node 或 FFmpeg')
    && runtimeCopy.includes('Windows 10/11 64 位')
    && runtimeCopy.includes('Microsoft Edge 151')
    && runtimeCopy.includes('自己的 DeepSeek API Key')
    && runtimeCopy.includes('实拍素材功能尚未开放')
    && dataLocationCopy.includes('安装入口默认在 D 盘创建软件文件夹')
    && dataLocationCopy.includes('任务、成片和加密 Key 保存在当前 Windows 使用者的本机数据目录')
    && dataLocationCopy.includes('两者分开')
    && settingsSavePreservesHidden;
  if (errors.length || !externalSurfaceOk || providerBadge !== 'DeepSeek · Key 已就绪' || !providerConfiguredClass || providerVerifiedBadge !== 'DeepSeek · 当前连接已验证' || !providerVerifiedClass || providerClearedBadge !== 'DeepSeek · 未配置' || clearKeyPayload?.provider?.clear_api_key !== true || defaultProductionMode !== 'motion' || !footageModeDisabled || !footageRemainsDisabledWhenReady || !modeFallsBackToMotion || !motionEngineText.includes('视频生成组件已就绪') || !footageEngineText.includes('尚未开放') && !footageEngineText.includes('仅支持纯动画') || visibleVersion !== 'v0.3.0' || !initialScreening.includes('1/3') || screeningAfterSelection !== initialScreening || !initialScreening.includes('项目启动结构含未知字段') || !initialScreening.includes('反证审核未执行') || initialScreening.includes('needs_evidence') || initialScreening.includes('候选1') || initialScreening.includes('ORIGINAL REVIEWED TITLE') || initialScreening.includes('<redacted-unknown-field>') || !refreshedScreening.includes('2/3') || !refreshedScreening.includes('项目启动结构校验通过') || !refreshedScreening.includes('被审核候选“测醛前为什么要先确认封闭时间？”') || refreshedScreening.includes('候选1') || initialTopics !== 3 || initialSelected !== 1 || homeDecisionSelects !== 0 || homeHasApproveRejectPair || detailedRejectOptions !== 0 || result.stageButtons !== 0 || result.persistentSideRails !== 0 || !composerFocused || !mobileNoOverflow || !composerContractOk) process.exit(1);
})();
