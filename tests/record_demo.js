const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const executablePath = process.env.CODEX_UI_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  const baseUrl = process.env.CONTENT_FACTORY_URL || 'http://127.0.0.1:8765';
  const output = process.env.CONTENT_FACTORY_DEMO_OUTPUT;
  if (!output) throw new Error('CONTENT_FACTORY_DEMO_OUTPUT is required');
  const browser = await chromium.launch({ headless: true, executablePath });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: 'runtime/demo-recording', size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  await page.locator('.demo-production').scrollIntoViewIfNeeded();
  await page.waitForTimeout(5000);
  await page.locator('[data-view="jobs"]').click();
  await page.locator('#view-jobs.active').waitFor();
  await page.waitForTimeout(3000);
  const detailButton = page.locator('button', { hasText: '查看/精修' }).first();
  await detailButton.click();
  await page.locator('#jobDetailPanel:not([hidden])').waitFor();
  await page.waitForTimeout(4500);
  await page.locator('#approvedScriptInput').scrollIntoViewIfNeeded();
  await page.waitForTimeout(4000);
  await page.locator('#artifactLinks').scrollIntoViewIfNeeded();
  await page.waitForTimeout(3500);
  const video = page.video();
  await page.close();
  await video.saveAs(path.resolve(output));
  await context.close();
  await browser.close();
  process.stdout.write(JSON.stringify({ ok: true, output: path.resolve(output) }));
})().catch(error => { console.error(error); process.exit(1); });

