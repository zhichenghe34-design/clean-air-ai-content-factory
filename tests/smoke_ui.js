const { chromium } = require('playwright');

(async () => {
  const executablePath = process.env.CODEX_UI_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  const browser = await chromium.launch({ headless: true, executablePath });
  const baseUrl = process.env.CONTENT_FACTORY_URL || 'http://127.0.0.1:8765';
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  page.on('pageerror', err => errors.push(err.message));
  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  const demoTopic = await page.locator('#demoTopic').inputValue();
  const demoButton = await page.locator('#createDemoBtn').innerText();
  await page.screenshot({ path: 'runtime/ui-smoke.png', fullPage: true });
  await page.locator('[data-view="catalog"]').click();
  await page.locator('#view-catalog.active').waitFor();
  await page.screenshot({ path: 'runtime/catalog-smoke.png', fullPage: true });
  const installRoot = await page.locator('#hardwareFacts .wide strong').innerText();
  await page.locator('[data-view="settings"]').click();
  await page.locator('#view-settings.active').waitFor();
  const storageRoot = await page.locator('#storageRoot').inputValue();
  const storageDirectories = await page.locator('#storageDirectories > div').count();
  await page.screenshot({ path: 'runtime/storage-settings-smoke.png', fullPage: true });
  const result = {
    title: await page.title(),
    heading: await page.locator('h1').innerText(),
    pipelineNodes: await page.locator('.pipe-node').count(),
    packageCards: await page.locator('#packageGrid .package-card').count(),
    hardwareProfile: await page.locator('#hardwareProfile').innerText(),
    installPolicy: await page.locator('#installPolicyBadge').innerText(),
    installRoot,
    storageRoot,
    storageDirectories,
    demoTopic,
    demoButton,
    errors,
  };
  process.stdout.write(JSON.stringify(result));
  await browser.close();
  if (errors.length) process.exit(1);
})();
