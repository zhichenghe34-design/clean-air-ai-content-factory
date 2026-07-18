const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const executablePath = process.env.CODEX_UI_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  const baseUrl = process.env.CONTENT_FACTORY_URL || 'http://127.0.0.1:8765';
  const outputDir = path.resolve(process.env.CONTENT_FACTORY_DEMO_FRAMES || 'runtime/demo-frames');
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await page.screenshot({ path: path.join(outputDir, '01-workbench.png') });
  await page.locator('.demo-production').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(outputDir, '02-demo-entry.png') });
  await page.locator('[data-view="jobs"]').click();
  await page.locator('#view-jobs.active').waitFor();
  await page.screenshot({ path: path.join(outputDir, '03-job-history.png') });
  await page.locator('button', { hasText: '查看/精修' }).first().click();
  await page.locator('#jobDetailPanel:not([hidden])').waitFor();
  await page.locator('#jobDetailPanel').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(outputDir, '04-human-refinement.png') });
  await page.locator('#artifactLinks').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(outputDir, '05-artifacts.png') });
  await page.locator('[data-view="settings"]').click();
  await page.locator('#view-settings.active').waitFor();
  await page.screenshot({ path: path.join(outputDir, '06-safety-settings.png') });
  await browser.close();
  process.stdout.write(JSON.stringify({ ok: true, outputDir }));
})().catch(error => { console.error(error); process.exit(1); });

